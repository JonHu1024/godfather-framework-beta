// 材料与参考：支持拖拽与批量多文件上传、自适应不同任务包文件类型、查看历史产物与参考摘要
import { api, artifactUrl } from "../api.js";
import { currentProject } from "../state.js";
import { clear, empty, fmtBytes, fmtTime, h, pill, showError, table, toast } from "../ui.js";

export async function render(state) {
  const root = h("div", { class: "stack-lg", "data-testid": "view-materials" });
  if (!state.currentProjectId) {
    root.append(empty("请先在顶部新建或选择一个项目。"));
    return root;
  }

  const project = currentProject();
  const isSft = project?.taskpack === "general_sft";
  const materials = await api.materials(state.currentProjectId);

  const kindSelect = h(
    "select",
    { "data-testid": "mat-kind-select" },
    (isSft
      ? [
          ["dataset", "训练语料（JSONL / JSON / CSV / Markdown）"],
          ["corpus", "原始纯文本文档"],
          ["asset", "附件资源"],
        ]
      : [
          ["markdown", "主 Markdown 源文档 (.md / .txt)"],
          ["asset", "正文配图 / 素材资源 (.png / .jpg / .pdf)"],
          ["reference_md", "参考样例 Markdown（将注入教师上下文）"],
          ["reference_tex", "参考样例 TeX（将注入教师上下文）"],
          ["reference_pdf", "参考成稿 PDF（仅人工对照）"],
        ]
    ).map(([val, label]) => h("option", { value: val }, label))
  );

  const dialectSelect = h(
    "select",
    { "data-testid": "mat-dialect-select" },
    [
      ["pandoc-markdown", "Pandoc Markdown（支持管道表格/公式）"],
      ["gfm", "GitHub Flavored Markdown"],
      ["commonmark", "CommonMark 标准方言"],
    ].map(([val, label]) => h("option", { value: val }, label))
  );

  const fileInput = h("input", { type: "file", multiple: true, style: { display: "none" }, "data-testid": "mat-file-input" });
  const uploadQueueLog = h("div", { class: "stack", "data-testid": "mat-upload-log" });

  async function uploadFiles(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    clear(uploadQueueLog);
    let okCount = 0;
    for (const file of list) {
      const row = h("div", { class: "row-between subcard" }, h("span", {}, `${file.name} (${fmtBytes(file.size)})`), pill("上传解析中…", "accent"));
      uploadQueueLog.append(row);
      const fd = new FormData();
      fd.append("kind", kindSelect.value);
      fd.append("filename", file.name);
      fd.append("dialect", dialectSelect.value);
      fd.append("file", file);
      try {
        const res = await api.uploadMaterial(state.currentProjectId, fd);
        okCount += 1;
        row.lastChild.replaceWith(
          pill(res.document_id ? `已解析 (${res.parse?.block_count ?? 0} 块)` : "已入库", "ok")
        );
      } catch (err) {
        row.lastChild.replaceWith(pill(`失败：${err.message}`, "danger"));
      }
    }
    if (okCount > 0) {
      toast(`成功导入 ${okCount}/${list.length} 个文件`, "ok");
      window.dispatchEvent(new CustomEvent("gf:refresh"));
    }
  }

  const dropzone = h(
    "div",
    {
      class: "dropzone",
      "data-testid": "mat-dropzone",
      onClick: () => fileInput.click(),
    },
    fileInput,
    h("div", { style: { fontWeight: "600", fontSize: "15px" } }, "点击选择或将多个文件拖放到这里上传"),
    h(
      "div",
      { class: "muted", style: { fontSize: "12.5px", marginTop: "4px" } },
      isSft
        ? "支持一次拖入多个 .jsonl / .json / .csv / .md / .txt 文件，服务端将自动切分样本条目。"
        : "支持一次上传多个 .md 源文档、配图（保持相对文件名），或上传参考样例 .md / .tex。"
    )
  );
  fileInput.addEventListener("change", () => uploadFiles(fileInput.files).catch(showError));
  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("is-dragover");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("is-dragover");
    uploadFiles(e.dataTransfer?.files).catch(showError);
  });

  root.append(
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        {},
        h("h1", {}, "材料与参考"),
        h("p", {}, "导入主源文档、配图及可选 reference_md / reference_tex 参考样例。")
      )
    ),
    h(
      "section",
      { class: "card stack" },
      h("div", { class: "grid cols-2" }, h("label", { class: "stack" }, h("span", { class: "muted" }, "材料类型"), kindSelect), !isSft ? h("label", { class: "stack" }, h("span", { class: "muted" }, "Markdown 解析方言"), dialectSelect) : null),
      dropzone,
      uploadQueueLog
    )
  );

  // 源文档列表
  const docs = materials.source_documents || [];
  const refs = materials.reference_examples || [];
  const arts = materials.artifacts || [];

  root.append(
    h(
      "div",
      { class: "grid cols-2" },
      h(
        "section",
        { class: "card stack" },
        h("h2", {}, `已导入源文档 (${docs.length})`),
        docs.length
          ? table(
              ["文件名", "方言", "大小", "状态", "文件预览"],
              docs.map((d) => [
                d.filename,
                pill(d.dialect || "default", "muted"),
                fmtBytes(d.size_bytes),
                pill(d.status, d.status === "parsed" ? "ok" : ""),
                d.artifact_id ? h("a", { href: artifactUrl(d.artifact_id), target: "_blank" }, "打开") : "—",
              ])
            )
          : empty("尚未导入任何源文档")
      ),
      h(
        "section",
        { class: "card stack" },
        h("h2", {}, `参考样例 (${refs.length})`),
        refs.length
          ? table(
              ["标题", "包含材料", "作用域", "时间"],
              refs.map((r) => {
                const links = [];
                if (r.md_artifact_id) links.push(h("a", { href: artifactUrl(r.md_artifact_id), target: "_blank" }, "MD"));
                if (r.tex_artifact_id) links.push(h("a", { href: artifactUrl(r.tex_artifact_id), target: "_blank" }, "TeX"));
                if (r.pdf_artifact_id) links.push(h("a", { href: artifactUrl(r.pdf_artifact_id), target: "_blank" }, "PDF"));
                return [r.title || r.id, h("div", { class: "row" }, ...links), r.scope, fmtTime(r.created_at)];
              })
            )
          : empty("暂无参考样例（上传 reference_md 或 reference_tex 后会自动注入教师 prompt 提示上下文）")
      )
    )
  );

  root.append(
    h(
      "section",
      { class: "card stack" },
      h("h2", {}, `本项目所有产物资产 (${arts.length})`),
      arts.length
        ? table(
            ["类型", "文件名/ID", "大小", "创建时间", "操作"],
            arts.slice(0, 30).map((a) => {
              let fn = a.id;
              try {
                fn = JSON.parse(a.meta_json || "{}").filename || a.id;
              } catch {
                // ignore
              }
              return [
                pill(a.kind, "muted"),
                fn,
                fmtBytes(a.size_bytes),
                fmtTime(a.created_at),
                h("a", { href: artifactUrl(a.id), target: "_blank" }, "下载/查看"),
              ];
            })
          )
        : empty("暂无产物")
    )
  );

  return root;
}
