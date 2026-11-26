"""M1 训练链路测试：数据准备、后端、运行器、导出、评测、模型版本。"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from godfather.training import backends as backend_registry
from godfather.training import models as model_registry
from godfather.training import resources as resource_service
from godfather.training.base import TrainingConfig
from godfather.training.datasets import (
    build_prompt,
    load_snapshot_examples,
    prepare_dataset,
)
from godfather.training.evaluate import _aggregate, evaluate_models, score_response


# --------------------------------------------------------------------------
# 辅助：构造一个可用的数据快照
# --------------------------------------------------------------------------
def make_snapshot(ctx, project, count: int = 6):
    """构造数据快照。

    切分按**源分组**进行（同一分组不跨集合），因此这里挑选能分别落进
    train / dev 的分组名，保证测试有可用的开发集。
    """
    from godfather.datasets import _group_split
    from godfather.util import canonical_json, new_id, now_iso

    seed = "test-1"
    wanted = {"train": count // 2, "dev": count - count // 2}
    groups: dict[str, list[str]] = {"train": [], "dev": []}
    index = 0
    while any(len(groups[s]) < wanted[s] for s in groups) and index < 500:
        name = f"doc-{index}"
        split = _group_split(name, seed, {"train": 0.8, "dev": 0.1, "holdout": 0.1})
        if split in groups and len(groups[split]) < wanted[split]:
            groups[split].append(name)
        index += 1

    # position 必须全局唯一：原来每个划分都从 0 开始编号，
    # 于是 train 的第 0 个样本和 dev 的第 0 个样本内容完全相同 ——
    # 内容聚类会把它们合并到同一个划分（正是防泄漏该做的事），
    # 这里的期望值也就跟着变了。
    position = 0
    for split, names in groups.items():
        for group in names:
            position += 1
            example_id = new_id("ex")
            ctx.db.insert(
                "training_examples",
                {
                    "id": example_id,
                    "project_id": project,
                    "task_type": "markdown_to_latex",
                    "source_group_id": group,
                    "source_revision": "sha256:x",
                    "input_payload_json": canonical_json(
                        {
                            "mode": "teacher_direct",
                            "markdown": f"# 标题 {position}\n\n正文 {position}",
                            "blocks": [
                                {"block_id": "block-0001", "kind": "heading", "text": f"标题 {position}", "path": ["标题"]},
                                {"block_id": "block-0002", "kind": "paragraph", "text": f"正文 {position} 的内容", "path": ["标题"]},
                            ],
                            "context": {"title": "示例文档"},
                        }
                    ),
                    "target_payload_json": canonical_json({"tex": f"\\section{{标题 {position}}}\n正文 {position} 的内容"}),
                    "teacher_json": canonical_json({"provider": "ollama", "model": "m", "revision": "unknown"}),
                    "policy_version": "policy-001",
                    "prompt_version": "prompt-001",
                    "reference_ids_json": "[]",
                    "annotation_ids_json": "[]",
                    "label_origin": "teacher_generated",
                    "validation_report_id": None,
                    "status": "accepted",
                    "split": split,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                },
            )
    from godfather.datasets import build_snapshot

    return build_snapshot(ctx.db, project, seed=seed, include_candidates=True)


# --------------------------------------------------------------------------
# 数据准备
# --------------------------------------------------------------------------
def test_prepare_dataset_writes_messages_and_manifest(ctx, project, tmp_path):
    snapshot = make_snapshot(ctx, project, count=6)
    prepared = prepare_dataset(ctx.db, snapshot.id, tmp_path)
    assert prepared.manifest["train_count"] == 3
    assert prepared.manifest["eval_count"] == 3
    assert prepared.manifest["train_count"] + prepared.manifest["eval_count"] == 6
    assert prepared.train_path.exists()
    records = [json.loads(line) for line in prepared.train_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records
    roles = [m["role"] for m in records[0]["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert (prepared.directory / "dataset_info.json").exists()
    assert (prepared.directory / "manifest.json").exists()


def test_prompt_contains_context_aligned_with_teacher(ctx, project, tmp_path):
    """§4.1：学生输入必须包含与教师对齐的上下文。"""
    snapshot = make_snapshot(ctx, project, count=2)
    examples = load_snapshot_examples(ctx.db, snapshot.id)
    prompt = build_prompt(examples[0])
    assert "文档标题" in prompt
    assert "所属章节" in prompt
    assert "待转换的 Markdown" in prompt


def test_dataset_info_registers_splits(ctx, project, tmp_path):
    snapshot = make_snapshot(ctx, project, count=4)
    prepared = prepare_dataset(ctx.db, snapshot.id, tmp_path)
    info = json.loads((prepared.directory / "dataset_info.json").read_text(encoding="utf-8"))
    assert "train" in info["identity"]
    assert "dev" in info["identity"]


# --------------------------------------------------------------------------
# 后端
# --------------------------------------------------------------------------
def test_local_backend_reports_missing_dependencies(monkeypatch):
    backend = backend_registry.LocalBackend()
    caps = backend.capabilities()
    assert caps.name == "local"
    assert isinstance(caps.available, bool)
    assert caps.device in {"cpu", "cuda"}


def test_local_backend_preflight_flags_missing_base_model(ctx, tmp_path):
    backend = backend_registry.LocalBackend()
    config = TrainingConfig(base_model=str(tmp_path / "nope"), dataset_snapshot_id="snap")
    checks = backend.preflight(config)
    base_check = next(c for c in checks if c.name == "基座模型")
    assert base_check.ok is False
    assert base_check.fatal is True


def test_local_backend_builds_command(ctx, tmp_path):
    backend = backend_registry.LocalBackend()
    config = TrainingConfig(base_model=str(tmp_path))
    command = backend.build_command(
        config,
        dataset_dir=tmp_path / "ds",
        output_dir=tmp_path / "out",
        config_path=tmp_path / "cfg.json",
    )
    assert "godfather.training.scripts.train_local" in command
    assert (tmp_path / "cfg.json").exists()


def test_local_backend_parses_step_lines():
    backend = backend_registry.LocalBackend()
    parsed = backend.parse_log_line('{"event": "step", "step": 10, "loss": 0.5}')
    assert parsed["metrics"]["loss"] == 0.5
    fatal = backend.parse_log_line('{"event": "fatal", "message": "boom"}')
    assert fatal["level"] == "error"


def test_llamafactory_backend_writes_yaml(ctx, tmp_path):
    backend = backend_registry.LlamaFactoryBackend()
    config = TrainingConfig(base_model=str(tmp_path), dataset_snapshot_id="snap", use_lora=True)
    backend.write_backend_config(
        config, tmp_path / "lf.yaml", tmp_path / "ds", tmp_path / "out"
    )
    text = (tmp_path / "lf.yaml").read_text(encoding="utf-8")
    assert "stage: sft" in text
    assert "lora_rank" in text


# --------------------------------------------------------------------------
# 运行器（用假后端跑真实流程）
# --------------------------------------------------------------------------
def test_runner_completes_and_records_metrics(ctx, project, tmp_path, monkeypatch):
    from godfather.training import runner
    from godfather.training.base import BackendCapabilities, PreflightCheck

    class FakeBackend:
        name = "fake"
        description = "测试后端"

        def capabilities(self):
            return BackendCapabilities(name="fake", available=True)

        def preflight(self, config):
            return [PreflightCheck(name="假后端", ok=True, detail="ok")]

        def build_command(self, config, *, dataset_dir, output_dir, config_path):
            output_dir.mkdir(parents=True, exist_ok=True)
            script = output_dir / "fake_train.py"
            script.write_text(
                "import json, sys\n"
                "print(json.dumps({'event': 'step', 'step': 1, 'loss': 0.25}))\n"
                "from pathlib import Path\n"
                "adapter = Path(sys.argv[1]) / 'adapter'\n"
                "adapter.mkdir(parents=True, exist_ok=True)\n"
                "(adapter / 'adapter_model.safetensors').write_text('x')\n"
                "(Path(sys.argv[1]) / 'train_metrics.json').write_text(json.dumps({'train_loss': 0.25}))\n"
                "print(json.dumps({'event': 'done', 'metrics': {'train_loss': 0.25}}))\n",
                encoding="utf-8",
            )
            return [sys.executable, str(script), str(output_dir)]

        def parse_log_line(self, line):
            return backend_registry.LocalBackend().parse_log_line(line)

        def env_for_run(self):
            return os.environ.copy()

    monkeypatch.setitem(backend_registry.BACKENDS, "fake", FakeBackend())
    snapshot = make_snapshot(ctx, project, count=6)
    config = TrainingConfig(
        backend="fake",
        base_model=str(tmp_path),
        dataset_snapshot_id=snapshot.id,
        output_dir=str(tmp_path / "run"),
    )
    run = runner.start_run(ctx, project, config)
    ctx.db.update("training_runs", run["run_id"], {"status": "running"})

    class FakeJob:
        job_id = "job-test"
        project_id = project

        def __init__(self):
            self.events = []

        def emit_event(self, **kwargs):
            self.events.append(kwargs)

    job = FakeJob()
    result = runner.run_training(ctx, job, project_id=project, config=config, run_id=run["run_id"])
    assert result["status"] == "succeeded"
    stored = ctx.db.query_one("SELECT * FROM training_runs WHERE id = ?", (run["run_id"],))
    assert stored["status"] == "succeeded"
    assert stored["adapter_path"]
    assert json.loads(stored["metrics_json"])["train_loss"] == 0.25
    # 资源锁应已释放
    assert resource_service.gpu_state(ctx.db)["locked"] is False


def test_runner_fails_honestly_when_backend_missing(ctx, project, tmp_path):
    from godfather.training import runner

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

    with pytest.raises(Exception) as excinfo:
        runner.run_training(ctx, FakeJob(), project_id=project, config=config, run_id=run["run_id"])
    assert "预检" in str(excinfo.value)


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------
def _make_fake_tool(tmp_path: Path, name: str, py_body: str) -> str:
    py_script = tmp_path / f"{name}.py"
    py_script.write_text(py_body, encoding="utf-8")
    if os.name == "nt":
        cmd = tmp_path / f"{name}.cmd"
        cmd.write_text(f'@echo off\r\n"{sys.executable}" "{py_script}" %*\r\n', encoding="utf-8")
        return str(cmd)
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\n{py_body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_export_pipeline_registers_model(ctx, project, tmp_path, monkeypatch):
    """用假的 llama.cpp / ollama 可执行文件走完整条导出链路。"""
    from godfather.training import export as export_module

    llama_dir = tmp_path / "llama.cpp"
    (llama_dir / "build" / "bin").mkdir(parents=True)
    (llama_dir / "convert_hf_to_gguf.py").write_text(
        "import sys\n"
        "out = sys.argv[sys.argv.index('--outfile') + 1]\n"
        "open(out, 'wb').write(b'GGUF')\n",
        encoding="utf-8",
    )
    quantize = _make_fake_tool(
        llama_dir / "build" / "bin",
        "llama-quantize",
        "import shutil, sys\nshutil.copyfile(sys.argv[1], sys.argv[2])\n",
    )
    assert quantize

    ollama = _make_fake_tool(
        tmp_path,
        "ollama",
        "import sys\nif len(sys.argv) > 3 and sys.argv[1] == 'create': print('created', sys.argv[3])\n",
    )

    monkeypatch.setattr(ctx.settings, "export", type(ctx.settings.export)())
    ctx.settings.export.llama_cpp_dir = str(llama_dir)
    ctx.settings.export.ollama_bin = ollama
    ctx.settings.export.model_name_prefix = "godfather-latex"

    # 假的 merge 脚本：在导出期望的目录里写出 merged/
    # （导出把 merged 放在本次运行目录下：<run_dir>/merged）
    def fake_build_command(self, config, *, dataset_dir, output_dir, config_path):
        output_dir.mkdir(parents=True, exist_ok=True)
        script = output_dir / "fake_merge.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            "merged = Path(sys.argv[1]) / 'merged'\n"
            "merged.mkdir(parents=True, exist_ok=True)\n"
            "(merged / 'model.safetensors').write_text('x')\n",
            encoding="utf-8",
        )
        return [sys.executable, str(script), str(output_dir)]

    monkeypatch.setattr(
        "godfather.training.scripts.merge_lora.main",
        lambda: 0,
        raising=False,
    )
    # 用真实的 merge 步骤替换为"已合并"的假实现
    original_merge = export_module._run

    def fake_run(command, **kwargs):
        if "merge_lora" in " ".join(command):
            # merge_config 里写着本次运行目录，merged 必须落在它下面
            config_path = command[command.index("--config") + 1]
            merged = Path(json.loads(Path(config_path).read_text(encoding="utf-8"))["merged_dir"])
            merged.mkdir(parents=True, exist_ok=True)
            (merged / "model.safetensors").write_text("x")
            return 0, "merged"
        if "convert_hf_to_gguf.py" in " ".join(command):
            outfile = command[command.index("--outfile") + 1]
            Path(outfile).write_bytes(b"GGUF")
            return 0, "converted"
        return original_merge(command, **kwargs)

    monkeypatch.setattr(export_module, "_run", fake_run)

    # 准备一次"成功"的训练运行
    run_id = "run-export"
    output_dir = tmp_path / "run"
    (output_dir / "adapter").mkdir(parents=True)
    (output_dir / "adapter" / "adapter_model.safetensors").write_text("x")
    ctx.db.insert(
        "training_runs",
        {
            "id": run_id,
            "project_id": project,
            "dataset_snapshot_id": None,
            "backend": "local",
            "base_model": str(tmp_path),
            "base_revision": "",
            "params_json": json.dumps({"base_model": str(tmp_path)}),
            "status": "succeeded",
            "log_path": "",
            "metrics_json": "{}",
            "adapter_path": str(output_dir / "adapter"),
            "checkpoint_path": str(output_dir),
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )

    class FakeJob:
        job_id = "job-export"
        project_id = project

        def emit_event(self, **kwargs):
            pass

    result = export_module.export_run(ctx, FakeJob(), project_id=project, run_id=run_id)
    assert result["status"] == "succeeded"
    assert result["name"].startswith("godfather-latex:")
    assert all(step["ok"] for step in result["steps"])
    models = model_registry.list_models(ctx.db, project)
    assert len(models) == 1
    assert models[0]["status"] == "candidate"
    assert models[0]["quantization"] == "q4_k_m"


def test_export_refuses_unfinished_run(ctx, project):
    from godfather.training import export as export_module

    ctx.db.insert(
        "training_runs",
        {
            "id": "run-failed",
            "project_id": project,
            "dataset_snapshot_id": None,
            "backend": "local",
            "base_model": "x",
            "base_revision": "",
            "params_json": "{}",
            "status": "failed",
            "log_path": "",
            "metrics_json": "{}",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    with pytest.raises(ValueError):
        export_module.export_run(ctx, None, project_id=project, run_id="run-failed")


# --------------------------------------------------------------------------
# 评测
# --------------------------------------------------------------------------
def test_score_response_metrics():
    example = {
        "input_payload": {
            "blocks": [
                {"text": "这是第一段正文内容"},
                {"text": "这是第二段正文内容"},
            ]
        }
    }
    score = score_response("\\section{标题}\n这是第一段正文内容", "\\section{标题}\n这是第一段正文内容", example)
    assert score["non_empty"] is True
    assert score["has_latex"] is True
    assert score["block_coverage"] == 0.5
    assert 0 <= score["similarity"] <= 1


def test_aggregate_reports_denominators_and_failures():
    records = [
        {"id": "a", "score": {"non_empty": True, "has_latex": True, "block_coverage": 1.0, "similarity": 0.9, "cjk_preserved": True}, "latency_ms": 10},
        {"id": "b", "score": {"non_empty": False, "has_latex": False, "block_coverage": None, "similarity": 0.0, "cjk_preserved": True}, "latency_ms": 20},
    ]
    aggregate = _aggregate(records)
    assert aggregate["n"] == 2
    assert aggregate["non_empty_rate"] == 0.5
    assert aggregate["coverage_denominator"] == 1
    assert len(aggregate["failures"]) == 1


def test_evaluate_models_reports_deltas(ctx, project, monkeypatch):
    from godfather.training import evaluate as evaluate_module

    snapshot = make_snapshot(ctx, project, count=6)
    monkeypatch.setattr(
        evaluate_module,
        "_call_ollama",
        lambda base_url, model, prompt, timeout=300, seed=None: (
            "\\section{标题}\n正文 0 的内容" if "candidate" in model else "",
            {"duration_ms": 5},
        ),
    )
    result = evaluate_models(
        ctx,
        None,
        project_id=project,
        candidate_model="candidate-model",
        baseline_model="baseline-model",
        snapshot_id=snapshot.id,
        split="dev",
        limit=3,
    )
    assert result["n_examples"] == 3
    assert result["aggregates"]["candidate"]["non_empty_rate"] == 1.0
    assert result["aggregates"]["baseline"]["non_empty_rate"] == 0.0
    assert result["deltas"]["non_empty_rate"] == 1.0
    stored = ctx.db.query_one("SELECT * FROM evaluation_runs WHERE id = ?", (result["evaluation_id"],))
    assert stored is not None


# --------------------------------------------------------------------------
# 模型版本与回滚
# --------------------------------------------------------------------------
def test_activate_and_rollback(ctx, project):
    for name in ("godfather-latex:v003", "godfather-latex:v004"):
        ctx.db.insert(
            "model_versions",
            {
                "id": f"mod-{name[-3:]}",
                "project_id": project,
                "name": name,
                "base_model": "base",
                "base_revision": "",
                "tokenizer_revision": "",
                "chat_template": "qwen",
                "quantization": "q4_k_m",
                "path": "",
                "status": "candidate",
                "metrics_json": "{}",
                "created_at": "2026-11-06T00:00:00+08:00",
            },
        )
    result = model_registry.activate(ctx.db, project, "mod-003")
    assert result["activated"] == "godfather-latex:v003"
    project_row = ctx.db.query_one("SELECT active_model FROM projects WHERE id = ?", (project,))
    assert project_row["active_model"] == "godfather-latex:v003"

    model_registry.activate(ctx.db, project, "mod-004")
    rolled = model_registry.rollback(ctx.db, project)
    assert rolled["activated"] == "godfather-latex:v003"
    project_row = ctx.db.query_one("SELECT active_model FROM projects WHERE id = ?", (project,))
    assert project_row["active_model"] == "godfather-latex:v003"


def test_rollback_without_history_fails(ctx, project):
    with pytest.raises(ValueError):
        model_registry.rollback(ctx.db, project)


# --------------------------------------------------------------------------
# 资源锁
# --------------------------------------------------------------------------
def test_resource_lock_is_exclusive(ctx, project):
    assert resource_service.acquire(ctx.db, "gpu", "trainer-1", project_id=project) is True
    assert resource_service.acquire(ctx.db, "gpu", "trainer-2", project_id=project) is False
    resource_service.release(ctx.db, "gpu", "trainer-1")
    assert resource_service.acquire(ctx.db, "gpu", "trainer-2", project_id=project) is True


def test_expired_lock_is_taken_over(ctx, project):
    resource_service.acquire(ctx.db, "gpu", "trainer-1", project_id=project)
    ctx.db.update("resource_locks", "gpu", {"expires_at": "2000-01-01T00:00:00+00:00"}, id_column="id")
    assert resource_service.acquire(ctx.db, "gpu", "trainer-2", project_id=project) is True


# --------------------------------------------------------------------------
# 训练脚本：标签掩码（回归：截断不能把答案切掉）
# --------------------------------------------------------------------------
def test_encode_pair_keeps_answer_tokens():
    """回归测试：序列超长时必须截断 prompt 开头，而不是把答案切掉。

    历史缺陷：整段文本从尾部截断，导致 answer 被切掉，训练"成功"但 loss 恒为 0。
    """
    pytest.importorskip("transformers")
    from godfather.training.scripts.train_local import encode_pair

    class FakeTokenizer:
        def __call__(self, text, add_special_tokens=False):
            return {"input_ids": list(range(len(text)))}

    tokenizer = FakeTokenizer()
    prompt = "p" * 200
    answer = "a" * 20
    input_ids, labels, truncated = encode_pair(tokenizer, prompt, answer, max_len=64)
    assert truncated is True
    assert len(input_ids) == 64
    supervised = [label for label in labels if label != -100]
    assert supervised, "答案 token 全部被截断，训练将没有任何监督信号"
    assert len(supervised) == 20


def test_encode_pair_masks_prompt():
    pytest.importorskip("transformers")
    from godfather.training.scripts.train_local import encode_pair

    class FakeTokenizer:
        def __call__(self, text, add_special_tokens=False):
            return {"input_ids": list(range(len(text)))}

    input_ids, labels, truncated = encode_pair(FakeTokenizer(), "prompt", "answer", max_len=64)
    assert truncated is False
    assert labels[:6] == [-100] * 6
    assert labels[6:] == [0, 1, 2, 3, 4, 5]  # answer 单独编码，token id 从 0 开始
