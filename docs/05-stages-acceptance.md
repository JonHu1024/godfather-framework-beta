# 阶段与验收

状态标记：✅ 已实现并验证 · 🟡 部分实现 · ⬜ 未实现

## M0：无训练的闭环 ✅

| 验收项 | 状态 | 证据 |
| --- | --- | --- |
| 项目、材料导入、参考案例 | ✅ | `api/projects.py`；原件带内容哈希与 revision |
| Ollama / 人工导入 provider | ✅ | `providers/`；含能力矩阵、健康检查、预算、审计、缓存 |
| Pandoc 基线 | ✅ | `pandoc_baseline` 模式，产出真实 PDF |
| 隔离编译 | ✅ | `compiler.py`：独立目录、`-no-shell-escape`、超时杀进程树、POSIX rlimit |
| PDF 批注 | ✅ | `web/js/views/workbench.js` + `annotate.js`；归一化坐标 + revision 绑定 |
| 样例修订 | ✅ | 批注 → 教师改写 → 重新编译验证 → SFT 候选 |
| 验证（四层） | ✅ | `validators.py`；每层独立结论与 findings |
| JSONL 导出 | ✅ | `datasets.py`；按分组切分，快照冻结 |
| 真实进度 | ✅ | `events` 表 + SSE；总量未知不伪造百分比 |
| 断点重启不丢结果 | ✅ | 任务表 + 租约 + 幂等键；重启后未完成任务可被接管 |

**实测记录**（见 `07-verification-log.md`）：一份中文学术 Markdown 在本机
用 Windows TeX Live 编译出 3 页 PDF；四种模式分别产生独立记录与产物。

## M1：一次完整训练 ✅（链路跑通，无质量结论）

已实现：

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 训练后端适配器 | ✅ | 自有 `transformers + PEFT` 脚本（`training/scripts/train_local.py`）+ LLaMA-Factory 适配器；训练进程独立于 Web 服务 |
| 训练日志 / 检查点 | ✅ | JSONL 日志、checkpoint、`training_runs.metrics_json` |
| GPU 资源队列 | ✅ | 数据库资源锁；训练、导出、评测**串行**（`training/resources.py`） |
| 固定开发集评测与基线对照 | ✅ | 候选 vs 基线同输入对照，指标带分母与失败清单（`training/evaluate.py`） |
| 导出 | ✅ | 合并适配器 → GGUF → 量化 → Modelfile → Ollama；任一步失败即失败，不写版本行 |
| 模型注册与回滚 | ✅ | `model_versions` 表 + `projects.active_model`；跨项目激活被拒 |
| 无 GPU 时的行为 | ✅ | 明确报告"无法训练 / 降级到 CPU"，**不伪造训练成功**（4bit 与 bitsandbytes 缺失都有如实日志） |

模型命名：`godfather-latex:v001`、`v002`…（序号全局唯一，避免跨项目同名互相覆盖）。

**如实说明**：M1 的验收只覆盖"链路能跑通"，**没有**任何模型质量结论 ——
本项目不含训练结果或效果承诺。`scripts/verify_m1.py` 带硬断言
（loss 必须有限、步数必须 > 0、候选不能比基线更差则不自动激活）。

## M2：DDL 任务包 ⬜

需要新增：`taskpacks/ddl/`（schema、可用窗口计算、候选生成、共享容量检查、
规则排序标签、JSON 输出与二次校验、偏好反馈）、控制器中的 DDL 动作。

## M3：降低标注与教师成本 🟡

| 能力 | 状态 |
| --- | --- |
| 内容寻址缓存 | ✅ 已实现（缓存键含输入/教师/提示词/策略/模板/验证器版本） |
| 预算预留与结算 | ✅ 已实现 |
| 有限重试与失败队列 | ✅ 已实现 |
| 多教师按能力路由 | ⬜ 尚未实现（`providers/` 接口已就绪） |
| 主动选择困难样例 | ⬜ 尚未实现（控制器 `inspect_failures` 已能定位失败层） |
| 移动端离线同步 | ⬜ 尚未实现（当前是局域网 HTTP 响应式页面，不是完整 PWA） |

## M4：偏好训练实验 ⬜

前置条件（缺一不做）：真实或已确认的偏好对足够可靠、SFT 基线稳定、
评测能区分收益。在此之前，"策略更新 + SFT" 本身就是有效产品。

## 与设计文档的差异（如实说明）

| 设计文档 | 本实现 | 原因 |
| --- | --- | --- |
| 独立 worker 进程 | 默认内嵌线程，可切 `worker.mode=external` | 单机单人使用更省事；两者共用同一任务表 |
| SyncTeX 定位 | 块→行映射 + PDF.js 文本层匹配 | SyncTeX 解析成本高，且置信度不足时需人工确认 |
| 响应级蒸馏（教师输出作 SFT 数据） | ✅ 已实现，但教师只提供响应文本 | 没有任何 logits 或隐式推理过程被获取，不做此声称 |
| 多教师路由、Agent 服务接入 | 仅接口预留 | 需要按实际提供方说明核验，不做未经核实的接入 |
