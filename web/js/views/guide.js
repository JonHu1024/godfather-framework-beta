// 使用指南（平实、准确、按当前项目进度即时打卡）
import { api } from "../api.js";
import { currentProject } from "../state.js";
import { h, pill, statCard } from "../ui.js";

function stepCard(done, stepNo, title, desc, hash) {
  return h(
    "a",
    { class: `step-card ${done ? "is-done" : ""}`, href: hash },
    h("div", { class: "row-between" }, h("span", { class: "step-num" }, `STEP ${stepNo}`), pill(done ? "已完成" : "待处理", done ? "ok" : "muted")),
    h("div", { class: "step-title" }, title),
    h("div", { class: "step-desc" }, desc)
  );
}

export async function render(state) {
  const root = h("div", { class: "stack-lg", "data-testid": "view-guide" });
  const project = currentProject();
  const ov = state.currentProjectId ? await api.overview(state.currentProjectId).catch(() => null) : null;
  const counts = ov?.counts || {};
  const hasSnapshots = state.currentProjectId ? (await api.snapshots(state.currentProjectId).catch(() => [])).length > 0 : false;
  const hasRuns = state.currentProjectId ? (await api.trainingRuns(state.currentProjectId).catch(() => [])).length > 0 : false;

  root.append(
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        {},
        h("h1", {}, "使用说明"),
        h("p", {}, "数据准备、教师生成、批注修订与本地 LoRA 微调操作指引。")
      ),
      project ? pill(`${project.name} (${project.taskpack})`, "accent") : pill("未选择项目", "warn")
    )
  );

  root.append(
    h(
      "section",
      { class: "card stack" },
      h("h2", {}, "基本流程"),
      h(
        "div",
        { class: "stepper" },
        stepCard(Boolean(state.currentProjectId && (counts.documents || 0) > 0), 1, "导入材料", "上传源文件、配图或 JSONL/CSV 语料。", "#/materials"),
        stepCard((counts.generations || 0) > 0 || (counts.examples_total || 0) > 0, 2, "生成与校验", "运行 rule_only / pandoc_baseline 或教师模型。", "#/queue"),
        stepCard((counts.annotations_open || 0) === 0 && (counts.generations || 0) > 0, 3, "批注修订", "在工作台框选或多选源块提交修订。", "#/workbench"),
        stepCard((counts.examples_accepted || 0) > 0 && hasSnapshots, 4, "冻结快照", "批量接纳样本，试算切分比例后冻结。", "#/datasets"),
        stepCard(hasRuns, 5, "训练导出", "LoRA/QLoRA 训练、配对评测并导出 GGUF。", "#/training")
      )
    )
  );

  root.append(
    h(
      "div",
      { class: "grid cols-2" },
      h(
        "section",
        { class: "card stack" },
        h("h2", {}, "常用提示与关键避坑"),
        h(
          "ul",
          { style: { margin: "0", paddingLeft: "18px" } },
          h("li", {}, h("strong", {}, "新建项目入口"), "：在页面最上方导航栏点击「＋ 新建项目」，可自由选择「Markdown → LaTeX」或「通用指令微调 (General SFT)」任务包。"),
          h("li", {}, h("strong", {}, "远程大模型教师"), "：点击左侧「模型接入中心」，一键填入 DeepSeek / MiniMax / Qwen 等地址与 API Key，并可在同一页面开启外部调用预算开关。"),
          h("li", {}, h("strong", {}, "多选源码块批注"), "：无论在 PDF 框选弹窗还是「指定源块」弹窗，均支持勾选、搜索与 Shift 连选多个连续块，确定后直接联合提交修订。"),
          h("li", {}, h("strong", {}, "留出集（Holdout）隔离"), "：切分出的 holdout 样本永不放进教师提示词或训练样本里，确保你的最终模型对比结论真实可靠。")
        )
      ),
      h(
        "section",
        { class: "card stack" },
        h("h2", {}, "当前项目实时状态"),
        h(
          "div",
          { class: "stats" },
          statCard("已导入文档", counts.documents ?? 0),
          statCard("训练样本总数", counts.examples_total ?? 0),
          statCard("已接纳样本", counts.examples_accepted ?? 0),
          statCard("待处理批注", counts.annotations_open ?? 0)
        )
      )
    )
  );

  return root;
}
