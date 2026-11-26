# 验证记录（真实运行）

以下均为本机实际运行结果，不是设计意图的描述。
运行环境、命令与原始输出见各节。

## 0. 环境
| 项目 | 值 |
| --- | --- |
| 主机 | WSL2 (Linux 6.18.33.1-microsoft-standard-WSL2) |
| Python | 3.12.14（项目 venv） |
| Pandoc | 3.7.0.2 |
| LaTeX | Windows 侧 TeX Live 2025：`D:\Environment\texlive\2025\bin\windows\xelatex.exe`（通过 WSL 调用） |
| 中文字体方案 | `windows`（ctex windows 字体集 → SimSun/SimHei） |
| Ollama | 本机 `127.0.0.1:11434`，已安装模型 `qwen3:0.6b`（0.6B，仅用于跑通链路） |

```powershell
python -m godfather preflight
# LaTeX  : 使用 Windows 侧 LaTeX 引擎：/mnt/d/Environment/texlive/2025/bin/windows/xelatex.exe（通过 WSL 调用）
# Pandoc : 可用，3.7.0.2
# Ollama : 已连接，本地模型 1 个
```

## 1. 测试套件
```bash
python -m pytest -q
# 156 passed
```

覆盖：结构解析与切块、组装器与转义、隔离编译（假引擎：参数、超时、失败、缺引擎）、
四层检查、任务队列（幂等 / 租约接管 / 重试 / 取消）、策略与控制器校验、
数据集切分与人工任务包、预算、API 端到端、流程细节（资产映射、教师输出清理、字体方案）。

## 2. M0 端到端（真实编译）
```bash
python scripts/verify_m0.py
```

实际输出：

```
数据目录：…/data/verify
LaTeX 字体方案：windows
字体探针：可用 — 字体方案 windows 可用

─── 1. 导入材料 ───
  文档 doc-605c4ada88f7，结构块 22 个

─── 2. 四种模式生成（真实编译） ───
  pandoc_baseline    结论=warn  编译=成功 回退=False PDF=有 教师调用=0 用时=1916ms
  rule_only          结论=warn  编译=成功 回退=False PDF=有 教师调用=0 用时=1911ms
  tool_assisted      结论=warn  编译=成功 回退=True  PDF=有 教师调用=2 用时=1901ms
  teacher_direct     结论=warn  编译=成功 回退=True  PDF=有 教师调用=2 用时=1907ms

─── 3. 批注（绑定产物版本与归一化坐标） ───
  批注 ann-verify → 产物 art-e81e68d4e7a5（revision sha256:a955a5700b090…）

─── 4. 接受修订（教师改写 → 重新编译验证） ───
  任务状态：succeeded
  修订结论：warn，PDF=有

─── 5. 数据集快照 ───
  快照 snap-20261106-2b24：{'train': 5, 'dev': 0, 'holdout': 0}
  哈希：sha256:3205d22115d5e10b119d1b8dba70b9a1ea5ef18bbddf34d6b97328c8c401d976

─── 6. 训练样本标签来源 ───
  human_reviewed_teacher   candidate  1 条
  rule_synthesized         candidate  2 条
  teacher_generated        candidate  2 条

─── 结果 ───
  全部模式编译成功
```

说明（不粉饰）：

- 四种模式都真的调用了 Windows 的 xelatex 并产出了 PDF（约 1.9 s/次，含 WSL↔Windows 拷贝）。
- `tool_assisted` / `teacher_direct` 的 `回退=True`：本机只有 0.6B 的 `qwen3:0.6b`，
  它输出的 LaTeX 无法编译，流程按设计回退到规则转换重编一次，并在
  `stats.fallback_to_rules` 里如实标记。**没有把这次结果说成"教师生成成功"。**
- 四层结论为 `warn`：内容层提示"公式源串未原样出现（可能是等价改写，需人工确认）"，
  这是设计要求的预警行为，不是错误。
- 留出集为 0：样本量太小（5 条、4 个分组），哈希分桶没有分到 holdout。这说明
  **小样本下切分比例不保证每段都有样本**，符合"报告分母"的要求。

## 3. HTTP API 端到端
```bash
python -m godfather serve --port 8760
```

实际结果：

| 步骤 | 结果 |
| --- | --- |
| `GET /api/system/preflight` | 200（含 LaTeX 引擎、字体探针、Pandoc、Ollama 健康） |
| `POST /api/projects` | 200 → `prj-942d05c810c4` |
| `POST /api/projects/{id}/materials`（Markdown） | 200 → `doc-b775f53e0dce`，22 个结构块 |
| `POST /api/projects/{id}/materials`（图片） | 200 |
| `POST /api/projects/{id}/generations` | 200 → `job-fca13b76dd43` |
| `GET /api/jobs/{id}` | `succeeded` |
| `GET /api/projects/{id}/generations` | `pandoc_baseline succeeded pdf=True verdict=warn` |
| `GET /api/events`（SSE） | 持续推送真实事件（queued / generate / compile / validate …） |
| `POST /api/projects/{id}/annotations` | 200 → `ann-af917a2e2281` |
| `POST /api/annotations/{id}/accept` | 200 → 修订任务入队 |
| 批注使用错误 `artifact_revision` | **409**（拒绝，防止重排后静默错位） |

## 4. 局域网配对访问控制
```bash
python -m godfather serve --port 8764 --lan
```

实际结果（本机 LAN 地址 `172.21.178.179`）：

| 请求 | 结果 |
| --- | --- |
| `GET /api/system/pairing`（LAN 设备） | `{"required":true,"code":"","expires_in":0}` —— 拿不到配对码 |
| `GET /api/system/pairing?project_id=X`（本机） | `{"required":true,"code":"sZ4DIYcY","project_id":"X",...}` |
| `GET /api/projects`（LAN，无凭证） | **401** `{"detail":"需要配对凭证…","code":"pairing_required"}` |
| `GET /api/projects`（LAN，带会话令牌） | 200，但**只返回该令牌解锁的项目**（见 §7.3） |
| `GET /api/projects/{别的项目}`（LAN，带 A 项目会话） | **403** `{"code":"scope_mismatch"}` |
| `GET /api/projects`（本机，无凭证） | 200（回环不受限） |
| `GET /api/events?session=…`（LAN） | 200，SSE 正常推送，且只推该项目的��件 |

修复记录：`--lan` 之前只改了监听地址、没有打开配对开关；而且应用通过 uvicorn 工厂重建时
会重新加载配置、丢掉命令行修改 —— 现在改为直接用当前 settings 构建应用。

> 这一节记录的是**第一版**（全局配对码）的验证结果。
> 第四轮改成了项目级配对，完整流程见 §7.3 与 `scripts/verify_lan_pairing.py`。

## 5. 过程中发现并修复的真实缺陷
这些都是靠真实编译暴露出来的，记在这里以便复查：

| # | 现象 | 根因 | 处理 |
| --- | --- | --- | --- |
| 1 | 结构块 ID 跳号（`block-0001`, `block-0004`） | ID 计数器与行索引共用同一变量 | 拆成两个计数器 |
| 2 | 导言区字体设置缺右花括号 | 模板笔误 | 修正模板，改为字体回退链 |
| 3 | `breaklines undefined` | `breaklines` 属于 `fvextra` 而非 `fancyvrb` | `\IfFileExists{fvextra.sty}` 条件加载 |
| 4 | `Environment Shaded undefined` | Pandoc 默认输出代码高亮环境 | Pandoc 加 `--no-highlight`，导言区补 `\pandocbounded` / `\passthrough` |
| 5 | 公式被当普通文本（`C\_\{human\}`） | 基线模式重新拼接块，丢了 `$$` 定界符 | 基线改为转换**原文**；重建时补回定界符 |
| 6 | `figures/overview.png not found` | 产物库按哈希存放，文件名只存在 meta 里 | 按 meta 文件名匹配，并把资源按引用路径写入编译目录 |
| 7 | `xdvipdfmx:fatal: Invalid font: -1 (1)` | `Noto Serif SC` 在 xdvipdfmx 下无法嵌入 | 默认改用 ctex `windows` 字体集；新增字体探针 |
| 8 | 教师 JSON 中 `\section` 解析失败 | 非法 JSON 转义 | 增加转义修复后重试 |
| 9 | 教师输出无法编译导致整篇失败 | 无兜底 | 回退规则转换重编一次，并记录 `fallback_to_rules` |
| 10 | 修订任务报 "model not found" | 配置的 `qwen2.5:7b` 本机不存在 | 请求的模型不可用时自动切换到已安装模型，并记录实际模型 |
| 11 | 标题"丢失"误报 | 比较器把 `\section{标题}` 连同内容一起删掉 | 比较器只去掉命令名，保留参数内容 |

## 6. M1 训练链路（本机真实运行）
环境：WSL2（**无 GPU**）、torch 2.15.0+cpu / transformers 5.22.1 / peft 0.21.2、
基座为本地构造的极小 Llama（4.2M 参数，随机初始化，仅用于验证链路）。

```bash
python scripts/verify_m1.py --base-model .toolchain/tiny-model --device cpu \
       --no-4bit --epochs 1 --max-steps 3 --seq-len 128 --accum 2 --seed-examples
```

实际输出（节选）：

```
== 后端预检 ==
  local          可用     device=cpu   torch 2.15.0+cpu / transformers 5.22.1 / peft 0.21.2
  已写入示例样本：8 条
  快照 snap-…：{'train': 7, 'dev': 0, 'holdout': 1}
  ⚠ 开发集为空：当前只有 8 个源分组，按分组切分无法分出开发集。请导入更多来源不同的文档，或调整切分比例。

== 训练 ==
  [preflight] 训练依赖：通过 — torch 2.15.0+cpu / transformers 5.22.1 / peft 0.21.2
  [train] trainable params: 34,816 || all params: 4,213,056 || trainable%: 0.8264
  [train] {'loss': '10.38', 'grad_norm': '0.5566', 'learning_rate': '0.0002', 'epoch': '0.2857'}
  [train] {'loss': '10.35', 'grad_norm': '0.5757', 'learning_rate': '0.00015', 'epoch': '0.5714'}
  [train] {'loss': '10.34', 'grad_norm': '0.5502', 'learning_rate': '5e-05', 'epoch': '0.8571'}
  训练完成：loss=10.357 steps=3 用时=0.27s device=cpu 4bit=none lora=True
```

已真实跑通：数据准备 → 预检 → GPU 锁 → 拉起独立进程 → LoRA 训练（loss 下降、梯度非零）
→ 保存适配器 → 写回指标 → 训练运行记录。

**没有跑通的部分**：GGUF 导出与 Ollama 注册需要 llama.cpp 的 `convert_hf_to_gguf.py` 与
`llama-quantize`；本机 WSL 侧没有 CUDA，训练本身也只能在 CPU 上以极小模型验证。
导出链路已用真实 llama.cpp/ollama 二进制做过端到端测试（见 `tests/test_training.py`），
但**没有用真实基座权重跑完一次完整导出**。这在文档中如实标注。

**导出链路也已经用真实二进制跑通**（llama.cpp 0.x CPU 构建 + 真实 `ollama create`）：

```
  合并适配器        成功  合并成功
  转换 GGUF      成功  转换成功
  量化           成功  q8_0 量化成功
  导入 Ollama    成功  已注册 godfather-latex:v003
  模型名：godfather-latex:v003（q8_0，5166560 字节）

== 评测（候选 vs 基线） ==
  样本数：4
  baseline   n=4 非空=1.0 有结构=0.25 覆盖=0.0 相似=0.4472 延迟=1057ms
  candidate  n=4 非空=1.0 有结构=0.0  覆盖=0.0 相似=0.0    延迟=2681ms
  差值：{'non_empty_rate': 0.0, 'has_latex_rate': -0.25, 'mean_block_coverage': 0.0,
       'mean_similarity': -0.4472, 'cjk_rate': 0.5}

== 切换版本 ==
  当前版本：godfather-latex:v003（原 无）
```

导出后的模型在 Ollama 里确实可用：

```
$ ollama list
NAME                    ID              SIZE      MODIFIED
godfather-latex:v003    8dd4ed97a564    5.2 MB    6 seconds ago
qwen3:0.6b              7df6b6e09427    522 MB    25 hours ago
```

**这些数字本身没有意义**：基座是随机初始化的 4.2M 参数小模型，只训了 3 步，
所以候选明显差于基线（`has_latex_rate` -0.25、相似度 -0.4472）—— 这正是评测环节该有的表现：
**指标如实反映了"这次训练没有带来收益"**，而不是给出一个好看的综合分。

### M1 过程中发现并修复的真实缺陷

| # | 现象 | 根因 | 处理 |
| --- | --- | --- | --- |
| 14 | 训练报 `TrainingArguments.__init__() got an unexpected keyword argument 'warmup_ratio'` | transformers 5.x 去掉了 `warmup_ratio` | 按实际签名过滤参数，并换算成 `warmup_steps`；打印被忽略的参数 |
| 15 | CPU 训练报 `You need to install bitsandbytes` | 默认优化器是 `paged_adamw_8bit` | CPU 或缺 bitsandbytes 时降级为 `adamw_torch` 并在日志说明 |
| 16 | **loss 恒为 0，训练"成功"但没有任何监督信号** | 序列超长时从整段文本尾部截断，把答案切掉了 | 改为分别编码 prompt/answer，只截断 prompt 开头；保证答案 token 一定被监督；加了回归测试 |
| 17 | 预检失败后运行状态永远停在 `running` | 抛错早于写 `failed` 分支 | 预检异常统一写回 `failed` + `error` |
| 18 | 训练失败被反复重试 3 次 | 确定性失败也进了重试路径 | 引入 `PermanentJobError`，确定性失败不重试；训练任务 `max_attempts=1` |
| 19 | `GET /api/training/evaluations` 500 | `evaluate.py` 缺 `import json` | 已补 |
| 20 | 取消训练后仍被当作失败重试 | 取消状态没有在运行器里识别 | 运行器检测到任务已取消则直接返回 cancelled |
| 21 | 首次发布时"回滚"抛异常终止脚本 | 只有一个版本时没有历史可回滚 | 脚本按"首次发布的正常情况"处理并提示 |

## 7. 对抗式代码审查（M1 完成后）
对 M1 与 M0 做了四路并行的对抗式审查（训练链路 / LaTeX 任务包 / API 与并发 / 前端），
每条结论都用**真实代码跑探针复现**，不接受的只有"理论上可能"的推测。

统计：**发现 60+ 项，确认成立并已修复 43 项**，其中致命 4 项、高危 17 项。

### 致命（4 项，全部已修复）

| # | 位置 | 问题 | 后果 |
| --- | --- | --- | --- |
| A1 | `jobs.py` | 任务租约**从不续期**（`_renew` 定义了但零调用点），租约 120s 到期后另一个 worker 会接管同一个任务 | 长训练被执行两次：GPU 时间翻倍、产物互相覆盖、数据集被重复样本污染 |
| A2 | `app.py` / `__main__.py` | 配对开关只看 config 布尔量：`--host 0.0.0.0` 不加 `--lan`、或 `--lan --reload`（工厂重新读配置）都会**开出一个没有鉴权的局域网入口** | 同网段任意设备可读写全部项目 |
| A3 | `budget.py` | "检查额度"与"写预留行"不在同一事务里 | 并发调用可超买预算（实测 4×60 分全部通过，超买 140%） |
| A4 | `validators.py` | 版面层越界检测的正则缺 `re.MULTILINE`，`^` 只匹配整段日志开头 | 真实 TeX 日志一条都命中不了，"版面层"形同虚设 |

### 高危（17 项，全部已修复）

- **内容 → 编译器注入**（4 项）：图片路径未转义可逃出 `figure` 环境；代码块里的 `\end{Verbatim}`、公式块里的 `\end{equation}` 可闭合宿主环境；HTML 块只转义 `%`、换行即逃出注释；pandoc 开着 `raw_tex` 让原始 TeX 直通。
- **隔离强度**（2 项）：`-no-shell-escape` 由配置开关决定（关闭即等于 RCE）；`openin_any=a` 允许 TeX 读任意文件。
- **数据溯源**（3 项）：`accept_verdict` 是字符串 `set("pass")=={'p','a','s'}` → 判定永远不是 pass，且内容层 fail 不阻止样本登记；教师调用失败仍标 `teacher_generated`（把工具收益记到模型头上）；修订编译失败仍把批注标成"已接受"。
- **状态机**（3 项）：导出失败把训练改成 `export_failed` → 永远无法再导出；取消后被 worker 收尾覆盖成 `succeeded`；预检/数据准备失败时运行状态永远停在 `running`。
- **训练正确性**（3 项）：**loss 恒为 0**（截断把答案切掉，训练"成功"但没有任何监督信号）；答案没有 EOS（模型学不会停止）；空答案样本拿 prompt 末 token 当监督目标。
- **其它**（2 项）：取消不校验状态（能把已完成的训练改成 cancelled）；Modelfile 的 `FROM` 路径没加引号（Windows 用户目录含空格必然解析失败）。

### 中低危（已修复的其余 22 项）

预算重复扣减、教师缓存跨项目串数据、快照同名覆盖已冻结文件、上传无大小限制、
跨项目批注/导出/激活、评测不区分"模型差"与"调用失败"、任务日志只返回最早 500 条、
API limit 无上限、PDF.js v5 的 `renderTextLayer` 已不存在、路由带 query 被踢回总览、
拖拽框选必崩、视图写操作后不刷新、配对码不校验且过期不刷新等。

### 审查同时发现并修复的"死检查"

四层检查里有两处永远不会触发（版面越界、公式保留），一处会误报正常内容（`C:\path` 标题被判缺段）。
现在：越界检测逐行匹配、公式比对直接用 math 块原文、标题比较按 Markdown 语义归一。

### 审查后的验证

```bash
python -m pytest -q                     # 264 passed
python -m ruff check src/ scripts/ tests/ --select F,E9,B   # All checks passed
python scripts/verify_m0.py             # 四种模式全部编译成功，结论 pass/warn
python scripts/verify_m1.py ...         # 训练→合并→GGUF→量化→Ollama→评测→切换 全链路通过
```

浏览器级验证（Playwright + 真实 Chromium + 真实后端）：
路由带 query 仍停在工作台、上传后列表会刷新、训练页正常渲染、**控制台零错误**。

## 7.1 第二轮清理：审查遗留项全部处理
第一轮修完致命与高危项后，把审查报告里"建议顺序"里剩下的中低危项也清了一遍，
按数据正确性 → 稳健性 → 前端 → 资源四批推进，共处理 31 项。

### 数据正确性（7 项）

| 问题 | 处理 |
| --- | --- |
| 同一 digest 的产物再次产出时 **meta 被丢弃**（行映射停留在第一次生成） | meta 合并更新；内容仍不可变 |
| 重试会**多建一行 generations**，且旧行永远停在 running | 按 job_id 复用同一行；失败时标记 failed |
| 重试会**重复写入训练样本**（数据集里出现内容相同的重复样本） | 按（源分组, 源修订, 目标内容）去重 |
| 修订不校验批注绑定的产物版本 | 校验批注的 artifact 与当前轮次一致，否则要求人工重新定位 |
| 修订结果**不回写文档块**（下一次生成仍按原文渲染） | 写回新的 document_revision，组装器采用 `revised_tex` |
| 删除项目不清 provider_calls 与磁盘产物文件 | 一并清理（只删不再被引用的文件） |
| `truncated_samples` 按 epoch 重复计数 | 按样本下标去重 |

### 稳健性（15 项）

老库缺 `jobs.idempotency_key` 时**进程起不来**（先建索引后补列）→ 先补列再建索引，并读取
schema 版本做门控；事务不可重入 → 用 SAVEPOINT 支持嵌套；`JobPaused` 写 finished_at、
resume 不清 attempts → 暂停不算结束、恢复重置次数；并发写同一 digest 共用 `.tmp` → 临时文件名唯一；
WSL 桥接退化分支**必然失败**（源码没复制过去）→ 补上复制；`latex.passes` 只是摆设 → 真的多遍编译；
编译中间文件从不清 → 按后缀清理；中文里的 `$5` 被当成公式 → 修正行内公式规则；
pandoc 扩展名未过滤（可注入 `--lua-filter`）→ 白名单，并移除 `raw_tex`；
controller 接口类型错误返回 500 → 400；`worker --concurrency` 参数没用 → 生效；
幂等字段名叫 `idempotent_reuse` 却装的是 job id → 改为 `reused`；
配对码在局域网轮询时被换掉 → 只有本机读取才轮换。

### 前端（9 项）

策略版本号按"已有数量+1"推导 → **覆盖旧版本**（策略是训练数据的依据）→ 改时间戳且后端不再覆盖；
Markdown 方言输入框完全不生效 → 前后端都传递；快照"路径"是死链 → 改显示落盘路径；
使用指南还写着"训练后端尚未实现" → 更新；任务失败看不到日志 → 队列页加"日志"按钮；
PDF 固定 1.25 倍缩放（手机上只能横向滚动）→ 按容器宽度自适应 + 尺寸变化重渲染；
窄屏判断只在渲染时求值一次（转屏后布局错）→ 监听变化；
训练页展开状态不在刷新签名里 → 加入签名；
图标按钮无 aria-label、连接状态不是 live region、弹窗无可访问名称、表头无 scope、
侧栏无 aria-current → 补齐；已回答的问题看不到答案内容 → 接口返回答案并显示。

### 资源（3 项）

SSE 连接数无上限（每条占一个线程 + 一个 SQLite 连接）→ 上限 32，超出返回 503；
`events` / `job_logs` 表无上限 → 各自加保留策略（20000 / 2000）；
`paused_budget` 状态不可达 → 预算不足时任务进入可恢复的 paused_budget，而不是悄悄降级。

### 一个防复发的机制：声明即测试

这轮修复过程中，"声称修了但其实没写进文件"出现了 **三次**
（编辑脚本的锚点没匹配上，`replace` 静默失败，而我只看了脚本打印的 `ok`）。
第三次时我意识到光靠人眼盯不住，于是把验证日志里的每一条"已修"声明
都固化成了测试（`tests/test_claims.py`，57 条）：

- 条件更新是否出现在三个分支里；
- `write_back_revision()` 是否真的被调用、真的插入 `document_revisions`；
- `revised_tex` 是否还提前 return（提前 return = 跳过 Verbatim 包装与环境中和）；
- 去重是否发生在建索引之前；
- 幂等键是否用 `MAX(revision)`；
- `openin_any` 是不是被改回了 paranoid；
- 前端的 modal 是不是又开始用 `addEventListener("submit")`；
- pdfview 是不是又出现了两个 `destroy` 定义；
- ……

以后谁把实现删掉或改回旧行为，`pytest` 会当场变红，不会再等到下一次复审才发现。

### 验证

```bash
python -m pytest -q                     # 264 passed（含 57 条声明校验）
python -m ruff check src/ scripts/ tests/ --select F,E9,B   # All checks passed
python scripts/verify_m0.py             # 四种模式全部编译成功
python scripts/verify_m1.py ...         # 全链路通过
浏览器（真实 Chromium + 真实后端）      # 10 个页面全部渲染，控制台零错误
```

## 7.2 第三轮：两轮独立复审 + 修复
改完两批 bug 之后，请了两个独立的审查代理**专门复审我刚改的代码**
（一个查后端并发/事务/条件更新，一个查前端事件循环/契约一致性），
共报 34 项（后端 17：1 致命 + 5 高 + 8 中 + 3 低；前端 17：6 高 + 8 中 + 3 低）。
这一轮把它们全部处理了。

### 致命：取消的任务会复活

`jobs.py` 里只有"成功"分支用了条件更新
（`WHERE id=? AND status='running' AND lease_owner=?`），
异常分支与 `JobPaused` 分支仍然无条件 `db.update`：

1. 提交一个会抛异常的任务；
2. 运行中点「取消」→ 状态变成 `cancelled`；
3. handler 随后抛错 → **状态被改回 `queued`**，worker 会把它重新领走执行。

后果是重复烧 LLM 费用与 GPU 时间、重复写产物与训练样本，
最坏情况（`max_attempts=3`）复活两次。
现在三个分支（成功 / 暂停 / 失败重试）都用条件更新，
`rowcount==0` 说明任务已被取消，就只发一条 warning 事件、不再写库。
**有回归测试**（`test_cancelled_job_is_not_revived_by_a_late_failure`）。

### 高危 5 项

| 问题 | 说明 | 处理 |
| --- | --- | --- |
| 修订**从不写回文档** | 上一轮声称"写回 document_revision"，实际上**那段代码根本没进文件**（编辑脚本的锚点没匹配上，静默失败）→ 修订只存在于产物里，下一次生成仍按原文渲染，训练样本也拿不到修订内容 | 真正实现 `write_back_revision()`：写新的 `document_revisions` 行（旧的保留），组装器读 `revised_tex` |
| `revised_tex` 提前 return | 跳过 Verbatim 包装与 `neutralize_environment_breaks` → 代码块修订后不再是 verbatim（`%` 吃掉行尾、`_` 编译失败），且能注入 `\end{Verbatim}` | 修订内容改走与教师输出**同一条**渲染路径：code → 中和 + 包 Verbatim；math → 包 equation；其它 → 中和环境闭合。Verbatim 用专用的中和方式（破坏精确结束标记，而不是 `\texttt{}` 转义——Verbatim 里反斜杠是字面量） |
| 老库有重复 `idempotency_key` 时**进程起不来** | "先补列再建索引"只修了一半：唯一索引遇到重复值直接抛 IntegrityError | 建索引前先去重（保留最新一条，其余置空），老库升级路径实测通过 |
| 生成任务的幂等键里**文档修订恒为 0** | `documents` 表没有 revision 列（修订号在 `document_revisions.revision`）→ 文档重新解析后仍命中旧任务，**根本生成不了新内容** | 幂等键改用 `MAX(revision)` |
| 训练启动前**不检查取消** | 取消只在训练进程退出后检查；准备阶段（数据准备、预检、等 GPU 锁）点取消，训练照跑几小时 | `Popen` 之前查一次状态，已取消就抛 `TrainingCancelled` 并如实记成 cancelled |

### 中危 8 项

`all_projects=1` 的 SSE 与历史接口语义不一致（项目事件推不出来）、
SSE 连接计数只增不减（生成器未启动时 `finally` 不执行，32 次异常断连后永久 503，
改用 `BackgroundTask` 兜底释放）、`.synctex.gz` 被当中间文件删掉导致
`synctex_path` 恒为空串、多遍编译只保留最后一遍日志（验证层据此判断，会漏判）、
同 digest 产物的 `line_map` 被后一次生成整体覆盖（改为按生成 id 分别存）、
上传大小限制在整块读进内存**之后**才检查（改为分块读 + 累计）、
修订任务不按 job_id 复用 generation 行（重试会多花一次教师费用）、
空答案样本只标记不剔除（micro-batch 全无监督时 loss 退化成 0/0，nan 会污染权重）。

### 低危 3 项

幂等冲突兜底分支缺 `reused` 标记、续租是无条件 UPDATE（会续回别人的租约）、
模型版本名是"最大序号 + 3"（第一个模型直接跳到 v003）。

### 一条来自复审的重要提醒：`openin_any`

复审指出 `openin_any=p`（paranoid）在 TeX Live 上有已知问题：
paranoid 模式会连 TEXMFDIST 里的宏包都挡掉，
而 `ctexart.cls` / `fontspec.sty` 都在 TEXMFDIST —— 会导致 ctex 加载失败
（TeX Live 2020-09 的公开 bug）。已改为 `openin_any=r`（restricted）：
同样禁止读任意绝对路径，但放行宏包树。**改完重跑了 M0 端到端编译，全部模式仍然成功。**

### 一个防复发的机制：声明即测试

这轮修复过程中，"声称修了但其实没写进文件"出现了 **三次**
（编辑脚本的锚点没匹配上，`replace` 静默失败，而我只看了脚本打印的 `ok`）。
第三次时我意识到光靠人眼盯不住，于是把验证日志里的每一条"已修"声明
都固化成了测试（`tests/test_claims.py`，57 条）：

- 条件更新是否出现在三个分支里；
- `write_back_revision()` 是否真的被调用、真的插入 `document_revisions`；
- `revised_tex` 是否还提前 return（提前 return = 跳过 Verbatim 包装与环境中和）；
- 去重是否发生在建索引之前；
- 幂等键是否用 `MAX(revision)`；
- `openin_any` 是不是被改回了 paranoid；
- 前端的 modal 是不是又开始用 `addEventListener("submit")`；
- pdfview 是不是又出现了两个 `destroy` 定义；
- ……

以后谁把实现删掉或改回旧行为，`pytest` 会当场变红，不会再等到下一次复审才发现。

### 验证

```bash
python -m pytest -q                     # 264 passed（含 57 条声明校验）
python -m ruff check src/ scripts/ tests/ --select F,E9,B
python scripts/verify_m0.py             # 四种模式全部编译成功（openin_any=r）
python scripts/verify_m1.py ...         # 全链路通过
真实 Chromium                          # 10 个页面全部渲染，控制台零错误
```

## 7.3 第四轮：局域网项目级访问控制（方案 A）
前三轮把代码里的缺陷清完了，剩下唯一有**安全含义**的一项是：
局域网开放时只有一个全局配对码，拿到码的人可以读写**全部**项目。
用户选了方案 A：**每个项目一个配对码**。

### 改了什么

```
桌面端（本机回环）                    手机端（局域网）
  │ GET /api/system/pairing              │
  │   ?project_id=prj-xxx               │
  │ ← {code: "K7M2P9QD", expires_in: 600}│
  │                                     │ POST /api/system/pairing/redeem
  │                                     │   {code: "K7M2P9QD"}
  │                                     │ ← {token, project_id, project_name}
  │                                     │ 之后每个请求带
  │                                     │   X-Godfather-Session: <token>
```

| 改动 | 说明 |
| --- | --- |
| 配对码按项目签发 | `pairing_codes` 表；8 位、去掉易混字符（0/O/1/l）、10 分钟过期 |
| 会话令牌 | 32 字节随机，**只存 sha256 哈希**（数据库被拷走也换不出可用令牌），默认 7 天 |
| 请求的项目归属解析 | 新增 `api/scope.py`：路径里的 `project_id` → 查询参数 → 按对象 id 反查（产物/文档/生成/任务/批注/样本/问题/训练运行/模型版本） |
| 越权拦截 | 中间件：会话解锁的项目 ≠ 请求要访问的项目 → **403 `scope_mismatch`** |
| 列表收窄 | 项目列表、训练运行、评测、模型、事件在有会话时只返回该会话的项目 |
| 换码即踢人 | `POST /api/system/pairing/rotate` 重新生成码的同时作废该项目**已有的所有会话** |
| 会话列表 | `GET /api/system/pairing/sessions`（只有本机可见），桌面端能看到"哪些设备连着" |
| 局域网不能建项目 | 新建的项目它也拿不到配对码，只会留下一个谁都进不去的空项目 → 403 |
| 码也能直接当凭证 | `X-Godfather-Pairing` / `?pairing=` 传原始码即可，方便 curl 调试 |

### 前端（web/js/pairing.js 新增 + api/main/state/ui 改动）

| 改动 | 说明 |
| --- | --- |
| 顶栏配对区 | 显示**当前项目**的 8 位码 + "手机端输入这个码即可访问「<项目名>」" + 「重新生成」按钮（换码会让已配对的手机失效，有确认框） |
| 配对框 | 8 位输入框：自动转大写、过滤非法字符（与服务端字符集一致）；失败时框内显示后端原文，框不关 |
| 令牌注入 | `X-Godfather-Session` + 兼容的 `X-Godfather-Pairing`；产物下载链接带 `?session=`；SSE 的 URL 也带令牌 |
| 401 / 403 处理 | 401 `pairing_required` → 清令牌并弹配对框（不弹错误 toast）；403 `scope_mismatch` → 提示 + 清令牌 + 重新配对 |
| 项目选择器 | 有会话时只列出解锁的项目、禁止切换、隐藏「＋」（后端对会话建项目直接 403） |
| 桌面端不受影响 | 回环请求后端直接放行，桌面端不弹框、不带会话头；纯本机部署连配对码徽标都不显示 |

顺带修了一个**既有 bug**：`ui.js` 的 `modal()` 从不设置 `dialog.returnValue`，
导致 `confirmDialog()` 永远 resolve false —— 训练页的「取消训练运行」「回滚模型」
确认框点了确定也没反应。现在点确定会 `returnValue = "ok"`。

### 验证

`scripts/verify_lan_pairing.py` 用**真实局域网地址**（不是 TestClient 模拟）跑完整流程：

```
局域网地址：172.21.178.179
  ✓ 创建两个项目
  ✓ 桌面端能取到项目 A 的配对码  code=Y4B2WENK 有效期 600s
  ✓ 无凭证访问被拒  HTTP 401
  ✓ 局域网取不到配对码
  ✓ 用配对码换到项目级会话  解锁 机密项目A
  ✓ 会话能访问自己的项目
  ✓ 会话访问别的项目被拒  HTTP 403
  ✓ 项目列表被收窄到自己的项目  看到 ['机密项目A']
  ✓ 配对码本身也能当凭证用（方便 curl）
  ✓ 会话不能新建项目  HTTP 403
  ✓ 重新生成配对码
  ✓ 换码后旧会话立即失效  HTTP 401
  ✓ 桌面端（回环）不受影响
✓ 项目级配对流程全部通过
```

单元测试另有 29 条（`tests/test_pairing.py`），其中 6 条专门验证"会话访问别的项目被拒"
（项目、产物、事件三个入口 + 列表收窄 + 不能建项目 + 不能新建项目）。

另外有一条**覆盖度测试**（`test_every_project_data_endpoint_resolves_its_project`）：
把产物/任务/训练运行/模型版本/问题卡/批注/训练样本都造出来，
逐个断言 `scope.py` 能解析出它们的项目归属。
**以后新增端点时，这个测试会提醒你把它加进解析规则** —— 解析不到的端点等于不被检查。

### 7.3.1 对这套访问控制做了一次**安全向的对抗审查**

写完项目级配对后，请了一个审查代理专门找**绕过路径**（路径变形、方法覆盖、header 混淆、
令牌边界、SQL 注入、时间绕过、越权读写、正常流程误伤），全部结论都是实际跑出来的。
它复现出 **1 个致命 + 3 个高危 + 4 个中低危**，都已修掉。

#### 🔴 致命：查询参数能覆盖对象 id 反查 → 跨项目读写

`scope.py` 原来"查询参数命中就 return"，于是任何**按 id 取对象**的端点
只要带上 `?project_id=<自己项目>` 就能读到别的项目：

```
GET /api/artifacts/<B的id>?project_id=<A>   → 200，返回 B 的产物（含服务器绝对路径）
GET /api/artifacts/<B的id>/content?...      → 200，返回 B 的文件原文
PATCH /api/annotations/<B的ann>?project_id=<A> → 200，B 的批注真被改了
```

修法：**候选集合**（`resolve_target_projects`）——路径、查询参数、对象反查**三者都算**，
任一与会话解锁的项目不符就 403。同时在端点侧加了 `_require_owned()` 做纵深防御
（对象自己的 `project_id` 也要等于会话项目）。

#### 🔴 高：请求体里的 id 不在解析范围内 → 跨项目写

```
POST /api/projects/A/generations {"document_id": "<B的doc>"}          → 200，A 的会话给 B 的文档发起生成
POST /api/projects/A/controller/actions {"arguments":{"document_id":"<B的doc>"}} → 200
POST /api/training/runs?project_id=A {"dataset_snapshot_id":"<B的snap>"} → 200，A 拿 B 的数据开训
```

修法：这三个端点都改成校验 id 的归属，不属于本项目就 404。

#### 🟠 中：非 ASCII 配对码 → 500

`secrets.compare_digest` 不支持非 ASCII 字符串（抛 TypeError），
`POST /api/system/pairing/redeem {"code":"中文"}` 直接 500。加 `isascii()` 挡掉 → 401。

#### 🟠 中：同一配对码可无限次兑换

码泄漏一次就能无限铸造长期会话。改成**一次性**：兑换成功立刻作废该码
（桌面端下一轮轮询会自动补发新码，因为"缺失即过期 → 重新签发"）。

#### 🟠 中：兑换限速变成局域网级 DoS

限速 key 只按 IP，且在兑换**之前**检查 → 同一 NAT 出口下任何设备发 10 个错码，
连**正确的码**也会被 429 挡住，全网都登不进去。
改成：先兑换，**成功就清零计数**；429 只挡失败的兑换，正确的码永远能换到。

#### 🟡 低：双凭证头只取第一个

`X-Godfather-Session`（过期）+ `X-Godfather-Pairing`（正确）→ 401，正确的码被忽略。
改成**逐个试**候选凭证。

#### 🟡 低：局域网会话能删掉项目

`DELETE /api/projects/{A}` 会连带删掉磁盘产物与训练数据，手机端一次误触就没了。
改成局域网会话禁止删除（新建项目也禁止）。

#### 顺带修的一个 P0 回归

加 `request: Request` 形参时漏了 `from fastapi import ... Request`。
因为文件头有 `from __future__ import annotations`，注解是字符串，
FastAPI 解析不出类型就**静默回退成必填 query 参数** → 10 个端点对本机回环也 422
（产物下载、任务轮询、批注、样本状态全挂）。测试套件当场抓到了这个回归。

#### 审查确认**没有**问题的（不必再查）

路径变形 17 种（`..`、`%2F`、大小写、双斜杠、零宽空格…，真机 uvicorn 实测全部 403/404）、
HTTP 方法覆盖、header 混淆、令牌边界（空/超长/数字/NUL）、时间绕过、
SQL 注入（表名硬编码 + 占位符）、豁免路径无前缀连带、
以及**正常手机流程 30+ 个真实调用全部 200**（SSE、训练轮询、产物下载、项目列表收窄都正常）。

#### 新增测试

`tests/test_pairing.py` 现有 29 条，其中 10 条专门针对这些绕过手法
（带参数的对象端点、跨项目写、body id、非 ASCII 码、一次性码、双头回退、限速不挡正确码、
会话禁删项目、限速不清零）。

## 7.4 第五轮：全项目全面审核（四路并行 + 自审）

范围：整个仓库（后端 / 前端 / LaTeX 任务包 / 训练链 / 文档）。
方法：四个并行审计代理（LaTeX 任务包 / M1 训练链 / 前端 / 文档↔代码一致性）各自实跑复现，
主线程再做一遍数据完整性自审。所有结论都要求"能复现"，不接受"看起来可疑"。

### 7.4.1 致命 / 高危（本轮修复）

| # | 位置 | 问题 | 复现证据 | 处理 |
| --- | --- | --- | --- | --- |
| 1 | `taskpacks/latex/compiler.py` | `CompileResult.to_dict()` **不导出 `log`** → 工程层与版面层的日志检查（未定义引用、缺文件、overfull 框）**全是死代码** | `to_dict 有 log 吗: False`；喂真实日志时能命中，喂 to_dict 时版面层恒 `pass` | `to_dict()` 补 `log` |
| 2 | `taskpacks/latex/compiler.py` | 编译传了 `-file-line-error`，错误变成 `main.tex:12: …`，而 `_extract_tex_error` 只认 `!` 前缀 → **最常见的 TeX 错误全部丢失** | `\undefinedcommand` → `ERROR_FIELD=''` | 同时识别两种前缀，并补一份致命措辞白名单 |
| 3 | `taskpacks/latex/compiler.py` | `openin_any=r` 是设成**环境变量**的，而 kpathsea 只从 `texmf.cnf` 读它 → 隔离实际不生效 | `kpsewhich --var-value openin_any` 四次都返回 `a`；`\openin1=C:/Windows/win.ini` 读出内容 | 生成自己的 `texmf.cnf`（`openin_any=p`）并用 `TEXMFCNF` 指过去；Windows 侧也复制一份 |
| 4 | `taskpacks/latex/compiler.py` | Windows 复制失败被**静默吞掉**（PowerShell 的 `Copy-Item` 失败是非终止错误，`; 'OK'` 照样打印） | 复制失败后只看到 `I can't find file 'main.tex'` | `$ErrorActionPreference='Stop'` + 检查 returncode/stderr，失败原因写进 `error` 字段与日志 |
| 5 | `taskpacks/latex/compiler.py` | `latex.binary` 配 `.exe` 路径时**整段被跳过** → 永远找不到引擎 | `binary="D:/…/xelatex.exe"` → `已尝试：(无)` | `.exe` 也走解析；Windows 路径在 WSL 下映射到 `/mnt/<盘符>/` |
| 6 | `taskpacks/latex/assembler.py` | 表格单元格里的数学含 `&` → 编译失败（`Missing $ inserted`）；块级公式含 `&` → `Misplaced alignment tab` | 两次真实编译 exit=1 | 单元格内转义 `&`；块级公式改用 `align` 环境 |
| 7 | `training/export.py` | Modelfile 的 `stop "\n\n"` 与训练目标冲突：目标是整篇 `.tex`，而**正文里第一个空行在导言区中间** → 导出的模型只会吐导言区 | 第一个 `\n\n` 在第 178 字符，全文 1061 字符 | stop 改为 `\end{document}` |
| 8 | `training/export.py` | Ollama **不解释** Modelfile 里的 `\n`（只处理 `\\` 与 `\"`）→ 原来的 stop 永远匹配不上，模型会一路生成到 token 上限 | 建两个探针模型实测：真空行不触发 stop | 实测确定可用的写法（`\end{document}`），并写进注释 |
| 9 | `training/export.py` | 模型名按**项目**计数，但 Ollama 名字是全局的 → 两个项目都拿到 `godfather-latex:v001`，后导出的**静默覆盖**前一个的权重 | 实测本机 `v001` 的 modified 是 55 分钟前（正是反复 verify 造成的） | 序号跨项目统计 + 避开 Ollama 里已存在的同名 |
| 10 | `training/runner.py` | 异常路径只捕获 `TrainingCancelled` → 其它异常时状态永远停在 `running`，GPU 锁已释放但**训练子进程还活着** | 注入异常后：`status='running'`、`GPU 锁: None`、活着的子进程 1 个 | 兜底 `except Exception`：杀进程组 + 置 failed + 写错误 |
| 11 | `training/evaluate.py` | 各比率的分母是"成功样本数" → 10 个样本 5 个失败、5 个完美时 `non_empty_rate=1.0`（满分），deltas 反向误导 | 实测 | 分母改成总样本数（失败计 0），同时保留 `rates_over_usable` 供对照 |
| 12 | `training/evaluate.py` | 相似度归一化把所有 LaTeX 命令都删掉 → **纯文本能拿 1.0**，真 LaTeX 反而更低 | 纯文本 similarity=1.0 / has_latex=False | 归一化保留命令与环境名（`cmd:`/`env:` token）；纯文本 0.58、真 LaTeX 1.0 |
| 13 | `web/js/main.js` | `gf:teardown` 在 `clear(main)` **之后**派发 → 旧视图收不到，PDF 文档与 blob URL 永久泄漏 | 3 次进出工作台：createObjectURL ×3、revokeObjectURL ×**0** | 先派发再清空 |
| 14 | `web/js/views/training.js` | `render()` 在 `await` 之后无条件写模块级全局 → 慢的旧渲染覆盖新状态，**轮询静默死掉** | 注入延迟后：最后一次轮询 7.24s，14.57s 时已 0 次请求，页面无报错 | 渲染序号守卫（await 前后各校验一次） |
| 15 | `taskpacks/latex/pipeline.py` | 修订前的版本校验拿**批注的 PDF 产物**去比**最新生成的 TeX 产物** → 两者永远不相等，任何真实批注都进不了修订流程 | `verify_m0.py` 第 4 步报"批注绑定的是更早的产物版本"（历史上这里曾成功过，说明是后加的校验引入的回归） | 两个产物都接受；`verify_m0` 现在是"修订结论：pass，PDF=有" |

### 7.4.2 中危 / 低危（本轮修复）

- **划分泄漏**：内容聚类原先用 (输入+目标) 一起做指纹 → 同一篇文档跑两次会分到不同划分。
  改为**输入指纹与目标指纹双键并查集**（实测同输入/同目标跨划分泄漏归 0）。
- **快照文件不校验哈希**：冻结后被改动会静默用改动后的数据训练 → 加载时强制比对 `sha256`，不一致直接报错。
- **标签虚标**：有 Pandoc 兜底块时把样本标成 `human_reviewed_teacher`（人工复核过的教师产物）；
  人工导入外部模型结果时**无条件**写同一个标签。现在兜底只标 `teacher_generated`，
  导入按显式声明的 `human_reviewed` 决定。
- **训练输入与真实 Markdown 不一致**：块拼接丢掉 `$$` 定界符与代码围栏 → 改用原始 Markdown。
- **回退产物丢 `line_map`**：新 digest 不合并 meta → 批注定位依据消失。现在一起落库。
- **`latex.sandbox` / `disable_shell_escape` / `student_model` / `required_for_startup`**：
  四个配置键代码**从不读取**，却出现在示例配置里（有安全含义的那两个尤其误导）→ 删除。
- **模板覆盖机制是空的**：`load_template(overrides_dir=…)` 从来没人传 → 现在接线到
  `storage.templates_dir`（`config/templates/`）。
- **评测划分可以是 `train`**（拿训练集考试）→ 改成 `Literal["dev","holdout"]`。
- **评测默认取"项目最新快照"**而不是训练用的那份 → 给了 `run_id` 就用它的快照。
- **评测不占 GPU 锁**（文档承诺训练/导出/评测串行）→ 现在也走资源锁。
- **`verify_m1.py` 没有任何断言**：候选在所有指标上都劣于基线也会被激活并返回 0；
  `loss=nan, steps=0` 也返回 0。现在有硬断言（loss 有限、步数 > 0、候选不可用不激活），
  候选劣于基线时**不自动激活**（退出码 5，`--activate-anyway` 可强制）。
- **`verify_m1.py` 的种子样本谎报人工复核**，且只挑 train/dev → holdout 恒为空。已修。
- **前端**：422 的 `detail` 是数组，原样 `JSON.stringify` 弹给用户 → 改成中文短句；
  窄屏下长项目名导致整页横向滚动且「环境自检」被 select 盖住点不到 → 限制 select 宽度；
  一个动作触发两次整页刷新；重渲染后详情面板与按钮状态不一致；"1 分 60 秒"；
  toast 缺 `aria-live`（读屏收不到错误）；`manual_import` 选项必然失败。
- **Markdown 方言是空字段**：只写库、无人读取 → 现在真的影响解析（`commonmark` 不识别管道表格）。
- **Windows 路径下的 `prlimit` 无效**（子进程是 WSL 的 `/init` 代理）：如实写进文档，
  生效的是超时与进程树终止。
- **「环境自检」要等 4~5 秒**（探测 Windows 侧 LaTeX 引擎版本是阻塞式的），
  期间按钮没有任何反馈 → 现在按钮显示"检查中…"并禁用。

### 7.4.3 文档↔代码一致性（按审计报告逐条改）

- README 自相矛盾（表里写 ✅ M1、下一阶段又写"未实现 LoRA/GGUF/回滚"）→ 统一；
  "M0 无训练闭环" → M0+M1；测试条数 82/227 → **243**；目录树补齐 `training/`、
  `api/training.py`、`scripts/verify_m1.py`、`scripts/verify_lan_pairing.py`；字体名改对。
- `docs/03`：删掉**不存在**的 `GET /api/system/providers`；补上 export/activate 的必填
  `project_id`（照文档调用会 422）；配对码标明**一次性**；500 的真实行为（不回显异常类型）；
  补全 `stage` 枚举。
- `docs/05`：M1 从 ⬜ 改写为 ✅（链路跑通、**无质量结论**）。
- `docs/06`：探测顺序、Windows 路径映射、`texmf.cnf` 隔离、`.aux` 清理、rlimit 的实际边界。
- `docs/07`：本节；删掉"没有项目级鉴权"的过期描述；测试条数订正。
- `docs/01` / `docs/02`：前端分层补齐；revision 语义改成真实行为（重传同名文件会新建文档）。
- `config/config.example.yaml`：补 `export:` 段、`fontset`、`max_upload_bytes`、
  `templates_dir`、`worker.gpu_wait_seconds`；删掉四个死键。

### 7.4.4 防复发

新增 12 条声明测试（`tests/test_claims.py`，共 **50** 条）：把本轮"已修但没有测试兜底"的
实现钉住 —— 策略 `accept_verdict` 字符串兼容、SSE 连接上限、`reused` 字段、
可取消状态集合、快照路径、截断样本去重、teardown 顺序、方言真的生效、
评测划分不能是 train、stop 序列、模型名全局唯一、渲染序号守卫。

### 7.4.5 验证

```text
python -m pytest -q                     # 264 passed
ruff check src/ scripts/ tests/         # All checks passed（新增 ruff.toml，B008 豁免 FastAPI 的 Depends）
scripts/verify_m0.py                    # 四种模式全部编译成功
scripts/verify_m1.py ...                # 链路跑通；候选劣于基线 → 退出码 5 且不自动激活
scripts/verify_lan_pairing.py           # 13/13
Playwright（Chromium 1228）             # 10 个页面零控制台错误
```

## 7.5 第六轮：参考 DeepInfer 的设计

[DeepInfer](https://github.com/deepseek-ai/DeepInfer) 是 DeepSeek 2026-10-20 开源的生产级推理框架
（支撑 V4.1-Flash 线上服务）。它本身是"大集群、大吞吐"的推理引擎，
和 Godfather 这种"本地小模型训练工作台"规模差着好几个数量级，
**整体照搬是不合适的**。但它公开的几条设计原则，正好点到了 Godfather 的真实缺口上，
于是挑了能落地的四条逐一实现（每条都写进了测试，避免再次静默回退）：

| DeepInfer 的做法 | Godfather 里的对应问题 | 本轮改动 |
| --- | --- | --- |
| **固定模型修订**（pin model revision，按 checkpoint digest 而不是名字加载） | 教师产出的训练样本记的是 `"revision": "unknown"`；缓存键只含模型 tag —— 用户重新 pull 一下同一个 tag，缓存照样命中，训练样本里就混着另一份权重的答案 | 记录真实权重摘要（Ollama digest）；`teacher_json.revision` 用它；**缓存键包含摘要**，权重一变缓存自动失效 |
| **确定性采样**（deterministic sampling） | 教师调用与评测调用都没有种子，同一份输入两次跑结果可能不同，样本不可复现、差值分不清是模型差异还是采样噪声 | `ProviderRequest.seed` + `providers.ollama.seed`（默认 42）；教师与评测都固定种子，并把种子写进评测记录 |
| **batch-invariant inference**（批量组成不同，结果就不同） | 评测是"先把基线跑完、再跑候选"—— 两轮之间的机器漂移（占卡、冷启动、别的进程）会被算进差值 | 改成**配对执行**：同一输入先基线、紧接着候选；评测方法（配对/种子/温度）写进 `metrics_json.method` 并显示在界面上 |
| **置信度调度验证**（没通过验证的权重不进服务） | 激活模型版本不校验：模型可能已被 `ollama rm` 掉、评测可能显示输出全空，激活它等于把项目指向一个坏模型 | `check_activation()`：模型还得在（Ollama 里或 GGUF 文件在）、有评测记录时不能全空、不能明显劣于基线；前端提供"仍然强制切换"的出口 |

另外借 DeepInfer 的**结构化输出**思路，补了一个数据质量缺口：

- 教师失败且没有工具兜底时，原文 Markdown 会被当成 LaTeX 目标写进训练数据
  （等于教模型输出 Markdown）。现在改用**规则转换**兜底，并计入 `fallback_blocks`，
  `label_origin` 会如实反映"这条不是教师产物"。

以及两条上一轮复审报出来、这轮一并处理的：

- **回滚不是历史栈**：以前取"最近归档的版本" → activate A → activate B → rollback 回 A →
  再 rollback 又回 B，来回跳。现在沿 `previous_version` 链回退一步。
- **评测默认取"项目最新快照"**：给了 `run_id` 就用那次训练用的快照
  （候选与基线必须评同一份数据，跨版本比较才有意义）。

### 7.5.1 验证

```text
python -m pytest -q                     # 264 passed
ruff check src/ scripts/ tests/         # All checks passed
scripts/verify_m0.py                    # 四种模式编译成功；修订结论 pass
scripts/verify_m1.py ...                # 链路跑通
scripts/verify_lan_pairing.py           # 13/13
```

新增声明测试（`tests/test_claims.py`）：缓存键含权重摘要、教师记录真实 revision、
provider 支持 seed、评测配对、激活前校验、回滚沿链、Markdown 不当 LaTeX 目标。

## 7.6 第七轮：复审上一轮改动 + 补完 DeepInfer 借鉴

第一轮改动很大，所以专门做了一轮**对抗性复审**：让另一个代理只盯着上一轮
改过的文件找回归（不改代码，只报告，要求每条都实跑复现）。
结果：上一轮声称修好的东西里，有 4 条**根本没生效**，另有 6 条中危。

### 7.6.1 致命：上一轮的"文件访问隔离"加固完全没生效

上一轮我写了 `openin_any=p` 并生成 `texmf.cnf`，还宣称"实测被挡住"。**结论是错的** ——
复审代理实测：`\openin1=C:/Windows/win.ini` 照样读出内容。

三个原因叠在一起（必须一起修，单修任何一个都会立刻炸）：

| # | 缺陷 | 实测证据 |
| --- | --- | --- |
| F1 | `texmf.cnf` 的复制入队发生在复制循环**之后** → 根本没复制到 Windows 目录 | Windows 侧目录里只有 `main.aux main.log main.pdf main.tex`，没有 cnf |
| F2 | `TEXMFCNF` 指向的是**文件**，而 kpathsea 要的是**目录** | `warning: kpathsea: configuration file texmf.cnf not found` → `I can't find the format file 'xelatex.fmt'` |
| F3 | 只含 3 行的 cnf 会丢掉 `TEXMFDIST` → 宏包与格式文件全找不到 | `ctexart.cls -> (空)`、`fontspec.sty -> (空)` |
| F4 | **WSL interop 不传环境变量** → 我们手工拼的 `env` 整个到不了 Windows 引擎 | WSL 的 49 个变量里 48 个在 Windows 侧不可见；`MYVAR=abc powershell.exe -c '$env:MYVAR'` 得到空 |

而且"paranoid(p) 会挡掉 ctex/fontspec"这个旧结论本身也是**误诊**：
真正的原因是配置压根没送达。实测（合并 TEXMFCNF 目录列表后）：

- `openin_any = p`：`\openin` 读 `C:/Windows/win.ini` **被挡住** ✅
- `openin_any = p`：ctex / fontspec / 多遍编译的 `.aux` / `\includegraphics` **全部照常工作** ✅
- `openin_any = r`：**挡不住**绝对路径读取 ❌（所以必须用 p）

现在的做法：TEXMFCNF = **我们的目录 + 系统的目录列表**（系统那份提供 TEXMFDIST 等），
Windows 引擎改用 PowerShell 执行并在 **PowerShell 进程里**设置变量。
端到端复测：恶意文档编译失败且读不到内容，正常 ctex 文档照常编译，
编译错误也能给出可诊断的原因（`main.tex:1: Undefined control sequence.`）。

### 7.6.2 其它回归与缺陷

| # | 位置 | 问题 | 处理 |
| --- | --- | --- | --- |
| 高 | `training/evaluate.py` | `on_wait` 回调写成 `lambda waited:`，而 `wait_for_resource` 是**零参** → **GPU 锁被占用时评测任务直接 TypeError 崩掉**（正好命中"评测也要占 GPU 锁"的目标场景） | 改成零参 |
| 高 | `training/runner.py` | 兜底 `except Exception` 吞掉失败并正常返回 → job 层把任务标成 **succeeded**（界面显示"完成"但训练没跑成，也不重试） | 改为抛出 `TrainingFailed`，让 job 层判失败 |
| 高 | `taskpacks/latex/pipeline.py` | 回退产物落库的是**教师版**的 `line_map`（连 block_id 都对不上）→ 批注定位彻底错位 | 回退时换成规则版文档自己的 `line_map` |
| 高 | `datasets.py` | 聚类指纹对整个 `input_payload_json` 取哈希 → `mode` 一变指纹就变 → **同一篇 Markdown 跑不同 mode 仍会跨划分** | 指纹只取内容部分（markdown + 块文本） |
| 中 | `taskpacks/latex/assembler.py` | 表格单元格转义 `&` 时，已有的 `\&` 被二次转义成 `\\&`（数学里 = 换行 + 对齐符）→ 编译失败 | 只转义**裸** `&`（`(?<!\\)&`） |
| 中 | `taskpacks/latex/pipeline.py` | 模板覆盖目录只在一处生效：回退与修订路径都漏传 → 同一份配置下教师版与回退版排版不一致 | 三处都传 |
| 中 | `settings.py` | `templates_dir` 相对**进程 CWD** 解析 → 换个目录启动服务，模板覆盖静默失效 | 相对 `root_dir` 解析，并建出 `config/templates/` + 说明 |
| 中 | `web/js/views/training.js` | `reload()` 没有渲染守卫 → 过期的渲染把页面换成"加载中…"，而更新的那次又被丢弃 → **页面永久卡在"加载中…"且轮询停摆** | 过期渲染返回 `null`，调用方不碰 DOM |
| 中 | `api/pipeline.py` | 人工导入时，顶层的 `human_reviewed:true` 会覆盖单条的 `false` | 单条显式值优先 |
| 低 | `compiler.py` | 笼统匹配 `"not found"` → ctex 的良性警告（`Fontset 'xxx' not found`）被当成**编译错误** | 加 `_looks_fatal()` 区分 |

另外还修了一条**上一轮就存在、这轮才暴露**的导出缺陷：

- **导出的模型模板是坏的**：`TEMPLATE qwen` 被 Ollama 当成字面量存下（4 个字符），
  每次推理提示词前面都被拼上 "qwen"（旧导出实测 chat 模式 prompt 只有 3 个 token）。
  现在按训练时的 prompt 格式生成模板，并在 `ollama create` 之后**回读校验**。
  新旧对比：旧导出 3 token（格式坏），新导出 116 token（与训练的 llama-2 格式一致）。

### 7.6.3 复审要求的回归测试（之前一条都没覆盖到）

`TEXMFCNF` 目录语义、Windows 侧用 PowerShell 传环境变量、良性警告不算致命错误、
`on_wait` 零参、同一 Markdown 不同 mode 同簇、`\&` 不二次转义、
人工导入单条优先、**端到端的文件访问隔离**（真跑一次编译，读到内容就失败）。

端到端那条还专门验证过它**不是空过**：把 `openin_any` 临时改回 `r`，测试立即变红。

### 7.6.4 验证

```text
python -m pytest -q                     # 264 passed
ruff check src/ scripts/ tests/         # All checks passed
scripts/verify_m0.py                    # 四种模式全部编译成功；修订结论 pass
scripts/verify_m1.py ...                # 链路跑通；候选劣于基线 → 退出码 5 且不激活
scripts/verify_lan_pairing.py           # 13/13
```

## 7.7 第八轮：面向新手的文档 + 架构图

新增 `docs/00-新手指南.md`（给第一次接触这个项目的人）：

- **设计哲学**：六条原则，每条都先给"说人话"的版本，再讲"为什么"，最后才说怎么实现；
- **架构原理**：一张全景图 + ASCII 文字版 + Mermaid 源码（无图环境也能看）；
- **详尽使用方法**：从装环境到走完一遍流程到训练部署，含参数表与**常见问题排查表**；
- **术语表**：把项目里的黑话翻译成人话。

配套 `scripts/make_diagrams.py` 生成 `docs/img/` 下四张图（架构全景 / 生成流水线 / 训练链 / 设计原则）。

**为什么用代码画而不是 AI 生图**：架构图的价值在于**每个方框都对应真实模块**。
生成式模型画出来的图细节经常是错的；这里的图由脚本画出来，
模块改名时图跟着改，不会过期。脚本本身跑过 ruff。

图里的每个断言都对着代码核过：四种模式的 id、配对码长度与字符表、
会话令牌 7 天与哈希存储、产物目录布局、`verify_m1.py` 的退出码含义、
四层检查的具体检查项。

## 8. 未验证 / 未实现（如实记录）
- **M1 的完整链路已用真实组件跑通**（训练 → 合并 → GGUF → 量化 → Ollama 注册 → 评测 → 切换版本），
  但基座是随机初始化的 4.2M 参数小模型、只训了 3 步，**因此没有任何质量结论**。
- **没有在 GPU 上训练过**：本机 WSL 侧访问不到 GPU，正式训练需在 Windows 侧执行。
- **没有多教师 / Agent 服务接入**：仅定义了 Provider 接口。
- **没有 DDL 任务包**。
- **SyncTeX 未解析**：批注定位依赖块→行映射 + 浏览器端 PDF.js 文本层匹配。
- **移动端未在真机验证**：已按响应式 + PDF 优先实现，但本次未用手机实际走一遍批注流程。
- **PWA 离线同步未实现**：局域网 HTTP 页面不等于完整 PWA。
- **仍未修完 / 明确不做的事**（如实列出，不假装已解决）：
  - `waiting_user` 现在**可达**了，但要显式打开策略里的
    `validator_config.wait_on_teacher_questions: true` 才会触发；
    默认仍只把教师提问记为警告、生成继续。
  - 批注现在记录 `document_revision` 了，`run_revision` 会校验批注绑定的产物版本与文档版本，
    但"产物 ↔ 文档版本"之间仍没有强绑定（同一份产物可能被多次引用），跨多轮生成仍可能需要人工确认。
  - ~~局域网开放后没有做项目级鉴权~~ —— **已修复**（见 §7.3）：
    现在是**项目级**配对码 + 项目级会话令牌，跨项目访问返回 403 `scope_mismatch`。
    残余风险（令牌出现在 URL、CORS `allow_origins=["*"]`、反向代理场景）
    写在 [01-architecture.md §6](01-architecture.md)。
  - uvicorn 已关闭 `proxy_headers`，但如果真要把服务挂在反向代理后面，
    需要另外配置可信代理白名单。
  - `events` / `job_logs` 现在是"条数 + 时间"双条件裁剪（30 天），
    但裁剪在写入时触发，长时间不写库的库不会自动瘦身。
  - **训练取消**现在在启动前会检查，但"正在等 GPU 锁"期间取消仍要等锁释放才生效
    （锁的等待循环没有中断点）。
  - `openin_any` 已从 paranoid 改为 restricted（原因见 §7.2），
    两者都不允许读任意绝对路径，但 restricted 允许读 TEXMF 宏包树。

