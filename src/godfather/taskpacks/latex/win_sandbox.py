"""Windows 原生 TeX 编译隔离沙箱（AppContainer + JobObject + 虚拟盘符映射）。

设计目标：
1. 不依赖管理员权限：使用每任务临时 AppContainer Profile（0 网络能力），
   TeX 子进程及派生进程（如 xdvipdfmx）只能在 Windows 内核权限边界内运行。
2. 默认拒绝本机其余路径读写：
   - 本次作业目录（work_dir）：仅授予当前任务 AppContainer SID 完全控制权；
   - TeX 工具链根目录（如 TeX Live / MiKTeX）：仅授予自定义只读能力 SID
     ``godfatherTexToolchain``（RX，无任何写入权，且不触碰 C:\\Users 或代码库）。
   - 系统 TEMP / USERPROFILE / LOCALAPPDATA / TEXMFVAR 全部重定向到工作目录内。
3. 虚拟盘符映射（subst）：避免 TeX / kpathsea 调用 GetLongPathName 时枚举 D:\\ 等上级盘符。
4. 进程树与超时硬限制：使用 Win32 JobObject（KILL_ON_JOB_CLOSE + TerminateJobObject），
   超时或异常退出时原子级清理整个进程树、沙箱 Profile 与临时映射盘符。
5. 严格失败封闭（Fail-Closed）：隔离环境无法初始化时直接抛出 SandboxInitError，
   绝不静默降级为无隔离编译。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
import string
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_CAPABILITY_NAME = "godfatherTexToolchain"
_DRIVE_LOCK = threading.Lock()
_TOOLCHAIN_ACL_LOCK = threading.Lock()

# Win32 常量
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_SE_GROUP_ENABLED = 0x00000004
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_INFINITE = 0xFFFFFFFF
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class SandboxInitError(RuntimeError):
    """Windows 编译隔离环境初始化失败（必须中止编译，不得降级运行）。"""


@dataclass
class SandboxRunResult:
    exit_code: int
    stdout: str
    timed_out: bool = False
    error: str = ""


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wt.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wt.BOOL),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(_SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wt.DWORD),
        ("Reserved", wt.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD),
        ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD),
        ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(wt.BYTE)),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _win32_apis():
    if os.name != "nt":
        raise SandboxInitError("win_sandbox 仅支持在原生 Windows 环境调用")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kb = ctypes.WinDLL("kernelbase", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    ue = ctypes.WinDLL("userenv", use_last_error=True)

    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wt.BOOL
    adv.FreeSid.argtypes = [ctypes.c_void_p]
    adv.FreeSid.restype = ctypes.c_void_p

    ue.CreateAppContainerProfile.argtypes = [
        wt.LPCWSTR,
        wt.LPCWSTR,
        wt.LPCWSTR,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ue.CreateAppContainerProfile.restype = ctypes.c_long
    ue.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wt.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ue.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    ue.DeleteAppContainerProfile.argtypes = [wt.LPCWSTR]
    ue.DeleteAppContainerProfile.restype = ctypes.c_long

    kb.DeriveCapabilitySidsFromName.argtypes = [
        wt.LPCWSTR,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(wt.DWORD),
    ]
    kb.DeriveCapabilitySidsFromName.restype = wt.BOOL

    k32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wt.DWORD,
        wt.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.InitializeProcThreadAttributeList.restype = wt.BOOL
    k32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    k32.UpdateProcThreadAttribute.restype = wt.BOOL
    k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

    k32.CreateProcessW.argtypes = [
        wt.LPCWSTR,
        wt.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.BOOL,
        wt.DWORD,
        ctypes.c_void_p,
        wt.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    k32.CreateProcessW.restype = wt.BOOL

    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    k32.CreateJobObjectW.restype = wt.HANDLE
    k32.SetInformationJobObject.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
    ]
    k32.SetInformationJobObject.restype = wt.BOOL
    k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k32.AssignProcessToJobObject.restype = wt.BOOL
    k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    k32.TerminateJobObject.restype = wt.BOOL
    k32.ResumeThread.argtypes = [wt.HANDLE]
    k32.ResumeThread.restype = wt.DWORD

    return k32, kb, adv, ue


def _sid_to_str(adv, k32, sid: ctypes.c_void_p) -> str:
    buf = wt.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(buf)):
        raise SandboxInitError(f"无法解析安全标识符 SID（err={ctypes.get_last_error()}）")
    try:
        return str(buf.value)
    finally:
        k32.LocalFree(buf)


def _derive_toolchain_capability(kb, adv, k32) -> tuple[ctypes.c_void_p, str, object]:
    """生成固定只读能力 SID（用于授权沙箱只读访问 TeX 工具链目录）。"""
    cap_groups = ctypes.POINTER(ctypes.c_void_p)()
    cap_g_cnt = wt.DWORD()
    cap_sids = ctypes.POINTER(ctypes.c_void_p)()
    cap_s_cnt = wt.DWORD()
    ok = kb.DeriveCapabilitySidsFromName(
        _CAPABILITY_NAME,
        ctypes.byref(cap_groups),
        ctypes.byref(cap_g_cnt),
        ctypes.byref(cap_sids),
        ctypes.byref(cap_s_cnt),
    )
    if not ok or cap_s_cnt.value < 1:
        raise SandboxInitError(
            f"无法生成 Windows 工具链只读能力标识（err={ctypes.get_last_error()}）"
        )
    sid_ptr = cap_sids[0]
    sid_str = _sid_to_str(adv, k32, sid_ptr)
    # 保持指针底层缓冲区存活，避免被 GC
    return sid_ptr, sid_str, (cap_groups, cap_sids)


def detect_toolchain_root(binary: str | Path) -> Path:
    """识别 TeX 工具链根目录（或隔离测试用二进制目录）。"""
    path = Path(binary).resolve()
    parts_lower = [part.lower() for part in path.parts]
    parent = path.parent
    # Windows 系统目录（如 cmd.exe / powershell）自带 ALL APPLICATION PACKAGES 权限
    if any(p in ("windows", "system32", "syswow64") for p in parts_lower[:3]):
        return parent
    # TeX Live: <root>/bin/windows/xelatex.EXE
    if parent.name.lower() == "windows" and parent.parent.name.lower() == "bin":
        return parent.parent.parent
    # POSIX-like bin: <root>/bin/xelatex.exe
    if parent.name.lower() == "bin":
        return parent.parent
    # MiKTeX: <root>/miktex/bin/x64/xelatex.exe
    if "miktex" in parts_lower and "bin" in parts_lower:
        idx = parts_lower.index("miktex")
        if idx > 0:
            return Path(*path.parts[:idx])
        return Path(*path.parts[: idx + 1])
    return parent


def _is_windows_system_dir(path: Path) -> bool:
    win_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    try:
        path.resolve().relative_to(Path(win_root).resolve())
        return True
    except ValueError:
        return False


def _ensure_toolchain_readonly_acl(toolchain_root: Path, cap_sid_str: str) -> None:
    """确保工具链根目录允许 godfatherTexToolchain（或系统自带 S-1-15-2-1）只读访问。"""
    if _is_windows_system_dir(toolchain_root):
        return
    with _TOOLCHAIN_ACL_LOCK:
        probe = subprocess.run(
            ["icacls", str(toolchain_root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if probe.returncode != 0:
            raise SandboxInitError(
                f"无法读取工具链目录权限（{toolchain_root}）：{(probe.stderr or probe.stdout).strip()}"
            )
        acl_text = probe.stdout or ""
        if cap_sid_str in acl_text or "ALL APPLICATION PACKAGES" in acl_text or "S-1-15-2-1" in acl_text:
            return
        grant = subprocess.run(
            ["icacls", str(toolchain_root), "/grant", f"*{cap_sid_str}:(OI)(CI)RX"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        if grant.returncode != 0:
            detail = (grant.stderr or grant.stdout or "").strip()
            raise SandboxInitError(
                f"无法为 Windows 隔离环境配置工具链只读权限（{toolchain_root}）：{detail or '权限不足'}"
            )


def _grant_workdir_acl(work_dir: Path, ac_sid_str: str) -> None:
    res = subprocess.run(
        ["icacls", str(work_dir), "/grant", f"*{ac_sid_str}:(OI)(CI)F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise SandboxInitError(
            f"无法为工作目录配置隔离读写权限（{work_dir}）：{detail or 'icacls 失败'}"
        )


def _acquire_virtual_drives(work_dir: Path, toolchain_root: Path | None) -> tuple[str, str | None]:
    """用空闲虚拟盘符挂载 work_dir 与工具链根目录，避免枚举上级盘符。"""
    with _DRIVE_LOCK:
        used = {ch for ch in string.ascii_uppercase if Path(f"{ch}:\\").exists()}
        free = [ch for ch in reversed(string.ascii_uppercase) if ch not in used and ch >= "M"]
        need = 2 if toolchain_root is not None else 1
        if len(free) < need:
            raise SandboxInitError("没有可用的 Windows 虚拟盘符用于映射隔离编译目录")
        work_drive = free[0]
        tool_drive = free[1] if toolchain_root is not None else None

        res_w = subprocess.run(
            ["subst", f"{work_drive}:", str(work_dir.resolve())],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if res_w.returncode != 0:
            raise SandboxInitError(
                f"无法挂载隔离工作盘符 {work_drive}:：{(res_w.stderr or res_w.stdout).strip()}"
            )
        if tool_drive and toolchain_root is not None:
            res_t = subprocess.run(
                ["subst", f"{tool_drive}:", str(toolchain_root.resolve())],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
            if res_t.returncode != 0:
                subprocess.run(["subst", f"{work_drive}:", "/D"], capture_output=True, check=False)
                raise SandboxInitError(
                    f"无法挂载隔离工具链盘符 {tool_drive}:：{(res_t.stderr or res_t.stdout).strip()}"
                )
        return work_drive, tool_drive


def _release_virtual_drives(*drives: str | None) -> None:
    with _DRIVE_LOCK:
        for drv in drives:
            if not drv:
                continue
            try:
                subprocess.run(
                    ["subst", f"{drv}:", "/D"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except Exception:
                pass


def _rewrite_path_text(text: str, mapping: list[tuple[Path, str]]) -> str:
    """将文本/环境变量值里的物理前缀改写为隔离虚拟盘符。"""
    if not text:
        return text
    out = text
    for src_root, drv_root in mapping:
        src_win = str(src_root.resolve()).rstrip("\\/")
        src_posix = src_win.replace("\\", "/")
        for variant in (src_win, src_posix):
            pattern = re.compile(re.escape(variant), re.IGNORECASE)
            slash = "/" if variant == src_posix else "\\"
            target = drv_root.rstrip("\\/")
            if variant == src_posix:
                target = target.replace("\\", "/")
            out = pattern.sub(lambda _m, t=target, s=slash: t, out)
    return out


def _quote_windows_cmdline(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv)


def _rewrite_sandbox_args_and_env(
    args: list[str],
    env: dict[str, str],
    *,
    work_dir: Path,
    work_drive: str,
    toolchain_root: Path | None,
    tool_drive: str | None,
) -> tuple[list[str], dict[str, str]]:
    mappings: list[tuple[Path, str]] = [(work_dir.resolve(), f"{work_drive}:")]
    if toolchain_root is not None and tool_drive:
        mappings.append((toolchain_root.resolve(), f"{tool_drive}:"))

    new_args: list[str] = []
    for idx, arg in enumerate(args):
        text = str(arg)
        if idx == 0:
            resolved = Path(text).resolve()
            if toolchain_root is not None and tool_drive:
                try:
                    rel = resolved.relative_to(toolchain_root.resolve())
                    mapped_exe = f"{tool_drive}:\\{rel}"
                    if resolved.suffix.lower() in {".cmd", ".bat"}:
                        comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
                        new_args.extend([comspec, "/c", mapped_exe])
                    else:
                        new_args.append(mapped_exe)
                    continue
                except ValueError:
                    pass
            try:
                rel_w = resolved.relative_to(work_dir.resolve())
                mapped_w = f"{work_drive}:\\{rel_w}"
                if resolved.suffix.lower() in {".cmd", ".bat"}:
                    comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
                    new_args.extend([comspec, "/c", mapped_w])
                else:
                    new_args.append(mapped_w)
                continue
            except ValueError:
                pass
            if resolved.suffix.lower() in {".cmd", ".bat"}:
                comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
                new_args.extend([comspec, "/c", str(resolved)])
            else:
                new_args.append(str(resolved))
            continue
        if text.startswith("-output-directory="):
            new_args.append(f"-output-directory={work_drive}:/")
            continue
        new_args.append(_rewrite_path_text(text, mappings))

    # 重建安全环境变量：所有用户目录、缓存、临时路径限定到 work_drive 内
    work_root_drv = f"{work_drive}:\\"
    temp_dir = work_dir / "sandbox-temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_drv = f"{work_drive}:\\sandbox-temp"
    texmf_var_dir = work_dir / "texmf-var"
    texmf_var_dir.mkdir(parents=True, exist_ok=True)
    texmf_var_drv = f"{work_drive}:\\texmf-var"

    safe_env = dict(env or {})
    for key in ("USERPROFILE", "HOME"):
        safe_env[key] = work_root_drv
    for key in ("TEMP", "TMP", "LOCALAPPDATA", "APPDATA"):
        safe_env[key] = temp_drv
    for key in ("TEXMFVAR", "TEXMFCACHE", "TEXMFCONFIG"):
        safe_env[key] = texmf_var_drv

    # PATH / TEXMFCNF 等含物理前缀的路径全部切换成映射盘符
    if "PATH" in safe_env:
        safe_env["PATH"] = _rewrite_path_text(safe_env["PATH"], mappings)
    if "TEXMFCNF" in safe_env:
        # 使用 forward slashes 更贴合 kpathsea
        posix_map = [
            (src, drv.replace("\\", "/")) for src, drv in mappings
        ]
        rewritten = safe_env["TEXMFCNF"]
        for src_root, drv_root in posix_map:
            src_win = str(src_root.resolve()).rstrip("\\/")
            src_posix = src_win.replace("\\", "/")
            for variant in (src_win, src_posix):
                clean_drv = drv_root.rstrip("/")
                rewritten = re.sub(
                    re.escape(variant),
                    lambda _m, t=clean_drv: t,
                    rewritten,
                    flags=re.IGNORECASE,
                )
        # 修复如 Z:/bin 紧跟或缺斜杠
        safe_env["TEXMFCNF"] = rewritten
    # 确保系统基本变量不丢失
    for sys_key in ("SystemRoot", "WINDIR", "COMSPEC", "ProgramData", "SystemDrive"):
        if not safe_env.get(sys_key) and os.environ.get(sys_key):
            safe_env[sys_key] = os.environ[sys_key]

    return new_args, safe_env


def run_sandboxed(
    args: list[str],
    *,
    work_dir: Path,
    env: dict[str, str],
    timeout_seconds: int = 120,
    job_label: str = "manual",
) -> SandboxRunResult:
    """在 Windows AppContainer + JobObject + 隔离虚拟盘符中运行编译命令。"""
    if not args:
        raise SandboxInitError("编译命令为空")

    k32, kb, adv, ue = _win32_apis()
    resolved_work = work_dir.resolve()
    resolved_work.mkdir(parents=True, exist_ok=True)
    binary_path = Path(args[0]).resolve()
    toolchain_root = detect_toolchain_root(binary_path)
    map_toolchain = None if _is_windows_system_dir(toolchain_root) else toolchain_root

    cap_ptr, cap_sid_str, _cap_keepalive = _derive_toolchain_capability(kb, adv, k32)
    if map_toolchain is not None:
        _ensure_toolchain_readonly_acl(map_toolchain, cap_sid_str)

    safe_label = re.sub(r"[^A-Za-z0-9._-]", "-", str(job_label or "run"))[:40]
    profile_name = f"Godfather.Tex.{safe_label}.{os.getpid()}.{threading.get_ident()}"

    ac_sid = ctypes.c_void_p()
    hr = ue.CreateAppContainerProfile(
        profile_name,
        "Godfather TeX Sandbox",
        "Isolated LaTeX compiler profile",
        None,
        0,
        ctypes.byref(ac_sid),
    )
    if hr < 0:
        hr2 = ue.DeriveAppContainerSidFromAppContainerName(profile_name, ctypes.byref(ac_sid))
        if hr2 < 0 or not ac_sid:
            raise SandboxInitError(
                f"无法创建 Windows AppContainer 隔离配置文件（hr={hr:#x}/{hr2:#x}）"
            )

    work_drive: str | None = None
    tool_drive: str | None = None
    h_job = None
    r_pipe = None
    w_pipe = None
    attr_buf = None
    attr_initialized = False
    pi = _PROCESS_INFORMATION()

    try:
        ac_sid_str = _sid_to_str(adv, k32, ac_sid)
        _grant_workdir_acl(resolved_work, ac_sid_str)
        work_drive, tool_drive = _acquire_virtual_drives(resolved_work, map_toolchain)

        sandbox_args, sandbox_env = _rewrite_sandbox_args_and_env(
            args,
            env,
            work_dir=resolved_work,
            work_drive=work_drive,
            toolchain_root=map_toolchain,
            tool_drive=tool_drive,
        )

        # 构建 JobObject（父句柄关闭即连带清理，超时后强制 TerminateJobObject）
        h_job = k32.CreateJobObjectW(None, None)
        if not h_job:
            raise SandboxInitError(f"无法创建 Windows JobObject（err={ctypes.get_last_error()}）")
        job_info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        job_info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            h_job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(job_info),
            ctypes.sizeof(job_info),
        ):
            raise SandboxInitError(f"无法配置 JobObject 进程限制（err={ctypes.get_last_error()}）")

        cap_arr = (_SID_AND_ATTRIBUTES * 1)()
        cap_arr[0].Sid = cap_ptr
        cap_arr[0].Attributes = _SE_GROUP_ENABLED
        sec_caps = _SECURITY_CAPABILITIES(ac_sid, cap_arr, 1, 0)

        attr_size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
        attr_buf = ctypes.create_string_buffer(attr_size.value)
        if not k32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(attr_size)):
            raise SandboxInitError("无法初始化进程安全属性列表")
        attr_initialized = True
        if not k32.UpdateProcThreadAttribute(
            attr_buf,
            0,
            _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(sec_caps),
            ctypes.sizeof(sec_caps),
            None,
            None,
        ):
            raise SandboxInitError(f"无法附加 AppContainer 安全属性（err={ctypes.get_last_error()}）")

        sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
        r_pipe, w_pipe = wt.HANDLE(), wt.HANDLE()
        if not k32.CreatePipe(ctypes.byref(r_pipe), ctypes.byref(w_pipe), ctypes.byref(sa), 0):
            raise SandboxInitError("无法创建沙箱输出管道")
        k32.SetHandleInformation(r_pipe, _HANDLE_FLAG_INHERIT, 0)

        # Windows 要求 Environment block 按变量名大小写不敏感字典序排序
        sorted_items = sorted(
            ((str(k), str(v)) for k, v in sandbox_env.items() if v not in (None, "")),
            key=lambda kv: kv[0].upper(),
        )
        env_block = "\0".join(f"{k}={v}" for k, v in sorted_items) + "\0\0"
        env_buf = ctypes.create_unicode_buffer(env_block)

        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        # 在 WinSta0\Default 上允许 AppContainer 完成 user32/字体库初始化（含在无头测试桌面启动的场景）
        si.StartupInfo.lpDesktop = "WinSta0\\Default"
        si.StartupInfo.hStdInput = None
        si.StartupInfo.hStdOutput = w_pipe
        si.StartupInfo.hStdError = w_pipe
        si.lpAttributeList = ctypes.cast(attr_buf, ctypes.c_void_p)

        cmdline = _quote_windows_cmdline(sandbox_args)
        creation_flags = (
            _CREATE_SUSPENDED
            | _EXTENDED_STARTUPINFO_PRESENT
            | _CREATE_NO_WINDOW
            | _CREATE_UNICODE_ENVIRONMENT
        )
        ok = k32.CreateProcessW(
            None,
            ctypes.create_unicode_buffer(cmdline),
            None,
            None,
            True,
            creation_flags,
            env_buf,
            f"{work_drive}:\\",
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            err = ctypes.get_last_error()
            raise SandboxInitError(f"无法在 Windows AppContainer 沙箱中启动编译器（WinError {err}）")

        # 关闭父进程持有的写端管道，使子进程退出后 ReadFile 自动 EOF
        k32.CloseHandle(w_pipe)
        w_pipe = None

        if not k32.AssignProcessToJobObject(h_job, pi.hProcess):
            k32.TerminateProcess(pi.hProcess, 1)
            raise SandboxInitError(f"无法将沙箱进程绑定到 JobObject（err={ctypes.get_last_error()}）")
        if k32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
            k32.TerminateJobObject(h_job, 1)
            raise SandboxInitError(f"无法恢复沙箱进程执行（err={ctypes.get_last_error()}）")

        chunks: list[bytes] = []

        def _drain_pipe() -> None:
            buf = ctypes.create_string_buffer(4096)
            read = wt.DWORD()
            while k32.ReadFile(r_pipe, buf, 4096, ctypes.byref(read), None) and read.value:
                chunks.append(buf.raw[: read.value])

        reader = threading.Thread(target=_drain_pipe, daemon=True)
        reader.start()

        timeout_ms = max(1000, int(timeout_seconds * 1000))
        wait_rc = k32.WaitForSingleObject(pi.hProcess, timeout_ms)
        timed_out = False
        exit_code = -1
        error = ""
        if wait_rc == _WAIT_TIMEOUT:
            timed_out = True
            k32.TerminateJobObject(h_job, 1)
            error = f"编译超时（>{timeout_seconds}s），已终止沙箱进程树"
            exit_code = -9
        elif wait_rc == _WAIT_OBJECT_0:
            code = wt.DWORD()
            if k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code)):
                raw_code = int(code.value)
                # Windows unsigned DWORD 转回带符号 int（若未超出 32 位正数范围直接保留）
                exit_code = raw_code if raw_code < 0x80000000 else raw_code - 0x100000000
        else:
            error = f"等待沙箱进程结束失败（wait_rc={wait_rc}）"

        # 确保进程树内没有任何残留派生进程再关管
        time.sleep(0.02)
        if timed_out:
            try:
                k32.CloseHandle(r_pipe)
                r_pipe = None
            except Exception:
                pass
        reader.join(timeout=3)
        stdout = b"".join(chunks).decode("utf-8", errors="replace")
        return SandboxRunResult(
            exit_code=exit_code,
            stdout=stdout,
            timed_out=timed_out,
            error=error,
        )
    finally:
        if pi.hThread:
            k32.CloseHandle(pi.hThread)
        if pi.hProcess:
            k32.CloseHandle(pi.hProcess)
        if w_pipe:
            k32.CloseHandle(w_pipe)
        if r_pipe:
            k32.CloseHandle(r_pipe)
        if h_job:
            k32.TerminateJobObject(h_job, 1)
            k32.CloseHandle(h_job)
        if attr_initialized and attr_buf is not None:
            k32.DeleteProcThreadAttributeList(attr_buf)
        _release_virtual_drives(work_drive, tool_drive)
        try:
            ue.DeleteAppContainerProfile(profile_name)
        except Exception:
            pass
        if ac_sid:
            try:
                adv.FreeSid(ac_sid)
            except Exception:
                pass
