// 后端 API 封装（自动附带局域网项目级会话令牌，兼容全部视图与 pairing.js）
import { handleScopeMismatch, promptPairing } from "./pairing.js";

export const pairing = {
  token: localStorage.getItem("gf.pairing.token") || "",
  projectId: localStorage.getItem("gf.pairing.project") || "",
  required: false,
  get paired() {
    return Boolean(this.token && this.projectId);
  },
  set(token, projectId) {
    this.token = token || "";
    this.projectId = projectId || "";
    if (this.token) localStorage.setItem("gf.pairing.token", this.token);
    else localStorage.removeItem("gf.pairing.token");
    if (this.projectId) localStorage.setItem("gf.pairing.project", this.projectId);
    else localStorage.removeItem("gf.pairing.project");
  },
  clear() {
    this.token = "";
    this.projectId = "";
    localStorage.removeItem("gf.pairing.token");
    localStorage.removeItem("gf.pairing.project");
  },
  header() {
    return this.token ? { "x-godfather-session": this.token } : {};
  },
};

export function pairingHeader() {
  return pairing.header();
}

function notifyAuthDenied(message) {
  window.dispatchEvent(new CustomEvent("gf:auth-denied", { detail: { message } }));
}

export function artifactUrl(artifactId) {
  const token = pairing.token || "";
  const base = `/api/artifacts/${encodeURIComponent(artifactId)}/content`;
  return token ? `${base}?session=${encodeURIComponent(token)}` : base;
}

export async function req(path, { method = "GET", body = null, raw = false } = {}) {
  const headers = { ...pairing.header() };
  let payload = body;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const response = await fetch(path, { method, headers, body: payload });
  if (response.status === 401) {
    const info = await response.json().catch(() => ({}));
    pairing.clear();
    notifyAuthDenied(info.detail || "需要输入配对码或更换正确项目的配对码");
    promptPairing();
    const error = new Error(info.detail || "需要输入配对码");
    error.status = 401;
    error.pairingRequired = true;
    throw error;
  }
  if (response.status === 403) {
    const info = await response.json().catch(() => ({}));
    if (info.code === "scope_mismatch") {
      const err = new Error(info.detail || "当前配对码只解锁了另一个项目，请改用本项目的配对码");
      err.status = 403;
      err.unlockedProjectId = info.unlocked_project_id || "";
      notifyAuthDenied(err.message);
      handleScopeMismatch(err);
      throw err;
    }
    const err = new Error(info.detail || "无权访问此操作");
    err.status = 403;
    throw err;
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    } catch {
      // ignore non-json body
    }
    const err = new Error(detail);
    err.status = response.status;
    throw err;
  }
  if (raw) return response;
  return response.json();
}

export const pairingInfo = (projectId = "") =>
  req(`/api/system/pairing${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`);

export const rotatePairingCode = (projectId) =>
  req("/api/system/pairing/rotate", { method: "POST", body: { project_id: projectId } });

export async function redeemPairingCode(code) {
  const resp = await fetch("/api/system/pairing/redeem", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.detail || "配对失败");
    err.status = resp.status;
    throw err;
  }
  return data;
}

export const api = {
  get: (path) => req(path),
  post: (path, body = null) => req(path, { method: "POST", body }),
  put: (path, body = null) => req(path, { method: "PUT", body }),
  patch: (path, body = null) => req(path, { method: "PATCH", body }),
  delete: (path) => req(path, { method: "DELETE" }),

  // 系统与插件
  preflight: () => req("/api/system/preflight"),
  pairingInfo,
  rotatePairing: rotatePairingCode,
  pairingSessions: (projectId = "") =>
    req(`/api/system/pairing/sessions${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  taskpacks: () => req("/api/taskpacks"),
  plugins: () => req("/api/plugins"),
  discoverPlugins: () => req("/api/plugins/discover", { method: "POST" }),
  enablePlugin: (name) => req(`/api/plugins/${encodeURIComponent(name)}/enable`, { method: "POST" }),
  disablePlugin: (name) => req(`/api/plugins/${encodeURIComponent(name)}/disable`, { method: "POST" }),
  reloadPlugin: (name) => req(`/api/plugins/${encodeURIComponent(name)}/reload`, { method: "POST" }),

  // 模型接入中心 (BUG-03)
  modelsAccess: () => req("/api/models-access"),
  saveModelsAccess: (payload) => req("/api/models-access", { method: "PUT", body: payload }),
  testProvider: (payload) => req("/api/models-access/test", { method: "POST", body: payload }),
  providerModels: (provider) => req(`/api/models-access/models?provider=${encodeURIComponent(provider)}`),
  unloadOllama: (model = "") => req("/api/models-access/ollama/unload", { method: "POST", body: { model } }),

  // 项目
  projects: () => req("/api/projects"),
  createProject: (payload) => req("/api/projects", { method: "POST", body: payload }),
  updateProject: (id, payload) => req(`/api/projects/${id}`, { method: "PATCH", body: payload }),
  deleteProject: (id) => req(`/api/projects/${id}`, { method: "DELETE" }),
  overview: (id) => req(`/api/projects/${id}/overview`),

  // 材料与文档
  materials: (id) => req(`/api/projects/${id}/materials`),
  uploadMaterial: (id, formData) => req(`/api/projects/${id}/materials`, { method: "POST", body: formData }),
  documents: (id) => req(`/api/projects/${id}/documents`),
  documentDetail: (id) => req(`/api/documents/${id}`),

  // 生成与队列
  startGeneration: (id, payload) => req(`/api/projects/${id}/generations`, { method: "POST", body: payload }),
  generations: (id) => req(`/api/projects/${id}/generations`),
  generationDetail: (id) => req(`/api/generations/${id}`),
  jobs: (id, status = "") => req(`/api/projects/${id}/jobs${status ? `?status=${status}` : ""}`),
  jobDetail: (id) => req(`/api/jobs/${id}`),
  cancelJob: (id) => req(`/api/jobs/${id}/cancel`, { method: "POST" }),
  resumeJob: (id) => req(`/api/jobs/${id}/resume`, { method: "POST" }),

  // 批注与协同修订
  annotations: (projectId, documentId = "", status = "") => {
    const qs = new URLSearchParams();
    if (documentId) qs.set("document_id", documentId);
    if (status) qs.set("status", status);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return req(`/api/projects/${projectId}/annotations${suffix}`);
  },
  createAnnotation: (projectId, payload) =>
    req(`/api/projects/${projectId}/annotations`, { method: "POST", body: payload }),
  updateAnnotation: (id, payload) => req(`/api/annotations/${id}`, { method: "PATCH", body: payload }),
  deleteAnnotation: (id) => req(`/api/annotations/${id}`, { method: "DELETE" }),
  acceptAnnotation: (id, payload = null) =>
    req(`/api/annotations/${id}/accept`, { method: "POST", body: payload || {} }),

  // 策略与控制器
  policies: (projectId) => req(`/api/projects/${projectId}/policies`),
  activePolicy: (projectId) => req(`/api/projects/${projectId}/policies/active`),
  savePolicy: (projectId, payload) => req(`/api/projects/${projectId}/policies`, { method: "POST", body: payload }),
  activatePolicy: (projectId, policyId) =>
    req(`/api/projects/${projectId}/policies/${policyId}`, { method: "PATCH" }),
  proposeAction: (projectId, action = null) =>
    req(`/api/projects/${projectId}/controller/propose`, { method: "POST", body: { action } }),
  executeAction: (projectId, action) =>
    req(`/api/projects/${projectId}/controller/actions`, { method: "POST", body: { action } }),

  // 样本、快照与手工包
  examples: (projectId) => req(`/api/projects/${projectId}/examples`),
  updateExample: (id, status) => req(`/api/examples/${id}`, { method: "PATCH", body: { status } }),
  batchExampleStatus: (projectId, exampleIds, status) =>
    req(`/api/projects/${projectId}/examples/batch-status`, {
      method: "POST",
      body: { example_ids: exampleIds, status },
    }),
  snapshots: (projectId) => req(`/api/projects/${projectId}/datasets/snapshots`),
  previewSnapshot: (projectId, payload) =>
    req(`/api/projects/${projectId}/datasets/snapshots/preview`, { method: "POST", body: payload }),
  createSnapshot: (projectId, payload) =>
    req(`/api/projects/${projectId}/datasets/snapshots`, { method: "POST", body: payload }),
  exportTaskpack: (projectId, limit = 50) =>
    req(`/api/projects/${projectId}/datasets/taskpack?limit=${limit}`),
  importTaskpack: (projectId, payload) =>
    req(`/api/projects/${projectId}/datasets/import`, { method: "POST", body: { payload } }),

  // 问题收件箱与事件
  questions: (projectId) => req(`/api/projects/${projectId}/questions`),
  answerQuestion: (id, answer) => req(`/api/questions/${id}/answer`, { method: "POST", body: answer }),
  eventsHistory: (projectId = "", limit = 100) =>
    req(`/api/events/history?project_id=${encodeURIComponent(projectId)}&limit=${limit}`),

  // M1 训练 / 导出 / 评测 / 模型版本
  trainingBackends: () => req("/api/training/backends"),
  trainingResources: () => req("/api/training/resources"),
  trainingRuns: (projectId) => req(`/api/training/runs?project_id=${encodeURIComponent(projectId)}`),
  trainingRunDetail: (runId) => req(`/api/training/runs/${runId}`),
  startTrainingRun: (projectId, payload) =>
    req(`/api/training/runs?project_id=${encodeURIComponent(projectId)}`, { method: "POST", body: payload }),
  cancelTrainingRun: (runId) => req(`/api/training/runs/${runId}/cancel`, { method: "POST" }),
  restartTrainingRun: (runId) => req(`/api/training/runs/${runId}/restart`, { method: "POST" }),
  exportTrainingRun: (runId, payload) =>
    req(`/api/training/runs/${runId}/export`, { method: "POST", body: payload }),
  startEvaluation: (projectId, payload) =>
    req(`/api/training/evaluations?project_id=${encodeURIComponent(projectId)}`, { method: "POST", body: payload }),
  evaluations: (projectId) => req(`/api/training/evaluations?project_id=${encodeURIComponent(projectId)}`),
  models: (projectId) => req(`/api/training/models?project_id=${encodeURIComponent(projectId)}`),
  activateModel: (modelId, { force = false } = {}) =>
    req(`/api/training/models/${modelId}/activate?force=${force ? "true" : "false"}`, { method: "POST" }),
  rollbackModel: (projectId) =>
    req(`/api/training/models/rollback?project_id=${encodeURIComponent(projectId)}`, { method: "POST" }),
};
