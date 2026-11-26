# 数据模型

元数据全部在 SQLite（`data/godfather.sqlite3`），产物在内容寻址文件仓库。
建表语句见 `src/godfather/db.py` 的 `SCHEMA_SQL`。

## 1. 实体关系

```
Project ──┬── SourceDocument ──── Document ──── DocumentRevision
          │         │                 │
          │         │                 ├── Generation ──── ValidationReport
          │         │                 │
          ├── ReferenceExample        ├── Annotation
          │                           │
          ├── Policy ──── PolicyPreference
          │
          ├── TrainingExample ──── DatasetSnapshot
          ├── TrainingRun ──── EvaluationRun ──── ModelVersion
          ├── Artifact（PDF / TeX / log / asset）
          ├── Job ──── JobLog
          ├── ProviderCall
          ├── ControllerAction
          └── QuestionCard
```

## 2. 核心表

### projects
| 字段 | 说明 |
| --- | --- |
| id / name / taskpack | 项目标识与任务包类型（当前只有 `markdown_to_latex`） |
| active_model | 当前使用的模型版本指针（发布可回滚，M1 使用） |
| policy_version | 当前生效的策略版本 |
| settings_json | 项目级设置（可覆盖预算上限等全局设置） |

### source_documents / document_revisions
- `source_documents` 保存**不可变原件**：内容哈希 `sha256`、`revision`、字节数、方言。
- `document_revisions` 保存每次解析的结构树（`blocks_json`）与全局上下文（`context_json`）。
- 同一文件被修改 → 新 revision → 只重建受影响的派生产物。
  **当前实现的真实行为**：再次上传同名文件会**新建一个文档**（新的 source_document），
  而不是给原文档加 revision —— 目前只有"批注修订写回"会产生同一文档的 revision ≥ 2。
  要按文件归并，需要引入文件名指纹或内容哈希来复用 source_document（尚未实现）。

### blocks（存于 `document_revisions.blocks_json`）
```json
{
  "block_id": "block-0007",
  "kind": "heading|paragraph|list|table|code|math|quote|image|html|hr",
  "text": "…",
  "level": 2,
  "path": ["第 2 章 方法", "2.1 数据切分"],
  "line_start": 42,
  "line_end": 48,
  "meta": {"language": "python", "labels": ["eq:1"], "src": "figures/a.png"}
}
```
`block_id` 由内容顺序稳定生成：同样的输入永远得到同样的 ID。

### artifacts
| 字段 | 说明 |
| --- | --- |
| kind | `markdown / tex / pdf / log / asset / reference_*` |
| revision | 内容哈希（`sha256:…`），产物不可变 |
| meta_json | 例如 PDF 的 `generation_id`、TeX 的块→行号映射 |

### generations
一次生成轮次：`mode`（四种模式之一）、`provider`、`model`、`policy_version`、
`tex_artifact_id`、`pdf_artifact_id`、`stats_json`（含四层结论、编译信息、教师调用统计、警告）。

### validation_reports
每个生成轮次四条记录（工程 / 内容 / 版面 / 偏好）：
```json
{
  "layer": "content",
  "verdict": "warn",
  "counts": {"fail": 0, "warn": 2},
  "findings": [
    {"level": "warn", "code": "math_changed",
     "message": "公式源串未原样出现在 TeX 中（可能是等价改写，需人工确认）"}
  ]
}
```

### annotations
批注绑定**产物 + 修订 + 归一化坐标**：

| 字段 | 说明 |
| --- | --- |
| artifact_id / artifact_revision | 圈选时看到的那一份 PDF（重排后不允许静默错位） |
| page_index / viewport_rotation | 页码与视口旋转 |
| bbox_json | `[x, y, w, h]`，0–1 归一化坐标 |
| comment / category / scope | 文字意见、类别、作用范围 |
| source_block_candidates_json | 候选源块 ID（定位不可靠时为空，需人工确认） |
| mapping_confidence | `confirmed / needs_review` |
| label_origin | `human / agent_review`（不把自动评审伪装成人工） |

### policy / policy_preferences
- `policies`：版本化策略包（YAML 文本 + 哈希 + 状态 `draft/active`）。
- `policy_preferences`：一条条偏好，含 `scope`（project/document/block/block_kind）、
  `priority`、`confidence`、`status`、`evidence`、`rule`（机器可判定规则）。

### training_examples
一条训练样本（字段与设计文档 §4 一致）：

```json
{
  "id": "ex-…",
  "task_type": "markdown_to_latex",
  "source_group_id": "src-…",
  "source_revision": "sha256:…",
  "input_payload": {"mode": "teacher_direct", "markdown": "…", "blocks": []},
  "target_payload": {"tex": "…"},
  "teacher": {"provider": "ollama", "model": "qwen3:0.6b", "revision": "unknown"},
  "policy_version": "policy-002",
  "prompt_version": "prompt-001",
  "reference_ids": [],
  "annotation_ids": ["ann-…"],
  "label_origin": "human_reviewed_teacher",
  "validation_report_id": null,
  "status": "candidate",
  "split": "train"
}
```

`label_origin` 的取值与含义：

| 值 | 含义 |
| --- | --- |
| `teacher_generated` | 教师生成并通过检查 |
| `human_reviewed_teacher` | 人工批注触发的修订，经重新编译验证 |
| `rule_synthesized` | 确定性规则转换（工具产物，不是模型能力） |
| `manual_import` | 人工导入的教师结果 |

### dataset_snapshots
- `counts_json`：train / dev / holdout 数量。
- `manifest_json`：分组归属、种子、比例、标签来源、策略版本、`holdout_excluded_from_prompt`。
- 快照冻结后不可修改；同一 seed 下内容哈希可复现。

### jobs / provider_calls / controller_actions / question_cards
- `jobs`：任务队列（含租约、幂等键、attempt 计数）。
- `provider_calls`：每次外部调用的审计（用量、费用状态、耗时、错误、缓存键）。
- `controller_actions`：建议、校验结果与执行结果。
- `question_cards`：需要用户回答的关键问题。

## 3. 切分规则（实现于 `datasets.py`）

1. 先按 `source_group_id` 分组（同一文档 / 同一原始场景）。
2. 用 `sha256(seed + group_id)` 把**分组**整体划入 train / dev / holdout。
3. 之后才切块、生成变体；同组样本绝不跨集合。
4. 留出集涉及的分组 ID 记入 `manifest.group_assignments`，
   导出人工任务包时会自动排除（`holdout_excluded_from_prompt = true`）。
5. 人工任务包同时带 `input_hash`：导入时校验对应关系，输入变了就拒绝导入。
