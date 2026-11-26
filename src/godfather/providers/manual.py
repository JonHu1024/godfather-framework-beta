"""人工导入 Provider（教师输出由用户在外部服务取得后导回）。

流程：
1. 导出任务包（JSON）：含 sample_id、输入哈希、schema、策略/提示词版本。
2. 用户在获授权的外部服务取得输出。
3. 导回 JSON：[{sample_id, input_hash, output, teacher_model, notes}]。
4. 导入时校验 sample_id 与 input_hash 对应关系与完整性；不匹配则拒绝该条。

费用状态固定为 unknown（无可读取的真实用量），用调用数与时长硬限。
"""
from __future__ import annotations

from .base import (
    COST_UNKNOWN,
    Capabilities,
    Provider,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)

REQUIRED_FIELDS = ("sample_id", "input_hash", "output")


class ManualImportProvider(Provider):
    name = "manual_import"
    protocol = "file"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            provider=self.name,
            name="人工导入教师输出",
            protocol=self.protocol,
            text=True,
            image=False,
            structured_output=True,
            context_limit=0,
            concurrency=1,
            usage_visible=False,
            tools=[],
            supports_cancel=False,
            notes="从任务包导出的 JSON 导回；无真实用量数据，费用显示为未知",
        )

    def healthcheck(self) -> tuple[bool, str]:
        return True, "人工导入模式随时可用（需要先导出任务包）"

    def estimate_cost(self, request: ProviderRequest) -> tuple[float, str]:
        return 0.0, COST_UNKNOWN

    def generate(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            ok=False,
            provider=self.name,
            error="人工导入模式不能直接调用；请使用任务包导出/导回流程",
            error_kind="manual_required",
            usage=ProviderUsage(cost_status=COST_UNKNOWN, usage_visible=False),
        )


def validate_import_payload(payload: dict) -> tuple[bool, list[str]]:
    """校验导回文件的结构。返回 (是否合法, 问题列表)。"""
    problems: list[str] = []
    if not isinstance(payload, dict):
        return False, ["顶层必须是对象"]
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        return False, ["缺少 samples 数组"]
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            problems.append(f"#{index}: 元素不是对象")
            continue
        for field in REQUIRED_FIELDS:
            if field not in sample or sample[field] in (None, ""):
                problems.append(f"#{index}: 缺少字段 {field}")
    return (not problems), problems


def match_samples(payload: dict, expected: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """把导回结果与期望样本匹配。

    expected: {sample_id: {"input_hash": ..., ...}}
    返回 (匹配成功列表, 失败列表)。
    """
    samples = (payload or {}).get("samples") or []
    matched, failed = [], []
    for sample in samples:
        sample_id = sample.get("sample_id")
        target = expected.get(sample_id)
        if target is None:
            failed.append({"sample": sample, "reason": "未知 sample_id"})
            continue
        if sample.get("input_hash") != target.get("input_hash"):
            failed.append(
                {
                    "sample": sample,
                    "reason": "input_hash 不匹配（输入已变化，需重新导出任务包）",
                    "expected": target.get("input_hash"),
                    "got": sample.get("input_hash"),
                }
            )
            continue
        matched.append({**target, **sample})
    return matched, failed
