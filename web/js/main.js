// 前端入口：路由、项目切换、Cordis 任务包元数据、SSE 实时连接与全局作业跟踪
import { api, pairingHeader } from "./api.js";
import { refreshPairing } from "./pairing.js";
import { applySidebarCollapsed, applyTheme, currentProject, setCurrentProject, state } from "./state.js";
import { clear, h, modal, showError, toast } from "./ui.js";
import { render as renderDashboard } from "./views/dashboard.js";
import { render as renderDatasets } from "./views/datasets.js";
import { render as renderDocuments } from "./views/documents.js";
import { render as renderGuide } from "./views/guide.js";
import { render as renderInbox } from "./views/inbox.js";
import { render as renderMaterials } from "./views/materials.js";
import { render as renderModelsAccess } from "./views/models_access.js";
import { render as renderPlugins } from "./views/plugins.js";
import { render as renderPolicy } from "./views/policy.js";
import { render as renderQueue } from "./views/queue.js";
import { render as renderTraining } from "./views/training.js";
import { render as renderWorkbench } from "./views/workbench.js";

const ROUTES = {
  guide: renderGuide,
  dashboard: renderDashboard,
  materials: renderMaterials,
  documents: renderDocuments,
  queue: renderQueue,
  workbench: renderWorkbench,
  policy: renderPolicy,
  datasets: renderDatasets,
  training: renderTraining,
  models: renderModelsAccess,
  plugins: renderPlugins,
  inbox: renderInbox,
};

let eventSource = null;
let renderToken = 0;
let refreshTimer = null;

function currentRoute() {
  const hash = (location.hash || "#/dashboard").replace(/^#\/?/, "");
  const name = hash.split("?")[0] || "dashboard";
  return ROUTES[name] ? name : "dashboard";
}

function setActiveNav(route) {
  document.querySelectorAll("#sidenav [data-nav]").forEach((link) => {
    link.classList.toggle("active", link.dataset.nav === route);
  });
}

function setDrawerOpen(open) {
  const nav = document.getElementById("sidenav");
  const backdrop = document.getElementById("nav-backdrop");
  if (!nav || !backdrop) return;
  nav.classList.toggle("open", open);
  backdrop.classList.toggle("hidden", !open);
}

function updateBadges() {
  const jobsEl = document.getElementById("badge-jobs");
  const annEl = document.getElementById("badge-ann");
  const inboxEl = document.getElementById("badge-inbox");
  const pill = document.getElementById("btn-active-jobs-pill");
  const setBadge = (el, n) => {
    if (!el) return;
    const count = Number(n || 0);
    el.textContent = String(count);
    el.classList.toggle("hidden", count <= 0);
  };
  setBadge(jobsEl, state.navCounts.jobs);
  setBadge(annEl, state.navCounts.annotations);
  setBadge(inboxEl, state.navCounts.inbox);
  if (pill) {
    const running = Number(state.navCounts.jobs || 0);
    pill.textContent = running > 0 ? `运行中任务 ${running}` : "无运行任务";
    pill.classList.toggle("hidden", running <= 0);
    pill.classList.toggle("pill-accent", running > 0);
  }
}

async function refreshGlobalCounts() {
  if (!state.currentProjectId) {
    state.navCounts = { jobs: 0, annotations: 0, inbox: 0 };
    updateBadges();
    return;
  }
  try {
    const ov = await api.overview(state.currentProjectId);
    const counts = ov.counts || {};
    state.navCounts = {
      jobs: (ov.active_jobs || []).length,
      annotations: Number(counts.annotations_open || 0),
      inbox: (ov.open_questions || []).length,
    };
    state.activeJobs = ov.active_jobs || [];
    updateBadges();
  } catch {
    // 忽略后台角标刷新抖动，避免干扰主界面操作
  }
}

function updateProjectBadge() {
  const badge = document.getElementById("project-taskpack-badge");
  if (!badge) return;
  const proj = currentProject();
  if (!proj) {
    badge.textContent = "未选择项目";
    badge.className = "pill pill-muted";
    return;
  }
  const spec = state.taskpacks.find((t) => t.id === proj.taskpack);
  badge.textContent = spec ? spec.name : proj.taskpack || "markdown_to_latex";
  badge.className = "pill pill-accent";
}

async function loadMetadata() {
  try {
    const tp = await api.taskpacks();
    state.taskpacks = tp.taskpacks || [];
    state.defaultTaskpack = tp.default || "markdown_to_latex";
  } catch {
    state.taskpacks = [];
  }
  const list = await api.projects();
  state.projects = list;
  const select = document.getElementById("project-select");
  clear(select);
  if (!list.length) {
    select.append(h("option", { value: "" }, "暂无项目（点击右侧新建）"));
    setCurrentProject("");
  } else {
    const exists = list.some((p) => p.id === state.currentProjectId);
    if (!exists) setCurrentProject(list[0].id);
    for (const p of list) {
      select.append(
        h(
          "option",
          { value: p.id, selected: p.id === state.currentProjectId },
          p.name
        )
      );
    }
  }
  state.loaded = true;
  updateProjectBadge();
  refreshPairing();
}

async function renderCurrent() {
  const token = ++renderToken;
  const route = currentRoute();
  setActiveNav(route);
  setDrawerOpen(false);
  const main = document.getElementById("main");
  main.classList.toggle("is-workbench", route === "workbench");
  window.dispatchEvent(new CustomEvent("gf:teardown"));
  clear(main).append(h("div", { class: "loading" }, "正在渲染…"));
  try {
    const container = await ROUTES[route](state);
    if (token !== renderToken) return;
    clear(main).append(container);
    refreshGlobalCounts();
  } catch (error) {
    if (token !== renderToken) return;
    showError(error);
    clear(main).append(h("div", { class: "alert danger" }, `页面加载失败：${error.message}`));
  }
}

function openNewProjectModal() {
  const nameInput = h("input", {
    placeholder: "例如：高等数学讲义转 TeX，或 客服问答微调",
    style: { width: "100%" },
    "data-testid": "new-project-name",
  });
  const packs = state.taskpacks.length
    ? state.taskpacks
    : [
        {
          id: "markdown_to_latex",
          name: "Markdown → LaTeX",
          description: "面向 Markdown 转 LaTeX 的完整编译、PDF 批注与微调闭环。",
        },
        {
          id: "general_sft",
          name: "通用指令微调（General SFT）",
          description: "支持 JSONL / CSV / Markdown 问答与摘要等任意 SFT 微调。",
        },
      ];
  const taskSelect = h(
    "select",
    { style: { width: "100%" }, "data-testid": "new-project-taskpack" },
    packs.map((t) => h("option", { value: t.id, selected: t.id === state.defaultTaskpack }, `${t.name} (${t.id})`))
  );
  const descBox = h("div", { class: "subcard muted", style: { fontSize: "12.5px" } });
  const syncPackDesc = () => {
    const chosen = packs.find((t) => t.id === taskSelect.value);
    descBox.textContent = chosen ? chosen.description : "";
  };
  taskSelect.addEventListener("change", syncPackDesc);
  syncPackDesc();

  const noteInput = h("textarea", {
    placeholder: "备注（可选，例如训练目标模型规模、格式约束）",
    style: { width: "100%" },
    "data-testid": "new-project-desc",
  });

  modal({
    title: "新建微调项目",
    body: h(
      "div",
      { class: "stack" },
      h("label", { class: "stack" }, h("span", { class: "muted" }, "项目名称 *"), nameInput),
      h("label", { class: "stack" }, h("span", { class: "muted" }, "挂载 Cordis 任务包 (TaskPack) *"), taskSelect, descBox),
      h("label", { class: "stack" }, h("span", { class: "muted" }, "项目描述"), noteInput)
    ),
    okText: "创建并进入",
    onOk: async () => {
      const name = nameInput.value.trim();
      if (!name) {
        toast("请输入项目名称", "warn");
        return false;
      }
      const created = await api.createProject({
        name,
        taskpack: taskSelect.value,
        description: noteInput.value.trim(),
      });
      await loadMetadata();
      setCurrentProject(created.id);
      document.getElementById("project-select").value = created.id;
      updateProjectBadge();
      toast(`已创建项目「${name}」`, "ok");
      connectEvents();
      await renderCurrent();
    },
  });
}

async function openPreflightModal() {
  try {
    const data = await api.preflight();
    state.preflight = data;
    const providerCards = (data.providers || []).map((p) =>
      h(
        "div",
        { class: "row-between subcard" },
        h("span", {}, h("strong", {}, p.provider), " ", h("span", { class: "muted" }, p.message || "")),
        h("span", { class: `pill ${p.ok ? "pill-ok" : "pill-warn"}` }, p.ok ? "可用" : "未就绪")
      )
    );
    modal({
      title: "运行环境与健康自检",
      body: h(
        "div",
        { class: "stack" },
        h(
          "div",
          { class: "row-between subcard" },
          h("span", {}, h("strong", {}, "系统平台"), " ", h("span", { class: "muted" }, `${data.platform?.os || "?"} / Python ${data.platform?.python || "?"}`)),
          h("span", { class: "pill pill-accent" }, `默认教师：${data.default_teacher_provider || "ollama"}`)
        ),
        h(
          "div",
          { class: "row-between subcard" },
          h("span", {}, h("strong", {}, `LaTeX (${data.latex?.engine || "xelatex"})`), " ", h("span", { class: "muted" }, data.latex?.version || data.latex?.message || "")),
          h("span", { class: `pill ${data.latex?.ok ? "pill-ok" : "pill-warn"}` }, data.latex?.ok ? "可用" : "不可用")
        ),
        h(
          "div",
          { class: "row-between subcard" },
          h("span", {}, h("strong", {}, "Pandoc"), " ", h("span", { class: "muted" }, data.pandoc?.version || "未检测到（纯规则/教师/SFT模式仍可用）")),
          h("span", { class: `pill ${data.pandoc?.ok ? "pill-ok" : "pill-muted"}` }, data.pandoc?.ok ? "已安装" : "可选")
        ),
        h("div", { class: "stack" }, h("strong", {}, "教师模型提供方状态"), ...providerCards),
        h(
          "div",
          { class: "subcard" },
          h("div", { class: "muted", style: { fontSize: "12px" } }, `已挂载 Cordis 任务包：${(data.taskpacks || []).map((t) => t.id).join("、") || "无"}`),
          data.data_dir ? h("div", { class: "muted mono-wrap", style: { fontSize: "12px", marginTop: "4px" } }, `数据目录：${data.data_dir}`) : null
        )
      ),
      okText: "去模型接入中心",
      onOk: () => {
        location.hash = "#/models";
      },
    });
  } catch (error) {
    showError(error);
  }
}

function connectEvents() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  const connEl = document.getElementById("conn-status");
  const qs = new URLSearchParams({ project_id: state.currentProjectId || "" });
  const header = pairingHeader();
  const token = header["x-godfather-session"] || header["x-godfather-pairing"] || "";
  if (token) qs.set("session", token);

  eventSource = new EventSource(`/api/events?${qs.toString()}`);
  eventSource.onopen = () => {
    if (connEl) {
      connEl.textContent = "实时已连接";
      connEl.className = "pill pill-ok";
    }
  };
  eventSource.onerror = () => {
    if (connEl) {
      connEl.textContent = "实时重连中…";
      connEl.className = "pill pill-warn";
    }
  };
  eventSource.addEventListener("job", (ev) => {
    try {
      const payload = JSON.parse(ev.data);
      state.events.unshift(payload);
      if (state.events.length > 150) state.events.length = 150;
      window.dispatchEvent(new CustomEvent("gf:event", { detail: payload }));

      if (payload.stage === "done" || payload.stage === "failed" || payload.stage === "queued") {
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => {
          refreshGlobalCounts();
        }, 250);
      }
      if (payload.stage === "done") {
        toast(payload.message || "后台任务已完成", "ok");
      } else if (payload.stage === "failed") {
        toast(payload.message || "任务执行失败", "error");
      }
    } catch {
      // ignore malformed SSE frame
    }
  });
}

function bindGlobalUi() {
  applyTheme(state.theme);
  applySidebarCollapsed(state.sidebarCollapsed);
  const themeBtn = document.getElementById("btn-theme");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      applyTheme(state.theme === "dark" ? "light" : "dark");
    });
  }

  document.getElementById("btn-sidebar-collapse")?.addEventListener("click", () => {
    applySidebarCollapsed(!state.sidebarCollapsed);
  });

  document.getElementById("btn-nav-toggle")?.addEventListener("click", () => {
    const isOpen = document.getElementById("sidenav")?.classList.contains("open");
    setDrawerOpen(!isOpen);
  });
  document.getElementById("nav-backdrop")?.addEventListener("click", () => setDrawerOpen(false));
  document.getElementById("modal-close-x")?.addEventListener("click", () => {
    document.getElementById("modal")?.close();
  });

  document.getElementById("project-select").addEventListener("change", (event) => {
    setCurrentProject(event.target.value);
    updateProjectBadge();
    refreshPairing();
    connectEvents();
    renderCurrent();
  });

  document.getElementById("btn-new-project").addEventListener("click", openNewProjectModal);
  document.getElementById("btn-preflight").addEventListener("click", openPreflightModal);
  document.getElementById("btn-active-jobs-pill")?.addEventListener("click", () => {
    location.hash = "#/queue";
  });

  window.addEventListener("hashchange", renderCurrent);
  window.addEventListener("gf:refresh", async (ev) => {
    const requestedRoute = ev?.detail?.route;
    if (requestedRoute && requestedRoute !== currentRoute()) {
      location.hash = `#/${requestedRoute}`;
      return;
    }
    await renderCurrent();
  });
  window.addEventListener("gf:auth-denied", () => {
    const connEl = document.getElementById("conn-status");
    if (connEl) {
      connEl.textContent = "等待配对凭证";
      connEl.className = "pill pill-warn";
    }
    refreshPairing();
  });
}

async function init() {
  bindGlobalUi();
  try {
    await loadMetadata();
  } catch (error) {
    showError(error);
  }
  connectEvents();
  await renderCurrent();
}

init();
