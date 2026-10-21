"""Blob store: HTML parts and attachments of emails on disk (not in Mongo).

Metadata lives in the blobs collection ({_id: 32 hex, mail_id, user_id, kind, name, mime,
size, disk, created_at}), the file is BLOB_DIR/<id> (HTML is gzipped). Starred emails
are protected from deletion by TTL and by the total limit. Write limits (total, per email,
minimum free space) are checked in save_blob; copies for several recipients are
hard links to one file.
"""
import asyncio
import gzip
import logging
import os
import hashlib
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from bson import ObjectId
from bson.errors import InvalidId

from src.app.db import storage

logger = logging.getLogger(__name__)

MAINTENANCE_INTERVAL = 6 * 3600
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def blob_dir() -> str:
    return os.getenv("BLOB_DIR", "/data/blobs")


def attach_max_bytes() -> int:
    return _env_int("BLOB_ATTACH_MAX_MB", 5) * 1024 * 1024


def html_max_bytes() -> int:
    return _env_int("BLOB_HTML_MAX_KB", 512) * 1024


def ttl_days() -> int:
    return _env_int("BLOB_TTL_DAYS", 30)


def max_total_bytes() -> int:
    return _env_int("BLOB_MAX_MB", 300) * 1024 * 1024


def per_mail_max_bytes() -> int:
    return _env_int("BLOB_PER_MAIL_MB", 20) * 1024 * 1024


def min_free_bytes() -> int:
    return _env_int("BLOB_MIN_FREE_MB", 500) * 1024 * 1024


class BlobRejected(ValueError):
    """Blob not saved because of limits (size, email, disk)."""


# Total size of blobs on disk (from metadata); initialized lazily
_total: Optional[int] = None


def reset_counter() -> None:
    global _total
    _total = None


async def _current_total() -> int:
    global _total
    if _total is None:
        docs = await _coll().find({}, {"disk": 1, "size": 1}).to_list(length=None)
        _total = sum(int(d.get("disk") or d.get("size") or 0) for d in docs)
    return _total


def _free_bytes() -> int:
    os.makedirs(blob_dir(), exist_ok=True)
    return shutil.disk_usage(blob_dir()).free


def blob_path(blob_id: str) -> str:
    """Path to the blob file; the id must be exactly 32 hex chars to prevent directory traversal."""
    if not isinstance(blob_id, str) or not _ID_RE.match(blob_id):
        raise ValueError("bad blob id")
    return os.path.join(blob_dir(), blob_id)


def _coll():
    return storage.db.db.blobs


def _write_file(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _read_file(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _link_or_write(path: str, data: bytes, src: Optional[str]) -> None:
    """Copy of an email for another recipient: a hard link to an already written file
    (takes no space; deleting one copy does not affect the others)."""
    if src and os.path.exists(src):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            os.link(src, path)
            return
        except OSError:
            pass
    _write_file(path, data)


async def save_blob(
    data: bytes, *, mail_id: str, user_id: Optional[int], kind: str, name: str, mime: str
) -> str:
    """Saves a blob and returns its id. BlobRejected (ValueError) when limits are exceeded:
    file size, total per email, BLOB_MAX_MB, low disk space."""
    global _total
    if kind not in ("html", "att"):
        raise ValueError("kind must be html|att")
    if kind == "html" and len(data) > html_max_bytes():
        raise BlobRejected("html too large")
    if kind == "att" and len(data) > attach_max_bytes():
        raise BlobRejected("attachment too large")
    stored = gzip.compress(data) if kind == "html" else data
    disk = len(stored)
    mail_id = str(mail_id)

    # Per-email limit (all its blobs combined)
    used = await _coll().find({"mail_id": mail_id}, {"size": 1}).to_list(length=None)
    if sum(int(d.get("size") or 0) for d in used) + len(data) > per_mail_max_bytes():
        raise BlobRejected("per-mail limit")

    # Global limit: when exceeded, clean synchronously; if it still doesn't fit, reject
    if await _current_total() + disk > max_total_bytes():
        await enforce_blob_limits(reserve=disk)
        if await _current_total() + disk > max_total_bytes():
            logger.warning(f"Blob store full (BLOB_MAX_MB): {name} not saved")
            raise BlobRejected("blob store full")

    # Free space on the partition
    free = await asyncio.to_thread(_free_bytes)
    if free - disk < min_free_bytes():
        logger.warning(f"Low disk space ({free // 1048576} MB): {name} not saved")
        raise BlobRejected("low disk space")

    # Dedup for several recipients of one email: same sha256 -> hard link
    sha = hashlib.sha256(stored).hexdigest()
    twin = await _coll().find_one({"sha": sha, "kind": kind})
    src = blob_path(twin["_id"]) if twin else None

    blob_id = uuid.uuid4().hex
    _total = await _current_total() + disk  # reserve before writing
    try:
        await asyncio.to_thread(_link_or_write, blob_path(blob_id), stored, src)
        await _coll().insert_one(
            {
                "_id": blob_id,
                "mail_id": mail_id,
                "user_id": user_id,
                "kind": kind,
                "name": name,
                "mime": mime,
                "size": len(data),
                "disk": disk,
                "sha": sha,
                "created_at": datetime.now(timezone.utc),
            }
        )
    except Exception:
        _total = max(0, (_total or 0) - disk)
        await asyncio.to_thread(_remove_file, blob_path(blob_id))
        raise
    return blob_id


async def get_blob_meta(blob_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(blob_id, str) or not _ID_RE.match(blob_id):
        return None
    return await _coll().find_one({"_id": blob_id})


async def read_html(blob_id: str) -> Optional[str]:
    meta = await get_blob_meta(blob_id)
    if not meta or meta.get("kind") != "html":
        return None
    raw = await asyncio.to_thread(_read_file, blob_path(blob_id))
    if raw is None:
        return None
    try:
        return gzip.decompress(raw).decode("utf-8", errors="replace")
    except (OSError, EOFError):
        logger.warning(f"blob {blob_id}: not gzip")
        return None


async def _delete_by_ids(blob_ids: List[str]) -> int:
    global _total
    if not blob_ids:
        return 0
    docs = await _coll().find({"_id": {"$in": blob_ids}}, {"disk": 1, "size": 1}).to_list(length=None)
    freed = sum(int(d.get("disk") or d.get("size") or 0) for d in docs)
    for bid in blob_ids:
        await asyncio.to_thread(_remove_file, blob_path(bid))
    await _coll().delete_many({"_id": {"$in": blob_ids}})
    if _total is not None:
        _total = max(0, _total - freed)
    return len(blob_ids)


async def delete_blobs_for_mails(mail_ids: List[str]) -> int:
    """Deletes files and metadata of the blobs of the given emails; returns their count."""
    ids = [str(m) for m in mail_ids]
    if not ids:
        return 0
    docs = await _coll().find({"mail_id": {"$in": ids}}, {"_id": 1}).to_list(length=None)
    return await _delete_by_ids([d["_id"] for d in docs])


# --- Candidate selection (pure functions, covered by tests/test_data_v2.py) ---


def select_expired(
    metas: Iterable[Dict[str, Any]], starred_mail_ids: Set[str], now: datetime, ttl: int
) -> List[str]:
    """Ids of blobs older than ttl days, except starred emails. ttl<=0 means TTL is off."""
    if ttl <= 0:
        return []
    border = now - timedelta(days=ttl)
    return [
        m["_id"]
        for m in metas
        if m["created_at"] < border and m["mail_id"] not in starred_mail_ids
    ]


def select_over_limit(
    metas: List[Dict[str, Any]], starred_mail_ids: Set[str], limit_bytes: int
) -> List[str]:
    """Oldest blobs of NON-starred emails while the total size on disk exceeds the limit.
    Starred sizes count toward the total, but those blobs are never deleted."""
    total = sum(int(m.get("disk") or m.get("size") or 0) for m in metas)
    out: List[str] = []
    if limit_bytes <= 0 or total <= limit_bytes:
        return out
    for m in sorted(metas, key=lambda x: x["created_at"]):
        if total <= limit_bytes:
            break
        if m["mail_id"] in starred_mail_ids:
            continue
        out.append(m["_id"])
        total -= int(m.get("disk") or m.get("size") or 0)
    return out


async def _starred_mail_ids(mail_ids: Iterable[str]) -> Set[str]:
    oids = []
    for m in set(mail_ids):
        try:
            oids.append(ObjectId(m))
        except (InvalidId, TypeError):
            continue
    if not oids:
        return set()
    docs = await storage.db.db.mails.find(
        {"_id": {"$in": oids}, "starred": True}, {"_id": 1}
    ).to_list(length=None)
    return {str(d["_id"]) for d in docs}


async def enforce_blob_limits(
    now: Optional[datetime] = None, reserve: int = 0
) -> Dict[str, int]:
    """TTL, then the total limit (reserve is how many bytes must be freed for a new write). Returns {"expired": n, "over_limit": n}."""
    now = now or datetime.now(timezone.utc)
    global _total
    metas = await _coll().find({}).sort("created_at", 1).to_list(length=None)
    # Also recompute the counter from the actual metadata
    _total = sum(int(m.get("disk") or m.get("size") or 0) for m in metas)
    if not metas:
        return {"expired": 0, "over_limit": 0}
    starred = await _starred_mail_ids(m["mail_id"] for m in metas)
    expired = select_expired(metas, starred, now, ttl_days())
    await _delete_by_ids(expired)
    gone = set(expired)
    left = [m for m in metas if m["_id"] not in gone]
    over = select_over_limit(left, starred, max(0, max_total_bytes() - reserve))
    await _delete_by_ids(over)
    return {"expired": len(expired), "over_limit": len(over)}


async def blob_maintenance_loop():
    """Every 6 hours: blob TTL and limit."""
    while True:
        try:
            res = await enforce_blob_limits()
            if res["expired"] or res["over_limit"]:
                logger.info(f"Blob maintenance: {res}")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Blob maintenance failed")
        await asyncio.sleep(MAINTENANCE_INTERVAL)
