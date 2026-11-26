"""API 端到端测试：导入 → 生成 → 批注 → 修订 → 数据集。"""
from __future__ import annotations

import io

from tests.conftest import drain_jobs

SAMPLE = """# 示例文档

这是第一段，包含公式 $a^2 + b^2 = c^2$。

## 第二节

- 条目一
- 条目二

| A | B |
| --- | --- |
| 1 | 2 |
"""


def test_project_lifecycle(client):
    response = client.post("/api/projects", json={"name": "测试", "taskpack": "markdown_to_latex"})
    assert response.status_code == 200
    project_id = response.json()["id"]

    assert client.get("/api/projects").status_code == 200
    detail = client.get(f"/api/projects/{project_id}").json()
    assert detail["name"] == "测试"
    assert client.get(f"/api/projects/{project_id}/overview").status_code == 200


def test_preflight_reports_environment(client):
    data = client.get("/api/system/preflight").json()
    assert "latex" in data
    assert "pandoc" in data
    assert "providers" in data
    # 没有 LaTeX 时必须如实报告不可用
    assert data["latex"]["ok"] in (True, False)
    if not data["latex"]["ok"]:
        assert "未找到 LaTeX 引擎" in data["latex"]["message"]


def test_upload_markdown_parses_document(client):
    project_id = client.post("/api/projects", json={"name": "测试"}).json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/materials",
        data={"kind": "markdown", "filename": "sample.md"},
        files={"file": ("sample.md", io.BytesIO(SAMPLE.encode("utf-8")), "text/markdown")},
    )
    assert response.status_code == 200
    payload = response.json()
    document_id = payload["document_id"]
    assert payload["parse"]["block_count"] > 0

    document = client.get(f"/api/documents/{document_id}").json()
    assert document["blocks"]
    assert document["context"]["headings"]


def test_generation_pipeline_and_dataset(client, settings):
    project_id = client.post("/api/projects", json={"name": "测试"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/materials",
        data={"kind": "markdown", "filename": "sample.md"},
        files={"file": ("sample.md", io.BytesIO(SAMPLE.encode("utf-8")), "text/markdown")},
    ).json()
    document_id = upload["document_id"]

    job = client.post(
        f"/api/projects/{project_id}/generations",
        json={"document_id": document_id, "mode": "pandoc_baseline", "provider": "ollama"},
    )
    assert job.status_code == 200
    job_id = job.json()["job_id"]

    app_ctx = client.app.state.ctx
    drain_jobs(app_ctx)

    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["status"] in {"succeeded", "failed"}

    generations = client.get(f"/api/projects/{project_id}/generations").json()
    assert generations
    generation = client.get(f"/api/generations/{generations[0]['id']}").json()
    # 四层检查报告齐全
    layers = {report["layer"] for report in generation["validation_reports"]}
    assert layers == {"engineering", "content", "layout", "preference"}


def test_annotation_requires_matching_revision(client):
    project_id = client.post("/api/projects", json={"name": "测试"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/materials",
        data={"kind": "markdown", "filename": "sample.md"},
        files={"file": ("sample.md", io.BytesIO(SAMPLE.encode("utf-8")), "text/markdown")},
    ).json()
    artifact_id = upload["artifact"]["id"]
    revision = upload["artifact"]["revision"]

    bad = client.post(
        f"/api/projects/{project_id}/annotations",
        json={
            "artifact_id": artifact_id,
            "artifact_revision": "sha256:deadbeef",
            "comment": "这里太挤",
        },
    )
    assert bad.status_code == 409

    good = client.post(
        f"/api/projects/{project_id}/annotations",
        json={
            "artifact_id": artifact_id,
            "artifact_revision": revision,
            "document_id": upload["document_id"],
            "page_index": 0,
            "bbox": [0.1, 0.2, 0.4, 0.3],
            "comment": "这里的定理不要断开到下一页",
            "category": "layout",
            "source_block_candidates": ["block-0001"],
            "mapping_confidence": "confirmed",
        },
    )
    assert good.status_code == 200

    listed = client.get(f"/api/projects/{project_id}/annotations").json()
    assert len(listed) == 1
    assert listed[0]["bbox"] == [0.1, 0.2, 0.4, 0.3]


def test_accept_annotation_without_blocks_is_rejected(client):
    project_id = client.post("/api/projects", json={"name": "测试"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/materials",
        data={"kind": "markdown", "filename": "sample.md"},
        files={"file": ("sample.md", io.BytesIO(SAMPLE.encode("utf-8")), "text/markdown")},
    ).json()
    created = client.post(
        f"/api/projects/{project_id}/annotations",
        json={
            "artifact_id": upload["artifact"]["id"],
            "artifact_revision": upload["artifact"]["revision"],
            "document_id": upload["document_id"],
            "comment": "这里太挤",
        },
    ).json()
    response = client.post(f"/api/annotations/{created['id']}/accept")
    assert response.status_code == 400
    assert "mapping_confidence" in response.json()["detail"]


def test_dataset_snapshot_endpoint(client):
    project_id = client.post("/api/projects", json={"name": "测试"}).json()["id"]
    response = client.post(f"/api/projects/{project_id}/datasets/snapshots", json={"seed": "abc"})
    assert response.status_code == 200
    payload = response.json()
    assert "counts" in payload
    assert payload["manifest"]["holdout_excluded_from_prompt"] is True


def test_events_history_available(client):
    project_id = client.post("/api/projects", json={"name": "测试"}).json()["id"]
    client.post(
        f"/api/projects/{project_id}/materials",
        data={"kind": "markdown", "filename": "sample.md"},
        files={"file": ("sample.md", io.BytesIO(SAMPLE.encode("utf-8")), "text/markdown")},
    ).json()
    history = client.get(f"/api/events/history?project_id={project_id}").json()
    assert isinstance(history, list)
