import asyncio
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from aiosmtpd.lmtp import LMTP

from src.app.bot.auth import owner_of
from src.app.bot.sender import send_attachments, send_telegram_message
from src.app.db import storage
from src.app.mail import blobstore
from src.app.utils.headers import (
    collect_headers,
    is_bulk,
    is_spam_verdict,
    parse_list_unsubscribe,
    sender_authenticated,
)
from src.app.utils.parser import (
    clean_local_part,
    extract_html,
    extract_otp,
    match_route,
    original_recipient,
    parse_email,
)
from src.app.utils.settings import default_settings, is_silent

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
# authserv-id our rspamd puts into Authentication-Results (milter_headers.conf: hostname)
AUTHSERV_ID = os.getenv("AUTHSERV_ID") or f"mail.{os.getenv('MAIL_DOMAIN', 'example.com')}"


class Handler:
    @staticmethod
    async def _find_alias(rcpt: str):
        """Alias by recipient address (lowercase, without +tag); a DB failure must not lose the email."""
        local, _, domain = rcpt.lower().partition("@")
        address = f"{local.split('+')[0]}@{domain}"
        try:
            return await storage.db.get_alias(address)
        except Exception:
            logger.exception("Alias lookup failed")
            return None

    @staticmethod
    async def _store_blobs(oid: str, user_id, html_text, attachments) -> None:
        """HTML and attachments -> blob store, then links in the email. A failure does not lose the email."""
        try:
            html_id = None
            if html_text:
                data = html_text.encode("utf-8")
                try:
                    html_id = await blobstore.save_blob(
                        data, mail_id=oid, user_id=user_id, kind="html",
                        name="mail.html", mime="text/html",
                    )
                except blobstore.BlobRejected as e:
                    logger.info(f"HTML {oid} not saved: {e}")
            saved = []
            for name, data, mime in attachments:
                if len(data) > blobstore.attach_max_bytes():
                    logger.info(f"Attachment {name} ({len(data)} bytes) exceeds the limit, Telegram only")
                    continue
                try:
                    bid = await blobstore.save_blob(
                        data, mail_id=oid, user_id=user_id, kind="att", name=name, mime=mime
                    )
                except blobstore.BlobRejected as e:
                    logger.info(f"Attachment {name} not saved: {e}")
                    continue
                except Exception:
                    logger.exception(f"Failed to save attachment {name}")
                    continue
                saved.append({"id": bid, "name": name, "mime": mime, "size": len(data)})
            if html_id or saved:
                await storage.db.set_mail_blobs(oid, html_id, saved)
        except Exception:
            logger.exception(f"Blob store failed for mail {oid}")

    @staticmethod
    async def _user_settings(user_id) -> dict:
        try:
            return await storage.db.get_settings(user_id)
        except Exception:
            logger.exception("Settings lookup failed")
            return default_settings()

    async def handle_DATA(self, server, session, envelope):
        mail_from = envelope.mail_from
        rcpt_tos = envelope.rcpt_tos
        logger.info(f"Receiving mail from {mail_from} to {rcpt_tos}")

        try:
            msg, body, attachments = parse_email(envelope.content)
            # Headers are added by rspamd (local.d/milter_headers.conf); forged copies from the
            # sender are stripped by Postfix, and every instance is checked anyway
            is_spam = is_spam_verdict(msg)
            subject = str(msg.get("Subject") or "")
            otp = extract_otp(body, subject=subject)
            bulk = is_bulk(msg)
            unsubscribe = parse_list_unsubscribe(msg)
            headers = collect_headers(msg)
            html_text = extract_html(msg)

            # Auto-rules by sender are evaluated for each recipient (personal
            # ones beat global ones): allow (whitelist, saves forwarding with a
            # broken SPF) or mute (no notifications)
            sender_addr = parseaddr(str(msg.get("From") or mail_from))[1]
            # "allow" is only honoured when From is confirmed by DKIM/DMARC results of our own
            # filter: the From header alone is trivially forged
            from_authenticated = sender_authenticated(msg, sender_addr, AUTHSERV_ID)
            # Server-side dedup key: hash of the raw message + recipient (Message-ID is attacker-chosen)
            raw_hash = hashlib.sha256(envelope.content).hexdigest()

            for rcpt in rcpt_tos:
                domain = rcpt.split("@")[-1].lower()
                local = clean_local_part(rcpt)

                # Route: domain from the DB -> domain owner from env -> prefix -> catch-all to admin
                user_id = await storage.db.get_domain_route(domain)
                if not user_id:
                    user_id = owner_of(domain)
                if not user_id:
                    user_id = await match_route(storage.db, local)
                if not user_id and ADMIN_ID:
                    user_id = ADMIN_ID
                    logger.info(f"Using catch-all routing for {rcpt}")

                mail_doc = {
                    "message_id": msg.get("Message-ID"),
                    "dedup_key": f"{raw_hash}:{rcpt.lower()}",
                    "from": str(msg.get("From") or mail_from),
                    "to": rcpt,
                    "subject": subject,
                    "body_preview": body[:200],
                    "full_body": body,
                    "otp": otp,
                    "envelope_from": mail_from,
                    "headers": headers,
                    "bulk": bulk,
                    "read": False,
                    "timestamp": datetime.now(timezone.utc),
                }
                if unsubscribe:
                    mail_doc["unsubscribe"] = unsubscribe
                # Real addressee (To) and the forwarding mailbox; the envelope rcpt after forwarding is not it
                orig_to, forwarded_by = original_recipient(msg, mail_from, rcpt)
                if orig_to:
                    mail_doc["orig_to"] = orig_to
                if forwarded_by:
                    mail_doc["forwarded_by"] = forwarded_by
                if user_id:
                    # Email owner, also for spam/muted (visibility in the Mini App)
                    mail_doc["user_id"] = user_id

                try:
                    rule_action = await storage.db.match_rule(sender_addr, user_id or None)
                except Exception:
                    logger.exception("Rule lookup failed")
                    rule_action = None
                if rule_action == "allow" and not from_authenticated:
                    logger.info(f"Allow rule for {sender_addr} ignored: sender not authenticated")
                    rule_action = None

                # Alias ("address per service"): when disabled, the email is stored without a notification
                alias = await self._find_alias(rcpt)
                if alias:
                    mail_doc["alias"] = alias["address"]
                    mail_doc["alias_note"] = alias.get("note") or alias["address"]
                    if alias.get("disabled"):
                        mail_doc["status"] = "blocked"
                        await storage.db.save_mail(mail_doc)
                        logger.info(f"Blocked by disabled alias {alias['address']}")
                        continue

                if is_spam and rule_action != "allow":
                    # Do not delete silently: keep it for /history in case of a false positive
                    mail_doc["status"] = "spam"
                    await storage.db.save_mail(mail_doc)
                    logger.info(f"Spam stored: {mail_doc['message_id']}")
                    continue

                if rule_action == "mute":
                    mail_doc["status"] = "muted"
                    await storage.db.save_mail(mail_doc)
                    logger.info(f"Muted sender {sender_addr}: {mail_doc['message_id']}")
                    continue

                if not user_id:
                    mail_doc["status"] = "unassigned"
                    await storage.db.save_mail(mail_doc)
                    logger.info(f"Unassigned mail to {rcpt}")
                    continue

                mail_doc["status"] = "delivered"
                user_cfg = await self._user_settings(user_id)
                minutes = user_cfg.get("otp_hide_minutes") or 0
                if otp and minutes > 0:
                    # When to hide the code: the masking query goes by mask_at (no starvation)
                    mail_doc["mask_at"] = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                oid, created = await storage.db.save_mail_once(mail_doc)
                if not created:
                    logger.info(f"Duplicate delivery ignored: {oid}")
                    continue
                await self._store_blobs(oid, user_id, html_text, attachments)

                # Silent: newsletter without a code or quiet hours without a code (a code always makes a sound)
                silent = is_silent(bulk, otp, user_cfg)
                tg_msg_id = await send_telegram_message(user_id, mail_doc, oid=oid, silent=silent)
                if tg_msg_id:
                    await storage.db.add_delivery(oid, user_id, tg_msg_id)
                    if attachments:
                        await send_attachments(user_id, attachments)
                else:
                    # Email is in the DB but the notification did not go out: visible in /history
                    await storage.db.set_mail_status(oid, "failed")

            return "250 OK"
        except Exception:
            logger.exception("Error handling mail")
            return "451 Internal Server Error"


class LMTPServer:
    def __init__(self, port=8024):
        self.handler = Handler()
        self.port = port
        self.server = None

    async def start(self):
        loop = asyncio.get_running_loop()

        def factory():
            return LMTP(self.handler, enable_SMTPUTF8=True)

        self.server = await loop.create_server(factory, "0.0.0.0", self.port)
        logger.info(f"LMTP Server started on port {self.port}")

    def stop(self):
        if self.server:
            self.server.close()
