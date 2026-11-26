"""生成架构图（PNG）。

为什么不用 AI 生图：架构图的价值在于**准确**（每个方框都要对应真实模块），
生成式模型画出来的图细节经常是错的。这里的图由代码画出来，
改代码时图跟着改，不会过期。

用法：python scripts/make_diagrams.py
输出：docs/img/*.png
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img"

# ---- 字体：优先微软雅黑，找不到就退回常见中文字体 ----
CANDIDATES = [
    "/home/jon_hu/.local/share/fonts/msyh.ttc",
    "/mnt/c/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
for path in CANDIDATES:
    if Path(path).exists():
        try:
            font_manager.fontManager.addfont(path)
            name = font_manager.FontProperties(fname=path).get_name()
            plt.rcParams["font.sans-serif"] = [name]
            break
        except Exception:  # noqa: BLE001
            continue
plt.rcParams["axes.unicode_minus"] = False

# ---- 配色（与网页风格一致：深色底 + 青色强调） ----
BG = "#0f1218"
BOX_EDGE = "#2b3442"
BOX_FACE = "#171d26"
ACCENT = "#4ec9b0"
ACCENT2 = "#7aa2f7"
WARN = "#f2c14e"
TEXT = "#d7dde8"
MUTED = "#8b95a5"

fig_w = 12.0


def box(ax, x, y, w, h, title, subtitle="", edge=BOX_EDGE, face=BOX_FACE,
        title_color=TEXT, dashed=False, title_size=12, sub_size=9.5):
    """画一个圆角方框，返回它的中心点。"""
    patch = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        linewidth=1.4,
        edgecolor=edge,
        facecolor=face,
        linestyle="--" if dashed else "-",
        zorder=2,
    )
    ax.add_patch(patch)
    if subtitle:
        ax.text(x + w / 2, y + h * 0.62, title, ha="center", va="center",
                color=title_color, fontsize=title_size, weight="bold", zorder=3)
        ax.text(x + w / 2, y + h * 0.28, subtitle, ha="center", va="center",
                color=MUTED, fontsize=sub_size, zorder=3)
    else:
        ax.text(x + w / 2, y + h / 2, title, ha="center", va="center",
                color=title_color, fontsize=title_size, weight="bold", zorder=3)
    return (x + w / 2, y + h / 2)


def arrow(ax, p1, p2, label="", color=ACCENT, style="-|>", rad=0.0, lx=0.0, ly=0.0,
          fontsize=8.5, dashed=False):
    ax.add_patch(FancyArrowPatch(
        p1, p2, arrowstyle=style, mutation_scale=13,
        linewidth=1.3, color=color,
        connectionstyle=f"arc3,rad={rad}",
        linestyle="--" if dashed else "-",
        shrinkA=3, shrinkB=3, zorder=1,
    ))
    if label:
        mx = (p1[0] + p2[0]) / 2 + lx
        my = (p1[1] + p2[1]) / 2 + ly
        ax.text(mx, my, label, ha="center", va="center", color=color,
                fontsize=fontsize, zorder=4,
                bbox=dict(boxstyle="round,pad=0.18", fc=BG, ec="none", alpha=0.92))


def new_fig(height):
    fig, ax = plt.subplots(figsize=(fig_w, height), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    return fig, ax


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    fig.savefig(path, facecolor=BG, bbox_inches="tight", pad_inches=0.18)
    plt.close(fig)
    print(f"  写出 {path.relative_to(ROOT)}")
    return path


# ==========================================================================
# 图 1：架构全景
# ==========================================================================
def diagram_architecture():
    fig, ax = new_fig(7.6)

    ax.text(0.5, 0.965, "Godfather 架构全景", ha="center", va="center",
            color=TEXT, fontsize=17, weight="bold")
    ax.text(0.5, 0.925, "浏览器  →  API  →  应用上下文  →  能力模块  →  外部程序",
            ha="center", va="center", color=MUTED, fontsize=10)

    LX, LW = 0.03, 0.58          # 左侧主体区
    RX, RW = 0.68, 0.29          # 右侧外部程序区

    # ---- 浏览器 ----
    box(ax, LX, 0.74, LW, 0.135,
             "浏览器（零构建：原生 ES 模块 + 本地 PDF.js）",
             "使用指南 · 总览 · 材料 · 文档 · 队列 · 工作台 · 策略 · 数据集 · 训练与模型 · 收件箱",
             edge=ACCENT, title_size=12)

    # ---- API ----
    box(ax, LX, 0.585, LW, 0.105,
              "FastAPI 路由层  api/",
              "projects · pipeline · events · training · scope（只做校验 / 鉴权 / 转发）")

    # ---- 应用上下文 ----
    core_y, core_h = 0.315, 0.205
    box(ax, LX, core_y, LW, core_h, "", edge=ACCENT2)
    ax.text(LX + 0.018, core_y + core_h - 0.035, "应用上下文 AppContext（唯一的“总闸”）",
            ha="left", va="center", color=ACCENT2, fontsize=11, weight="bold", zorder=3)
    parts = [
        (0.055, "db", "SQLite 元数据"),
        (0.205, "store", "内容寻址产物仓库"),
        (0.355, "queue", "任务队列（租约）"),
        (0.505, "teacher", "教师网关 + 预算"),
    ]
    pw = 0.135
    core_centers = []
    for x, t, s in parts:
        core_centers.append(box(ax, x, core_y + 0.028, pw, 0.075, t, s,
                                 edge=ACCENT2, face="#131a24",
                                 title_size=10, sub_size=7.8))

    # ---- 能力模块 ----
    caps = [
        (0.055, "providers", "ollama / manual"),
        (0.205, "taskpacks/latex", "解析·组装·编译·检查"),
        (0.355, "policy", "策略包 · 偏好引擎"),
        (0.505, "training", "训练·导出·评测·版本"),
    ]
    cap_centers = []
    for x, t, s in caps:
        cap_centers.append(box(ax, x, 0.175, pw, 0.075, t, s,
                                edge=BOX_EDGE, title_size=10, sub_size=7.8))

    # 上下文 → 能力模块
    for c, cap in zip(core_centers, cap_centers, strict=True):
        arrow(ax, (c[0], core_y + 0.028), (cap[0], 0.25), color=MUTED)

    # ---- 右侧：外部程序 ----
    ax.text(RX + RW / 2, 0.885, "外部程序", ha="center", va="center",
            color=WARN, fontsize=11, weight="bold")
    exts = [
        (0.755, "Ollama", "本地推理（教师 / 候选模型）"),
        (0.625, "LaTeX 引擎", "MiKTeX / TeX Live（隔离编译）"),
        (0.495, "Pandoc", "纯工具基线"),
        (0.365, "基座模型", "HuggingFace 本地目录"),
        (0.235, "训练数据", "冻结快照 JSONL"),
    ]
    ext_centers = []
    for y, t, s in exts:
        ext_centers.append(box(ax, RX, y, RW, 0.085, t, s,
                                edge=WARN, face="#201c14",
                                title_size=10.5, sub_size=8))

    # 能力模块 → 外部程序：走"总线"通道，所有连线共用一条竖线，互不交叉
    bus_x = 0.648
    # (起点 x, 起点 y, 终点 x, 终点 y, 说明)
    links = [
        (0.1225, 0.2125, 0.680, 0.7975, "调用教师"),   # providers → Ollama
        (0.2725, 0.2125, 0.680, 0.6675, "编译"),       # taskpacks → LaTeX
        (0.2725, 0.2125, 0.680, 0.5375, "工具基线"),   # taskpacks → Pandoc
        (0.5725, 0.2125, 0.680, 0.4075, "读基座"),     # training → 基座模型
        (0.5725, 0.2125, 0.680, 0.7975, "注册模型"),   # training → Ollama
        (0.680, 0.2775, 0.640, 0.2125, "读快照"),       # 训练数据 → training
    ]
    for x0, y0, x1, y1, label in links:
        ax.plot([x0, bus_x], [y0, y0], color=WARN, linewidth=1.3, zorder=1)
        ax.plot([bus_x, bus_x], [y0, y1], color=WARN, linewidth=1.3, zorder=1)
        arrow(ax, (bus_x, y1), (x1, y1), color=WARN, style="-", fontsize=8)
        ax.text((bus_x + x1) / 2, y1 + 0.016, label, ha="center", va="center",
                color=WARN, fontsize=8, zorder=4,
                bbox=dict(boxstyle="round,pad=0.15", fc=BG, ec="none", alpha=0.95))

    # 层间箭头
    arrow(ax, (0.32, 0.74), (0.32, 0.69), "REST / JSON + SSE 进度", lx=0.14, ly=0.008)
    arrow(ax, (0.32, 0.585), (0.32, 0.52), "业务调用", lx=0.055, ly=0.0)

    return save(fig, "01-架构全景.png")


# ==========================================================================
# 图 2：一次生成的数据流
# ==========================================================================
def diagram_pipeline():
    fig, ax = new_fig(5.6)
    ax.text(0.5, 0.955, "一次生成：数据从哪来、到哪去", ha="center", va="center",
            color=TEXT, fontsize=16, weight="bold")

    steps = [
        ("你的 Markdown", "原件不可变 + 内容哈希"),
        ("解析成块", "block_id 稳定编号"),
        ("按块生成", "四种模式分开记"),
        ("组装 .tex", "记录行映射"),
        ("隔离编译", "独立目录 · 禁 shell · 超时"),
        ("四层检查", "工程 / 内容 / 版面 / 偏好"),
    ]
    n = len(steps)
    w, gap = 0.125, 0.032
    x0 = 0.030
    y, h = 0.66, 0.17
    centers = []
    for i, (t, s) in enumerate(steps):
        x = x0 + i * (w + gap)
        edge = ACCENT if i in (0, n - 1) else BOX_EDGE
        centers.append(box(ax, x, y, w, h, t, s, edge=edge, title_size=10.5, sub_size=8))
    for a, b in zip(centers, centers[1:], strict=False):
        arrow(ax, (a[0] + w / 2 + 0.004, y + h / 2), (b[0] - w / 2 - 0.004, y + h / 2),
              color=ACCENT, style="-|>")

    # 反馈回路：四层检查 → 用户批注 → 只改那一处 → 回到解析/生成
    fb_y = 0.44
    last_x = centers[-1][0]
    rev_x = centers[1][0]
    ax.plot([last_x, last_x], [y - 0.005, fb_y], color=WARN, linewidth=1.4, zorder=1)
    ax.plot([last_x, rev_x], [fb_y, fb_y], color=WARN, linewidth=1.4, zorder=1)
    arrow(ax, (rev_x, fb_y), (rev_x, y - 0.005), color=WARN)
    ax.text(0.5, fb_y + 0.035,
            "你在 PDF 上圈出“这里不对” → 教师只改这一处 → 重新编译 → 重新检查",
            ha="center", va="center", color=WARN, fontsize=10, zorder=4,
            bbox=dict(boxstyle="round,pad=0.25", fc=BG, ec="none", alpha=0.95))

    # 出口：一条训练样本
    box(ax, 0.26, 0.145, 0.48, 0.155,
              "一条训练样本",
              "输入 Markdown + 完整 .tex + 来源标记（rule / teacher / 人工复核）",
              edge=ACCENT, title_size=12, sub_size=9)
    # 出口连线：折线（竖 → 横 → 下），不走曲线
    out_right = 0.26 + 0.48
    ax.plot([last_x, last_x], [fb_y - 0.005, 0.345], color=ACCENT, linewidth=1.3, zorder=1)
    ax.plot([last_x, out_right - 0.03], [0.345, 0.345], color=ACCENT, linewidth=1.3, zorder=1)
    arrow(ax, (out_right - 0.03, 0.345), (out_right - 0.03, 0.302), color=ACCENT)

    ax.text(0.5, 0.055,
            "四种模式分别记、分别评：工具（Pandoc）的功劳不会被算到模型头上",
            ha="center", va="center", color=MUTED, fontsize=9.5)
    ax.text(0.5, 0.015,
            "样本只有通过检查、并且被你接纳，才会进入训练集",
            ha="center", va="center", color=MUTED, fontsize=9.5)

    return save(fig, "02-生成流水线.png")


# ==========================================================================
# 图 3：M1 训练链
# ==========================================================================
def diagram_training():
    fig, ax = new_fig(5.6)
    ax.text(0.5, 0.95, "M1 训练链：快照 → 训练 → 导出 → 评测 → 激活", ha="center",
            va="center", color=TEXT, fontsize=16, weight="bold")

    stages = [
        ("冻结的快照", "JSONL + 哈希\n加载时强制校验"),
        ("构造样本", "system+user→assistant\n空答案剔除并计数"),
        ("训练", "LoRA / QLoRA\n只从答案算 loss"),
        ("合并 → GGUF", "量化 q4_k_m / q8_0"),
        ("注册 Ollama", "模板与训练格式一致\n回读校验"),
        ("固定开发集评测", "配对执行 · 固定种子\n候选 vs 基线"),
        ("激活 / 回滚", "激活前校验\n沿历史链回退"),
    ]
    n = len(stages)
    w = 0.105
    gap = 0.035
    x_start = 0.027
    y = 0.56
    h = 0.20
    centers = []
    for i, (t, s) in enumerate(stages):
        x = x_start + i * (w + gap)
        edge = ACCENT if i in (0, n - 1) else BOX_EDGE
        centers.append(box(ax, x, y, w, h, t, s, edge=edge, title_size=9.5, sub_size=7.4))
    for a, b in zip(centers, centers[1:], strict=False):
        arrow(ax, (a[0] + w / 2 + 0.004, y + h / 2), (b[0] - w / 2 - 0.004, y + h / 2),
              color=ACCENT, style="-|>")

    # GPU 锁横条
    lock = box(ax, 0.16, 0.34, 0.68, 0.10,
               "GPU 资源锁：训练 / 导出 / 评测 串行使用同一把锁",
               "8GB 显存的卡上不会两个任务同时抢显存",
               edge=WARN, face="#201c14", title_size=11, sub_size=9)
    for c in centers[2:6]:
        arrow(ax, (c[0], y - 0.005), (lock[0], 0.44), color=WARN, dashed=True,
              lx=0.0, ly=0.02, rad=0.0)

    # 三条硬规则
    rules = [
        "快照文件哈希对不上 → 直接报错，不训练",
        "评测固定种子 → 同权重同输入同输出",
        "候选劣于基线 → 不激活，退出码 5",
    ]
    for i, text in enumerate(rules):
        ax.text(0.5, 0.21 - i * 0.055, "· " + text, ha="center", va="center",
                color=MUTED, fontsize=9.8)

    ax.text(0.5, 0.02, "开发机上的基座是随机初始化的小模型 —— 链路验证 ≠ 质量结论",
            ha="center", va="center", color=WARN, fontsize=9.5)

    return save(fig, "03-训练链.png")


# ==========================================================================
# 图 4：设计哲学六原则
# ==========================================================================
def diagram_principles():
    fig, ax = new_fig(4.6)
    ax.text(0.5, 0.94, "六条设计原则", ha="center", va="center",
            color=TEXT, fontsize=16, weight="bold")

    items = [
        ("原件不可变\n一切留痕", "内容哈希 + 版本号\n改过的永远查得到"),
        ("不把工具的\n功劳算到模型头上", "四种模式分开记\n来源逐条标注"),
        ("先验证\n再发布", "四层检查给问题清单\n激活前必须过校验"),
        ("不确定就\n说不确定", "错误信息是真话\n每个比率都带分母"),
        ("数据是资产\n切分防泄漏", "按源分组 + 内容聚类\n同一篇不许跨划分"),
        ("人在环里\n机器不越权", "教师提问等人回答\n控制器只走白名单"),
    ]
    cols = 3
    w, h = 0.27, 0.26
    gx, gy = 0.07, 0.10
    for i, (t, s) in enumerate(items):
        r, c = divmod(i, cols)
        x = 0.04 + c * (w + gx)
        y = 0.50 - r * (h + gy)
        edge = ACCENT if i in (0, 2, 3) else BOX_EDGE
        box(ax, x, y, w, h, t, s, edge=edge, title_size=11.5, sub_size=8.8)

    ax.text(0.5, 0.035,
            "共同点：每一步都留下可以检查、可以回溯的痕迹 —— 而不是给一个好看的分数",
            ha="center", va="center", color=MUTED, fontsize=10)

    return save(fig, "04-设计原则.png")


def main() -> int:
    print("生成架构图：")
    diagram_architecture()
    diagram_pipeline()
    diagram_training()
    diagram_principles()
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
