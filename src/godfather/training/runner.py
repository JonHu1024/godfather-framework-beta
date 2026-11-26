"""训练运行器：拉起后端进程、实时记录日志、处理取消与资源锁。

取消与恢复语义（设计文档 §3.3）：
- 取消 = 终止训练进程树；已产生的检查点保留；
- **没有完整优化器状态时只能重新启动训练**，不能冒充精确续训。
  本项目默认使用 HF Trainer 的 checkpoint（不含优化器状态时续训语义为"重新开始"），
  因此恢复动作一律标记为 `restart`，并在运行记录里写清楚。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from ..context import AppContext
from ..jobs import STATUS_PAUSED_RESOURCE, JobContext, JobPaused, PermanentJobError
from ..util import canonical_json, now_iso
from . import resources
from .backends import get_backend
from .base import TrainingConfig
from .datasets import prepare_dataset

# 运行中的训练进程（用于取消）
RUNNING_PROCESSES: dict[str, subprocess.Popen] = {}


class TrainingCancelled(Exception):
    """训练被取消（启动前或运行中）。"""


class TrainingFailed(Exception):
    """训练失败：状态已写进 training_runs，这里把失败传播给 job 层。

    任务层据此把 job 标成 failed（并按 max_attempts 重试），
    而不是"handler 正常返回 → 任务成功"。
    """


def _terminate_training_process(run_id: str) -> None:
    """训练异常时收掉子进程。

    不收的话：状态标成失败、GPU 锁释放了，但训练进程还在跑 ——
    相当于两个训练同时占着同一块卡。
    """
    process = RUNNING_PROCESSES.pop(run_id, None)
    if process is None:
        return
    try:
        if process.poll() is None:
            if os.name != "nt":
                try:
                    os.killpg(os.getpgid(process.pid), 15)
                except (ProcessLookupError, PermissionError, OSError):
                    process.terminate()
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(os.getpgid(process.pid), 9)
                    except (ProcessLookupError, PermissionError, OSError):
                        process.kill()
                else:
                    process.kill()
    except Exception:  # noqa: BLE001
        return


def cancel_run(run_id: str) -> bool:
    proc = RUNNING_PROCESSES.get(run_id)
    if proc is None or proc.poll() is not None:
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=20,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    return True


def run_training(
    ctx: AppContext,
    job: JobContext,
    *,
    project_id: str,
    config: TrainingConfig,
    run_id: str,
    resume_semantics: str = "restart",
) -> dict:
    """执行一次训练。返回运行记录。"""
    backend = get_backend(config.backend)
    output_dir = Path(config.output_dir or (ctx.settings.data_dir / "training" / run_id))
    output_dir.mkdir(parents=True, exist_ok=True)

    ctx.db.update(
        "training_runs",
        run_id,
        {"status": "running", "started_at": now_iso()},
    )

    # ---- 数据准备 ----
    job.emit_event(stage="dataset", message="准备训练数据")
    try:
        prepared = prepare_dataset(
            ctx.db,
            config.dataset_snapshot_id,
            output_dir,
            train_split=config.train_split,
            eval_split=config.eval_split,
            max_train=config.max_train_samples,
            max_eval=config.max_eval_samples,
            taskpacks=getattr(ctx, "taskpacks", None),
        )
        if prepared.manifest["train_count"] == 0:
            raise PermanentJobError(
                f"快照 {config.dataset_snapshot_id} 的 {config.train_split} 划分为空，无法训练"
                "（请先在「数据集版本」接纳样本并冻结包含训练集的快照）"
            )
    except Exception as exc:
        # 数据准备失败也必须如实记录：否则运行状态会永远停在 running
        ctx.db.update(
            "training_runs",
            run_id,
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": now_iso(),
                "updated_at": now_iso(),
            },
        )
        raise
    job.emit_event(
        stage="dataset",
        done=prepared.manifest["train_count"],
        total=prepared.manifest["train_count"],
        message=f"训练样本 {prepared.manifest['train_count']} 条，评测样本 {prepared.manifest['eval_count']} 条",
        payload=prepared.manifest,
    )

    # ---- 预检 ----
    try:
        checks = backend.preflight(config)
        fatal = [check for check in checks if not check.ok and check.fatal]
        for check in checks:
            job.emit_event(
                stage="preflight",
                message=f"{check.name}：{'通过' if check.ok else '未通过'} — {check.detail}",
                level="info" if check.ok else "warning",
            )
        if fatal:
            raise PermanentJobError("预检未通过：" + "；".join(f"{c.name}（{c.detail}）" for c in fatal))
    except Exception as exc:
        ctx.db.update(
            "training_runs",
            run_id,
            {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "finished_at": now_iso(), "updated_at": now_iso()},
        )
        raise

    # ---- 资源锁 ----
    owner = f"training:{run_id}"
    should_stop = (
        ctx.db.query_one("SELECT status FROM jobs WHERE id = ?", (job.job_id,)) or {}
    ).get("status") == "cancelled"
    acquired = resources.wait_for_resource(
        ctx.db,
        resources.GPU_RESOURCE,
        owner,
        project_id=project_id,
        timeout_seconds=int(getattr(ctx.settings.worker, "gpu_wait_seconds", 900)),
        poll_seconds=5.0,
        on_wait=lambda: job.emit_event(
            stage="resource", message="等待 GPU 资源（其它训练或推理占用中）", level="warning"
        ),
        # 等待锁期间也要能取消
        should_cancel=lambda: (
            ctx.db.query_one("SELECT status FROM jobs WHERE id = ?", (job.job_id,)) or {}
        ).get("status") == "cancelled",
    )
    if not acquired and should_stop:
        # 是被取消而不是等锁超时：如实记成 cancelled
        ctx.db.update(
            "training_runs",
            run_id,
            {
                "status": "cancelled",
                "finished_at": now_iso(),
                "updated_at": now_iso(),
                "notes": "等待 GPU 资源期间被取消",
            },
        )
        job.emit_event(stage="resource", message="等待 GPU 资源期间被取消，训练未启动", level="warning")
        return {"run_id": run_id, "status": "cancelled"}
    if not acquired:
        ctx.db.update(
            "training_runs",
            run_id,
            {
                "status": "paused",
                "error": "等待 GPU 资源超时，训练未启动",
                "updated_at": now_iso(),
            },
        )
        raise JobPaused(STATUS_PAUSED_RESOURCE, "等待 GPU 资源超时，训练未启动")

    # 锁的续租心跳：训练可能远超锁的 TTL，没有续租会被另一个训练"接管"，
    # 两个训练就会同时占用同一块 GPU。
    heartbeat = resources.ResourceHeartbeat(ctx.db, resources.GPU_RESOURCE, owner)
    heartbeat.__enter__()
    try:
        gpu = resources.gpu_state(ctx.db, ctx.settings.providers.ollama.base_url)
        if gpu.get("ollama_models"):
            job.emit_event(
                stage="resource",
                message=f"注意：Ollama 仍常驻 {len(gpu['ollama_models'])} 个模型，可能影响训练显存",
                level="warning",
            )

        # ---- 启动 ----
        config_path = output_dir / "backend_config.json"
        command = backend.build_command(
            config,
            dataset_dir=prepared.directory,
            output_dir=output_dir,
            config_path=config_path,
        )
        ctx.db.update("training_runs", run_id, {"params_json": canonical_json(config.to_dict())})
        job.emit_event(
            stage="train",
            message="启动训练：" + " ".join(command[-3:]),
            payload={"command": command, "output_dir": str(output_dir)},
        )

        env = backend.env_for_run()
        # 启动前必须再查一次取消状态：
        # 训练准备（数据准备、预检、等 GPU 锁）可能已经跑了几分钟，
        # 用户在这期间点了取消，训练还照跑几小时，GPU 被白占。
        status_row = ctx.db.query_one("SELECT status FROM jobs WHERE id = ?", (job.job_id,))
        if status_row and status_row["status"] == "cancelled":
            job.emit_event(stage="train", message="训练已被取消，不启动", level="warning")
            raise TrainingCancelled("训练在启动前被取消")

        started = time.time()
        process = subprocess.Popen(
            command,
            cwd=str(output_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name != "nt"),
        )
        RUNNING_PROCESSES[run_id] = process

        metrics: dict[str, Any] = {}
        last_emit = 0.0
        log_lines: list[str] = []
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            log_lines.append(line)
            parsed = backend.parse_log_line(line)
            now = time.time()
            if parsed and parsed.get("metrics"):
                metrics.update(parsed["metrics"])
            if parsed and (now - last_emit > 0.5 or parsed.get("level") == "error"):
                last_emit = now
                job.emit_event(
                    stage="train",
                    message=parsed.get("message", line)[:500],
                    level=parsed.get("level", "info"),
                    payload=parsed.get("metrics", {}),
                )
            elif not parsed:
                job.emit_event(stage="train", message=line[:500])

        exit_code = process.wait()
        duration = time.time() - started
        RUNNING_PROCESSES.pop(run_id, None)

        log_path = output_dir / "train.log"
        log_path.write_text("\n".join(log_lines), encoding="utf-8")

        # 被取消不算失败，但已产出的日志与适配器路径必须保留（否则排障与续训都无从下手）
        job_row = ctx.db.query_one("SELECT status FROM jobs WHERE id = ?", (job.job_id,))
        if job_row and job_row["status"] == "cancelled":
            adapter_dir = output_dir / "adapter"
            ctx.db.update(
                "training_runs",
                run_id,
                {
                    "status": "cancelled",
                    "finished_at": now_iso(),
                    "updated_at": now_iso(),
                    "log_path": str(log_path),
                    "adapter_path": str(adapter_dir) if adapter_dir.exists() else "",
                    "notes": "已取消；续训语义为重新开始（不含优化器状态）",
                },
            )
            job.emit_event(stage="train", message="训练已取消", level="warning")
            return {"run_id": run_id, "status": "cancelled", "log_path": str(log_path)}

        metrics_path = output_dir / "train_metrics.json"
        if metrics_path.exists():
            try:
                metrics.update(json.loads(metrics_path.read_text(encoding="utf-8")))
            except Exception:
                pass

        adapter_dir = output_dir / "adapter"
        adapter_ok = adapter_dir.exists() and any(adapter_dir.iterdir())
        if exit_code != 0 or not adapter_ok:
            detail = "训练进程未成功退出" if exit_code != 0 else "训练结束但没有产出适配器"
            ctx.db.update(
                "training_runs",
                run_id,
                {
                    "status": "failed",
                    "error": detail,
                    "metrics_json": canonical_json({"exit_code": exit_code, **metrics}),
                    "finished_at": now_iso(),
                    "updated_at": now_iso(),
                },
            )
            raise RuntimeError(f"{detail}（退出码 {exit_code}），详见日志 {log_path}")

        ctx.db.update(
            "training_runs",
            run_id,
            {
                "status": "succeeded",
                "adapter_path": str(adapter_dir),
                "checkpoint_path": str(output_dir),
                "log_path": str(log_path),
                "metrics_json": canonical_json(
                    {"exit_code": exit_code, "duration_seconds": round(duration, 1), **metrics}
                ),
                "finished_at": now_iso(),
                "updated_at": now_iso(),
            },
        )
        job.emit_event(
            stage="train",
            message=f"训练完成，用时 {duration:.0f}s",
            payload=metrics,
        )
        return {
            "run_id": run_id,
            "status": "succeeded",
            "adapter_path": str(adapter_dir),
            "output_dir": str(output_dir),
            "metrics": metrics,
            "resume_semantics": resume_semantics,
        }
    except TrainingCancelled as cancelled:
        # 启动前被取消：不算失败，如实记成 cancelled
        ctx.db.update(
            "training_runs",
            run_id,
            {
                "status": "cancelled",
                "finished_at": now_iso(),
                "updated_at": now_iso(),
                "notes": "训练在启动前被取消",
            },
        )
        job.emit_event(stage="train", message=str(cancelled), level="warning")
        return {"run_id": run_id, "status": "cancelled"}
    except Exception as exc:  # noqa: BLE001
        # 其它任何异常都必须如实落到状态上：
        # 以前这里只捕获 TrainingCancelled，别的异常会跳过状态更新 →
        # training_runs.status 永远停在 "running"（界面上一个僵尸训练），
        # 而 finally 又把 GPU 锁释放了 → 另一个训练可以同时抢同一块卡。
        _terminate_training_process(run_id)
        detail = f"{type(exc).__name__}: {exc}"
        ctx.db.update(
            "training_runs",
            run_id,
            {
                "status": "failed",
                "error": detail,
                "finished_at": now_iso(),
                "updated_at": now_iso(),
                "notes": f"训练异常终止：{detail}",
            },
        )
        job.emit_event(
            stage="train",
            message=f"训练异常终止：{detail}",
            level="error",
        )
        # 必须**重新抛出**：job 层看到 handler 正常返回就会把任务标成 succeeded，
        # 界面上显示"任务完成"但训练根本没跑成，而且不会触发重试。
        # 状态已经在上面如实写进 training_runs，这里只负责让 job 层判失败。
        raise TrainingFailed(detail) from exc
    finally:
        heartbeat.__exit__(None, None, None)
        resources.release(ctx.db, resources.GPU_RESOURCE, owner)


def start_run(ctx: AppContext, project_id: str, config: TrainingConfig, run_id: str | None = None) -> dict:
    """登记一次训练运行（状态 queued，真正的执行交给 worker）。"""
    from ..util import new_id

    run_id = run_id or new_id("run")
    output_dir = config.output_dir or str(ctx.settings.data_dir / "training" / run_id)
    config.output_dir = output_dir
    ctx.db.insert(
        "training_runs",
        {
            "id": run_id,
            "project_id": project_id,
            "dataset_snapshot_id": config.dataset_snapshot_id,
            "backend": config.backend,
            "base_model": config.base_model,
            "base_revision": config.base_revision,
            "params_json": canonical_json(config.to_dict()),
            "status": "queued",
            "log_path": "",
            "metrics_json": "{}",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )
    return {"run_id": run_id, "status": "queued", "output_dir": output_dir}
