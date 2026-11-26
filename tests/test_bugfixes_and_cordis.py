"""BUG-01..BUG-10 修复验证、Cordis 热插拔内核、远程 API 接入与通用 SFT 任务包回归测试。"""
from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

from godfather.context import AppContext
from godfather.cordis import CordisKernel, PluginMeta
from godfather.providers.base import COST_KNOWN, COST_UNKNOWN, ProviderRequest
from godfather.providers.openai_compatible import OpenAICompatibleProvider
from godfather.taskpacks.latex.assembler import render_block
from godfather.taskpacks.latex.compiler import _build_environment
from godfather.taskpacks.latex.mdparse import Block, parse_markdown
from godfather.taskpacks.latex.pipeline import parse_json_object, sanitize_teacher_tex
from godfather.taskpacks.latex.validators import check_content, check_dangerous_commands
from godfather.training.base import TrainingBackend
from tests.conftest import drain_jobs


# --------------------------------------------------------------------------
# BUG-01 & BUG-02: Windows 环境变量与 UTF-8 保护
# --------------------------------------------------------------------------
def test_bug01_windows_compiler_environment_preserves_essential_vars(monkeypatch):
    from godfather.taskpacks.latex import compiler as comp

    monkeypatch.setattr(comp, "IS_WINDOWS", True)
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\Alice")
    monkeypatch.setenv("TEMP", "C:\\Users\\Alice\\AppData\\Local\\Temp")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    env = _build_environment(
        Path("C:/work/compile/j1"),
        windows_engine=False,
        texmfcnf_dir="C:/work/cnf",
        system_texmfcnf="C:/texlive/cnf",
    )
    assert env["USERPROFILE"] == "C:\\Users\\Alice"
    assert env["TEMP"] == "C:\\Users\\Alice\\AppData\\Local\\Temp"
    assert env["TEXMFCNF"] == "C:/work/cnf;C:/texlive/cnf"
    assert "HOME" not in env
    assert "HTTP_PROXY" not in env


def test_bug02_training_env_sets_utf8_and_preserves_windows_vars(monkeypatch):
    monkeypatch.setenv("TEMP", "C:\\Temp")
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    env = TrainingBackend.env_for_run()
    assert env["PYTHONUTF8"] == "1"
    assert env["TEMP"] == "C:\\Temp"
    assert env["SystemRoot"] == "C:\\Windows"


# --------------------------------------------------------------------------
# BUG-04 & BUG-05 & BUG-07: Math 组装、Markdown 公式内行首符号、Pandoc 列表白名单
# --------------------------------------------------------------------------
def test_bug04_math_block_avoids_double_equation_and_duplicate_labels():
    block = Block(block_id="block-0003", kind="math", text="E = mc^2", meta={"labels": ["eq:mc2"]})

    # 情形 1：教师直接输出了 \begin{equation}...\label{...}\end{equation}
    tex1, _, _ = render_block(block, "\\begin{equation}\nE = mc^2 \\label{eq:mc2}\n\\end{equation}")
    assert tex1.count("\\begin{equation}") == 1
    assert tex1.count("\\label{") == 1

    # 情形 2：教师输出了 \begin{align*}...\end{align*} 或 $$...$$
    tex2, _, _ = render_block(block, "\\begin{align}\na &= 1 \\\\\nb &= 2\n\\end{align}")
    assert "\\begin{equation}" not in tex2
    assert tex2.count("\\begin{align}") == 1
    assert "\\label{eq:mc2}" in tex2

    tex3, _, _ = render_block(block, "$$\nx^2 + y^2 = z^2\n$$")
    assert "$$" not in tex3
    assert tex3.count("\\begin{equation}") == 1


def test_bug05_mdparse_keeps_plus_minus_inside_display_math():
    md = "# 测试\n\n$$\na = 1\n+ b\n- c\n> 0\n$$\n\n正文。\n"
    parsed = parse_markdown(md)
    kinds = [b.kind for b in parsed.blocks]
    assert kinds == ["heading", "math", "paragraph"]
    assert "+ b" in parsed.blocks[1].text
    assert "- c" in parsed.blocks[1].text
    assert "> 0" in parsed.blocks[1].text


def test_bug07_pandoc_enum_definitions_not_flagged_as_dangerous():
    tex = (
        "\\documentclass{article}\n\\begin{document}\n"
        "\\begin{enumerate}\n\\def\\labelenumi{\\arabic{enumi}.}\n\\item 第一条\n\\end{enumerate}\n"
        "\\end{document}\n"
    )
    assert check_dangerous_commands(tex) == []

    # 真正的自定义宏仍应报警
    bad = "\\documentclass{article}\n\\begin{document}\n\\def\\hack{1}\n\\end{document}\n"
    assert any(f["code"] == "dangerous_definition" for f in check_dangerous_commands(bad))


# --------------------------------------------------------------------------
# BUG-08: Thinking 剥离、严重截断检测、多选源块批注修订、删除批注
# --------------------------------------------------------------------------
def test_bug08_thinking_strip_and_content_truncation_checks():
    raw = "<think>我先思考一下怎么写</think>\n```json\n{\"tex\": \"\\\\textbf{好}\", \"warnings\": []}\n```"
    parsed = parse_json_object(raw)
    assert parsed["tex"] == "\\textbf{好}"

    unclosed = "正文片段\n<think>超长推理未闭合"
    cleaned = sanitize_teacher_tex(unclosed)
    assert "<think>" not in cleaned
    assert cleaned == "正文片段"

    long_source = "# 章节\n\n" + "\n\n".join(f"这是第 {i} 段足够长的中文技术测试正文内容。" for i in range(6))
    blocks = parse_markdown(long_source).blocks
    report = check_content(blocks=blocks, tex_text="\\section{章节}\n极短。", source_text=long_source)
    codes = {f["code"] for f in report["findings"]}
    assert "paragraphs_heavily_missing" in codes or "content_severely_truncated" in codes


def test_bug08_multiblock_annotation_and_accept_flow(client, ctx, monkeypatch):
    project_id = client.post("/api/projects", json={"name": "多块修订测试"}).json()["id"]
    md = "# 标题\n\n第一段测试正文内容。\n\n第二段公式说明内容。\n"
    upload = client.post(
        f"/api/projects/{project_id}/materials",
        data={"kind": "markdown", "filename": "doc.md"},
        files={"file": ("doc.md", io.BytesIO(md.encode("utf-8")), "text/markdown")},
    ).json()

    # 1. 创建一条处于 needs_review 且带有 1 个候选块的批注：直接 accept 必须拒绝（BUG-08）
    ann = client.post(
        f"/api/projects/{project_id}/annotations",
        json={
            "artifact_id": upload["artifact"]["id"],
            "artifact_revision": upload["artifact"]["revision"],
            "document_id": upload["document_id"],
            "comment": "把第二段和第三段合并规范表达",
            "source_block_candidates": ["block-0002"],
            "mapping_confidence": "needs_review",
        },
    ).json()
    blocked = client.post(f"/api/annotations/{ann['id']}/accept")
    assert blocked.status_code == 400
    assert "needs_review" in blocked.json()["detail"]

    # 2. 用假教师返回多块修订 JSON，并通过 accept 接口传入 block_ids 多选确认 + 触发修订
    class MockTeacher:
        name = "ollama"

        def capabilities(self):
            from godfather.providers.base import Capabilities

            return Capabilities(provider="ollama", name="mock")

        def healthcheck(self):
            return True, "ok"

        def estimate_cost(self, request):
            from godfather.providers.base import COST_FREE

            return 0.0, COST_FREE

        def generate(self, request):
            from godfather.providers.base import COST_FREE, ProviderResult, ProviderUsage

            return ProviderResult(
                ok=True,
                provider="ollama",
                model="mock-teacher",
                content=json.dumps(
                    {
                        "blocks": {
                            "block-0002": "第一段修订后的中文测试内容。",
                            "block-0003": "第二段修订后的官方公式说明。",
                        },
                        "warnings": [],
                    },
                    ensure_ascii=False,
                ),
                usage=ProviderUsage(output_tokens=42, cost_status=COST_FREE, usage_visible=True),
            )

    app_ctx: AppContext = client.app.state.ctx
    monkeypatch.setitem(app_ctx.registry._providers, "ollama", MockTeacher())

    # 同步执行时将队列提交在测试客户端内排干
    res = client.post(
        f"/api/annotations/{ann['id']}/accept",
        json={"block_ids": ["block-0002", "block-0003"], "provider": "ollama", "model": "mock-teacher"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["block_ids"] == ["block-0002", "block-0003"]
    drain_jobs(app_ctx)

    job_row = app_ctx.queue.get(body["job_id"])
    assert job_row["status"] == "succeeded"
    result_payload = json.loads(job_row["result_json"])
    assert result_payload["block_ids"] == ["block-0002", "block-0003"]

    # 文档最新版本中两块均应写入 revised_tex
    doc_detail = client.get(f"/api/documents/{upload['document_id']}").json()
    by_id = {b["block_id"]: b for b in doc_detail["blocks"]}
    assert by_id["block-0002"]["revised_tex"] == "第一段修订后的中文测试内容。"
    assert by_id["block-0003"]["revised_tex"] == "第二段修订后的官方公式说明。"

    # 3. 补测 DELETE /api/annotations/{id}
    ann2 = client.post(
        f"/api/projects/{project_id}/annotations",
        json={
            "artifact_id": upload["artifact"]["id"],
            "artifact_revision": upload["artifact"]["revision"],
            "document_id": upload["document_id"],
            "comment": "待删除测试",
        },
    ).json()
    assert client.delete(f"/api/annotations/{ann2['id']}").status_code == 200


# --------------------------------------------------------------------------
# BUG-03: OpenAICompatibleProvider + 模型接入 API + 局域网权限隔离
# --------------------------------------------------------------------------
def test_bug03_openai_compatible_provider_and_models_access_api(client, monkeypatch):
    # 1. Mock httpx.get / httpx.post 验证 OpenAICompatibleProvider（含 <think> 剥离与计费）
    import httpx

    class FakeResp:
        def __init__(self, code, data):
            self.status_code = code
            self._data = data
            self.text = json.dumps(data)

        def json(self):
            return self._data

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, headers=None, timeout=None: FakeResp(200, {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}),
    )
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, headers=None, json=None, timeout=None: FakeResp(
            200,
            {
                "id": "cmp-1",
                "model": "deepseek-reasoner",
                "choices": [{"message": {"content": "<think>思考中</think>OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            },
        ),
    )

    prov = OpenAICompatibleProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="sk-secret-12345678",
        default_model="deepseek-reasoner",
        pricing_known=False,
    )
    ok, msg = prov.healthcheck()
    assert ok is True
    assert prov.list_models() == ["deepseek-chat", "deepseek-reasoner"]
    res = prov.generate(ProviderRequest(prompt="ping"))
    assert res.ok is True
    assert res.content == "OK"
    assert res.raw["thinking_stripped"] is True
    assert res.usage.cost_status == COST_UNKNOWN

    paid_prov = OpenAICompatibleProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="sk-secret",
        default_model="deepseek-chat",
        pricing_known=True,
        input_price_per_1m_cents=100.0,
        output_price_per_1m_cents=200.0,
    )
    res2 = paid_prov.generate(ProviderRequest(prompt="ping"))
    assert res2.usage.cost_status == COST_KNOWN
    assert res2.usage.cost_cents > 0

    # 2. 通过 /api/models-access 保存远程 API，校验明文 api_key 永不回显
    put_resp = client.put(
        "/api/models-access",
        json={
            "default_teacher_provider": "openai_compatible",
            "openai_compatible": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-topsecret-abcdef",
                "teacher_model": "deepseek-chat",
            },
            "budget": {"paid_calls_enabled": True, "max_calls": 50},
        },
    )
    assert put_resp.status_code == 200
    body = put_resp.json()
    oa = body["providers"]["openai_compatible"]
    assert oa["enabled"] is True
    assert oa["has_api_key"] is True
    assert "api_key" not in oa
    assert "topsecret-abcdef" not in oa["api_key_masked"]

    # 3. 远程局域网配对会话访问 /api/models-access 与 /api/plugins 修改端点必须被 403 拦截
    proj_id = client.post("/api/projects", json={"name": "LAN隔离"}).json()["id"]
    pairing = client.app.state.pairing
    pairing.enabled = True
    code = pairing.issue_code(proj_id)
    remote = TestClient(client.app, client=("192.168.1.50", 54321))
    session = remote.post("/api/system/pairing/redeem", json={"code": code}).json()
    headers = {"x-godfather-session": session["token"]}
    assert remote.get("/api/models-access", headers=headers).status_code == 403
    assert remote.post("/api/plugins/discover", headers=headers).status_code == 403
    pairing.enabled = False


# --------------------------------------------------------------------------
# BUG-09 & BUG-10: 快照比例试算 + 参考样例上下文注入
# --------------------------------------------------------------------------
def test_bug09_and_bug10_snapshot_preview_and_reference_snippets(client, ctx):
    project_id = client.post("/api/projects", json={"name": "快照与参考"}).json()["id"]
    # 上传参考 md/tex
    client.post(
        f"/api/projects/{project_id}/materials",
        data={"kind": "reference_md", "filename": "ref1.md"},
        files={"file": ("ref1.md", io.BytesIO(b"# Sample Title\nRef paragraph"), "text/markdown")},
    )
    client.post(
        f"/api/projects/{project_id}/materials",
        data={"kind": "reference_tex", "filename": "ref1.tex"},
        files={"file": ("ref1.tex", io.BytesIO(b"\\section{Sample Title}"), "application/x-tex")},
    )
    from godfather.taskpacks.latex.pipeline import _load_reference_snippets

    snippets = _load_reference_snippets(client.app.state.ctx, project_id)
    assert len(snippets) == 2

    # 试算接口 + 非法比例校验
    prev = client.post(
        f"/api/projects/{project_id}/datasets/snapshots/preview",
        json={"seed": "s1", "ratios": {"train": 0.7, "dev": 0.2, "holdout": 0.1}},
    )
    assert prev.status_code == 200
    assert prev.json()["ratios"]["dev"] == 0.2

    bad = client.post(
        f"/api/projects/{project_id}/datasets/snapshots/preview",
        json={"seed": "s1", "ratios": {"train": 0, "dev": 0, "holdout": 0}},
    )
    assert bad.status_code == 400


# --------------------------------------------------------------------------
# Cordis 微内核热插拔生命周期 + 通用 SFT 任务包闭环
# --------------------------------------------------------------------------
def test_cordis_kernel_reactive_inject_and_external_hot_reload(tmp_path):
    kernel = CordisKernel()
    events: list[str] = []

    def plugin_consumer(ctx):
        svc = ctx.inject("greeter")
        events.append(f"mounted:{svc()}")
        return lambda: events.append("disposed:consumer")

    fiber = kernel.mount(
        plugin_consumer,
        name="consumer",
        meta=PluginMeta(name="consumer", inject=("greeter",)),
    )
    assert fiber.state == "pending"

    # 提供 greeter 服务后自动解锁挂起中的 consumer
    def plugin_provider(ctx):
        ctx.provide("greeter", lambda: "hi")

    kernel.mount(plugin_provider, name="provider")
    assert fiber.state == "active"
    assert "mounted:hi" in events

    # 停用 provider 会级联销毁 consumer（归位为 pending）并触发效果回收
    kernel.disable("provider")
    assert fiber.state == "pending"
    assert "disposed:consumer" in events

    # 测试目录外部插件动态发现与代码修改后的热重载
    plug_dir = tmp_path / "plugins"
    plug_dir.mkdir()
    plug_file = plug_dir / "custom_pack.py"
    plug_file.write_text(
        "PLUGIN_META = {'name': 'custom-demo', 'version': '1.0'}\n"
        "def apply(ctx):\n"
        "    ctx.provide('demo_val', 'v1')\n",
        encoding="utf-8",
    )
    discovered = kernel.discover_directory(plug_dir)
    assert len(discovered) == 1
    assert kernel.get_service("demo_val") == "v1"

    plug_file.write_text(
        "PLUGIN_META = {'name': 'custom-demo', 'version': '2.0'}\n"
        "def apply(ctx):\n"
        "    ctx.provide('demo_val', 'v2')\n",
        encoding="utf-8",
    )
    reloaded = kernel.reload("custom-demo")
    assert reloaded.meta.version == "2.0"
    assert kernel.get_service("demo_val") == "v2"
    kernel.shutdown()


def test_general_sft_taskpack_end_to_end(client):
    # 查询 taskpacks 接口应同时包含 markdown_to_latex 与 general_sft
    packs = client.get("/api/taskpacks").json()["taskpacks"]
    ids = {p["id"] for p in packs}
    assert {"markdown_to_latex", "general_sft"} <= ids

    # 创建通用 SFT 项目并导入 JSONL 语料
    prj = client.post("/api/projects", json={"name": "客服SFT", "taskpack": "general_sft"}).json()
    jsonl = (
        '{"instruction": "如何退款？", "output": "请在订单页点击申请退款。"}\n'
        '{"instruction": "如何修改密码？", "output": "前往账户安全设置页更新密码。"}\n'
    ).encode("utf-8")
    up = client.post(
        f"/api/projects/{prj['id']}/materials",
        data={"kind": "dataset", "filename": "qa.jsonl"},
        files={"file": ("qa.jsonl", io.BytesIO(jsonl), "application/jsonl")},
    )
    assert up.status_code == 200
    doc_id = up.json()["document_id"]
    assert up.json()["parse"]["block_count"] == 2

    # 触发 sft.generate (rule_only 直接入库已有答案)
    gen = client.post(
        f"/api/projects/{prj['id']}/generations",
        json={"document_id": doc_id, "mode": "rule_only"},
    )
    assert gen.status_code == 200
    assert gen.json()["job_type"] == "sft.generate"
    drain_jobs(client.app.state.ctx)

    examples = client.get(f"/api/projects/{prj['id']}/examples").json()
    assert len(examples) == 2
    assert all(e["task_type"] == "general_sft" for e in examples)

    # 批量接纳 + 冻结快照
    batch = client.post(
        f"/api/projects/{prj['id']}/examples/batch-status",
        json={"example_ids": [e["id"] for e in examples], "status": "accepted"},
    )
    assert batch.json()["updated_count"] == 2
    snap = client.post(
        f"/api/projects/{prj['id']}/datasets/snapshots",
        json={"seed": "sft-1", "ratios": {"train": 0.5, "dev": 0.5, "holdout": 0.0}},
    )
    assert snap.status_code == 200
    assert sum(snap.json()["counts"].values()) == 2
