"""训练 API 的端到端测试（不依赖 GPU/训练依赖）。"""
from __future__ import annotations

from tests.test_training import make_snapshot


def test_backends_endpoint_reports_capabilities(client):
    response = client.get("/api/training/backends")
    assert response.status_code == 200
    data = response.json()
    names = {backend["name"] for backend in data["backends"]}
    assert {"local", "llamafactory"} <= names
    assert "resources" in data
    assert "quantize_methods" in data


def test_resources_endpoint(client):
    response = client.get("/api/training/resources")
    assert response.status_code == 200
    data = response.json()
    assert data["resource"] == "gpu"
    assert "ollama_models" in data


def test_create_run_requires_snapshot(client, project):
    response = client.post(
        f"/api/training/runs?project_id={project}",
        json={"dataset_snapshot_id": "missing", "base_model": "/tmp/x"},
    )
    assert response.status_code == 404


def test_create_run_rejects_unknown_backend(client, project, ctx):
    snapshot = make_snapshot(ctx, project, count=4)
    response = client.post(
        f"/api/training/runs?project_id={project}",
        json={"dataset_snapshot_id": snapshot.id, "base_model": "/tmp/x", "backend": "nope"},
    )
    assert response.status_code == 400


def test_create_run_registers_and_enqueues(client, project, ctx):
    snapshot = make_snapshot(ctx, project, count=6)
    response = client.post(
        f"/api/training/runs?project_id={project}",
        json={
            "dataset_snapshot_id": snapshot.id,
            "base_model": "/tmp/does-not-exist",
            "backend": "local",
            "num_train_epochs": 1,
            "max_steps": 2,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"]
    assert payload["job_id"]

    runs = client.get(f"/api/training/runs?project_id={project}").json()
    assert len(runs) == 1
    assert runs[0]["backend"] == "local"
    assert runs[0]["params"]["dataset_snapshot_id"] == snapshot.id
    assert runs[0]["params"]["seed"] == 42


def test_training_run_fails_honestly_without_dependencies(client, project, ctx):
    """没有训练依赖时必须失败，而不是假装成功。"""
    from tests.conftest import drain_jobs

    snapshot = make_snapshot(ctx, project, count=6)
    response = client.post(
        f"/api/training/runs?project_id={project}",
        json={
            "dataset_snapshot_id": snapshot.id,
            "base_model": "/tmp/does-not-exist",
            "backend": "local",
            "max_steps": 1,
        },
    )
    job_id = response.json()["job_id"]
    drain_jobs(client.app.state.ctx)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["error"]

    runs = client.get(f"/api/training/runs?project_id={project}").json()
    assert runs[0]["status"] == "failed"


def test_cancel_training_run(client, project, ctx):
    snapshot = make_snapshot(ctx, project, count=4)
    response = client.post(
        f"/api/training/runs?project_id={project}",
        json={"dataset_snapshot_id": snapshot.id, "base_model": "/tmp/x"},
    )
    run_id = response.json()["run_id"]
    cancelled = client.post(f"/api/training/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] == run_id
    runs = client.get(f"/api/training/runs?project_id={project}").json()
    assert runs[0]["status"] == "cancelled"


def test_export_requires_successful_run(client, project, ctx):
    ctx.db.insert(
        "training_runs",
        {
            "id": "run-nope",
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
    response = client.post(f"/api/training/runs/run-nope/export?project_id={project}", json={})
    assert response.status_code == 200  # 任务入队后在执行阶段失败
    job_id = response.json()["job_id"]

    from tests.conftest import drain_jobs

    drain_jobs(client.app.state.ctx)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert "导出" in (job["error"] or "")


def test_models_and_rollback_endpoints(client, project, ctx):
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
    listing = client.get(f"/api/training/models?project_id={project}").json()
    assert len(listing["models"]) == 2

    activated = client.post(f"/api/training/models/mod-003/activate?project_id={project}").json()
    assert activated["activated"] == "godfather-latex:v003"
    listing = client.get(f"/api/training/models?project_id={project}").json()
    assert listing["active_model"] == "godfather-latex:v003"

    client.post(f"/api/training/models/mod-004/activate?project_id={project}")
    rolled = client.post(f"/api/training/models/rollback?project_id={project}").json()
    assert rolled["activated"] == "godfather-latex:v003"


def test_evaluations_endpoint_requires_candidate(client, project):
    response = client.post(f"/api/training/evaluations?project_id={project}", json={"candidate_model": ""})
    assert response.status_code == 400


def test_evaluation_endpoint_runs_job(client, project, ctx, monkeypatch):
    from godfather.training import evaluate as evaluate_module

    snapshot = make_snapshot(ctx, project, count=8)
    monkeypatch.setattr(
        evaluate_module,
        "_call_ollama",
        lambda base_url, model, prompt, timeout=300, seed=None: ("\\section{x}\n内容", {"duration_ms": 3}),
    )
    response = client.post(
        f"/api/training/evaluations?project_id={project}",
        json={"candidate_model": "cand", "baseline_model": "base", "snapshot_id": snapshot.id, "limit": 2},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    from tests.conftest import drain_jobs

    drain_jobs(client.app.state.ctx)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded"
    evaluations = client.get(f"/api/training/evaluations?project_id={project}").json()
    assert evaluations
    assert evaluations[0]["metrics"]["n_examples"] >= 1
