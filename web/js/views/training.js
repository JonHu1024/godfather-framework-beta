// 训练与模型：M1（真实训练 / 导出 / 评测）
// 说明：本页所有数字都来自后端返回；后端没给的字段一律显示「—」，不做任何推算或伪造。
import { api } from "../api.js";
import {
  clear,
  confirmDialog,
  empty,
  fmtBytes,
  fmtTime,
  h,
  modal,
  pill,
  showError,
  statCard,
  table,
  toast,
} from "../ui.js";
import { state } from "../state.js";

const POLL_MS = 3000;

const DEVICE_OPTIONS = [
  ["auto", "auto（自动）"],
  ["cpu", "cpu"],
  ["cuda", "cuda"],
];

const SPLIT_OPTIONS = [
  ["dev", "dev（开发集）"],
  ["holdout", "holdout（留出集）"],
  ["train", "train（训练集）"],
];

const QUANT_FALLBACK = ["q4_k_m", "q4_0", "q5_k_m", "q8_0", "f16"];

// 运行状态 → 徽标颜色
const STATUS_KIND = {
  queued: "muted",
  pending: "muted",
  running: "accent",
  succeeded: "ok",
  failed: "danger",
  export_failed: "danger",
  cancelled: "muted",
  paused: "warn",
};

// 模型版本状态 → [中文标签, 徽标颜色]
const MODEL_STATUS = {
  candidate: ["候选", ""],
  active: ["当前版本", "ok"],
  archived: ["历史版本", "muted"],
};

// 评测指标：每一项都要显示分母 n
const METRIC_ROWS = [
  { key: "non_empty_rate", label: "非空输出率", countKey: "non_empty" },
  { key: "has_latex_rate", label: "含 LaTeX 结构率", countKey: "has_latex" },
  { key: "mean_block_coverage", label: "平均块覆盖率", denomKey: "coverage_denominator" },
  { key: "mean_similarity", label: "与参考文本相似度" },
  { key: "cjk_rate", label: "中文保留率", countKey: "cjk_preserved" },
  { key: "mean_latency_ms", label: "平均延迟", unit: "ms" },
];

// 模块级状态：轮询句柄、当前展开的运行、当前根节点与项目
let pollTimer = null;
let expandedRunId = "";
let rootEl = null;
let currentProjectId = "";

/* ------------------------------------------------------------------ */
/* 通用工具                                                            */
/* ------------------------------------------------------------------ */

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** 数字输入 → 数值；空值或非法值回退到默认值。 */
function numOr(input, fallback) {
  const raw = String(input.value ?? "").trim();
  if (!raw) return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

/** 表单字段外壳：标题 + 控件 + 可选说明。 */
function field(label, control, hint) {
  return h(
    "label",
    { class: "stack" },
    h("span", { class: "muted", style: { fontSize: "12px" } }, label),
    control,
    hint ? h("span", { class: "faint", style: { fontSize: "11.5px" } }, hint) : null
  );
}

function formatSeconds(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 60) return `${value.toFixed(1)} 秒`;
  // 先取整再拆分：先 Math.round(value % 60) 会出现"1 分 60 秒"（119.7s）
  const total = Math.round(value);
  const minutes = Math.floor(total / 60);
  return `${minutes} 分 ${total % 60} 秒`;
}

function formatNumber(value, digits = 4) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toFixed(digits);
}

/* ------------------------------------------------------------------ */
/* 训练运行：指标提取                                                  */
/* ------------------------------------------------------------------ */

/** 从 metrics 里取 loss（只有数值本身，不做任何“提升百分比”换算）。 */
function runLoss(run) {
  const metrics = run.metrics || {};
  const value = metrics.train_loss ?? metrics.loss;
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(4) : "—";
}

/** 训练步数。 */
function runSteps(run) {
  const metrics = run.metrics || {};
  const value = metrics.step ?? metrics.global_step ?? metrics.steps;
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  return Number.isFinite(number) ? String(number) : "—";
}

/** 耗时：优先用后端记录的 duration_seconds，否则用起止时间相减。 */
function runDuration(run) {
  const metrics = run.metrics || {};
  if (metrics.duration_seconds !== undefined && metrics.duration_seconds !== null) {
    return formatSeconds(Number(metrics.duration_seconds));
  }
  const started = run.started_at ? Date.parse(run.started_at) : NaN;
  const finished = run.finished_at ? Date.parse(run.finished_at) : NaN;
  if (Number.isFinite(started) && Number.isFinite(finished) && finished >= started) {
    return formatSeconds((finished - started) / 1000);
  }
  return "—";
}

function canCancel(run) {
  return ["queued", "pending", "running", "paused"].includes(run.status);
}

/* ------------------------------------------------------------------ */
/* 标题区：后端能力与 GPU 资源                                          */
/* ------------------------------------------------------------------ */

function buildBackendCard(backendInfo, backendError) {
  if (backendError) {
    return h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, h("h3", {}, "后端能力")),
      h(
        "div",
        { class: "notice danger" },
        h("strong", {}, "后端能力探测失败。"),
        h("div", { style: { marginTop: "4px" } }, backendError.message || String(backendError))
      )
    );
  }

  const backends = backendInfo?.backends || [];
  const rows = backends.map((backend) => [
    h("span", { class: "mono" }, backend.name),
    pill(backend.available ? "可用" : "不可用", backend.available ? "ok" : "danger"),
    backend.device || "—",
    h("span", { class: "faint mono" }, backend.version || "—"),
    h(
      "div",
      { class: "stack" },
      h("span", {}, backend.description || "—"),
      backend.executable ? h("span", { class: "faint mono" }, backend.executable) : null,
      backend.notes ? h("span", { class: "faint" }, backend.notes) : null
    ),
  ]);

  return h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("h3", {}, "训练后端")),
    backends.length
      ? table(["后端", "状态", "设备", "版本", "说明"], rows)
      : empty("后端没有返回任何训练后端")
  );
}

function buildResourceCard(resources) {
  if (!resources) {
    return h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, h("h3", {}, "资源状态")),
      empty("后端没有返回资源状态")
    );
  }

  const ollamaModels = resources.ollama_models || [];
  const modelRows = ollamaModels.map((model) => [
    h("span", { class: "mono" }, model.name || "—"),
    fmtBytes(model.size_vram || 0),
    model.expires_at ? fmtTime(model.expires_at) : "—",
  ]);

  const body = h(
    "div",
    { class: "stack" },
    h(
      "div",
      { class: "grid cols-4" },
      statCard("GPU 资源锁", resources.locked ? "占用中" : "空闲", {
        hint: resources.owner ? `持有者 ${resources.owner}` : "训练与推理串行调度",
        kind: resources.locked ? "warn" : "ok",
      }),
      statCard("Ollama 常驻模型", ollamaModels.length ? `${ollamaModels.length} 个` : "0 个", {
        hint: "常驻模型会占住显存",
      }),
      statCard("Ollama 占用显存", fmtBytes(resources.ollama_vram_bytes || 0), {
        hint: "来自 /api/ps",
      }),
      statCard("锁获取时间", resources.acquired_at ? fmtTime(resources.acquired_at) : "—", {
        hint: resources.acquired_at ? "" : "当前没有资源锁",
      })
    ),
    ollamaModels.length
      ? table(["模型", "显存", "过期时间"], modelRows)
      : empty("Ollama 当前没有常驻模型"),
    h("div", { class: "muted", style: { fontSize: "12px" } }, resources.hint || "")
  );

  return h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("h3", {}, "资源状态")),
    ollamaModels.length
      ? h(
          "div",
          { class: "alert warn row-between", style: { marginBottom: "10px" } },
          h(
            "div",
            {},
            h("strong", {}, "Ollama 正占用显存，训练前建议卸载常驻模型。"),
            h("div", { style: { marginTop: "3px" } }, "可点击右侧按钮一键释放，或在终端执行 ollama stop <模型名>。")
          ),
          h(
            "button",
            {
              type: "button",
              class: "btn danger sm",
              "data-testid": "training-unload-ollama-btn",
              onClick: async () => {
                try {
                  const res = await api.unloadOllama("");
                  toast(res.ok ? `已释放显存：${(res.unloaded || []).join(", ") || "完成"}` : `卸载警告：${(res.errors || []).join("; ")}`, res.ok ? "ok" : "warn");
                  window.dispatchEvent(new CustomEvent("gf:refresh"));
                } catch (err) {
                  showError(err);
                }
              },
            },
            "一键释放显存"
          )
        )
      : null,
    body
  );
}

/* ------------------------------------------------------------------ */
/* 新建训练运行表单                                                    */
/* ------------------------------------------------------------------ */

function buildRunForm({ snapshots, backends, quantMethods }) {
  const snapshotSelect = h(
    "select",
    {},
    snapshots.length
      ? snapshots.map((snapshot) => {
          const counts = snapshot.counts || {};
          const warnings = snapshot.manifest?.warnings || [];
          const label = `${snapshot.version} · train ${counts.train || 0} / dev ${counts.dev || 0} / holdout ${counts.holdout || 0}`;
          return h(
            "option",
            { value: snapshot.id, title: warnings.length ? warnings.join("；") : "" },
            warnings.length ? `${label}（${warnings.length} 条告警）` : label
          );
        })
      : [h("option", { value: "" }, "（还没有冻结的数据快照）")]
  );

  const snapshotNote = h("div", { class: "faint", style: { fontSize: "11.5px" } });
  function refreshSnapshotNote() {
    clear(snapshotNote);
    const snapshot = snapshots.find((item) => item.id === snapshotSelect.value);
    if (!snapshot) {
      snapshotNote.append("先在「数据集版本」冻结一个快照。");
      return;
    }
    const warnings = snapshot.manifest?.warnings || [];
    if (!warnings.length) {
      snapshotNote.append(`快照 ${snapshot.version} 无告警。`);
      return;
    }
    snapshotNote.append(
      h("span", { class: "pill pill-warn" }, `${warnings.length} 条告警`),
      " ",
      warnings.join("；")
    );
  }
  snapshotSelect.addEventListener("change", refreshSnapshotNote);
  refreshSnapshotNote();

  const availableBackends = backends.filter((backend) => backend.available);
  const backendSelect = h(
    "select",
    {},
    backends.length
      ? backends.map((backend, index) =>
          h(
            "option",
            {
              value: backend.name,
              disabled: !backend.available,
              // 全部不可用时选中第一项，避免下拉框显示成空白
              selected: !backend.available && !availableBackends.length && index === 0,
            },
            backend.available ? backend.name : `${backend.name}（不可用）`
          )
        )
      : [h("option", { value: "" }, "（没有可用后端）")]
  );
  // 全部后端都不可用时，浏览器不会选中任何项；仍然提交给后端，由预检如实报失败原因
  const backendName = () => backendSelect.value || backends[0]?.name || "local";

  const deviceSelect = h(
    "select",
    {},
    DEVICE_OPTIONS.map(([value, label]) => h("option", { value }, label))
  );

  const baseModelInput = h("input", { type: "text", placeholder: "/path/to/base-model", value: "" });
  const baseRevisionInput = h("input", { type: "text", placeholder: "commit hash（可留空）" });
  const tokenizerRevisionInput = h("input", { type: "text", placeholder: "commit hash（可留空）" });
  const chatTemplateInput = h("input", { type: "text", placeholder: "例如 qwen（可留空）" });

  const useLoraInput = h("input", { type: "checkbox", checked: true });
  const loadIn4bitInput = h("input", { type: "checkbox", checked: true });
  const loraRankInput = h("input", { type: "number", value: "16", min: "1" });
  const loraAlphaInput = h("input", { type: "number", value: "32", min: "1" });
  const loraDropoutInput = h("input", { type: "number", value: "0.05", min: "0", step: "0.01" });

  const seqLengthInput = h("input", { type: "number", value: "2048", min: "128" });
  const batchSizeInput = h("input", { type: "number", value: "1", min: "1" });
  const accumStepsInput = h("input", { type: "number", value: "16", min: "1" });
  const learningRateInput = h("input", { type: "number", value: "0.0002", min: "0", step: "0.00001" });
  const epochsInput = h("input", { type: "number", value: "3", min: "0", step: "0.1" });
  const seedInput = h("input", { type: "number", value: "42" });
  const notesInput = h("input", { type: "text", placeholder: "备注（可留空）" });

  const submitButton = h("button", { class: "btn primary" }, "提交训练");
  if (!snapshots.length) {
    submitButton.disabled = true;
  }

  submitButton.onclick = async () => {
    if (!snapshotSelect.value) {
      toast("请先在「数据集版本」冻结一个数据快照", "error");
      return;
    }
    if (!baseModelInput.value.trim()) {
      toast("请填写基座模型的本地目录", "error");
      return;
    }
    const payload = {
      dataset_snapshot_id: snapshotSelect.value,
      base_model: baseModelInput.value.trim(),
      base_revision: baseRevisionInput.value.trim(),
      tokenizer_revision: tokenizerRevisionInput.value.trim(),
      chat_template: chatTemplateInput.value.trim(),
      backend: backendName(),
      device: deviceSelect.value,
      use_lora: useLoraInput.checked,
      load_in_4bit: loadIn4bitInput.checked,
      lora_rank: numOr(loraRankInput, 16),
      lora_alpha: numOr(loraAlphaInput, 32),
      lora_dropout: numOr(loraDropoutInput, 0.05),
      max_seq_length: numOr(seqLengthInput, 2048),
      per_device_train_batch_size: numOr(batchSizeInput, 1),
      gradient_accumulation_steps: numOr(accumStepsInput, 16),
      learning_rate: numOr(learningRateInput, 0.0002),
      num_train_epochs: numOr(epochsInput, 3),
      warmup_ratio: 0.05,
      max_steps: -1,
      logging_steps: 5,
      save_steps: 100,
      seed: numOr(seedInput, 42),
      gradient_checkpointing: true,
      train_split: "train",
      eval_split: "dev",
      max_train_samples: -1,
      max_eval_samples: 32,
      notes: notesInput.value.trim(),
    };
    submitButton.disabled = true;
    try {
      const result = await api.post(`/api/training/runs?project_id=${currentProjectId}`, payload);
      toast(`训练任务已入队：${result.run_id}`, "ok");
      await reload();
    } catch (error) {
      showError(error);
      submitButton.disabled = false;
    }
  };

  return h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("h3", {}, "新建训练运行"), h("div", { class: "spacer" }),
      h("span", { class: "muted", style: { fontSize: "12px" } }, "训练读取的是冻结快照：快照一旦冻结就不可修改")),
    h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "grid cols-3" },
        field("数据快照", snapshotSelect, "按源分组切分，同一文档不跨集合"),
        field("基座模型（本地目录）", baseModelInput, "必须是本机可访问的模型目录"),
        field("训练后端", backendSelect, "不可用的后端已禁用")
      ),
      snapshotNote,
      h(
        "div",
        { class: "grid cols-3" },
        field("设备", deviceSelect, "auto 会自动探测 CUDA / CPU"),
        field("基座修订", baseRevisionInput),
        field("分词器修订", tokenizerRevisionInput)
      ),
      h(
        "div",
        { class: "grid cols-3" },
        field("Chat Template", chatTemplateInput, "记录用，导出时也会用到"),
        field("LoRA", useLoraInput, "关闭则做全参数微调"),
        field("QLoRA（4bit）", loadIn4bitInput, "无 CUDA 时会自动降级并在日志中说明")
      ),
      h(
        "div",
        { class: "grid cols-4" },
        field("LoRA rank", loraRankInput),
        field("LoRA alpha", loraAlphaInput),
        field("LoRA dropout", loraDropoutInput),
        field("序列长度", seqLengthInput)
      ),
      h(
        "div",
        { class: "grid cols-4" },
        field("单卡 batch", batchSizeInput),
        field("梯度累积", accumStepsInput),
        field("学习率", learningRateInput),
        field("训练轮数", epochsInput)
      ),
      h(
        "div",
        { class: "grid cols-3" },
        field("随机种子", seedInput),
        field("备注", notesInput),
        field(" ", submitButton)
      ),
      !availableBackends.length
        ? h(
            "div",
            { class: "notice warn" },
            "当前没有可用的训练后端：提交后会在预检阶段失败，运行记录里会写明具体原因。安装 torch / transformers 或 LLaMA-Factory 后再试。"
          )
        : null,
      !snapshots.length
        ? h("div", { class: "notice warn" }, "还没有冻结的数据快照，先去「数据集版本」冻结一个。")
        : null
    )
  );
}

/* ------------------------------------------------------------------ */
/* 训练运行列表与详情                                                  */
/* ------------------------------------------------------------------ */

function buildRunsCard(runs, loadError) {
  if (loadError) {
    return h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, h("h3", {}, "训练运行")),
      h(
        "div",
        { class: "notice danger" },
        h("strong", {}, "训练运行列表读取失败。"),
        h("div", { style: { marginTop: "4px" } }, loadError.message || String(loadError))
      )
    );
  }

  const rows = runs.map((run) => [
    h("span", { class: "mono" }, run.id),
    pill(run.status || "—", STATUS_KIND[run.status] || ""),
    run.backend || "—",
    h("span", { class: "mono" }, run.base_model || "—"),
    runLoss(run),
    runSteps(run),
    runDuration(run),
    run.ollama_name ? h("span", { class: "mono" }, run.ollama_name) : "—",
    h(
      "div",
      { class: "btn-row" },
      h(
        "button",
        { class: "btn ghost", onclick: () => toggleDetail(run.id) },
        expandedRunId === run.id ? "收起详情" : "查看详情"
      ),
      h("button", { class: "btn ghost", onclick: () => openExportDialog(run) }, "导出"),
      canCancel(run)
        ? h("button", { class: "btn danger", onclick: () => cancelRun(run) }, "取消")
        : null
    ),
  ]);

  const host = h("div", {});
  host.append(
    rows.length
      ? table(["运行", "状态", "后端", "基座", "最终 loss", "步数", "耗时", "生成模型", "操作"], rows)
      : empty("还没有训练运行")
  );

  return h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "card-title" },
      h("h3", {}, "训练运行"),
      h("div", { class: "spacer" }),
      h("span", { class: "muted", style: { fontSize: "12px" } }, "loss 为训练损失数值本身，不代表质量评分")
    ),
    host
  );
}

function buildRunDetail(detail) {
  const params = detail.params || {};
  const paramRows = [
    ["数据快照", params.dataset_snapshot_id || "—"],
    ["基座模型", params.base_model || "—"],
    ["基座修订", params.base_revision || "—"],
    ["分词器修订", params.tokenizer_revision || "—"],
    ["Chat Template", params.chat_template || "—"],
    ["后端", params.backend || "—"],
    ["设备", params.device || "—"],
    ["使用 LoRA", params.use_lora === undefined ? "—" : params.use_lora ? "是" : "否"],
    ["4bit（QLoRA）", params.load_in_4bit === undefined ? "—" : params.load_in_4bit ? "是" : "否"],
    ["LoRA rank / alpha / dropout", `${params.lora_rank ?? "—"} / ${params.lora_alpha ?? "—"} / ${params.lora_dropout ?? "—"}`],
    ["序列长度", params.max_seq_length ?? "—"],
    ["单卡 batch", params.per_device_train_batch_size ?? "—"],
    ["梯度累积", params.gradient_accumulation_steps ?? "—"],
    ["学习率", params.learning_rate ?? "—"],
    ["训练轮数", params.num_train_epochs ?? "—"],
    ["warmup 比例", params.warmup_ratio ?? "—"],
    ["max steps", params.max_steps ?? "—"],
    ["logging / save steps", `${params.logging_steps ?? "—"} / ${params.save_steps ?? "—"}`],
    ["随机种子", params.seed ?? "—"],
    ["梯度检查点", params.gradient_checkpointing === undefined ? "—" : params.gradient_checkpointing ? "是" : "否"],
    ["训练 / 评测划分", `${params.train_split ?? "—"} / ${params.eval_split ?? "—"}`],
    ["样本上限（训练 / 评测）", `${params.max_train_samples ?? "—"} / ${params.max_eval_samples ?? "—"}`],
    ["输出目录", params.output_dir || "—"],
  ];

  const jobRows = (detail.jobs || []).map((job) => [
    h("span", { class: "mono" }, job.id),
    pill(job.status || "—", STATUS_KIND[job.status] || ""),
    job.error ? h("span", { class: "muted" }, String(job.error).slice(0, 200)) : "—",
    fmtTime(job.created_at),
  ]);

  return h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "card-title" },
      h("h3", {}, `运行详情 · ${detail.id}`),
      h("div", { class: "spacer" }),
      pill(detail.status || "—", STATUS_KIND[detail.status] || "")
    ),
    h(
      "div",
      { class: "grid cols-4" },
      statCard("最终 loss", runLoss(detail), { hint: "训练损失数值本身" }),
      statCard("步数", runSteps(detail), { hint: "后端记录的步数" }),
      statCard("耗时", runDuration(detail), { hint: "含排队与训练" }),
      statCard("设备", params.device || "—", { hint: detail.base_model ? `基座 ${detail.base_model}` : "" })
    ),
    h(
      "div",
      { class: "row", style: { marginTop: "10px" } },
      pill(params.use_lora ? "LoRA" : "全参数微调", params.use_lora ? "accent" : "muted"),
      pill(params.load_in_4bit ? "4bit（QLoRA）" : "非 4bit", params.load_in_4bit ? "accent" : "muted"),
      pill(detail.ollama_name ? `Ollama：${detail.ollama_name}` : "尚未导出 Ollama 模型", "muted")
    ),
    detail.error
      ? h(
          "div",
          { class: "notice danger", style: { marginTop: "10px" } },
          h("strong", {}, "错误信息"),
          h("div", { style: { marginTop: "4px" } }, detail.error)
        )
      : null,
    h(
      "div",
      { class: "stack", style: { marginTop: "12px" } },
      h("h3", {}, "超参数"),
      table(
        ["参数", "值"],
        paramRows.map(([label, value]) => [label, h("span", { class: "mono" }, String(value))])
      ),
      h("h3", {}, "日志与产物"),
      table(
        ["项目", "路径"],
        [
          ["日志", h("span", { class: "mono" }, detail.log_path || "—")],
          ["适配器", h("span", { class: "mono" }, detail.adapter_path || "—")],
          ["量化产物", h("span", { class: "mono" }, detail.quantized_path || "—")],
          ["备注", detail.notes || "—"],
        ]
      ),
      h("h3", {}, "关联任务"),
      jobRows.length
        ? table(["任务", "状态", "错误", "创建时间"], jobRows)
        : empty("这个运行没有关联任务")
    )
  );
}

async function toggleDetail(runId) {
  if (expandedRunId === runId) {
    expandedRunId = "";
    clear(detailHost);
    return;
  }
  expandedRunId = runId;
  clear(detailHost).append(h("div", { class: "loading" }, "加载详情…"));
  try {
    const detail = await api.get(`/api/training/runs/${runId}`);
    if (expandedRunId !== runId) return;
    clear(detailHost).append(buildRunDetail(detail));
  } catch (error) {
    showError(error);
    clear(detailHost).append(h("div", { class: "notice danger" }, `加载详情失败：${error.message}`));
  }
  refreshRuns();
}

/* ------------------------------------------------------------------ */
/* 导出与取消                                                          */
/* ------------------------------------------------------------------ */

function openExportDialog(run) {
  const quantMethods = quantizeMethods.length ? quantizeMethods : QUANT_FALLBACK;
  const quantSelect = h(
    "select",
    {},
    quantMethods.map((method) => h("option", { value: method }, method))
  );
  const nameInput = h("input", { type: "text", placeholder: "留空则自动命名（如 godfather-latex:v003）" });

  modal({
    title: `导出模型 · ${run.id}`,
    body: h(
      "div",
      { class: "stack" },
      h("div", { class: "muted" }, "导出流程：合并适配器 → 转换 GGUF → 量化 → 导入 Ollama → 登记模型版本。导出任务在后台执行。"),
      field("量化方式", quantSelect),
      field("模型名（可留空）", nameInput)
    ),
    okText: "开始导出",
    onOk: async () => {
      try {
        const result = await api.post(`/api/training/runs/${run.id}/export?project_id=${currentProjectId}`, {
          quantization: quantSelect.value,
          model_name: nameInput.value.trim(),
          template: "",
          system_prompt: "",
        });
        toast(`导出任务已入队：${result.job_id}`, "ok");
        return true;
      } catch (error) {
        showError(error);
        return false;
      }
    },
  });
}

async function cancelRun(run) {
  const confirmed = await confirmDialog(
    `确定取消训练运行 ${run.id}？正在进行的训练进程会被终止；续训语义为重新开始（不含优化器状态）。`
  );
  if (!confirmed) return;
  try {
    const result = await api.post(`/api/training/runs/${run.id}/cancel`, {});
    toast(result.process_killed ? "已取消，训练进程已终止" : "已取消", "ok");
    await reload();
  } catch (error) {
    showError(error);
  }
}

/* ------------------------------------------------------------------ */
/* 模型版本                                                            */
/* ------------------------------------------------------------------ */

async function activateModel(model) {
  try {
    const result = await api.post(`/api/training/models/${model.id}/activate?project_id=${currentProjectId}`, {});
    toast(`已切换当前版本：${result.activated}`, "ok");
    await reload();
  } catch (error) {
    // 激活前有校验（模型还在不在、比不比基线差）。
    // 被拦下时给一个"仍然强制切换"的出口 —— 用户可能确实知道自己在做什么。
    if (error.status === 400) {
      const force = await confirmDialog(
        `切换被拦下：${error.message}\n\n仍然强制切换为当前版本？`
      );
      if (!force) return;
      try {
        const forced = await api.post(
          `/api/training/models/${model.id}/activate?project_id=${currentProjectId}&force=true`,
          {}
        );
        toast(`已强制切换当前版本：${forced.activated}`, "warn");
        await reload();
      } catch (forceError) {
        showError(forceError);
      }
      return;
    }
    showError(error);
  }
}

async function rollbackModel() {
  const confirmed = await confirmDialog(
    "确定回滚到上一个模型版本？当前版本会被归档，并立即成为新的当前版本。"
  );
  if (!confirmed) return;
  try {
    const result = await api.post(`/api/training/models/rollback?project_id=${currentProjectId}`, {});
    toast(`已回滚到：${result.activated}`, "ok");
    await reload();
  } catch (error) {
    showError(error);
  }
}

function buildModelsCard(models, activeModel) {
  const rows = models.map((model) => {
    const isActive = Boolean(activeModel) && model.name === activeModel;
    const [label, kind] = MODEL_STATUS[model.status] || [model.status || "—", ""];
    return [
      h(
        "div",
        { class: "row" },
        h("span", { class: "mono" }, model.name),
        isActive ? pill("当前版本", "ok") : null
      ),
      h("span", { class: "mono" }, model.base_model || "—"),
      model.quantization || "—",
      pill(label, kind),
      fmtTime(model.created_at),
      fmtTime(model.deployed_at),
      h(
        "div",
        { class: "btn-row" },
        isActive
          ? h("span", { class: "faint", style: { fontSize: "12px" } }, "正在使用")
          : h("button", { class: "btn ghost", onclick: () => activateModel(model) }, "设为当前版本")
      ),
    ];
  });

  return h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "card-title" },
      h("h3", {}, "模型版本"),
      h("div", { class: "spacer" }),
      activeModel ? h("span", { class: "pill pill-ok" }, `当前：${activeModel}`) : null,
      h(
        "button",
        { class: "btn ghost", onclick: () => rollbackModel() },
        "回滚到上一版本"
      )
    ),
    rows.length
      ? table(["名称", "基座", "量化", "状态", "创建时间", "部署时间", "操作"], rows)
      : empty("还没有模型版本，先完成一次训练并导出")
  );
}

/* ------------------------------------------------------------------ */
/* 评测                                                                */
/* ------------------------------------------------------------------ */

function metricCell(aggregate, metric) {
  if (!aggregate) return "—";
  const value = aggregate[metric.key];
  if (value === null || value === undefined) return "—";
  const text = metric.unit === "ms" ? `${Math.round(Number(value))} ms` : formatNumber(value);
  const parts = [];
  // 分母是**总样本数**（n）：后端现在按总样本算比率（失败计 0），
  // 显示时必须用同一个分母，否则"1.0（1/1）"与"1.0（1/4）"会自相矛盾。
  const denom = aggregate.n ?? "—";
  if (metric.countKey !== undefined && aggregate[metric.countKey] !== undefined) {
    parts.push(`${aggregate[metric.countKey]}/${denom}`);
  }
  if (metric.denomKey !== undefined && aggregate[metric.denomKey] !== undefined) {
    parts.push(`分母 ${aggregate[metric.denomKey]}/${denom}`);
  }
  if (!parts.length) parts.push(`n=${aggregate.n ?? "—"}`);
  // 有调用失败时要显式说出来，否则"率高"会被误读成"模型好"
  if (aggregate.errors) parts.push(`${aggregate.errors} 次调用失败`);
  return h("span", {}, text, h("span", { class: "faint" }, `（${parts.join("，")}）`));
}

function deltaCell(deltas, key) {
  if (!deltas) return "—";
  const value = deltas[key];
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const sign = number > 0 ? "+" : "";
  return h("span", { class: "mono" }, `${sign}${number.toFixed(4)}`);
}

function buildEvaluationTable(evaluation) {
  const metrics = evaluation.metrics || {};
  const aggregates = metrics.aggregates || {};
  const deltas = metrics.deltas || {};
  const rows = METRIC_ROWS.map((metric) => [
    metric.label,
    metricCell(aggregates.baseline, metric),
    metricCell(aggregates.candidate, metric),
    deltaCell(deltas, metric.key),
  ]);

  return h(
    "div",
    { class: "stack" },
    table(["指标", "基线", "候选", "差值（候选 − 基线）"], rows),
    h(
      "div",
      { class: "muted", style: { fontSize: "12px" } },
      `样本数 n=${metrics.n_examples ?? "—"}；括号内是该指标的分母（按**总样本**算，调用失败的样本计 0）。`
      + "差值只做减法，不做质量换算。"
      + (() => {
        // 评测方法要写出来：不同的采样设置/执行顺序，差值就不可比
        const method = metrics.method || {};
        const bits = [];
        if (method.paired) bits.push("配对执行（同一输入先基线后候选，抵消机器漂移）");
        if (method.seed !== undefined && method.seed !== null) bits.push(`采样种子 ${method.seed}`);
        if (method.temperature !== undefined) bits.push(`温度 ${method.temperature}`);
        return bits.length ? ` 评测方式：${bits.join("；")}。` : "";
      })()
      + (() => {
        // 调用失败必须显式说明：否则"率高"会被误读成"模型好"
        const failed = ["baseline", "candidate"]
          .map((role) => aggregates[role]?.errors || 0)
          .reduce((sum, value) => sum + value, 0);
        return failed ? ` 本次共 ${failed} 次调用失败（失败样本已从指标里剔除）。` : "";
      })()
    )
  );
}

function buildEvaluationsCard(evaluations, loadError) {
  if (loadError) {
    return h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, h("h3", {}, "评测结果")),
      h(
        "div",
        { class: "notice danger" },
        h("strong", {}, "评测结果读取失败。"),
        h("div", { style: { marginTop: "4px" } }, loadError.message || String(loadError))
      )
    );
  }
  if (!evaluations.length) {
    return h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, h("h3", {}, "评测结果")),
      empty("还没有评测结果，先提交一次评测")
    );
  }

  const [latest, ...rest] = evaluations;
  const history = rest.map((evaluation) => {
    const metrics = evaluation.metrics || {};
    const aggregates = metrics.aggregates || {};
    return h(
      "details",
      { class: "card", style: { display: "block" } },
      h(
        "summary",
        { style: { cursor: "pointer" } },
        h("strong", {}, evaluation.id),
        " ",
        h("span", { class: "muted" }, `候选 ${evaluation.candidate_model_id || "—"} / 基线 ${evaluation.baseline_model_id || "—"}`),
        " ",
        pill(evaluation.split || "—", "accent"),
        " ",
        h("span", { class: "faint" }, `n=${metrics.n_examples ?? "—"}`),
        " ",
        h("span", { class: "faint" }, fmtTime(evaluation.created_at))
      ),
      h("div", { style: { marginTop: "10px" } }, buildEvaluationTable(evaluation)),
      h(
        "div",
        { class: "muted", style: { fontSize: "12px", marginTop: "8px" } },
        `基线失败 ${(aggregates.baseline?.failures || []).length} 条 / 候选失败 ${(aggregates.candidate?.failures || []).length} 条`
      )
    );
  });

  return h(
    "div",
    { class: "stack" },
    h(
      "div",
      { class: "card" },
      h(
        "div",
        { class: "card-title" },
        h("h3", {}, "最近一次评测"),
        h("div", { class: "spacer" }),
        h("span", { class: "muted", style: { fontSize: "12px" } }, `${latest.id} · ${fmtTime(latest.created_at)}`)
      ),
      buildEvaluationTable(latest)
    ),
    h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, h("h3", {}, `历史评测（${evaluations.length}）`)),
      history.length ? h("div", { class: "stack" }, history) : empty("只有最近一次评测")
    )
  );
}

function buildEvaluationForm({ snapshots, models, activeModel, ollamaModels }) {
  const candidateOptions = [];
  for (const model of models) {
    candidateOptions.push([model.name, `${model.name}（${model.quantization || "未标注量化"}）`]);
  }
  for (const model of ollamaModels) {
    if (model.name && !models.some((item) => item.name === model.name)) {
      candidateOptions.push([model.name, `${model.name}（Ollama 常驻）`]);
    }
  }

  const candidateSelect = h(
    "select",
    {},
    [
      ...candidateOptions.map(([value, label]) => h("option", { value }, label)),
      h("option", { value: "" }, "其它（手填 Ollama 模型名）"),
    ]
  );
  // 只有选中「手填」时才允许输入模型名
  const candidateInput = h("input", {
    type: "text",
    placeholder: "例如 qwen2.5:7b",
    disabled: candidateSelect.value !== "",
  });
  candidateSelect.addEventListener("change", () => {
    const manual = candidateSelect.value === "";
    candidateInput.disabled = !manual;
    if (!manual) candidateInput.value = "";
  });

  const baselineInput = h("input", {
    type: "text",
    placeholder: "基线模型（Ollama 模型名）",
    value: activeModel || "",
  });

  const snapshotSelect = h(
    "select",
    {},
    snapshots.length
      ? snapshots.map((snapshot) =>
          h("option", { value: snapshot.id }, `${snapshot.version} · dev ${(snapshot.counts || {}).dev || 0}`)
        )
      : [h("option", { value: "" }, "（没有可用快照）")]
  );

  const splitSelect = h(
    "select",
    {},
    SPLIT_OPTIONS.map(([value, label]) => h("option", { value }, label))
  );
  const limitInput = h("input", { type: "number", value: "32", min: "1" });

  const submitButton = h("button", { class: "btn primary" }, "提交评测");
  submitButton.onclick = async () => {
    const candidate = candidateSelect.value || candidateInput.value.trim();
    if (!candidate) {
      toast("请选择或填写候选模型", "error");
      return;
    }
    submitButton.disabled = true;
    try {
      const result = await api.post(`/api/training/evaluations?project_id=${currentProjectId}`, {
        candidate_model: candidate,
        baseline_model: baselineInput.value.trim(),
        snapshot_id: snapshotSelect.value,
        split: splitSelect.value,
        limit: numOr(limitInput, 32),
        run_id: "",
      });
      toast(`评测任务已入队：${result.job_id}`, "ok");
      await reload();
    } catch (error) {
      showError(error);
      submitButton.disabled = false;
    }
  };

  return h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "card-title" },
      h("h3", {}, "发起评测"),
      h("div", { class: "spacer" }),
      h("span", { class: "muted", style: { fontSize: "12px" } }, "评测在后台执行，完成后结果会出现在下方表格")
    ),
    h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "grid cols-3" },
        field("候选模型", candidateSelect),
        field("候选模型名（手填）", candidateInput, "选「其它」后可填写"),
        field(
          "基线模型",
          baselineInput,
          activeModel ? `默认当前版本 ${activeModel}` : "项目还没有当前版本"
        )
      ),
      h(
        "div",
        { class: "grid cols-4" },
        field("评测快照", snapshotSelect),
        field("评测集", splitSelect),
        field("条数上限", limitInput),
        field(" ", submitButton)
      ),
      !snapshots.length
        ? h("div", { class: "notice warn" }, "还没有冻结的数据快照，无法评测。")
        : null
    )
  );
}

/* ------------------------------------------------------------------ */
/* 页面渲染                                                            */
/* ------------------------------------------------------------------ */

let quantizeMethods = [];
let detailHost = null;
let runsHost = null;
let evaluationsHost = null;
let runsSignature = "";
let evaluationsSignature = "";

async function reload() {
  const previous = rootEl;
  try {
    const next = await render();
    // 过期渲染（返回 null）：不要动 DOM，
    // 让更新的那次渲染接管，否则会把页面换成"加载中…"并停掉轮询。
    if (!next) return;
    if (!previous || !previous.parentNode) {
      // 页面已经被换掉了（例如用户切了路由）：这次替换作废，
      // 否则新的 root 永远不会接上 DOM，轮询也会自杀。
      stopPolling();
      return;
    }
    previous.parentNode.replaceChild(next, previous);
  } catch (error) {
    showError(error);
  }
}

/** 轮询：只刷新运行列表与评测结果，保持表单内容不被冲掉。 */
function refreshRuns() {
  if (!runsHost) return;
  api
    .get(`/api/training/runs?project_id=${currentProjectId}`)
    .then((runs) => {
      if (!runsHost) return;
      // 内容没变就不动 DOM，避免打断正在展开的详情与鼠标操作
      const signature = JSON.stringify(
        runs.map((run) => [run.id, run.status, run.error, run.ollama_name, run.metrics, run.finished_at]),
      ) + `#${expandedRunId}`;
      if (signature === runsSignature) return;
      runsSignature = signature;
      // 保留卡片外壳，只替换内部内容
      clear(runsHost).append(...buildRunsCard(runs).childNodes);
    })
    .catch((error) => {
      // 列表读取失败只影响这一块，不影响页面其它部分
      if (!runsHost) return;
      runsSignature = "";
      clear(runsHost).append(...buildRunsCard([], error).childNodes);
    });
}

function refreshEvaluations() {
  if (!evaluationsHost) return;
  api
    .get(`/api/training/evaluations?project_id=${currentProjectId}`)
    .then((evaluations) => {
      if (!evaluationsHost) return;
      const signature = JSON.stringify(
        evaluations.map((evaluation) => [evaluation.id, evaluation.created_at, evaluation.metrics])
      );
      if (signature === evaluationsSignature) return;
      evaluationsSignature = signature;
      clear(evaluationsHost).append(buildEvaluationsCard(evaluations, null));
    })
    .catch((error) => {
      // 评测列表读取失败只影响这一块，不影响页面其它部分
      if (!evaluationsHost) return;
      evaluationsSignature = "";
      clear(evaluationsHost).append(buildEvaluationsCard([], error));
    });
}

function startPolling(root) {
  stopPolling();
  pollTimer = setInterval(() => {
    if (!root.isConnected) {
      stopPolling();
      return;
    }
    refreshRuns();
    refreshEvaluations();
  }, POLL_MS);
}

// 渲染序号：并发渲染时只有最后一次的结果能生效。
// 没有它的话，慢的那次渲染会在 await 之后覆盖模块级全局（rootEl / runsHost…），
// 并把轮询定时器绑到已经脱离 DOM 的节点上 —— 轮询从此静默死掉，
// 页面看起来"正常"但状态再也不更新了。
let renderToken = 0;

export async function render() {
  stopPolling();
  const token = ++renderToken;
  const projectId = state.projectId;
  if (!projectId) return h("div", { class: "empty" }, "请先创建项目。");
  const projectChanged = currentProjectId !== projectId;
  currentProjectId = projectId;
  if (projectChanged) expandedRunId = "";

  let backendInfo = null;
  let backendError = null;
  try {
    backendInfo = await api.get("/api/training/backends");
    quantizeMethods = backendInfo?.quantize_methods || [];
  } catch (error) {
    backendError = error;
  }

  const [snapshots, runs, modelsPayload, evaluationsResult] = await Promise.all([
    api.get(`/api/projects/${projectId}/datasets/snapshots`),
    api.get(`/api/training/runs?project_id=${projectId}`),
    api.get(`/api/training/models?project_id=${projectId}`),
    // 评测列表单独兜底：读取失败只让这一块报错，不拖垮整页
    api
      .get(`/api/training/evaluations?project_id=${projectId}`)
      .then((data) => ({ data: data || [], error: null }))
      .catch((error) => ({ data: [], error })),
  ]);
  const evaluations = evaluationsResult.data;

  const models = modelsPayload?.models || [];
  const activeModel = modelsPayload?.active_model || "";
  const resources = backendInfo?.resources || null;
  const backends = backendInfo?.backends || [];
  const ollamaModels = resources?.ollama_models || [];

  // 渲染期间又发起了新的渲染：这次结果作废（否则会覆盖新状态）。
  // 返回 null 而不是占位符 —— 占位符会让调用方把页面换成"加载中…"，
  // 而更新的那次渲染的节点又被丢弃 → 页面永久卡在"加载中…"且轮询停摆。
  if (token !== renderToken) return null;

  const root = h("div", {});
  rootEl = root;

  // ---- 标题区 ----
  const backendSummary = backendError
    ? pill("后端能力未知", "danger")
    : pill(
        `可用后端 ${backends.filter((backend) => backend.available).length}/${backends.length}`,
        backends.some((backend) => backend.available) ? "ok" : "danger"
      );
  const gpuSummary = resources
    ? pill(
        resources.locked ? `GPU 占用中${resources.owner ? `（${resources.owner}）` : ""}` : "GPU 空闲",
        resources.locked ? "warn" : "ok"
      )
    : pill("资源状态未知", "muted");

  root.append(
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        {},
        h("h1", {}, "训练与模型"),
        h(
          "div",
          { class: "sub" },
          "M1：真实训练、导出与评测——本页所有数字都来自后端记录，没有 mock 指标"
        )
      ),
      h("div", { class: "spacer" }),
      h("div", { class: "row" }, backendSummary, gpuSummary)
    )
  );

  // ---- 后端能力 ----
  root.append(buildBackendCard(backendInfo, backendError));

  // ---- 资源状态 ----
  root.append(buildResourceCard(resources));

  // ---- 新建训练运行 ----
  root.append(buildRunForm({ snapshots, backends, quantizeMethods }));

  // ---- 训练运行 ----
  runsHost = buildRunsCard(runs);
  runsSignature = JSON.stringify(
    runs.map((run) => [run.id, run.status, run.error, run.ollama_name, run.metrics, run.finished_at])
  );
  root.append(runsHost);

  // ---- 运行详情（点击「查看详情」后出现） ----
  detailHost = h("div", {});
  root.append(detailHost);
  // 重新渲染后详情面板会被清空，但 expandedRunId 还留着 →
  // 按钮显示"收起详情"而面板不见了。这里补载一次。
  if (expandedRunId) {
    const runId = expandedRunId;
    clear(detailHost).append(h("div", { class: "loading" }, "加载详情…"));
    api
      .get(`/api/training/runs/${runId}`)
      .then((detail) => {
        if (expandedRunId !== runId || !detailHost) return;
        clear(detailHost).append(buildRunDetail(detail));
      })
      .catch(() => {
        if (expandedRunId !== runId || !detailHost) return;
        clear(detailHost).append(h("div", { class: "notice danger" }, "加载详情失败"));
      });
  }

  // ---- 模型版本 ----
  root.append(buildModelsCard(models, activeModel));

  // ---- 评测 ----
  root.append(buildEvaluationForm({ snapshots, models, activeModel, ollamaModels }));

  evaluationsHost = h("div", {});
  evaluationsHost.append(buildEvaluationsCard(evaluations, evaluationsResult.error));
  evaluationsSignature = evaluationsResult.error
    ? ""
    : JSON.stringify(evaluations.map((evaluation) => [evaluation.id, evaluation.created_at, evaluation.metrics]));
  root.append(evaluationsHost);

  // 绑定轮询前再确认一次：渲染期间可能又发起了新渲染
  if (token !== renderToken) return null;
  startPolling(root);
  return root;
}
