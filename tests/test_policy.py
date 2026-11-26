"""策略包与控制器测试。"""
from __future__ import annotations

from godfather.controller import actions as controller
from godfather.policy import engine as policy_engine

POLICY_YAML = """
version: policy-002
notes: 测试策略
preferences:
  - pref_id: pref-001
    content: 表格宽度不超过 0.9\\textwidth
    scope: project
    priority: 80
    confidence: 0.8
    rule:
      kind: require_package
      package: booktabs
  - pref_id: pref-002
    content: 定理使用 theorem 环境
    scope: document
    scope_value: doc-1
    priority: 60
    rule:
      kind: require_environment
      environment: theorem
prompt_template: "你是教师"
validator_config:
  accept_verdict: [pass, warn]
  fail_on_layers: [engineering]
"""


def test_parse_policy():
    package = policy_engine.parse_policy(POLICY_YAML)
    assert package.version == "policy-002"
    assert len(package.preferences) == 2
    assert package.preferences[0].pref_id == "pref-001"
    assert package.preferences[0].rule["package"] == "booktabs"
    assert package.validator_config["fail_on_layers"] == ["engineering"]


def test_save_and_load_policy(ctx, project):
    package = policy_engine.parse_policy(POLICY_YAML)
    saved = policy_engine.save_policy(ctx.db, project, package, status="active")
    loaded = policy_engine.load_active_policy(ctx.db, project)
    assert loaded.version == "policy-002"
    assert len(loaded.preferences) == 2
    assert saved["sha256"] == loaded.sha256


def test_scope_filtering():
    package = policy_engine.parse_policy(POLICY_YAML)
    project_prefs = policy_engine.applicable_preferences(package, scope="project")
    assert len(project_prefs) == 1
    doc_prefs = policy_engine.applicable_preferences(package, scope="document", scope_value="doc-1")
    assert [p.pref_id for p in doc_prefs] == ["pref-001", "pref-002"]
    other = policy_engine.applicable_preferences(package, scope="document", scope_value="doc-2")
    assert [p.pref_id for p in other] == ["pref-001"]


def test_conflict_detection():
    yaml_text = """
version: p
preferences:
  - pref_id: a
    content: A
    rule: {kind: require_package, key: font, value: serif}
  - pref_id: b
    content: B
    rule: {kind: require_package, key: font, value: sans}
"""
    package = policy_engine.parse_policy(yaml_text)
    conflicts = policy_engine.detect_conflicts(package.preferences)
    assert conflicts
    assert conflicts[0]["preferences"] == ["a", "b"]


def test_controller_rejects_unknown_action(ctx, project):
    outcome = controller.validate_action(ctx.db, project, {"summary": "x", "action": "rm_rf"})
    assert outcome.ok is False
    assert any("不允许的动作类型" in e for e in outcome.errors)


def test_controller_rejects_fake_evidence(ctx, project):
    outcome = controller.validate_action(
        ctx.db, project, {"summary": "x", "action": "propose_rule", "arguments": {"content": "y"}, "evidence_ids": ["nope"]}
    )
    assert outcome.ok is False
    assert any("证据 ID 不存在" in e for e in outcome.errors)


def test_controller_requires_content_for_rule(ctx, project):
    outcome = controller.validate_action(ctx.db, project, {"summary": "x", "action": "propose_rule", "arguments": {}})
    assert outcome.ok is False


def test_controller_executes_whitelisted_action(ctx, project):
    action = {
        "summary": "加一条表格样式偏好",
        "action": "propose_rule",
        "arguments": {"content": "使用 booktabs", "pref_id": "rule-001"},
        "evidence_ids": [],
    }
    outcome = controller.validate_action(ctx.db, project, action)
    assert outcome.ok is True
    result = controller.execute_action(ctx, project, outcome.normalized)
    assert result["status"] == "succeeded"
    assert result["layer"] == "policy"
    policies = ctx.db.query_all("SELECT * FROM policies WHERE project_id = ?", (project,))
    assert policies


def test_controller_never_runs_shell(ctx, project):
    """控制器不接受任何形式的命令执行字段。"""
    action = {
        "summary": "恶意",
        "action": "clarify",
        "arguments": {"question": "?", "command": "rm -rf /"},
        "evidence_ids": [],
    }
    outcome = controller.validate_action(ctx.db, project, action)
    assert outcome.ok is True
    result = controller.execute_action(ctx, project, outcome.normalized)
    # 只创建问题卡，不会执行命令
    assert result["status"] == "succeeded"
    assert "question_card_id" in result["result"]
