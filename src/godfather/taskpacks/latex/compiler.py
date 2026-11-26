"""隔离 LaTeX 编译（Windows / Linux 均可）。

安全措施（设计文档 §11）：
- 禁用 shell escape（`-no-shell-escape`）；
- 在独立临时目录中编译，只复制本次任务需要的文件；
- 无网络环境变量（POSIX 清除代理变量；Windows 不注入代理）；
- 资源限制：POSIX 用 rlimit 限制 CPU/内存/文件大小；Windows 用进程组 + 强制终止树；
- 超时即终止并标记失败，不允许无限自修复。

诚实说明：Windows 缺少 rlimit 等价物，严格隔离建议使用 WSL2 / Linux worker，
详见 docs/06-windows-latex.md。
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt"


@dataclass
class CompileResult:
    ok: bool
    engine: str
    binary: str = ""
    exit_code: int = -1
    log: str = ""
    log_path: str = ""
    pdf_path: str = ""
    aux_path: str = ""
    synctex_path: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    work_dir: str = ""
    engine_found: bool = True

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            # 日志必须带上：四层校验里的工程层与版面层都靠它
            # （未定义引用、缺文件、overfull 框……）。
            # 以前这里没有 log，两个检查层等于从来没跑过。
            "log": self.log,
            "engine": self.engine,
            "binary": self.binary,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "error": self.error,
            "warnings": self.warnings,
            "log_path": self.log_path,
            "pdf_path": self.pdf_path,
            "synctex_path": self.synctex_path,
            "work_dir": self.work_dir,
            "engine_found": self.engine_found,
        }


def detect_engine(settings: Any) -> tuple[str | None, str]:
    """返回 (可执行文件路径, 说明)。找不到返回 (None, 原因)。

    在 WSL/Linux 上也会尝试 `xelatex.exe`：Windows 的 TeX Live / MiKTeX
    通常已挂在 PATH 里，可以直接调用（工作目录需要放在 Windows 可访问的位置）。
    """
    cfg = settings.latex
    candidates: list[str] = []
    if cfg.binary:
        candidates.append(cfg.binary)
    else:
        candidates.extend(cfg.candidates or [])
        if cfg.engine:
            candidates.insert(0, cfg.engine)

    tried: list[str] = []
    for candidate in list(candidates):
        if not candidate:
            continue
        tried.append(candidate)
        # .exe / .bat / .cmd 也要试：
        # 用户在 config 里配的往往就是完整 Windows 路径
        # （"C:/Program Files/MiKTeX/.../xelatex.exe"），
        # 以前这类候选被整段跳过 → 永远找不到引擎，而且连"试过什么"都不报。
        resolved = _resolve_executable(candidate)
        if resolved:
            return resolved, f"使用 LaTeX 引擎：{resolved}"
        if not candidate.lower().endswith((".exe", ".bat", ".cmd")):
            # WSL interop：Windows 侧的同名可执行文件
            tried.append(f"{candidate}.exe")
            resolved = _resolve_executable(f"{candidate}.exe")
            if resolved:
                return resolved, f"使用 Windows 侧 LaTeX 引擎：{resolved}（通过 WSL 调用）"
    return None, (
        "未找到 LaTeX 引擎。已尝试：" + ", ".join(tried or ["(无)"]) +
        "。请安装 MiKTeX / TeX Live，或在 config/config.yaml 的 latex.binary 中指定完整路径。"
    )


def _wsl_mount_path(candidate: str) -> str | None:
    """把 Windows 风格路径映射到 WSL 的挂载点（/mnt/c/...）。

    用户在 config 里配的通常是 Windows 路径
    （"C:/Program Files/MiKTeX/.../xelatex.exe"），
    在 WSL 下这个路径**不存在**，必须映射到 /mnt/<盘符>/ 才找得到。
    """
    text = str(candidate).replace("\\", "/")
    if len(text) < 3 or text[1] != ":" or not text[0].isalpha():
        return None
    drive = text[0].lower()
    rest = text[2:].lstrip("/")
    return f"/mnt/{drive}/{rest}"


def _resolve_executable(candidate: str) -> str | None:
    path = Path(candidate)
    if path.is_absolute() or path.exists():
        if path.is_file():
            return str(path)
        return None
    found = shutil.which(candidate)
    if found:
        return found
    # WSL：Windows 路径 → /mnt/<盘符>/…
    mapped = _wsl_mount_path(candidate)
    if mapped:
        mapped_path = Path(mapped)
        if mapped_path.is_file():
            return str(mapped_path)
    return None


def engine_version(binary: str, timeout: int = 15) -> str:
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (proc.stdout or proc.stderr) else ""
    except Exception:
        return ""


# LaTeX 的中间文件后缀（只删这些，不动调用方放在同目录的其它文件）
LATEX_INTERMEDIATE_SUFFIXES = (
    ".aux", ".fls", ".out", ".toc", ".lof", ".lot", ".bbl", ".blg", ".idx", ".ind",
    ".glo", ".gls", ".xdv", ".nav", ".snm", ".vrb", ".bcf", ".run.xml", ".fdb_latexmk",
    ".xdv.log", ".bbl.log",
)


def _cleanup_intermediates(work_dir: Path) -> None:
    """删除 LaTeX 的中间文件（只删已知的中间产物后缀）。"""
    try:
        for path in work_dir.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if name in {"main.tex", "main.pdf", "main.log"}:
                continue
            if any(name.endswith(suffix) for suffix in LATEX_INTERMEDIATE_SUFFIXES):
                path.unlink()
    except OSError:
        return


def _apply_limits(pid: int, cpu_seconds: int, memory_mb: int, file_mb: int) -> None:
    """给已经启动的子进程施加资源上限。

    不用 preexec_fn：多线程 worker 里 fork 后重建锁可能死锁（CPython 明确不保证）。
    这里在父进程侧用 prlimit 施加（Linux），失败不影响编译，只是少一层限制。
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return
    if not hasattr(resource, "prlimit"):
        return
    limits = [
        (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5)),
        (resource.RLIMIT_FSIZE, (file_mb * 1024 * 1024, file_mb * 1024 * 1024)),
        (resource.RLIMIT_NOFILE, (256, 256)),
        (resource.RLIMIT_CORE, (0, 0)),
    ]
    try:
        limits.append((resource.RLIMIT_AS, (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024)))
    except (ValueError, AttributeError):
        pass
    for res, value in limits:
        try:
            resource.prlimit(pid, res, value)
        except (ValueError, OSError, PermissionError):
            continue


# 单个路径片段的合法字符：允许中文等 Unicode 字母（中文命名的图片很常见），
# 但拒绝控制字符与 Windows 保留字符。
UNSAFE_ASSET_CHARS = set('<>:"\'|?*') | {chr(c) for c in range(32)}


def sanitize_asset_path(name: str) -> str | None:
    """把素材相对路径规整到编译目录内。

    拒绝绝对路径、`..` 穿越、以及 Windows 保留字符；
    返回 None 表示该路径不可用（调用方应记录为"缺资源"而不是写入目录外）。
    """
    if not name:
        return None
    normalized = name.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return None
    parts = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        if any(char in UNSAFE_ASSET_CHARS for char in part):
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)



# 由 Godfather 生成的 texmf.cnf 内容。
#
# 这是让 openin_any / openout_any / shell_escape 真正生效的唯一途径
# （kpathsea 只从 texmf.cnf 读它们，设环境变量无效 —— 实测：
#  环境变量设 p/r/a，kpsewhich --var-value openin_any 始终是 texmf.cnf 里的值）。
#
# 关键点：**不能只用自己的 cnf 替换系统的**。
# kpathsea 会把 TEXMFCNF 列表里每个目录下的 texmf.cnf 都读一遍，
# 我们的只覆盖 openin_any / openout_any / shell_escape 三个键，
# TEXMFDIST 等仍来自系统那份 —— 只放一份 3 行的 cnf 会让
# ctexart.cls、xelatex.fmt 全都找不到（实测："I can't find the format file"）。
TEXMF_CNF_CONTENT = (
    "# 由 Godfather 生成：限制 TeX 的文件访问范围\n"
    "# openin_any = p（paranoid）：禁止读取当前目录之外的文件。\n"
    "# 实测：ctex/fontspec、多遍编译的 .aux、includegraphics 都照常工作，\n"
    "#       但 openin / input 读任意绝对路径会被挡住。\n"
    "# （以前用 restricted(r)：实测挡不住绝对路径读取。）\n"
    "openin_any = p\n"
    "openout_any = p\n"
    "shell_escape = f\n"
)

# 查系统 TEXMFCNF 列表的结果缓存（查一次就够）
_SYSTEM_TEXMFCNF_CACHE: dict[str, str] = {}


def _system_texmfcnf(binary: str, windows_engine: bool) -> str:
    """查系统自己的 TEXMFCNF 目录列表。

    我们自己的 cnf 要**前置**到这个列表里：
    这样我们的 openin_any=p 生效，而 TEXMFDIST 等仍由系统那份提供。
    """
    key = f"{binary}|{windows_engine}"
    if key in _SYSTEM_TEXMFCNF_CACHE:
        return _SYSTEM_TEXMFCNF_CACHE[key]
    value = ""
    try:
        if windows_engine:
            # Windows 侧：环境变量传不过 WSL 边界（实测 48/49 个变量丢失），
            # 但 stdout 可以 —— 通过 PowerShell 调用同目录的 kpsewhich.exe。
            kpsewhich = str(Path(binary).with_name("kpsewhich.exe"))
            win_kpsewhich = windows_path_for(Path(kpsewhich))
            output = _run_capture(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    f"& '{win_kpsewhich}' --var-value TEXMFCNF",
                ],
                timeout=30,
            )
        else:
            kpse_name = "kpsewhich.exe" if IS_WINDOWS else "kpsewhich"
            kpsewhich = shutil.which(kpse_name) or str(Path(binary).with_name(kpse_name))
            output = _run_capture([kpsewhich, "--var-value", "TEXMFCNF"], timeout=30)
        value = (output or "").strip()
    except Exception:  # noqa: BLE001
        value = ""
    _SYSTEM_TEXMFCNF_CACHE[key] = value
    return value


def _ps_quote(value: str) -> str:
    """PowerShell 单引号字符串（内部单引号要写两遍）。"""
    return "'" + str(value).replace("'", "''") + "'"


def _powershell_command(args: list[str], env: dict[str, str]) -> list[str]:
    r"""把一条 Windows 命令包成 PowerShell 调用，并在其中设置环境变量。

    用法：`powershell.exe -NoProfile -Command "$env:X='1'; & 'C:\...\xelatex.exe' 'arg1'"`

    为什么必须这么做：WSL interop 不传环境变量
    （实测 `MYVAR=abc powershell.exe -c '$env:MYVAR'` 得到空），
    只有 WSLENV 里列出的变量才过界，而我们的 env 是自己拼的。
    """
    if not args:
        return args
    binary = args[0]
    rest = args[1:]
    parts = [
        f"$env:{key}={_ps_quote(value)}"
        for key, value in (env or {}).items()
        if value not in (None, "")
    ]
    binary_win = binary
    try:
        binary_win = windows_path_for(Path(binary))
    except Exception:  # noqa: BLE001
        binary_win = binary
    quoted = " ".join(_ps_quote(arg) for arg in rest)
    command = "; ".join(parts + [f"& {_ps_quote(binary_win)} {quoted}".rstrip()])
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]


def _build_environment(
    work_dir: Path,
    windows_engine: bool = False,
    texmfcnf_dir: str = "",
    system_texmfcnf: str = "",
) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "SOURCE_DATE_EPOCH": "1700000000",
        "FORCE_SOURCE_DATE": "1",
        # openin_any / openout_any 只认 texmf.cnf，环境变量里写它们**无效**。
        # 真正的限制由 TEXMFCNF 指向的 cnf 文件提供（见 TEXMF_CNF_CONTENT）。
        "max_print_line": "1000",
        "error_line": "254",
        "half_error_line": "238",
    }
    if IS_WINDOWS or windows_engine:
        # 原生 Windows 下保留 TeX Live / MiKTeX / 字体缓存所需的系统与用户目录变量（不传入代理）
        for win_key in (
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "TEMP",
            "TMP",
            "HOMEDRIVE",
            "HOMEPATH",
            "ProgramData",
            "SystemDrive",
            "PUBLIC",
        ):
            val = os.environ.get(win_key, "")
            if val:
                env[win_key] = val
    # TEXMFCNF 是**目录列表**（不是文件路径！kpathsea 会往里拼 texmf.cnf）：
    # 我们的目录在前（覆盖 openin_any 等），系统的目录在后（提供 TEXMFDIST 等）。
    if texmfcnf_dir:
        separator = ";" if (windows_engine or IS_WINDOWS) else ":"
        env["TEXMFCNF"] = (
            f"{texmfcnf_dir}{separator}{system_texmfcnf}"
            if system_texmfcnf
            else str(texmfcnf_dir)
        )
    if not windows_engine and not IS_WINDOWS:
        # 仅在原生 POSIX 编译下把 TeX 用户目录限制在沙箱内；Windows 上覆盖 HOME/TEXMFVAR 会破坏系统字体与 MiKTeX 配置
        env.update(
            {
                "TEXMFCONFIG": str(work_dir / "texmf-config"),
                "TEXMFVAR": str(work_dir / "texmf-var"),
                "TEXMFCACHE": str(work_dir / "texmf-cache"),
                "HOME": str(work_dir / "home"),
            }
        )
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    if not IS_WINDOWS:
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
    return {k: v for k, v in env.items() if v}


# --------------------------------------------------------------------------
# WSL → Windows 引擎桥接
# --------------------------------------------------------------------------
def is_windows_binary(path: str) -> bool:
    return path.lower().endswith((".exe", ".bat", ".cmd"))


def _log_bridge_error(message: str) -> None:
    """桥接（WSL↔Windows）失败要留下痕迹。

    这些错误以前全被吞掉，用户只看到"编译失败"，无从排查。
    """
    import logging

    logging.getLogger("godfather.latex").warning("Windows 桥接：%s", message)


def _run_capture(command: list[str], timeout: int = 20) -> str:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def windows_path_for(linux_path: Path) -> str:
    """把 Linux 路径转成 Windows 路径（需要 wslpath）。"""
    result = _run_capture(["wslpath", "-w", str(linux_path)])
    return result or str(linux_path)


def windows_temp_dir() -> Path | None:
    """取 Windows 侧 TEMP 目录（返回 Linux 侧可访问的路径）。"""
    raw = _run_capture(["cmd.exe", "/c", "echo %TEMP%"])
    if not raw:
        return None
    linux = _run_capture(["wslpath", "-u", raw.strip()])
    if linux:
        candidate = Path(linux.strip())
        if candidate.exists():
            return candidate
    return None


def windows_copy_file(src_linux: Path, dst_windows_dir: str) -> bool:
    """把 Linux 侧文件复制到 Windows 目录（由 Windows 进程执行，避免 UNC 路径被 TeX 误解析）。

    注意：PowerShell 的 Copy-Item 失败是**非终止错误** ——
    错误写到 stderr、returncode 仍是 0、`; 'OK'` 照样打印。
    只看 stdout 里的 "OK" 会谎报成功（实测：复制失败后编译报
    "I can't find file 'main.tex'"，完全看不出是复制没成功）。
    所以这里要：$ErrorActionPreference='Stop' + 检查 returncode + 检查 stderr。
    """
    src_windows = windows_path_for(src_linux).replace("'", "''")
    dst = dst_windows_dir.replace("'", "''")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        (
            "$ErrorActionPreference='Stop'; "
            f"Copy-Item -LiteralPath '{src_windows}' -Destination '{dst}' -Force; "
            "'OK'"
        ),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        _log_bridge_error(f"复制到 Windows 目录失败（{src_linux}）：{exc}")
        return False
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0 or stderr:
        _log_bridge_error(
            f"复制到 Windows 目录失败（{src_linux} → {dst}）："
            f"returncode={proc.returncode} {stderr[:400]}"
        )
        return False
    return "OK" in stdout


SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def safe_job_id(job_id: str) -> str:
    """job_id 会进入目录名与 PowerShell 命令，必须白名单化。"""
    if not SAFE_JOB_ID_RE.match(job_id or ""):
        return "job-" + hashlib.sha256((job_id or "").encode("utf-8")).hexdigest()[:12]
    return job_id


def windows_compile_dirs(job_id: str) -> tuple[str, Path] | None:
    """为 WSL→Windows 编译准备输出目录。

    返回 (Windows 路径, Linux 侧可见路径)；失败返回 None。
    目录由 Windows 侧创建（Linux 侧往往对 C: 盘只读）。
    """
    raw = _run_capture(["cmd.exe", "/c", "echo %TEMP%"])
    if not raw:
        return None
    windows_dir = f"{raw.strip().rstrip(chr(92))}\\godfather-compile\\{safe_job_id(job_id)}"
    created = _run_capture(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"New-Item -ItemType Directory -Force -Path '{windows_dir}' | Out-Null; "
            f"if (Test-Path '{windows_dir}') {{ 'OK' }}",
        ],
        timeout=40,
    )
    if "OK" not in created:
        _run_capture(["cmd.exe", "/c", f"mkdir {windows_dir.replace(chr(92), '/')}"])
    linux = _run_capture(["wslpath", "-u", windows_dir])
    if not linux:
        return None
    linux_path = Path(linux.strip())
    if not linux_path.exists():
        return None
    return windows_dir, linux_path


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
        else:
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = None
            if pgid and pgid == proc.pid:
                os.killpg(pgid, 9)
            else:
                # 拿不到独立进程组时只杀自己，避免连宿主进程组一起带走
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def compile_tex(
    tex_source: str,
    *,
    settings: Any,
    work_root: Path,
    job_id: str = "manual",
    assets: dict[str, bytes] | None = None,
    main_filename: str = "main.tex",
) -> CompileResult:
    """在隔离目录中编译 LaTeX 源码。"""
    cfg = settings.latex
    binary, message = detect_engine(settings)
    if not binary:
        return CompileResult(
            ok=False,
            engine=cfg.engine,
            exit_code=-2,
            error=message,
            engine_found=False,
        )

    # Windows 侧的引擎（例如在 WSL 中调用 Windows 的 TeX Live）：
    #   源码放在 Linux 工作目录（Windows 通过 UNC 读取），
    #   输出写到 Windows 侧临时目录（Windows 进程才能写），再从 Linux 侧读回。
    windows_engine = (not IS_WINDOWS) and is_windows_binary(binary)
    output_dir_arg = ""
    output_dir_linux: Path | None = None
    cwd_dir: Path | None = None
    input_arg = main_filename
    pending_copies: list[tuple[Path, str]] = []

    job_id = safe_job_id(job_id)
    if windows_engine:
        bridge = windows_compile_dirs(job_id)
        if bridge:
            output_dir_arg, output_dir_linux = bridge
            cwd_dir = output_dir_linux
            input_arg = main_filename
            # 源码与资源先复制到 Windows 目录：TeX 不能直接读取 UNC 路径
            pending_copies = [(Path(work_root) / "compile" / job_id / main_filename, output_dir_arg)]
        else:
            # 桥接不可用（没有可用的 %TEMP% 或 PowerShell）：
            # 退化方案必须把**源码也复制过去**，否则 Windows 引擎在 Linux 目录里
            # 根本找不到 main.tex，报出来的错会让人摸不着头脑。
            output_dir_arg = windows_path_for(Path(work_root) / "compile" / job_id)
            pending_copies = [(Path(work_root) / "compile" / job_id / main_filename, output_dir_arg)]

    work_dir = Path(work_root).resolve() / "compile" / job_id
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "home").mkdir(exist_ok=True)
    (work_dir / "texmf-var").mkdir(exist_ok=True)

    tex_path = work_dir / main_filename
    tex_path.write_text(tex_source, encoding="utf-8")

    rejected_assets: list[str] = []
    for name, payload in (assets or {}).items():
        safe_name = sanitize_asset_path(name)
        if safe_name is None:
            rejected_assets.append(name)
            continue
        target = work_dir / safe_name
        # 双重保险：确认解析后的路径仍在编译目录内
        try:
            target.resolve().relative_to(work_dir.resolve())
        except ValueError:
            rejected_assets.append(name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    # ---- 隔离配置（texmf.cnf）----
    # 必须在**复制循环之前**准备好：Windows 侧读不到 Linux/UNC 上的文件，
    # 读不到就会静默退回系统 texmf.cnf（openin_any=a）→ 形同没设。
    if windows_engine and output_dir_arg:
        # 放在 Windows 侧编译输出目录里（已经是 Windows 能访问的目录）
        cnf_dir_windows = output_dir_arg
        cnf_source = work_dir / "texmf.cnf"
        cnf_source.write_text(TEXMF_CNF_CONTENT, encoding="utf-8")
        pending_copies.append((cnf_source, output_dir_arg))
    else:
        cnf_dir = work_dir / "texmf-cnf"
        cnf_dir.mkdir(parents=True, exist_ok=True)
        cnf_source = cnf_dir / "texmf.cnf"
        cnf_source.write_text(TEXMF_CNF_CONTENT, encoding="utf-8")
        cnf_dir_windows = str(cnf_dir)

    if pending_copies:
        for source, windows_dir in pending_copies:
            if not windows_copy_file(source, windows_dir):
                # 早期返回也要带上素材消毒的告警：
                # 素材落盘发生在编译之前，那些结论不该因为编译没跑起来就丢掉。
                warnings = []
                if rejected_assets:
                    warnings.append(
                        "以下素材路径不安全或越界，已忽略：" + ", ".join(rejected_assets[:10])
                    )
                return CompileResult(
                    ok=False,
                    engine=cfg.engine,
                    binary=binary,
                    exit_code=-4,
                    error=f"无法把源文件复制到 Windows 编译目录：{source}",
                    warnings=warnings,
                    work_dir=str(work_dir),
                )
        for name in (assets or {}):
            safe_name = sanitize_asset_path(name)
            if safe_name is None:
                continue
            source = work_dir / safe_name
            if not source.exists():
                continue
            relative = Path(safe_name)
            if relative.parent != Path("."):
                subdir = f"{output_dir_arg}\\{relative.parent}".replace("/", "\\")
                _run_capture(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-Command",
                        f"New-Item -ItemType Directory -Force -Path '{subdir}' | Out-Null; 'OK'",
                    ],
                    timeout=40,
                )
                windows_copy_file(source, subdir)
            else:
                windows_copy_file(source, output_dir_arg)

    args = [
        binary,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        f"-output-directory={output_dir_arg or work_dir}",
    ]
    # 恒定禁用 shell escape：这是硬约束，不能由配置开关决定
    # （配置里写 false 会让 \write18 变成任意命令执行）。
    args.append("-no-shell-escape")
    args.append("-synctex=1")
    args.append(input_arg)

    env = _build_environment(
        work_dir,
        windows_engine=windows_engine,
        texmfcnf_dir=cnf_dir_windows,
        system_texmfcnf=_system_texmfcnf(binary, windows_engine),
    )
    timeout = int(cfg.timeout_seconds)
    cpu_seconds = max(30, timeout)
    memory_mb = 4096
    file_mb = 256

    # ---- Windows 引擎：命令与��境变量都要在 Windows 侧解决 ----
    #
    # WSL interop **不传环境变量**（实测：WSL 的 49 个变量里 48 个在 Windows 侧不可见，
    # 只有 WSLENV 里列出的才过��）→ 直接把 xelatex.exe 跑起来的话，
    # 我们设的 TEXMFCNF / max_print_line / SOURCE_DATE_EPOCH 全都到不了，
    # 隔离与可诊断性一起失效。
    # 所以 Windows 路径改成：用 PowerShell 执行，并在 **PowerShell 进程里**设好变量。
    if windows_engine:
        # 变量写进 PowerShell 命令里（子进程 env 传不过 WSL 边界）。
        # 子进程 env 仍然保留：PATH 要用来找到 powershell.exe 本身。
        args = _powershell_command(args, env)

    kwargs: dict[str, Any] = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        # 不使用 preexec_fn：多线程 worker 里 fork 后重建锁可能死锁。
        # 资源限制改为在子进程启动后用 prlimit 施加（见 _apply_limits）。
        kwargs["start_new_session"] = True

    started = time.time()
    timed_out = False
    error = ""
    exit_code = -1
    stdout = ""
    # 多遍编译的每一遍日志都要留：验证层（未定义引用、缺文件、overfull）
    # 是基于日志判断的，只留最后一遍会漏判。
    pass_logs: list[str] = []
    # 多遍编译：交叉引用（\ref）与目录需要第二遍才能解析出来。
    # 配置里的 passes 以前只是摆设（实际只编一遍）。
    passes = max(1, min(int(getattr(cfg, "passes", 1) or 1), 5))
    for _pass_index in range(passes):
        if IS_WINDOWS:
            from . import win_sandbox

            try:
                sb_res = win_sandbox.run_sandboxed(
                    args,
                    work_dir=work_dir,
                    env=env,
                    timeout_seconds=timeout,
                    job_label=f"{job_id}-p{_pass_index + 1}",
                )
            except win_sandbox.SandboxInitError as exc:
                return CompileResult(
                    ok=False,
                    engine=cfg.engine,
                    binary=binary,
                    exit_code=-5,
                    error=f"Windows 编译隔离环境初始化失败：{exc}",
                    work_dir=str(work_dir),
                )
            stdout = sb_res.stdout or ""
            exit_code = sb_res.exit_code
            if sb_res.timed_out:
                timed_out = True
                error = sb_res.error or f"编译超时（>{timeout}s），已终止沙箱进程树"
            elif sb_res.error and not error:
                error = sb_res.error
            pass_logs.append(stdout)
            if timed_out or exit_code != 0:
                break
            continue

        try:
            proc = subprocess.Popen(
                args,
                cwd=str(cwd_dir or work_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **kwargs,
            )
            _apply_limits(proc.pid, cpu_seconds, memory_mb, file_mb)
            try:
                stdout, _ = proc.communicate(timeout=timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(proc)
                try:
                    stdout, _ = proc.communicate(timeout=10)
                except Exception:
                    stdout = ""
                exit_code = proc.returncode if proc.returncode is not None else -9
                error = f"编译超时（>{timeout}s），已终止进程树"
            pass_logs.append(stdout or "")
            if timed_out:
                break
            # 第一遍失败就不用再编了
            if exit_code != 0:
                break
        except FileNotFoundError as exc:
            return CompileResult(
                ok=False,
                engine=cfg.engine,
                binary=binary,
                exit_code=-3,
                error=f"无法启动编译器：{exc}",
                work_dir=str(work_dir),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            break

    duration_ms = int((time.time() - started) * 1000)
    # 每遍的日志都要保留（验证层依赖它）
    log_text = "\n".join(part for part in pass_logs if part) or (stdout or "")
    log_path = work_dir / "main.log"
    try:
        log_path.write_text(log_text, encoding="utf-8")
    except OSError:
        pass
    # 产物路径要在清理中间文件**之前**记下来：
    # .synctex.gz 以前被当中间文件删掉，synctex_path 就恒为空串。
    result_dir = output_dir_linux or work_dir
    pdf_path = result_dir / "main.pdf"
    aux_path = result_dir / "main.aux"
    synctex_path = result_dir / "main.synctex.gz"

    # 清理中间文件：.aux/.log/.fls 等会一直堆积，占满磁盘
    _cleanup_intermediates(work_dir)

    warnings: list[str] = []
    if rejected_assets:
        warnings.append(
            "以下素材路径不安全或越界，已忽略："
            + ", ".join(rejected_assets[:10])
        )
    if not error:
        error = _extract_tex_error(log_text)

    ok = bool(not timed_out and exit_code == 0 and pdf_path.exists())
    if not pdf_path.exists() and ok:
        ok = False
        error = error or "编译器报告成功但未生成 PDF"

    return CompileResult(
        ok=ok,
        engine=cfg.engine,
        binary=binary,
        exit_code=exit_code,
        log=log_text[-200000:],
        log_path=str(log_path),
        pdf_path=str(pdf_path) if pdf_path.exists() else "",
        aux_path=str(aux_path) if aux_path.exists() else "",
        synctex_path=str(synctex_path) if synctex_path.exists() else "",
        duration_ms=duration_ms,
        timed_out=timed_out,
        error=error,
        warnings=warnings,
        work_dir=str(work_dir),
    )


ERROR_PATTERNS = (
    "! Undefined control sequence",
    "! LaTeX Error",
    "! Package .* Error",
    "Emergency stop",
    "Fatal error occurred",
    "! File .* not found",
    "!pdfTeX error",
)


# 编译时传了 -file-line-error，TeX 的错误会变成
#   main.tex:12: Undefined control sequence.
# 这种格式（没有 "!" 前缀）。只认 "^!" 会把最常见的 TeX 错误
# （未定义命令、Missing $ inserted、Misplaced alignment tab……）全部丢掉，
# 用户只看到"编译失败（退出码 1）"，完全不知道原因。
_FILE_LINE_ERROR_RE = re.compile(r"^[^\s:]+\.(?:tex|sty|cls)(?:\(\d+\))?:\s*\S")
# 已知是致命错误的措辞（TeX 级错误信息）
# 致命错误的措辞（TeX 级错误信息）。
# 注意：**不能**包含笼统的 "not found" ——
# ctex 的良性警告（"Package ctex Warning: Fontset 'xxx' not found, using 'fandol'"）
# 会��它命中，把一次成功编译误报成失败。
_FATAL_PHRASES = (
    "Emergency stop",
    "Fatal error occurred",
    "Undefined control sequence",
    "Missing $ inserted",
    "Misplaced alignment tab",
    "Extra alignment tab",
    "not found",          # 只在"行首是错误"时才算致命（见 _looks_fatal）
)

# 这些措辞本身是良性警告，不算致命错误
_BENIGN_PHRASES = (
    "Warning:",
    "Fontset",
    "using",
)


def _looks_fatal(line: str) -> bool:
    """判断一行日志是不是致命错误。

    良性警告（ctex 的 "Fontset 'xxx' not found, using 'fandol'"）不能算错误：
    笼统地匹配 "not found" 会把一次成功的编译误报成失败。
    """
    if not any(phrase in line for phrase in _FATAL_PHRASES):
        return False
    if "not found" in line and not any(
        phrase in line for phrase in _FATAL_PHRASES if phrase != "not found"
    ):
        # 只有 "not found" 命中：必须排除明显的警告形态
        if "Warning:" in line or "Fontset" in line:
            return False
        # "! LaTeX Error: File `xxx.sty' not found." 这类是真正的错误
        if "Error:" in line or line.lstrip().startswith("!"):
            return True
        return False
    return True


def _extract_tex_error(log: str) -> str:
    if not log:
        return ""
    lines = log.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("!"):
            snippet = [line]
            for follow in lines[index + 1 : index + 6]:
                snippet.append(follow)
                if follow.startswith("l."):
                    break
            return "\n".join(snippet).strip()
    # -file-line-error 格式
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not _FILE_LINE_ERROR_RE.match(stripped):
            continue
        if not _looks_fatal(stripped):
            continue
        snippet = [stripped]
        for follow in lines[index + 1 : index + 4]:
            snippet.append(follow)
        return "\n".join(snippet).strip()
    # 兜底：整段日志里找第一行致命措辞
    # （同样要排除良性警告：ctex 的 "Fontset 'xxx' not found" 不是错误）
    for line in lines:
        if _looks_fatal(line):
            return line.strip()
    return ""


def render_page_images(pdf_path: str | Path, out_dir: Path, dpi: int = 110) -> list[str]:
    """可选：用 pdftoppm 生成页面 PNG（未安装 poppler 时返回空列表）。"""
    binary = shutil.which("pdftoppm")
    if not binary:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [binary, "-png", "-r", str(dpi), str(pdf_path), str(out_dir / "page")],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )
    except Exception:
        return []
    return sorted(str(p) for p in out_dir.glob("page-*.png"))


def platform_info() -> dict:
    return {
        "os": platform.system(),
        "release": platform.release(),
        "python": sys.version.split()[0],
        "is_windows": IS_WINDOWS,
        "pdftoppm": shutil.which("pdftoppm") or "",
        "pandoc": shutil.which("pandoc") or "",
    }


# --------------------------------------------------------------------------
# 字体方案
# --------------------------------------------------------------------------
def resolve_fontset(settings: Any) -> str:
    r"""决定使用哪个 ctex 字体集。

    - ``auto``：Windows 上用 ctex 的 windows 字体集（SimSun/SimHei…），
      其它平台用 TeX Live 自带的 fandol（最稳，不依赖系统字体）。
    - ``manual``：由用户指定字体名，用 ``\IfFontExistsTF`` 回退链。
    """
    plan = (getattr(settings.latex, "fontset", "auto") or "auto").strip()
    if plan.lower() != "auto":
        return plan
    binary, _message = detect_engine(settings)
    windows_side = IS_WINDOWS or (binary and is_windows_binary(binary))
    return "windows" if windows_side else "fandol"


def probe_fonts(settings: Any, work_root: Path | None = None) -> dict:
    """编译一个最小中文文档，验证字体是否可用。

    某些 CJK 字体在 xdvipdfmx 下会报 "Invalid font: -1"，
    这个探针能在预检阶段就发现，而不是等到正式编译才失败。
    """
    fontset = resolve_fontset(settings)
    root = Path(work_root or settings.work_dir)
    source = (
        "\\documentclass[UTF8,a4paper,11pt"
        + (f",fontset={fontset}]" if fontset.lower() not in {"manual", "none"} else "]{fontex")
        + "{ctexart}\n"
        "\\usepackage{fontspec}\n"
        "\\begin{document}\n字体探针：中文字形测试 ABC 123。\n"
        "\\end{document}\n"
    )
    source = source.replace("{fontex}", "{ctexart}")
    result = compile_tex(
        source,
        settings=settings,
        work_root=root,
        job_id=f"font-probe-{os.getpid()}-{uuid.uuid4().hex[:6]}",
    )
    if result.ok:
        return {"ok": True, "fontset": fontset, "message": f"字体方案 {fontset} 可用"}
    message = (result.error or result.log or "").strip().splitlines()
    tail = message[-1] if message else "未知错误"
    return {
        "ok": False,
        "fontset": fontset,
        "message": f"字体方案 {fontset} 不可用：{tail[:200]}",
        "log_tail": (result.log or "")[-1200:],
    }
