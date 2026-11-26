# Windows 环境与 LaTeX / 训练排障说明

Godfather 在 **原生 Windows（PowerShell）**、**WSL2 调用 Windows TeX Live** 以及 **原生 Linux** 下均经过完整回归验证。针对 Windows 上最容易遇到的几类环境问题说明如下：

## 1. TeX Live / MiKTeX 环境变量与隔离 (`compiler.py`)

- 原生 Windows 下运行 `xelatex.exe`（或通过 WSL 桥接调用 `xelatex.exe`），系统目录列表 **`TEXMFCNF` 必须用分号 `;` 拼接**（POSIX 用冒号 `:`），且必须保留 `USERPROFILE`、`LOCALAPPDATA`、`APPDATA`、`TEMP`、`TMP` 等系统路径变量（同时严禁写入代理环境变量以免被网络劫持），否则 MiKTeX/字体缓存会直接报错崩溃。
- 若使用 **MiKTeX** 且首次编译文档提示某个宏包 `.sty` 找不到，请打开 **MiKTeX Console → Settings → General**，将 *Always install missing packages on-the-fly* 设为开启，因为非交互后台参数 `-interaction=nonstopmode` 无法响应弹窗。
- 中文模板 `config/templates/preamble_zh_ctex.tex` 在 `fontset: auto` 下：Windows 优先走 `ctex` 自带的 `windows` 字体集（SimSun/SimHei 等），POSIX 自动使用 TeX Live 内置的 `fandol`。

## 2. 本地微调子进程环境 (`training/base.py`)

- Windows 子进程默认可能出现 GBK 编码与 Socket 初始化错误（如 `import torch` 时报 `WinError 10106` 或 UTF-8 读取报错）。
- `TrainingBackend.env_for_run()` 已强制注入 `PYTHONUTF8=1`，并显式透传 `SystemRoot`、`WINDIR`、`COMSPEC`、`USERPROFILE`、`TEMP`、`CUDA_VISIBLE_DEVICES` 等环境变量。

## 3. llama.cpp 新版目录与工具查找 (`training/export.py`)

- `llama.cpp` 新版本（v1.0+ 以后）把 Python 转换脚本从根目录调整或与 CMake 构建目录 `build/bin/Release` 并存，Windows 下请确认配置中的 `export.llama_cpp_dir` 指向含 `convert_hf_to_gguf.py` 且已编译出 `llama-quantize.exe` 的仓库目录（可在浏览器 **`模型接入中心`** 页直接修改保存）。
