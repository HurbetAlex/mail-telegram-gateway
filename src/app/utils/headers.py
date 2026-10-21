"""Parsing email headers: list for the Mini App, List-Unsubscribe, newsletter flag.

All functions are pure and never raise: a malformed header must not
break email intake.
"""
import re
from typing import Any, Dict, List, Optional

# Heavy service headers: Google signatures and tracking, they are just noise in the Mini App
_HEAVY = {
    h.lower()
    for h in (
        "DKIM-Signature",
        "X-Google-DKIM-Signature",
        "ARC-Seal",
        "ARC-Message-Signature",
        "ARC-Authentication-Results",
        "X-Gm-Message-State",
        "X-Google-Smtp-Source",
        "X-Received",
        "X-Gm-Gg",
        "X-Gm-Features",
    )
}

MAX_VALUE_LEN = 1000
MAX_TOTAL_BYTES = 32 * 1024

_BULK_PRECEDENCE = {"bulk", "list", "junk"}
_ANGLE_RE = re.compile(r"<([^<>]+)>")


def collect_headers(msg) -> List[List[str]]:
    """All headers in order as [name, value], without heavy ones; total <= 32 KB."""
    result: List[List[str]] = []
    total = 0
    try:
        items = list(msg.items())
    except Exception:
        return result
    for name, value in items:
        try:
            name = str(name)
            if name.lower() in _HEAVY:
                continue
            value = str(value)
            if len(value) > MAX_VALUE_LEN:
                value = value[:MAX_VALUE_LEN] + "…"
            total += len(name) + len(value)
            if total > MAX_TOTAL_BYTES:
                result.append(["X-Truncated", "…"])
                break
            result.append([name, value])
        except Exception:
            continue
    return result


def _get(msg, name: str) -> str:
    try:
        return str(msg.get(name) or "").strip()
    except Exception:
        return ""


def parse_list_unsubscribe(msg) -> Optional[Dict[str, Any]]:
    """{"http", "mailto", "one_click"} from List-Unsubscribe; None if there are no links."""
    raw = _get(msg, "List-Unsubscribe")
    if not raw:
        return None
    # Usually <url>, <mailto:..>; just in case we also tolerate values without brackets
    candidates = _ANGLE_RE.findall(raw) or [p.strip() for p in raw.split(",")]
    http = mailto = None
    for c in candidates:
        c = c.strip()
        low = c.lower()
        if http is None and low.startswith(("http://", "https://")):
            http = c
        elif mailto is None and low.startswith("mailto:"):
            mailto = c
    if not http and not mailto:
        return None
    post = _get(msg, "List-Unsubscribe-Post").replace(" ", "").lower()
    # One-Click per RFC 8058 is allowed only over https
    one_click = bool(
        http and http.lower().startswith("https://") and "list-unsubscribe=one-click" in post
    )
    return {"http": http, "mailto": mailto, "one_click": one_click}


def is_bulk(msg) -> bool:
    """Newsletter: has List-Unsubscribe/List-Id or Precedence bulk/list/junk."""
    if _get(msg, "List-Unsubscribe") or _get(msg, "List-Id"):
        return True
    return _get(msg, "Precedence").lower() in _BULK_PRECEDENCE


# --- Trust in headers added by our own filter -------------------------------------
# Anything in an incoming message can be forged by the sender, including X-Spam-* and
# Authentication-Results. Postfix strips such headers on input (config/postfix/header_checks)
# and rspamd adds its own; the helpers below stay safe even if one copy slips through.

def is_spam_verdict(msg: Any) -> bool:
    """True if ANY X-Spam-Status header says "Yes".

    A sender can add a fake "No" but cannot remove the "Yes" that rspamd adds, so every
    instance is checked instead of the first one. A forged "Yes" only marks the sender's
    own mail as spam.
    """
    try:
        values = msg.get_all("X-Spam-Status") or []
        return any(str(v).strip().lower().startswith("yes") for v in values)
    except Exception:
        return False
