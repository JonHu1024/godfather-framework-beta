"""示例外部热插拔 Cordis TaskPack 插件模板（文件名以下划线开头故默认不自动装载）。

如需体验，将其复制为 `plugins/demo_echo_taskpack.py` 后，在 Web 界面「Cordis 插件」页点击扫描挂载。
"""
from __future__ import annotations

from godfather.cordis import CordisContext, PluginMeta, TaskPackSpec

PLUGIN_META = PluginMeta(
    name="taskpack-demo-echo",
    display_name="自定义任务包模板（Demo Echo）",
    version="0.1.0",
    description="演示如何在 plugins/ 下热插拔新增自定义 SFT 任务包并随时重载代码。",
    kind="taskpack",
    inject=("taskpacks", "queue"),
)


def apply(ctx: CordisContext) -> None:
    taskpacks = ctx.inject("taskpacks")
    queue = ctx.inject("queue")

    spec = TaskPackSpec(
        id="demo_echo",
        name="自定义演示任务包",
        description="示例模板：仅展示插件挂载与队列热注册。",
        version="0.1.0",
        input_formats=(".txt", ".jsonl"),
        modes=[
            {"id": "rule_only", "label": "规则示例", "description": "快速验证挂载", "uses_teacher": False}
        ],
        system_prompt="你是一个精简输出助手。",
        default_job_kind="demo.generate",
    )

    ctx.effect(taskpacks.register(spec))
    ctx.effect(
        queue.register_disposable(
            "demo.generate",
            lambda job: {"ok": True, "echo": job.payload},
        )
    )
