# API 与实时事件接口概览

所有受保护端点在局域网 `--lan` 模式下强制执行项目级配对令牌校验（Header `x-godfather-session` 或 `x-godfather-pairing`；静态资源下载 URL 支持 `?session=...`）。

## 1. Cordis 插件、任务包与模型接入（仅限本机操作修改）

- `GET /api/taskpacks`：列出已挂载的 Cordis 任务包契约（`markdown_to_latex`、`general_sft` 及外部 `./plugins/` 扩展）。
- `GET /api/plugins`：查看所有 `PluginFiber` 状态（`active` / `pending` / `disposed`）、注入依赖与回收句柄数。
- `POST /api/plugins/discover`：热扫描并挂载 `./plugins/` 外部模块。
- `POST /api/plugins/{name}/enable` / `disable` / `reload`：运行时启停与热重载插件。
- `GET /api/models-access` / `PUT /api/models-access`：配置默认教师、远程 OpenAI 兼容 API（密钥永不回显明文）、Ollama、预算闸门与 LaTeX/导出路径。
- `POST /api/models-access/test`：探测 `/models` 列表或发起测试对话 ping。
- `GET /api/models-access/models?provider=...`：查询可用模型列表。
- `POST /api/models-access/ollama/unload`：卸载常驻 Ollama 模型释放显存。

## 2. 项目、材料、文档与生成

- `POST /api/projects`：创建项目，允许传已挂载的任意 `taskpack` ID。
- `GET /api/projects/{id}/overview`：项目进度、四层校验计数、预算使用状态与控制器建议。
- `POST /api/projects/{id}/materials`：分块流式上传（`markdown` / `corpus` / `dataset` / `asset` / `reference_md` / `reference_tex` / `reference_pdf`）。
- `POST /api/projects/{id}/generations`：按项目所挂载的任务包自动投递 `latex.generate` 或 `sft.generate`。
- `GET /api/artifacts/{id}/content`：产物内容下载。

## 3. 批注、多选源块修订与样本

- `POST /api/projects/{id}/annotations`：创建批注（支持绑定多个 `source_block_candidates` 与 `mapping_confidence`）。
- `PATCH /api/annotations/{id}` / `DELETE /api/annotations/{id}`：编辑意见、调整关联源块或删除批注。
- `POST /api/annotations/{id}/accept`：支持传入确认的 `block_ids`（多选）、`provider`、`model`、`force`；`needs_review` 未确认前会拒绝静默修订。
- `POST /api/projects/{id}/examples/batch-status`：批量将多条样本设为 `accepted` / `rejected` / `candidate` / `needs_review`。
- `POST /api/projects/{id}/datasets/snapshots/preview`：只读试算切分比例、去重簇数量与警告。
- `POST /api/projects/{id}/datasets/snapshots`：按簇聚类切分并原子写入 LF 换行 JSONL + SHA-256。

## 4. 训练、导出与评测 (`/api/training/*`) 与 SSE (`/api/events`)

- `GET /api/training/backends` / `resources`
- `POST /api/training/runs` / `runs/{id}/cancel` / `runs/{id}/restart` / `runs/{id}/export`
- `POST /api/training/evaluations`（禁止用 `train` 作评测集；同批配对交错执行）
- `POST /api/training/models/{id}/activate` / `models/rollback`
- `GET /api/events`（SSE 实时任务事件流）& `GET /api/events/history`
