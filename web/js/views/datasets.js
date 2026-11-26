// 样本与快照：多选批量审阅样本 + 快照比例/种子自定义与冻结前试算预览（BUG-09）
import { api } from "../api.js";
import { clear, empty, fmtTime, h, modal, pill, pre, showError, table, toast } from "../ui.js";

let statusFilter = "all";

export async function render(state) {
  const root = h("div", { class: "stack-lg", "data-testid": "view-datasets" });
  if (!state.currentProjectId) {
    root.append(empty("请先在顶部新建或选择一个项目。"));
    return root;
  }

  const [examples, snapshots] = await Promise.all([
    api.examples(state.currentProjectId),
    api.snapshots(state.currentProjectId),
  ]);

  const selectedIds = new Set();
  const batchBar = h("div", { class: "row hidden", "data-testid": "ds-batch-bar" });
  const listHost = h("div", { class: "stack", "data-testid": "ds-examples-host" });
  const filterWrap = h("div", { class: "chip-tabs" });

  function filteredExamples() {
    if (statusFilter === "all") return examples;
    return examples.filter((e) => e.status === statusFilter);
  }

  function updateBatchBar() {
    clear(batchBar);
    if (!selectedIds.size) {
      batchBar.classList.add("hidden");
      return;
    }
    batchBar.classList.remove("hidden");
    const runBatch = async (nextStatus) => {
      try {
        const res = await api.batchExampleStatus(state.currentProjectId, [...selectedIds], nextStatus);
        toast(`已批量更新 ${res.updated_count} 条样本为 ${nextStatus}`, "ok");
        window.dispatchEvent(new CustomEvent("gf:refresh"));
      } catch (err) {
        showError(err);
      }
    };
    batchBar.append(
      pill(`已选 ${selectedIds.size} 条`, "accent"),
      h("button", { type: "button", class: "btn primary sm", "data-testid": "ds-batch-accept", onClick: () => runBatch("accepted") }, "批量接纳 (accepted)"),
      h("button", { type: "button", class: "btn danger sm", "data-testid": "ds-batch-reject", onClick: () => runBatch("rejected") }, "批量剔除 (rejected)"),
      h("button", { type: "button", class: "btn sm", onClick: () => runBatch("needs_review") }, "标为待复核"),
      h(
        "button",
        {
          type: "button",
          class: "btn ghost sm",
          onClick: () => {
            selectedIds.clear();
            renderTable();
          },
        },
        "取消多选"
      )
    );
  }

  function renderFilterTabs() {
    clear(filterWrap);
    const counts = {
      all: examples.length,
      accepted: examples.filter((e) => e.status === "accepted").length,
      candidate: examples.filter((e) => e.status === "candidate").length,
      needs_review: examples.filter((e) => e.status === "needs_review").length,
      rejected: examples.filter((e) => e.status === "rejected").length,
    };
    for (const [key, label] of [
      ["all", `全部 (${counts.all})`],
      ["accepted", `已接纳 (${counts.accepted})`],
      ["candidate", `待审候选 (${counts.candidate})`],
      ["needs_review", `待复核 (${counts.needs_review})`],
      ["rejected", `已剔除 (${counts.rejected})`],
    ]) {
      filterWrap.append(
        h(
          "button",
          {
            type: "button",
            class: `chip-tab ${statusFilter === key ? "active" : ""}`,
            onClick: () => {
              statusFilter = key;
              selectedIds.clear();
              renderFilterTabs();
              renderTable();
            },
          },
          label
        )
      );
    }
  }

  function renderTable() {
    clear(listHost);
    updateBatchBar();
    const list = filteredExamples();
    if (!list.length) {
      listHost.append(empty("当前筛选条件下没有样本。请先运行生成并校验，或放宽筛选条件。"));
      return;
    }
    const allBox = h("input", {
      type: "checkbox",
      checked: list.length > 0 && list.every((e) => selectedIds.has(e.id)),
      "aria-label": "全选可见样本",
      onChange: () => {
        if (allBox.checked) list.forEach((e) => selectedIds.add(e.id));
        else list.forEach((e) => selectedIds.delete(e.id));
        renderTable();
      },
    });
    listHost.append(
      table(
        [allBox, "样本 ID", "任务 / 分组", "标签来源", "输入/目标预览", "状态", "操作"],
        list.map((ex) => {
          const box = h("input", {
            type: "checkbox",
            checked: selectedIds.has(ex.id),
            "aria-label": `选择样本 ${ex.id}`,
            onChange: () => {
              if (box.checked) selectedIds.add(ex.id);
              else selectedIds.delete(ex.id);
              updateBatchBar();
            },
          });
          const inpText = String(ex.input_payload?.markdown || ex.input_payload?.prompt || "");
          const tgtText = String(ex.target_payload?.tex || ex.target_payload?.text || "");
          const statusTone = ex.status === "accepted" ? "ok" : ex.status === "rejected" ? "danger" : ex.status === "needs_review" ? "warn" : "muted";
          return [
            box,
            h("span", { class: "mono" }, ex.id),
            h("div", {}, h("div", {}, ex.task_type), h("div", { class: "faint mono", style: { fontSize: "11px" } }, ex.source_group_id)),
            pill(ex.label_origin || "unknown", "muted"),
            h(
              "div",
              { style: { maxWidth: "300px" } },
              h("div", { class: "muted", style: { fontSize: "12px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } }, `IN: ${inpText.replace(/\s+/g, " ").slice(0, 80)}`),
              h("div", { class: "faint", style: { fontSize: "12px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } }, `OUT: ${tgtText.replace(/\s+/g, " ").slice(0, 80)}`)
            ),
            pill(ex.status, statusTone),
            h(
              "div",
              { class: "row", style: { gap: "4px" } },
              h(
                "button",
                {
                  type: "button",
                  class: "btn ghost sm",
                  onClick: () => modal({ title: `样本明细：${ex.id}`, body: h("div", { class: "stack" }, h("strong", {}, "输入 (input_payload)"), pre(JSON.stringify(ex.input_payload, null, 2)), h("strong", {}, "目标 (target_payload)"), pre(JSON.stringify(ex.target_payload, null, 2)), h("strong", {}, "教师审计信息"), pre(JSON.stringify(ex.teacher, null, 2))), okText: "关闭" }),
                },
                "详情"
              ),
              ex.status !== "accepted"
                ? h(
                    "button",
                    {
                      type: "button",
                      class: "btn ok sm",
                      onClick: async () => {
                        await api.updateExample(ex.id, "accepted");
                        toast("已接纳样本", "ok");
                        window.dispatchEvent(new CustomEvent("gf:refresh"));
                      },
                    },
                    "接纳"
                  )
                : null,
              ex.status !== "rejected"
                ? h(
                    "button",
                    {
                      type: "button",
                      class: "btn danger sm",
                      onClick: async () => {
                        await api.updateExample(ex.id, "rejected");
                        toast("已剔除样本", "ok");
                        window.dispatchEvent(new CustomEvent("gf:refresh"));
                      },
                    },
                    "剔除"
                  )
                : null
            ),
          ];
        })
      )
    );
  }

  function openFreezeModal() {
    const versionIn = h("input", { value: `snap-${new Date().toISOString().slice(0, 10).replace(/-/g, "")}-${snapshots.length + 1}`, "data-testid": "snap-version-input" });
    const seedIn = h("input", { value: "godfather-1", "data-testid": "snap-seed-input" });
    const trainIn = h("input", { type: "number", step: "0.05", min: "0", max: "1", value: "0.8", "data-testid": "snap-ratio-train" });
    const devIn = h("input", { type: "number", step: "0.05", min: "0", max: "1", value: "0.1", "data-testid": "snap-ratio-dev" });
    const holdoutIn = h("input", { type: "number", step: "0.05", min: "0", max: "1", value: "0.1", "data-testid": "snap-ratio-holdout" });
    const includeCand = h("input", { type: "checkbox", checked: false, "data-testid": "snap-include-candidates" });
    const previewBox = h("div", { class: "subcard stack", "data-testid": "snap-preview-box" }, h("span", { class: "muted" }, "正在试算切分结果…"));

    async function refreshPreview() {
      try {
        const res = await api.previewSnapshot(state.currentProjectId, {
          seed: seedIn.value.trim() || "godfather-1",
          include_candidates: includeCand.checked,
          ratios: {
            train: Number(trainIn.value || 0),
            dev: Number(devIn.value || 0),
            holdout: Number(holdoutIn.value || 0),
          },
        });
        clear(previewBox);
        previewBox.append(
          h(
            "div",
            { class: "row-between" },
            h("strong", {}, `预估切分分布（共 ${res.total_examples} 条 / ${res.group_count} 源分组 / ${res.cluster_count} 去重簇）`),
            h("div", { class: "row" }, pill(`train ${res.counts?.train ?? 0}`, "accent"), pill(`dev ${res.counts?.dev ?? 0}`, res.counts?.dev ? "ok" : "warn"), pill(`holdout ${res.counts?.holdout ?? 0}`, res.counts?.holdout ? "ok" : "warn"))
          ),
          ...(res.warnings || []).map((w) => h("div", { class: "alert warn", style: { padding: "7px 10px" } }, w))
        );
      } catch (err) {
        clear(previewBox);
        previewBox.append(h("div", { class: "alert danger" }, err.message));
      }
    }

    for (const ctrl of [seedIn, trainIn, devIn, holdoutIn, includeCand]) {
      ctrl.addEventListener("change", refreshPreview);
    }

    const presets = h(
      "div",
      { class: "row", style: { gap: "6px" } },
      h("span", { class: "muted", style: { fontSize: "12px" } }, "比例预设："),
      [
        ["80/10/10 默认", [0.8, 0.1, 0.1]],
        ["70/20/10 多评测", [0.7, 0.2, 0.1]],
        ["60/20/20 小样本", [0.6, 0.2, 0.2]],
        ["100/0/0 仅训练", [1.0, 0.0, 0.0]],
      ].map(([label, [tr, dv, ho]]) =>
        h(
          "button",
          {
            type: "button",
            class: "btn sm",
            onClick: () => {
              trainIn.value = String(tr);
              devIn.value = String(dv);
              holdoutIn.value = String(ho);
              refreshPreview();
            },
          },
          label
        )
      )
    );

    modal({
      title: "冻结可复现数据集快照 (BUG-09)",
      body: h(
        "div",
        { class: "stack" },
        h("div", { class: "grid cols-2" }, h("label", { class: "stack" }, h("span", { class: "muted" }, "快照版本号"), versionIn), h("label", { class: "stack" }, h("span", { class: "muted" }, "随机划分种子 (seed)"), seedIn)),
        presets,
        h(
          "div",
          { class: "grid cols-3" },
          h("label", { class: "stack" }, h("span", { class: "muted" }, "train 比例"), trainIn),
          h("label", { class: "stack" }, h("span", { class: "muted" }, "dev 比例"), devIn),
          h("label", { class: "stack" }, h("span", { class: "muted" }, "holdout 比例"), holdoutIn)
        ),
        h("label", { class: "row" }, includeCand, h("span", {}, "同时包含尚未人工接纳的 candidate 状态样本")),
        previewBox
      ),
      okText: "确认冻结快照",
      onOk: async () => {
        const res = await api.createSnapshot(state.currentProjectId, {
          version: versionIn.value.trim() || undefined,
          seed: seedIn.value.trim() || "godfather-1",
          include_candidates: includeCand.checked,
          ratios: {
            train: Number(trainIn.value || 0),
            dev: Number(devIn.value || 0),
            holdout: Number(holdoutIn.value || 0),
          },
        });
        toast(`快照 ${res.version} 已冻结（SHA-256: ${(res.sha256 || "").slice(0, 18)}…）`, "ok");
        window.dispatchEvent(new CustomEvent("gf:refresh"));
      },
    });
    refreshPreview();
  }

  root.append(
    h(
      "div",
      { class: "page-head" },
      h("div", {}, h("h1", {}, "样本与快照"), h("p", {}, "按来源去重防泄漏切分样本，生成带 SHA-256 校验的只读快照。")),
      h(
        "div",
        { class: "row" },
        h(
          "button",
          {
            type: "button",
            class: "btn sm",
            onClick: async () => {
              const pack = await api.exportTaskpack(state.currentProjectId, 50);
              modal({ title: "导出手工导入包（不含 holdout）", body: pre(JSON.stringify(pack, null, 2)), okText: "关闭" });
            },
          },
          "导出手工任务包"
        ),
        h("button", { type: "button", class: "btn primary sm", "data-testid": "ds-open-freeze-btn", onClick: openFreezeModal }, "冻结新数据快照")
      )
    ),
    h(
      "section",
      { class: "card stack" },
      h("div", { class: "row-between" }, filterWrap, batchBar),
      listHost
    ),
    h(
      "section",
      { class: "card stack", "data-testid": "ds-snapshots-card" },
      h("h2", {}, `已冻结快照 (${snapshots.length})`),
      snapshots.length
        ? table(
            ["版本号", "train / dev / holdout", "SHA-256 摘要", "落盘路径", "冻结时间"],
            snapshots.map((s) => [
              h("strong", { class: "mono" }, s.version),
              h("div", { class: "row" }, pill(`train ${s.counts?.train ?? 0}`, "accent"), pill(`dev ${s.counts?.dev ?? 0}`, "ok"), pill(`holdout ${s.counts?.holdout ?? 0}`, "muted")),
              h("span", { class: "mono faint", style: { fontSize: "11.5px" } }, (s.sha256 || "").slice(0, 22) + "…"),
              h("span", { class: "mono-wrap", style: { fontSize: "11.5px" } }, s.jsonl_path),
              fmtTime(s.created_at),
            ])
          )
        : empty("尚未冻结任何快照：请至少接纳一部分样本后点击右上角「冻结新数据快照」。")
    )
  );

  renderFilterTabs();
  renderTable();
  return root;
}
