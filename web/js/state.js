// 前端共享状态
export const state = {
  projects: [],
  loaded: false,
  currentProjectId: localStorage.getItem("gf.project") || "",
  get projectId() {
    return this.currentProjectId;
  },
  set projectId(id) {
    setCurrentProject(id);
  },
  taskpacks: [],
  defaultTaskpack: "markdown_to_latex",
  preflight: null,
  modelsAccess: null,
  events: [],
  activeJobs: [],
  navCounts: { jobs: 0, annotations: 0, inbox: 0 },
  pairingRequired: false,
  theme: localStorage.getItem("gf.theme") || "light",
  sidebarCollapsed: localStorage.getItem("gf.sidebarCollapsed") === "1",
};

export function applyTheme(theme) {
  const next = theme === "dark" ? "dark" : "light";
  state.theme = next;
  localStorage.setItem("gf.theme", next);
  document.documentElement.setAttribute("data-theme", next);
  if (next === "dark") {
    document.body.setAttribute("data-ds-dark-theme", "");
  } else {
    document.body.removeAttribute("data-ds-dark-theme");
  }
}

export function applySidebarCollapsed(collapsed) {
  state.sidebarCollapsed = Boolean(collapsed);
  localStorage.setItem("gf.sidebarCollapsed", state.sidebarCollapsed ? "1" : "0");
  document.getElementById("app")?.classList.toggle("is-collapsed", state.sidebarCollapsed);
}

export function setCurrentProject(id) {
  state.currentProjectId = id || "";
  if (id) localStorage.setItem("gf.project", id);
  else localStorage.removeItem("gf.project");
}

export function currentProject() {
  return state.projects.find((p) => p.id === state.currentProjectId) || null;
}

export function currentTaskpackSpec() {
  const proj = currentProject();
  const id = proj?.taskpack || state.defaultTaskpack || "markdown_to_latex";
  return state.taskpacks.find((t) => t.id === id) || null;
}
