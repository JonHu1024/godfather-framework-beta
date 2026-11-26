// 轻量 DOM / 交互工具（无框架依赖）
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "html") el.innerHTML = value;
    else if (key === "text") el.textContent = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "style" && typeof value === "object") {
      Object.assign(el.style, value);
    } else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function toast(message, kind = "") {
  const host = document.getElementById("toast-host");
  const el = h("div", { class: `toast ${kind}` }, message);
  host.append(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3800);
}

export function showError(error) {
  console.error(error);
  // 配对相关的错误有专门的界面流程（弹配对框 / 提示重新配对），
  // 这里不要再弹一次错误提示，免得同一个问题弹两遍。
  if (error?.pairingRequired || error?.handled) return;
  toast(error?.message || String(error), "error");
}

export function modal({ title, body, okText = "确定", onOk }) {
  const dialog = document.getElementById("modal");
  document.getElementById("modal-title").textContent = title;
  const bodyHost = clear(document.getElementById("modal-body"));
  bodyHost.append(body);
  const okButton = document.getElementById("modal-ok");
  okButton.textContent = okText;
  const cancelButton = document.getElementById("modal-cancel");
  if (cancelButton) cancelButton.onclick = () => dialog.close();
  // 上一次弹窗留下的"确定"不能算数：每次打开先清掉
  dialog.returnValue = "";
  // onOk 可能是 async：必须 await，否则返回 false 也拦不住关闭
  okButton.onclick = async () => {
    if (onOk) {
      try {
        const result = await onOk();
        if (result === false) return;
      } catch (error) {
        showError(error);
        return;
      }
    }
    // 标记是"确定"关的：confirmDialog 靠它区分确定/关闭
    dialog.returnValue = "ok";
    dialog.close();
  };
  // 回车不能静默关掉弹窗：表单隐式提交不触发 click，会丢输入。
  // 用赋值而不是 addEventListener：这个 form 是 index.html 里那个永不替换的，
  // 每次 addEventListener 都会再叠一个监听器，开 N 次弹窗后按一次回车会触发 N 次 onOk。
  const form = dialog.querySelector("form");
  if (form) {
    form.onsubmit = (event) => {
      event.preventDefault();
      okButton.click();
    };
  }
  dialog.showModal();
  return dialog;
}

export function confirmDialog(message) {
  return new Promise((resolve) => {
    const dialog = modal({
      title: "确认",
      body: h("p", {}, message),
      okText: "确定",
      onOk: () => resolve(true),
    });
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}

export function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function fmtBytes(bytes) {
  if (!bytes) return "0B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(index ? 1 : 0)}${units[index]}`;
}

export function pill(text, kind = "") {
  return h("span", { class: `pill ${kind ? `pill-${kind}` : ""}` }, text);
}

export function statCard(label, value, { hint = "", kind = "" } = {}) {
  return h(
    "div",
    { class: `stat ${kind}` },
    h("div", { class: "label" }, label),
    h("div", { class: "value" }, value),
    hint ? h("div", { class: "hint" }, hint) : null,
  );
}

export function table(headers, rows) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, headers.map((label) => h("th", { scope: "col" }, label)))),
      h("tbody", {}, rows.map((cells) => h("tr", {}, cells.map((cell) => h("td", {}, cell)))))
    ),
  );
}

export function empty(message) {
  return h("div", { class: "empty" }, message);
}

/** 简单的 YAML 展示（保持原样，避免引入解析依赖） */
export function pre(text) {
  return h("pre", {}, text ?? "");
}


/**
 * 请求整页刷新。
 *
 * 各视图的 render() 由 main.js 统一挂载，视图内部再调自己的 render()
 * 只会生成游离 DOM 被丢弃 —— 所以数据变更后必须通过这个事件让 main.js 重新渲染。
 */
export function requestRefresh(route = "") {
  window.dispatchEvent(new CustomEvent("gf:refresh", { detail: { route } }));
}
