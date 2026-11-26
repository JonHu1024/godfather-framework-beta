"""局域网配对凭证（项目级）。

设计要求（§5.5）：
- 首版：同一局域网中，用带短期配对凭证的链接或二维码连接桌面服务；
- 登录后的会话有访问控制。

**项目级**（第三轮加强）：
早先只有一个全局配对码 —— 拿到码的人可以访问**全部**项目。
现在改成每个项目一个配对码：

- 桌面端（本机回环请求）为每个项目签发短期配对码（默认 10 分钟过期）；
- 局域网设备用某个项目的配对码兑换，得到一个**绑定该项目**的会话令牌；
- 会话令牌只能访问那个项目的资源，访问别的项目一律 403。

实现要点：
- 配对码与会话都存在 SQLite 里：worker 可能是独立进程，内存态共享不了；
- 会话令牌只存哈希（sha256），数据库被拷走也换不出可用令牌；
- 只保护 API；静态资源（前端与 PDF.js）照常加载，否则页面无法启动。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from typing import Any

# 配对码字符集：去掉容易看错的 0/O/1/l
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 8
# 会话令牌有效期：手机端不想每 10 分钟重输一次码
SESSION_TTL_SECONDS = 7 * 24 * 3600


def _random_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def _parse_iso(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


class PairingManager:
    """项目级配对码与会话。

    与早先的全局码相比，行为变化：
    - ``issue_code(project_id)`` 给**某个项目**发码；
    - ``current(project_id=...)`` 读某个项目的码；
    - ``redeem(code)`` 用码换会话，返回 {token, project_id, expires_at}；
    - ``verify(token)`` 校验会话，返回它解锁的项目 id（None = 无效）。
    """

    def __init__(
        self,
        db: Any,
        ttl_seconds: int = 600,
        enabled: bool = False,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
    ):
        self.db = db
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.enabled = enabled
        self.session_ttl_seconds = max(300, int(session_ttl_seconds))

    # ---- 配对码 ----

    def issue_code(self, project_id: str) -> str:
        """给项目签发配对码（覆盖旧的）。"""
        code = _random_code()
        now = time.time()
        self.db.execute(
            "INSERT INTO pairing_codes(project_id, code, created_at, expires_at) "
            "VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
            "code=excluded.code, created_at=excluded.created_at, expires_at=excluded.expires_at",
            (project_id, code, _iso(now), _iso(now + self.ttl_seconds)),
        )
        return code

    def current(self, project_id: str = "", *, rotate_if_expired: bool = False) -> dict:
        """当前配对码。

        默认**不**轮换：局域网设备轮询这个接口就会把用户正在看的码换掉，
        手机刚输入的码会突然失效。只有本机显式要求时才轮换。
        """
        if not self.enabled:
            return {"required": False, "code": "", "expires_in": 0, "project_id": ""}
        if not project_id:
            # 不指定项目：只说明"需要配对"，不给码（码是项目级的）
            return {"required": True, "code": "", "expires_in": 0, "project_id": ""}
        row = self.db.query_one("SELECT * FROM pairing_codes WHERE project_id = ?", (project_id,))
        expires = 0.0
        if row:
            try:
                expires = _parse_iso(row["expires_at"])
            except (TypeError, ValueError):
                expires = 0.0
        if expires <= time.time():
            if rotate_if_expired:
                code = self.issue_code(project_id)
                return {
                    "required": True,
                    "code": code,
                    "expires_in": self.ttl_seconds,
                    "project_id": project_id,
                }
            return {"required": True, "code": "", "expires_in": 0, "project_id": project_id}
        return {
            "required": True,
            "code": row["code"],
            "expires_in": int(max(0, expires - time.time())),
            "project_id": project_id,
        }

    def rotate(self, project_id: str) -> str:
        return self.issue_code(project_id)

    # ---- 会话 ----

    def redeem(self, code: str) -> dict | None:
        """用配对码兑换项目级会话。

        成功返回 {token, project_id, expires_at}，失败返回 None。
        """
        if not self.enabled:
            return None
        cleaned = (code or "").strip().upper()
        if not cleaned:
            return None
        # compare_digest 不支持非 ASCII 字符串（会抛 TypeError → 500），
        # 所以先挡掉；码本身也只可能是 ASCII。
        if not cleaned.isascii():
            return None
        rows = self.db.query_all("SELECT * FROM pairing_codes")
        now = time.time()
        for row in rows:
            if not secrets.compare_digest(str(row["code"]).upper(), cleaned):
                continue
            try:
                expires = _parse_iso(row["expires_at"])
            except (TypeError, ValueError):
                continue
            if expires <= now:
                continue
            # 一次性：兑换成功后立刻作废这个码。
            # 不这样的话，码泄漏一次就能无限铸造长期会话。
            # 桌面端下一轮轮询会自动签发新码（缺失即过期 → 重新签发）。
            self.db.execute("DELETE FROM pairing_codes WHERE project_id = ?", (row["project_id"],))
            token = secrets.token_urlsafe(32)
            self.db.execute(
                "INSERT INTO pairing_sessions(token_hash, project_id, created_at, expires_at, last_seen_at) "
                "VALUES(?,?,?,?,?)",
                (
                    _hash_token(token),
                    row["project_id"],
                    _iso(now),
                    _iso(now + self.session_ttl_seconds),
                    _iso(now),
                ),
            )
            return {
                "token": token,
                "project_id": row["project_id"],
                "expires_at": _iso(now + self.session_ttl_seconds),
            }
        return None

    def verify(self, token: str | None) -> str | None:
        """校验会话令牌，返回它解锁的项目 id；无效返回 None。"""
        if not self.enabled:
            return ""
        if not token:
            return None
        row = self.db.query_one(
            "SELECT * FROM pairing_sessions WHERE token_hash = ?", (_hash_token(token),)
        )
        if not row:
            return None
        try:
            expires = _parse_iso(row["expires_at"])
        except (TypeError, ValueError):
            return None
        if expires <= time.time():
            self.revoke(token)
            return None
        self.db.execute(
            "UPDATE pairing_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (_iso(time.time()), _hash_token(token)),
        )
        return row["project_id"]

    def revoke(self, token: str) -> bool:
        """注销会话（桌面端"踢掉手机"）。"""
        if not token:
            return False
        cur = self.db.execute(
            "DELETE FROM pairing_sessions WHERE token_hash = ?", (_hash_token(token),)
        )
        return cur.rowcount > 0

    def revoke_project_sessions(self, project_id: str) -> int:
        """注销某个项目的全部会话（重新生成配对码时调用）。"""
        cur = self.db.execute(
            "DELETE FROM pairing_sessions WHERE project_id = ?", (project_id,)
        )
        return cur.rowcount

    def sessions(self, project_id: str = "") -> list[dict]:
        """列出会话（桌面端可见，用于"踢掉手机"）。"""
        if project_id:
            rows = self.db.query_all(
                "SELECT * FROM pairing_sessions WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            )
        else:
            rows = self.db.query_all("SELECT * FROM pairing_sessions ORDER BY created_at DESC")
        return [
            {
                "project_id": row["project_id"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "last_seen_at": row["last_seen_at"],
            }
            for row in rows
        ]
