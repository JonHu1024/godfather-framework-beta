// 批注层：框选/圈画 → 归一化坐标；渲染已有批注
export class AnnotationLayer {
  constructor(pdfView, { onSelect } = {}) {
    this.pdfView = pdfView;
    this.onSelect = onSelect || (() => {});
    this.annotations = [];
    this.selectedId = "";
    this.pending = null; // {pageIndex, bbox}
    this._bind();
  }

  setSelected(id) {
    this.selectedId = id || "";
    this.render();
  }

  _bind() {
    this.pdfView.container.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      // 点在已有批注框上时不触发新框选（BUG-08 UI 误触修复）
      if (event.target.closest?.(".annot-rect")) return;
      const overlay = event.target.closest?.(".annot-layer");
      if (!overlay) return;
      const holder = overlay.parentElement;
      const rect = holder.getBoundingClientRect();
      this.pending = {
        pageIndex: Number(overlay.dataset.page) - 1,
        startX: event.clientX - rect.left,
        startY: event.clientY - rect.top,
        rect,
        holder,
      };
      holder.setPointerCapture?.(event.pointerId);
    });

    this.pdfView.container.addEventListener("pointermove", (event) => {
      if (!this.pending) return;
      const rect = this.pending.rect;
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      this.pending.current = {
        left: Math.min(this.pending.startX, x),
        top: Math.min(this.pending.startY, y),
        width: Math.abs(x - this.pending.startX),
        height: Math.abs(y - this.pending.startY),
      };
      this._drawPending();
    });

    const finish = (event) => {
      if (!this.pending) return;
      // 先把要用到的字段取出来，再清空 pending —— 否则后面读 this.pending 会是 null
      const { current, holder, pageIndex } = this.pending;
      this.pending = null;
      this._clearPending();
      if (!current || current.width < 6 || current.height < 6) return;
      const rect = holder.getBoundingClientRect();
      const width = rect.width || 1;
      const height = rect.height || 1;
      const bbox = [
        Number((current.left / width).toFixed(4)),
        Number((current.top / height).toFixed(4)),
        Number((current.width / width).toFixed(4)),
        Number((current.height / height).toFixed(4)),
      ];
      this.onSelect({ pageIndex, bbox });
    };
    this.pdfView.container.addEventListener("pointerup", finish);
    this.pdfView.container.addEventListener("pointercancel", finish);
  }

  _drawPending() {
    if (!this.pending) return;
    const holder = this.pending.holder;
    let box = holder.querySelector(".annot-rect.pending");
    if (!box) {
      box = document.createElement("div");
      box.className = "annot-rect pending";
      const label = document.createElement("span");
      label.className = "annot-label";
      label.textContent = "新批注";
      box.append(label);
      holder.append(box);
    }
    const current = this.pending.current;
    box.style.left = `${current.left}px`;
    box.style.top = `${current.top}px`;
    box.style.width = `${current.width}px`;
    box.style.height = `${current.height}px`;
  }

  _clearPending() {
    this.pdfView.container.querySelectorAll(".annot-rect.pending").forEach((node) => node.remove());
  }

  setAnnotations(annotations) {
    this.annotations = annotations;
    this.render();
  }

  render() {
    this.pdfView.container.querySelectorAll(".annot-layer").forEach((overlay) => {
      overlay.querySelectorAll(".annot-rect:not(.pending)").forEach((node) => node.remove());
      const pageIndex = Number(overlay.dataset.page) - 1;
      for (const annotation of this.annotations) {
        if (annotation.page_index !== pageIndex) continue;
        const box = document.createElement("div");
        const isSelected = annotation.id && annotation.id === this.selectedId;
        const statusCls = annotation.status === "accepted" ? " is-accepted" : "";
        const selCls = isSelected ? " is-selected" : "";
        box.className = `annot-rect${statusCls}${selCls}`;
        box.dataset.annotationId = annotation.id || "";
        box.style.left = `${(annotation.bbox?.[0] || 0) * 100}%`;
        box.style.top = `${(annotation.bbox?.[1] || 0) * 100}%`;
        box.style.width = `${(annotation.bbox?.[2] || 0) * 100}%`;
        box.style.height = `${(annotation.bbox?.[3] || 0) * 100}%`;
        box.title = annotation.comment || annotation.category;
        const label = document.createElement("span");
        label.className = "annot-label";
        const blocksCount = (annotation.source_block_candidates || []).length;
        label.textContent = blocksCount
          ? `${annotation.category || "批注"} (${blocksCount}块)`
          : (annotation.category || "待定位");
        box.append(label);
        box.addEventListener("pointerdown", (e) => e.stopPropagation());
        box.addEventListener("click", (event) => {
          event.stopPropagation();
          this.setSelected(annotation.id);
          this.onSelect({ annotation, pageIndex });
        });
        overlay.append(box);
      }
    });
  }
}
