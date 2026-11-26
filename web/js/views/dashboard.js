// 项目总览：进度漏斗、预算审计、控制器下一步建议与局域网配对状态
import { api } from "../api.js";
import { empty, fmtTime, h, modal, pill, pre, showError, statCard, table, toast } from "../ui.js";

export async function render(state) {
  const root = h("div", { class: "stack-lg", "data-testid": "view-dashboard" });
  if (!state.currentProjectId) {
    root.append(
      h("div", { class: "page-head" }, h("h1", {}, "工作台总览")),
      empty("还没有选中的项目。请点击顶部导航栏「＋ 新建项目」创建一个新项目。")
    );
    return root;
  }

  const ov = await api.overview(state.currentProjectId);
  const c = ov.counts || {};
  const b = ov.budget || {};
  const nextAction = ov.next_action || {};

  root.append(
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        {},
        h("h1", {}, ov.project?.name || "总览"),
        h("p", {}, `${ov.project?.taskpack || "markdown_to_latex"} · 启用模型：${ov.project?.active_model || "未发布"}`)
      ),
      h(
        "div",
        { class: "row" },
        h("a", { class: "btn sm", href: "#/materials" }, "上传材料"),
        h("a", { class: "btn sm", href: "#/queue" }, "发起生成"),
        h("a", { class: "btn sm", href: "#/workbench" }, "进入工作台"),
        h(
          "button",
          {
            type: "button",
            class: "btn ghost sm",
            "data-testid": "dash-pairing-btn",
            onClick: () => openPairingModal(state.currentProjectId),
          },
          "局域网设备配对"
        )
      )
    )
  );

  // 核心指标卡
  root.append(
    h(
      "div",
      { class: "stats" },
      statCard("已解析文档", c.documents ?? 0, { hint: "原始材料 / 语料文档" }),
      statCard("生成轮次", c.generations ?? 0, { hint: "基线 / 规则 / 教师 / 修订" }),
      statCard("待处理批注", c.annotations_open ?? 0, { hint: "人工框选或待确认问题" }),
      statCard("已接纳样本", c.examples_accepted ?? 0, { hint: `候选 ${c.examples_candidate ?? 0} / 总 ${c.examples_total ?? 0}` }),
      statCard(
        "外部调用额度",
        b.paid_calls_enabled ? `${b.calls_used ?? 0}/${b.max_calls ?? 0}` : "仅免费/限次",
        { hint: b.paid_calls_enabled ? `已用 ${b.spent_cents ?? 0} ¢` : "付费调用默认关闭（可在模型接入开启）" }
      )
    )
  );

  // 控制器建议与活跃任务
  const actionCard = h(
    "section",
    { class: "card stack", "data-testid": "dash-controller-card" },
    h(
      "div",
      { class: "row-between" },
      h("h2", {}, "建议动作"),
      pill(nextAction.action || "none", "accent")
    ),
    h("div", {}, nextAction.summary || "当前无待定动作。"),
    nextAction.expected_benefit ? h("div", { class: "muted", style: { fontSize: "12.5px" } }, `预期收益：${nextAction.expected_benefit}`) : null,
    h(
      "div",
      { class: "row" },
      nextAction.action
        ? h(
            "button",
            {
              type: "button",
              class: "btn primary sm",
              "data-testid": "dash-exec-next-btn",
              onClick: async () => {
                try {
                  const res = await api.executeAction(state.currentProjectId, nextAction);
                  toast(res.executed ? `已执行动作：${nextAction.action}` : `未执行：${(res.errors || []).join("; ")}`, res.executed ? "ok" : "warn");
                  window.dispatchEvent(new CustomEvent("gf:refresh"));
                } catch (err) {
                  showError(err);
                }
              },
            },
            "执行建议动作"
          )
        : null,
      h(
        "button",
        {
          type: "button",
          class: "btn sm",
          onClick: async () => {
            const proposed = await api.proposeAction(state.currentProjectId);
            modal({
              title: "控制器建议详情",
              body: pre(JSON.stringify(proposed, null, 2)),
              okText: "关闭",
            });
          },
        },
        "查看建议 JSON"
      )
    )
  );

  const layers = ov.validation_by_layer || {};
  const layerRows = Object.entries(layers).map(([layer, counts]) => [
    h("strong", {}, layer),
    String(counts.pass ?? 0),
    String(counts.warn ?? 0),
    String(counts.fail ?? 0),
    String(counts.skipped ?? 0),
  ]);
  const validationCard = h(
    "section",
    { class: "card stack" },
    h("h2", {}, "四层质量检查分布"),
    layerRows.length ? table(["层次", "Pass", "Warn", "Fail", "Skipped"], layerRows) : empty("尚未产生生成记录，等待第一轮基线生成后统计。")
  );

  root.append(h("div", { class: "grid cols-2" }, actionCard, validationCard));

  // 最近生成与活跃队列
  const gens = ov.recent_generations || [];
  const jobs = ov.active_jobs || [];
  root.append(
    h(
      "div",
      { class: "grid cols-2" },
      h(
        "section",
        { class: "card stack" },
        h("div", { class: "row-between" }, h("h2", {}, "最近生成记录"), h("a", { href: "#/queue", class: "muted", style: { fontSize: "12.5px" } }, "查看全部 →")),
        gens.length
          ? table(
              ["ID", "模式", "状态", "创建时间"],
              gens.map((g) => [h("span", { class: "mono" }, g.id), g.mode, pill(g.status, g.status === "succeeded" ? "ok" : g.status === "failed" ? "danger" : "warn"), fmtTime(g.created_at)])
            )
          : empty("暂无生成记录")
      ),
      h(
        "section",
        { class: "card stack" },
        h("div", { class: "row-between" }, h("h2", {}, `进行中任务 (${jobs.length})`), h("a", { href: "#/inbox", class: "muted", style: { fontSize: "12.5px" } }, "问题收件箱 →")),
        jobs.length
          ? table(
              ["任务类型", "状态", "进度", "阶段"],
              jobs.map((j) => [j.type, pill(j.status, "accent"), `${j.progress_done}/${j.progress_total || "?"}`, j.stage || "queued"])
            )
          : empty("当前队列空闲")
      )
    )
  );

  return root;
}

async function openPairingModal(projectId) {
  try {
    const info = await api.pairingInfo(projectId);
    const sessions = await api.pairingSessions(projectId).catch(() => ({ sessions: [] }));
    modal({
      title: "局域网配对与项目级访问权限",
      body: h(
        "div",
        { class: "stack" },
        !info.required
          ? h("div", { class: "alert info" }, "当前服务仅绑定在本机回环地址，未启用局域网配对码。使用 `--lan` 启动后会为每个项目签发独立 8 位配对码。")
          : h(
              "div",
              { class: "subcard stack" },
              h("div", { class: "row-between" }, h("span", { class: "muted" }, "本项目配对码："), h("strong", { class: "mono", style: { fontSize: "20px", letterSpacing: "2px" } }, info.code || "—")),
              h("div", { class: "faint", style: { fontSize: "12px" } }, `剩余有效期：${info.expires_in ?? 0} 秒（仅解锁本项目；本机密钥配置页不对局域网开放）`),
              h(
                "button",
                {
                  type: "button",
                  class: "btn danger sm",
                  onClick: async () => {
                    const rotated = await api.rotatePairing(projectId);
                    toast(`已轮换配对码为 ${rotated.code}，所有旧设备会话已作废`, "ok");
                    document.getElementById("modal")?.close();
                    openPairingModal(projectId);
                  },
                },
                "轮换配对码并踢出旧设备"
              )
            ),
        h("div", { class: "muted", style: { fontSize: "12.5px" } }, `当前已配对活跃设备会话：${(sessions.sessions || []).length} 个`)
      ),
      okText: "关闭",
    });
  } catch (err) {
    showError(err);
  }
}
