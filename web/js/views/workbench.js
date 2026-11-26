// 标注工作台：三栏校对、多选源块定位器 (BlockPicker)、就地确认修订、编辑与删除批注
import { AnnotationLayer } from "../annotate.js";
import { api, artifactUrl, req } from "../api.js";
import { createBlockPicker } from "../components/block_picker.js";
import { PdfView } from "../pdfview.js";
import { currentProject } from "../state.js";
import { clear, confirmDialog, empty, fmtTime, h, modal, pill, showError, toast } from "../ui.js";

const CATEGORIES = [
  ["notation", "数学符号/公式问题"],
  ["layout", "排版或分页异常"],
  ["table_overflow", "表格越界/列宽不当"],
  ["missing_content", "漏段/内容缺失/误改"],
  ["style", "样式或字体不一致"],
  ["other", "其他问题"],
];

const CONFIDENCE_LABELS = {
  confirmed: ["已确认定位", "ok"],
  matched: ["自动命中", "accent"],
  needs_review: ["待确认源块", "warn"],
};

let activePdfView = null;
let lastDocId = "";
let lastGenId = "";
let statusFilter = "all";
let selectedAnnotationId = "";

function normalizeText(text) {
  return (text || "")
    .replace(/\s+/g, "")
    .replace(/[，。！？、；：""''（）()]/g, "")
    .toLowerCase();
}

function inferCandidateBlocks(pdfView, blocks, pageIndex) {
  const pageText = normalizeText(pdfView?.pageText(pageIndex) || "");
  if (!pageText) return [];
  const matches = [];
  for (const block of blocks) {
    const snippet = normalizeText((block.text || "").slice(0, 26));
    if (snippet.length >= 4 && pageText.includes(snippet)) {
      matches.push(block.block_id);
    }
  }
  return matches.slice(0, 8);
}

async function loadTeacherRoutingOptions() {
  try {
    const ma = await api.modelsAccess();
    const providers = [];
    if (ma.providers?.ollama?.enabled) {
      providers.push({
        id: "ollama",
        label: `Ollama (${ma.providers.ollama.teacher_model || "本地"})`,
        defaultModel: ma.providers.ollama.teacher_model || "",
      });
    }
    if (ma.providers?.openai_compatible?.enabled) {
      providers.push({
        id: "openai_compatible",
        label: `远程 API (${ma.providers.openai_compatible.teacher_model || "未设模型"})`,
        defaultModel: ma.providers.openai_compatible.teacher_model || "",
      });
    }
    if (ma.providers?.manual?.enabled) {
      providers.push({ id: "manual_import", label: "离线手工导入", defaultModel: "" });
    }
    return {
      defaultProvider: ma.default_teacher_provider || providers[0]?.id || "ollama",
      providers,
    };
  } catch {
    return {
      defaultProvider: "ollama",
      providers: [{ id: "ollama", label: "Ollama 本地", defaultModel: "" }],
    };
  }
}

export async function render(state) {
  const root = h("div", { class: "stack-lg", "data-testid": "view-workbench" });
  if (!state.currentProjectId) {
    root.append(empty("请先在顶部新建或选择一个项目。"));
    return root;
  }

  const project = currentProject();
  const isLatex = !project || project.taskpack === "markdown_to_latex";

  const docs = await api.documents(state.currentProjectId);
  if (!docs.length) {
    root.append(
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "标注与修订工作台"))),
      empty("暂无文档：请先在「材料与参考」中导入 Markdown 或语料，并在「生成与队列」跑完至少一轮生成。")
    );
    return root;
  }

  if (!lastDocId || !docs.some((d) => d.id === lastDocId)) lastDocId = docs[0].id;

  const [detail, allGenerations, allAnnotations] = await Promise.all([
    api.documentDetail(lastDocId),
    api.generations(state.currentProjectId),
    api.annotations(state.currentProjectId, lastDocId),
  ]);

  const docGens = allGenerations.filter((g) => g.document_id === lastDocId);
  if (!lastGenId || !docGens.some((g) => g.id === lastGenId)) {
    const withPdf = docGens.find((g) => g.pdf_artifact_id);
    lastGenId = (withPdf || docGens[0] || {}).id || "";
  }
  const activeGen = docGens.find((g) => g.id === lastGenId) || null;
  const genDetail = activeGen ? await api.generationDetail(activeGen.id) : null;
  const blocks = detail.blocks || [];

  // 记录页面离开或重绘时的观察器清理
  const cleanup = () => {
    if (activePdfView) {
      activePdfView.destroy();
      activePdfView = null;
    }
  };
  window.removeEventListener("gf:teardown", cleanup);
  window.addEventListener("gf:teardown", cleanup, { once: true });

  // ---- 顶栏选择器与产物链接 ----
  const docSelect = h(
    "select",
    { "aria-label": "选择文档", "data-testid": "wb-doc-select" },
    docs.map((d) => h("option", { value: d.id, selected: d.id === lastDocId }, `${d.title || d.id}${d.block_count != null ? ` (${d.block_count} 块)` : ""}`))
  );
  docSelect.addEventListener("change", () => {
    lastDocId = docSelect.value;
    lastGenId = "";
    selectedAnnotationId = "";
    window.dispatchEvent(new CustomEvent("gf:refresh"));
  });

  const genSelect = h(
    "select",
    { "aria-label": "选择生成轮次", "data-testid": "wb-gen-select" },
    docGens.length
      ? docGens.map((g) =>
          h(
            "option",
            { value: g.id, selected: g.id === lastGenId },
            `${g.mode} · ${g.status} · ${g.provider || "rule"}${g.model ? `/${g.model}` : ""} · ${fmtTime(g.created_at)}`
          )
        )
      : [h("option", { value: "" }, "尚无生成轮次")]
  );
  genSelect.addEventListener("change", () => {
    lastGenId = genSelect.value;
    window.dispatchEvent(new CustomEvent("gf:refresh"));
  });

  const links = h("div", { class: "row" });
  if (activeGen?.tex_artifact_id) {
    links.append(h("a", { class: "btn sm", href: artifactUrl(activeGen.tex_artifact_id), target: "_blank", "data-testid": "wb-tex-link" }, isLatex ? "查看 .tex" : "查看产物 JSONL"));
  }
  const activeLogId = activeGen?.stats?.compile?.log_artifact_id;
  if (activeLogId) {
    links.append(h("a", { class: activeGen.status === "failed" ? "btn sm danger" : "btn sm", href: artifactUrl(activeLogId), target: "_blank", "data-testid": "wb-log-link" }, "编译日志 (.log)"));
  }
  if (activeGen?.pdf_artifact_id) {
    links.append(h("a", { class: "btn sm", href: artifactUrl(activeGen.pdf_artifact_id), target: "_blank", "data-testid": "wb-pdf-link" }, "新标签打开 PDF"));
  }

  root.append(
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        {},
        h("h1", {}, "批注工作台"),
        h(
          "p",
          {},
          isLatex
            ? "拖拽 PDF 框选或左侧多选源块创建批注，确认定位后提交修订。"
            : "选择源条目创建批注或发起局部修订。"
        )
      ),
      h("div", { class: "row" }, docSelect, genSelect, links)
    )
  );

  // ---- 编译失败提示（直接暴露可点开的 .tex 与 .log 诊断入口）----
  if (activeGen && activeGen.status === "failed" && isLatex) {
    const compErr = activeGen.stats?.compile?.error || "LaTeX 编译未通过，请查看编译日志或源码排查。";
    root.append(
      h(
        "div",
        { class: "alert danger row-between", "data-testid": "wb-compile-error-banner" },
        h("div", {}, h("strong", {}, "本轮编译失败："), compErr),
        h(
          "div",
          { class: "art-links" },
          activeGen.tex_artifact_id ? h("a", { class: "art-chip", href: artifactUrl(activeGen.tex_artifact_id), target: "_blank" }, ".tex") : null,
          activeLogId ? h("a", { class: "art-chip is-danger", href: artifactUrl(activeLogId), target: "_blank" }, ".log") : null
        )
      )
    );
  }

  // ---- 四层质量校验提醒（含截断与 <think> 泄漏报警）----
  if (genDetail?.validation_reports?.length) {
    const alerts = [];
    for (const rep of genDetail.validation_reports) {
      if (rep.verdict === "fail" || rep.verdict === "warn") {
        const findings = (rep.details?.findings || []).filter((f) => f.level === "fail" || f.level === "warn");
        for (const f of findings.slice(0, 3)) {
          alerts.push(h("span", { class: `pill ${f.level === "fail" ? "pill-danger" : "pill-warn"}` }, `${rep.layer}: ${f.message}`));
        }
      }
    }
    if (alerts.length) {
      root.append(h("div", { class: "alert warn row", "data-testid": "wb-validation-alerts" }, h("strong", {}, "本轮校验提示："), ...alerts));
    }
  }

  // ---- 左栏：源块清单（支持搜索、按类过滤与多选联动）----
  let leftPickMode = false;
  const leftPicked = new Set();
  const blockSearch = h("input", {
    type: "search",
    placeholder: "过滤源块…",
    style: { width: "100%" },
    "data-testid": "wb-block-search",
  });
  const togglePickBtn = h("button", { type: "button", class: "btn sm", "data-testid": "wb-left-pick-toggle" }, "开启左栏多选");
  const batchFromLeftBtn = h("button", { type: "button", class: "btn primary sm hidden", "data-testid": "wb-create-from-picked" }, "按所选块新建批注 (0)");

  const blockListEl = h("div", { class: "wb-col-body", "data-testid": "wb-blocks-list" });
  let highlightedBlockId = "";

  function renderBlocksList() {
    clear(blockListEl);
    const q = blockSearch.value.trim().toLowerCase();
    const filtered = blocks.filter((b) => {
      if (!q) return true;
      return `${b.block_id} ${b.kind} ${(b.path || []).join(" ")} ${b.text || ""}`.toLowerCase().includes(q);
    });
    if (!filtered.length) {
      blockListEl.append(h("div", { class: "muted", style: { padding: "12px" } }, "无匹配源块"));
      return;
    }
    for (const b of filtered) {
      const isPicked = leftPicked.has(b.block_id);
      const isActive = highlightedBlockId === b.block_id;
      const item = h(
        "div",
        {
          class: `block-item ${isActive ? "active" : ""} ${isPicked ? "is-picked" : ""}`,
          "data-testid": `wb-block-${b.block_id}`,
          onClick: () => {
            if (leftPickMode) {
              if (leftPicked.has(b.block_id)) leftPicked.delete(b.block_id);
              else leftPicked.add(b.block_id);
              batchFromLeftBtn.textContent = `按所选块新建批注 (${leftPicked.size})`;
              batchFromLeftBtn.classList.toggle("hidden", leftPicked.size === 0);
              renderBlocksList();
              return;
            }
            highlightedBlockId = b.block_id;
            renderBlocksList();
            if (activePdfView) {
              const p = activePdfView.findPageOfText(b.text || "");
              if (p > 0) {
                activePdfView.scrollToPage(p);
                toast(`已在第 ${p} 页定位到「${b.block_id}」`, "ok");
              }
            }
          },
        },
        h(
          "div",
          { class: "row-between" },
          h("span", { class: "kind" }, `${b.block_id} · ${b.kind}`),
          leftPickMode
            ? h("span", { class: `pill ${isPicked ? "pill-ok" : "pill-muted"}`, style: { fontSize: "11px", padding: "0 6px" } }, isPicked ? "已勾选" : "点击勾选")
            : b.revised_tex
            ? h("span", { class: "pill pill-ok", style: { fontSize: "10.5px", padding: "0 5px" } }, "已修订")
            : null
        ),
        (b.path || []).length ? h("div", { class: "faint", style: { fontSize: "11px" } }, b.path.join(" / ")) : null,
        h("div", { class: "snippet" }, b.text || "(空块)")
      );
      blockListEl.append(item);
    }
  }

  togglePickBtn.addEventListener("click", () => {
    leftPickMode = !leftPickMode;
    togglePickBtn.classList.toggle("primary", leftPickMode);
    togglePickBtn.textContent = leftPickMode ? "退出左栏多选" : "开启左栏多选";
    batchFromLeftBtn.classList.toggle("hidden", !leftPickMode || leftPicked.size === 0);
    renderBlocksList();
  });

  blockSearch.addEventListener("input", renderBlocksList);
  renderBlocksList();

  const leftCol = h(
    "section",
    { class: "wb-col", "data-testid": "wb-left-pane" },
    h(
      "div",
      { class: "wb-col-head" },
      h("strong", {}, `源块 (${blocks.length})`),
      h("div", { class: "row" }, togglePickBtn, batchFromLeftBtn),
      blockSearch
    ),
    blockListEl
  );

  // ---- 中栏：PDF 预览 / 非 PDF 语料预览 ----
  const pdfHost = h("div", { class: "wb-col-body pdf-stage", "data-testid": "wb-pdf-stage" });
  const zoomText = h("span", { class: "pill pill-muted", "data-testid": "wb-zoom-text" }, "100%");
  const pageJumpInput = h("input", {
    type: "number",
    min: "1",
    value: "1",
    style: { width: "58px", padding: "3px 6px" },
    "aria-label": "页码",
  });
  let pdfView = null;
  let annotLayer = null;

  const centerCol = h(
    "section",
    { class: "wb-col", "data-testid": "wb-center-pane" },
    h(
      "div",
      { class: "wb-col-head" },
      h("strong", {}, isLatex ? "PDF 预览" : "内容预览"),
      activeGen?.pdf_artifact_id
        ? h(
            "div",
            { class: "row" },
            h("button", { type: "button", class: "btn sm", onClick: () => pdfView?.setZoom((pdfView.zoomFactor || 1) - 0.15) }, "−"),
            zoomText,
            h("button", { type: "button", class: "btn sm", onClick: () => pdfView?.setZoom((pdfView.zoomFactor || 1) + 0.15) }, "+"),
            h("button", { type: "button", class: "btn ghost sm", onClick: () => pdfView?.setZoom(1.0) }, "适应"),
            pageJumpInput,
            h("button", { type: "button", class: "btn sm", onClick: () => pdfView?.scrollToPage(Number(pageJumpInput.value || 1)) }, "跳转")
          )
        : null
    ),
    pdfHost
  );

  // ---- 批注弹窗 helper：创建 / 编辑 / 多选指定源块 / 就地确认并接受 ----
  async function openCreateAnnotationModal({ pageIndex = 0, bbox = [0.05, 0.05, 0.9, 0.1], preselected = null } = {}) {
    if (!activeGen) {
      toast("请先在「生成与队列」至少生成一轮结果后再新建批注", "warn");
      return;
    }
    const targetArtifactId = activeGen.pdf_artifact_id || activeGen.tex_artifact_id;
    const artifact = await reqArtifactMeta(targetArtifactId);
    const suggested = inferCandidateBlocks(pdfView, blocks, pageIndex);
    const initial = preselected && preselected.length ? preselected : suggested;

    const picker = createBlockPicker({
      blocks,
      initialSelected: initial,
      suggestedIds: suggested,
      testIdPrefix: "create-ann-picker",
    });
    const categoryEl = h("select", { "data-testid": "ann-category" }, CATEGORIES.map(([k, v]) => h("option", { value: k }, v)));
    const scopeEl = h(
      "select",
      { "data-testid": "ann-scope" },
      [
        h("option", { value: "block" }, "局部块（默认仅改所选块）"),
        h("option", { value: "document" }, "当前文档（保留全局记录）"),
        h("option", { value: "project" }, "扩散建议（后续可在策略记录审核）"),
      ]
    );
    const commentEl = h("textarea", {
      placeholder: "描述需要修改的具体问题，例如：公式编号重复、表格超出右边距、术语翻译需统一…",
      "data-testid": "ann-comment",
    });

    modal({
      title: `新建批注（第 ${pageIndex + 1} 页）`,
      body: h(
        "div",
        { class: "stack" },
        h("div", { class: "grid cols-2" }, h("label", { class: "stack" }, h("span", { class: "muted" }, "问题类别"), categoryEl), h("label", { class: "stack" }, h("span", { class: "muted" }, "作用域"), scopeEl)),
        h("label", { class: "stack" }, h("span", { class: "muted" }, "修改要求（会直接发给教师模型执行）*"), commentEl),
        h("div", { class: "stack" }, h("span", { class: "muted" }, "关联源码块（支持多选，留空则标为待确认）"), picker.el)
      ),
      okText: "保存批注",
      onOk: async () => {
        const comment = commentEl.value.trim();
        if (!comment) {
          toast("请填写修改要求", "warn");
          return false;
        }
        const selected = picker.getSelectedIds();
        // BUG-08 修复：只要人手确认选了 >=1 个块，confidence 一律为 confirmed
        const confidence = selected.length >= 1 ? "confirmed" : "needs_review";
        const created = await api.createAnnotation(state.currentProjectId, {
          artifact_id: artifact.id,
          artifact_revision: artifact.revision,
          document_id: lastDocId,
          page_index: pageIndex,
          bbox,
          comment,
          category: categoryEl.value,
          scope: scopeEl.value,
          source_block_candidates: selected,
          mapping_confidence: confidence,
        });
        selectedAnnotationId = created.id || "";
        toast(selected.length ? `批注已保存并确认关联 ${selected.length} 个源块` : "批注已保存（待确认源块）", "ok");
        await reloadAnnotations();
      },
    });
  }

  batchFromLeftBtn.addEventListener("click", () => {
    openCreateAnnotationModal({ pageIndex: 0, bbox: [0.05, 0.05, 0.9, 0.1], preselected: [...leftPicked] });
  });

  function openAssignBlocksModal(annotation) {
    const suggested = inferCandidateBlocks(pdfView, blocks, annotation.page_index || 0);
    const picker = createBlockPicker({
      blocks,
      initialSelected: annotation.source_block_candidates || [],
      suggestedIds: suggested,
      testIdPrefix: "assign-blocks-picker",
    });

    modal({
      title: "指定源块（支持搜索与多选）",
      body: h(
        "div",
        { class: "stack" },
        h("div", { class: "subcard muted", style: { fontSize: "12.5px" } }, `批注意见：${annotation.comment || "(无文字)"}`),
        picker.el
      ),
      okText: "确认并保存选中的源块",
      onOk: async () => {
        const chosen = picker.getSelectedIds();
        if (!chosen.length) {
          toast("请至少勾选 1 个源码块", "warn");
          return false;
        }
        await api.updateAnnotation(annotation.id, {
          source_block_candidates: chosen,
          mapping_confidence: "confirmed",
        });
        toast(`已确认绑定 ${chosen.length} 个源块（${chosen.join(", ")}）`, "ok");
        await reloadAnnotations();
      },
    });
  }

  function openEditAnnotationModal(annotation) {
    const commentEl = h("textarea", { "data-testid": "edit-ann-comment" }, annotation.comment || "");
    commentEl.value = annotation.comment || "";
    const catEl = h(
      "select",
      { "data-testid": "edit-ann-category" },
      CATEGORIES.map(([k, v]) => h("option", { value: k, selected: annotation.category === k }, v))
    );
    const scopeEl = h(
      "select",
      { "data-testid": "edit-ann-scope" },
      [
        ["block", "局部块"],
        ["document", "当前文档"],
        ["project", "项目策略候选"],
      ].map(([k, v]) => h("option", { value: k, selected: annotation.scope === k }, v))
    );

    modal({
      title: "编辑批注内容",
      body: h(
        "div",
        { class: "stack" },
        h("div", { class: "grid cols-2" }, h("label", { class: "stack" }, h("span", { class: "muted" }, "类别"), catEl), h("label", { class: "stack" }, h("span", { class: "muted" }, "作用域"), scopeEl)),
        h("label", { class: "stack" }, h("span", { class: "muted" }, "修改意见 *"), commentEl)
      ),
      okText: "保存修改",
      onOk: async () => {
        if (!commentEl.value.trim()) {
          toast("修改意见不能为空", "warn");
          return false;
        }
        await api.updateAnnotation(annotation.id, {
          comment: commentEl.value.trim(),
          category: catEl.value,
          scope: scopeEl.value,
        });
        toast("批注已更新", "ok");
        await reloadAnnotations();
      },
    });
  }

  async function openConfirmAndAcceptModal(annotation) {
    const routing = await loadTeacherRoutingOptions();
    const suggested = inferCandidateBlocks(pdfView, blocks, annotation.page_index || 0);
    const picker = createBlockPicker({
      blocks,
      initialSelected: annotation.source_block_candidates || suggested,
      suggestedIds: suggested,
      testIdPrefix: "accept-confirm-picker",
    });

    const provSelect = h(
      "select",
      { "data-testid": "revise-provider-select" },
      routing.providers.map((p) => h("option", { value: p.id, selected: p.id === routing.defaultProvider }, p.label))
    );
    const modelInput = h("input", {
      placeholder: "留空则使用所选提供方的默认模型",
      "data-testid": "revise-model-input",
    });
    const forceBox = h("input", { type: "checkbox", checked: annotation.status === "accepted" });

    modal({
      title: "确认源块并提交教师修订",
      body: h(
        "div",
        { class: "stack" },
        annotation.mapping_confidence !== "confirmed"
          ? h("div", { class: "alert warn" }, "该批注尚未经过人工确认定位。请在下方多选列表中勾选/核对要修改的源码块（支持合并多个块协同修订）。")
          : null,
        h("div", { class: "subcard" }, h("strong", {}, "修订意见："), annotation.comment),
        picker.el,
        h(
          "div",
          { class: "grid cols-2" },
          h("label", { class: "stack" }, h("span", { class: "muted" }, "执行教师提供方"), provSelect),
          h("label", { class: "stack" }, h("span", { class: "muted" }, "模型覆盖 (可选)"), modelInput)
        ),
        h("label", { class: "row" }, forceBox, h("span", { class: "muted", style: { fontSize: "12.5px" } }, "即使之前已执行过修订也强制新建一轮（绕过幂等复用）"))
      ),
      okText: "确认并开始修订",
      onOk: async () => {
        const chosen = picker.getSelectedIds();
        if (!chosen.length) {
          toast("请至少选择 1 个要修订的源码块", "warn");
          return false;
        }
        const res = await api.acceptAnnotation(annotation.id, {
          block_ids: chosen,
          provider: provSelect.value,
          model: modelInput.value.trim(),
          confirm: true,
          force: forceBox.checked,
        });
        toast(
          res.reused
            ? `复用已有修订任务 (${res.job_id})`
            : `已提交 ${chosen.length} 块协同修订任务 (${res.provider})，正在后台执行…`,
          "ok"
        );
        // 监听该任务完成，完成后自动刷新到最新生成的轮次
        watchRevisionJob(res.job_id);
        await reloadAnnotations();
      },
    });
  }

  function watchRevisionJob(jobId) {
    let attempts = 0;
    const poll = async () => {
      attempts += 1;
      if (attempts > 90) return;
      try {
        const j = await api.jobDetail(jobId);
        if (j.status === "succeeded") {
          toast("修订已完成并生成新版本！正在切换到新轮次…", "ok");
          lastGenId = j.result?.generation_id || "";
          window.dispatchEvent(new CustomEvent("gf:refresh"));
          return;
        }
        if (j.status === "failed" || j.status === "cancelled") {
          toast(`修订未完成 (${j.status})：${j.error || ""}`, "error");
          window.dispatchEvent(new CustomEvent("gf:refresh"));
          return;
        }
      } catch {
        return;
      }
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 1800);
  }

  // ---- 右栏：批注列表与操作 ----
  const annBody = h("div", { class: "wb-col-body", "data-testid": "wb-annotations-list" });
  const filterBar = h("div", { class: "chip-tabs", "data-testid": "wb-ann-filters" });
  const addManualAnnBtn = h(
    "button",
    {
      type: "button",
      class: "btn primary sm",
      "data-testid": "wb-btn-new-annotation",
      onClick: () => openCreateAnnotationModal({ pageIndex: 0 }),
    },
    "＋ 手动新建批注"
  );

  let currentAnnotations = allAnnotations;

  async function reloadAnnotations() {
    currentAnnotations = await api.annotations(state.currentProjectId, lastDocId);
    renderFilterTabs();
    renderAnnotations();
    if (annotLayer) annotLayer.setAnnotations(currentAnnotations);
  }

  function renderFilterTabs() {
    clear(filterBar);
    const counts = {
      all: currentAnnotations.length,
      open: currentAnnotations.filter((a) => a.status !== "accepted").length,
      needs_review: currentAnnotations.filter((a) => a.mapping_confidence === "needs_review" || !(a.source_block_candidates || []).length).length,
      accepted: currentAnnotations.filter((a) => a.status === "accepted").length,
    };
    for (const [key, label] of [
      ["all", `全部 (${counts.all})`],
      ["open", `待处理 (${counts.open})`],
      ["needs_review", `待定位 (${counts.needs_review})`],
      ["accepted", `已采纳 (${counts.accepted})`],
    ]) {
      filterBar.append(
        h(
          "button",
          {
            type: "button",
            class: `chip-tab ${statusFilter === key ? "active" : ""}`,
            "data-testid": `wb-filter-${key}`,
            onClick: () => {
              statusFilter = key;
              renderFilterTabs();
              renderAnnotations();
            },
          },
          label
        )
      );
    }
  }

  function renderAnnotations() {
    clear(annBody);
    const list = currentAnnotations.filter((a) => {
      if (statusFilter === "open") return a.status !== "accepted";
      if (statusFilter === "needs_review") return a.mapping_confidence === "needs_review" || !(a.source_block_candidates || []).length;
      if (statusFilter === "accepted") return a.status === "accepted";
      return true;
    });
    if (!list.length) {
      annBody.append(
        h("div", { class: "empty" }, currentAnnotations.length ? "当前筛选条件下无批注" : "尚无批注：在右侧 PDF 上拖拽框选，或点击上方「手动新建批注」。")
      );
      return;
    }

    for (const ann of list) {
      const blocksSelected = ann.source_block_candidates || [];
      const needsConfirm = ann.mapping_confidence === "needs_review" || !blocksSelected.length;
      const [confLabel, confTone] = CONFIDENCE_LABELS[ann.mapping_confidence] || [ann.mapping_confidence, "muted"];

      const card = h(
        "div",
        {
          class: `annot-card ${selectedAnnotationId === ann.id ? "is-selected" : ""}`,
          "data-testid": `wb-ann-card-${ann.id}`,
          onClick: () => {
            selectedAnnotationId = ann.id;
            if (annotLayer) annotLayer.setSelected(ann.id);
            if (pdfView) pdfView.scrollToPage((ann.page_index || 0) + 1);
            renderAnnotations();
          },
        },
        h(
          "div",
          { class: "row-between" },
          h(
            "div",
            { class: "row", style: { gap: "5px" } },
            pill(`P${(ann.page_index || 0) + 1} · ${ann.category}`, "accent"),
            pill(ann.status === "accepted" ? "已采纳修订" : "待修订", ann.status === "accepted" ? "ok" : ""),
            pill(confLabel, confTone)
          ),
          h("span", { class: "faint", style: { fontSize: "11px" } }, fmtTime(ann.updated_at || ann.created_at))
        ),
        h("div", { class: "comment" }, ann.comment || "(未填具体意见)"),
        h(
          "div",
          { class: "annot-block-chips" },
          blocksSelected.length
            ? blocksSelected.map((bid) => h("span", { class: "bp-chip", style: { fontSize: "11.5px" } }, bid))
            : h("span", { class: "pill pill-warn" }, "尚未绑定源块 —— 请点击「指定源块 (多选)」")
        ),
        h(
          "div",
          { class: "row", style: { marginTop: "8px", gap: "6px" } },
          h(
            "button",
            {
              type: "button",
              class: needsConfirm ? "btn primary sm" : "btn sm",
              "data-testid": `wb-ann-assign-${ann.id}`,
              onClick: (e) => {
                e.stopPropagation();
                openAssignBlocksModal(ann);
              },
            },
            blocksSelected.length ? `指定源块 (${blocksSelected.length})` : "指定源块 (多选)"
          ),
          h(
            "button",
            {
              type: "button",
              class: needsConfirm ? "btn sm" : "btn primary sm",
              "data-testid": `wb-ann-accept-${ann.id}`,
              onClick: (e) => {
                e.stopPropagation();
                openConfirmAndAcceptModal(ann).catch(showError);
              },
            },
            ann.status === "accepted" ? "再次修订" : "接受并修订"
          ),
          h(
            "button",
            {
              type: "button",
              class: "btn ghost sm",
              "data-testid": `wb-ann-edit-${ann.id}`,
              onClick: (e) => {
                e.stopPropagation();
                openEditAnnotationModal(ann);
              },
            },
            "编辑"
          ),
          h(
            "button",
            {
              type: "button",
              class: "btn danger sm",
              "data-testid": `wb-ann-delete-${ann.id}`,
              onClick: async (e) => {
                e.stopPropagation();
                if (!(await confirmDialog("确定删除这条批注吗？"))) return;
                await api.deleteAnnotation(ann.id);
                if (selectedAnnotationId === ann.id) selectedAnnotationId = "";
                toast("已删除批注", "ok");
                await reloadAnnotations();
              },
            },
            "删除"
          )
        )
      );
      annBody.append(card);
    }
  }

  renderFilterTabs();
  renderAnnotations();

  const rightCol = h(
    "section",
    { class: "wb-col", "data-testid": "wb-right-pane" },
    h("div", { class: "wb-col-head" }, h("strong", {}, "批注列表与修订"), addManualAnnBtn, filterBar),
    annBody
  );

  root.append(h("div", { class: "workbench" }, leftCol, centerCol, rightCol));

  // ---- 加载 PDF 预览或文本预览 ----
  if (activeGen?.pdf_artifact_id) {
    pdfView = new PdfView(pdfHost);
    activePdfView = pdfView;
    pdfView.onZoomChange = (factor) => {
      zoomText.textContent = `${Math.round(factor * 100)}%`;
    };
    annotLayer = new AnnotationLayer(pdfView, {
      onSelect: ({ annotation, pageIndex, bbox }) => {
        if (annotation) {
          selectedAnnotationId = annotation.id;
          renderAnnotations();
          document.querySelector(`[data-testid="wb-ann-card-${annotation.id}"]`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
          return;
        }
        openCreateAnnotationModal({ pageIndex, bbox }).catch(showError);
      },
    });
    pdfView.onPageRendered = () => annotLayer.render();
    try {
      await pdfView.load(artifactUrl(activeGen.pdf_artifact_id));
      pageJumpInput.max = String(pdfView.pageCount || 1);
      annotLayer.setAnnotations(currentAnnotations);
    } catch (error) {
      pdfHost.append(h("div", { class: "alert danger" }, `PDF 渲染失败：${error.message}`));
    }
  } else if (activeGen?.tex_artifact_id) {
    pdfHost.classList.add("is-text-fallback");
    try {
      const resp = await fetch(artifactUrl(activeGen.tex_artifact_id));
      const rawText = await resp.text();
      pdfHost.append(
        h(
          "div",
          { class: "card", style: { width: "100%" } },
          h("div", { class: "row-between", style: { marginBottom: "8px" } }, h("strong", {}, "产物源码预览（本轮无 PDF 输出）")),
          h("pre", {}, rawText.slice(0, 12000) + (rawText.length > 12000 ? "\n…（已截断，请点击右上角链接查看完整文件）" : ""))
        )
      );
    } catch {
      pdfHost.append(empty("无法读取本轮文本产物"));
    }
  } else {
    pdfHost.append(empty("本轮没有可预览的 PDF 或 TeX 产物，请查看生成队列日志定位原因。"));
  }

  return root;
}

async function reqArtifactMeta(artifactId) {
  return req(`/api/artifacts/${encodeURIComponent(artifactId)}`);
}
