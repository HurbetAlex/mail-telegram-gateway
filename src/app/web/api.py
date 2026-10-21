import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.parse import quote
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl

from aiohttp import web
from bson import ObjectId

from src.app.bot.auth import (
    ADMIN_ID,
    ALLOWED_IDS,
    DOMAIN_OWNERS,
    can_access,
    domain_of,
    scope,
    user_domains,
    validate_alias_address,
)
from src.app import monitor
from src.app.bot.sender import bot, send_telegram_message
from src.app.db import storage
from src.app.mail import blobstore, composer
from src.app.mail import unsubscribe as unsubscribe_mod

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PAGE_SIZE = 20
INIT_DATA_MAX_AGE = 24 * 3600

# Sending emails from the Mini App
MAX_RCPTS = 10
MAX_SUBJECT = 300
MAX_BODY = 50_000
MAX_ATTACHMENTS = 10
SEND_LIMIT = 20            # sends ...
SEND_WINDOW = 10 * 60      # ... per 10 minutes per user
TEMPLATE_NAME_MAX = 64
TEMPLATE_TEXT_MAX = 4000
# Request body with attachments up to 15 MB in base64 (~20 MB) plus headroom
CLIENT_MAX_SIZE = 24 * 1024 * 1024

_STRICT_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+'-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$"
)
_MIME_RE = re.compile(r"^[A-Za-z0-9][\w.+-]*/[A-Za-z0-9][\w.+-]*$")
_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")

# user_id -> timestamps of recent sends (in process memory)
_send_log: Dict[int, Deque[float]] = {}


def validate_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    """Verify the Telegram WebApp initData signature. Returns the user or None."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    given_hash = pairs.pop("hash", None)
    if not given_hash:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, given_hash):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", 0)) > INIT_DATA_MAX_AGE:
            return None
        return json.loads(pairs.get("user", "{}"))
    except (ValueError, json.JSONDecodeError):
        return None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    user = validate_init_data(request.headers.get("X-Init-Data", ""))
    if not user or user.get("id") not in ALLOWED_IDS:
        return web.json_response({"error": "unauthorized"}, status=401)
    request["user_id"] = int(user["id"])
    return await handler(request)


def _ttl_days() -> int:
    """Email retention period (0 = auto-cleanup disabled). Read from env on every call."""
    try:
        return max(0, int(os.getenv("MAIL_TTL_DAYS", "180")))
    except ValueError:
        return 180


def _bad(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def _brief(m: Dict[str, Any]) -> Dict[str, Any]:
    ts = m.get("timestamp")
    return {
        "id": str(m["_id"]),
        "from": m.get("from") or "",
        "to": m.get("to") or "",
        "orig_to": m.get("orig_to") or "",
        "forwarded_by": m.get("forwarded_by") or "",
        "subject": m.get("subject") or "",
        "status": m.get("status") or "",
        "otp": m.get("otp"),
        "preview": m.get("body_preview") or "",
        "date": ts.isoformat() if hasattr(ts, "isoformat") else None,
        "starred": bool(m.get("starred")),
        "bulk": bool(m.get("bulk")),
        "alias_note": m.get("alias_note") or "",
        "unsubscribe": bool(m.get("unsubscribe")),
        "unsubscribed": bool(m.get("unsubscribed_at")),
        # Old emails without a read field count as read
        "read": m.get("read") is not False,
        "has_attachments": bool(m.get("attachments")),
        "has_html": bool(m.get("html_id")),
    }


def _expires_at(m: Dict[str, Any]) -> Optional[str]:
    """When auto-cleanup will delete the email: time from _id + TTL; None for starred emails or when cleanup is disabled."""
    ttl = _ttl_days()
    if ttl <= 0 or m.get("starred"):
        return None
    created = getattr(m.get("_id"), "generation_time", None)
    if created is None:
        return None
    return (created + timedelta(days=ttl)).isoformat()


def _oid_or_400(request: web.Request) -> Optional[str]:
    oid = request.match_info.get("oid", "")
    return oid if ObjectId.is_valid(oid) else None


async def _json_body(request: web.Request) -> Optional[Dict[str, Any]]:
    """Request body as a JSON object; anything else (garbage, array, empty) gives None."""
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


async def list_mails(request: web.Request) -> web.Response:
    q = request.query
    query = q.get("query", "").strip()[:200]
    if q.get("bulk") == "1":
        query = f"{query} is:bulk".strip()  # the "Newsletters" chip: same search operator
    status = q.get("status", "").strip() or None
    try:
        page = max(0, int(q.get("page", 0)))
    except ValueError:
        page = 0

    # Admin sees everything; everyone else sees only their own emails
    user_filter = scope(request["user_id"])
    skip = page * PAGE_SIZE
    unread = await storage.db.count_unread(user_filter)

    if q.get("expiring") == "1":
        ttl = _ttl_days()
        if ttl <= 0:
            return web.json_response({"total": 0, "page": page, "items": [], "has_more": False, "ttl_days": 0, "unread": unread})
        mails, total = await storage.db.get_expiring(
            ttl, user_id=user_filter, skip=skip, limit=PAGE_SIZE
        )
        return web.json_response(
            {"total": total, "page": page, "items": [_brief(m) for m in mails],
             "has_more": total > skip + PAGE_SIZE, "ttl_days": _ttl_days(), "unread": unread}
        )

    if q.get("starred") == "1":
        # No total count: fetch one extra record to tell whether another page exists
        rows = await storage.db.get_starred(user_filter, skip=skip, limit=PAGE_SIZE + 1)
        has_more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        return web.json_response(
            {"total": skip + len(rows) + (1 if has_more else 0), "page": page,
             "items": [_brief(m) for m in rows], "has_more": has_more,
             "ttl_days": _ttl_days(), "unread": unread}
        )

    mails, total = await storage.db.get_history(
        query, skip=skip, limit=PAGE_SIZE, user_id=user_filter, status=status
    )
    return web.json_response(
        {"total": total, "page": page, "items": [_brief(m) for m in mails],
         "has_more": total > skip + PAGE_SIZE, "ttl_days": _ttl_days(), "unread": unread}
    )


async def _load_accessible(request: web.Request):
    """Email by {oid} with an access check: (mail, None) or (None, ready error response)."""
    oid = _oid_or_400(request)
    if oid is None:
        return None, _bad("invalid id")
    mail = await storage.db.get_mail(oid)
    if not mail:
        return None, _bad("not found", 404)
    # Emails without user_id (spam/muted/catch-all/old) are admin-only
    if not can_access(request["user_id"], mail):
        return None, _bad("forbidden", 403)
    return mail, None


async def get_mail(request: web.Request) -> web.Response:
    mail, err = await _load_accessible(request)
    if err:
        return err
    data = _brief(mail)
    data["body"] = mail.get("full_body") or ""
    data["headers"] = [
        [str(h[0]), str(h[1])]
        for h in (mail.get("headers") or [])
        if isinstance(h, (list, tuple)) and len(h) >= 2
    ]
    data["envelope_from"] = mail.get("envelope_from") or ""
    ua = mail.get("unsubscribed_at")
    data["unsubscribed_at"] = ua.isoformat() if hasattr(ua, "isoformat") else None
    data["expires_at"] = _expires_at(mail)
    data["message_id"] = mail.get("message_id") or ""
    data["can_rule"] = True  # access to the email is already checked: rules are personal (admin's are global)
    data["attachments"] = [
        {"id": a.get("id"), "name": a.get("name") or "", "mime": a.get("mime") or "", "size": int(a.get("size") or 0)}
        for a in (mail.get("attachments") or [])
        if isinstance(a, dict)
    ]
    return web.json_response(data)


async def star_mail(request: web.Request) -> web.Response:
    mail, err = await _load_accessible(request)
    if err:
        return err
    body = await _json_body(request)
    if body is None or not isinstance(body.get("starred"), bool):
        return _bad("starred must be boolean")
    ok = await storage.db.set_starred(
        str(mail["_id"]), body["starred"], user_id=scope(request["user_id"])
    )
    if not ok:
        return _bad("not found", 404)
    return web.json_response({"ok": True, "starred": body["starred"]})


async def _unsubscribe(mail: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Unsubscribe for an email (shared by single, batch and the "Newsletters" tab)."""
    if not mail.get("unsubscribe"):
        return "none", None
    # No repeat POST to the sender needed, same as the bot button
    if mail.get("unsubscribed_at"):
        return "already", None
    status, info = await unsubscribe_mod.unsubscribe(mail)
    if status in ("posted", "mailed"):
        await storage.db.set_unsubscribed(str(mail["_id"]))
    return status, info


async def unsubscribe_mail(request: web.Request) -> web.Response:
    mail, err = await _load_accessible(request)
    if err:
        return err
    if not mail.get("unsubscribe"):
        return _bad("no unsubscribe option")
    status, info = await _unsubscribe(mail)
    return web.json_response({"status": status, "info": info})


def _alias_json(a: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(a["_id"]),
        "address": a.get("address") or "",
        "note": a.get("note") or "",
        "disabled": bool(a.get("disabled")),
        "mail_count": int(a.get("mail_count") or 0),
        "sender_domains": list(a.get("sender_domains") or []),
    }


async def list_aliases(request: web.Request) -> web.Response:
    rows = await storage.db.list_aliases(scope(request["user_id"]))
    return web.json_response({"items": [_alias_json(a) for a in rows]})


async def create_alias(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return _bad("invalid json")
    name, note = body.get("name"), body.get("note", "")
    if not isinstance(name, str) or not isinstance(note, str):
        return _bad("name and note must be strings")
    uid = request["user_id"]
    address, error = validate_alias_address(uid, name)
    if error:
        return _bad(error)
    try:
        doc = await storage.db.create_alias(uid, address, note.strip()[:200])
    except ValueError as e:
        return _bad(str(e), 409)
    return web.json_response(_alias_json(doc), status=201)


def _alias_id_or_none(request: web.Request) -> Optional[str]:
    aid = request.match_info.get("aid", "")
    return aid if ObjectId.is_valid(aid) else None


async def toggle_alias(request: web.Request) -> web.Response:
    aid = _alias_id_or_none(request)
    if aid is None:
        return _bad("invalid id")
    uid = scope(request["user_id"])
    current = next(
        (a for a in await storage.db.list_aliases(uid) if str(a["_id"]) == aid), None
    )
    if current is None:
        return _bad("not found", 404)
    disabled = not current.get("disabled")
    if not await storage.db.set_alias_disabled(aid, uid, disabled):
        return _bad("not found", 404)
    return web.json_response({"ok": True, "disabled": disabled})


async def delete_alias(request: web.Request) -> web.Response:
    aid = _alias_id_or_none(request)
    if aid is None:
        return _bad("invalid id")
    if not await storage.db.delete_alias(aid, scope(request["user_id"])):
        return _bad("not found", 404)
    return web.json_response({"ok": True})


# --- Parity with the bot: profile, stats, search, admin actions, sending ---

def _is_admin(request: web.Request) -> bool:
    return request["user_id"] == ADMIN_ID


def _forbidden_admin() -> web.Response:
    return _bad("Admin only", 403)


def _iso(dt: Any) -> Optional[str]:
    return dt.isoformat() if hasattr(dt, "isoformat") else None


async def get_me(request: web.Request) -> web.Response:
    uid = request["user_id"]
    is_admin = uid == ADMIN_ID
    users: List[Dict[str, Any]] = []
    if is_admin:
        users.append({"user_id": ADMIN_ID, "domain": domain_of(ADMIN_ID)})
        users += [
            {"user_id": u, "domain": d}
            for d, u in sorted(DOMAIN_OWNERS.items())
            if u != ADMIN_ID
        ]
    return web.json_response(
        {
            "user_id": uid,
            "is_admin": is_admin,
            "from_addresses": await composer.user_from_addresses(uid, domain_of(uid)),
            "domains": sorted(user_domains(uid)),
            "ttl_days": _ttl_days(),
            "users": users,
            "unread": await storage.db.count_unread(scope(uid)),
        }
    )


async def get_stats(request: web.Request) -> web.Response:
    flt = scope(request["user_id"])
    by_status = await storage.db.get_stats(user_id=flt)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    last_24h = await storage.db.get_counts_since(since, user_id=flt)
    routes = len(await storage.db.get_all_routes()) if flt is None else None
    return web.json_response(
        {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "last_24h": last_24h,
            "starred": await storage.db.count_starred(flt),
            "aliases": await storage.db.count_aliases(flt),
            "routes": routes,
        }
    )


async def whereis(request: web.Request) -> web.Response:
    q = request.query.get("q", "").strip()[:100]
    if not q:
        return _bad("Empty query")
    rows = await storage.db.whereis(q, scope(request["user_id"]))
    return web.json_response(
        {
            "items": [
                {
                    "to": r.get("to") or "",
                    "via": r.get("forwarded_by"),
                    "count": int(r.get("count") or 0),
                    "first": _iso(r.get("first")),
                    "last": _iso(r.get("last")),
                    "sample_from": r.get("sender") or "",
                }
                for r in rows
            ]
        }
    )


async def list_unassigned(request: web.Request) -> web.Response:
    if not _is_admin(request):
        return _forbidden_admin()
    mails = await storage.db.get_unassigned()
    return web.json_response({"items": [_brief(m) for m in mails]})


async def assign_mail(request: web.Request) -> web.Response:
    """"Me" button: the email goes to the admin in Telegram and gets the delivered status."""
    if not _is_admin(request):
        return _forbidden_admin()
    mail, err = await _load_accessible(request)
    if err:
        return err
    if mail.get("status") != "unassigned":
        return _bad("Email is already assigned", 409)
    uid = request["user_id"]
    oid = str(mail["_id"])
    tg_msg_id = await send_telegram_message(uid, mail, oid=oid)
    if not tg_msg_id:
        return _bad("Failed to forward the email to Telegram", 502)
    await storage.db.set_mail_status(oid, "delivered")
    await storage.db.add_delivery(oid, uid, tg_msg_id)
    return web.json_response({"ok": True})


async def rule_for_mail(request: web.Request) -> web.Response:
    """Rule for the email's sender (🔕 button, /mute, /allow): personal, global for the admin."""
    mail, err = await _load_accessible(request)
    if err:
        return err
    body = await _json_body(request)
    if body is None:
        return _bad("Invalid JSON")
    action, target = body.get("action"), body.get("target")
    if action not in ("mute", "allow"):
        return _bad("action: mute or allow")
    if target not in ("address", "domain"):
        return _bad("target: address or domain")
    sender = parseaddr(str(mail.get("from") or ""))[1].lower()
    if "@" not in sender:
        return _bad("Could not parse the sender address")
    pattern = sender if target == "address" else sender.rpartition("@")[2]
    if not pattern:
        return _bad("Could not parse the sender address")
    await storage.db.add_rule(pattern, action, user_id=scope(request["user_id"]))
    return web.json_response({"ok": True, "pattern": pattern})


def _parse_recipients(raw: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    if not isinstance(raw, str):
        return None, "Specify a recipient"
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    if not parts:
        return None, "Specify a recipient"
    if len(parts) > MAX_RCPTS:
        return None, f"No more than {MAX_RCPTS} recipients"
    out: List[str] = []
    for p in parts:
        if len(p) > 254 or not _STRICT_EMAIL_RE.match(p):
            return None, f"Invalid address: {p[:80]}"
        if p.lower() not in (x.lower() for x in out):
            out.append(p)
    return out, None


def _clean_filename(name: Any) -> str:
    """Attachment name without paths and control characters."""
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip().strip(".")
    return name[:200] or "file.bin"


def _parse_attachments(raw: Any) -> Tuple[Optional[List[Tuple[str, bytes, str]]], Optional[str]]:
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "attachments must be an array"
    if len(raw) > MAX_ATTACHMENTS:
        return None, f"No more than {MAX_ATTACHMENTS} attachments"
    out: List[Tuple[str, bytes, str]] = []
    total = 0
    for a in raw:
        if not isinstance(a, dict) or not isinstance(a.get("data"), str):
            return None, "Invalid attachment"
        # Estimate before decoding: avoid inflating memory on an obviously oversized
        total += len(a["data"]) * 3 // 4
        if total > composer.MAX_ATTACHMENT_SIZE + 3:
            return None, "Attachments exceed 15 MB"
        try:
            data = base64.b64decode(a["data"], validate=True)
        except (binascii.Error, ValueError):
            return None, "Attachment: invalid base64"
        mime = a.get("mime")
        mime = mime if isinstance(mime, str) and _MIME_RE.match(mime) else "application/octet-stream"
        out.append((_clean_filename(a.get("name")), data, mime))
    if sum(len(d) for _, d, _ in out) > composer.MAX_ATTACHMENT_SIZE:
        return None, "Attachments exceed 15 MB"
    return out, None


def _rate_limited(user_id: int) -> bool:
    """Sliding window: SEND_LIMIT sends per SEND_WINDOW seconds. An attempt counts."""
    now = time.monotonic()
    log = _send_log.setdefault(user_id, deque())
    while log and now - log[0] > SEND_WINDOW:
        log.popleft()
    if len(log) >= SEND_LIMIT:
        return True
    log.append(now)
    return False


async def _mail_for_compose(user_id: int, oid: Any):
    """Email for reply/forward with an access check: (mail, None) or (None, error)."""
    if not isinstance(oid, str) or not ObjectId.is_valid(oid):
        return None, _bad("Invalid email id")
    mail = await storage.db.get_mail(oid)
    if not mail:
        return None, _bad("Email not found", 404)
    if not can_access(user_id, mail):
        return None, _bad("No access to this email", 403)
    return mail, None


async def send_mail(request: web.Request) -> web.Response:
    uid = request["user_id"]
    body = await _json_body(request)
    if body is None:
        return _bad("Invalid JSON")

    from_addr = composer.validate_from_address(uid, body.get("from"))
    if from_addr is None:
        return _bad("This sender address is not available to you")

    rcpts, error = _parse_recipients(body.get("to"))
    if error:
        return _bad(error)

    subject, text = body.get("subject", ""), body.get("body", "")
    if not isinstance(subject, str) or not isinstance(text, str):
        return _bad("subject and body must be strings")
    subject = " ".join(subject.split())  # no line breaks (header injection)
    if len(subject) > MAX_SUBJECT:
        return _bad(f"Subject is longer than {MAX_SUBJECT} characters")
    if len(text) > MAX_BODY:
        return _bad(f"Text is longer than {MAX_BODY} characters")

    attachments, error = _parse_attachments(body.get("attachments"))
    if error:
        return _bad(error)

    reply_to, forward_of = body.get("reply_to"), body.get("forward_of")
    if reply_to and forward_of:
        return _bad("Cannot reply and forward at the same time")

    data: Dict[str, Any] = {
        "mode": "new",
        "from_addr": from_addr,
        "to": rcpts,
        "subject": subject or "(no subject)",
        "body_text": text,
        "body_html": "",
        "attachments": attachments,
    }
    if reply_to:
        mail, err = await _mail_for_compose(uid, reply_to)
        if err:
            return err
        st = composer.reply_state(mail)
        if st is None:
            return _bad("Could not parse the sender address")
        # "Re: ..." subject and thread headers work as in the bot; from/to are set by the user
        data.update(
            mode="reply",
            subject=subject or st["subject"],
            in_reply_to=st["in_reply_to"],
            orig_from=st["orig_from"],
            orig_body=st["orig_body"],
        )
    elif forward_of:
        mail, err = await _mail_for_compose(uid, forward_of)
        if err:
            return err
        st = composer.fwd_state(mail)
        data.update(
            mode="fwd",
            subject=subject or st["subject"],
            orig_from=st["orig_from"],
            orig_subject=st["orig_subject"],
            orig_body=st["orig_body"],
        )
    elif not text.strip() and not attachments:
        return _bad("Empty email")

    if _rate_limited(uid):
        return _bad("Too many sends, wait a few minutes", 429)
    try:
        await composer.send_and_record(uid, data)
    except Exception as e:
        logger.exception("API: failed to send mail")
        return _bad(f"Failed to send: {e}", 502)
    return web.json_response({"ok": True})


# --- Templates ---

def _template_json(t: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": str(t["_id"]), "name": t.get("name") or "", "text": t.get("text") or ""}


async def list_templates(request: web.Request) -> web.Response:
    rows = await storage.db.get_templates(request["user_id"])
    return web.json_response({"items": [_template_json(t) for t in rows]})


async def create_template(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return _bad("Invalid JSON")
    name, text = body.get("name"), body.get("text")
    if not isinstance(name, str) or not isinstance(text, str):
        return _bad("name and text must be strings")
    name, text = name.strip(), text.strip()
    if not name or not text:
        return _bad("Both name and text are required")
    if len(name) > TEMPLATE_NAME_MAX:
        return _bad(f"Name is longer than {TEMPLATE_NAME_MAX} characters")
    if len(text) > TEMPLATE_TEXT_MAX:
        return _bad(f"Text is longer than {TEMPLATE_TEXT_MAX} characters")
    doc = await storage.db.add_template(request["user_id"], name, text)
    return web.json_response({"item": _template_json(doc)}, status=201)


async def delete_template(request: web.Request) -> web.Response:
    tid = request.match_info.get("tid", "")
    if not ObjectId.is_valid(tid):
        return _bad("Invalid id")
    tpl = await storage.db.get_template(tid)
    # Someone else's template is indistinguishable from a missing one
    if not tpl or tpl.get("user_id") != request["user_id"]:
        return _bad("Template not found", 404)
    if not await storage.db.delete_template(tid):
        return _bad("Template not found", 404)
    return web.json_response({"ok": True})


# --- Routes (admin) ---

def _route_json(r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for kind in ("prefix", "domain", "regex"):
        if r.get(kind):
            return {
                "kind": kind,
                "value": str(r[kind]),
                "user_id": r.get("user_id"),
                "disabled": bool(r.get("disabled")),
            }
    return None


async def list_routes(request: web.Request) -> web.Response:
    if not _is_admin(request):
        return _forbidden_admin()
    items = [j for j in map(_route_json, await storage.db.get_all_routes()) if j]
    return web.json_response({"items": items})


def _route_key(body: Optional[Dict[str, Any]]) -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    """(kind, value) from the request body or an error; regex routes are not editable via the API."""
    if body is None:
        return None, "Invalid JSON"
    kind, value = body.get("kind"), body.get("value")
    if kind not in ("prefix", "domain") or not isinstance(value, str) or not value.strip():
        return None, "kind: prefix or domain, value is a string"
    value = value.strip().lower()
    return (kind, value.lstrip("@") if kind == "domain" else value), None


async def create_route(request: web.Request) -> web.Response:
    if not _is_admin(request):
        return _forbidden_admin()
    body = await _json_body(request)
    key, error = _route_key(body)
    if error:
        return _bad(error)
    kind, value = key
    user_id = body.get("user_id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return _bad("user_id must be a number")
    if kind == "prefix":
        if not _PREFIX_RE.match(value):
            return _bad("Prefix: Latin letters, digits, . _ + - (up to 64 characters)")
        await storage.db.add_route(value, user_id)
    else:
        if not _DOMAIN_RE.match(value):
            return _bad("Invalid domain")
        await storage.db.add_domain_route(value, user_id)
    return web.json_response({"ok": True, "kind": kind, "value": value, "user_id": user_id}, status=201)


async def toggle_route(request: web.Request) -> web.Response:
    if not _is_admin(request):
        return _forbidden_admin()
    key, error = _route_key(await _json_body(request))
    if error:
        return _bad(error)
    kind, value = key
    enabled = (
        await storage.db.toggle_route(value)
        if kind == "prefix"
        else await storage.db.toggle_domain_route(value)
    )
    if enabled is None:
        return _bad("Route not found", 404)
    return web.json_response({"disabled": not enabled})


async def delete_route(request: web.Request) -> web.Response:
    if not _is_admin(request):
        return _forbidden_admin()
    key, error = _route_key(await _json_body(request))
    if error:
        return _bad(error)
    kind, value = key
    deleted = (
        await storage.db.delete_route(value)
        if kind == "prefix"
        else await storage.db.delete_domain_route(value)
    )
    if not deleted:
        return _bad("Route not found", 404)
    return web.json_response({"ok": True})


# --- Rules (personal; the admin's are global) ---

async def list_rules(request: web.Request) -> web.Response:
    rows = await storage.db.get_rules(scope(request["user_id"]))
    return web.json_response(
        {
            "items": [
                {
                    "id": str(r["_id"]),
                    "pattern": r.get("pattern") or "",
                    "action": r.get("action") or "mute",
                    "user_id": r.get("user_id"),  # null = global
                }
                for r in rows
            ]
        }
    )


async def create_rule(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return _bad("Invalid JSON")
    pattern, action = body.get("pattern"), body.get("action")
    if action not in ("mute", "allow"):
        return _bad("action: mute or allow")
    pattern = pattern.strip().lower() if isinstance(pattern, str) else ""
    if not pattern or len(pattern) > 254 or re.search(r"\s", pattern):
        return _bad("pattern: sender address or domain")
    await storage.db.add_rule(pattern, action, user_id=scope(request["user_id"]))
    return web.json_response({"ok": True, "pattern": pattern, "action": action}, status=201)


async def delete_rule(request: web.Request) -> web.Response:
    rid = request.match_info.get("rid", "")
    if not ObjectId.is_valid(rid):
        return _bad("Invalid id")
    # A non-admin deletes only their own rules (filtered by user_id in storage)
    if not await storage.db.delete_rule_by_oid(rid, scope(request["user_id"])):
        return _bad("Rule not found", 404)
    return web.json_response({"ok": True})


# --- v2: read state, HTML, attachments, deletion, batch actions ---

MAX_BATCH = 100
MAX_SENDERS = 50
BATCH_ACTIONS = ("star", "unstar", "read", "unread", "delete", "unsubscribe")
# Active types are served as binary: the browser must not execute or render them
_ACTIVE_MIME_PREFIXES = (
    "text/html", "application/xhtml", "image/svg", "text/xml", "application/xml",
    "text/javascript", "application/javascript", "application/x-javascript",
    "application/x-shockwave-flash", "text/xsl", "application/xslt",
)


def _safe_mime(mime: Any) -> str:
    m = str(mime or "").strip().lower()
    if not _MIME_RE.match(m) or m.startswith(_ACTIVE_MIME_PREFIXES) or m.endswith("+xml"):
        return "application/octet-stream"
    return m


def _ids_from(body: Optional[Dict[str, Any]], key: str = "ids", limit: int = MAX_BATCH):
    """List of string ids from the body: (list, None) or (None, error)."""
    if body is None:
        return None, "Invalid JSON"
    ids = body.get(key)
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        return None, f"{key}: non-empty array of strings"
    if len(ids) > limit:
        return None, f"No more than {limit} items"
    return list(dict.fromkeys(ids)), None


async def mark_read(request: web.Request) -> web.Response:
    body = await _json_body(request)
    ids, error = _ids_from(body)
    if error:
        return _bad(error)
    if not isinstance(body.get("read"), bool):
        return _bad("read must be boolean")
    n = await storage.db.mark_read(ids, body["read"], scope(request["user_id"]))
    return web.json_response({"updated": n})


async def get_mail_html(request: web.Request) -> web.Response:
    mail, err = await _load_accessible(request)
    if err:
        return err
    html_id = mail.get("html_id")
    html_text = await blobstore.read_html(html_id) if html_id else None
    if html_text is None:
        return _bad("HTML not stored", 404)
    return web.json_response({"html": html_text})


def _read_blob_file(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


async def get_blob(request: web.Request) -> web.Response:
    meta = await blobstore.get_blob_meta(request.match_info.get("bid", ""))
    if not meta or meta.get("kind") != "att":
        return _bad("File not found", 404)
    mail = await storage.db.get_mail(str(meta.get("mail_id") or ""))
    if not mail:
        return _bad("File not found", 404)
    if not can_access(request["user_id"], mail):
        return _bad("No access", 403)
    data = await asyncio.to_thread(_read_blob_file, blobstore.blob_path(meta["_id"]))
    if data is None:
        return _bad("File not found", 404)
    name = _clean_filename(meta.get("name"))
    return web.Response(
        body=data,
        content_type=_safe_mime(meta.get("mime")),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private",
            "Content-Security-Policy": "sandbox",
        },
    )


async def _delete_tg_messages(deliveries: List[Dict[str, Any]]) -> None:
    """Bot messages for deleted emails; a failure (older than 48 h, already deleted) does not matter."""
    for d in deliveries:
        try:
            await bot.delete_message(chat_id=d["user_id"], message_id=d["tg_message_id"])
        except Exception:
            logger.debug("tg delete failed for %s", d, exc_info=True)


async def _delete_mails(user_id: int, ids: List[str]) -> int:
    """Deletes emails (access already checked) plus Telegram messages and blobs."""
    res = await storage.db.delete_mails(ids, scope(user_id))
    await _delete_tg_messages(res.get("deliveries") or [])
    if res.get("mail_ids"):
        try:
            await blobstore.delete_blobs_for_mails(res["mail_ids"])
        except Exception:
            logger.warning("blob cleanup failed", exc_info=True)
    return int(res.get("deleted") or 0)


async def delete_mail(request: web.Request) -> web.Response:
    mail, err = await _load_accessible(request)
    if err:
        return err
    if not await _delete_mails(request["user_id"], [str(mail["_id"])]):
        return _bad("not found", 404)
    return web.json_response({"ok": True})


async def batch_mails(request: web.Request) -> web.Response:
    uid = request["user_id"]
    body = await _json_body(request)
    ids, error = _ids_from(body)
    if error:
        return _bad(error)
    action = body.get("action")
    if action not in BATCH_ACTIONS:
        return _bad("action: " + ", ".join(BATCH_ACTIONS))

    results: Dict[str, str] = {}
    mails: Dict[str, Dict[str, Any]] = {}
    for i in ids:
        if not ObjectId.is_valid(i):
            results[i] = "invalid"
            continue
        m = await storage.db.get_mail(i)
        if not m:
            results[i] = "not_found"
        elif not can_access(uid, m):
            results[i] = "forbidden"
        else:
            mails[i] = m

    if action in ("star", "unstar"):
        for i in mails:
            ok = await storage.db.set_starred(i, action == "star", user_id=scope(uid))
            results[i] = "ok" if ok else "not_found"
    elif action in ("read", "unread"):
        await storage.db.mark_read(list(mails), action == "read", scope(uid))
        results.update({i: "ok" for i in mails})
    elif action == "delete":
        if mails:
            await _delete_mails(uid, list(mails))
        results.update({i: "ok" for i in mails})
    else:  # unsubscribe
        for i, m in mails.items():
            results[i] = (await _unsubscribe(m))[0]

    done_statuses = ("posted", "mailed") if action == "unsubscribe" else ("ok",)
    return web.json_response(
        {"done": sum(1 for v in results.values() if v in done_statuses), "results": results}
    )


# --- v2: newsletters, codes, contacts, settings, server ---

async def list_newsletters(request: web.Request) -> web.Response:
    rows = await storage.db.get_newsletters(scope(request["user_id"]))
    items = [dict(r, last=_iso(r.get("last"))) for r in rows]
    return web.json_response({"items": items})


def _senders_from(body: Optional[Dict[str, Any]]):
    senders, error = _ids_from(body, "senders", MAX_SENDERS)
    if error:
        return None, error
    senders = [x.strip().lower() for x in senders]
    if any(not x or len(x) > 254 or re.search(r"\s", x) for x in senders):
        return None, "senders: sender addresses"
    return list(dict.fromkeys(senders)), None


async def newsletters_unsubscribe(request: web.Request) -> web.Response:
    uid = request["user_id"]
    body = await _json_body(request)
    senders, error = _senders_from(body)
    if error:
        return _bad(error)
    known = {n["sender"]: n for n in await storage.db.get_newsletters(scope(uid))}
    results: Dict[str, str] = {}
    for sender in senders:
        n = known.get(sender)
        mail = await storage.db.get_mail(n["latest_mail_id"]) if n else None
        if not mail:
            results[sender] = "not_found"
        elif not can_access(uid, mail):
            results[sender] = "forbidden"
        else:
            results[sender] = (await _unsubscribe(mail))[0]
    return web.json_response({"results": results})


async def newsletters_mute(request: web.Request) -> web.Response:
    body = await _json_body(request)
    senders, error = _senders_from(body)
    if error:
        return _bad(error)
    target = body.get("target")
    if target not in ("address", "domain"):
        return _bad("target: address or domain")
    owner = scope(request["user_id"])  # admin: global rules
    patterns = []
    for sender in senders:
        pattern = sender if target == "address" else sender.rpartition("@")[2]
        if not pattern:
            return _bad(f"Invalid sender: {sender[:80]}")
        patterns.append(pattern)
    for pattern in dict.fromkeys(patterns):
        await storage.db.add_rule(pattern, "mute", user_id=owner)
    return web.json_response({"ok": True, "patterns": list(dict.fromkeys(patterns))})


async def list_codes(request: web.Request) -> web.Response:
    rows = await storage.db.get_codes(scope(request["user_id"]))
    items = [
        {
            "mail_id": r.get("mail_id"),
            "otp": r.get("otp"),
            "from": r.get("from") or "",
            "subject": r.get("subject") or "",
            "at": _iso(r.get("at")),
        }
        for r in rows
    ]
    return web.json_response({"items": items})


async def list_contacts(request: web.Request) -> web.Response:
    q = request.query.get("q", "").strip()[:100]
    items = await storage.db.get_contacts(request["user_id"], q)
    return web.json_response({"items": items})


async def get_settings(request: web.Request) -> web.Response:
    return web.json_response(await storage.db.get_settings(request["user_id"]))


async def update_settings(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return _bad("Invalid JSON")
    patch = body["patch"] if isinstance(body.get("patch"), dict) else body
    try:
        data = await storage.db.set_settings(request["user_id"], patch)
    except ValueError as e:
        return _bad(str(e))
    return web.json_response(data)


async def get_server(request: web.Request) -> web.Response:
    if not _is_admin(request):
        return _forbidden_admin()
    return web.json_response(await monitor.server_status())


async def server_unban(request: web.Request) -> web.Response:
    if not _is_admin(request):
        return _forbidden_admin()
    body = await _json_body(request)
    ip = body.get("ip") if body else None
    try:
        addr = ipaddress.ip_address(ip.strip()) if isinstance(ip, str) else None
    except ValueError:
        addr = None
    if addr is None:
        return _bad("Invalid IP address")
    try:
        monitor.request_unban(str(addr))
    except ValueError:
        return _bad("Invalid IP address")
    return web.json_response({"ok": True})


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Unexpected errors go to the log; only a generic 500 without a traceback goes out."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("API error on %s %s", request.method, request.path)
        return _bad("internal error", 500)


def make_app() -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, auth_middleware], client_max_size=CLIENT_MAX_SIZE
    )
    app.router.add_get("/api/mails", list_mails)
    app.router.add_get("/api/mails/{oid}", get_mail)
    app.router.add_post("/api/mails/{oid}/star", star_mail)
    app.router.add_post("/api/mails/{oid}/unsubscribe", unsubscribe_mail)
    app.router.add_get("/api/aliases", list_aliases)
    app.router.add_post("/api/aliases", create_alias)
    app.router.add_post("/api/aliases/{aid}/toggle", toggle_alias)
    app.router.add_delete("/api/aliases/{aid}", delete_alias)
    app.router.add_post("/api/mails/read", mark_read)
    app.router.add_post("/api/mails/batch", batch_mails)
    app.router.add_delete("/api/mails/{oid}", delete_mail)
    app.router.add_get("/api/mails/{oid}/html", get_mail_html)
    app.router.add_get("/api/blobs/{bid}", get_blob)
    app.router.add_get("/api/newsletters", list_newsletters)
    app.router.add_post("/api/newsletters/unsubscribe", newsletters_unsubscribe)
    app.router.add_post("/api/newsletters/mute", newsletters_mute)
    app.router.add_get("/api/codes", list_codes)
    app.router.add_get("/api/contacts", list_contacts)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", update_settings)
    app.router.add_get("/api/server", get_server)
    app.router.add_post("/api/server/unban", server_unban)
    app.router.add_get("/api/me", get_me)
    app.router.add_get("/api/stats", get_stats)
    app.router.add_get("/api/whereis", whereis)
    app.router.add_get("/api/unassigned", list_unassigned)
    app.router.add_post("/api/mails/{oid}/assign", assign_mail)
    app.router.add_post("/api/mails/{oid}/rule", rule_for_mail)
    app.router.add_post("/api/send", send_mail)
    app.router.add_get("/api/templates", list_templates)
    app.router.add_post("/api/templates", create_template)
    app.router.add_delete("/api/templates/{tid}", delete_template)
    app.router.add_get("/api/routes", list_routes)
    app.router.add_post("/api/routes", create_route)
    app.router.add_post("/api/routes/toggle", toggle_route)
    app.router.add_post("/api/routes/delete", delete_route)
    app.router.add_get("/api/rules", list_rules)
    app.router.add_post("/api/rules", create_rule)
    app.router.add_delete("/api/rules/{rid}", delete_rule)
    return app


async def start_api(port: int = 8080) -> web.AppRunner:
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"API server started on port {port}")
    return runner
