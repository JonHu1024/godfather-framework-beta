# 架构说明：Cordis 热插拔微内核与安全边界

从 v0.2 开始，Godfather 把原先散落在 `app.py` 里的 LaTeX 硬编码流水线抽象为参照 **Cordis**（及 DeepSeekHarness 插件机制）构建的 IoC 微内核。

---

## 1. Cordis 微内核分层 (`src/godfather/cordis/`)

1. **核心抽象 (`kernel.py`)**
   - `Service` 与 `provide / inject`：基础组件（数据库 `db`、产物存储 `store`、队列 `queue`、任务包注册表 `taskpacks`、模型提供方注册表 `providers`、训练后端注册表 `training_backends`）均作为 IoC 服务注册在根内核。
   - `PluginFiber` 状态机：`pending`（等待依赖服务就绪）→ `loading` → `active`（活跃）→ `disposing` / `disposed`。若某依赖服务被停用或撤销，依赖它的所有下级插件会自动安全退回 `pending`。
   - `EffectScope` 副作用作用域：每个插件在 `apply(ctx)` 里通过 `ctx.effect(...)` 注册的队列处理器 (`queue.register_disposable`)、提供方 (`providers.register`)、任务包 (`taskpacks.register`) 与事件监听 (`ctx.on`) 都会在插件停用或热重载时逆序自动卸载，不会留下孤儿状态或双倍回调。
   - 动态热加载：扫描 `plugins/*.py` 与 `plugins/*/plugin.py`，支持在不中断 FastAPI 进程的前提下修改外部插件并执行 `reload`。

2. **任务包契约 (`contracts.py`)**
   - `TaskPackSpec` 统一定义了每类微调任务的能力与钩子：`input_formats`（支持上传类型）、`modes`（运行模式列表）、`system_prompt`、`stop_sequences`、`ingest_material`、`run_generation`、`format_training_example`（把 DB 样本转为多轮对话 messages）、`score_prediction`（对应场景的评测计分器）。
   - 训练数据准备 (`training/datasets.py`)、配对评测 (`training/evaluate.py`)、Modelfile 导出 (`training/export.py`) 与动作控制器 (`controller/actions.py`) 均按项目的 `taskpack` 字段动态查询 `ctx.taskpacks` 分发，不再写死单一任务格式。

---

## 2. 安全边界（不可退让的不变量）

- **LaTeX 编译器沙箱**：恒定传入 `-no-shell-escape`，写死前置 `texmf.cnf` 目录设 `openin_any = p` 与 `openout_any = p`，仅导入本次渲染所需白名单相对路径素材，禁用网络代理环境变量。
- **局域网项目级访问控制**：非本机回环启动需兑换项目级 8 位一次性配对码；跨项目资源 ID 访问或与 `?project_id=` 矛盾一律 `403 scope_mismatch`；远程会话无权访问或修改 `/api/models-access`（API Key 配置）与 `/api/plugins`（服务端代码装载）。
- **数据集与密钥防泄漏**：`holdout` 划分禁止写入手动任务包及教师示范片段；API Key 永不从接口明文返回，运行时覆盖落在隔离的 `runtime_config.json`。
