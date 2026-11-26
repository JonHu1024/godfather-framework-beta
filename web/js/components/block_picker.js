// 可复用的「源块多选定位器」组件：支持搜索、按类型筛选、勾选/整行点击、Shift 范围连选、已选标签屑
import { clear, h } from "../ui.js";

const KIND_LABELS = {
  all: "全部",
  heading: "标题",
  paragraph: "正文",
  math: "公式",
  table: "表格",
  list: "列表",
  code: "代码",
  sft_item: "语料条",
};

export function createBlockPicker({
  blocks = [],
  initialSelected = [],
  suggestedIds = [],
  testIdPrefix = "block-picker",
  onChange = () => {},
} = {}) {
  const selected = new Set((initialSelected || []).filter(Boolean));
  const suggested = new Set((suggestedIds || []).filter(Boolean));
  let searchQuery = "";
  let kindFilter = "all";
  let onlyMatched = false;
  let lastAnchorIndex = -1;

  const root = h("div", { class: "block-picker", "data-testid": testIdPrefix });
  const selectedBar = h("div", { class: "bp-selected-bar", "data-testid": `${testIdPrefix}-selected` });
  const searchInput = h("input", {
    type: "search",
    placeholder: "搜索 block_id、标题路径或正文关键词…",
    "aria-label": "搜索源块",
    "data-testid": `${testIdPrefix}-search`,
  });
  const kindsWrap = h("div", { class: "bp-kinds" });
  const listWrap = h("div", { class: "bp-list", role: "listbox", "aria-multiselectable": "true", "data-testid": `${testIdPrefix}-list` });
  const matchedToggle = h(
    "button",
    {
      type: "button",
      class: "btn sm",
      title: "仅展示按当前页文本自动命中的候选源块",
      "data-testid": `${testIdPrefix}-filter-matched`,
    },
    `本页匹配 (${suggested.size})`
  );

  function visibleBlocks() {
    const q = searchQuery.trim().toLowerCase();
    return blocks.filter((b) => {
      if (kindFilter !== "all" && b.kind !== kindFilter) return false;
      if (onlyMatched && !suggested.has(b.block_id)) return false;
      if (!q) return true;
      const hay = `${b.block_id} ${b.kind} ${(b.path || []).join(" ")} ${b.text || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }

  function getSelectedIds() {
    // 尽量保持源文档中的先后顺序，用户未出现在 blocks 里的额外 ID 也保留在末尾
    const ordered = [];
    for (const b of blocks) {
      if (selected.has(b.block_id)) ordered.push(b.block_id);
    }
    for (const id of selected) {
      if (!ordered.includes(id)) ordered.push(id);
    }
    return ordered;
  }

  function emit() {
    onChange(getSelectedIds());
  }

  function renderSelectedBar() {
    clear(selectedBar);
    const ids = getSelectedIds();
    if (!ids.length) {
      selectedBar.append(
        h("span", { class: "muted", style: { fontSize: "12.5px" } }, "尚未选择源块（支持单选、多选或 Shift 连选）")
      );
      return;
    }
    selectedBar.append(h("span", { class: "muted", style: { fontSize: "12px", fontWeight: "600" } }, `已选 ${ids.length} 块：`));
    for (const id of ids) {
      selectedBar.append(
        h(
          "span",
          { class: "bp-chip", "data-testid": `${testIdPrefix}-chip-${id}` },
          id,
          h(
            "button",
            {
              type: "button",
              title: `移除 ${id}`,
              "aria-label": `移除 ${id}`,
              onClick: () => {
                selected.delete(id);
                renderSelectedBar();
                renderList();
                emit();
              },
            },
            "×"
          )
        )
      );
    }
  }

  function renderKinds() {
    clear(kindsWrap);
    const present = new Set(blocks.map((b) => b.kind));
    const available = ["all", ...Object.keys(KIND_LABELS).filter((k) => k !== "all" && present.has(k))];
    for (const k of available) {
      kindsWrap.append(
        h(
          "button",
          {
            type: "button",
            class: `chip-tab ${kindFilter === k ? "active" : ""}`,
            onClick: () => {
              kindFilter = k;
              renderKinds();
              renderList();
            },
          },
          KIND_LABELS[k] || k
        )
      );
    }
  }

  function toggleBlock(visible, index, event) {
    const id = visible[index].block_id;
    if (event?.shiftKey && lastAnchorIndex >= 0 && lastAnchorIndex < visible.length) {
      const start = Math.min(lastAnchorIndex, index);
      const end = Math.max(lastAnchorIndex, index);
      const targetState = !selected.has(id);
      for (let i = start; i <= end; i += 1) {
        if (targetState) selected.add(visible[i].block_id);
        else selected.delete(visible[i].block_id);
      }
    } else {
      if (selected.has(id)) selected.delete(id);
      else selected.add(id);
    }
    lastAnchorIndex = index;
    renderSelectedBar();
    renderList();
    emit();
  }

  function renderList() {
    clear(listWrap);
    const visible = visibleBlocks();
    if (!visible.length) {
      listWrap.append(h("div", { class: "muted", style: { padding: "14px", textAlign: "center" } }, "没有符合筛选条件的源块"));
      return;
    }
    visible.forEach((b, idx) => {
      const isSelected = selected.has(b.block_id);
      const isMatched = suggested.has(b.block_id);
      const checkbox = h("input", {
        type: "checkbox",
        checked: isSelected,
        "aria-label": `选择 ${b.block_id}`,
        "data-testid": `${testIdPrefix}-checkbox-${b.block_id}`,
      });
      checkbox.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleBlock(visible, idx, e);
      });
      const row = h(
        "div",
        {
          class: `bp-item ${isSelected ? "is-selected" : ""}`,
          role: "option",
          "aria-selected": isSelected ? "true" : "false",
          "data-testid": `${testIdPrefix}-item-${b.block_id}`,
          onClick: (e) => toggleBlock(visible, idx, e),
        },
        checkbox,
        h(
          "div",
          { class: "bp-item-body" },
          h(
            "div",
            { class: "bp-item-meta" },
            h("span", { class: "mono", style: { fontWeight: "700", fontSize: "12px" } }, b.block_id),
            h("span", { class: "pill pill-muted", style: { padding: "0 6px", fontSize: "11px" } }, KIND_LABELS[b.kind] || b.kind),
            isMatched ? h("span", { class: "pill pill-accent", style: { padding: "0 6px", fontSize: "11px" } }, "本页匹配") : null,
            (b.path || []).length ? h("span", { class: "faint", style: { fontSize: "11.5px" } }, b.path.join(" / ")) : null
          ),
          h("div", { class: "bp-item-text", title: b.text || "" }, (b.text || "(空块)").replace(/\s+/g, " "))
        )
      );
      listWrap.append(row);
    });
  }

  searchInput.addEventListener("input", () => {
    searchQuery = searchInput.value;
    renderList();
  });

  matchedToggle.addEventListener("click", () => {
    onlyMatched = !onlyMatched;
    matchedToggle.classList.toggle("primary", onlyMatched);
    renderList();
  });

  const selectVisibleBtn = h(
    "button",
    {
      type: "button",
      class: "btn sm",
      "data-testid": `${testIdPrefix}-select-visible`,
      onClick: () => {
        for (const b of visibleBlocks()) selected.add(b.block_id);
        renderSelectedBar();
        renderList();
        emit();
      },
    },
    "全选当前列表"
  );

  const selectMatchedBtn = suggested.size
    ? h(
        "button",
        {
          type: "button",
          class: "btn sm",
          "data-testid": `${testIdPrefix}-select-matched`,
          onClick: () => {
            for (const id of suggested) selected.add(id);
            renderSelectedBar();
            renderList();
            emit();
          },
        },
        "选用本页匹配"
      )
    : null;

  const clearBtn = h(
    "button",
    {
      type: "button",
      class: "btn ghost sm",
      "data-testid": `${testIdPrefix}-clear`,
      onClick: () => {
        selected.clear();
        renderSelectedBar();
        renderList();
        emit();
      },
    },
    "清空"
  );

  const toolbar = h(
    "div",
    { class: "bp-toolbar" },
    searchInput,
    suggested.size ? matchedToggle : null,
    selectMatchedBtn,
    selectVisibleBtn,
    clearBtn
  );

  root.append(selectedBar, toolbar, kindsWrap, listWrap);
  renderSelectedBar();
  renderKinds();
  renderList();

  return {
    el: root,
    getSelectedIds,
    setSelectedIds(ids) {
      selected.clear();
      for (const id of ids || []) selected.add(id);
      renderSelectedBar();
      renderList();
    },
  };
}
