// 问题收件箱：集中展示教师或规则流程暂停等待人回答的决策卡，答完后一键恢复任务
import { api } from "../api.js";
import { empty, fmtTime, h, pill, showError, toast } from "../ui.js";

export async function render(state) {
  const root = h("div", { class: "stack-lg", "data-testid": "view-inbox" });
  if (!state.currentProjectId) {
    root.append(empty("请先在顶部新建或选择一个项目。"));
    return root;
  }

  const cards = await api.questions(state.currentProjectId);
  root.append(
    h(
      "div",
      { class: "page-head" },
      h("div", {}, h("h1", {}, "待决策问题收件箱"), h("p", {}, "当教师模型或策略流程遇到歧义并暂停等待用户时，问题会出现在这里，回答后可继续执行任务。"))
    )
  );

  if (!cards.length) {
    root.append(empty("收件箱为空：当前没有任何需要人工回答的阻塞问题。"));
    return root;
  }

  for (const c of cards) {
    const isAnswered = c.status === "answered";
    const answerInput = h("textarea", { placeholder: "输入你的明确回答或决策意见…", style: { width: "100%" }, "data-testid": `inbox-answer-${c.id}` });
    if (isAnswered && c.answer?.answer) answerInput.value = String(c.answer.answer);

    root.append(
      h(
        "section",
        { class: "card stack", "data-testid": `inbox-card-${c.id}` },
        h("div", { class: "row-between" }, h("strong", {}, c.question), h("div", { class: "row" }, pill(isAnswered ? "已回答" : "待处理", isAnswered ? "ok" : "warn"), h("span", { class: "faint" }, fmtTime(c.created_at)))),
        c.context?.block_id ? h("div", { class: "muted", style: { fontSize: "12.5px" } }, `来源位置：${c.context.block_id}`) : null,
        !isAnswered
          ? h(
              "div",
              { class: "stack" },
              answerInput,
              h(
                "div",
                { class: "row" },
                h(
                  "button",
                  {
                    type: "button",
                    class: "btn primary sm",
                    "data-testid": `inbox-submit-${c.id}`,
                    onClick: async () => {
                      if (!answerInput.value.trim()) {
                        toast("请先填写回答内容", "warn");
                        return;
                      }
                      try {
                        await api.answerQuestion(c.id, { answer: answerInput.value.trim() });
                        if (c.job_id) {
                          await api.resumeJob(c.job_id).catch(() => {});
                        }
                        toast("已提交回答并尝试恢复关联任务", "ok");
                        window.dispatchEvent(new CustomEvent("gf:refresh"));
                      } catch (err) {
                        showError(err);
                      }
                    },
                  },
                  c.job_id ? "保存回答并恢复关联任务" : "保存回答"
                )
              )
            )
          : h("div", { class: "subcard" }, h("strong", {}, "已记录回答："), JSON.stringify(c.answer))
      )
    );
  }

  return root;
}
