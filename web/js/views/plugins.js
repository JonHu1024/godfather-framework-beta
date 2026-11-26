// Cordis 插件与任务包管理中心
import { api } from "../api.js";
import { empty, h, pill, showError, table, toast } from "../ui.js";

const STATE_KIND = {
  active: ["运行中 (ACTIVE)", "ok"],
  pending: ["等待依赖 (PENDING)", "warn"],
  loading: ["加载中", "accent"],
  disposed: ["已停用 (DISPOSED)", "muted"],
  failed: ["挂载失败 (FAILED)", "danger"],
};

export async function render() {
  const root = h("div", { class: "stack-lg", "data-testid": "view-plugins" });
  const data = await api.plugins();
  const fibers = data.fibers || [];
  const taskpacks = data.taskpacks || [];
  const services = data.services || [];

  root.append(
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        {},
        h("h1", {}, "插件管理"),
        h("p", {}, "Cordis 插件纤维状态与外部 ./plugins/ 目录扩展。")
      ),
      h(
        "div",
        { class: "row" },
        h(
          "button",
          {
            type: "button",
            class: "btn primary",
            "data-testid": "plugins-discover-btn",
            onClick: async () => {
              try {
                const res = await api.discoverPlugins();
                toast(`扫描完成，发现外部插件：${res.discovered?.length || 0} 个`, "ok");
                window.dispatchEvent(new CustomEvent("gf:refresh"));
              } catch (err) {
                showError(err);
              }
            },
          },
          "扫描外部插件"
        )
      )
    )
  );

  // 已挂载 TaskPack 概览
  root.append(
    h(
      "section",
      { class: "card stack", "data-testid": "plugins-taskpacks-card" },
      h("h2", {}, `已注册的任务包契约 (${taskpacks.length})`),
      taskpacks.length
        ? table(
            ["TaskPack ID", "显示名称", "版本", "输入格式", "支持模式", "特性说明"],
            taskpacks.map((tp) => [
              h("span", { class: "mono", style: { fontWeight: "700" } }, tp.id),
              tp.name,
              pill(`v${tp.version}`, "muted"),
              (tp.input_formats || []).join(" "),
              h("div", { class: "row" }, (tp.modes || []).map((m) => pill(m.id, "accent"))),
              tp.description,
            ])
          )
        : empty("当前没有活跃的任务包，请在下方插件纤维列表中重新启用。")
    )
  );

  // PluginFiber 热插拔列表
  root.append(
    h(
      "section",
      { class: "card stack", "data-testid": "plugins-fibers-card" },
      h("div", { class: "row-between" }, h("h2", {}, `Cordis PluginFiber 生命周期管理 (${fibers.length})`), data.plugins_dir ? h("span", { class: "muted mono", style: { fontSize: "12px" } }, `外置目录：${data.plugins_dir}`) : null),
      table(
        ["插件名称", "分类", "状态", "注入依赖 (inject)", "回收句柄数", "操作"],
        fibers.map((f) => {
          const [stText, stTone] = STATE_KIND[f.state] || [f.state, "muted"];
          const toggleBtn = f.enabled
            ? h(
                "button",
                {
                  type: "button",
                  class: "btn danger sm",
                  "data-testid": `plugin-disable-${f.name}`,
                  onClick: async () => {
                    try {
                      await api.disablePlugin(f.name);
                      toast(`已停用插件 ${f.name}，所有绑定处理器已回收`, "ok");
                      window.dispatchEvent(new CustomEvent("gf:refresh"));
                    } catch (err) {
                      showError(err);
                    }
                  },
                },
                "停用"
              )
            : h(
                "button",
                {
                  type: "button",
                  class: "btn primary sm",
                  "data-testid": `plugin-enable-${f.name}`,
                  onClick: async () => {
                    try {
                      await api.enablePlugin(f.name);
                      toast(`已启用插件 ${f.name}`, "ok");
                      window.dispatchEvent(new CustomEvent("gf:refresh"));
                    } catch (err) {
                      showError(err);
                    }
                  },
                },
                "启用"
              );

          const reloadBtn = f.hot_reloadable
            ? h(
                "button",
                {
                  type: "button",
                  class: "btn sm",
                  "data-testid": `plugin-reload-${f.name}`,
                  onClick: async () => {
                    try {
                      await api.reloadPlugin(f.name);
                      toast(`已热重载 ${f.name}`, "ok");
                      window.dispatchEvent(new CustomEvent("gf:refresh"));
                    } catch (err) {
                      showError(err);
                    }
                  },
                },
                "热重载"
              )
            : null;

          return [
            h("div", {}, h("div", { style: { fontWeight: "600" } }, f.display_name || f.name), h("div", { class: "faint mono", style: { fontSize: "11.5px" } }, `${f.name} · ${f.source}`)),
            pill(f.kind || "extension", "muted"),
            h("div", {}, pill(stText, stTone), f.error ? h("div", { class: "faint", style: { color: "var(--danger)", fontSize: "11.5px", marginTop: "3px" } }, f.error) : null),
            h("div", { class: "row" }, (f.inject || []).map((dep) => pill(dep, (f.missing_dependencies || []).includes(dep) ? "danger" : "muted"))),
            String(f.effect_count ?? 0),
            h("div", { class: "row" }, toggleBtn, reloadBtn),
          ];
        })
      )
    )
  );

  // IoC Services
  root.append(
    h(
      "section",
      { class: "card stack" },
      h("h2", {}, `IoC 内核活跃服务 (${services.length})`),
      h("div", { class: "row" }, services.map((s) => pill(`${s.name} (${s.type})`, "accent")))
    )
  );

  return root;
}
