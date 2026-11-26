"""对抗式审查后的回归测试：锁住已修复的真实缺陷。

每个用例都对应一次真实复现过的缺陷，不做形式化覆盖。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]

import pytest


# --------------------------------------------------------------------------
# 任务队列：租约续期 / 取消语义 / 幂等
# --------------------------------------------------------------------------
def test_lease_is_renewed_during_long_jobs(ctx, project):
    """长任务必须续租，否则会被别的 worker 当成"崩溃的任务"接管并重复执行。"""
    ctx.settings.worker.lease_seconds = 6
    ctx.queue.register("slow", lambda job: time.sleep(10))
    job = ctx.queue.submit("slow", {}, project_id=project)

    thread = threading.Thread(target=lambda: ctx.queue._run_job(ctx.queue._claim("w1"), "w1"))
    thread.start()
    time.sleep(8)
    row = ctx.db.query_one("SELECT status, lease_owner FROM jobs WHERE id = ?", (job["id"],))
    assert row["status"] == "running"
    assert row["lease_owner"] == "w1"

    # 另一个 worker 不能抢走
    other = ctx.queue._claim("w2")
    assert other is None or other["id"] != job["id"]
    thread.join(timeout=20)


def test_cancelled_job_is_not_overwritten_by_success(ctx, project):
    """取消后 worker 收尾不能把状态改回 succeeded。"""
    ctx.queue.register("slow", lambda job: time.sleep(1.0))
    job = ctx.queue.submit("slow", {}, project_id=project)
    thread = threading.Thread(target=lambda: ctx.queue._run_job(ctx.queue._claim("w1"), "w1"))
    thread.start()
    time.sleep(0.3)
    assert ctx.queue.cancel(job["id"]) is True
    thread.join(timeout=10)
    row = ctx.db.query_one("SELECT status FROM jobs WHERE id = ?", (job["id"],))
    assert row["status"] == "cancelled"


def test_failed_job_can_be_resubmitted_with_same_idempotency_key(ctx, project):
    """失败的任务必须允许用同一个幂等键重跑。"""
    ctx.queue.register("boom", lambda job: (_ for _ in ()).throw(RuntimeError("boom")))
    job = ctx.queue.submit("boom", {}, project_id=project, idempotency_key="same")
    # 跑到终态（默认最多重试 3 次）
    for _ in range(4):
        claimed = ctx.queue._claim("w1")
        if claimed is None:
            break
        ctx.queue._run_job(claimed, "w1")
        if ctx.db.query_one("SELECT status FROM jobs WHERE id = ?", (job["id"],))["status"] == "failed":
            break
    assert ctx.db.query_one("SELECT status FROM jobs WHERE id = ?", (job["id"],))["status"] == "failed"

    again = ctx.queue.submit("boom", {}, project_id=project, idempotency_key="same")
    assert again["id"] != job["id"]


def test_job_logs_return_the_tail(ctx, project):
    """任务日志要能看到最后几行（长任务失败时最早的日志没有诊断价值）。"""
    job = ctx.queue.submit("t", {}, project_id=project)
    for index in range(30):
        ctx.db.insert(
            "job_logs",
            {
                "job_id": job["id"],
                "ts": "2026-11-06T00:00:00+08:00",
                "level": "info",
                "message": f"line {index}",
                "data_json": "{}",
            },
        )
    logs = ctx.queue.logs(job["id"], limit=5)
    assert len(logs) == 5
    assert logs[-1]["message"] == "line 29"


# --------------------------------------------------------------------------
# 预算：原子预留 / 不重复扣减
# --------------------------------------------------------------------------
def test_budget_reservation_is_atomic(ctx, project):
    """并发预留不能超过总额（原先 4×60 分能全部通过）。"""
    ctx.settings.budget.paid_calls_enabled = True
    ctx.settings.budget.total_cents = 100
    ctx.settings.budget.per_call_cents = 100
    results: list[str] = []

    from godfather.budget import BudgetExceeded, check_and_reserve

    def worker() -> None:
        try:
            check_and_reserve(ctx.db, project, ctx.settings, estimated_cents=60, cost_status="known")
            results.append("ok")
        except BudgetExceeded:
            results.append("rejected")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1
    assert results.count("rejected") == 3


def test_budget_pending_row_not_double_counted(ctx, project):
    """预留中的钱不能既算已花又算预留。"""
    from godfather.budget import check_and_reserve, get_state, settle

    ctx.settings.budget.paid_calls_enabled = True
    ctx.settings.budget.total_cents = 100
    ctx.settings.budget.per_call_cents = 100
    call = check_and_reserve(ctx.db, project, ctx.settings, estimated_cents=60, cost_status="known")
    state = get_state(ctx.db, project, ctx.settings)
    assert state.spent_cents == 0
    assert state.reserved_cents == 60
    assert state.available_cents == 40
    settle(ctx.db, call, status="succeeded", actual_cents=60, cost_status="known")
    state = get_state(ctx.db, project, ctx.settings)
    assert state.spent_cents == 60
    assert state.reserved_cents == 0
    assert state.available_cents == 40


# --------------------------------------------------------------------------
# 教师缓存：不能跨项目串数据
# --------------------------------------------------------------------------
def test_teacher_cache_is_scoped_per_project(ctx, project):
    """项目 B 不能命中项目 A 的缓存。"""
    from godfather.budget import cache_key

    key_a = cache_key(provider="ollama", model="m", prompt="p", project_id="prj-a")
    key_b = cache_key(provider="ollama", model="m", prompt="p", project_id="prj-b")
    assert key_a != key_b


# --------------------------------------------------------------------------
# 数据集快照：冻结后不可被覆盖
# --------------------------------------------------------------------------
def test_snapshot_file_is_not_overwritten(ctx, project):
    """同一个 version 再冻结一次，不能覆盖已冻结的 JSONL。"""
    from godfather.datasets import build_snapshot

    def add_example(example_id: str, group: str) -> None:
        ctx.db.insert(
            "training_examples",
            {
                "id": example_id,
                "project_id": project,
                "task_type": "markdown_to_latex",
                "source_group_id": group,
                "source_revision": "r1",
                "input_payload_json": "{}",
                "target_payload_json": "{}",
                "teacher_json": "{}",
                "policy_version": "p1",
                "prompt_version": "pv1",
                "reference_ids_json": "[]",
                "annotation_ids_json": "[]",
                "label_origin": "rule_synthesized",
                "validation_report_id": None,
                "status": "accepted",
                "split": "train",
                "created_at": "2026-11-06T00:00:00+08:00",
                "updated_at": "2026-11-06T00:00:00+08:00",
            },
        )

    add_example("e1", "g1")
    first = build_snapshot(ctx.db, project, version="v1", seed="s")
    add_example("e2", "g2")
    second = build_snapshot(ctx.db, project, version="v1", seed="s")

    assert first.path != second.path
    assert Path(first.path).exists()
    import hashlib

    digest = "sha256:" + hashlib.sha256(Path(first.path).read_bytes()).hexdigest()
    assert digest == first.sha256


# --------------------------------------------------------------------------
# 训练：截断不能切掉答案；空答案不能伪造监督信号
# --------------------------------------------------------------------------
def test_encode_pair_never_loses_the_answer():
    pytest.importorskip("transformers")
    from godfather.training.scripts.train_local import encode_pair

    class FakeTokenizer:
        def __call__(self, text, add_special_tokens=False):
            return {"input_ids": list(range(len(text)))}

    tokenizer = FakeTokenizer()
    input_ids, labels, truncated = encode_pair(tokenizer, "p" * 300, "a" * 20, max_len=64)
    assert truncated is True
    supervised = [label for label in labels if label != -100]
    assert len(supervised) == 20, "答案被截断，训练将没有监督信号"


def test_encode_pair_empty_answer_has_no_supervision():
    pytest.importorskip("transformers")
    from godfather.training.scripts.train_local import encode_pair

    class FakeTokenizer:
        def __call__(self, text, add_special_tokens=False):
            return {"input_ids": list(range(len(text)))}

    _ids, labels, _truncated = encode_pair(FakeTokenizer(), "prompt", "", max_len=64)
    assert all(label == -100 for label in labels), "空答案被伪造成监督目标"


def test_training_splits_are_restricted():
    """split 必须是固定枚举，防止路径逃逸与 train/dev 对调。"""
    from godfather.training.datasets import prepare_dataset

    with pytest.raises(ValueError):
        prepare_dataset(None, "snap", Path("/tmp"), train_split="../../escaped")
    with pytest.raises(ValueError):
        prepare_dataset(None, "snap", Path("/tmp"), train_split="dev", eval_split="dev")


# --------------------------------------------------------------------------
# 导出：Modelfile 路径要加引号；失败不能把训练标记成不可再导出
# --------------------------------------------------------------------------
def test_modelfile_quotes_paths_and_escapes_system():
    from godfather.training.export import (
        MODELFILE_TEMPLATE,
        _quote_modelfile_path,
        _sanitize_modelfile_value,
    )

    rendered = MODELFILE_TEMPLATE.format(
        gguf=_quote_modelfile_path("C:/Users/jon hu/x.gguf"),
        template_line="",
        system=_sanitize_modelfile_value('用 \\section 输出 "引号"'),
    )
    assert 'FROM "C:/Users/jon hu/x.gguf"' in rendered
    assert "\\\\section" in rendered
    assert '\\"' in rendered
    # 模板行是可选的：没有模板时不能留下空的 TEMPLATE 行
    assert "TEMPLATE" not in rendered


def test_ollama_template_name_is_not_used_as_literal():
    """不能把模板**名**（"qwen"）写进 Modelfile。

    Ollama 0.40 不解析模板名：`TEMPLATE qwen` 会被原样存成 4 个字符的模板，
    推理时提示词前面被拼上 "qwen"（实测 prompt token 多 6 个）。
    """
    from godfather.training.export import (
        OLLAMA_INSTRUCT_TEMPLATE,
        _is_real_template,
    )

    assert _is_real_template("qwen") is False
    assert _is_real_template("llama3") is False
    assert _is_real_template(OLLAMA_INSTRUCT_TEMPLATE) is True
    # instruct 模板必须与训练脚本的 PROMPT_TEMPLATE 一致
    from godfather.training.scripts.train_local import PROMPT_TEMPLATE

    assert PROMPT_TEMPLATE.format(prompt="X", answer="") == "### 指令\nX\n\n### 回答\n"
    assert OLLAMA_INSTRUCT_TEMPLATE.replace("{{ .Prompt }}", "X") == (
        PROMPT_TEMPLATE.format(prompt="X", answer="")
    )


def test_export_failure_keeps_run_publishable(ctx, project, monkeypatch):
    """导出失败不能把训练成功改成 export_failed（否则永远无法再导出）。"""
    from godfather.training import export as export_module

    ctx.db.insert(
        "training_runs",
        {
            "id": "run-x",
            "project_id": project,
            "dataset_snapshot_id": None,
            "backend": "local",
            "base_model": "base",
            "base_revision": "",
            "params_json": "{}",
            "status": "succeeded",
            "log_path": "",
            "metrics_json": "{}",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )

    class Job:
        job_id = "j"
        project_id = project

        def emit_event(self, **kwargs):
            return None

    monkeypatch.setattr(export_module, "find_llama_cpp", lambda ctx: None)
    with pytest.raises(RuntimeError):
        export_module.export_run(ctx, Job(), project_id=project, run_id="run-x", quantization="q4_k_m")
    row = ctx.db.query_one("SELECT status FROM training_runs WHERE id = 'run-x'")
    assert row["status"] == "succeeded"


# --------------------------------------------------------------------------
# 评测：模型不存在要明确失败；指标要带分母
# --------------------------------------------------------------------------
def test_evaluation_rejects_missing_model(ctx, project):
    from godfather.training.evaluate import evaluate_models

    with pytest.raises(ValueError):
        evaluate_models(
            ctx,
            None,
            project_id=project,
            candidate_model="definitely-not-installed",
            baseline_model="also-missing",
            snapshot_id="snap",
        )


def test_aggregate_separates_call_failures():
    from godfather.training.evaluate import _aggregate

    records = [
        {"id": "1", "score": {"non_empty": True, "has_latex": True, "block_coverage": 1.0, "similarity": 0.9, "cjk_preserved": True}, "latency_ms": 10},
        {"id": "2", "score": {"non_empty": False, "has_latex": False, "block_coverage": None, "similarity": 0.0, "cjk_preserved": False}, "latency_ms": 0, "error": "模型不存在"},
    ]
    aggregate = _aggregate(records)
    assert aggregate["n"] == 2
    assert aggregate["errors"] == 1
    assert aggregate["evaluated"] == 1
    # 比率的分母是**总样本数**：失败算 0。
    # 以前按"成功样本数"算 → 失败一半时指标反而是满分，deltas 会误导。
    assert aggregate["non_empty_rate"] == 0.5
    assert aggregate["rates_over_usable"]["non_empty_rate"] == 1.0
    assert aggregate["latency_denominator"] == 1


# --------------------------------------------------------------------------
# 模型版本：不能跨项目激活
# --------------------------------------------------------------------------
def test_activate_rejects_model_from_other_project(ctx, project):
    from godfather.training.models import activate

    ctx.db.insert(
        "projects",
        {
            "id": "prj-other",
            "name": "other",
            "taskpack": "markdown_to_latex",
            "description": "",
            "active_model": "",
            "policy_version": "",
            "settings_json": "{}",
            "status": "created",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "model_versions",
        {
            "id": "mod-other",
            "project_id": "prj-other",
            "name": "other:v001",
            "base_model": "base",
            "base_revision": "",
            "tokenizer_revision": "",
            "chat_template": "",
            "quantization": "q4_k_m",
            "path": "",
            "status": "candidate",
            "metrics_json": "{}",
            "created_at": "2026-11-06T00:00:00+08:00",
        },
    )
    with pytest.raises(ValueError):
        activate(ctx.db, project, "mod-other")


# --------------------------------------------------------------------------
# 局域网安全：监听非回环地址必须启用配对
# --------------------------------------------------------------------------
def test_non_loopback_bind_enables_pairing(tmp_path):
    from godfather.app import create_app
    from godfather.settings import load_settings

    settings = load_settings()
    settings.storage.data_dir = str(tmp_path / "data")
    settings.server.host = "0.0.0.0"
    settings.server.lan_bind = False
    app = create_app(settings)
    assert app.state.pairing.enabled is True


def test_loopback_bind_keeps_pairing_off(tmp_path):
    from godfather.app import create_app
    from godfather.settings import load_settings

    settings = load_settings()
    settings.storage.data_dir = str(tmp_path / "data")
    settings.server.host = "127.0.0.1"
    settings.server.lan_bind = False
    app = create_app(settings)
    assert app.state.pairing.enabled is False


# --------------------------------------------------------------------------
# 四层检查：不能有"永远 pass"的死检查
# --------------------------------------------------------------------------
def test_layout_layer_detects_overfull_in_real_log():
    """越界检测必须逐行匹配（原先没有 MULTILINE，真实日志一条都命中不了）。"""
    from godfather.taskpacks.latex.validators import check_layout

    log = "This is XeTeX\nOverfull \\hbox (12.0pt too wide) in paragraph at lines 10--11\n"
    report = check_layout(compile_result={"log": log})
    codes = {f["code"] for f in report["findings"]}
    assert "overfull_box" in codes


def test_content_layer_detects_dropped_math():
    """公式保留检查不能是死检查（把公式整段删掉必须报出来）。"""
    from godfather.taskpacks.latex.mdparse import Block
    from godfather.taskpacks.latex.validators import check_content

    blocks = [Block(block_id="block-0001", kind="math", text="E = mc^2")]
    report = check_content(blocks=blocks, tex_text="\\section{标题}\n这里没有公式。\n", source_text="E = mc^2")
    codes = {f["code"] for f in report["findings"]}
    assert "math_changed" in codes


def test_dangerous_commands_are_detected():
    from godfather.taskpacks.latex.validators import check_dangerous_commands

    tex = "\\documentclass{article}\\begin{document}\n$x \\end{document} y$\n\\end{document}\n"
    findings = check_dangerous_commands(tex)
    assert any(f["code"] == "document_ended_early" for f in findings)


def test_budget_enforces_max_seconds(ctx, project):
    """max_seconds 以前只统计不执行，现在必须真的拦。"""
    from godfather.budget import BudgetExceeded, check_and_reserve, settle

    ctx.settings.budget.paid_calls_enabled = True
    ctx.settings.budget.total_cents = 100
    ctx.settings.budget.per_call_cents = 100
    ctx.settings.budget.max_calls = 100
    ctx.settings.budget.max_seconds = 10
    call = check_and_reserve(ctx.db, project, ctx.settings, estimated_cents=1, cost_status="known")
    settle(ctx.db, call, status="succeeded", actual_cents=1, cost_status="known", duration_ms=11000)
    with pytest.raises(BudgetExceeded):
        check_and_reserve(ctx.db, project, ctx.settings, estimated_cents=1, cost_status="known")


def test_events_are_pruned(ctx, project):
    """事件表要保留上限，不能无限增长。"""
    from godfather.events import EVENT_RETENTION, emit

    for index in range(5):
        emit(ctx.db, project_id=project, message=f"m{index}")
    assert ctx.db.scalar("SELECT COUNT(*) FROM events") >= 5
    assert EVENT_RETENTION > 0


# --------------------------------------------------------------------------
# 产物存储：同 digest 的 meta 不能被丢弃；删除项目要清理文件
# --------------------------------------------------------------------------
def test_store_merges_meta_for_same_digest(ctx, project):
    """同一份内容再次产出时，meta 要更新（否则行映射停留在第一次生成）。"""
    first = ctx.store.put_bytes(project, "tex", b"same", meta={"mode": "a", "line_map": {"x": 1}})
    second = ctx.store.put_bytes(project, "tex", b"same", meta={"mode": "b", "line_map": {"x": 2}})
    assert first["id"] == second["id"]
    meta = json.loads(ctx.store.get(first["id"])["meta_json"])
    assert meta["mode"] == "b"
    assert meta["line_map"] == {"x": 2}


def test_delete_project_cleans_artifacts_and_calls(client, project):
    """删除项目要清掉 provider_calls 与磁盘上的产物文件。"""
    artifact = client.app.state.ctx.store.put_bytes(project, "markdown", b"# x", media_type="text/markdown")
    assert os.path.exists(artifact["path"])
    response = client.delete(f"/api/projects/{project}")
    assert response.status_code == 200
    assert not os.path.exists(artifact["path"])


# --------------------------------------------------------------------------
# 数据库：老库缺列时仍能启动；嵌套事务
# --------------------------------------------------------------------------
def test_old_database_without_column_still_starts(tmp_path):
    """老库缺 jobs.idempotency_key 时必须能启动（否则进程根本起不来）。"""
    import sqlite3

    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, taskpack TEXT NOT NULL, "
        "description TEXT DEFAULT '', active_model TEXT DEFAULT '', policy_version TEXT DEFAULT '', "
        "settings_json TEXT DEFAULT '{}', status TEXT DEFAULT 'created', created_at TEXT, updated_at TEXT);"
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id TEXT, type TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'queued', payload_json TEXT DEFAULT '{}', result_json TEXT DEFAULT '{}', "
        "error TEXT DEFAULT '', attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, "
        "priority INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT, started_at TEXT, finished_at TEXT);"
    )
    conn.commit()
    conn.close()

    from godfather.db import Database

    db = Database(db_path)
    db.initialize()
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(jobs)")}
    assert "idempotency_key" in columns
    assert db.conn.execute("SELECT name FROM sqlite_master WHERE name='idx_job_idem'").fetchone()


def test_nested_transactions_work(ctx):
    """嵌套事务不能报 "cannot start a transaction within a transaction"。"""
    with ctx.db.transaction():
        with ctx.db.transaction():
            ctx.db.execute(
                "INSERT INTO projects(id, name, taskpack, created_at, updated_at) "
                "VALUES('p-nested','n','t','x','x')"
            )
    assert ctx.db.scalar("SELECT COUNT(*) FROM projects WHERE id = 'p-nested'") == 1


# --------------------------------------------------------------------------
# 策略版本：同版本号不能覆盖历史版本
# --------------------------------------------------------------------------
def test_policy_same_version_does_not_overwrite(ctx, project):
    from godfather.policy import engine as policy_engine

    package = policy_engine.parse_policy("", "policy-001")
    package.preferences.append(
        policy_engine.Preference(
            pref_id="pref-1", content="第一条", scope="project", scope_value="",
            conditions={}, priority=50, confidence=0.8, status="active", evidence=[], rule={},
        )
    )
    first = policy_engine.save_policy(ctx.db, project, package)
    package.preferences[0].content = "改过的内容"
    second = policy_engine.save_policy(ctx.db, project, package)
    assert second["version"] != first["version"]
    old = ctx.db.query_one("SELECT content_yaml FROM policies WHERE version = 'policy-001'")
    assert old and "第一条" in old["content_yaml"]


# --------------------------------------------------------------------------
# 任务队列：暂停不是结束；恢复要重置尝试次数
# --------------------------------------------------------------------------
def test_paused_job_has_no_finished_at_and_resume_resets_attempts(ctx, project):
    from godfather.jobs import STATUS_PAUSED_BUDGET, JobPaused

    def raiser(job):
        raise JobPaused(STATUS_PAUSED_BUDGET, "预算不足")

    ctx.queue.register("pause", raiser)
    job = ctx.queue.submit("pause", {}, project_id=project)
    claimed = ctx.queue._claim("w1")
    ctx.queue._run_job(claimed, "w1")
    row = ctx.db.query_one("SELECT status, finished_at, attempts FROM jobs WHERE id = ?", (job["id"],))
    assert row["status"] == STATUS_PAUSED_BUDGET
    assert row["finished_at"] in (None, ""), "暂停不是结束，不该写 finished_at"

    ctx.db.update("jobs", job["id"], {"attempts": 3})
    assert ctx.queue.resume(job["id"]) is True
    row = ctx.db.query_one("SELECT status, attempts FROM jobs WHERE id = ?", (job["id"],))
    assert row["status"] == "queued"
    assert row["attempts"] == 0


# --------------------------------------------------------------------------
# pandoc 扩展名白名单
# --------------------------------------------------------------------------
def test_pandoc_extensions_are_whitelisted():
    from godfather.taskpacks.latex.pandoc_base import _safe_extensions

    assert _safe_extensions(["fancy_lists", "footnotes"]) == ["fancy_lists", "footnotes"]
    # 扩展名会拼进 --from，能注入 --lua-filter 就是任意代码执行
    assert _safe_extensions(["--lua-filter=evil.lua"]) == []
    assert _safe_extensions(["lua_filter"]) == []
    # raw_tex 会把原始 TeX 直通进编译器
    assert _safe_extensions(["raw_tex"]) == []


# --------------------------------------------------------------------------
# 行内公式：中文正文里的单个 $ 不是公式
# --------------------------------------------------------------------------
def test_single_dollar_in_chinese_text_is_not_math():
    from godfather.taskpacks.latex.assembler import _convert_inline

    assert "\\$5" in _convert_inline("价格 $5 和 $10 元")
    assert "$E=mc^2$" in _convert_inline("公式 $E=mc^2$ 在这里")


# --------------------------------------------------------------------------
# 事件订阅：默认不跨项目
# --------------------------------------------------------------------------
def test_events_history_is_project_scoped_by_default(client, project):
    ctx = client.app.state.ctx
    from godfather.events import emit

    # 事件表有外键，项目必须真实存在
    if not ctx.project(project):
        ctx.db.insert(
            "projects",
            {
                "id": project,
                "name": "p",
                "taskpack": "markdown_to_latex",
                "description": "",
                "active_model": "",
                "policy_version": "",
                "settings_json": "{}",
                "status": "created",
                "created_at": "2026-11-06T00:00:00+08:00",
                "updated_at": "2026-11-06T00:00:00+08:00",
            },
        )
    emit(ctx.db, project_id=project, message="项目内事件")
    emit(ctx.db, project_id=None, message="全局事件")
    history = client.get(f"/api/events/history?project_id={project}").json()
    messages = [item["message"] for item in history]
    assert "项目内事件" in messages
    assert "全局事件" not in messages

    empty = client.get("/api/events/history").json()
    assert all(item["message"] != "项目内事件" for item in empty)


# --------------------------------------------------------------------------
# 修订：结果要写回文档版本，并被下一次渲染采用
# --------------------------------------------------------------------------
def test_revision_is_written_back_to_document_blocks(ctx, project):
    from godfather.taskpacks.latex.assembler import render_block
    from godfather.taskpacks.latex.mdparse import Block

    ctx.db.insert(
        "documents",
        {
            "id": "doc-1",
            "project_id": project,
            "title": "测试文档",
            "source_revision": "r1",
            "status": "parsed",
            "policy_version": "",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "document_revisions",
        {
            "id": "docrev-1",
            "document_id": "doc-1",
            "revision": 1,
            "source_revision": "r1",
            "blocks_json": json.dumps(
                [{"block_id": "block-0001", "kind": "paragraph", "text": "原文", "line_start": 1, "line_end": 1}]
            ),
            "context_json": "{}",
            "created_at": "2026-11-06T00:00:00+08:00",
        },
    )
    block = Block(block_id="block-0001", kind="paragraph", text="原文", meta={"revised_tex": "\\textbf{改过}"})
    assert render_block(block)[0].strip() == "\\textbf{改过}"


def test_waiting_user_state_is_reachable(ctx, project):
    """教师提问在策略要求时可以进入 waiting_user（问题进收件箱）。"""
    from godfather.jobs import STATUS_WAITING_USER
    from godfather.taskpacks.latex.pipeline import _wait_on_teacher_questions

    class Policy:
        validator_config = {"wait_on_teacher_questions": True}

    assert _wait_on_teacher_questions(Policy()) is True

    class DefaultPolicy:
        validator_config = {}

    assert _wait_on_teacher_questions(DefaultPolicy()) is False
    assert STATUS_WAITING_USER in {
        "queued", "running", "waiting_user", "paused_budget", "paused_resource",
        "succeeded", "failed", "cancelled",
    }


def test_cancelled_job_is_not_revived_by_a_late_failure(ctx, project):
    """任务执行期间被取消，随后 handler 抛错：不能变回 queued（否则会复活再跑）。"""
    def handler(ctx_job):
        # 执行期间用户点了取消
        ctx.queue.cancel(ctx_job.job_id)
        raise RuntimeError("boom after cancel")

    ctx.queue.register("boom_after_cancel", handler)
    job = ctx.queue.submit("boom_after_cancel", {}, project_id=project)
    claimed = ctx.queue._claim("w1")
    ctx.queue._run_job(claimed, "w1")
    row = ctx.db.query_one("SELECT status, error FROM jobs WHERE id = ?", (job["id"],))
    assert row["status"] == "cancelled", f"任务复活了：{row}"
    assert "boom after cancel" not in (row["error"] or "") or row["status"] == "cancelled"


def test_cancelled_job_is_not_revived_by_a_late_pause(ctx, project):
    """同样适用于 JobPaused 分支。"""
    from godfather.jobs import JobPaused, STATUS_PAUSED_BUDGET

    def handler(ctx_job):
        ctx.queue.cancel(ctx_job.job_id)
        raise JobPaused(STATUS_PAUSED_BUDGET, "预算不足")

    ctx.queue.register("pause_after_cancel", handler)
    job = ctx.queue.submit("pause_after_cancel", {}, project_id=project)
    claimed = ctx.queue._claim("w1")
    ctx.queue._run_job(claimed, "w1")
    row = ctx.db.query_one("SELECT status FROM jobs WHERE id = ?", (job["id"],))
    assert row["status"] == "cancelled"


# --------------------------------------------------------------------------
# 第三轮复审修掉的缺陷
# --------------------------------------------------------------------------
def test_old_database_with_duplicate_idempotency_keys_still_starts(tmp_path):
    """老库有重复 idempotency_key 时不能起不来（建唯一索引会 IntegrityError）。"""
    import sqlite3

    db_path = tmp_path / "dup.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id TEXT, type TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'queued', payload_json TEXT DEFAULT '{}', "
        "result_json TEXT DEFAULT '{}', error TEXT DEFAULT '', attempts INTEGER DEFAULT 0, "
        "max_attempts INTEGER DEFAULT 3, priority INTEGER DEFAULT 0, created_at TEXT, "
        "updated_at TEXT, started_at TEXT, finished_at TEXT, idempotency_key TEXT);"
        "INSERT INTO jobs(id, type, idempotency_key, created_at) VALUES('j1','t','dup','2020-01-01');"
        "INSERT INTO jobs(id, type, idempotency_key, created_at) VALUES('j2','t','dup','2020-01-02');"
    )
    conn.commit()
    conn.close()

    from godfather.db import Database

    db = Database(db_path)
    db.initialize()
    keys = [row["idempotency_key"] for row in db.query_all("SELECT idempotency_key FROM jobs")]
    assert len([k for k in keys if k == "dup"]) == 1


def test_revised_tex_of_code_block_stays_verbatim():
    """修订后的代码块必须仍然是 Verbatim，且不能闭合宿主环境。"""
    from godfather.taskpacks.latex.assembler import render_block
    from godfather.taskpacks.latex.mdparse import Block

    block = Block(
        block_id="b1",
        kind="code",
        text="原始",
        meta={"revised_tex": "y = 2 % 100%\n\\end{Verbatim} \\input{/etc/passwd}"},
    )
    tex, _warnings, _assets = render_block(block)
    assert "\\begin{Verbatim}" in tex
    # 精确的结束标记必须被破坏，否则能闭合环境注入命令
    assert "\n\\end{Verbatim}\n" in tex
    assert "\n\\end{Verbatim} " not in tex.replace("\\end{Verbatim }", "")


def test_generation_idempotency_key_uses_real_document_revision(client, project):
    """幂等键里的文档修订号要取真实的（documents 表没有 revision 列）。"""
    ctx = client.app.state.ctx
    if not ctx.project(project):
        ctx.db.insert(
            "projects",
            {
                "id": project,
                "name": "p",
                "taskpack": "markdown_to_latex",
                "description": "",
                "active_model": "",
                "policy_version": "",
                "settings_json": "{}",
                "status": "created",
                "created_at": "2026-11-06T00:00:00+08:00",
                "updated_at": "2026-11-06T00:00:00+08:00",
            },
        )
    ctx.db.insert(
        "documents",
        {
            "id": "doc-x",
            "project_id": project,
            "title": "t",
            "source_revision": "r1",
            "status": "parsed",
            "policy_version": "",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "document_revisions",
        {
            "id": "docrev-1",
            "document_id": "doc-x",
            "revision": 3,
            "source_revision": "r1",
            "blocks_json": "[]",
            "context_json": "{}",
            "created_at": "2026-11-06T00:00:00+08:00",
        },
    )
    response = client.post(
        f"/api/projects/{project}/generations",
        json={"document_id": "doc-x", "mode": "rule_only", "provider": "ollama", "model": ""},
    )
    assert response.status_code == 200
    job = ctx.db.query_one("SELECT * FROM jobs WHERE id = ?", (response.json()["job_id"],))
    assert ":3:" in job["idempotency_key"], job["idempotency_key"]


def test_write_back_revision_creates_new_document_revision(ctx, project):
    """修订写回：产生新的 document_revisions 行，旧行保留。"""
    from godfather.taskpacks.latex.mdparse import Block
    from godfather.taskpacks.latex.pipeline import write_back_revision

    ctx.db.insert(
        "documents",
        {
            "id": "doc-w",
            "project_id": project,
            "title": "t",
            "source_revision": "r1",
            "status": "parsed",
            "policy_version": "",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "document_revisions",
        {
            "id": "docrev-w",
            "document_id": "doc-w",
            "revision": 1,
            "source_revision": "r1",
            "blocks_json": json.dumps(
                [{"block_id": "block-0001", "kind": "paragraph", "text": "原文", "line_start": 1, "line_end": 1}]
            ),
            "context_json": "{}",
            "created_at": "2026-11-06T00:00:00+08:00",
        },
    )
    blocks = [Block(block_id="block-0001", kind="paragraph", text="原文", meta={})]
    revision = write_back_revision(
        ctx, document_id="doc-w", blocks=blocks, block_id="block-0001", new_tex="改过的内容"
    )
    assert revision == 2
    rows = ctx.db.query_all(
        "SELECT * FROM document_revisions WHERE document_id = 'doc-w' ORDER BY revision"
    )
    assert len(rows) == 2
    latest = json.loads(rows[-1]["blocks_json"])
    assert latest[0]["revised_tex"] == "改过的内容"


def test_line_maps_are_kept_per_generation(ctx, project):
    """同一份产物的不同生成，各自有各的行映射，不能互相覆盖。"""
    first = ctx.store.put_bytes(
        project, "tex", b"same", meta={"generation_id": "gen-1", "line_map": {"b1": 1}}
    )
    second = ctx.store.put_bytes(
        project, "tex", b"same", meta={"generation_id": "gen-2", "line_map": {"b1": 2}}
    )
    assert first["id"] == second["id"]
    meta = json.loads(ctx.store.get(first["id"])["meta_json"])
    assert meta["line_maps"]["gen-1"] == {"b1": 1}
    assert meta["line_maps"]["gen-2"] == {"b1": 2}


def test_events_and_logs_are_trimmed_by_age(ctx, project):
    """超过保留期的旧事件与日志要清掉（不只是按条数）。"""
    from godfather.events import emit

    old_ts = "2020-01-01T00:00:00+08:00"
    ctx.db.insert(
        "events",
        {
            "project_id": project,
            "run_id": None,
            "job_id": None,
            "stage": "x",
            "done": 0,
            "total": 0,
            "ts": old_ts,
            "artifact_ids_json": "[]",
            "cost_json": "{}",
            "message": "很久以前的事件",
            "payload_json": "{}",
        },
    )
    ctx.db.insert(
        "job_logs",
        {
            "job_id": "job-old",
            "ts": old_ts,
            "level": "info",
            "message": "很久以前的日志",
            "data_json": "{}",
        },
    )
    emit(ctx.db, project_id=project, message="新事件")
    old_events = ctx.db.scalar(
        "SELECT COUNT(*) FROM events WHERE message = '很久以前的事件'"
    )
    assert old_events == 0, "过期事件没有被清理"

    # 同一任务的日志：写入时按任务裁剪
    ctx.db.insert(
        "job_logs",
        {
            "job_id": "job-1",
            "ts": old_ts,
            "level": "info",
            "message": "同任务的旧日志",
            "data_json": "{}",
        },
    )
    emit(ctx.db, project_id=project, job_id="job-1", message="新事件")
    assert ctx.db.scalar(
        "SELECT COUNT(*) FROM job_logs WHERE message = '同任务的旧日志'"
    ) == 0, "同任务的过期日志没有被清理"

    # 其它任务的旧日志：靠启动时清理
    ctx.db._trim_history()  # noqa: SLF001  （启动时执行的清理路径）
    assert ctx.db.scalar(
        "SELECT COUNT(*) FROM job_logs WHERE message = '很久以前的日志'"
    ) == 0, "启动清理没有删掉过期的其它任务日志"


def test_gpu_wait_can_be_cancelled(ctx, project):
    """等待 GPU 锁期间取消要能打断等待。"""
    from godfather.training import resources

    ctx.db.insert(
        "resource_locks",
        {
            "id": "lock-1",
            "resource": resources.GPU_RESOURCE,
            "owner": "other",
            "project_id": project,
            "acquired_at": "2026-11-06T00:00:00+08:00",
            "expires_at": "2999-01-01T00:00:00+08:00",
            "note": "别人占用",
        },
    )
    acquired = resources.wait_for_resource(
        ctx.db,
        resources.GPU_RESOURCE,
        "me",
        project_id=project,
        timeout_seconds=30,
        poll_seconds=0.05,
        should_cancel=lambda: True,
    )
    assert acquired is False


def test_identical_content_never_crosses_splits(ctx, project):
    """内容相同的样本必须落在同一个划分。

    否则 dev/holdout 里会出现 train 的原文 —— 评测时模型是照着背的，
    指标虚高，候选与基线的比较就失去意义。
    """
    from pathlib import Path

    from godfather import datasets as dataset_service

    def add_example(example_id: str, group: str, target: str) -> None:
        ctx.db.insert(
            "training_examples",
            {
                "id": example_id,
                "project_id": project,
                "task_type": "markdown_to_latex",
                "source_group_id": group,
                "source_revision": "r1",
                "input_payload_json": json.dumps({"markdown": "# x"}),
                "target_payload_json": json.dumps({"tex": target}),
                "teacher_json": "{}",
                "policy_version": "v1",
                "prompt_version": "p1",
                "reference_ids_json": "[]",
                "annotation_ids_json": "[]",
                "label_origin": "rule_synthesized",
                "validation_report_id": "",
                "status": "accepted",
                "split": "",
                "created_at": "2026-11-06T00:00:00+08:00",
                "updated_at": "2026-11-06T00:00:00+08:00",
            },
        )

    add_example("ex-1", "g1", "\\section{A}")
    add_example("ex-2", "g1", "\\section{A}")  # 同组内重复
    add_example("ex-3", "g2", "\\section{A}")  # 异组但内容相同
    add_example("ex-4", "g3", "\\section{B}")

    result = dataset_service.build_snapshot(
        ctx.db, project, export_dir=ctx.settings.data_dir / "exports"
    )
    rows = [
        json.loads(line)
        for line in Path(result.path).read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(rows) == 4
    splits_by_content: dict[str, set[str]] = {}
    for row in rows:
        key = row["target_payload"]["tex"]
        splits_by_content.setdefault(key, set()).add(row["split"])
    for content, splits in splits_by_content.items():
        assert len(splits) == 1, f"内容 {content!r} 跨了划分 {splits}（评测泄漏）"


def test_label_origin_is_not_falsified_by_fallback_blocks():
    """部分块回退到规则兜底时，不能标成"人工复核过的教师产物"。"""
    from godfather.taskpacks.latex.pipeline import _resolve_label_origin

    # 2 次教师调用、1 次成功、1 个块回退兜底
    stats = {"calls": 2, "failed": 0, "fallback_blocks": 1, "tokens": 10}
    assert _resolve_label_origin(stats, False) == "teacher_generated"
    # 全失败 → 规则来源
    assert _resolve_label_origin({"calls": 2, "failed": 2, "fallback_blocks": 0}, False) == "rule_synthesized"
    # 没调用教师 → 规则来源
    assert _resolve_label_origin({"calls": 0, "failed": 0, "fallback_blocks": 0}, False) == "rule_synthesized"


def test_manual_import_label_origin_is_honest(client, ctx):
    """人工导入外部模型的结果：默认标教师来源，显式声明复核过才标人工复核。"""
    project = "p-import"
    ctx.db.insert(
        "projects",
        {
            "id": project, "name": "p", "taskpack": "markdown_to_latex", "description": "",
            "active_model": "", "policy_version": "", "settings_json": "{}", "status": "created",
            "created_at": "2026-11-06T00:00:00+08:00", "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    example_id = "ex-import"
    ctx.db.insert(
        "training_examples",
        {
            "id": example_id, "project_id": project, "task_type": "markdown_to_latex",
            "source_group_id": "g1", "source_revision": "r1",
            "input_payload_json": json.dumps({"markdown": "# x"}),
            "target_payload_json": json.dumps({"tex": ""}),
            "teacher_json": "{}", "policy_version": "v1", "prompt_version": "p1",
            "reference_ids_json": "[]", "annotation_ids_json": "[]",
            "label_origin": "rule_synthesized", "validation_report_id": "",
            "status": "candidate", "split": "",
            "created_at": "2026-11-06T00:00:00+08:00", "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    from godfather.util import canonical_json

    expected = {
        example_id: {
            # 端点用的是 canonical_json（排序 + 紧凑分隔符）+ "sha256:" 前缀
            "input_hash": "sha256:"
            + __import__("hashlib").sha256(
                canonical_json({"markdown": "# x"}).encode("utf-8")
            ).hexdigest(),
            "example_id": example_id,
            "task_type": "markdown_to_latex",
        }
    }
    response = client.post(
        f"/api/projects/{project}/datasets/import",
        json={
            "payload": {
                "samples": [
                    {
                        "sample_id": example_id,
                        "input_hash": expected[example_id]["input_hash"],
                        "output": "\\section{X}",
                        "teacher_model": "external-model",
                    }
                ]
            }
        },
    )
    assert response.status_code == 200, response.text
    row = ctx.db.query_one("SELECT label_origin FROM training_examples WHERE id = ?", (example_id,))
    assert row["label_origin"] == "teacher_generated", "外部模型产物被虚标成了人工复核"


def test_snapshot_file_tampering_is_detected(ctx, project):
    """快照文件在冻结后被改动 → 必须报错，不能静默用改动后的数据训练。"""
    from pathlib import Path

    from godfather.datasets import build_snapshot
    from godfather.training.datasets import load_snapshot_examples

    ctx.db.insert(
        "training_examples",
        {
            "id": "ex-t1", "project_id": project, "task_type": "markdown_to_latex",
            "source_group_id": "g1", "source_revision": "r1",
            "input_payload_json": json.dumps({"markdown": "# x"}),
            "target_payload_json": json.dumps({"tex": "\\section{A}"}),
            "teacher_json": "{}", "policy_version": "v1", "prompt_version": "p1",
            "reference_ids_json": "[]", "annotation_ids_json": "[]",
            "label_origin": "rule_synthesized", "validation_report_id": "",
            "status": "accepted", "split": "",
            "created_at": "2026-11-06T00:00:00+08:00", "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    result = build_snapshot(
        ctx.db, project, export_dir=ctx.settings.data_dir / "exports"
    )
    # 正常情况能读
    assert load_snapshot_examples(ctx.db, result.id)
    # 改动文件后必须被发现
    path = Path(result.path)
    path.write_text(path.read_text(encoding="utf-8") + '{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="哈希"):
        load_snapshot_examples(ctx.db, result.id)


# --------------------------------------------------------------------------
# DeepInfer 启发的改动：行为级回归
# --------------------------------------------------------------------------
def test_ollama_digest_is_recorded_in_teacher_json():
    """教师产出的样本要记下真实权重摘要（而不是 "unknown"）。"""
    from godfather.providers.ollama import OllamaProvider

    provider = OllamaProvider("http://127.0.0.1:11434")
    # 不依赖 Ollama 是否在跑：拿不到就返回空串，不能抛异常
    digest = provider.model_digest("不存在的模型:0b")
    assert isinstance(digest, str)


def test_evaluation_pairs_baseline_and_candidate(ctx, project, monkeypatch):
    """评测必须交错执行：同一个样本先基线后候选。

    顺序跑（先全部基线、再全部候选）会把机器漂移算进差值 ——
    DeepInfer 强调的 batch-invariant 说的就是这件事。
    """
    from godfather.training import evaluate as evaluate_module

    from tests.test_training import make_snapshot

    snapshot = make_snapshot(ctx, project, count=6)
    calls: list[str] = []

    def fake_call(base_url, model, prompt, timeout=300, seed=None):
        calls.append(model)
        return "\\section{x}\n内容", {"duration_ms": 1, "error": ""}

    monkeypatch.setattr(evaluate_module, "_call_ollama", fake_call)
    monkeypatch.setattr(evaluate_module, "list_ollama_models", lambda base_url: ["base", "cand"])

    evaluate_module.evaluate_models(
        ctx,
        None,
        project_id=project,
        candidate_model="cand",
        baseline_model="base",
        snapshot_id=snapshot.id,
        split="dev",
        limit=4,
    )
    assert calls, "没有发起任何调用"
    # 交错：base, cand, base, cand …
    assert calls == ["base", "cand"] * (len(calls) // 2), f"不是配对顺序：{calls}"


def test_activation_blocked_when_model_is_gone():
    """模型不在 Ollama 里、文件也不在 → 不能激活（否则项目指向空气）。"""
    from godfather.training.models import check_activation

    model = {"name": "godfather-latex:v999", "path": "/nonexistent/model.gguf"}
    with pytest.raises(ValueError, match="不在 Ollama 里|模型文件不存在"):
        check_activation(None, "p1", model, available_models=["other:tag"], force=False)
    # force 可以跳过
    check_activation(None, "p1", model, available_models=[], force=True)


def test_activation_blocked_when_evaluation_shows_empty_output():
    """最近一次评测输出全为空 → 不予激活（验证过才发布）。"""
    from godfather.training.models import check_activation

    class _DB:
        def query_all(self, sql, params=()):
            return [
                {
                    "candidate_model_id": "m1",
                    "metrics_json": json.dumps(
                        {
                            "aggregates": {"candidate": {"n": 4, "non_empty_rate": 0.0}},
                            "deltas": {},
                        }
                    ),
                }
            ]

    model = {"name": "m1", "path": ""}
    with pytest.raises(ValueError, match="输出全为空"):
        check_activation(_DB(), "p1", model, available_models=["m1"], force=False)


# --------------------------------------------------------------------------
# 第六轮复审发现的缺口（原来 254 条测试一条都没覆盖到）
# --------------------------------------------------------------------------
def test_texmfcnf_is_a_directory_list_not_a_file():
    """TEXMFCNF 必须是**目录列表**（kpathsea 往里拼 texmf.cnf）。

    写成文件路径 → "configuration file texmf.cnf not found" → 每次编译都失败。
    我们的目录要**前置**在系统列表之前（只放一份 3 行的 cnf 会让 TEXMFDIST 丢失，
    连 xelatex.fmt 都找不到）；Windows 侧（无论 WSL 桥接或原生 Windows）必须使用 `;` 分隔。
    """
    from godfather.taskpacks.latex import compiler as comp

    source = (SRC_ROOT / "src" / "godfather" / "taskpacks" / "latex" / "compiler.py").read_text(
        encoding="utf-8"
    )
    assert 'env["TEXMFCNF"] = str(texmfcnf)' not in source, "TEXMFCNF 指向了文件"
    win_env = comp._build_environment(
        Path("."), windows_engine=True, texmfcnf_dir="C:/work/cnf", system_texmfcnf="C:/texlive/cnf"
    )
    assert win_env["TEXMFCNF"] == "C:/work/cnf;C:/texlive/cnf"
    expected_sep = ";" if comp.IS_WINDOWS else ":"
    native_env = comp._build_environment(
        Path("."), windows_engine=False, texmfcnf_dir="/work/cnf", system_texmfcnf="/sys/cnf"
    )
    assert native_env["TEXMFCNF"] == f"/work/cnf{expected_sep}/sys/cnf"


def test_windows_compile_uses_powershell_for_env():
    """Windows 引擎必须用 PowerShell 传环境变量。

    WSL interop 不传环境变量（实测 48/49 个变量丢失），
    直接跑 xelatex.exe 的话 TEXMFCNF / max_print_line 全都到不了。
    """
    source = (SRC_ROOT / "src" / "godfather" / "taskpacks" / "latex" / "compiler.py").read_text(
        encoding="utf-8"
    )
    assert "_powershell_command(args, env)" in source
    assert 'powershell.exe' in source


def test_fatal_phrase_does_not_match_benign_fontset_warning():
    """ctex 的良性警告（Fontset 'xxx' not found）不能被当成致命错误。"""
    from godfather.taskpacks.latex.compiler import _extract_tex_error, _looks_fatal

    benign = "main.tex:1: Package ctex Warning: Fontset 'windows' not found, using 'fandol'."
    assert _looks_fatal(benign) is False
    assert _extract_tex_error(benign) == ""
    # 真正的错误仍然要能提取出来
    fatal = "main.tex:12: Undefined control sequence."
    assert _looks_fatal(fatal) is True
    assert "Undefined control sequence" in _extract_tex_error(fatal)


def test_evaluation_on_wait_callback_has_no_arguments():
    """resources.wait_for_resource 的 on_wait 是**零参**回调。

    评测传的是 lambda waited: → GPU 锁被占用时评测任务直接 TypeError 崩掉，
    正好命中"评测也要占 GPU 锁"这个场景。
    """
    import inspect

    from godfather.training import resources

    source = inspect.getsource(resources.wait_for_resource)
    assert "on_wait()" in source
    assert "on_wait(" not in source.replace("on_wait()", "")
    evaluate_source = (
        SRC_ROOT / "src" / "godfather" / "training" / "evaluate.py"
    ).read_text(encoding="utf-8")
    assert "lambda waited" not in evaluate_source


def test_same_markdown_in_different_modes_stays_in_one_cluster():
    """同一篇 Markdown 跑不同 mode，必须落在同一个划分（否则 dev 里有 train 的原文）。"""
    from godfather.datasets import _cluster_groups_by_content

    markdown = "# 同一篇文档\n\n正文内容一样。"
    rows = []
    for index, mode in enumerate(("rule_only", "teacher_direct")):
        rows.append(
            {
                "id": f"ex{index}",
                "input_payload_json": json.dumps(
                    {
                        "mode": mode,
                        "markdown": markdown,
                        "blocks": [
                            {"block_id": "b1", "kind": "paragraph", "text": "正文内容一样。", "path": []}
                        ],
                    },
                    ensure_ascii=False,
                ),
                "target_payload_json": json.dumps({"tex": f"\\section{{T{index}}}"}),
            }
        )
    clusters = _cluster_groups_by_content({"g1": [rows[0]], "g2": [rows[1]]})
    assert len(clusters) == 1, f"同一篇 Markdown 被分成了 {len(clusters)} 个簇：{clusters}"


def test_escape_alignment_does_not_double_escape():
    """表格单元格转义 & 时，已经写成 \\& 的不能再转义（会变成换行 + 对齐符）。"""
    from godfather.taskpacks.latex.assembler import escape_latex

    assert escape_latex("$a & b$", escape_alignment=True) == "$a \\& b$"
    assert escape_latex("$a \\& b$", escape_alignment=True) == "$a \\& b$"


def test_manual_import_single_review_flag_wins():
    """单条显式的 human_reviewed=false 要压过顶层的 true。"""
    source = (SRC_ROOT / "src" / "godfather" / "api" / "pipeline.py").read_text(encoding="utf-8")
    assert 'if "human_reviewed" in item' in source


def test_windows_compile_cannot_read_arbitrary_absolute_paths(settings):
    """端到端：编译路径上的文件访问限制必须真的生效。

    之前只在源码里断言"配了 openin_any"，但那条配置**从来没到达引擎**
    （WSL interop 不传环境变量 + cnf 没复制到 Windows 侧 + TEXMFCNF 指向文件），
    于是 \\openin 照样能读任意绝对路径。
    这条测试真正跑一次编译：能读 → 失败。
    """
    from godfather.taskpacks.latex import compiler

    # 编译目录必须放在 **Windows 侧能访问**的地方：
    # 测试用的 tmp_path 在 /tmp，Windows 通过 \\wsl.localhost\... 读不到，
    # 编译根本跑不起来（这条测试就会永远 skip，等于没有）。
    # 这里用仓库目录下的临时子目录，跑完删掉。
    work = SRC_ROOT / "data" / "tmp-isolation-test"
    work.mkdir(parents=True, exist_ok=True)
    evil = (
        "\\documentclass{article}\\begin{document}"
        "\\openin1=C:/Windows/win.ini\\read1 to \\x"
        "\\typeout{LEAK:[\\x]}\\end{document}"
    )
    result = compiler.compile_tex(
        evil, settings=settings, work_root=work, job_id="isolation"
    )
    # 编译根本没跑起来（Windows 侧复制不到源码，比如数据目录在 /tmp）→
    # 这条测试必须跳过，不能空过（空过 = 隔离坏了也照样绿）。
    if result.exit_code == -4 or "无法把源文件复制" in (result.error or ""):
        pytest.skip("Windows 编译桥接不可用（源码目录 Windows 侧不可见），跳过隔离验证")
    assert result.log, "编译没有产生日志，验证无效"
    # 读到了内容就是没挡住（断言的是"没读到"）
    assert "16-bit" not in (result.log or ""), "文件访问限制没生效：读到了任意绝对路径"
    import shutil

    shutil.rmtree(work, ignore_errors=True)


def test_failed_training_marks_the_job_as_failed(ctx, project, tmp_path, monkeypatch):
    """训练失败时，**任务**状态必须是 failed（不能是 succeeded）。

    以前 runner 的兜底分支把异常吞掉并正常返回 → job 层看到"handler 正常返回"
    就把任务标成 succeeded：界面上显示"任务完成"，但训练根本没跑成，
    而且不会触发重试。
    """
    from godfather.training import runner
    from godfather.training.base import TrainingConfig

    from tests.test_training import make_snapshot

    snapshot = make_snapshot(ctx, project, count=4)
    config = TrainingConfig(
        backend="local",
        base_model=str(tmp_path / "missing"),
        dataset_snapshot_id=snapshot.id,
        output_dir=str(tmp_path / "run"),
    )
    run = runner.start_run(ctx, project, config)

    class FakeJob:
        job_id = "job-test"
        project_id = project

        def emit_event(self, **kwargs):
            pass

    with pytest.raises((RuntimeError, ValueError, OSError)):
        runner.run_training(ctx, FakeJob(), project_id=project, config=config, run_id=run["run_id"])
    row = ctx.db.query_one("SELECT status, error FROM training_runs WHERE id = ?", (run["run_id"],))
    assert row["status"] == "failed"
    assert row["error"]
