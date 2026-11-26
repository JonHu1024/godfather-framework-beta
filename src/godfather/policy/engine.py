"""策略包（policy）与确定性规则引擎。

策略包结构（设计文档 §8）：
    version, notes,
    preferences: [{pref_id, content, scope, scope_value, conditions, priority,
                   confidence, status, evidence, rule}],
    prompt_template, rubric, validator_config

偏好之间可能冲突：引擎显式报告冲突，不允许"最后一次生成悄悄覆盖"。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from ..db import Database
from ..util import canonical_json, new_id, now_iso, sha256_text

DEFAULT_PROMPT_TEMPLATE = """你是 Markdown→LaTeX 教师。输入包括 Markdown 结构块、全局文档上下文、模板限制、适用偏好、少量相关参考和验证错误。
只转换本次指定块，不改写原文事实、公式含义、编号含义或论证。不得补造缺失资源。
遵守允许的 LaTeX 环境和宏包范围；导言区由组装器统一管理。
返回 JSON：block_id、tex、required_assets、warnings、questions。tex 保存目标片段，不附加 Markdown 代码围栏。
跨块引用依赖提供的全局映射；发现缺失时记录问题，不猜编号。
修复时只修改有证据的问题，保留其他内容；审美评价服从明确策略，策略未明确则标明假设。
编译和内容检查由外部工具完成。不得声称你未实际执行的编译已经成功。"""

DEFAULT_VALIDATOR_CONFIG = {
    "layers": ["engineering", "content", "layout", "preference"],
    "fail_on_layers": ["engineering"],
    "accept_verdict": "pass",
}


@dataclass
class Preference:
    pref_id: str
    content: str
    scope: str = "project"
    scope_value: str = ""
    conditions: dict = field(default_factory=dict)
    priority: int = 50
    confidence: float = 0.5
    status: str = "active"
    evidence: list[str] = field(default_factory=list)
    rule: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pref_id": self.pref_id,
            "content": self.content,
            "scope": self.scope,
            "scope_value": self.scope_value,
            "conditions": self.conditions,
            "priority": self.priority,
            "confidence": self.confidence,
            "status": self.status,
            "evidence": self.evidence,
            "rule": self.rule,
        }


@dataclass
class PolicyPackage:
    version: str
    preferences: list[Preference] = field(default_factory=list)
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE
    rubric: dict = field(default_factory=dict)
    validator_config: dict = field(default_factory=lambda: dict(DEFAULT_VALIDATOR_CONFIG))
    notes: str = ""
    id: str = ""
    sha256: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "notes": self.notes,
            "prompt_template": self.prompt_template,
            "rubric": self.rubric,
            "validator_config": self.validator_config,
            "preferences": [p.to_dict() for p in self.preferences],
            "sha256": self.sha256,
        }

    def to_yaml(self) -> str:
        payload = {
            "version": self.version,
            "notes": self.notes,
            "preferences": [p.to_dict() for p in self.preferences],
            "prompt_template": self.prompt_template,
            "rubric": self.rubric,
            "validator_config": self.validator_config,
        }
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def parse_policy(text: str, version: str = "policy-001") -> PolicyPackage:
    data = yaml.safe_load(text) or {}
    preferences = []
    for index, raw in enumerate(data.get("preferences") or [], start=1):
        preferences.append(
            Preference(
                pref_id=str(raw.get("pref_id") or f"pref-{index:03d}"),
                content=str(raw.get("content") or ""),
                scope=str(raw.get("scope") or "project"),
                scope_value=str(raw.get("scope_value") or ""),
                conditions=raw.get("conditions") or {},
                priority=int(raw.get("priority", 50)),
                confidence=float(raw.get("confidence", 0.5)),
                status=str(raw.get("status") or "active"),
                evidence=list(raw.get("evidence") or []),
                rule=raw.get("rule") or {},
            )
        )
    package = PolicyPackage(
        version=str(data.get("version") or version),
        preferences=preferences,
        prompt_template=str(data.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE),
        rubric=data.get("rubric") or {},
        validator_config=data.get("validator_config") or dict(DEFAULT_VALIDATOR_CONFIG),
        notes=str(data.get("notes") or ""),
    )
    package.sha256 = sha256_text(package.to_yaml())
    return package


def save_policy(db: Database, project_id: str, package: PolicyPackage, status: str = "active", notes: str = "") -> dict:
    content_yaml = package.to_yaml()
    package.sha256 = sha256_text(content_yaml)
    existing = db.query_one(
        "SELECT * FROM policies WHERE project_id = ? AND version = ?", (project_id, package.version)
    )
    if existing:
        # 同版本号已存在：内容相同就复用，内容不同就另起一个版本号。
        # 原来直接覆盖，会把已经用过的历史版本改掉（策略是训练数据的依据，不能被静默篡改）。
        if existing["content_yaml"] == content_yaml:
            db.update(
                "policies",
                existing["id"],
                {
                    "status": status,
                    "notes": notes or existing.get("notes", ""),
                },
            )
            policy_id = existing["id"]
        else:
            suffix = 2
            while db.query_one(
                "SELECT id FROM policies WHERE project_id = ? AND version = ?",
                (project_id, f"{package.version}-{suffix}"),
            ):
                suffix += 1
            package.version = f"{package.version}-{suffix}"
            content_yaml = package.to_yaml()
            package.sha256 = sha256_text(content_yaml)
            policy_id = new_id("pol")
            db.insert(
                "policies",
                {
                    "id": policy_id,
                    "project_id": project_id,
                    "version": package.version,
                    "content_yaml": content_yaml,
                    "sha256": package.sha256,
                    "status": status,
                    "notes": notes,
                    "created_at": now_iso(),
                },
            )

    else:
        policy_id = new_id("pol")
        db.insert(
            "policies",
            {
                "id": policy_id,
                "project_id": project_id,
                "version": package.version,
                "content_yaml": content_yaml,
                "sha256": package.sha256,
                "status": status,
                "notes": notes or package.notes,
                "created_at": now_iso(),
            },
        )
    # 同一项目只能有一个 active 版本：
    # 否则两个版本都是 active，"启用历史版本"的按钮永远不会出现，
    # 用户也无法知道当前生效的到底是哪一个。
    if status == "active":
        db.execute(
            "UPDATE policies SET status = 'draft' WHERE project_id = ? AND status = 'active' AND id != ?",
            (project_id, policy_id),
        )
    db.execute("DELETE FROM policy_preferences WHERE policy_id = ?", (policy_id,))
    for pref in package.preferences:
        db.insert(
            "policy_preferences",
            {
                "id": new_id("pref"),
                "policy_id": policy_id,
                "pref_id": pref.pref_id,
                "content": pref.content,
                "scope": pref.scope,
                "scope_value": pref.scope_value,
                "conditions_json": canonical_json(pref.conditions),
                "priority": pref.priority,
                "confidence": pref.confidence,
                "status": pref.status,
                "evidence_json": canonical_json(pref.evidence),
                "conflicts_json": canonical_json([]),
                "created_at": now_iso(),
            },
        )
    db.update("projects", project_id, {"policy_version": package.version, "updated_at": now_iso()})
    return {"id": policy_id, "version": package.version, "sha256": package.sha256, "status": status}


def load_active_policy(db: Database, project_id: str) -> PolicyPackage:
    row = db.query_one(
        "SELECT * FROM policies WHERE project_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    if not row:
        row = db.query_one(
            "SELECT * FROM policies WHERE project_id = ? ORDER BY created_at DESC LIMIT 1", (project_id,)
        )
    if not row:
        return parse_policy("", "policy-default")
    return parse_policy(row["content_yaml"], row["version"])


def list_policies(db: Database, project_id: str) -> list[dict]:
    return db.query_all(
        "SELECT id, version, sha256, status, notes, created_at FROM policies WHERE project_id = ? "
        "ORDER BY created_at DESC",
        (project_id,),
    )


def applicable_preferences(
    package: PolicyPackage,
    *,
    scope: str = "project",
    scope_value: str = "",
    block_kind: str = "",
    document_id: str = "",
) -> list[Preference]:
    """按作用域筛选当前适用的偏好，按优先级排序。"""
    selected: list[Preference] = []
    for pref in package.preferences:
        if pref.status not in {"active", "confirmed"}:
            continue
        if pref.scope == "project":
            pass
        elif pref.scope == "document":
            if scope != "document" or (scope_value and pref.scope_value and pref.scope_value != scope_value):
                continue
        elif pref.scope == "block":
            if scope != "block" or (scope_value and pref.scope_value and pref.scope_value != scope_value):
                continue
        elif pref.scope == "block_kind":
            if pref.scope_value and pref.scope_value != block_kind:
                continue
        else:
            continue
        conditions = pref.conditions or {}
        if conditions.get("block_kind") and conditions["block_kind"] != block_kind:
            continue
        if conditions.get("document_id") and conditions["document_id"] != document_id:
            continue
        selected.append(pref)
    selected.sort(key=lambda p: (-p.priority, p.pref_id))
    return selected


def detect_conflicts(preferences: list[Preference]) -> list[dict]:
    """检测显式冲突：同一 rule.kind + 同一 key 但取值相反。"""
    conflicts: list[dict] = []
    grouped: dict[tuple[str, str], list[Preference]] = {}
    for pref in preferences:
        rule = pref.rule or {}
        kind = rule.get("kind")
        if not kind:
            continue
        key = rule.get("key") or kind
        grouped.setdefault((kind, key), []).append(pref)

    for (kind, key), group in grouped.items():
        if len(group) < 2:
            continue
        values = {canonical_json(p.rule.get("value")) for p in group}
        if len(values) > 1:
            conflicts.append(
                {
                    "kind": kind,
                    "key": key,
                    "preferences": [p.pref_id for p in group],
                    "values": [p.rule.get("value") for p in group],
                    "resolution": "需用户确认或按 priority+confidence 取最高",
                }
            )
    return conflicts


def render_preferences(preferences: list[Preference]) -> str:
    if not preferences:
        return "（当前没有适用于本范围的显式偏好；未明确之处请标注假设，不要臆造项目风格。）"
    lines = []
    for pref in preferences:
        scope = pref.scope if pref.scope == "project" else f"{pref.scope}:{pref.scope_value}"
        lines.append(f"- [{pref.pref_id}] ({scope}, 优先级 {pref.priority}, 置信度 {pref.confidence:.2f}) {pref.content}")
    return "\n".join(lines)


def build_teacher_prompt(
    package: PolicyPackage,
    *,
    block: dict,
    context: dict,
    preferences: list[Preference],
    reference_snippets: list[str] | None = None,
    validation_errors: list[str] | None = None,
    extra_instructions: str = "",
) -> str:
    """把策略包中的提示词模板与当前上下文拼装为教师输入。"""
    template = package.prompt_template or DEFAULT_PROMPT_TEMPLATE
    references = "\n".join(reference_snippets or []) or "（无）"
    errors = "\n".join(f"- {e}" for e in (validation_errors or [])) or "（无）"
    payload = {
        "block": canonical_json(block, ensure_ascii=False),
        "context": canonical_json(context, ensure_ascii=False),
        "preferences": render_preferences(preferences),
        "references": references,
        "validation_errors": errors,
        "extra_instructions": extra_instructions,
    }
    prompt = template + "\n\n---\n输入：\n" + payload["block"] + "\n\n全局上下文：\n" + payload["context"]
    prompt += f"\n\n适用偏好：\n{payload['preferences']}"
    prompt += f"\n\n相关参考：\n{payload['references']}"
    prompt += f"\n\n已知问题：\n{payload['validation_errors']}"
    if extra_instructions:
        prompt += f"\n\n额外要求：\n{extra_instructions}"
    prompt += "\n\n只输出 JSON：{\"block_id\": ..., \"tex\": ..., \"required_assets\": [], \"warnings\": [], \"questions\": []}"
    return prompt
