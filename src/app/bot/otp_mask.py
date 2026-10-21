"""Masking codes in old notifications.

After the owner's otp_hide_minutes (a per-user setting, default
OTP_HIDE_MINUTES) have passed since sending, the code in the message is
replaced with ••••••  and the copy button is removed: the code does not sit in
the chat forever. It stays in Mongo (visible in /history and the Mini App).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from src.app.bot.sender import bot, format_mail, mail_keyboard
from src.app.db import storage
from src.app.utils.settings import default_settings

logger = logging.getLogger(__name__)

# Anything older than a day is left alone: otherwise the first run would try to edit hundreds of old messages
WINDOW = timedelta(hours=24)
INTERVAL = 60

# These Telegram errors mean "nothing to edit": treat the message as handled
_DONE_MARKERS = (
    "message to edit not found",
    "message is not modified",
    "message can't be edited",
    "message_id_invalid",
)


def hide_minutes() -> int:
    """Default for users without their own setting (OTP_HIDE_MINUTES)."""
    return default_settings()["otp_hide_minutes"]


async def _user_minutes(user_id, cache: Dict[Any, int]) -> int:
    """Minutes for the delivery's owner; settings are cached for one pass."""
    if user_id not in cache:
        try:
            cache[user_id] = (await storage.db.get_settings(user_id))["otp_hide_minutes"]
        except Exception:
            logger.exception("OTP mask: failed to read settings")
            cache[user_id] = hide_minutes()
    return cache[user_id]


def is_done_error(e: Exception) -> bool:
    text = str(e).lower()
    return any(m in text for m in _DONE_MARKERS)


async def _mask_delivery(delivery: Dict[str, Any], mail: Dict[str, Any], oid: str) -> bool:
    """True: delivery handled (edited, or nothing to edit)."""
    chat_id, mid = delivery["user_id"], delivery["tg_message_id"]
    text = format_mail(mail, mask_otp=True)
    kb = mail_keyboard(oid, mail, otp_masked=True)
    for parse_mode in ("HTML", None):
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=mid, text=text, parse_mode=parse_mode, reply_markup=kb
            )
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
            return False
        except TelegramForbiddenError as e:
            logger.info(f"OTP mask {oid}: chat {chat_id} unavailable ({e})")
            return True
        except TelegramBadRequest as e:
            if is_done_error(e):
                logger.info(f"OTP mask {oid}: {e}")
                return True
            logger.warning(f"OTP mask {oid} (parse_mode={parse_mode}): {e}")
        except Exception as e:
            logger.error(f"OTP mask {oid}: {e}")
            return False
    return False


async def mask_once(now: datetime = None) -> int:
    """One pass; returns the number of masked mails.

    The main query is by mask_at (set on delivery: time + owner's minutes), so
    mails that are not yet due or have masking disabled do not occupy the window.
    Mails without mask_at (old ones) are handled for compatibility within 24 h by timestamp.
    """
    now = now or datetime.now(timezone.utc)
    cache: Dict[Any, int] = {}
    due = await storage.db.get_mails_due_for_mask(now, limit=200)
    legacy = await storage.db.get_mails_for_otp_mask(
        older_than=now, newer_than=now - WINDOW, limit=200
    )
    done = 0
    for mail in due + legacy:
        oid = str(mail["_id"])
        minutes = await _user_minutes(mail.get("user_id"), cache)
        if minutes <= 0:
            # Owner disabled masking: mark it so the mail does not keep showing up in the query
            await storage.db.set_otp_masked(oid)
            continue
        if "mask_at" not in mail:
            ts = mail["timestamp"]
            if ts.tzinfo is None:  # pymongo returns naive UTC
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > now - timedelta(minutes=minutes):
                continue
        deliveries = await storage.db.get_deliveries(oid)
        results = [await _mask_delivery(d, mail, oid) for d in deliveries]
        # No deliveries (or all handled): mark it, otherwise we would loop on this mail forever;
        # on a transient error it stays unmarked and is retried on the next pass
        if all(results):
            await storage.db.set_otp_masked(oid)
            done += 1
    return done


async def otp_mask_loop():
    # The loop always runs: individual users' minutes may differ from the default
    while True:
        try:
            await mask_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OTP mask pass failed")
        await asyncio.sleep(INTERVAL)
