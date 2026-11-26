// PDF.js 封装：按需渲染页面 + 文本层（用于批注与源块定位）
import * as pdfjsLib from "../vendor/pdfjs/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL("../vendor/pdfjs/pdf.worker.min.mjs", import.meta.url).href;

export class PdfView {
  constructor(container) {
    this.container = container;
    this.doc = null;
    this.pages = [];
    this.texts = [];
    this.scale = 1.0;
    this.zoomFactor = 1.0;
    this._pageWidthPt = 595;
    this._renderEpoch = 0;
    this._observer = null;
    this._resizeObserver = null;
    this._resizeRaf = null;
    this.onZoomChange = null;
  }

  /** 释放文档、观察器与对象 URL（视图离开时应调用）。 */
  destroy() {
    this._renderEpoch += 1;
    if (this._resizeRaf !== null) {
      cancelAnimationFrame(this._resizeRaf);
      this._resizeRaf = null;
    }
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
    if (this._observer) {
      this._observer.disconnect();
      this._observer = null;
    }
    if (this.doc && typeof this.doc.destroy === "function") {
      this.doc.destroy().catch(() => {});
    }
    this.doc = null;
    this.pages = [];
    this.texts = [];
  }

  get pageCount() {
    return this.doc ? this.doc.numPages : 0;
  }

  /** 按容器宽度自适应缩放，并叠加用户手动缩放系数（扣除 .pdf-stage 左右 36px 内边距与滚动条余量）。 */
  fitScale(pageWidthPt = this._pageWidthPt || 595) {
    const width = this.container.clientWidth || 0;
    const avail = Math.max(0, width - 44);
    const base = avail ? Math.max(0.35, Math.min(2.0, avail / (pageWidthPt || 595))) : 0.85;
    const effective = Math.max(0.35, Math.min(3.2, base * (this.zoomFactor || 1.0)));
    return Math.round(effective * 100) / 100;
  }

  setZoom(factor) {
    this.zoomFactor = Math.max(0.5, Math.min(2.5, Number(factor) || 1.0));
    this.rerenderAll();
    if (this.onZoomChange) this.onZoomChange(this.zoomFactor, this.scale);
  }

  rerenderAll() {
    this._renderEpoch += 1;
    this.scale = this.fitScale();
    for (const holder of this.container.querySelectorAll("[data-page]")) {
      holder.dataset.rendered = "";
      for (const child of [...holder.children]) {
        if (!child.classList.contains("annot-layer")) child.remove();
      }
    }
    if (this.doc) {
      for (const holder of this.container.querySelectorAll("[data-page]")) {
        const rect = holder.getBoundingClientRect();
        if (rect.bottom >= -200 && rect.top <= (window.innerHeight || 900) + 400) {
          this._renderPage(Number(holder.dataset.page)).catch(() => {});
        }
      }
      if (this.doc.numPages > 0) this._renderPage(1).catch(() => {});
    }
  }

  scrollToPage(pageNumber) {
    const holder = this.container.querySelector(`[data-page="${pageNumber}"]`);
    if (holder) {
      holder.scrollIntoView({ behavior: "smooth", block: "start" });
      this._renderPage(pageNumber).catch(() => {});
    }
  }

  async load(url) {
    this.destroy();
    this.doc = await pdfjsLib.getDocument({ url, withCredentials: false }).promise;
    this.pages = [];
    this.texts = [];
    this.container.innerHTML = "";
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            this._renderPage(Number(entry.target.dataset.page));
          }
        }
      },
      { root: this._scrollRoot(), rootMargin: "400px 0px" }
    );
    this._observer = observer;

    for (let number = 1; number <= this.doc.numPages; number += 1) {
      const holder = document.createElement("div");
      holder.className = "pdf-canvas-wrap";
      holder.dataset.page = String(number);
      holder.innerHTML = `<div style="width:100%;max-width:480px;height:680px;color:#999;font-size:12px;padding:8px">第 ${number} 页</div>`;
      this.container.append(holder);
      // 每一页都要 observe：只 observe 已渲染的页会让第 2 页起永远停留在占位块，
      // 连带 findPageOfText 只能搜到第 1 页。
      observer.observe(holder);
    }
    // 首屏立即渲染
    if (this.doc.numPages > 0) this._renderPage(1);
    // 容器宽度变化（旋转屏幕、拖动分栏）后重新渲染（使用 rAF 避免 ResizeObserver loop 警告）
    if (typeof ResizeObserver !== "undefined") {
      this._resizeObserver = new ResizeObserver(() => {
        if (this._resizeRaf !== null) cancelAnimationFrame(this._resizeRaf);
        this._resizeRaf = requestAnimationFrame(() => {
          this._resizeRaf = null;
          if (!this.doc || !this.container.isConnected || !this.container.clientWidth) return;
          const next = this.fitScale();
          if (Math.abs(next - this.scale) < 0.02) return;
          this._renderEpoch += 1;
          this.scale = next;
          // 只清掉 canvas 与文本层：**不能整块 innerHTML=""**，
          // 那会把 .annot-layer 一起抹掉，所有批注框消失且不会重绘。
          for (const holder of this.container.querySelectorAll("[data-page]")) {
            holder.dataset.rendered = "";
            for (const child of [...holder.children]) {
              if (!child.classList.contains("annot-layer")) child.remove();
            }
          }
          if (this.doc) {
            for (const holder of this.container.querySelectorAll("[data-page]")) {
              const rect = holder.getBoundingClientRect();
              if (rect.bottom >= -200 && rect.top <= (window.innerHeight || 900) + 400) {
                this._renderPage(Number(holder.dataset.page)).catch(() => {});
              }
            }
            if (this.doc.numPages > 0) this._renderPage(1).catch(() => {});
          }
        });
      });
      this._resizeObserver.observe(this.container);
    }
  }

  /** 找到真正在滚动的容器：IntersectionObserver 的 root 必须是它，
   *  否则 root 覆盖全部内容，所有页会一次性渲染（rootMargin 预加载失效）。 */
  _scrollRoot() {
    let node = this.container.parentElement;
    while (node) {
      const style = window.getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowY)) return node;
      node = node.parentElement;
    }
    return null;
  }

  async _renderPage(number) {
    if (!this.doc) return;
    const holder = this.container.querySelector(`[data-page="${number}"]`);
    if (!holder || holder.dataset.rendered === "1") return;
    holder.dataset.rendered = "1";
    const epoch = this._renderEpoch;
    let page;
    try {
      page = await this.doc.getPage(number);
      if (!this.doc || epoch !== this._renderEpoch) return;
    } catch (err) {
      // 失败或文档已被销毁时复位标记
      holder.dataset.rendered = "";
      if (!this.doc || String(err?.message || "").includes("Transport destroyed")) return;
      throw err;
    }
    const baseViewport = page.getViewport({ scale: 1.0 });
    this._pageWidthPt = baseViewport.width || 595;
    this.scale = this.fitScale(this._pageWidthPt);
    const viewport = page.getViewport({ scale: this.scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(viewport.width);
    canvas.height = Math.floor(viewport.height);
    const context = canvas.getContext("2d");
    await page.render({ canvasContext: context, viewport }).promise;
    if (!this.doc || epoch !== this._renderEpoch) return;
    // 重绘单页时同样只清理非批注层节点，保留 .annot-layer 与已有批注框
    for (const child of [...holder.children]) {
      if (!child.classList.contains("annot-layer")) child.remove();
    }
    holder.prepend(canvas);

    // 文本层：用于把页面内容与源块对应起来
    const textLayer = document.createElement("div");
    textLayer.className = "textLayer";
    holder.append(textLayer);
    let text = "";
    try {
      const content = await page.getTextContent();
      text = content.items.map((item) => item.str).join(" ");
      // PDF.js v5 去掉了 renderTextLayer，改用 TextLayer 类
      if (pdfjsLib.TextLayer) {
        const layer = new pdfjsLib.TextLayer({
          textContentSource: content,
          container: textLayer,
          viewport,
        });
        await layer.render();
      } else {
        console.warn("PDF.js 没有导出 TextLayer，文本层不可用（不影响批注定位）");
      }
    } catch (err) {
      console.warn("text layer failed", err);
    }
    this.texts[number - 1] = text;

    // 供批注层挂载（重绘时复用已有的层，批注框才不会被抹掉）
    let overlay = holder.querySelector(".annot-layer");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.className = "annot-layer";
      overlay.dataset.page = String(number);
      holder.append(overlay);
    }
    this._observer?.observe(holder);
    if (this.onPageRendered) this.onPageRendered(number, overlay);
  }

  pageText(pageIndex) {
    return this.texts[pageIndex] || "";
  }

  /** 在整篇文本中查找与给定文本最匹配的页码（1-based），找不到返回 0。 */
  findPageOfText(needle, { minRatio = 0.6 } = {}) {
    const target = normalize(needle);
    if (!target) return 0;
    const probe = target.slice(0, 40);
    for (let index = 0; index < this.pageCount; index += 1) {
      const pageText = normalize(this.pageText(index));
      if (!pageText) continue;
      if (pageText.includes(probe)) return index + 1;
    }
    // 退化：用较短的片段匹配
    const short = probe.slice(0, 12);
    if (short.length >= 4) {
      for (let index = 0; index < this.pageCount; index += 1) {
        const pageText = normalize(this.pageText(index));
        if (pageText && pageText.includes(short)) return index + 1;
      }
    }
    return 0;
  }

}

export function normalize(text) {
  return (text || "")
    .replace(/\s+/g, "")
    .replace(/[，。！？、；：""''（）()]/g, "")
    .toLowerCase();
}
