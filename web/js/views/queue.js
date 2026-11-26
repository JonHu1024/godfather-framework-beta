// 生成与任务队列：按任务包展示可用模式、切换本地 Ollama / 远程 OpenAI 兼容 API（BUG-03）、查看实时日志
import { api, artifactUrl } from "../api.js";
import { currentTaskpackSpec } from "../state.js";
import { clear, empty, fmtTime, h, modal, pill, pre, showError, table, toast } from "../ui.js";

const FALLBACK_MODES = [
  { id: "pandoc_baseline", label: "pandoc_baseline（Pandoc 工具基线）", uses_teacher: false },
  { id: "rule_only", label: "rule_only（纯内置规则转换）", uses_teacher: false },
  { id: "tool_assisted", label: "tool_assisted（Pandoc + 教师协同）", uses_teacher: true },
  { id: "teacher_direct", label: "teacher_direct（教师模型直出，用于 SFT）", uses_teacher: true },
];

export async function render(state) {
  const root = h("div", { class: "stack-lg", "data-testid": "view-queue" });
  if (!state.currentProjectId) {
    root.append(empty("请先在顶部新建或选择一个项目。"));
    return root;
  }

  const [docs, generations, jobs, ma] = await Promise.all([
    api.documents(state.currentProjectId),
    api.generations(state.currentProjectId),
    api.jobs(state.currentProjectId),
    api.modelsAccess().catch(() => null),
  ]);

  const spec = currentTaskpackSpec();
  const isSft = spec?.id === "general_sft";
  const modes = spec?.modes?.length ? spec.modes : FALLBACK_MODES;

  const docSelect = h(
    "select",
    { "data-testid": "queue-doc-select" },
    docs.map((d) => h("option", { value: d.id }, `${d.title || d.id}${d.block_count != null ? ` (${d.block_count} 块)` : ""}`))
  );
  const modeSelect = h(
    "select",
    { "data-testid": "queue-mode-select" },
    modes.map((m) => h("option", { value: m.id }, `${m.id} — ${m.label}`))
  );

  // 动态装配可用的 Provider 与模型默认建议
  const providersList = [];
  const defaultProv = ma?.default_teacher_provider || "ollama";
  if (ma?.providers) {
    if (ma.providers.ollama?.enabled) {
      providersList.push({
        id: "ollama",
        label: `Ollama 本地 (${ma.providers.ollama.teacher_model || "默认"})`,
        defaultModel: ma.providers.ollama.teacher_model || "",
      });
    }
    if (ma.providers.openai_compatible?.enabled) {
      providersList.push({
        id: "openai_compatible",
        label: `远程 API (${ma.providers.openai_compatible.teacher_model || "已配置"})`,
        defaultModel: ma.providers.openai_compatible.teacher_model || "",
      });
    }
    if (ma.providers.manual?.enabled) {
      providersList.push({ id: "manual_import", label: "手工导入 (manual_import)", defaultModel: "" });
    }
  } else {
    providersList.push(
      { id: "ollama", label: "Ollama 本地 (ollama)", defaultModel: "" },
      { id: "openai_compatible", label: "远程 API (openai_compatible)", defaultModel: "" },
      { id: "manual_import", label: "手工导入 (manual_import)", defaultModel: "" }
    );
  }
  if (!providersList.length) {
    providersList.push({ id: "ollama", label: "Ollama (默认)", defaultModel: "" });
  }

  const providerSelect = h(
    "select",
    { "data-testid": "queue-provider-select" },
    providersList.map((p) => h("option", { value: p.id, selected: p.id === defaultProv }, p.label))
  );

  const modelInput = h("input", {
    list: "queue-models-datalist",
    placeholder: "模型名称（留空用当前提供方默认模型）",
    "data-testid": "queue-model-input",
  });
  const modelDatalist = h("datalist", { id: "queue-models-datalist" });

  async function syncProviderModels() {
    const selected = providersList.find((p) => p.id === providerSelect.value);
    if (selected?.defaultModel && !modelInput.value) {
      modelInput.placeholder = `留空默认：${selected.defaultModel}`;
    }
    try {
      const res = await api.providerModels(providerSelect.value);
      clear(modelDatalist);
      for (const m of res.models || []) modelDatalist.append(h("option", { value: m }));
    } catch {
      // ignore if unreachable
    }
  }
  providerSelect.addEventListener("change", () => {
    modelInput.value = "";
    syncProviderModels();
  });
  syncProviderModels();

  const templateSelect = h(
    "select",
    { "data-testid": "queue-template-select" },
    [
      ["preamble_zh_ctex", "中文学术文档 (ctexart)"],
      ["preamble_en_academic", "英文学术文档 (article)"],
    ].map(([k, v]) => h("option", { value: k }, v))
  );

  const startBtn = h(
    "button",
    {
      type: "button",
      class: "btn primary",
      disabled: !docs.length,
      "data-testid": "queue-start-btn",
      onClick: async () => {
        try {
          const res = await api.startGeneration(state.currentProjectId, {
            document_id: docSelect.value,
            mode: modeSelect.value,
            provider: providerSelect.value,
            model: modelInput.value.trim(),
            template_name: templateSelect.value,
          });
          toast(
            res.reused
              ? `该文档/模式/模型组合已有运行记录 (${res.job_id})，请直接查看进度`
              : `已提交任务 ${res.job_id} (${res.job_type || "generate"} · ${res.provider})`,
            res.reused ? "warn" : "ok"
          );
          window.dispatchEvent(new CustomEvent("gf:refresh"));
        } catch (err) {
          showError(err);
        }
      },
    },
    "发起生成任务"
  );

  root.append(
    h(
      "div",
      { class: "page-head" },
      h("div", {}, h("h1", {}, "生成与队列"), h("p", {}, "选择转换模式、教师提供方与模板发起生成任务。")),
      h("a", { class: "btn sm", href: "#/models" }, "配置模型接入")
    ),
    h(
      "section",
      { class: "card stack", "data-testid": "queue-launch-card" },
      h("h2", {}, "提交新生成任务"),
      modelDatalist,
      h(
        "div",
        { class: "grid cols-4" },
        h("label", { class: "stack" }, h("span", { class: "muted" }, "目标文档"), docSelect),
        h("label", { class: "stack" }, h("span", { class: "muted" }, "生成模式"), modeSelect),
        h("label", { class: "stack" }, h("span", { class: "muted" }, "教师提供方"), providerSelect),
        h("label", { class: "stack" }, h("span", { class: "muted" }, "模型覆盖（可选）"), modelInput)
      ),
      h(
        "div",
        { class: "row-between" },
        !isSft ? h("label", { class: "row" }, h("span", { class: "muted" }, "导言区模板："), templateSelect) : h("span"),
        startBtn
      )
    )
  );

  // 生成轮次表（保持 artifactUrl 携带局域网会话令牌）
  root.append(
    h(
      "section",
      { class: "card stack", "data-testid": "queue-generations-card" },
      h("h2", {}, `生成轮次 (${generations.length})`),
      generations.length
        ? table(
            ["轮次 ID", "模式", "提供方 / 模型", "状态", "产物下载 / 预览", "时间"],
            generations.map((g) => {
              const links = [];
              if (g.tex_artifact_id) {
                links.push(h("a", { href: artifactUrl(g.tex_artifact_id), target: "_blank" }, isSft ? "JSONL" : ".tex"));
              }
              if (g.pdf_artifact_id) {
                links.push(h("a", { href: artifactUrl(g.pdf_artifact_id), target: "_blank" }, "PDF"));
              }
              return [
                h("span", { class: "mono" }, g.id),
                pill(g.mode, "accent"),
                `${g.provider || "rule"}${g.model ? ` / ${g.model}` : ""}`,
                pill(g.status, g.status === "succeeded" ? "ok" : g.status === "failed" ? "danger" : "warn"),
                links.length ? h("div", { class: "row" }, ...links) : "—",
                fmtTime(g.created_at),
              ];
            })
          )
        : empty("尚未执行过生成任务")
    )
  );

  // 任务队列与状态控制
  root.append(
    h(
      "section",
      { class: "card stack", "data-testid": "queue-jobs-card" },
      h("h2", {}, `后台任务队列 (${jobs.length})`),
      jobs.length
        ? table(
            ["任务 ID", "类型", "状态", "进度 / 阶段", "错误信息", "操作"],
            jobs.map((j) => {
              const actions = [
                h(
                  "button",
                  {
                    type: "button",
                    class: "btn ghost sm",
                    onClick: async () => {
                      const d = await api.jobDetail(j.id);
                      const logs = (d.logs || []).map((l) => `[${l.ts}] ${l.level}: ${l.message}`).join("\n") || "暂无详细日志";
                      modal({ title: `任务详情：${j.id}`, body: pre(logs), okText: "关闭" });
                    },
                  },
                  "日志"
                ),
              ];
              if (j.status === "queued" || j.status === "running") {
                actions.push(
                  h(
                    "button",
                    {
                      type: "button",
                      class: "btn danger sm",
                      onClick: async () => {
                        await api.cancelJob(j.id);
                        toast(`已取消任务 ${j.id}`, "ok");
                        window.dispatchEvent(new CustomEvent("gf:refresh"));
                      },
                    },
                    "取消"
                  )
                );
              } else if (String(j.status).startsWith("paused") || j.status === "waiting_user") {
                actions.push(
                  h(
                    "button",
                    {
                      type: "button",
                      class: "btn primary sm",
                      onClick: async () => {
                        await api.resumeJob(j.id);
                        toast(`已恢复任务 ${j.id}`, "ok");
                        window.dispatchEvent(new CustomEvent("gf:refresh"));
                      },
                    },
                    "恢复"
                  )
                );
              }
              return [
                h("span", { class: "mono" }, j.id),
                j.type,
                pill(j.status, j.status === "succeeded" ? "ok" : j.status === "failed" ? "danger" : "warn"),
                `${j.progress_done}/${j.progress_total || "?"} (${j.stage || "—"})`,
                h("span", { class: "muted", style: { maxWidth: "260px", display: "inline-block" } }, j.error || "—"),
                h("div", { class: "row" }, ...actions),
              ];
            })
          )
        : empty("任务队列为空")
    )
  );

  const hasActiveJobs = jobs.some((j) => j.status === "queued" || j.status === "running");
  let pollTimer = null;
  const onEvent = (ev) => {
    const stage = ev?.detail?.stage;
    if (stage === "done" || stage === "failed") {
      window.dispatchEvent(new CustomEvent("gf:refresh"));
    }
  };
  const teardown = () => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    window.removeEventListener("gf:event", onEvent);
  };
  window.addEventListener("gf:event", onEvent);
  window.addEventListener("gf:teardown", teardown, { once: true });
  if (hasActiveJobs) {
    pollTimer = setInterval(() => {
      if (!root.isConnected) {
        teardown();
        return;
      }
      window.dispatchEvent(new CustomEvent("gf:refresh"));
    }, 3000);
  }

  return root;
}
