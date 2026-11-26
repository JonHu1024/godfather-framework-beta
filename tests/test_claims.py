"""验证日志里的每一条"已修"声明都必须真的在代码里。

背景：这个仓库在第三轮修复中出现过三次"声称修了但其实没写进文件"
（编辑脚本的锚点没匹配上，replace 静默失败，而我只看了脚本打印的 ok）。
所以把声明固化成测试：只要有人把实现删掉或改回旧行为，这里就会红。

对应文档：docs/07-verification-log.md §7 / §7.2
"""

from __future__ import annotations

from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src" / "godfather"


def read(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# §7.2 致命：取消的任务不能复活
# --------------------------------------------------------------------------
def test_claim_cancelled_job_cannot_be_revived():
    """jobs.py 的三个收尾分支都要用条件更新。"""
    source = read("jobs.py")
    # 成功 / 暂停 / 失败重试三个分支
    assert source.count("AND status = ? AND (lease_owner = ? OR lease_owner = '')") >= 3, (
        "jobs.py 里的条件更新少于 3 处：取消的任务会被改回 queued"
    )
    assert "STATUS_QUEUED," in source


def test_claim_job_renewal_is_conditional():
    """续租必须带 lease_owner 条件（否则会续回别人的租约）。"""
    source = read("jobs.py")
    assert "WHERE id = ? AND lease_owner = ?" in source


# --------------------------------------------------------------------------
# §7.2 高危：修订写回
# --------------------------------------------------------------------------
def test_claim_revision_is_written_back():
    """run_revision 成功后必须写回 document_revisions。"""
    source = read("taskpacks/latex/pipeline.py")
    assert "def write_back_revision(" in source, "缺少 write_back_revision"
    # 必须在"批注标成 accepted"的分支里被调用
    accepted_index = source.index('"status": "accepted"')
    call_index = source.index("write_back_revision(", accepted_index)
    assert call_index > accepted_index, "写回没有接在已接受分支上"
    # 写回函数里必须真的插入 document_revisions
    body = source[source.index("def write_back_revision("):]
    body = body[: body.index("\ndef ")]
    assert '"document_revisions"' in body
    assert '"revised_tex"' in body or "revised_tex" in body


def test_claim_revised_tex_goes_through_the_normal_render_path():
    """revised_tex 不能提前 return 跳过 Verbatim 包装与环境中和。"""
    source = read("taskpacks/latex/assembler.py")
    body = source[source.index("def render_block("):]
    body = body[: body.index("\ndef ")]
    # 不能再有 "if revised ...: return revised"
    assert "return revised.rstrip()" not in body, "revised_tex 仍然提前返回"
    # 代码块修订后必须包 Verbatim
    assert "neutralize_verbatim_breaks" in body
    assert "Verbatim" in body
    # 普通块也要中和环境闭合
    assert "neutralize_environment_breaks" in body


def test_claim_verbatim_neutralization_breaks_the_exact_end_marker():
    """Verbatim 里反斜杠是字面量，中和必须破坏精确的结束标记。"""
    source = read("taskpacks/latex/assembler.py")
    assert "def neutralize_verbatim_breaks(" in source
    body = source[source.index("def neutralize_verbatim_breaks("):]
    body = body[: body.index("\ndef ")]
    assert "\\\\end{" in body, "Verbatim 中和没有改写结束标记"
    # 不能用 \texttt{} 转义（Verbatim 里反斜杠是字面量）
    assert "textbackslash" not in body


# --------------------------------------------------------------------------
# §7.2 高危：老库升级
# --------------------------------------------------------------------------
def test_claim_duplicate_idempotency_keys_do_not_break_startup():
    """建唯一索引前必须先去重。"""
    source = read("db.py")
    assert "def _dedupe_job_idempotency_keys(" in source
    assert "_dedupe_job_idempotency_keys(self.conn)" in source
    # 去重必须发生在建索引脚本之前
    assert source.index("_dedupe_job_idempotency_keys(self.conn)") < source.index(
        "self.conn.executescript(SCHEMA_SQL)"
    )


def test_claim_migrations_run_before_index_creation():
    """先补列再建索引（老库缺列时进程要能起来）。"""
    source = read("db.py")
    first_migration = source.index("_apply_migrations(self.conn)")
    schema = source.index("self.conn.executescript(SCHEMA_SQL)")
    assert first_migration < schema


# --------------------------------------------------------------------------
# §7.2 高危：幂等键与取消
# --------------------------------------------------------------------------
def test_claim_generation_idempotency_key_uses_real_revision():
    """幂等键里的文档修订号必须来自 document_revisions。"""
    source = read("api/pipeline.py")
    assert "MAX(revision)" in source, "幂等键没有用真实的文档修订号"
    # 注释里提到旧写法不算，代码里不能再用
    code_lines = [
        line for line in source.splitlines()
        if "document.get('revision', 0)" in line and not line.strip().startswith("#")
    ]
    assert not code_lines, "还在用 documents.revision（该列不存在）"


def test_claim_training_checks_cancellation_before_start():
    """训练启动前必须查一次取消状态。"""
    source = read("training/runner.py")
    body = source[source.index("def run_training("):]
    start_check = body.index('status_row["status"] == "cancelled"')
    popen = body.index("subprocess.Popen(")
    assert start_check < popen, "启动前没有检查取消状态"


def test_claim_gpu_lock_wait_can_be_cancelled():
    """等 GPU 锁期间要能被打断。"""
    resources = read("training/resources.py")
    assert "should_cancel" in resources
    runner = read("training/runner.py")
    assert "should_cancel=" in runner


# --------------------------------------------------------------------------
# §7.2 中危
# --------------------------------------------------------------------------
def test_claim_sse_scope_semantics_are_consistent():
    """events 的三个作用域：具体项目 / None（只全局）/ ""（全部）。"""
    source = read("events.py")
    assert "elif project_id is None:" in source, "read_events 缺少只全局分支"
    api = read("api/events.py")
    # 两个接口都要把 scope 原值传下去（不能 or None，否则 "" 变成 None）
    assert "project_id=scope or None" not in api, "scope 被 or None 改写，跨项目订阅失效"


def test_claim_sse_connection_counter_is_released():
    """生成器未启动时 finally 不执行，必须有 BackgroundTask 兜底。"""
    source = read("api/events.py")
    assert "BackgroundTask" in source, "SSE 连接计数没有兜底释放（32 次异常断连后永久 503）"


def test_claim_synctex_is_not_deleted_as_intermediate():
    """synctex 文件不能被中间文件清理删掉。"""
    source = read("taskpacks/latex/compiler.py")
    assert '".synctex.gz"' not in source, "synctex 仍在清理列表里"
    # 产物路径要在清理之前记下来
    body = source[source.index("def compile_document("):] if "def compile_document(" in source else source
    assert body.index("synctex_path = ") < body.index("_cleanup_intermediates(work_dir)")


def test_claim_multipass_logs_are_accumulated():
    """多遍编译要保留每一遍的日志（验证层依赖它）。"""
    source = read("taskpacks/latex/compiler.py")
    assert "pass_logs" in source
    assert "pass_logs.append" in source


def test_claim_line_maps_are_kept_per_generation():
    """同一产物的各次生成各有各的行映射。"""
    source = read("storage.py")
    assert "_merge_meta" in source
    assert '"line_maps"' in source


def test_claim_upload_limit_is_checked_while_streaming():
    """上传要分块读并累计（整块读进内存挡不住 OOM）。"""
    source = read("api/projects.py")
    assert "await file.read(1024 * 1024)" in source
    assert "total > max_bytes" in source


def test_claim_revision_reuses_generation_row():
    """修订任务按 job_id 复用 generation 行。"""
    source = read("taskpacks/latex/pipeline.py")
    body = source[source.index("def run_revision("):]
    body = body[: body.index("\ndef ")]
    assert "WHERE job_id = ?" in body, "修订没有按 job_id 复用 generation 行"


def test_claim_empty_answers_are_dropped_before_training():
    """空答案样本要在进数据集之前剔除。"""
    source = read("training/scripts/train_local.py")
    assert "usable_records" in source, "空答案样本没有在构造数据集前剔除"
    assert "dropped_empty_answer_samples" in source


# --------------------------------------------------------------------------
# §7.2 低危
# --------------------------------------------------------------------------
def test_claim_model_version_numbering_starts_at_one():
    """模型版本号是"最大序号 + 1"。"""
    source = read("training/export.py")
    assert "index = highest + 1" in source
    assert "index = highest + 3" not in source


# --------------------------------------------------------------------------
# §7.2 openin_any
# --------------------------------------------------------------------------
def test_claim_openin_any_is_paranoid_and_reaches_the_engine():
    r"""文件访问限制必须真的生效。

    之前的结论"paranoid(p) 会挡掉 ctex/fontspec"是**误诊**：
    真正的原因是 kpathsea 只从 texmf.cnf 读 openin_any，
    而我们把它设成了环境变量 → 从来没生效过（本机系统值是 a，能读任意绝对路径）。
    实测（合并 TEXMFCNF 目录列表后）：
      - openin_any = p 时 \openin 读 C:/Windows/win.ini **被挡住**；
      - ctex/fontspec 正常、多遍编译的 .aux 可读、\includegraphics 正常；
      - restricted(r) **挡不住**绝对路径读取。
    """
    source = read("taskpacks/latex/compiler.py")
    # paranoid 才是能挡住绝对路径的那一档
    assert "openin_any = p" in source
    assert "openin_any = r" not in source
    # TEXMFCNF 是**目录列表**：我们的目录 + 系统的目录
    # （只有我们的 3 行 cnf 会让 TEXMFDIST 丢失 → 连 xelatex.fmt 都找不到）
    assert "system_texmfcnf" in source
    assert "_system_texmfcnf(" in source
    # Windows 引擎必须用 PowerShell 传环境变量（WSL interop 不传）
    assert "_powershell_command(" in source


# --------------------------------------------------------------------------
# §7.1 第一轮 / 第二轮的关键声明
# --------------------------------------------------------------------------
def test_claim_artifact_meta_is_merged_not_dropped():
    """同一 digest 的产物再次产出时 meta 要合并。"""
    source = read("storage.py")
    assert "_merge_meta(" in source
    body = source[source.index("def _register("):]
    body = body[: body.index("\n    def ")]
    assert "_merge_meta(" in body, "登记路径没有用合并后的 meta"


def test_claim_pandoc_extensions_are_whitelisted():
    """pandoc 扩展名白名单（扩展名会拼进 --from，可注入 --lua-filter）。"""
    source = read("taskpacks/latex/pandoc_base.py")
    assert "SAFE_PANDOC_EXTENSIONS" in source
    assert '"raw_tex"' not in source, "raw_tex 会把原始 TeX 直通进编译器"


def test_claim_policy_versions_are_not_overwritten():
    """同版本号不同内容不能覆盖旧版本。"""
    source = read("policy/engine.py")
    assert "package.version = f\"{package.version}-{suffix}\"" in source
    # 同一项目只能有一个 active 版本
    assert "status = 'draft'" in source


def test_claim_paired_code_is_only_rotated_locally():
    """局域网轮询不能把用户正在看的配对码换掉。"""
    source = read("pairing.py")
    assert "rotate_if_expired" in source
    api = read("api/projects.py")
    assert "rotate_if_expired=is_local" in api


def test_claim_budget_check_and_reserve_are_atomic():
    """预算的检查与预留必须在同一个事务里（并发超买）。"""
    source = read("budget.py")
    assert "transaction()" in source


def test_claim_compiler_disables_shell_escape():
    """编译必须恒定 -no-shell-escape。"""
    source = read("taskpacks/latex/compiler.py")
    assert "-no-shell-escape" in source
    assert "-shell-escape" not in source.replace("-no-shell-escape", "")


def test_claim_asset_paths_are_sanitized():
    """素材路径不能越界。"""
    source = read("taskpacks/latex/compiler.py")
    assert "def sanitize_asset_path(" in source
    assert "relative_to" in source or "越界" in source


# --------------------------------------------------------------------------
# 前端声明
# --------------------------------------------------------------------------
def test_claim_modal_submit_listener_does_not_accumulate():
    """modal 每次都往同一个 form 上叠监听器会导致回车触发 N 次 onOk。"""
    source = (SRC.parent.parent / "web" / "js" / "ui.js").read_text(encoding="utf-8")
    assert "form.addEventListener(\"submit\"" not in source, "submit 监听器仍在累积"
    assert "form.onsubmit" in source


def test_claim_pdfview_has_a_single_destroy():
    """两个 destroy 会让后者覆盖前者，观察器永不释放。"""
    source = (SRC.parent.parent / "web" / "js" / "pdfview.js").read_text(encoding="utf-8")
    # 只算方法定义（"  destroy() {"）
    definitions = source.count("\n  destroy() {")
    assert definitions == 1, f"pdfview.js 里有 {definitions} 个 destroy 定义（后者会覆盖前者）"
    assert "_resizeObserver.disconnect()" in source


def test_claim_policy_activation_endpoint_exists():
    """前端会调 PATCH /api/projects/{id}/policies/{policy_id}。"""
    source = read("api/projects.py")
    assert '@router.patch("/api/projects/{project_id}/policies/{policy_id}")' in source


def test_claim_artifact_links_carry_the_pairing_code():
    """裸 <a href> 在局域网访问会 401。"""
    api = (SRC.parent.parent / "web" / "js" / "api.js").read_text(encoding="utf-8")
    assert "export function artifactUrl(" in api
    queue = (SRC.parent.parent / "web" / "js" / "views" / "queue.js").read_text(encoding="utf-8")
    assert "artifactUrl(" in queue
    assert 'href: `/api/artifacts/' not in queue, "还有裸的产物链接（手机上打不开）"


def test_claim_render_is_serialized():
    """并发渲染会让 URL 与页面内容对不上。"""
    source = (SRC.parent.parent / "web" / "js" / "main.js").read_text(encoding="utf-8")
    assert "renderToken" in source


def test_claim_annotations_survive_a_pdf_rerender():
    """缩放重绘不能抹掉 annot-layer（批注框会消失且不回来）。"""
    source = (SRC.parent.parent / "web" / "js" / "pdfview.js").read_text(encoding="utf-8")
    body = source[source.index("_resizeObserver = new ResizeObserver"):]
    body = body[: body.index("});", body.index("this._resizeObserver.observe"))]
    assert 'holder.innerHTML = ""' not in body, "重绘仍在清空 holder（会抹掉批注层）"
    assert "annot-layer" in body


# --------------------------------------------------------------------------
# §7.3 项目级配对
# --------------------------------------------------------------------------
def test_claim_pairing_code_is_per_project():
    """配对码必须按项目签发（全局码 = 全项目入口）。"""
    source = read("pairing.py")
    assert "def issue_code(self, project_id: str)" in source
    assert "pairing_codes" in source
    assert "def redeem(self, code: str)" in source


def test_claim_session_token_is_stored_hashed():
    """会话令牌只存哈希（数据库被拷走也换不出可用令牌）。"""
    source = read("pairing.py")
    assert "_hash_token(" in source
    assert "hashlib.sha256" in source


def test_claim_scope_mismatch_is_enforced():
    """会话访问别的项目必须 403。"""
    source = read("app.py")
    assert "scope_mismatch" in source
    assert "resolve_target_project" in source
    scope = read("api/scope.py")
    assert "def resolve_target_project(" in scope
    # 必须能从对象 id 反查项目（产物、任务、训练运行……）
    for table in ("artifacts", "jobs", "training_runs", "model_versions"):
        assert table in scope, f"{table} 没有纳入项目归属解析"


def test_claim_list_endpoints_are_narrowed():
    """列表接口在有会话时必须被收窄到该会话的项目。"""
    projects = read("api/projects.py")
    assert "pairing_project" in projects
    training = read("api/training.py")
    assert "_scoped_project" in training
    events = read("api/events.py")
    assert "pairing_project" in events


def test_claim_rotate_revokes_old_sessions():
    """换码 = 旧设备出局。"""
    source = read("pairing.py")
    assert "def revoke_project_sessions(" in source
    api = read("api/projects.py")
    assert "revoke_project_sessions" in api


# --------------------------------------------------------------------------
# §7.4：文档审计发现"已修但没有测试兜底"的声明
# --------------------------------------------------------------------------
def test_claim_accept_verdict_accepts_plain_string():
    """策略里的 accept_verdict 写成字符串 "pass" 时，判定必须成立。

    以前 set("pass") == {'p','a','s'} → 判定永远不是 pass。
    """
    from godfather.taskpacks.latex.pipeline import _overall_verdict

    class _Policy:
        validator_config = {"accept_verdict": "pass", "fail_on_layers": ["engineering"]}

    reports = [{"layer": "engineering", "verdict": "pass", "findings": []}]
    assert _overall_verdict(reports, _Policy()) == "pass"
    # 列表写法也要成立
    _Policy.validator_config = {"accept_verdict": ["pass"], "fail_on_layers": ["engineering"]}
    assert _overall_verdict(reports, _Policy()) == "pass"


def test_claim_sse_connection_limit_returns_503():
    """SSE 连接数上限 32，超出返回 503（防止长连接打满）。"""
    source = read("api/events.py")
    assert "503" in source
    assert "MAX_SSE_CONNECTIONS" in source or "max_connections" in source


def test_claim_reused_field_is_present():
    """幂等复用时返回的字段叫 reused（不是 idempotent_reuse 装 job id）。"""
    source = read("jobs.py")
    assert '"reused"' in source or "'reused'" in source
    api = read("api/pipeline.py")
    assert "reused" in api


def test_claim_cancellable_training_statuses():
    """取消训练要校验状态：不能把已完成的训练改成 cancelled。"""
    source = read("api/training.py")
    assert "CANCELLABLE_STATUSES" in source


def test_claim_content_layer_failure_still_registers_sample():
    """内容层 fail 不阻止样本登记（只有工程层失败才丢弃）。"""
    source = read("taskpacks/latex/pipeline.py")
    assert "label_origin" in source
    # 登记条件是 pass/warn，而不是"必须全 pass"
    assert '"pass"' in source and '"warn"' in source


def test_claim_snapshot_path_is_returned():
    """快照接口返回落盘路径（以前是死链）。"""
    source = read("api/pipeline.py")
    assert '"path"' in source


def test_claim_truncated_samples_deduplicated():
    """truncated_samples 按样本去重，不能按 epoch 重复计数。"""
    source = read("training/scripts/train_local.py")
    assert "truncated_samples: set" in source or "truncated_samples = set()" in source


def test_claim_teardown_is_dispatched_before_clear():
    """视图清理事件必须在清空主区**之前**派发，否则旧视图收不到（资源泄漏）。"""
    source = (SRC.parent.parent / "web" / "js" / "main.js").read_text(encoding="utf-8")
    # 用真正的调用语句定位（注释里也会出现 clear(main)）
    teardown_at = source.index('new CustomEvent("gf:teardown")')
    clear_at = source.index("clear(main).append(")
    assert teardown_at < clear_at, "gf:teardown 在 clear(main) 之后派发：PDF 文档与 blob URL 会泄漏"


def test_claim_markdown_dialect_is_actually_used():
    """Markdown 方言必须真的影响解析（以前只写库、无人读取）。"""
    from godfather.taskpacks.latex.mdparse import parse_markdown

    md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    assert [b.kind for b in parse_markdown(md, "t", dialect="pandoc-markdown").blocks] == ["table"]
    assert [b.kind for b in parse_markdown(md, "t", dialect="commonmark").blocks] == ["paragraph"]


def test_claim_evaluation_split_cannot_be_train():
    """评测划分不能是 train（拿训练集考试，指标虚高）。"""
    source = read("api/training.py")
    assert 'Literal["dev", "holdout"]' in source


def test_claim_export_stop_sequence_matches_target_end():
    """Modelfile 的 stop 必须与训练目标结尾一致（\\end{document}）。

    以前是 "\\n\\n"：Ollama 不解释 \\n（只认 texmf 之外的原样字符），
    那个序列永远匹配不上；而且正文里的第一个空行在导言区中间。
    """
    source = read("training/export.py")
    assert "end{{document}}" in source, "stop 序列不是 \\end{document}"


def test_claim_model_names_are_globally_unique():
    """模型名序号要跨项目统计（Ollama 名字是全局的，同名会静默覆盖）。"""
    source = read("training/export.py")
    assert "SELECT name FROM model_versions\"" in source or "FROM model_versions" in source
    assert "project_id = ?" not in source.split("def _default_model_name")[1].split("def ")[0]


# --------------------------------------------------------------------------
# §7.5：参考 DeepInfer 的设计后做的改动
# --------------------------------------------------------------------------
def test_claim_cache_key_includes_model_digest():
    """缓存键必须含权重摘要：同一个 tag 重新 pull 过之后缓存必须失效。

    DeepInfer 把"固定模型修订"当成基本要求；我们这边对应的是
    "产物要绑定到确切的那份权重"，否则训练样本里会混着另一份权重的答案。
    """
    from godfather.budget import cache_key

    a = cache_key(provider="ollama", model="qwen3:0.6b", prompt="x", model_digest="sha256:aaa")
    b = cache_key(provider="ollama", model="qwen3:0.6b", prompt="x", model_digest="sha256:bbb")
    assert a != b, "换了权重却命中同一个缓存键"


def test_claim_teacher_records_real_revision():
    """teacher_json 的 revision 不能是写死的 "unknown"，要用真实权重摘要。"""
    source = read("taskpacks/latex/pipeline.py")
    assert '"revision": "unknown"' not in source
    assert 'teacher_stats.get("model_digest")' in source


def test_claim_provider_supports_seed():
    """教师调用要能固定采样种子（可复现）。"""
    source = read("providers/base.py")
    assert "seed: int | None = None" in source
    ollama = read("providers/ollama.py")
    assert '"seed"' in ollama


def test_claim_evaluation_is_paired():
    """评测必须配对执行（同一输入先基线后候选），否则差值里混着机器漂移。"""
    source = read("training/evaluate.py")
    assert '"paired": True' in source
    # 不能再是"先把一个模型跑完再跑另一个"
    assert "for role, model in models:" not in source


def test_claim_activation_requires_verification():
    """激活模型前要校验（验证过才发布）。"""
    source = read("training/models.py")
    assert "def check_activation(" in source
    assert "latest_evaluation_for_model" in source
    api = read("api/training.py")
    assert "check_activation" in api
    assert "force" in api


def test_claim_rollback_follows_previous_version_chain():
    """回滚要沿 previous_version 链走，而不是取"最近归档的"。"""
    source = read("training/models.py")
    assert "previous_version" in source.split("def rollback(")[1].split("def ")[0]


def test_claim_no_markdown_as_latex_target():
    """教师没产出时不能把原始 Markdown 当作 LaTeX 训练目标。"""
    source = read("taskpacks/latex/pipeline.py")
    assert "def _rule_convert_chunk(" in source
    assert "_rule_convert_chunk(chunk_blocks_list)" in source
