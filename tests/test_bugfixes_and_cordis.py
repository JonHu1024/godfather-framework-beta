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


# --------------------------------------------------------------------------
# Audit Report Fixes (#1 Windows Sandbox, #2 Pandoc calc & Failed Job State,
# #3 Inline Markdown Formatting, #4 Force Rerun API)
# --------------------------------------------------------------------------
def test_report_bug1_windows_appcontainer_os_isolation_and_fail_closed(tmp_path, monkeypatch):
    """Windows 编译沙箱必须在 OS 层面拒绝越权路径读写，且初始化失败时严格 fail-closed。"""
    import os
    import sys
    import pytest
    from godfather.settings import Settings
    from godfather.taskpacks.latex import compiler as comp
    from godfather.taskpacks.latex import win_sandbox as ws

    if sys.platform != "win32":
        pytest.skip("仅适用于 Windows AppContainer 沙箱")

    # 1) 真实 OS 级 AppContainer 隔离：无法读取工作目录外的敏感文件，也无法向外部写入
    outside_dir = tmp_path / "outside_secret"
    outside_dir.mkdir()
    secret_file = outside_dir / "secret.txt"
    secret_file.write_text("SECRET_TOKEN_987654", encoding="utf-8")
    pwn_target = outside_dir / "pwn.txt"

    work_dir = tmp_path / "sandbox_work"
    work_dir.mkdir()
    cmd_exe = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    probe_cmd = (
        f'type "{secret_file}" & '
        f'(echo PWNED > "{pwn_target}" 2>nul) & '
        f'if exist "{pwn_target}" (echo WRITE_OK) else (echo WRITE_BLOCKED)'
    )
    res = ws.run_sandboxed(
        [cmd_exe, "/c", probe_cmd],
        work_dir=work_dir,
        env={"PATH": comp._build_environment(work_dir, windows_engine=True).get("PATH", "")},
        timeout_seconds=30,
        job_label="isolation-probe",
    )
    assert "SECRET_TOKEN_987654" not in res.stdout
    assert "WRITE_OK" not in res.stdout
    assert not pwn_target.exists()

    # 2) 初始化失败必须 fail-closed，不能回退到无隔离子进程执行
    monkeypatch.setattr(
        comp,
        "detect_engine",
        lambda _settings: (cmd_exe, "mock cmd"),
    )

    def _boom(*_args, **_kwargs):
        raise ws.SandboxInitError("mock ACL init failure")

    monkeypatch.setattr(ws, "run_sandboxed", _boom)
    c_res = comp.compile_tex(
        "\\documentclass{article}\\begin{document}Hi\\end{document}",
        settings=Settings(),
        work_root=tmp_path,
        job_id="failclosed",
    )
    assert c_res.ok is False
    assert c_res.exit_code == -5
    assert "初始化失败" in c_res.error


def test_report_bug1_real_xelatex_chinese_math_and_image_in_sandbox(tmp_path):
    """在真实 Windows TeX 沙箱内编译中文 ctexart + 数学公式 + 本地图片应正常生成 PDF。"""
    import sys
    import pytest
    from godfather.settings import Settings
    from godfather.taskpacks.latex import assembler, compiler as comp

    if sys.platform != "win32":
        pytest.skip("仅在 Windows 上验证 AppContainer + xelatex 联调")
    binary, _ = comp.detect_engine(Settings())
    if not binary:
        pytest.skip("当前环境未检测到可用 LaTeX 引擎")

    fig_path = Path(__file__).resolve().parent.parent / "examples" / "figures" / "overview.png"
    assert fig_path.exists()
    assets = {"figures/overview.png": fig_path.read_bytes()}

    parsed = parse_markdown(
        "# 中文测试\n\n正文中包含数学公式 $E = mc^2$ 与本地图片：\n\n![系统总览](figures/overview.png)\n"
    )
    assembled = assembler.assemble_document(
        parsed.blocks,
        settings=Settings(),
        title="沙箱实测",
        template_name="preamble_zh_ctex",
    )
    res = comp.compile_tex(
        assembled.tex,
        settings=Settings(),
        work_root=tmp_path,
        job_id="real_sandbox_zh",
        assets=assets,
    )
    assert res.ok, f"XeLaTeX 沙箱编译失败：{res.error}\n{res.log[-800:]}"
    pdf_file = Path(res.pdf_path)
    assert pdf_file.exists()
    assert pdf_file.read_bytes().startswith(b"%PDF-")


def test_report_bug2_pandoc_calc_table_compiles(tmp_path):
    """内置模板必须加载 calc 宏包（含 preamble_en_academic 映射），并能编译 Pandoc 比例列宽表格。"""
    import pytest
    from godfather.settings import Settings
    from godfather.taskpacks.latex import compiler as comp
    from godfather.taskpacks.latex.pipeline import wrap_body
    from godfather.taskpacks.latex.templates import DEFAULT_TEMPLATES, PREAMBLE_PLAIN, PREAMBLE_ZH_CTEX

    assert r"\usepackage{calc}" in PREAMBLE_ZH_CTEX
    assert r"\usepackage{calc}" in PREAMBLE_PLAIN
    assert r"\usepackage{caption}" in PREAMBLE_PLAIN
    assert DEFAULT_TEMPLATES["preamble_en_academic"] == PREAMBLE_PLAIN

    binary, _ = comp.detect_engine(Settings())
    if not binary:
        pytest.skip("当前环境未检测到可用 LaTeX 引擎")

    pandoc_table_tex = (
        r"\begin{longtable}[]{@{}"
        "\n"
        r"  >{\raggedright\arraybackslash}p{(\linewidth - 4\tabcolsep) * \real{0.3333}}"
        "\n"
        r"  >{\raggedright\arraybackslash}p{(\linewidth - 4\tabcolsep) * \real{0.3333}}"
        "\n"
        r"  >{\raggedright\arraybackslash}p{(\linewidth - 4\tabcolsep) * \real{0.3334}}@{}}"
        "\n"
        r"\toprule\noalign{}"
        "\n"
        r"字段 & 说明 & 示例 \\"
        "\n"
        r"\midrule\noalign{}"
        "\n"
        r"\endhead"
        "\n"
        r"\bottomrule\noalign{}"
        "\n"
        r"\endlastfoot"
        "\n"
        r"\textbf{粗体} & 数学 $x_1 + y_2$ & \texttt{code\_ok} \\"
        "\n"
        r"\end{longtable}"
    )

    for idx, tpl_name in enumerate(("preamble_zh_ctex", "preamble_en_academic")):
        doc = wrap_body(
            pandoc_table_tex,
            Settings(),
            template_name=tpl_name,
            title="Pandoc Table",
        )
        res = comp.compile_tex(doc, settings=Settings(), work_root=tmp_path, job_id=f"pandoc_calc_{idx}")
        assert res.ok, f"模板 {tpl_name} 编译 Pandoc 表格失败：{res.error}\n{res.log[-600:]}"
        assert Path(res.pdf_path).read_bytes().startswith(b"%PDF-")


def test_report_bug2_compile_failure_propagates_to_job_and_preserves_artifacts(client, monkeypatch):
    """编译或校验失败时，队列任务状态必须同步标记为 failed 且保留可读的 .tex 与编译 .log 产物。"""
    from godfather.taskpacks.latex import compiler as comp
    from godfather.taskpacks.latex.compiler import CompileResult

    def _fake_fail_compile(tex_source: str, *, settings, work_root: Path, job_id: str = "manual", **_kwargs):
        work_dir = Path(work_root) / "compile" / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        tex_p = work_dir / "main.tex"
        log_p = work_dir / "main.log"
        tex_p.write_text(tex_source, encoding="utf-8")
        log_text = "This is mock XeTeX\n! Undefined control sequence.\nl.12 \\badmacro\n"
        log_p.write_text(log_text, encoding="utf-8")
        return CompileResult(
            ok=False,
            engine="xelatex",
            binary="mock-xelatex",
            exit_code=1,
            log=log_text,
            log_path=str(log_p),
            pdf_path="",
            duration_ms=25,
            error="Undefined control sequence (l.12)",
            work_dir=str(work_dir),
        )

    monkeypatch.setattr(comp, "compile_tex", _fake_fail_compile)

    prj = client.post("/api/projects", json={"name": "失败回传测试"}).json()
    up = client.post(
        f"/api/projects/{prj['id']}/materials",
        data={"kind": "markdown", "filename": "bad.md"},
        files={"file": ("bad.md", io.BytesIO("# 测试失败回传\n\n正文字段。\n".encode("utf-8")), "text/markdown")},
    )
    assert up.status_code == 200
    doc_id = up.json()["document_id"]

    gen_resp = client.post(
        f"/api/projects/{prj['id']}/generations",
        json={"document_id": doc_id, "mode": "rule_only"},
    )
    assert gen_resp.status_code == 200
    job_id = gen_resp.json()["job_id"]
    drain_jobs(client.app.state.ctx)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["error"]
    assert job["result"]["compile_ok"] is False
    assert job["result"]["verdict"] == "fail"
    assert job["result"]["tex_artifact_id"]
    assert job["result"]["log_artifact_id"]

    gens = client.get(f"/api/projects/{prj['id']}/generations").json()
    assert len(gens) == 1
    g = gens[0]
    assert g["status"] == "failed"
    assert g["tex_artifact_id"] == job["result"]["tex_artifact_id"]
    log_art_id = g["stats"]["compile"]["log_artifact_id"]
    assert log_art_id == job["result"]["log_artifact_id"]

    tex_content = client.get(f"/api/artifacts/{g['tex_artifact_id']}/content")
    log_content = client.get(f"/api/artifacts/{log_art_id}/content")
    assert tex_content.status_code == 200
    assert "\\documentclass" in tex_content.text
    assert log_content.status_code == 200
    assert "Undefined control sequence" in log_content.text


def test_report_bug3_rule_only_inline_formatting_and_math_code_priority(tmp_path):
    """纯规则模式必须准确转换粗体/斜体/行内代码，并优先保护数学与代码中的 * 和 _。"""
    from godfather.settings import Settings
    from godfather.taskpacks.latex import assembler, compiler as comp

    md_text = (
        "# 包含 **加粗标题** 与 *斜体词* 的小节\n\n"
        "普通段落支持 **粗体**、*斜体*、__双下划线粗体__、_单下划线斜体_、***粗斜体***。\n\n"
        "行内代码 `snake_case * 2 + $x$` 与数学公式 $a_1 * b_2$ 和字面乘号 2 * 3 * 4 必须互不干扰。\n\n"
        "| 名称 | **格式** | 公式与代码 |\n"
        "| :--- | :---: | ---: |\n"
        "| 项1 | __关键__ | $\\alpha_1 & \\beta_2$ 与 `foo_bar * 1` |\n"
    )
    parsed = parse_markdown(md_text)
    assembled = assembler.assemble_document(
        parsed.blocks,
        settings=Settings(),
        title="行内格式测试",
        template_name="preamble_zh_ctex",
    )
    tex = assembled.tex
    body = tex.split(r"\begin{document}", 1)[1]

    assert r"\textbf{加粗标题}" in body
    assert r"\emph{斜体词}" in body
    assert r"\textbf{粗体}" in body
    assert r"\emph{斜体}" in body
    assert r"\textbf{双下划线粗体}" in body
    assert r"\emph{单下划线斜体}" in body
    assert r"\textbf{\emph{粗斜体}}" in body
    assert r"\texttt{snake\_case * 2 + \$x\$}" in body
    assert "$a_1 * b_2$" in body
    assert r"$\alpha_1 \& \beta_2$" in body
    assert r"\texttt{foo\_bar * 1}" in body
    assert "**粗体**" not in body
    assert "*斜体*" not in body

    binary, _ = comp.detect_engine(Settings())
    if binary:
        res = comp.compile_tex(tex, settings=Settings(), work_root=tmp_path, job_id="rule_inline")
        assert res.ok, f"规则模式行内格式编译失败：{res.error}\n{res.log[-600:]}"
        assert Path(res.pdf_path).read_bytes().startswith(b"%PDF-")


def test_report_bug4_generation_api_force_rerun(client, monkeypatch):
    """生成 API 默认复用既有任务（force=False），传入 force=True 时必须创建新 job_id 并保留两轮产物。"""
    from godfather.taskpacks.latex import compiler as comp
    from godfather.taskpacks.latex.compiler import CompileResult

    compile_calls = []

    def _fake_ok_compile(tex_source: str, *, settings, work_root: Path, job_id: str = "manual", **_kwargs):
        compile_calls.append(job_id)
        work_dir = Path(work_root) / "compile" / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        tex_p = work_dir / "main.tex"
        log_p = work_dir / "main.log"
        pdf_p = work_dir / "main.pdf"
        tex_p.write_text(tex_source, encoding="utf-8")
        log_p.write_text(f"compiled {job_id}\n", encoding="utf-8")
        pdf_p.write_bytes(b"%PDF-1.4\n1 0 obj <<>> endobj\ntrailer <<>>\n%%EOF\n")
        return CompileResult(
            ok=True,
            engine="xelatex",
            binary="mock-xelatex",
            exit_code=0,
            log=f"compiled {job_id}\n",
            log_path=str(log_p),
            pdf_path=str(pdf_p),
            duration_ms=10,
            work_dir=str(work_dir),
        )

    monkeypatch.setattr(comp, "compile_tex", _fake_ok_compile)

    prj = client.post("/api/projects", json={"name": "强制重跑测试"}).json()
    up = client.post(
        f"/api/projects/{prj['id']}/materials",
        data={"kind": "markdown", "filename": "doc.md"},
        files={"file": ("doc.md", io.BytesIO("# 重跑验证\n\n**加粗**文本。\n".encode("utf-8")), "text/markdown")},
    )
    assert up.status_code == 200
    doc_id = up.json()["document_id"]

    payload = {"document_id": doc_id, "mode": "rule_only", "template_name": "preamble_zh_ctex"}
    r1 = client.post(f"/api/projects/{prj['id']}/generations", json=payload)
    assert r1.status_code == 200
    assert r1.json()["reused"] is False
    job_1 = r1.json()["job_id"]
    drain_jobs(client.app.state.ctx)

    # force=False（默认）复用已有任务，不触发第二次编译
    r2 = client.post(f"/api/projects/{prj['id']}/generations", json={**payload, "force": False})
    assert r2.status_code == 200
    assert r2.json()["reused"] is True
    assert r2.json()["job_id"] == job_1
    assert len(compile_calls) == 1

    # force=True 忽略幂等复用，创建全新的 job_id 并生成第 2 轮产物
    r3 = client.post(f"/api/projects/{prj['id']}/generations", json={**payload, "force": True})
    assert r3.status_code == 200
    assert r3.json()["reused"] is False
    job_2 = r3.json()["job_id"]
    assert job_2 != job_1
    assert json.loads(client.app.state.ctx.queue.get(job_2)["payload_json"])["force"] is True
    drain_jobs(client.app.state.ctx)

    assert len(compile_calls) == 2
    gens = client.get(f"/api/projects/{prj['id']}/generations").json()
    assert len(gens) == 2
    assert gens[0]["id"] != gens[1]["id"]
    for g in gens:
        assert g["status"] == "succeeded"
        assert client.get(f"/api/artifacts/{g['tex_artifact_id']}/content").status_code == 200
        assert client.get(f"/api/artifacts/{g['pdf_artifact_id']}/content").status_code == 200


# --------------------------------------------------------------------------
# cfb6400 表格编译回归修复：规则模式表格内显示公式 + Pandoc 无标题表格兼容
# --------------------------------------------------------------------------
def test_report_cfb6400_rule_only_table_display_math(tmp_path):
    """表格单元格内的 $$...$$ 与 \\[...\\] 应转为带 \\displaystyle 的行内公式，正文独立公式与粗体/代码保持正常。"""
    from godfather.settings import Settings
    from godfather.taskpacks.latex import assembler, compiler as comp

    md_text = (
        "# 公式与表格混合测试\n\n"
        "正文中保留 **加粗文本** 与行内代码 `rate * 2 + $x$`，以及独立公式：\n\n"
        "$$\n\\int_0^1 x^2 \\, dx = \\frac{1}{3}\n$$\n\n"
        "| 公式类型 | 表达式 | 说明 |\n"
        "| :--- | :--- | :--- |\n"
        "| 积分公式 | $$\\int_0^1 x^2 \\, dx = \\frac{1}{3}$$ | 双美元符 |\n"
        "| 求和公式 | \\[\\sum_{i=1}^n i = \\frac{n(n+1)}{2}\\] | 方括号公式 |\n"
        "| 含对齐符 | $\\alpha_1 & \\beta_2$ | 正常表格公式 |\n"
    )
    parsed = parse_markdown(md_text)
    assembled = assembler.assemble_document(
        parsed.blocks,
        settings=Settings(),
        title="表格公式测试",
        template_name="preamble_zh_ctex",
    )
    body = assembled.tex.split(r"\begin{document}", 1)[1]

    assert r"\textbf{加粗文本}" in body
    assert r"\texttt{rate * 2 + \$x\$}" in body
    assert r"\begin{equation}" in body
    assert r"$\displaystyle \int_0^1 x^2 \, dx = \frac{1}{3}$" in body
    assert r"$\displaystyle \sum_{i=1}^n i = \frac{n(n+1)}{2}$" in body
    assert r"$\alpha_1 \& \beta_2$" in body
    # 表格内部不能残留 $$ 或 \[...\] 显示公式定界符
    table_chunk = body.split(r"\begin{tabular}", 1)[1].split(r"\end{tabular}", 1)[0]
    assert "$$" not in table_chunk
    assert r"\[" not in table_chunk

    binary, _ = comp.detect_engine(Settings())
    if binary:
        res = comp.compile_tex(
            assembled.tex,
            settings=Settings(),
            work_root=tmp_path,
            job_id="rule_table_display_math",
        )
        assert res.ok, f"规则模式表格显示公式编译失败：{res.error}\n{res.log[-600:]}"
        assert Path(res.pdf_path).read_bytes().startswith(b"%PDF-")


def test_report_cfb6400_pandoc_captionless_longtable_compat(tmp_path, monkeypatch):
    """Pandoc 生成的 \\def\\LTcaptype{none} 应随 longtable 版本兼容改写，且不影响有标题表格与比例列宽编译。"""
    from godfather.settings import Settings
    from godfather.taskpacks.latex import compiler as comp, pandoc_base as pb
    from godfather.taskpacks.latex.pipeline import wrap_body

    sample_pandoc_tex = (
        "{\n"
        "\\def\\LTcaptype{none}\n"
        "\\begin{longtable}[]{@{}ll@{}}\n"
        "\\toprule\\noalign{}\n"
        "无标题列1 & 无标题列2 \\\\\n"
        "\\midrule\\noalign{}\n"
        "\\endhead\n"
        "\\bottomrule\\noalign{}\n"
        "\\endlastfoot\n"
        "值1 & 值2 \\\\\n"
        "\\end{longtable}\n"
        "}\n\n"
        "\\begin{longtable}[]{@{}\n"
        "  >{\\raggedright\\arraybackslash}p{(\\linewidth - 2\\tabcolsep) * \\real{0.5000}}\n"
        "  >{\\raggedright\\arraybackslash}p{(\\linewidth - 2\\tabcolsep) * \\real{0.5000}}@{}}\n"
        "\\caption{带编号表格}\\tabularnewline\n"
        "\\toprule\\noalign{}\n"
        "比例列A & 比例列B \\\\\n"
        "\\midrule\\noalign{}\n"
        "\\endhead\n"
        "\\bottomrule\\noalign{}\n"
        "\\endlastfoot\n"
        "内容A & 内容B \\\\\n"
        "\\end{longtable}\n"
    )

    class _FakeProc:
        def __init__(self, stdout: str, returncode: int = 0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    new_sty = tmp_path / "new_longtable.sty"
    new_sty.write_text(
        r"\ProvidesPackage{longtable}[2024-10-27 v4.22]" "\n" r"\tl_if_empty:eTF { \LTcaptype }",
        encoding="utf-8",
    )
    old_sty = tmp_path / "old_longtable.sty"
    old_sty.write_text(
        r"\ProvidesPackage{longtable}[2020/01/07 v4.13 Multi-page Table package (DPC)]"
        "\n"
        r"\refstepcounter{\LTcaptype}",
        encoding="utf-8",
    )

    active_sty = new_sty

    def _fake_run(args, **_kwargs):
        if "kpsewhich" in str(args[0]):
            return _FakeProc(str(active_sty))
        if "--version" in args:
            return _FakeProc("pandoc 3.14")
        return _FakeProc(sample_pandoc_tex)

    monkeypatch.setattr(pb, "_find_kpsewhich", lambda _settings: "mock-kpsewhich")
    monkeypatch.setattr(pb.subprocess, "run", _fake_run)

    # 1) 新版 longtable（2024+ 以空值表示不编号）
    pb._inspect_longtable_sty_empty_captype.cache_clear()
    res_new = pb.convert_fragment("dummy", Settings())
    assert res_new.ok is True
    assert r"\def\LTcaptype{}" in res_new.tex
    assert r"\def\LTcaptype{none}" not in res_new.tex
    assert r"\caption{带编号表格}" in res_new.tex

    # 2) 旧版 longtable（< 2024）保持原样
    active_sty = old_sty
    pb._inspect_longtable_sty_empty_captype.cache_clear()
    res_old = pb.convert_fragment("dummy", Settings())
    assert res_old.ok is True
    assert r"\def\LTcaptype{none}" in res_old.tex

    # 3) 若本机存在 real kpsewhich + xelatex，用真实引擎核验兼容结果可编出 PDF
    monkeypatch.undo()
    pb._inspect_longtable_sty_empty_captype.cache_clear()
    binary, _ = comp.detect_engine(Settings())
    if binary and pb._find_kpsewhich(Settings()):
        compat = pb._compat_captionless_longtable(
            sample_pandoc_tex,
            empty_captype=pb._longtable_uses_empty_captype(Settings()),
        )
        for idx, tpl_name in enumerate(("preamble_zh_ctex", "preamble_en_academic")):
            doc = wrap_body(compat, Settings(), template_name=tpl_name, title="Pandoc 无标题表格兼容测试")
            c_res = comp.compile_tex(
                doc,
                settings=Settings(),
                work_root=tmp_path,
                job_id=f"pandoc_ltcap_{idx}",
            )
            assert c_res.ok, f"{tpl_name} 编译失败：{c_res.error}\n{c_res.log[-600:]}"
            assert Path(c_res.pdf_path).read_bytes().startswith(b"%PDF-")

