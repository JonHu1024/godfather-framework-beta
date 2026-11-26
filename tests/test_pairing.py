"""配对凭证与局域网访问控制测试（项目级）。"""
from __future__ import annotations


from godfather.pairing import PairingManager


def make_manager(db, **kwargs) -> PairingManager:
    kwargs.setdefault("ttl_seconds", 600)
    kwargs.setdefault("enabled", True)
    return PairingManager(db=db, **kwargs)


def add_project(db, project_id: str, name: str = "项目") -> str:
    db.insert(
        "projects",
        {
            "id": project_id,
            "name": name,
            "taskpack": "markdown_to_latex",
            "description": "",
            "active_model": "",
            "policy_version": "",
            "settings_json": "{}",
            "status": "created",
            "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    return project_id


def test_disabled_pairing_allows_everything(ctx):
    manager = make_manager(ctx.db, enabled=False)
    assert manager.verify(None) == ""
    assert manager.verify("whatever") == ""
    assert manager.current()["required"] is False


def test_code_is_per_project(ctx):
    """每个项目一个码：A 项目的码不能解锁 B 项目。"""
    add_project(ctx.db, "p1")
    add_project(ctx.db, "p2")
    manager = make_manager(ctx.db)
    code_a = manager.issue_code("p1")
    code_b = manager.issue_code("p2")
    assert code_a != code_b
    assert manager.current("p1")["code"] == code_a
    assert manager.current("p2")["code"] == code_b
    # 不指定项目不给码
    assert manager.current()["code"] == ""


def test_code_expires(ctx):
    add_project(ctx.db, "p1")
    manager = make_manager(ctx.db, ttl_seconds=30)
    code = manager.issue_code("p1")
    # 手动把过期时间推到过去
    ctx.db.execute(
        "UPDATE pairing_codes SET expires_at = ? WHERE project_id = 'p1'",
        ("2000-01-01T00:00:00+08:00",),
    )
    assert manager.current("p1")["code"] == ""
    assert manager.redeem(code) is None


def test_rotate_invalidates_old_code(ctx):
    add_project(ctx.db, "p1")
    manager = make_manager(ctx.db)
    old = manager.issue_code("p1")
    new = manager.rotate("p1")
    assert new != old
    # 旧码不能再兑换
    assert manager.redeem(old) is None
    assert manager.redeem(new) is not None


def test_redeem_returns_project_scoped_session(ctx):
    add_project(ctx.db, "p1")
    add_project(ctx.db, "p2")
    manager = make_manager(ctx.db)
    session = manager.redeem(manager.issue_code("p1"))
    assert session and session["project_id"] == "p1"
    assert manager.verify(session["token"]) == "p1"
    # 令牌不能解锁别的项目
    assert manager.verify(session["token"]) != "p2"


def test_session_token_is_stored_hashed(ctx):
    """数据库里不能出现可直接使用的令牌明文。"""
    add_project(ctx.db, "p1")
    manager = make_manager(ctx.db)
    session = manager.redeem(manager.issue_code("p1"))
    rows = ctx.db.query_all("SELECT token_hash FROM pairing_sessions")
    assert rows
    assert all(row["token_hash"] != session["token"] for row in rows)
    # 但校验仍然通过
    assert manager.verify(session["token"]) == "p1"


def test_revoked_session_is_rejected(ctx):
    add_project(ctx.db, "p1")
    manager = make_manager(ctx.db)
    session = manager.redeem(manager.issue_code("p1"))
    assert manager.revoke(session["token"]) is True
    assert manager.verify(session["token"]) is None


def test_lan_poll_does_not_rotate_the_code(ctx):
    """局域网设备的轮询不能把用户正在看的配对码换掉。"""
    add_project(ctx.db, "p1")
    manager = make_manager(ctx.db, ttl_seconds=30)
    code = manager.issue_code("p1")
    ctx.db.execute(
        "UPDATE pairing_codes SET expires_at = ? WHERE project_id = 'p1'",
        ("2000-01-01T00:00:00+08:00",),
    )
    # 非本机读取：不轮换，也不返回码
    state = manager.current("p1")
    assert state["code"] == ""
    # 本机读取：轮换出新码
    rotated = manager.current("p1", rotate_if_expired=True)["code"]
    assert rotated and rotated != code


def test_api_requires_pairing_for_remote_clients(client, settings):
    settings.server.lan_bind = True
    client.app.state.pairing.enabled = True
    # 回环（TestClient）请求不受限
    assert client.get("/api/projects").status_code == 200


def test_redeem_endpoint_returns_token(client, ctx):
    """兑换端点：局域网设备用码换会话。"""
    add_project(ctx.db, "p-project")
    pairing = client.app.state.pairing
    pairing.enabled = True
    code = pairing.issue_code("p-project")
    response = client.post("/api/system/pairing/redeem", json={"code": code})
    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == "p-project"
    assert payload["token"]
    # 错误的码
    bad = client.post("/api/system/pairing/redeem", json={"code": "ZZZZZZZZ"})
    assert bad.status_code == 401


# --------------------------------------------------------------------------
# 中间件：项目级访问控制（模拟局域网客户端）
# --------------------------------------------------------------------------
def _remote_client(client):
    """造一个"来自局域网"的客户端。

    Starlette 的 TestClient 支持 client=("host", port)，
    直接用它把来源地址换成局域网 IP（中间件据此判定不是本机）。
    """
    from starlette.testclient import TestClient

    return TestClient(client.app, client=("192.168.1.20", 51234))


def test_remote_client_needs_a_session(client, ctx):
    add_project(ctx.db, "p-remote")
    pairing = client.app.state.pairing
    pairing.enabled = True
    remote = _remote_client(client)
    # 没有凭证 → 401
    response = remote.get("/api/projects")
    assert response.status_code == 401
    assert response.json()["code"] == "pairing_required"


def test_session_cannot_reach_another_project(client, ctx):
    """A 项目的会话不能访问 B 项目的资源。"""
    add_project(ctx.db, "p-a", "A")
    add_project(ctx.db, "p-b", "B")
    pairing = client.app.state.pairing
    pairing.enabled = True
    session = pairing.redeem(pairing.issue_code("p-a"))
    headers = {"x-godfather-session": session["token"]}
    remote = _remote_client(client)

    # 自己的项目：可以
    assert remote.get("/api/projects/p-a", headers=headers).status_code == 200
    # 别人的项目：403
    response = remote.get("/api/projects/p-b", headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "scope_mismatch"
    # 项目列表只返回自己的项目
    listing = remote.get("/api/projects", headers=headers).json()
    assert [row["id"] for row in listing] == ["p-a"]


def test_session_cannot_read_another_projects_artifact(client, ctx):
    """按对象 id 反查项目：产物也要按项目隔离。"""
    add_project(ctx.db, "p-a", "A")
    add_project(ctx.db, "p-b", "B")
    artifact = ctx.store.put_bytes("p-b", "markdown", "# 别的项目".encode("utf-8"), media_type="text/markdown")
    pairing = client.app.state.pairing
    pairing.enabled = True
    session = pairing.redeem(pairing.issue_code("p-a"))
    remote = _remote_client(client)
    response = remote.get(
        f"/api/artifacts/{artifact['id']}",
        headers={"x-godfather-session": session["token"]},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "scope_mismatch"


def test_session_cannot_read_another_projects_events(client, ctx):
    """事件流也要按项目隔离。"""
    add_project(ctx.db, "p-a", "A")
    add_project(ctx.db, "p-b", "B")
    from godfather.events import emit

    emit(ctx.db, project_id="p-b", message="B 项目的事件")
    pairing = client.app.state.pairing
    pairing.enabled = True
    session = pairing.redeem(pairing.issue_code("p-a"))
    remote = _remote_client(client)
    headers = {"x-godfather-session": session["token"]}
    # 显式要别的项目 → 直接 403
    rejected = remote.get("/api/events/history?project_id=p-b", headers=headers)
    assert rejected.status_code == 403
    # 不带项目参数 → 被端点收窄到会话自己的项目，看不到 B 的事件
    history = remote.get("/api/events/history", headers=headers).json()
    assert isinstance(history, list)
    assert all(item["message"] != "B 项目的事件" for item in history)


def test_session_cannot_create_projects(client, ctx):
    """局域网会话不能新建项目（它拿不到新项目的配对码）。"""
    add_project(ctx.db, "p-a", "A")
    pairing = client.app.state.pairing
    pairing.enabled = True
    session = pairing.redeem(pairing.issue_code("p-a"))
    remote = _remote_client(client)
    response = remote.post(
        "/api/projects",
        json={"name": "偷偷建的项目", "taskpack": "markdown_to_latex"},
        headers={"x-godfather-session": session["token"]},
    )
    assert response.status_code == 403


def test_raw_code_is_accepted_as_credential(client, ctx):
    """简单客户端（curl）可以直接用配对码，不必先换会话。"""
    add_project(ctx.db, "p-a", "A")
    pairing = client.app.state.pairing
    pairing.enabled = True
    code = pairing.issue_code("p-a")
    remote = _remote_client(client)
    response = remote.get("/api/projects/p-a", headers={"x-godfather-pairing": code})
    assert response.status_code == 200


def test_rotate_revokes_old_sessions(client, ctx):
    """重新生成配对码 → 之前配对过的设备全部失效。"""
    add_project(ctx.db, "p-a", "A")
    pairing = client.app.state.pairing
    pairing.enabled = True
    code = pairing.issue_code("p-a")
    session = pairing.redeem(code)
    assert pairing.verify(session["token"]) == "p-a"
    response = client.post("/api/system/pairing/rotate", json={"project_id": "p-a"})
    assert response.status_code == 200
    assert response.json()["code"] != code
    # 旧会话失效
    assert pairing.verify(session["token"]) is None


def test_preflight_hides_local_paths_and_budget_from_sessions(client, ctx):
    """局域网会话不需要知道本机路径与主人的预算额度。"""
    add_project(ctx.db, "p-a", "A")
    pairing = client.app.state.pairing
    pairing.enabled = True
    session = pairing.redeem(pairing.issue_code("p-a"))
    remote = _remote_client(client)
    headers = {"x-godfather-session": session["token"]}
    body = remote.get("/api/system/preflight", headers=headers).json()
    assert body["data_dir"] == ""
    assert body["budget"]["total_cents"] == 0
    # 本机照常（额度可能是 0，取决于配置，只要不是被裁剪掉就行）
    local = client.get("/api/system/preflight").json()
    assert local["data_dir"]
    assert "total_cents" in local["budget"]


# --------------------------------------------------------------------------
# 覆盖度：新增端点时不能漏掉项目归属解析
# --------------------------------------------------------------------------
def test_every_project_data_endpoint_resolves_its_project(ctx):
    """所有含项目数据的端点都必须能解析出项目归属。

    解析不出来 → 中间件不检查 → 可能越权。
    新增端点时这个测试会提醒你把它加进 scope.py 的解析规则里。
    """
    from godfather.api.scope import resolve_target_project

    add_project(ctx.db, "pA", "A")
    add_project(ctx.db, "pB", "B")
    artifact = ctx.store.put_bytes("pB", "markdown", "x".encode(), media_type="text/markdown")
    ctx.db.insert(
        "jobs",
        {
            "id": "job-1", "project_id": "pB", "type": "t", "status": "succeeded",
            "payload_json": "{}", "result_json": "{}", "error": "", "attempts": 0,
            "max_attempts": 3, "priority": 0, "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "training_runs",
        {
            "id": "run-1", "project_id": "pB", "dataset_snapshot_id": None, "backend": "hf",
            "base_model": "", "base_revision": "", "params_json": "{}", "status": "succeeded",
            "log_path": "", "metrics_json": "{}", "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "model_versions",
        {
            "id": "mv-1", "project_id": "pB", "name": "m:v001", "path": "", "status": "candidate",
            "metrics_json": "{}", "created_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "question_cards",
        {
            "id": "q-1", "project_id": "pB", "question": "?", "context_json": "{}",
            "options_json": "[]", "status": "open", "answer_json": "",
            "created_at": "2026-11-06T00:00:00+08:00", "answered_at": None,
        },
    )
    ctx.db.insert(
        "annotations",
        {
            "id": "ann-1", "project_id": "pB", "document_id": "d", "artifact_id": "a",
            "artifact_revision": "r", "page_index": 0, "viewport_rotation": 0, "bbox_json": "[]",
            "comment": "c", "category": "other", "scope": "document",
            "source_block_candidates_json": "[]", "mapping_confidence": "needs_review",
            "label_origin": "human", "status": "open", "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "training_examples",
        {
            "id": "ex-1", "project_id": "pB", "task_type": "t", "source_group_id": "g",
            "source_revision": "r", "target_payload_json": "{}", "label_origin": "rule_synthesized",
            "status": "candidate", "split": "train", "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )

    cases = [
        "/api/projects/pB",
        "/api/projects/pB/annotations",
        "/api/projects/pB/policies",
        "/api/jobs/job-1",
        "/api/jobs/job-1/cancel",
        "/api/jobs/job-1/resume",
        f"/api/artifacts/{artifact['id']}",
        f"/api/artifacts/{artifact['id']}/content",
        "/api/questions/q-1/answer",
        "/api/annotations/ann-1",
        "/api/annotations/ann-1/accept",
        "/api/examples/ex-1",
        "/api/training/runs/run-1",
        "/api/training/runs/run-1/cancel",
        "/api/training/runs/run-1/export",
        "/api/training/models/mv-1/activate",
    ]
    for path in cases:
        assert resolve_target_project(ctx, path, {}) == "pB", f"{path} 解析不到项目归属（会被漏检）"


def test_cors_preflight_is_allowed_through(client, ctx):
    """CORS 预检不带自定义头，拦掉的话跨域调用会彻底不可用。"""
    add_project(ctx.db, "p-a", "A")
    pairing = client.app.state.pairing
    pairing.enabled = True
    remote = _remote_client(client)
    response = remote.options(
        "/api/projects",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code < 400


def test_redeem_is_rate_limited(client, ctx):
    """连续输错配对码会被限速（避免把兑换接口当爆破器）。"""
    import godfather.api.projects as projects_api

    projects_api._REDEEM_FAILURES.clear()
    add_project(ctx.db, "p-a", "A")
    pairing = client.app.state.pairing
    pairing.enabled = True
    remote = _remote_client(client)
    statuses = []
    for _ in range(12):
        statuses.append(
            remote.post("/api/system/pairing/redeem", json={"code": "ZZZZZZZZ"}).status_code
        )
    assert 429 in statuses, statuses
    projects_api._REDEEM_FAILURES.clear()


# --------------------------------------------------------------------------
# 越权绕过（复审员实际复现出来的，每条都要挡住）
# --------------------------------------------------------------------------
def _seed_two_projects(ctx):
    """A、B 两个项目，B 里放一个带标记的产物。"""
    add_project(ctx.db, "pA", "A")
    add_project(ctx.db, "pB", "B")
    artifact = ctx.store.put_bytes("pB", "markdown", "SECRET-B".encode(), media_type="text/markdown")
    ctx.db.insert(
        "documents",
        {
            "id": "doc-B", "project_id": "pB", "title": "B 的文档", "source_revision": "r1",
            "status": "parsed", "policy_version": "", "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "annotations",
        {
            "id": "ann-B", "project_id": "pB", "document_id": "doc-B", "artifact_id": artifact["id"],
            "artifact_revision": "r1", "page_index": 0, "viewport_rotation": 0, "bbox_json": "[]",
            "comment": "B 的批注", "category": "other", "scope": "document",
            "source_block_candidates_json": "[]", "mapping_confidence": "needs_review",
            "label_origin": "human", "status": "open", "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "dataset_snapshots",
        {
            "id": "snap-B", "project_id": "pB", "version": "v1", "jsonl_path": "/tmp/x.jsonl",
            "sha256": "sha256:x", "counts_json": "{}", "created_at": "2026-11-06T00:00:00+08:00",
        },
    )
    return artifact


def _session_a(client, ctx):
    pairing = client.app.state.pairing
    pairing.enabled = True
    return pairing.redeem(pairing.issue_code("pA"))["token"]


def test_query_project_id_cannot_override_object_ownership(client, ctx):
    """致命绕过：?project_id=<自己项目> 覆盖对象 id 反查 → 跨项目读写。

    任何"按 id 取对象"的端点，只要带上自己项目的 project_id 参数就能读到别人的数据。
    """
    artifact = _seed_two_projects(ctx)
    token = _session_a(client, ctx)
    remote = _remote_client(client)
    headers = {"x-godfather-session": token}

    # 不带参数：403
    assert remote.get(f"/api/artifacts/{artifact['id']}", headers=headers).status_code == 403
    # 带自己项目的参数：仍然必须 403
    for path in (
        f"/api/artifacts/{artifact['id']}",
        f"/api/artifacts/{artifact['id']}/content",
        "/api/documents/doc-B",
        "/api/annotations/ann-B",
    ):
        response = remote.get(f"{path}?project_id=pA", headers=headers)
        assert response.status_code == 403, f"{path}?project_id=pA 绕过了检查（{response.status_code}）"


def test_cross_project_write_is_blocked(client, ctx):
    """跨项目写：改批注、改样本、答问题卡都必须挡住。"""
    _seed_two_projects(ctx)
    ctx.db.insert(
        "training_examples",
        {
            "id": "ex-B", "project_id": "pB", "task_type": "t", "source_group_id": "g",
            "source_revision": "r", "target_payload_json": "{}", "label_origin": "rule_synthesized",
            "status": "candidate", "split": "train", "created_at": "2026-11-06T00:00:00+08:00",
            "updated_at": "2026-11-06T00:00:00+08:00",
        },
    )
    ctx.db.insert(
        "question_cards",
        {
            "id": "q-B", "project_id": "pB", "question": "?", "context_json": "{}",
            "options_json": "[]", "status": "open", "answer_json": "",
            "created_at": "2026-11-06T00:00:00+08:00", "answered_at": None,
        },
    )
    token = _session_a(client, ctx)
    remote = _remote_client(client)
    headers = {"x-godfather-session": token}

    response = remote.patch("/api/annotations/ann-B", json={"comment": "PWN"}, headers=headers)
    assert response.status_code == 403
    assert ctx.db.query_one("SELECT comment FROM annotations WHERE id='ann-B'")["comment"] == "B 的批注"

    response = remote.patch("/api/examples/ex-B", json={"status": "accepted"}, headers=headers)
    assert response.status_code == 403
    assert ctx.db.query_one("SELECT status FROM training_examples WHERE id='ex-B'")["status"] == "candidate"

    response = remote.post("/api/questions/q-B/answer", json={"answer": "x"}, headers=headers)
    assert response.status_code == 403
    assert ctx.db.query_one("SELECT status FROM question_cards WHERE id='q-B'")["status"] == "open"


def test_body_ids_cannot_cross_projects(client, ctx):
    """body 里传的 id 也必须校验归属。

    否则可以用 A 项目的会话对 B 的文档发起生成 / 拿 B 的快照开训。
    """
    _seed_two_projects(ctx)
    token = _session_a(client, ctx)
    remote = _remote_client(client)
    headers = {"x-godfather-session": token}

    # 对 B 的文档发起生成
    response = remote.post(
        "/api/projects/pA/generations",
        json={"document_id": "doc-B", "mode": "pandoc_baseline", "provider": "ollama", "model": ""},
        headers=headers,
    )
    assert response.status_code == 404, response.status_code

    # 拿 B 的数据快照开训
    response = remote.post(
        "/api/training/runs?project_id=pA",
        json={
            "dataset_snapshot_id": "snap-B",
            "backend": "hf",
            "base_model": "x",
            "train_split": "train",
            "eval_split": "dev",
        },
        headers=headers,
    )
    assert response.status_code == 404, response.status_code

    # 往 B 的文档上挂批注
    response = remote.post(
        "/api/projects/pA/annotations",
        json={
            "document_id": "doc-B",
            "artifact_id": "art-x",
            "artifact_revision": "r1",
            "comment": "PWN",
            "category": "other",
        },
        headers=headers,
    )
    assert response.status_code == 404, response.status_code


def test_non_ascii_code_does_not_crash(client, ctx):
    """非 ASCII 配对码要返回 401，不能 500。"""
    add_project(ctx.db, "pA", "A")
    client.app.state.pairing.enabled = True
    remote = _remote_client(client)
    response = remote.post("/api/system/pairing/redeem", json={"code": "中文配对码"})
    assert response.status_code == 401
    # 非 ASCII 的令牌（走查询参数；HTTP 头不允许非 ASCII，客户端会先报错）
    response = remote.get("/api/projects?session=%E4%B8%AD%E6%96%87%E4%BB%A4%E7%89%8C")
    assert response.status_code == 401


def test_pairing_code_is_single_use(client, ctx):
    """配对码一次性：兑换后立刻作废（防止码泄漏后无限铸造会话）。"""
    add_project(ctx.db, "pA", "A")
    pairing = client.app.state.pairing
    pairing.enabled = True
    code = pairing.issue_code("pA")
    first = pairing.redeem(code)
    assert first
    # 同一个码不能再换
    assert pairing.redeem(code) is None


def test_expired_session_falls_back_to_valid_pairing_code(client, ctx):
    """过期的 session 头 + 正确的配对码：应该成功（逐个试候选凭证）。"""
    add_project(ctx.db, "pA", "A")
    pairing = client.app.state.pairing
    pairing.enabled = True
    code = pairing.issue_code("pA")
    remote = _remote_client(client)
    response = remote.get(
        "/api/projects/pA",
        headers={"x-godfather-session": "expired-token", "x-godfather-pairing": code},
    )
    assert response.status_code == 200, response.status_code


def test_session_cannot_delete_its_project(client, ctx):
    """局域网会话不能删项目（会连带删掉磁盘产物）。"""
    add_project(ctx.db, "pA", "A")
    token = _session_a(client, ctx)
    remote = _remote_client(client)
    response = remote.delete("/api/projects/pA", headers={"x-godfather-session": token})
    assert response.status_code == 403
    assert client.app.state.ctx.project("pA")


def test_rate_limit_does_not_block_a_correct_code(client, ctx):
    """限速只能挡失败的兑换：正确的码永远要能换到（否则一台设备能锁死全网）。"""
    import godfather.api.projects as projects_api

    projects_api._REDEEM_FAILURES.clear()
    add_project(ctx.db, "pA", "A")
    pairing = client.app.state.pairing
    pairing.enabled = True
    remote = _remote_client(client)
    # 连发错误码直到触发限速
    for _ in range(15):
        remote.post("/api/system/pairing/redeem", json={"code": "ZZZZZZZZ"})
    # 正确的码仍然要能兑换成功
    code = pairing.issue_code("pA")
    response = remote.post("/api/system/pairing/redeem", json={"code": code})
    assert response.status_code == 200, response.status_code
    assert response.json()["token"]
    projects_api._REDEEM_FAILURES.clear()
