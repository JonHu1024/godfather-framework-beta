# 任务包 A：Markdown → LaTeX

## 1. 能力定义（loc 应该学什么）

基础转换交给确定性工具（Pandoc / 规则转换器）建立基线。
模型重点学习：

- 中文学术文档的约定、语义环境（定理 / 证明 / 例题）；
- 公式、表格、图片、引用与长文结构的可靠表达；
- 用户指定的排版风格；
- 对转换失败或版面缺陷做**受约束的局部修复**。

## 2. 两种任务模式（分别存数据、分别评测）

| 模式 | 说明 | 适合 |
| --- | --- | --- |
| 直接生成 | 从 Markdown 块生成 TeX 块 | 训练真正的转换模型 |
| 工具辅助 | Pandoc 先转，模型做语义环境处理 / 样式决策 / 局部修复 | 低预算起步 |

本实现对应四个生成模式：`pandoc_baseline`（工具对照）、`rule_only`（规则）、
`tool_assisted`（工具+教师）、`teacher_direct`（教师直接生成）。
**工具完成的收益不会被算到模型头上**：四种模式的 `stats.teacher` 分别记录调用数与失败数。

## 3. 教师生产流程（实现映射）

| 设计文档步骤 | 实现 |
| --- | --- |
| 1. 解析结构树，分配稳定 block_id | `taskpacks/latex/mdparse.py` |
| 2. 归集标题层级、术语、引用标识 | `mdparse.parse_markdown()` 返回的 `context` |
| 3. 检索少量相关参考，构造长度受控的输入 | `policy.build_teacher_prompt()`（参考检索待实现） |
| 4. 按结构切块，不在公式/代码/表格中间截断 | `mdparse.chunk_blocks()` |
| 5. 教师输出片段；组装器管理导言区 | `taskpacks/latex/assembler.py` |
| 6. 隔离编译，构建 PDF、日志、SyncTeX | `taskpacks/latex/compiler.py` |
| 7. 结构检查与人工抽查；失败进入有限次局部修复 | `validators.py` + 规则回退重编一次 |
| 8. 合格输出进入候选训练池；整篇仍需组装后检查 | `pipeline._record_training_example()` |

## 4. 四层质量检查

### 工程层
编译退出码、超时、缺资源（按文件名匹配）、未定义引用/引文警告、缺文件、标签重复定义。
**编译成功不代表内容正确**，所以这一层与内容层分开报告。

### 内容层
标题内容是否保留、列表条目数量、公式源串是否原样出现、缺段/重复段、
`\ref{}` 是否有对应 `\label`、中文是否整体丢失。

公式按源串比对：允许等价改写，所以只报 `warn`（"可能是等价改写，需人工确认"），
不会因为字符串不等就判错。

### 版面层
统计 `Overfull/Underfull \hbox`、`Float lost` 等日志信号；自动检测有限，
真实版面（图片比例、遮挡、表格裁切）仍需人在预览里看。

### 偏好层
把策略里的机器可判定规则（`require_package` / `require_environment` / `forbid_command`）
对照实际导言区与正文检查。审美评价不是客观真值，因此偏好层只报偏好项，不做整体美学打分。

## 5. 批注 → 数据

批注绑定 `artifact_id + artifact_revision + 页码 + 归一化坐标 + 候选源块`。

定位策略：
1. 优先用源行映射与 `block_id`；
2. 浏览器端用 PDF.js 文本层把页面文本与源块文本匹配，给出候选源块；
3. 匹配不可靠时 `mapping_confidence = needs_review`，界面要求用户确认；
   **不会让视觉模型猜一个位置去覆盖整篇 TeX**。

接受修订后的流程：

```
用户接受 → 教师按批注重写该块（只改有证据的问题）
         → 重新组装整篇 → 重新编译 → 重新四层检查
         → 通过后保存为 SFT 候选（label_origin = human_reviewed_teacher）
```

- 批注默认只作用于当前范围；多次一致反馈后可以建议升级为文档级规则，
  不会自动把一次性修正扩散为全局规则（升级动作由控制器提议、用户确认）。
- 只有原输出与修订输出在同一输入、同一适用偏好下可比较时，才可能构成偏好对
  （当前版本只记录 `accepted` 状态，不自动生成 DPO 数据）。

## 6. 组装器与行映射

`assemble_document()` 输出 `.tex` 文本与 `line_map`：`block_id → {start, end, kind}`。
行映射是"源块 ↔ 编译产物"的确定性对应关系，比视觉猜测可靠得多；
`teacher_direct` 模式下以 chunk 为单位，映射键是 `chunk-00x`。

## 7. 已知限制（如实记录）

- SyncTeX 未解析：页内定位目前靠 PDF.js 文本层匹配，置信度不足时需人工确认。
- 教师输出可能不合法：已做围栏清理、反引号清理、数学定界符配平、非法 JSON 转义修复；
  仍失败时回退到规则转换重编一次，并在 `stats.fallback_to_rules` 记录。
- 表格宽度、图片比例、跨页断行等版面问题不做自动修正，只做检测与提示。


### 教师提问与 waiting_user

教师返回的 `questions` 默认只作为警告记录，生成继续。
如果策略里打开 `validator_config.wait_on_teacher_questions: true`，
任务会进入可恢复的 `waiting_user`：

- 问题会写进「问题收件箱」（`question_cards`），用户在那里回答；
- 回答后用 `POST /api/jobs/{id}/resume` 恢复任务，生成从当前轮次继续
  （复用的还是同一个 generation 行，不会多出一行轮次记录）。
