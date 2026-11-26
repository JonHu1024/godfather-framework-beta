"""产物存储：内容寻址（content-addressed）的本机文件仓库。

- 文件按 sha256 存放，天然去重；数据库记录 kind / 元数据 / 业务关联。
- 产物不可变：同一内容重复写入不会覆盖已有产物（满足"成功产物不可被重试覆盖"）。
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .db import Database
from .util import new_id, now_iso, sha256_bytes, write_json


def _merge_meta(old_meta: dict, new_meta: dict) -> dict:
    """合并产物 meta。

    line_map 是"每次生成各自的"块→行号映射：同一份产物行只能存一份，
    整体覆盖会让先前的映射丢失（批注定位会错位）。
    这里按生成 id 分别保存。
    """
    merged = {**(old_meta or {}), **(new_meta or {})}
    line_map = (new_meta or {}).get("line_map")
    if isinstance(line_map, dict):
        generation_id = str((new_meta or {}).get("generation_id") or "")
        per_generation = dict(merged.get("line_maps") or {})
        if generation_id:
            per_generation[generation_id] = line_map
            merged["line_maps"] = per_generation
        merged["line_map"] = line_map
    return merged


class ArtifactStore:
    def __init__(self, db: Database, root: Path):
        self.db = db
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 内部 ----
    def _blob_path(self, digest: str) -> Path:
        hex_digest = digest.split(":", 1)[-1]
        return self.root / hex_digest[:2] / hex_digest

    # ---- 写入 ----
    def put_bytes(
        self,
        project_id: str,
        kind: str,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        parent_id: str | None = None,
        document_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict:
        digest = sha256_bytes(data)
        path = self._blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # 临时文件名必须唯一：两个进程同时写同一个 digest 时，
            # 共用 ".tmp" 会互相覆盖，写出半截文件。
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_bytes(data)
                tmp.replace(path)
            finally:
                if tmp.exists():
                    tmp.unlink()
        return self._register(project_id, kind, digest, path, media_type, parent_id, document_id, meta, len(data))

    def put_file(
        self,
        project_id: str,
        kind: str,
        file_path: str | Path,
        *,
        media_type: str = "application/octet-stream",
        parent_id: str | None = None,
        document_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict:
        file_path = Path(file_path)
        data = file_path.read_bytes()
        return self.put_bytes(
            project_id,
            kind,
            data,
            media_type=media_type,
            parent_id=parent_id,
            document_id=document_id,
            meta=meta,
        )

    def put_json(self, project_id: str, kind: str, value: Any, **kwargs: Any) -> dict:
        from .util import canonical_json

        return self.put_bytes(
            project_id,
            kind,
            canonical_json(value).encode("utf-8"),
            media_type="application/json",
            **kwargs,
        )

    def _register(
        self,
        project_id: str,
        kind: str,
        digest: str,
        path: Path,
        media_type: str,
        parent_id: str | None,
        document_id: str | None,
        meta: dict[str, Any] | None,
        size_bytes: int,
    ) -> dict:
        from .util import canonical_json

        existing = self.db.query_one(
            "SELECT * FROM artifacts WHERE revision = ? AND kind = ? AND project_id IS ?",
            (digest, kind, project_id),
        )
        if existing:
            # 同一份内容再次产出：内容是不可变的，但 meta 描述的是"这次怎么用"，
            # 直接丢弃新 meta 会让行映射等信息停留在第一次生成（批注可能错位）。
            if meta:
                from .util import canonical_json

                merged = _merge_meta(json.loads(existing["meta_json"] or "{}"), meta)
                values = {"meta_json": canonical_json(merged)}
                if self.db.has_column("artifacts", "updated_at"):
                    values["updated_at"] = now_iso()
                self.db.update("artifacts", existing["id"], values)
                existing["meta_json"] = canonical_json(merged)
            return existing
        artifact = {
            "id": new_id("art"),
            "project_id": project_id,
            "kind": kind,
            "revision": digest,
            "path": str(path),
            "media_type": media_type,
            "size_bytes": size_bytes,
            "parent_id": parent_id,
            "document_id": document_id,
            # 第一次登记也要走同样的归一化：
            # 否则第一次的 line_map 不会进 line_maps，后续合并时就只剩最新一次。
            "meta_json": canonical_json(_merge_meta({}, meta or {})),
            "created_at": now_iso(),
        }
        self.db.insert("artifacts", artifact)
        return artifact

    # ---- 读取 ----
    def get(self, artifact_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self.get(artifact_id)
        if not artifact:
            raise KeyError(f"artifact not found: {artifact_id}")
        return Path(artifact["path"]).read_bytes()

    def read_text(self, artifact_id: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(artifact_id).decode(encoding, errors="replace")

    def path_of(self, artifact_id: str) -> Path:
        artifact = self.get(artifact_id)
        if not artifact:
            raise KeyError(f"artifact not found: {artifact_id}")
        return Path(artifact["path"])

    def list_by_project(self, project_id: str, kind: str | None = None) -> list[dict]:
        if kind:
            return self.db.query_all(
                "SELECT * FROM artifacts WHERE project_id = ? AND kind = ? ORDER BY created_at DESC",
                (project_id, kind),
            )
        return self.db.query_all(
            "SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
        )

    def copy_to(self, artifact_id: str, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path_of(artifact_id), dest)
        return dest

    def merge_meta(self, artifact_id: str, meta: dict[str, Any]) -> dict[str, Any]:
        """合并更新产物的 meta。

        内容寻址的产物内容是不可变的；meta 描述的是"这份内容怎么被使用"，
        允许合并更新（但不允许整体替换丢字段）。
        """
        from .util import canonical_json

        row = self.db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if not row:
            raise KeyError(f"产物不存在：{artifact_id}")
        merged = _merge_meta(json.loads(row["meta_json"] or "{}"), meta)
        self.db.update("artifacts", artifact_id, {"meta_json": canonical_json(merged)})
        row["meta_json"] = canonical_json(merged)
        return row


def write_sidecar(path: Path, payload: Any) -> None:
    write_json(path, payload)
