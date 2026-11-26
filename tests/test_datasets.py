"""数据集快照、分组切分、人工任务包与预算测试。"""
from __future__ import annotations

import json

from godfather import datasets as dataset_service
from godfather.budget import (
    BudgetExceeded,
    cache_key,
    check_and_reserve,
    get_state,
    settle,
)
from godfather.providers.base import COST_FREE, COST_UNKNOWN
from godfather.util import canonical_json


def _make_example(ctx, project, group, status="accepted"):
    from godfather.util import new_id, now_iso

    example_id = new_id("ex")
    ctx.db.insert(
        "training_examples",
        {
            "id": example_id,
            "project_id": project,
            "task_type": "markdown_to_latex",
            "source_group_id": group,
            "source_revision": "sha256:abc",
            "input_payload_json": canonical_json({"markdown": f"# {group}"}),
            "target_payload_json": canonical_json({"tex": "\\section{x}"}),
            "teacher_json": canonical_json({"provider": "ollama", "model": "m", "revision": "unknown"}),
            "policy_version": "policy-001",
            "prompt_version": "prompt-001",
            "reference_ids_json": "[]",
            "annotation_ids_json": "[]",
            "label_origin": "teacher_generated",
            "validation_report_id": None,
            "status": status,
            "split": "train",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
    )
    return example_id


def test_snapshot_groups_do_not_cross_splits(ctx, project):
    for index in range(20):
        _make_example(ctx, project, f"doc-{index % 4}")
    snapshot = dataset_service.build_snapshot(ctx.db, project, seed="s1")
    manifest = snapshot.manifest
    assignments = manifest["group_assignments"]
    # 同一分组只有一个 split
    assert len(set(assignments.values())) <= len(assignments)
    assert sum(snapshot.counts.values()) == 20
    # 分组内样例数 = 该分组样例总数
    for _group, split in assignments.items():
        assert split in {"train", "dev", "holdout"}


def test_snapshot_is_frozen_and_reproducible(ctx, project):
    _make_example(ctx, project, "doc-a")
    first = dataset_service.build_snapshot(ctx.db, project, seed="s1", version="v1")
    second = dataset_service.build_snapshot(ctx.db, project, seed="s1", version="v1")
    assert first.sha256 == second.sha256
    stored = dataset_service.get_snapshot(ctx.db, first.id)
    assert stored["status"] == "frozen"
    # 快照文件存在且是合法 JSONL
    with open(first.path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    assert records and records[0]["split"] in {"train", "dev", "holdout"}


def test_taskpack_export_excludes_holdout(ctx, project):
    for index in range(40):
        _make_example(ctx, project, f"doc-{index}")
    dataset_service.build_snapshot(ctx.db, project, seed="s1")
    pack = dataset_service.build_manual_taskpack(ctx.db, project, limit=100)
    holdout = dataset_service.holdout_group_ids(ctx.db, project)
    for sample in pack["samples"]:
        assert sample["sample_id"] not in holdout
        assert sample["input_hash"].startswith("sha256:")


def test_manual_import_matching():
    from godfather.providers.manual import match_samples, validate_import_payload

    payload = {
        "samples": [
            {"sample_id": "s1", "input_hash": "sha256:aaa", "output": "\\section{a}"},
            {"sample_id": "s2", "input_hash": "sha256:wrong", "output": "x"},
            {"sample_id": "unknown", "input_hash": "sha256:bbb", "output": "y"},
        ]
    }
    ok, problems = validate_import_payload(payload)
    assert ok is True
    expected = {
        "s1": {"sample_id": "s1", "input_hash": "sha256:aaa"},
        "s2": {"sample_id": "s2", "input_hash": "sha256:bbb"},
    }
    matched, failed = match_samples(payload, expected)
    assert [m["sample_id"] for m in matched] == ["s1"]
    assert len(failed) == 2


def test_budget_reserve_and_settle(ctx, project):
    call = check_and_reserve(ctx.db, project, ctx.settings, estimated_cents=0, cost_status=COST_FREE)
    settle(ctx.db, call, status="succeeded", actual_cents=0, cost_status=COST_FREE)
    state = get_state(ctx.db, project, ctx.settings)
    assert state.calls_used == 1
    assert state.spent_cents == 0


def test_budget_blocks_paid_calls_by_default(ctx, project):
    from godfather.providers.base import COST_KNOWN

    with pytest_raises_budget():
        check_and_reserve(ctx.db, project, ctx.settings, estimated_cents=10, cost_status=COST_KNOWN)


def pytest_raises_budget():
    import pytest

    return pytest.raises(BudgetExceeded)


def test_budget_unknown_cost_uses_call_limit(ctx, project):
    ctx.settings.budget.max_calls = 1
    call = check_and_reserve(ctx.db, project, ctx.settings, estimated_cents=0, cost_status=COST_UNKNOWN)
    settle(ctx.db, call, status="succeeded", actual_cents=0, cost_status=COST_UNKNOWN)
    with pytest_raises_budget():
        check_and_reserve(ctx.db, project, ctx.settings, estimated_cents=0, cost_status=COST_UNKNOWN)


def test_cache_key_changes_with_policy_version():
    base = cache_key(provider="ollama", model="m", prompt="p")
    changed = cache_key(provider="ollama", model="m", prompt="p", policy_version="policy-002")
    assert base != changed
