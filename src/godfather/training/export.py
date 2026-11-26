"""导出路线（设计文档 §9.2）：

    训练适配器 → 与精确匹配的基础权重合并 → 转换为支持的 GGUF → 量化
    → 用 Modelfile 导入 Ollama → 固定回归集复测

每一步都是真实执行的外部命令；任何一步失败都会如实记录，不会生成"看起来成功"的模型记录。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..context import AppContext
from ..jobs import JobContext
from ..util import canonical_json, new_id, now_iso
from .base import TrainingConfig
from .evaluate import list_ollama_models
from .datasets import SYSTEM_PROMPT

QUANTIZE_METHODS = ("q4_k_m", "q4_0", "q5_k_m", "q8_0", "f16")

# stop 序列必须与**训练目标的结尾**一致：目标是整篇 .tex，结尾是 "\end{document}"。
#
# 两个坑：
# 1) 以前写的是 "\n\n"（反斜杠+n 两次）。实测 Ollama 的 Modelfile 解析器
#    **不把 \n 解释成换行**（只处理 \\ 与 \"），所以那个 stop 永远匹配不上，
#    模型会一路生成到 token 上限。而且就算解释成换行，正文里的第一个空行
#    也在导言区中间 —— 一样会把输出砍在导言区。
# 2) 这里必须写**单**反斜杠：文件里写 "\end{document}" 时解析出的值正是
#    一个反斜杠 + "end{document}"，与模型输出的结尾一致（已实测 stop 生效）。
MODELFILE_TEMPLATE = """# 由 Godfather 自动生成
FROM {gguf}
PARAMETER stop "\\end{{document}}"
{template_line}SYSTEM "{system}"
"""

# 训练脚本在没有 chat_template 时用的 prompt 格式：
# "### 指令\n{prompt}\n\n### 回答\n{answer}"
# 推理时必须用**完全相同**的格式，否则提示词与训练分布不一致。
INSTRUCT_PROMPT_PREFIX = "### 指令\n"
INSTRUCT_PROMPT_MIDDLE = "\n\n### 回答\n"

# 对应的 Ollama（Go template）模板：
# {{ .Prompt }} 是用户消息，模板负责把它包成训练时的样子。
OLLAMA_INSTRUCT_TEMPLATE = (
    "### 指令\n{{ .Prompt }}\n\n### 回答\n"
)


def _is_real_template(value: str) -> bool:
    """判断给的是不是**真正的模板**（而不是模板名）。

    Ollama 0.40 的 Modelfile 解析器**不解析模板名**：
    `TEMPLATE qwen` 会被原样存成一个 4 字符的模板字符串，
    于是每次推理提示词前面都被拼上 "qwen"（实测 prompt token 数多出 6 个）。
    真正的模板必须含 Go template 语法（{{ ... }}）。
    """
    return "{{" in (value or "") and "}}" in (value or "")


@dataclass
class ExportStep:
    name: str
    ok: bool
    detail: str = ""
    output: str = ""
    log: str = ""
    duration_ms: int = 0
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "output": self.output,
            "duration_ms": self.duration_ms,
            "metrics": self.metrics,
        }


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 3600,
    on_line=None,
) -> tuple[int, str]:
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name != "nt"),
        )
    except FileNotFoundError as exc:
        return -1, f"无法启动 {command[0]}：{exc}"
    # 用 communicate(timeout=...) 而不是"先读完 stdout 再 wait"：
    # 后者在命令挂住时会一直阻塞到进程自己结束，timeout 形同虚设。
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            stdout, _ = process.communicate(timeout=10)
        except Exception:
            stdout = ""
        return -2, (stdout or "") + f"\n命令超时（>{timeout}s），已终止"
    code = process.returncode if process.returncode is not None else -1
    text = stdout or ""
    if on_line and text:
        for line in text.splitlines():
            on_line(line)
    return code, text


def _kill_process_tree(process: subprocess.Popen) -> None:
    """结束子进程及其子孙（Windows 用 taskkill /T，POSIX 用进程组）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=30,
                check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), 9)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def find_llama_cpp(ctx: AppContext) -> Path | None:
    """定位 llama.cpp 目录。

    必须返回**绝对路径**：子进程会切换 cwd（cwd=llama.cpp），
    相对路径会指向错误的位置（表现为"转换失败（退出码 2）"）。
    """
    configured = getattr(ctx.settings.export, "llama_cpp_dir", "")
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            ctx.settings.data_dir.parent / ".toolchain" / "llama.cpp",
            Path(__file__).resolve().parent.parent.parent.parent / ".toolchain" / "llama.cpp",
            Path.home() / "llama.cpp",
        ]
    )
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.exists() and (
            (resolved / "convert_hf_to_gguf.py").exists()
            or (resolved / "conversion" / "convert_hf_to_gguf.py").exists()
        ):
            return resolved
    return None


def find_binary(llama_cpp: Path | None, name: str) -> str | None:
    if llama_cpp is None:
        return shutil.which(name)
    for relative in (
        f"build/bin/Release/{name}.exe",
        f"build/bin/Release/{name}",
        f"build/bin/{name}",
        f"build/bin/{name}.exe",
        f"build/bin/{name}.cmd",
        f"{name}",
        f"{name}.exe",
        f"{name}.cmd",
    ):
        candidate = llama_cpp / relative
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


def export_run(
    ctx: AppContext,
    job: JobContext,
    *,
    project_id: str,
    run_id: str,
    quantization: str = "q4_k_m",
    model_name: str = "",
    template: str = "",
    system_prompt: str = "",
) -> dict:
    """把一次训练运行导出为 Ollama 可用的模型版本。"""
    run = ctx.db.query_one("SELECT * FROM training_runs WHERE id = ?", (run_id,))
    if not run:
        raise ValueError(f"训练运行不存在：{run_id}")
    if run["status"] != "succeeded":
        raise ValueError(f"训练运行未成功（状态 {run['status']}），不能导出")
    if quantization not in QUANTIZE_METHODS:
        raise ValueError(f"未知量化方式：{quantization}")

    config = TrainingConfig.from_dict(json.loads(run["params_json"] or "{}"))
    adapter_dir = Path(run["adapter_path"] or "")
    # 运行目录：checkpoint_path 记录的就是本次运行的目录本身，
    # 再取 .parent 会落到上一级（产物会写到训练目录之外）。
    if run["checkpoint_path"]:
        output_dir = Path(run["checkpoint_path"])
    elif adapter_dir:
        output_dir = adapter_dir.parent
    else:
        raise ValueError("训练运行缺少产物路径，无法导出")
    output_dir = output_dir.resolve()
    merged_dir = output_dir / "merged"
    gguf_dir = output_dir / "gguf"
    gguf_dir.mkdir(parents=True, exist_ok=True)

    steps: list[ExportStep] = []

    # ---- 0. 占用 GPU ----
    # 合并基座要把整个模型读进显存，与训练必须串行；
    # 否则 8GB 卡上很容易 OOM。
    from . import resources as training_resources

    resource_owner = f"export:{run_id}"
    acquired = training_resources.wait_for_resource(
        ctx.db,
        training_resources.GPU_RESOURCE,
        resource_owner,
        project_id=project_id,
        timeout_seconds=int(getattr(ctx.settings.worker, "gpu_wait_seconds", 900)),
        poll_seconds=5.0,
        on_wait=lambda: job.emit_event(
            stage="resource", message="等待 GPU 资源（其它训练或推理占用中）", level="warning"
        ),
    )
    if not acquired:
        raise RuntimeError("等待 GPU 资源超时，导出未开始")
    resource_heartbeat = training_resources.ResourceHeartbeat(
        ctx.db, training_resources.GPU_RESOURCE, resource_owner
    )
    resource_heartbeat.__enter__()

    try:
        # ---- 1. 合并适配器 ----
        job.emit_event(stage="export", message="合并适配器与基座权重")
        merge_config = output_dir / "merge_config.json"
        merge_config.write_text(
            canonical_json(
                {
                    "base_model": config.base_model,
                    "base_revision": config.base_revision or "",
                    "adapter_dir": str(adapter_dir),
                    "merged_dir": str(merged_dir),
                }
            ),
            encoding="utf-8",
        )
        started = time.time()
        code, log = _run(
            [
            sys.executable,
            "-m",
            "godfather.training.scripts.merge_lora",
            "--config",
            str(merge_config.resolve()),
        ],
            cwd=str(output_dir),
            env=os.environ.copy(),
            timeout=3600,
            on_line=lambda line: job.emit_event(stage="export", message=line[:300]),
        )
        merged_ok = code == 0 and merged_dir.exists() and any(merged_dir.iterdir())
        # PEFT 不可用时训练会退化成全参数训练，产物本身就是完整模型，不需要合并
        metrics = json.loads(run["metrics_json"] or "{}")
        if (
            metrics.get("artifact_type") == "full"
            and adapter_dir.exists()
            and any(adapter_dir.iterdir())
        ):
            merged_ok = True
            merged_dir = adapter_dir
            steps.append(
                ExportStep(
                    name="合并适配器",
                    ok=True,
                    detail="训练产出的是完整模型（未使用 LoRA），直接用于转换",
                    output=str(merged_dir),
                )
            )
        steps.append(
            ExportStep(
                name="合并适配器",
                ok=merged_ok,
                detail="合并成功" if merged_ok else f"合并失败（退出码 {code}）",
                output=str(merged_dir) if merged_ok else "",
                log=log[-4000:],
                duration_ms=int((time.time() - started) * 1000),
            )
        )
        if not merged_ok:
            return _fail(ctx, project_id, run_id, steps, job)

        # ---- 2. 转 GGUF ----
        llama_cpp = find_llama_cpp(ctx)
        convert_script = None
        if llama_cpp:
            for rel in ("convert_hf_to_gguf.py", "conversion/convert_hf_to_gguf.py"):
                cand = llama_cpp / rel
                if cand.exists():
                    convert_script = cand
                    break
        gguf_path = gguf_dir / "model-f16.gguf"
        job.emit_event(stage="export", message="转换为 GGUF")
        if convert_script is None or not convert_script.exists():
            steps.append(
                ExportStep(
                    name="转换 GGUF",
                    ok=False,
                    detail="未找到 llama.cpp 的 convert_hf_to_gguf.py（请设置 config.yaml 的 export.llama_cpp_dir）",
                )
            )
            return _fail(ctx, project_id, run_id, steps, job)
        started = time.time()
        code, log = _run(
            [
                sys.executable,
                str(convert_script),
                str(merged_dir),
                "--outfile",
                str(gguf_path),
                "--outtype",
                "f16",
            ],
            cwd=str(llama_cpp.resolve()),
            env=os.environ.copy(),
            timeout=3600,
            on_line=lambda line: job.emit_event(stage="export", message=line[:300]),
        )
        gguf_ok = code == 0 and gguf_path.exists() and gguf_path.stat().st_size > 0
        steps.append(
            ExportStep(
                name="转换 GGUF",
                ok=gguf_ok,
                detail="转换成功" if gguf_ok else f"转换失败（退出码 {code}）",
                output=str(gguf_path) if gguf_ok else "",
                log=log[-4000:],
                duration_ms=int((time.time() - started) * 1000),
            )
        )
        if not gguf_ok:
            return _fail(ctx, project_id, run_id, steps, job)

        # ---- 3. 量化 ----
        quantize_binary = find_binary(llama_cpp, "llama-quantize")
        quantized_path = gguf_dir / f"model-{quantization}.gguf"
        job.emit_event(stage="export", message=f"量化为 {quantization}")
        if not quantize_binary:
            steps.append(ExportStep(name="量化", ok=False, detail="未找到 llama-quantize"))
            return _fail(ctx, project_id, run_id, steps, job)
        started = time.time()
        code, log = _run(
            [quantize_binary, str(gguf_path), str(quantized_path), quantization],
            cwd=str(llama_cpp.resolve()),
            env=os.environ.copy(),
            timeout=3600,
            on_line=lambda line: job.emit_event(stage="export", message=line[:300]),
        )
        quant_ok = code == 0 and quantized_path.exists() and quantized_path.stat().st_size > 0
        steps.append(
            ExportStep(
                name="量化",
                ok=quant_ok,
                detail=f"{quantization} 量化成功" if quant_ok else f"量化失败（退出码 {code}）",
                output=str(quantized_path) if quant_ok else "",
                log=log[-4000:],
                duration_ms=int((time.time() - started) * 1000),
                metrics={"size_bytes": quantized_path.stat().st_size if quantized_path.exists() else 0},
            )
        )
        if not quant_ok:
            return _fail(ctx, project_id, run_id, steps, job)

        # ---- 4. 导入 Ollama ----
        name = model_name or _default_model_name(ctx, project_id, config)
        modelfile = output_dir / "Modelfile"

        # 模板必须与**训练时的 prompt 格式**一致。
        #
        # 以前这里写死 "qwen"，而实测 Ollama 0.40 的 Modelfile 解析器
        # **不解析模板名**：`TEMPLATE qwen` 被原样存成 4 个字符的模板，
        # 于是每次推理提示词前面都被拼上 "qwen"（实测 prompt token 多 6 个），
        # 推理格式与训练完全对不上。
        #
        # 现在的规则：
        # - 训练用了分词器自带的 chat_template → GGUF 里会带上 → **不写** TEMPLATE 行
        #   （让 Ollama 用 GGUF 自带的那个，与训练完全一致）；
        # - 训练用了 instruct 格式（没有 chat_template）→ 写对应的 Ollama 模板；
        # - 用户显式给了真正的模板字符串（含 {{ }}）→ 用它。
        metrics_blob = run.get("metrics_json") or "{}"
        try:
            metrics_blob = json.loads(metrics_blob)
        except (TypeError, ValueError):
            metrics_blob = {}
        prompt_format = str(metrics_blob.get("prompt_format") or "")
        template_line = ""
        if template and _is_real_template(template):
            template_line = f"TEMPLATE {template}\n"
        elif prompt_format == "instruct":
            # 与训练脚本的 PROMPT_TEMPLATE 保持一致
            template_line = f"TEMPLATE {OLLAMA_INSTRUCT_TEMPLATE}\n"
        # prompt_format == "chat_template"：不写 TEMPLATE 行，
        # 让 Ollama 用 GGUF 自带的 chat template（与训练一致）。
        # 其它情况（老训练记录没有这个字段）：不写，
        # 至少不会像 "qwen" 那样往提示词前面塞字面量。

        # 读取项目关联的任务包（不同任务包的 system_prompt 与 stop_sequences 可热插拔定制）
        project_row = ctx.project(project_id) or {}
        task_type = str(project_row.get("taskpack") or "markdown_to_latex")
        taskpacks = getattr(ctx, "taskpacks", None)
        pack_spec = taskpacks.get_optional(task_type) if taskpacks is not None else None
        effective_system = system_prompt or (pack_spec.system_prompt if pack_spec and pack_spec.system_prompt else SYSTEM_PROMPT)

        if pack_spec and pack_spec.id != "markdown_to_latex":
            stop_lines = "".join(
                f'PARAMETER stop "{_sanitize_modelfile_value(seq)}"\n'
                for seq in (pack_spec.stop_sequences or [])
                if seq
            )
            modelfile_body = (
                f"# 由 Godfather 自动生成 ({pack_spec.id})\n"
                f"FROM {_quote_modelfile_path(str(quantized_path))}\n"
                f"{stop_lines}"
                f"{template_line}"
                f'SYSTEM "{_sanitize_modelfile_value(effective_system)}"\n'
            )
        else:
            modelfile_body = MODELFILE_TEMPLATE.format(
                gguf=_quote_modelfile_path(str(quantized_path)),
                template_line=template_line,
                # 系统提示词必须与训练时用的完全一致（单一来源）：
                # 否则导出后的模型与训练时的输入分布对不上。
                system=_sanitize_modelfile_value(effective_system),
            )
        modelfile.write_text(modelfile_body, encoding="utf-8")
        ollama_binary = ctx.settings.export.ollama_bin or "ollama"
        job.emit_event(stage="export", message=f"导入 Ollama：{name}")
        started = time.time()
        code, log = _run(
            [ollama_binary, "create", name, "-f", str(modelfile)],
            cwd=str(output_dir),
            env=os.environ.copy(),
            timeout=1800,
            on_line=lambda line: job.emit_event(stage="export", message=line[:300]),
        )
        ollama_ok = code == 0
        steps.append(
            ExportStep(
                name="导入 Ollama",
                ok=ollama_ok,
                detail=f"已注册 {name}" if ollama_ok else f"导入失败（退出码 {code}）",
                output=name if ollama_ok else "",
                log=log[-4000:],
                duration_ms=int((time.time() - started) * 1000),
            )
        )
        if not ollama_ok:
            return _fail(ctx, project_id, run_id, steps, job)

        # ---- 4.1 回读注册的模板，确认它不是"字面量名字" ----
        # Ollama 不解析模板名：`TEMPLATE qwen` 会被原样存成 4 个字符，
        # 推理时提示词前面就被拼上 "qwen"。这里回读确认。
        template_note = _verify_registered_template(
            ctx.settings.providers.ollama.base_url, name
        )
        if template_note:
            job.emit_event(stage="export", message=template_note, level="warning")
            steps.append(
                ExportStep(name="模板校验", ok=True, detail=template_note, output=name)
            )

        # ---- 5. 登记模型版本 ----
        model_id = new_id("mod")
        ctx.db.insert(
            "model_versions",
            {
                "id": model_id,
                "project_id": project_id,
                "name": name,
                "base_model": config.base_model,
                "base_revision": config.base_revision,
                "tokenizer_revision": config.tokenizer_revision,
                "chat_template": template or "qwen",
                "quantization": quantization,
                "path": str(quantized_path),
                "status": "candidate",
                "metrics_json": "{}",
                "source_run_id": run_id,
                "previous_version": "",
                "deployed_at": None,
                "notes": f"由训练运行 {run_id} 导出",
                "created_at": now_iso(),
            },
        )
        ctx.db.update(
            "training_runs",
            run_id,
            {
                "merged_path": str(merged_dir),
                "gguf_path": str(gguf_path),
                "quantized_path": str(quantized_path),
                "ollama_name": name,
                "updated_at": now_iso(),
            },
        )
        job.emit_event(
            stage="export",
            message=f"导出完成：{name}（{quantization}）",
            payload={"model_id": model_id, "name": name, "steps": [s.to_dict() for s in steps]},
        )
    finally:
        resource_heartbeat.__exit__(None, None, None)
        training_resources.release(ctx.db, training_resources.GPU_RESOURCE, resource_owner)
    return {
        "status": "succeeded",
        "model_id": model_id,
        "name": name,
        "quantization": quantization,
        "gguf_path": str(quantized_path),
        "size_bytes": quantized_path.stat().st_size,
        "steps": [s.to_dict() for s in steps],
    }


def _quote_modelfile_path(path: str) -> str:
    """FROM 的路径必须加引号：Windows 用户目录常含空格（"C:/Users/jon hu/..."）。"""
    normalized = path.replace("\\", "/")
    return f'"{normalized}"'


def _sanitize_modelfile_value(text: str) -> str:
    r"""Modelfile 的参数值不能带换行、未转义引号与反斜杠。

    反斜杠必须先转义：Modelfile 解析器会把 \s 当成转义序列，
    SYSTEM 里的 LaTeX 命令（\section 等）会被破坏。
    """
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def _verify_registered_template(base_url: str, name: str) -> str:
    """回读 Ollama 里注册的模板，确认它没被当成"字面量名字"。

    返回一句说明（没问题时返回空串）。
    """
    try:
        import httpx

        response = httpx.post(
            f"{base_url.rstrip('/')}/api/show", json={"name": name}, timeout=15.0
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001
        return ""  # 读不到就不断言（Ollama 可能没在跑）
    template = str(payload.get("template") or "")
    if not template:
        return (
            f"模型 {name} 没有注册任何模板：推理时用 Ollama 的默认格式。"
            "如果基座模型自带 chat template，通常说明 GGUF 里没带上它。"
        )
    if "{{" not in template:
        return (
            f"模型 {name} 注册的模板是字面量 {template!r}（Ollama 不解析模板名），"
            "推理时它会被拼在提示词前面 —— 建议改用真正的模板或让 GGUF 自带 chat template。"
        )
    return ""


def _default_model_name(ctx: AppContext, project_id: str, config: TrainingConfig) -> str:
    """生成模型版本名。

    名字在 **Ollama 里是全局的**（`ollama create` 同名会直接替换），
    所以序号必须跨项目统计 —— 以前只数本项目的版本，
    两个项目都会拿到 `godfather-latex:v001`，
    后导出的直接把前一个项目的权重覆盖掉：
    项目 A 的 model_versions 指向的权重已经变成 B 的了（DB 与 Ollama 不一致且没有任何报错）。

    另外还要避开 Ollama 里已存在的同名模型（可能是用户自己注册的）。
    """
    prefix = ctx.settings.export.model_name_prefix or "godfather-latex"
    # 跨项目统计最大序号
    rows = ctx.db.query_all("SELECT name FROM model_versions")
    highest = 0
    for row in rows:
        name = str(row["name"] or "")
        if ":" in name:
            suffix = name.rsplit(":", 1)[1]
            if suffix.startswith("v") and suffix[1:].isdigit():
                highest = max(highest, int(suffix[1:]))
    # 序号 +1（以前写成 +3，第一个模型直接跳到 v003，白白空两个号）
    index = highest + 1
    taken = {
        str(row["name"] or "")
        for row in ctx.db.query_all("SELECT name FROM model_versions")
    }
    # Ollama 里已存在的名字也不能用（可能是用户自己注册的模型）
    try:
        existing = list_ollama_models(ctx.settings.providers.ollama.base_url)
    except Exception:  # noqa: BLE001
        existing = None
    if existing:
        taken.update(existing)
    while f"{prefix}:v{index:03d}" in taken:
        index += 1
    return f"{prefix}:v{index:03d}"


def _fail(ctx: AppContext, project_id: str, run_id: str, steps: list[ExportStep], job: JobContext) -> dict:
    # 注意：不修改 training_runs.status —— 训练成功就是成功，
    # 导出失败只说明导出失败（可能是没装 llama.cpp），不该让这次训练永远无法再导出。
    # 导出状态记录在 notes 里，界面上单独显示。
    ctx.db.update(
        "training_runs",
        run_id,
        {
            "updated_at": now_iso(),
            "notes": canonical_json([s.to_dict() for s in steps]),
        },
    )
    failed = [s for s in steps if not s.ok]
    message = "；".join(f"{s.name}: {s.detail}" for s in failed) or "导出失败"
    job.emit_event(stage="export", message=f"导出失败：{message}", level="error")
    raise RuntimeError(f"导出失败：{message}")
