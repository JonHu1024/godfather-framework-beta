// 配对流程：
// - 桌面端（本机回环）：顶栏显示**当前项目**的配对码，手机输入它即可访问这个项目；
// - 手机端（局域网）：输入配对码 → 兑换成项目级会话令牌 → 之后所有请求都带这个令牌。
import { pairing, pairingInfo, redeemPairingCode, rotatePairingCode } from "./api.js";
import { state } from "./state.js";
import { clear, confirmDialog, h, modal, toast } from "./ui.js";

// 与服务端一致（见 src/godfather/pairing.py）：去掉容易看错的 0/O/1/I
const CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ";
const CODE_LENGTH = 8;
const CODE_PATTERN = new RegExp(`[^${CODE_ALPHABET}]`, "g");

/** 把输入收拾成合法配对码：大写、去掉非法字符、最多 8 位。 */
export function cleanCode(value) {
  return String(value || "")
    .toUpperCase()
    .replace(CODE_PATTERN, "")
    .slice(0, CODE_LENGTH);
}

/** 当前项目的名字（手机上项目列表只有会话解锁的那一个）。 */
function projectName(projectId) {
  const project = state.projects.find((item) => item.id === projectId);
  return project ? project.name : "";
}

// --------------------------------------------------------------------------
// 顶栏配对码
// --------------------------------------------------------------------------

let badge = null;
// 顶栏这块是不是"点一下就能输入配对码"的状态（手机端还没配对）
let badgeNeedsPairing = false;

/** 顶栏里的配对区域（第一次用到时才插进去）。 */
function ensureBadge() {
  if (badge) return badge;
  badge = h("div", { id: "pairing-status", class: "pairing-box" });
  badge.append(
    h("span", { id: "pairing-code", class: "pill pill-muted" }, "配对码 ——"),
    h("span", { id: "pairing-hint", class: "pairing-hint" }, ""),
    h(
      "button",
      {
        id: "pairing-refresh",
        class: "btn ghost btn-sm",
        title: "重新生成配对码（会让已配对的手机失效）",
        onclick: regenerate,
      },
      "重新生成",
    ),
  );
  // 还没配对时，点顶栏这块可以重新打开配对框（关掉弹窗后还有救）
  badge.addEventListener("click", (event) => {
    if (!badgeNeedsPairing) return;
    if (event.target.closest("#pairing-refresh")) return;
    promptPairing();
  });
  const right = document.querySelector(".topbar-right");
  if (right) right.prepend(badge);
  return badge;
}

function setBadge({ text, kind, title, hint, refresh, needsPairing = false }) {
  const host = ensureBadge();
  host.hidden = false;
  badgeNeedsPairing = needsPairing;
  const pill = host.querySelector("#pairing-code");
  pill.textContent = text;
  pill.className = `pill ${kind || "pill-muted"}${needsPairing ? " clickable" : ""}`;
  if (title) pill.title = title;
  const hintNode = host.querySelector("#pairing-hint");
  hintNode.textContent = hint || "";
  const button = host.querySelector("#pairing-refresh");
  button.hidden = !refresh;
}

/** 不需要配对时（纯本机部署）把整块藏起来。 */
function hideBadge() {
  if (badge) badge.hidden = true;
}

/**
 * 刷新顶栏的配对信息。
 *
 * 后端只在**回环**请求里给码：拿到码说明当前是本机（桌面端），
 * 只拿到 required 则是局域网访问（手机端）。
 */
export async function refreshPairing() {
  const projectId = state.projectId;
  try {
    const info = await pairingInfo(projectId);
    pairing.required = Boolean(info.required);
    if (!info.required) {
      // 纯本机部署（没开 --lan）：根本不需要配对，别占顶栏
      hideBadge();
      return info;
    }
    if (info.code) {
      // 桌面端：显示当前项目的配对码
      const name = projectName(projectId) || projectId;
      setBadge({
        text: `配对码 ${info.code}`,
        kind: "pill-warn",
        title: `手机端在配对框里输入该码即可访问「${name}」${info.expires_in ? `（${info.expires_in} 秒后过期）` : ""}`,
        hint: `手机端输入这个码即可访问「${name}」`,
        refresh: true,
      });
      return info;
    }
    if (pairing.paired) {
      // 手机端：已经配对，显示解锁的项目
      const name = projectName(pairing.projectId) || pairing.projectId;
      setBadge({
        text: `已配对「${name}」`,
        kind: "pill-ok",
        title: "当前会话只能访问这一个项目",
        hint: "",
        refresh: false,
      });
      return info;
    }
    if (!projectId && state.loaded) {
      // 桌面端能取到项目列表，只是还没建项目
      setBadge({
        text: "先建个项目",
        kind: "pill-muted",
        title: "配对码是项目级的：先新建一个项目才会生成配对码",
        hint: "",
        refresh: false,
      });
      return info;
    }
    setBadge({
      text: "需要配对码",
      kind: "pill-warn",
      title: "点击这里输入电脑上显示的配对码",
      hint: "",
      refresh: false,
      needsPairing: true,
    });
    return info;
  } catch (error) {
    console.warn("pairing info failed", error);
    return null;
  }
}

/** 重新生成当前项目的配对码。 */
async function regenerate() {
  const projectId = state.projectId;
  if (!projectId) {
    toast("先选中一个项目", "error");
    return;
  }
  const ok = await confirmDialog(
    "重新生成配对码会让已配对的手机失效（它们需要重新输入新码），确定吗？",
  );
  if (!ok) return;
  try {
    const info = await rotatePairingCode(projectId);
    const name = projectName(projectId) || projectId;
    setBadge({
      text: `配对码 ${info.code}`,
      kind: "pill-warn",
      title: `手机端在配对框里输入该码即可访问「${name}」`,
      hint: `手机端输入这个码即可访问「${name}」`,
      refresh: true,
    });
    toast("已生成新的配对码", "ok");
  } catch (error) {
    if (error.status === 404) {
      // TODO(后端)：后端没有提供 /api/system/pairing/rotate 时，
      // 退回到"重新取码"（码过期后服务端会自动轮换）。
      await refreshPairing();
      toast("后端没有重新生成接口，已重新取码", "error");
      return;
    }
    toast(error.message || "重新生成失败", "error");
  }
}

// --------------------------------------------------------------------------
// 配对框（手机端）
// --------------------------------------------------------------------------

let dialogOpen = false;

/**
 * 弹出配对框：输入 8 位配对码 → 兑换会话 → 落到对应的项目上。
 *
 * @param {string} message 额外的提示（例如"这个码解锁的是另一个项目"）
 */
export function promptPairing(message = "") {
  // 并发 401 时不能重复弹窗（对已打开的 dialog 再 showModal 会抛 InvalidStateError）
  if (dialogOpen) return;
  dialogOpen = true;

  const errorBox = h("div", { class: "notice danger", hidden: true });
  const input = h("input", {
    type: "text",
    class: "code-input",
    placeholder: "8 位配对码",
    maxlength: String(CODE_LENGTH),
    autocomplete: "off",
    autocapitalize: "characters",
    spellcheck: "false",
    value: "",
  });
  // 输入时自动大写并过滤非法字符
  input.addEventListener("input", () => {
    const cleaned = cleanCode(input.value);
    if (cleaned !== input.value) input.value = cleaned;
    clear(errorBox);
    errorBox.hidden = true;
  });

  const dialog = modal({
    title: "需要配对码",
    body: h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "muted" },
        "本服务以局域网方式运行，手机访问需要输入电脑上显示的配对码。" +
          "配对码是**项目级**的：输入哪个项目的码，就只能访问那一个项目。",
      ),
      message ? h("div", { class: "notice warn" }, message) : null,
      h("label", { class: "stack" }, "配对码", input),
      errorBox,
    ),
    okText: "连接",
    onOk: async () => {
      const code = cleanCode(input.value);
      if (!code) {
        showPairingError(errorBox, "请输入配对码");
        return false;
      }
      try {
        const result = await redeemPairingCode(code);
        // 存会话令牌（后端会拿它校验"只能访问这个项目"）
        pairing.set(result.token, result.project_id);
        // 界面直接落在配对码对应的项目上
        state.projectId = result.project_id;
        localStorage.setItem("gf.project", result.project_id);
        toast(`已配对到「${result.project_name || result.project_id}」`, "ok");
        setTimeout(() => location.reload(), 300);
        return true;
      } catch (error) {
        showPairingError(
          errorBox,
          error.status === 401 ? "配对码无效或已过期" : error.message || "配对失败",
        );
        return false;
      }
    },
  });
  dialog.addEventListener("close", () => {
    dialogOpen = false;
  }, { once: true });
  input.focus();
}

/** 在配对框里显示一行错误（不关弹窗）。 */
function showPairingError(box, message) {
  clear(box);
  box.textContent = message;
  box.hidden = false;
}

/**
 * 会话解锁的项目和当前请求的项目对不上。
 *
 * 服务端在 403 里带回了 unlocked_project_id：提示用户，并清掉令牌重新配对。
 */
export function handleScopeMismatch(error) {
  const unlocked = error?.unlockedProjectId || "";
  if (error) error.handled = true;
  toast(error?.message || "当前配对码只解锁了另一个项目", "error");
  pairing.clear();
  promptPairing(
    unlocked
      ? `当前配对码只解锁了另一个项目（${unlocked}），请输入那个项目的配对码。`
      : "当前配对码只解锁了另一个项目，请输入对应项目的配对码。",
  );
}
