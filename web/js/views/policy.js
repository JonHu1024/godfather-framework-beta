// 策略记录：可视化新增偏好规则 + YAML 版本对比、激活与编辑
import { api } from "../api.js";
import { empty, fmtTime, h, pill, showError, table, toast } from "../ui.js";

export async function render(state) {
  const root = h("div", { class: "stack-lg", "data-testid": "view-policy" });
  if (!state.currentProjectId) {
    root.append(empty("请先在顶部新建或选择一个项目。"));
    return root;
  }

  const [active, versions] = await Promise.all([
    api.activePolicy(state.currentProjectId),
    api.policies(state.currentProjectId),
  ]);

  const yamlEditor = h("textarea", {
    style: { width: "100%", minHeight: "260px", fontFamily: "var(--mono)", fontSize: "12.5px" },
    "data-testid": "policy-yaml-editor",
  });
  yamlEditor.value = active.yaml || "";
  const versionInput = h("input", { value: `policy-${String(versions.length + 1).padStart(3, "0")}`, "data-testid": "policy-version-input" });

  // 可视化新增规则表单
  const ruleContent = h("input", { placeholder: "例如：正文表格一律使用 booktabs 三线表，表注放在表格上方", style: { flex: "1" }, "data-testid": "policy-rule-content" });
  const ruleScope = h(
    "select",
    {},
    [
      ["project", "全局 (project)"],
      ["block_kind", "按块类型 (block_kind)"],
      ["document", "单篇文档 (document)"],
    ].map(([k, v]) => h("option", { value: k }, v))
  );
  const rulePriority = h("input", { type: "number", value: "70", min: "0", max: "100", style: { width: "80px" } });

  root.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "策略规则与版本记录"), h("p", {}, `当前启用策略版本：${active.version}（共 ${(active.preferences || []).length} 条规则）。`))),
    h(
      "section",
      { class: "card stack", "data-testid": "policy-quick-add" },
      h("h2", {}, "快捷添加一条项目偏好规则（自动保存为新启用版本）"),
      h(
        "div",
        { class: "row" },
        ruleContent,
        ruleScope,
        rulePriority,
        h(
          "button",
          {
            type: "button",
            class: "btn primary sm",
            "data-testid": "policy-rule-add-btn",
            onClick: async () => {
              if (!ruleContent.value.trim()) {
                toast("请填写规则内容", "warn");
                return;
              }
              try {
                const nextPrefs = [
                  ...(active.preferences || []),
                  {
                    pref_id: `pref-${String((active.preferences || []).length + 1).padStart(3, "0")}`,
                    content: ruleContent.value.trim(),
                    scope: ruleScope.value,
                    priority: Number(rulePriority.value || 70),
                    confidence: 0.9,
                    status: "active",
                  },
                ];
                await api.savePolicy(state.currentProjectId, {
                  version: `policy-${String(versions.length + 1).padStart(3, "0")}`,
                  preferences: nextPrefs,
                  status: "active",
                  notes: "可视化快捷新增规则",
                });
                toast("已新增规则并激活新版本", "ok");
                window.dispatchEvent(new CustomEvent("gf:refresh"));
              } catch (err) {
                showError(err);
              }
            },
          },
          "添加并激活"
        )
      )
    ),
    h(
      "div",
      { class: "grid cols-2" },
      h(
        "section",
        { class: "card stack" },
        h("h2", {}, `历史策略版本 (${versions.length})`),
        versions.length
          ? table(
              ["版本号", "状态", "备注", "创建时间", "操作"],
              versions.map((v) => [
                h("strong", { class: "mono" }, v.version),
                pill(v.status, v.status === "active" ? "ok" : "muted"),
                v.notes || "—",
                fmtTime(v.created_at),
                v.status === "active"
                  ? pill("当前使用", "accent")
                  : h(
                      "button",
                      {
                        type: "button",
                        class: "btn sm",
                        onClick: async () => {
                          await api.activatePolicy(state.currentProjectId, v.id);
                          toast(`已切换到版本 ${v.version}`, "ok");
                          window.dispatchEvent(new CustomEvent("gf:refresh"));
                        },
                      },
                      "设为启用"
                    ),
              ])
            )
          : empty("暂无策略历史")
      ),
      h(
        "section",
        { class: "card stack" },
        h("h2", {}, "YAML 源码编辑器"),
        h("label", { class: "row" }, h("span", { class: "muted" }, "新版本号："), versionInput),
        yamlEditor,
        h(
          "div",
          { class: "row" },
          h(
            "button",
            {
              type: "button",
              class: "btn primary sm",
              "data-testid": "policy-save-yaml-btn",
              onClick: async () => {
                try {
                  await api.savePolicy(state.currentProjectId, {
                    version: versionInput.value.trim() || "policy-next",
                    yaml: yamlEditor.value,
                    status: "active",
                  });
                  toast("策略包 YAML 已保存并启用", "ok");
                  window.dispatchEvent(new CustomEvent("gf:refresh"));
                } catch (err) {
                  showError(err);
                }
              },
            },
            "保存为新启用版本"
          )
        )
      )
    )
  );

  return root;
}
