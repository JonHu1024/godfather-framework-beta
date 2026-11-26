"""受控 LaTeX 模板。

导言区由确定性组装器统一管理（教师只负责正文片段），符合设计文档 §5.3 第 5 步。
用户可在 config/templates/ 下放置同名文件覆盖内置模板。
"""
from __future__ import annotations

from pathlib import Path

# 中文文档（XeLaTeX + ctex）
PREAMBLE_ZH_CTEX = r"""% ===== Godfather 自动生成导言区（请勿手改；改策略请改 policy.yaml） =====
\documentclass[UTF8,a4paper,11pt,fontset=__FONTSET__]{ctexart}
\usepackage{fontspec}
% 中文与等宽字体：按顺序选取第一个可用的字体（使用 ctex 字体集时此处为空）
__CJK_FONTS__
\usepackage{geometry}
\geometry{a4paper,margin=2.4cm}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{array}
\usepackage{caption}
\usepackage{hyperref}
\hypersetup{colorlinks=true,linkcolor=blue,urlcolor=blue}
\usepackage{enumitem}
\usepackage{fancyvrb}
\IfFileExists{fvextra.sty}{\usepackage{fvextra}\fvset{breaklines=true,breakanywhere=true}}{}
\fvset{fontsize=\small}
% Pandoc 输出的紧凑列表与图片宏需要在此定义
\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}
\providecommand{\pandocbounded}[1]{#1}
\providecommand{\passthrough}[1]{#1}
\setcounter{secnumdepth}{3}
\setcounter{tocdepth}{2}
% ===== 导言区结束 =====
"""

# 通用模板（无 ctex 依赖；中文需要系统已安装中文字体时改用 fontspec 直接配置）
PREAMBLE_PLAIN = r"""% ===== Godfather 自动生成导言区（无 ctex） =====
\documentclass[a4paper,11pt]{article}
\usepackage{fontspec}
\usepackage{geometry}
\geometry{a4paper,margin=2.4cm}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{array}
\usepackage{hyperref}
\usepackage{enumitem}
\usepackage{fancyvrb}
\IfFileExists{fvextra.sty}{\usepackage{fvextra}\fvset{breaklines=true,breakanywhere=true}}{}
\fvset{fontsize=\small}
\setcounter{secnumdepth}{3}
% ===== 导言区结束 =====
"""

# 封面/标题模板片段
TITLE_TEMPLATE = r"""\begin{document}
\begin{center}
{\LARGE\bfseries __TITLE__\par}
\vspace{0.6em}
__SUBTITLE__
\end{center}
\vspace{1em}
"""

DEFAULT_TEMPLATES = {
    "preamble_zh_ctex": PREAMBLE_ZH_CTEX,
    "preamble_plain": PREAMBLE_PLAIN,
    "title": TITLE_TEMPLATE,
}


def load_template(name: str, overrides_dir: str | Path | None = None) -> str:
    if overrides_dir:
        candidate = Path(overrides_dir) / name
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return DEFAULT_TEMPLATES.get(name, "")
