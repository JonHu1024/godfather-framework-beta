"""SQLite 元数据层：连接、建表、轻量查询助手。

设计约束：
- 元数据全部放 SQLite；产物（PDF/TeX/图片）放本机文件目录。
- 所有时间戳为 ISO8601 带时区字符串。
- 不做 ORM：表少、结构稳定，手写 SQL 更易审计与迁移。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_VERSION = 2

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 项目 ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    taskpack TEXT NOT NULL DEFAULT 'markdown_to_latex',
    description TEXT DEFAULT '',
    active_model TEXT DEFAULT '',
    policy_version TEXT DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 导入的原始材料（不可变原件 + 内容哈希） -----------------------------
CREATE TABLE IF NOT EXISTS source_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'text/markdown',
    sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    artifact_id TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    dialect TEXT NOT NULL DEFAULT 'pandoc-markdown',
    status TEXT NOT NULL DEFAULT 'imported',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_src_project ON source_documents(project_id);

-- 参考案例（md/tex/pdf 三元组或子集） ---------------------------------
CREATE TABLE IF NOT EXISTS reference_examples (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    md_artifact_id TEXT,
    tex_artifact_id TEXT,
    pdf_artifact_id TEXT,
    notes TEXT DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'project',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ref_project ON reference_examples(project_id);

-- 产物（内容寻址） -----------------------------------------------------
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    revision TEXT NOT NULL,
    path TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    parent_id TEXT,
    document_id TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_project ON artifacts(project_id, kind);

-- 文档：解析后的结构块 -------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    source_document_id TEXT REFERENCES source_documents(id) ON DELETE SET NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'parsed',
    policy_version TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_project ON documents(project_id);

CREATE TABLE IF NOT EXISTS document_revisions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    blocks_json TEXT NOT NULL DEFAULT '[]',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docrev_doc ON document_revisions(document_id);

-- 生成轮次 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS generations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
    mode TEXT NOT NULL DEFAULT 'tool_assisted',
    status TEXT NOT NULL DEFAULT 'queued',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    policy_version TEXT DEFAULT '',
    prompt_version TEXT DEFAULT '',
    job_id TEXT,
    tex_artifact_id TEXT,
    pdf_artifact_id TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gen_project ON generations(project_id);

-- 四层质量检查 --------------------------------------------------------
CREATE TABLE IF NOT EXISTS validation_reports (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    generation_id TEXT REFERENCES generations(id) ON DELETE CASCADE,
    layer TEXT NOT NULL,
    verdict TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_val_gen ON validation_reports(generation_id);

-- 批注（绑定 artifact + revision + 归一化坐标） -------------------------
CREATE TABLE IF NOT EXISTS annotations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT,
    artifact_id TEXT NOT NULL,
    artifact_revision TEXT NOT NULL,
    page_index INTEGER NOT NULL DEFAULT 0,
    viewport_rotation INTEGER NOT NULL DEFAULT 0,
    bbox_json TEXT NOT NULL DEFAULT '[]',
    comment TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'other',
    scope TEXT NOT NULL DEFAULT 'document',
    source_block_candidates_json TEXT NOT NULL DEFAULT '[]',
    mapping_confidence TEXT NOT NULL DEFAULT 'needs_review',
    label_origin TEXT NOT NULL DEFAULT 'human',
    status TEXT NOT NULL DEFAULT 'open',
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ann_project ON annotations(project_id, document_id);

-- 策略版本与偏好 ------------------------------------------------------
CREATE TABLE IF NOT EXISTS policies (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    content_yaml TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policy_project ON policies(project_id);

CREATE TABLE IF NOT EXISTS policy_preferences (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    pref_id TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'project',
    scope_value TEXT DEFAULT '',
    conditions_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 50,
    confidence REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'active',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    conflicts_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

-- 训练样本与数据快照 --------------------------------------------------
CREATE TABLE IF NOT EXISTS training_examples (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    source_group_id TEXT NOT NULL DEFAULT '',
    source_revision TEXT NOT NULL DEFAULT '',
    input_payload_json TEXT NOT NULL DEFAULT '{}',
    target_payload_json TEXT NOT NULL DEFAULT '{}',
    teacher_json TEXT NOT NULL DEFAULT '{}',
    policy_version TEXT DEFAULT '',
    prompt_version TEXT DEFAULT '',
    reference_ids_json TEXT NOT NULL DEFAULT '[]',
    annotation_ids_json TEXT NOT NULL DEFAULT '[]',
    label_origin TEXT NOT NULL DEFAULT 'rule_synthesized',
    validation_report_id TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    split TEXT NOT NULL DEFAULT 'train',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_example_project ON training_examples(project_id, status);

CREATE TABLE IF NOT EXISTS dataset_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    jsonl_path TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    counts_json TEXT NOT NULL DEFAULT '{}',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'frozen',
    created_at TEXT NOT NULL
);

-- 训练与评测（M1） -------------------------------------------------------
CREATE TABLE IF NOT EXISTS training_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    dataset_snapshot_id TEXT REFERENCES dataset_snapshots(id) ON DELETE SET NULL,
    backend TEXT NOT NULL DEFAULT 'local',
    base_model TEXT NOT NULL DEFAULT '',
    base_revision TEXT DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    log_path TEXT DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    candidate_model_id TEXT,
    baseline_model_id TEXT,
    split TEXT NOT NULL DEFAULT 'dev',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    per_example_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    base_model TEXT DEFAULT '',
    base_revision TEXT DEFAULT '',
    tokenizer_revision TEXT DEFAULT '',
    chat_template TEXT DEFAULT '',
    quantization TEXT DEFAULT '',
    path TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- 资源锁（GPU 等稀缺资源的串行调度） -------------------------------------
CREATE TABLE IF NOT EXISTS resource_locks (
    id TEXT PRIMARY KEY,
    resource TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL DEFAULT '',
    project_id TEXT,
    acquired_at TEXT NOT NULL,
    expires_at TEXT,
    note TEXT DEFAULT ''
);

-- 任务队列（数据库任务表 + 租约） --------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 100,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner TEXT DEFAULT '',
    lease_expires_at TEXT,
    idempotency_key TEXT,
    progress_done INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    stage TEXT DEFAULT '',
    error TEXT DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_status ON jobs(status, priority);
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_idem ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_joblog_job ON job_logs(job_id);

-- 进度事件（SSE 源，跨进程可读） ---------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    run_id TEXT,
    job_id TEXT,
    stage TEXT NOT NULL DEFAULT '',
    done INTEGER,
    total INTEGER,
    ts TEXT NOT NULL,
    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    cost_json TEXT NOT NULL DEFAULT '{}',
    message TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_event_project ON events(project_id, id);

-- 外部调用审计 --------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_calls (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    cache_key TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    cost_status TEXT NOT NULL DEFAULT 'unknown',
    cost_cents REAL NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

-- 教师调用缓存查询会按 (cache_key, project_id, status) 过滤，没有索引就是全表扫描
CREATE INDEX IF NOT EXISTS idx_call_cache ON provider_calls(cache_key, project_id, status);
CREATE INDEX IF NOT EXISTS idx_call_project ON provider_calls(project_id, created_at);

-- 局域网配对：每个项目一个配对码 ----------------------------------------
-- 配对码是短期的（桌面端本地可见），兑换后得到项目级的会话令牌。
CREATE TABLE IF NOT EXISTS pairing_codes (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pairing_sessions (
    token_hash TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pairing_session_project ON pairing_sessions(project_id);

-- 控制器动作 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS controller_actions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    arguments_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'proposed',
    validation_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    executed_at TEXT
);

-- 问答卡（需要用户回答的关键问题） ------------------------------------
CREATE TABLE IF NOT EXISTS question_cards (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id TEXT,
    question TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    options_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    answer_json TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    answered_at TEXT
);
"""


# 增量迁移：从旧版本 schema 升级时补充新列（幂等）
MIGRATIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "annotations",
        [
            # 批注绑定文档版本：文档重新解析后，旧批注的源块编号可能已经变了
            ("document_revision", "INTEGER DEFAULT 0"),
        ],
    ),
    (
        "jobs",
        [
            ("idempotency_key", "TEXT"),
            ("lease_owner", "TEXT DEFAULT ''"),
            ("lease_expires_at", "TEXT"),
        ],
    ),
    (
        "artifacts",
        [
            ("updated_at", "TEXT DEFAULT ''"),
        ],
    ),
    (
        "training_runs",
        [
            ("device", "TEXT DEFAULT ''"),
            ("adapter_path", "TEXT DEFAULT ''"),
            ("merged_path", "TEXT DEFAULT ''"),
            ("gguf_path", "TEXT DEFAULT ''"),
            ("quantized_path", "TEXT DEFAULT ''"),
            ("ollama_name", "TEXT DEFAULT ''"),
            ("checkpoint_path", "TEXT DEFAULT ''"),
            ("error", "TEXT DEFAULT ''"),
            ("started_at", "TEXT"),
            ("finished_at", "TEXT"),
            ("notes", "TEXT DEFAULT ''"),
        ],
    ),
    (
        "model_versions",
        [
            ("source_run_id", "TEXT DEFAULT ''"),
            ("previous_version", "TEXT DEFAULT ''"),
            ("deployed_at", "TEXT"),
            ("notes", "TEXT DEFAULT ''"),
        ],
    ),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, spec in columns:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def _dedupe_job_idempotency_keys(conn: sqlite3.Connection) -> None:
    """清理重复的 idempotency_key。

    老库里可能已经有重复的幂等键（加唯一索引之前的历史数据），
    直接建唯一索引会抛 IntegrityError，整个应用起不来。
    这里保留最新的一条，其余置空（置空不受唯一索引约束）。
    """
    try:
        rows = conn.execute(
            "SELECT idempotency_key, COUNT(*) AS c FROM jobs "
            "WHERE idempotency_key IS NOT NULL AND idempotency_key != '' "
            "GROUP BY idempotency_key HAVING c > 1"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        key = row[0]
        keep = conn.execute(
            "SELECT id FROM jobs WHERE idempotency_key = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if not keep:
            continue
        conn.execute(
            "UPDATE jobs SET idempotency_key = NULL WHERE idempotency_key = ? AND id != ?",
            (key, keep[0]),
        )


class Database:
    """线程安全的 SQLite 封装（每线程一个连接）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    # -- 连接管理 --
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- 初始化 --
    def initialize(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            # 先补列再建索引：老库可能缺 jobs.idempotency_key 这类列，
            # 先执行建索引的脚本会直接报错，进程根本起不来。
            _apply_migrations(self.conn)
            _dedupe_job_idempotency_keys(self.conn)
            self.conn.executescript(SCHEMA_SQL)
            _apply_migrations(self.conn)
            previous = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            if previous:
                try:
                    stored = int(previous[0])
                except (TypeError, ValueError):
                    stored = 0
                if stored > SCHEMA_VERSION:
                    import logging

                    logging.getLogger("godfather").warning(
                        "数据目录的 schema 版本是 %d，高于当前代码的 %d；"
                        "请升级 Godfather 或换一个数据目录",
                        stored,
                        SCHEMA_VERSION,
                    )
            self.conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._trim_history()
            self._initialized = True

    def _trim_history(self) -> None:
        """启动时清理过期的事件与日志。

        写入时会裁剪，但长期不写库的库（比如只跑训练、不产生事件的时段）
        永远不会触发裁剪。启动时补一次。
        """
        try:
            from .events import EVENT_RETENTION, EVENT_RETENTION_DAYS, _retention_threshold

            self.conn.execute(
                "DELETE FROM events WHERE ts < ?",
                (_retention_threshold(EVENT_RETENTION_DAYS),),
            )
            total = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            if total > EVENT_RETENTION:
                cutoff = self.conn.execute(
                    "SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?", (EVENT_RETENTION - 1,)
                ).fetchone()
                if cutoff:
                    self.conn.execute("DELETE FROM events WHERE id <= ?", (cutoff[0],))
            self.conn.execute(
                "DELETE FROM job_logs WHERE ts < ?",
                (_retention_threshold(EVENT_RETENTION_DAYS),),
            )
        except Exception:  # noqa: BLE001
            # 清理失败不能阻止启动
            return

    # -- 查询助手 --
    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query_all(self, sql: str, params: Sequence[Any] | dict = ()) -> list[dict]:
        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] | dict = ()) -> dict | None:
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Sequence[Any] | dict = ()) -> Any:
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务。

        嵌套调用要支持：内层用 SAVEPOINT，外层回滚时一起回滚。
        直接 BEGIN IMMEDIATE 会抛 "cannot start a transaction within a transaction"。
        """
        conn = self.conn
        nested = getattr(self._local, "in_transaction", False)
        if nested:
            name = f"sp_{id(self)}"
            conn.execute(f"SAVEPOINT {name}")
            try:
                yield conn
            except Exception:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
                raise
            else:
                conn.execute(f"RELEASE {name}")
            return
        conn.execute("BEGIN IMMEDIATE")
        self._local.in_transaction = True
        try:
            yield conn
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute("COMMIT")
        finally:
            self._local.in_transaction = False

    def has_column(self, table: str, column: str) -> bool:
        """表里是否已有某列（迁移前的老库可能没有）。"""
        try:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            return False
        return any(row[1] == column for row in rows)

    def insert(self, table: str, values: dict[str, Any]) -> dict:
        cols = list(values.keys())
        placeholders = ", ".join(["?"] * len(cols))
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        self.conn.execute(sql, [values[c] for c in cols])
        return values

    def update(self, table: str, row_id: str, values: dict[str, Any], id_column: str = "id") -> None:
        if not values:
            return
        cols = list(values.keys())
        assignments = ", ".join(f"{c} = ?" for c in cols)
        sql = f"UPDATE {table} SET {assignments} WHERE {id_column} = ?"
        self.conn.execute(sql, [*values.values(), row_id])

    def rows_to_dicts(self, rows: Iterable[sqlite3.Row]) -> list[dict]:
        return [dict(row) for row in rows]
