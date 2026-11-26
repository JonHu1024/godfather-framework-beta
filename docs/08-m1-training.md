# M1：训练与模型中心

本文说明 M1 的实现范围、命令链路、如何配置、以及**哪些部分是真实跑通的**。

## 1. 范围

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 训练后端适配器 | ✅ | `local`（自有 transformers+PEFT 脚本）与 `llamafactory`（CLI 适配器） |
| 数据快照 → 训练数据 | ✅ | messages 格式 + LLaMA-Factory 的 `dataset_info.json` |
| 训练运行与日志 | ✅ | 独立进程、实时日志落库、指标记录、取消 |
| GPU 资源串行调度 | ✅ | 数据库锁 + Ollama 常驻模型检查 |
| 导出 GGUF → 量化 → Ollama | ✅ | 合并 → convert_hf_to_gguf → llama-quantize → Modelfile → ollama create |
| 模型版本登记 / 切换 / 回滚 | ✅ | `model_versions` + `projects.active_model` |
| 固定开发集评测与基线对照 | ✅ | 同一输入、同一工具条件下比较，报分母 |
| 多后端同时维护 | ❌ | 只实现并维护一条链路（符合设计要求） |

## 2. 命令链路

```
数据快照（冻结）
   │  training/datasets.py：messages 格式 + dataset_info.json
   ▼
训练运行  training/runner.py
   │  ├─ 预检（基座是否存在、依赖、显存）
   │  ├─ 获取 GPU 锁（与推理串行）
   │  └─ 拉起独立进程 → 日志逐行落库 → 指标
   ▼
适配器输出（LoRA adapter 或全参数）
   │  training/scripts/merge_lora.py：与**精确匹配**的基座权重合并
   ▼
GGUF（f16）
   │  llama.cpp convert_hf_to_gguf.py
   ▼
量化 GGUF（默认 q4_k_m）
   │  llama.cpp llama-quantize
   ▼
Modelfile → ollama create godfather-latex:v001   # 序号全局唯一（跨项目也不重名）
   │
   ▼
固定开发集评测（候选 vs 基线）→ 决定是否切换 active_model
```

## 3. 配置

```yaml
export:
  llama_cpp_dir: "D:/Environment/texlive/../llama.cpp"   # llama.cpp 仓库目录（含 convert_hf_to_gguf.py）
  ollama_bin: "ollama"
  quantize_method: "q4_k_m"
  model_name_prefix: "godfather-latex"
```

`llama_cpp_dir` 留空时会在以下位置自动寻找：
`<项目>/.toolchain/llama.cpp`、`~/llama.cpp`。
找不到时导出会在**第 2 步**明确失败，并提示配置路径 —— 不会假装导出成功。

## 4. 训练参数与记录

每次运行都完整记录（`training_runs.params_json`）：

- 基座模型路径与**精确修订**（`base_revision`）、分词器修订、chat template；
- LoRA：rank / alpha / dropout / target_modules；QLoRA：是否 4bit；
- 优化：学习率、epoch、warmup、scheduler、weight decay、seed；
- 批处理：序列长度、per-device batch、梯度累积、gradient checkpointing；
- 数据：快照 ID、train/dev 划分、样本数、标签来源分布、训练文件哈希。

另外，**教师侧**的每次调用都会记录：教师模型名、**权重摘要（digest）**、采样种子。
样本的 `teacher_json.revision` 就是那份权重摘要 —— 以前写的是 `"unknown"`，
等于没记录，样本来源无法追溯。缓存键也包含这份摘要：
同一个模型 tag 被重新 pull 之后，旧缓存自动失效（否则训练样本里会混着另一份权重的答案）。

## 5. 取消与恢复语义（重要）

- **取消**：终止训练进程树（Windows 用 `taskkill /T`，POSIX 用 killpg），已产生的检查点保留。
- **恢复**：本项目的默认检查点**不含完整优化器状态**，因此"恢复"一律是**重新开始训练**，
  不是精确续训。界面上写作「续训语义：重新开始」。
  如果换成 LLaMA-Factory（`supports_resume=True`）并保留其优化器状态，可另行实现精确续训。

## 6. 评测口径

评测在**固定开发集**（快照的 dev 划分）上进行，候选与基线使用**完全相同的提示词**：

### 6.1 评测方法（决定了差值能不能信）

| 项 | 取值 | 为什么 |
| --- | --- | --- |
| 执行方式 | **配对**：同一输入先跑基线、紧接着跑候选 | 顺序跑（先把基线跑完再跑候选）会把两轮之间的机器状态漂移（占卡、冷启动、别的进程）算进差值。DeepInfer 强调的 "batch-invariant inference" 就是这件事：批量组成不同，结果就不同 |
| 采样温度 | `0.0` | 贪心解码 |
| 采样种子 | `providers.ollama.seed`（默认 42） | 固定种子 → 同输入同权重同输出，可复现 |
| 权重摘要 | 记录基线与候选的 digest | 只记模型 tag 是不够的：同一个 tag 重新 pull 之后权重就变了 |

这些都写进 `evaluation_runs.metrics_json.method`，界面上也会显示 ——
换一套采样设置或执行顺序，差值就不可比，必须让人看得见。

### 6.2 指标



| 指标 | 含义 | 分母 |
| --- | --- | --- |
| `non_empty_rate` | 输出非空 | 样本数 |
| `has_latex_rate` | 输出含 LaTeX 结构标记 | 样本数 |
| `mean_block_coverage` | 源块内容在输出中的覆盖比例 | 可比较的块数 |
| `mean_similarity` | 与参考答案的字符级相似度 | 样本数 |
| `cjk_rate` | 中文未丢失 | 样本数 |
| `mean_latency_ms` | 单条延迟 | 有延迟记录的样本数 |

每条评测记录都保存逐样本明细与失败清单（哪条为空、哪条没有 LaTeX 结构），
**不给没有分母的综合分**。

比率指标（`non_empty_rate` / `has_latex_rate` / `cjk_rate`）的分母是**总样本数**，
调用失败的样本计 0 —— 用"成功样本数"当分母会让"失败一半"显示成满分。
同时保留 `rates_over_usable` 供对照。

### 6.3 导出模型的模板必须与训练时的 prompt 格式一致

Ollama 的 Modelfile 里 `TEMPLATE <名字>` **不会被解析成内置模板名** ——
实测 Ollama 0.40 会把它**原样存成一个模板字符串**：

```text
TEMPLATE qwen   →  注册的模板就是 4 个字符的 "qwen"
```

于是每次推理，提示词前面都会被拼上 "qwen"（旧导出实测：
chat 模式 prompt 只有 3 个 token，格式完全坏掉）。

现在的规则：

| 训练时的 prompt 格式 | 导出时的处理 |
| --- | --- |
| 分词器自带 chat template（`has_chat_template=True`） | **不写** `TEMPLATE` 行 —— GGUF 里会带上分词器的模板，Ollama 自动用它，与训练完全一致 |
| instruct 格式（`PROMPT_TEMPLATE`） | 写对应的 Ollama 模板 `### 指令\n{{ .Prompt }}\n\n### 回答\n`，与训练脚本逐字一致 |
| 用户显式给了真正的模板（含 `{{ }}`） | 用它 |

`ollama create` 之后还会**回读** `/api/show`：
模板是"没有 `{{` 的短字符串"就说明被当成了字面量，会明确告警。

### 6.4 验证过才发布

激活一个模型版本前会校验（`model_registry.check_activation`）：

- 模型还在 Ollama 里，或 GGUF 文件还在（两者都没了 → 指向空气，拒绝）；
- 有评测记录的话，输出不能全为空、且不能明显劣于基线。

被拦下时前端会问"仍然强制切换吗"，`force=true` 可以跳过校验。
回滚沿 `previous_version` 链回退一步（不是"取最近归档的"，那会在两个版本之间来回跳）。

## 7. 硬件与现实约束

| 环境 | 情况 |
| --- | --- |
| Windows 侧 | RTX 5060 Laptop，8GB 显存，CUDA 13.3 —— 可训 1.5B 级 QLoRA（4bit、seq 2048、microbatch 1）；7B 偏紧 |
| WSL2 侧 | **访问不到 GPU**（`GPU access blocked`），训练必须在 Windows 上跑 |
| 内存 | WSL 侧 11GB，CPU 训练只适合冒烟验证，不适合正式训练 |
| Ollama | 常驻模型会占显存；训练前应 `ollama stop <model>`，系统会检测并提示 |

因此推荐结构：**Windows 跑训练与导出，WSL/Linux 跑 Web 服务与数据生产**，
两者共用同一份数据目录（或通过导出的 JSONL 交接）。

## 7.1 对抗式审查后修正的行为（与直觉不同，但都是对的）

| 行为 | 说明 |
| --- | --- |
| 训练集为空 → 拒绝训练 | 确定性失败，不重试，运行记录写 `failed` + 原因 |
| 预检失败 → 拒绝训练 | 缺依赖、基座不存在、显存不足都会写明原因 |
| 答案被 EOS 收尾 | 没有结束符模型学不会停止，评测会被 2048 token 拖死 |
| 空答案样本被剔除并计数 | 不会拿 prompt 末 token 当监督目标（那是伪造监督信号） |
| 序列超长时截断 **prompt 开头** | 截断整段尾部会把答案切掉，训练会"成功"但 loss 恒为 0 |
| 导出失败 **不** 改训练状态 | 训练成功就是成功，导出失败只说明导出失败，可以重试 |
| 评测前检查模型是否存在 | 模型没装与模型很烂在汇总层必须能区分 |
| 指标一律带分母 | 覆盖率、相似度、延迟都报告各自的样本数 |
| GPU 锁带续租心跳 | 训练可能几小时，锁不能被"过期接管" |
| 导出/评测也要占 GPU | 合并基座要吃整块显存，与训练必须串行 |

## 8. 验证状态（如实记录）

本机（WSL2、无 GPU）已经用**真实组件**跑通整条链路：

```
数据准备 → 预检 → GPU 锁 → 独立进程训练(torch+peft，真实 LoRA)
        → 合并适配器 → convert_hf_to_gguf.py → llama-quantize
        → ollama create → Ollama 内实际生成 → 开发集评测(候选 vs 基线) → 切换版本
```

细节与原始输出见 `docs/07-verification-log.md` 第 6 节。

**但没有任何质量结论**：验证用的基座是随机初始化的 4.2M 参数小模型，只训了 3 步，
评测明确显示候选**劣于**基线（`has_latex_rate` -0.25）。这说明的是"评测链路如实反映了失败"，
而不是"训练有效"。

正式训练需要：真实基座（1.5B 级中文模型）+ CUDA 环境（Windows 侧 RTX 5060 8GB）+ 真实语料。

## 9. 怎么在 Windows 上真的跑一次

```powershell
# 1. 准备环境（一次性）
pip install torch --index-url https://download.pytorch.org/whl/cu124
pip install transformers peft accelerate huggingface-hub

# 2. 准备基座权重（1.5B 级中文模型）
huggingface-cli download Qwen/Qwen2.5-1.5B-Instruct --local-dir D:\models\Qwen2.5-1.5B-Instruct
#   国内可用镜像：set HF_ENDPOINT=https://hf-mirror.com

# 3. 准备 llama.cpp（用于 GGUF 转换与量化）
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp && cmake -B build -DGGML_CUDA=ON && cmake --build build --config Release --target llama-quantize

# 4. 指向配置
#    config/config.yaml:
#      export.llama_cpp_dir: "D:/tools/llama.cpp"

# 5. 在 Web 界面「训练与模型」页：选快照 → 填基座路径 → 提交
#    （或用 API：POST /api/training/runs）

# 6. 训练完成后点「导出」→ 生成 godfather-latex:v003 → 「评测」→ 通过后「设为当前版本」
```

## 10. 与设计文档的差异

| 设计文档 | 本实现 | 原因 |
| --- | --- | --- |
| 首选 LLaMA-Factory 适配器 | 同时提供 `llamafactory` 适配器与自有 `local` 后端，默认用 `local` | 自有后端依赖更少（无需装 LLaMA-Factory 全家桶），便于先跑通；同时保留 LLaMA-Factory 入口 |
| 训练与推理串行调度 | 数据库锁 + Ollama 占用检测 | 单机单用户足够；跨机调度留待以后 |
| 导出后自动复测 | 导出与评测分成两个显式步骤 | 复测要消耗时间与显存，交给用户决定时机；避免"发布即评测"的隐式耦合 |
| 精确续训 | 明确标注为「重新开始」 | 默认检查点不含优化器状态，不能冒充精确续训 |
