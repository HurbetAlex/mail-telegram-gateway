"""Shared logic for composing and sending emails: bot and Mini App API.

The bot keeps the draft in FSM data, the API builds the same dict from the request body,
so body, quote, forward and saving "sent" are computed in one place.
"""
import html
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Tuple

from src.app.bot.auth import ALIAS_LOCAL_RE, user_domains
from src.app.db import storage
from src.app.mail.smtp import paragraphize, send_email

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_SIZE = 15 * 1024 * 1024
QUOTE_LIMIT = 3000
FWD_BODY_LIMIT = 20000


def _esc(s) -> str:
    return html.escape(str(s or ""))


def final_bodies(data: dict) -> Tuple[str, Optional[str]]:
    """Builds the final text and html bodies with a quote (reply) or an embedded email (fwd)."""
    text = data.get("body_text") or ""
    html_frag = paragraphize(data.get("body_html") or _esc(text))
    mode = data.get("mode")
    orig_body = data.get("orig_body") or ""
    orig_from = data.get("orig_from") or ""

    if mode == "reply" and orig_body:
        quoted = "\n".join("> " + line for line in orig_body.splitlines())
        text = f"{text}\n\n{orig_from} wrote:\n{quoted}"
        orig_html = _esc(orig_body).replace("\n", "<br>")
        html_frag += (
            f'<div style="margin-top:24px;color:#6b7280;font-size:14px;">{_esc(orig_from)} wrote:</div>'
            f'<blockquote style="margin:8px 0 0;padding:4px 0 4px 16px;border-left:3px solid #e5e7eb;'
            f'color:#6b7280;font-size:14px;line-height:1.5;">{orig_html}</blockquote>'
        )
    elif mode == "fwd":
        orig_subject = data.get("orig_subject") or ""
        header = (
            "---------- Forwarded message ----------\n"
            f"From: {orig_from}\nSubject: {orig_subject}\n\n"
        )
        text = (f"{text}\n\n" if text else "") + header + orig_body
        orig_html = _esc(orig_body).replace("\n", "<br>")
        html_frag += (
            f'<div style="margin-top:24px;padding:16px;background:#f9fafb;border-radius:8px;">'
            f'<div style="color:#9ca3af;font-size:12px;margin-bottom:8px;">'
            f"Forwarded message · From: {_esc(orig_from)} · Subject: {_esc(orig_subject)}</div>"
            f'<div style="color:#374151;font-size:14px;line-height:1.5;">{orig_html}</div></div>'
        )

    return text, html_frag or None


class InvalidSender(ValueError):
    """The From address does not belong to the user's domains."""


def default_from_address(user_id: int) -> Optional[str]:
    """Default sender for the user: <first label>@subdomain, else mail@<domain>."""
    domains = sorted(user_domains(user_id))
    for d in domains:
        if d.count(".") >= 2:
            return validate_from_address(user_id, f"{d.split('.')[0]}@{d}")
    for d in domains:
        return validate_from_address(user_id, f"mail@{d}")
    return None


def _reply_from(user_id: int, mail: Dict[str, Any]) -> str:
    """The address the mail was sent to, only if it is the user's own; otherwise the default.

    The recipient of a stored mail is attacker-influenced (any local part on our domain is
    accepted), so it is never trusted as a From address without validation.
    """
    return validate_from_address(user_id, mail.get("to")) or default_from_address(user_id) or ""


def reply_state(mail: Dict[str, Any], user_id: int) -> Optional[Dict[str, Any]]:
    """Initial data for replying to an email; None if the sender address cannot be parsed."""
    _, orig_from_addr = parseaddr(str(mail.get("from") or ""))
    if not orig_from_addr:
        return None
    subject = str(mail.get("subject") or "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return dict(
        mode="reply",
        from_addr=_reply_from(user_id, mail),
        to=[orig_from_addr],
        subject=subject,
        in_reply_to=mail.get("message_id"),
        orig_from=str(mail.get("from") or ""),
        orig_body=(mail.get("full_body") or "")[:QUOTE_LIMIT],
        attachments=[],
        quote_note="+ quote of the original email",
    )


def fwd_state(mail: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    """Initial data for forwarding an email."""
    subject = str(mail.get("subject") or "")
    if not subject.lower().startswith("fwd:"):
        subject = f"Fwd: {subject}"
    return dict(
        mode="fwd",
        from_addr=_reply_from(user_id, mail),
        subject=subject,
        orig_from=str(mail.get("from") or ""),
        orig_subject=str(mail.get("subject") or ""),
        orig_body=(mail.get("full_body") or "")[:FWD_BODY_LIMIT],
        attachments=[],
        quote_note="+ the original email in full",
    )


async def send_and_record(user_id: int, data: Dict[str, Any]) -> str:
    """Sends an email from a draft and saves it to history (status sent).

    The send_email exception is propagated: the caller decides (the bot writes to the chat, the API returns 502).
    """
    from_addr = validate_from_address(user_id, data.get("from_addr"))
    if from_addr is None:
        raise InvalidSender("Sender address is not allowed for this user")
    data = {**data, "from_addr": from_addr}
    text, html_frag = final_bodies(data)
    msg_id = await send_email(
        from_addr=data["from_addr"],
        to_addrs=data["to"],
        subject=data["subject"],
        body_text=text,
        html_content=html_frag,
        attachments=data.get("attachments"),
        in_reply_to=data.get("in_reply_to"),
    )
    await storage.db.save_mail(
        {
            "message_id": msg_id,
            "from": data["from_addr"],
            "to": ", ".join(data["to"]),
            "subject": data["subject"],
            "body_preview": text[:200],
            "full_body": text,
            "status": "sent",
            "user_id": user_id,
            "timestamp": datetime.now(timezone.utc),
        }
    )
    try:
        # Contacts for autocomplete read the sent collection
        await storage.db.log_sent(user_id, data["from_addr"], data["to"], data["subject"], msg_id)
    except Exception:
        logger.warning("log_sent failed", exc_info=True)
    return msg_id


async def user_from_addresses(user_id: int, domain: str) -> List[str]:
    """Suggestions for the From field: sent -> enabled routes/aliases -> received
    by frequency (top 10) -> default address for the domain. No duplicates, lowercase,
    only addresses that pass validate_from_address."""
    domains = user_domains(user_id)
    ordered: List[str] = []

    def add(addr: Any) -> None:
        a = validate_from_address(user_id, addr)
        if a and a not in ordered:
            ordered.append(a)

    for a in await storage.db.get_sent_from_addresses(user_id):
        add(a)
    routes = await storage.db.get_all_routes()
    for p in sorted(
        {r["prefix"] for r in routes
         if r.get("user_id") == user_id and r.get("prefix") and not r.get("disabled")}
    ):
        add(f"{p}@{domain}")
    for al in await storage.db.list_aliases(user_id):
        if al.get("address") and not al.get("disabled"):
            add(al["address"])
    for a in await storage.db.get_recipient_addresses(user_id, domains, limit=10):
        add(a)
    # Default: subdomain -> <first label>@subdomain; apex only if the list is empty
    for d in sorted(domains):
        if d.count(".") >= 2:
            add(f"{d.split('.')[0]}@{d}")
    if not ordered:
        for d in sorted(domains):
            add(f"mail@{d}")
    return ordered


def validate_from_address(user_id: int, raw: Any) -> Optional[str]:
    """Sender address -> normalized address or None.

    Any local-part is allowed (same validation as aliases) on the user's domains;
    a foreign domain and an invalid local-part are rejected.
    """
    if not isinstance(raw, str):
        return None
    local, sep, domain = raw.strip().lower().partition("@")
    if not sep or not ALIAS_LOCAL_RE.match(local) or domain not in user_domains(user_id):
        return None
    return f"{local}@{domain}"
