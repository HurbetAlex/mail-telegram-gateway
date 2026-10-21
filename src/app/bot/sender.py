import asyncio
import html
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

logger = logging.getLogger(__name__)

bot_token = os.getenv("BOT_TOKEN")
if not bot_token:
    raise ValueError("BOT_TOKEN is required")

bot = Bot(token=bot_token)

MAX_TG_ATTACHMENTS = 10
MASK = "••••••"


def _esc(s) -> str:
    return html.escape(str(s or ""))


def recipient_lines(mail_doc: Dict[str, Any], icon: bool = True) -> str:
    """"To" / "Delivered to" / "Forwarded by" lines: shared logic for the bot.

    To = the original recipient (To header) when known; the envelope rcpt after
    forwarding is shown separately. Old mails without orig_to/forwarded_by get one line.
    """
    to = mail_doc.get("to") or "?"
    orig_to = mail_doc.get("orig_to")
    forwarded_by = mail_doc.get("forwarded_by")
    shown = orig_to or to
    lines = [f"{'📨 ' if icon else ''}<b>To:</b> <code>{_esc(shown)}</code>"]
    if orig_to and orig_to.lower() != to.lower():
        lines.append(f"↪️ <b>Delivered to:</b> <code>{_esc(to)}</code>")
    if forwarded_by and forwarded_by.lower() not in (shown.lower(), to.lower()):
        lines.append(f"🔁 <b>Forwarded by:</b> <code>{_esc(forwarded_by)}</code>")
    if mail_doc.get("alias_note"):
        lines.append(f"🏷 {_esc(mail_doc['alias_note'])}")
    return "\n".join(lines)


def format_mail(mail_doc: Dict[str, Any], mask_otp: bool = False) -> str:
    """mask_otp hides the code in the header as well as in the subject/preview, where it is usually repeated."""
    otp = str(mail_doc.get("otp") or "")
    mask = bool(mask_otp and otp)

    def hide(text: str) -> str:
        return text.replace(otp, MASK) if mask else text

    parts = []
    if otp:
        shown = MASK if mask else otp
        parts.append(f"🔐 Code: <code>{_esc(shown)}</code>\n")
    parts.append(f"📧 <b>From:</b> {_esc(mail_doc.get('from', '?'))}")
    parts.append(recipient_lines(mail_doc))
    parts.append(f"📝 <b>Subject:</b> {_esc(hide(str(mail_doc.get('subject') or 'no subject')))}")
    preview = (mail_doc.get("body_preview") or "").strip()
    if preview:
        parts.append(f"\n{_esc(hide(preview))}")
    return "\n".join(parts)


def mail_keyboard(
    oid: Optional[str], mail_doc: Optional[Dict[str, Any]] = None, otp_masked: bool = False
) -> InlineKeyboardMarkup:
    mail_doc = mail_doc or {}
    rows = []
    otp = str(mail_doc.get("otp") or "")
    if oid and otp and not otp_masked:
        # CopyTextButton copies the code to the clipboard in one tap, without sending it to the chat
        rows.append(
            [InlineKeyboardButton(text=f"📋 {otp}", copy_text=CopyTextButton(text=otp))]
        )
    if oid:
        rows.append(
            [
                InlineKeyboardButton(text="↩️ Reply", callback_data=f"reply:{oid}"),
                InlineKeyboardButton(text="➡️ Forward", callback_data=f"fwd:{oid}"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="📄 Full text", callback_data=f"full:{oid}"),
                InlineKeyboardButton(text="🔕 Mute", callback_data=f"mute:{oid}"),
                InlineKeyboardButton(text="🗑", callback_data="del_msg"),
            ]
        )
        extra = [
            InlineKeyboardButton(
                text="🌟 Starred" if mail_doc.get("starred") else "⭐ Star",
                callback_data=f"star:{oid}",
            )
        ]
        if mail_doc.get("unsubscribe"):
            if mail_doc.get("unsubscribed_at"):
                extra.append(
                    InlineKeyboardButton(text="✅ Unsubscribe sent", callback_data="unsub_done")
                )
            else:
                extra.append(
                    InlineKeyboardButton(text="🚫 Unsubscribe", callback_data=f"unsub:{oid}")
                )
        rows.append(extra)
    else:
        rows.append([InlineKeyboardButton(text="🗑 Delete", callback_data="del_msg")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_telegram_message(
    user_id: int,
    mail_doc: Dict[str, Any],
    oid: Optional[str] = None,
    silent: bool = False,
) -> Optional[int]:
    """Mail notification. On an HTML markup error, retries without it.

    silent: no sound (newsletters without a code).
    """
    text = format_mail(mail_doc)
    kb = mail_keyboard(oid, mail_doc)

    for parse_mode in ("HTML", None):
        for _attempt in range(2):
            try:
                sent = await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=kb,
                    disable_notification=silent,
                )
                return sent.message_id
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.5)
            except TelegramBadRequest as e:
                logger.warning(f"send_message (parse_mode={parse_mode}): {e}")
                break  # try the next parse_mode
            except Exception as e:
                logger.error(f"Failed to send telegram message to {user_id}: {e}")
                await asyncio.sleep(1)

    return None


async def send_attachments(user_id: int, attachments: List[Tuple[str, bytes, str]]):
    for name, data, _mime in attachments[:MAX_TG_ATTACHMENTS]:
        try:
            await bot.send_document(
                chat_id=user_id,
                document=BufferedInputFile(data, filename=name),
            )
        except Exception as e:
            logger.error(f"Failed to send attachment {name} to {user_id}: {e}")
    skipped = len(attachments) - MAX_TG_ATTACHMENTS
    if skipped > 0:
        logger.warning(f"Skipped {skipped} attachments over limit for {user_id}")
