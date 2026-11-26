# Godfather Framework

> **一句话讲清楚**：Godfather 是一个面向个人的**通用本地特化模型微调工作台**。当你手头只有原始资料、不想靠玄学提示词碰运气，也不希望微调完的模型“看着挺好、一跑就错”时，它可以帮你把 **数据整理 → 教师生成/人工纠偏 → 去重切片冻结 → 本地 QLoRA 微调 → 导出 GGUF → 与原基线同场对比评测** 整条链路老老实实串起来。

从 v0.2 起，核心升级为参照 **Cordis** 思想实现的**轻量热插拔微内核**：

- 内置 **`markdown_to_latex` 任务包**：解决中文/科技 Markdown 转 LaTeX 时公式嵌套、行内符号误拆、`ctex` 编译报错、PDF 校对定位难的问题。
- 内置 **`general_sft` 任务包**：支持直接导入 `.jsonl` / `.json` / `.csv` / `.md` 语料做通用指令、问答、结构化抽取微调，不绑死在 LaTeX 场景。
- **插件化扩展**：在 `plugins/` 目录下放一个遵循 `apply(ctx)` 约定的 `.py` 文件，就能在界面「**Cordis 插件**」中心一键扫描、启用、停用或热重载，不用重启主服务。

---

## 1. 为什么不用直接写个脚本微调？

自己写微调脚本最容易踩五个坑，Godfather 把它们变成了硬约束：

1. **训练集与评测集互相泄漏**：同一篇文档切出来的两段、或同一输入换了个模式生成的样本，如果不聚类就切分，模型只是在做“背题”，指标虚高。Godfather 用并查集按内容指纹和 `source_group_id` 聚类后才划分 `train / dev / holdout`，且 `holdout` **永远不进提示词和训练集**。
2. **远程教师 API 格式脏、推理模型乱吞字**：DeepSeek-R1、Qwen3 等模型常吐出 `<think>...</think>` 或多余代码围栏；本框架自动剥离思考过程、修复合法 JSON 转义、并对正文的严重截断发出警告。
3. **批注修订“改了等于没改”**：工作台支持对同一问题框选**多选关联多个源码块**协同修订，必须人工确认定位 (`confirmed`) 后才提交，修订结果自动写回新版本 `document_revisions` 供下轮生成与训练继承。
4. **Windows 编译/训练神秘崩掉**：修好了原生 Windows 下 XeLaTeX 缺少 `USERPROFILE`/`TEMP`、路径分隔符用成冒号 `:`、Pandoc 列表宏被误伤、`PyTorch WinError 10106`、以及训练时 `max_seq_length` 过小先读完权重再报错等问题。
5. **指标口径掺水**：调用失败或空输出不从分母里剔除，配对评测把“基线”与“候选”在同一批次交错跑在同一条输入上，对比分差不混进机器热身波动。

---

## 2. 五分钟快速上手

### 环境要求

- Python `3.10 ~ 3.12`（推荐用 `uv` 创建虚拟环境）
- 若需跑通 `markdown_to_latex` 的 PDF 编译，需本机安装 `xelatex`（TeX Live 或 MiKTeX 均可；Pandoc 可选，不装也可跑 `rule_only` 和 `teacher_direct`）
- 若需在本地执行训练与导出：`torch` + `transformers` + `peft`（以及 `llama.cpp` + `ollama`）

### 安装与启动

```bash
# 1. 创建环境并安装可编辑依赖
uv venv --python 3.12 .venv
# Windows PowerShell 激活（Linux/macOS 用 source .venv/bin/activate）：
.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"

# 可选：如果要在同一台机器本地微调小模型（如 Qwen2.5-0.5B/1.5B/7B）：
uv pip install -e ".[dev,train]"

# 2. 启动 Web 工作台与内置后台队列（默认仅本机回环访问，端口 8760）
python -m godfather serve
```

浏览器打开 <http://127.0.0.1:8760> 即可使用。

> 需要同局域网手机/平板协同审核时，可带 `--lan` 启动：系统会自动为**每个项目**生成独立 8 位一次性配对码，远端设备只能看解锁的那一个项目，且无权查看或修改主机的 API Key 与插件设置。

---

## 3. 日常使用五步走

1. **创建项目**：在页面顶部工具栏点击 **`＋ 新建项目`**，选择你要用的 Cordis 任务包（如 `Markdown → LaTeX` 或 `通用指令微调 General SFT`）。
2. **接入教师模型**：点击左侧 **`模型接入中心`**：
   - 使用本地模型：填写 Ollama 地址与默认模型（支持在训练前**一键释放 Ollama 显存**）。
   - 使用远程大模型：选 DeepSeek / MiniMax / 阿里云 Qwen / Kimi / 智谱 / vLLM 预设，填上 API Key 并开启「允许已定价付费调用」或设置最大调用次数，点 **「发送测试对话 Ping」** 验证连通性（配置即时加密/脱敏落盘，不回显明文密钥）。
3. **导入材料与参考**：在 **`材料与参考`** 直接拖拽或批量选择多个源文件。若上传了 `reference_md` / `reference_tex`，系统会自动抓取参考片段放进教师上下文作为风格示例。
4. **生成与多块协同标注**：
   - 到 **`生成与队列`** 选择模式（`rule_only` / `pandoc_baseline` / `tool_assisted` / `teacher_direct`）与提供方模型启动。
   - 在 **`标注工作台`** 查看 PDF 与验证报告：框选问题区域时，可以用搜索与多选勾选器一次性勾选并确认多个对应源块（也可开启左栏多选），确认后再点「接受并修订」。
5. **批量选样、试算快照与训练部署**：
   - 在 **`样本与快照`** 多选批量接纳通过验证的样本；冻结快照弹窗里可自由设定 `train/dev/holdout` 比例并**实时试算条数**再落盘。
   - 在 **`训练与模型`** 配置基座模型与 QLoRA 参数启动真实训练，跑完后一键合并转 GGUF 写入 Ollama 并与基线同场配对评测。

---

## 4. Cordis 热插拔插件开发示例

想扩展一个全新的业务场景（比如 SQL 生成、代码评审或财务抽样），只需在 `./plugins/my_sql_pack.py` 写一个标准插件（也可参考 [plugins/README.md](file:///d:/Codes/Antigravity/Codes/GodfatherFramework/plugins/README.md)）：

```python
from godfather.cordis import CordisContext, PluginMeta, TaskPackSpec

PLUGIN_META = PluginMeta(
    name="taskpack-sql-gen",
    display_name="Text-to-SQL 微调任务包",
    version="0.1.0",
    kind="taskpack",
    inject=("app", "taskpacks", "queue"),
)

def apply(ctx: CordisContext) -> None:
    taskpacks = ctx.inject("taskpacks")
    queue = ctx.inject("queue")

    spec = TaskPackSpec(
        id="text_to_sql",
        name="Text to SQL",
        description="自然语言转 SQL 查询",
        default_job_kind="sql.generate",
    )
    # 关键：通过 ctx.effect(...) 登记的所有资源在插件停用/热重载时会自动清理，不留脏状态
    ctx.effect(taskpacks.register(spec))
    ctx.effect(queue.register_disposable("sql.generate", lambda job: {"ok": True}))
```

保存后在前端 **`Cordis 插件`** 页点击「**扫描并挂载外部 ./plugins/ 插件**」，即可不重启直接新建 `text_to_sql` 项目。

---

## 5. 目录导航

```text
src/godfather/
  cordis/             # Cordis 微内核：Service IoC、EffectScope 回收树、热重载与内置挂载
  providers/          # Ollama、OpenAI 兼容远程 API、手工导入与思维链清洗
  policy/             # 偏好策略包管理、冲突检测与版本回滚
  taskpacks/
    latex/            # Markdown→LaTeX：解析、组装、XeLaTeX 沙箱编译、4层检查、协同修订
    general_sft/      # 通用指令微调任务包：JSONL/CSV/Markdown 解析与相似度/格式评测
  training/           # 数据准备、Local(transformers+PEFT) / LLaMA-Factory 后端、GGUF 导出、配对评测
  api/                # FastAPI 路由与项目级作用域校验
web/                  # 零构建 ES Module 原生前端（支持明暗主题、多选源块定位器、模型接入中心、插件中心）
plugins/              # 外部热插拔 Cordis 插件放置目录
docs/                 # 详细新手指南、架构说明与 Windows TeX 注意事项
```

进一步阅读：
- [00-新手指南.md](file:///d:/Codes/Antigravity/Codes/GodfatherFramework/docs/00-新手指南.md)
- [01-architecture.md](file:///d:/Codes/Antigravity/Codes/GodfatherFramework/docs/01-architecture.md)
- [06-windows-latex.md](file:///d:/Codes/Antigravity/Codes/GodfatherFramework/docs/06-windows-latex.md)
