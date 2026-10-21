"""User settings (user_settings): defaults, validation, quiet hours, digest.

Everything is pure functions with no DB access, so they can be tested and
reused from the bot, LMTP, background loops and the Mini App API.
"""
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

SETTING_KEYS = (
    "quiet_from",
    "quiet_to",
    "tz",
    "digest_enabled",
    "digest_time",
    "otp_hide_minutes",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def default_settings() -> Dict[str, Any]:
    """Defaults from env (read on every call, convenient for tests)."""
    digest_time = os.getenv("DIGEST_TIME", "09:00")
    return {
        "quiet_from": None,
        "quiet_to": None,
        "tz": os.getenv("DIGEST_TZ", "Europe/Kyiv"),
        # empty DIGEST_TIME: the digest is disabled globally, by default and for users
        "digest_enabled": bool(digest_time),
        "digest_time": digest_time if _HHMM_RE.match(digest_time or "") else "09:00",
        "otp_hide_minutes": max(0, min(1440, _env_int("OTP_HIDE_MINUTES", 15))),
    }


def parse_hhmm(value: str) -> int:
    """'HH:MM' -> minutes from midnight. ValueError if the format is wrong."""
    m = _HHMM_RE.match(value or "")
    if not m:
        raise ValueError(f"Time must be in HH:MM format, got {value!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def validate_settings_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validates and normalizes a partial update; ValueError on any error."""
    if not isinstance(patch, dict):
        raise ValueError("Settings must be an object")
    unknown = set(patch) - set(SETTING_KEYS)
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(map(str, unknown)))}")
    out: Dict[str, Any] = {}
    for key, value in patch.items():
        if key in ("quiet_from", "quiet_to"):
            if value in (None, ""):
                out[key] = None
            else:
                parse_hhmm(value)
                out[key] = value
        elif key == "digest_time":
            parse_hhmm(value)
            out[key] = value
        elif key == "tz":
            if not isinstance(value, str) or not value:
                raise ValueError("Timezone is not specified")
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                raise ValueError(f"Unknown timezone: {value}")
            out[key] = value
        elif key == "digest_enabled":
            if not isinstance(value, bool):
                raise ValueError("digest_enabled must be true/false")
            out[key] = value
        elif key == "otp_hide_minutes":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1440:
                raise ValueError("otp_hide_minutes: integer from 0 to 1440")
            out[key] = value
    return out


def merge_settings(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Defaults + saved values (known keys only)."""
    res = default_settings()
    for key in SETTING_KEYS:
        if doc and key in doc:
            res[key] = doc[key]
    return res


def _local_minutes(settings: Dict[str, Any], now_utc: datetime) -> int:
    try:
        tz = ZoneInfo(settings.get("tz") or "UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        tz = timezone.utc
    local = now_utc.astimezone(tz)
    return local.hour * 60 + local.minute


def in_quiet_hours(settings: Dict[str, Any], now_utc: Optional[datetime] = None) -> bool:
    """Are the user's quiet hours active now? Interval [from, to), may span midnight."""
    q_from, q_to = settings.get("quiet_from"), settings.get("quiet_to")
    if not q_from or not q_to:
        return False
    try:
        start, end = parse_hhmm(q_from), parse_hhmm(q_to)
    except ValueError:
        return False
    if start == end:
        return False
    now_utc = now_utc or datetime.now(timezone.utc)
    cur = _local_minutes(settings, now_utc)
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def is_silent(bulk: bool, otp: Optional[str], settings: Dict[str, Any],
              now_utc: Optional[datetime] = None) -> bool:
    """Silent notification: newsletter without a code OR quiet hours without a code.
    Emails with a code always make a sound."""
    if otp:
        return False
    return bool(bulk) or in_quiet_hours(settings, now_utc)


def digest_due(settings: Dict[str, Any], now_utc: Optional[datetime] = None,
               window_minutes: int = 1) -> bool:
    """Time to send the digest: enabled, and local time within [digest_time, +window_minutes)."""
    if not settings.get("digest_enabled"):
        return False
    try:
        target = parse_hhmm(settings.get("digest_time") or "")
    except ValueError:
        return False
    now_utc = now_utc or datetime.now(timezone.utc)
    cur = _local_minutes(settings, now_utc)
    return (cur - target) % 1440 < max(1, window_minutes)
