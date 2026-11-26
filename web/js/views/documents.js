// 文档结构：查看分块明细、复制 block_id 并支持直达生成队列与工作台
import { api } from "../api.js";
import { clear, empty, fmtTime, h, pill, pre, toast } from "../ui.js";

let selectedDocId = "";

export async function render(state) {
  const root = h("div", { class: "stack-lg", "data-testid": "view-documents" });
  if (!state.currentProjectId) {
    root.append(empty("请先在顶部新建或选择一个项目。"));
    return root;
  }

  const docs = await api.documents(state.currentProjectId);
  if (!docs.length) {
    root.append(
      h("div", { class: "page-head" }, h("h1", {}, "文档结构浏览器")),
      empty("暂无已解析文档，请先去「材料与参考」导入。")
    );
    return root;
  }

  if (!selectedDocId || !docs.some((d) => d.id === selectedDocId)) selectedDocId = docs[0].id;
  const detail = await api.documentDetail(selectedDocId);

  const filterInput = h("input", { type: "search", placeholder: "搜索块内容或 ID…", style: { minWidth: "220px" } });
  const detailPane = h("div", { class: "stack" });

  function renderBlocks() {
    clear(detailPane);
    const q = filterInput.value.trim().toLowerCase();
    const blocks = (detail.blocks || []).filter((b) => {
      if (!q) return true;
      return `${b.block_id} ${b.kind} ${(b.path || []).join(" ")} ${b.text || ""}`.toLowerCase().includes(q);
    });
    if (!blocks.length) {
      detailPane.append(empty("没有匹配的结构块"));
      return;
    }
    for (const b of blocks) {
      detailPane.append(
        h(
          "div",
          { class: "subcard stack", style: { gap: "6px" } },
          h(
            "div",
            { class: "row-between" },
            h(
              "div",
              { class: "row" },
              h("strong", { class: "mono" }, b.block_id),
              pill(b.kind, "accent"),
              (b.path || []).length ? h("span", { class: "muted", style: { fontSize: "12px" } }, b.path.join(" / ")) : null,
              b.revised_tex ? pill("含修订版本", "ok") : null
            ),
            h(
              "button",
              {
                type: "button",
                class: "btn ghost sm",
                onClick: () => {
                  navigator.clipboard?.writeText(b.block_id).catch(() => {});
                  toast(`已复制源块 ID：${b.block_id}`, "ok");
                },
              },
              "复制 ID"
            )
          ),
          pre(b.text || "(空块)"),
          b.revised_tex ? h("details", {}, h("summary", { class: "muted", style: { cursor: "pointer" } }, "查看已写回的修订 TeX"), pre(b.revised_tex)) : null
        )
      );
    }
  }

  filterInput.addEventListener("input", renderBlocks);
  renderBlocks();

  root.append(
    h(
      "div",
      { class: "page-head" },
      h("div", {}, h("h1", {}, "文档结构与版本检查"), h("p", {}, `当前文档版本 revision=${detail.revision} · 共 ${(detail.blocks || []).length} 个稳定结构块。`)),
      h(
        "div",
        { class: "row" },
        h("a", { class: "btn sm", href: "#/queue" }, "发起生成"),
        h("a", { class: "btn primary sm", href: "#/workbench" }, "在工作台中打开")
      )
    ),
    h(
      "div",
      { class: "grid cols-3" },
      h(
        "section",
        { class: "card stack" },
        h("h2", {}, `文档 (${docs.length})`),
        docs.map((d) =>
          h(
            "div",
            {
              class: `block-item ${d.id === selectedDocId ? "active" : ""}`,
              onClick: () => {
                selectedDocId = d.id;
                window.dispatchEvent(new CustomEvent("gf:refresh"));
              },
            },
            h("div", { class: "row-between" }, h("strong", {}, d.title || d.id), pill(d.status, "muted")),
            h("div", { class: "faint", style: { fontSize: "12px" } }, `修订 r${d.revision} · ${d.block_count} 块 · ${fmtTime(d.updated_at)}`)
          )
        )
      ),
      h(
        "section",
        { class: "card stack", style: { gridColumn: "span 2" } },
        h("div", { class: "row-between" }, h("h2", {}, `结构块列表`), filterInput),
        detailPane
      )
    )
  );

  return root;
}
