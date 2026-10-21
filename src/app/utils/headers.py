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


def _aligned(from_domain: str, auth_domain: str) -> bool:
    a, b = from_domain.lower().strip("."), auth_domain.lower().strip("<>. ")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def sender_authenticated(msg: Any, from_addr: str, authserv_id: str) -> bool:
    """True if our own Authentication-Results (authserv-id match) confirms the From domain.

    Requires dmarc=pass, or dkim=pass whose header.d is aligned with the From domain.
    SPF alone is not enough (it validates the envelope sender, not From).
    """
    from_domain = (from_addr or "").rpartition("@")[2].strip().lower()
    if not from_domain or not authserv_id:
        return False
    try:
        values = msg.get_all("Authentication-Results") or []
        for raw in values:
            parts = [p.strip() for p in str(raw).replace("\n", " ").replace("\r", " ").split(";")]
            if not parts or parts[0].split(" ")[0].lower() != authserv_id.lower():
                continue
            for part in parts[1:]:
                tokens = part.split()
                if not tokens:
                    continue
                method = tokens[0].lower()
                props = {}
                for tok in tokens[1:]:
                    k, sep, v = tok.partition("=")
                    if sep:
                        props[k.lower()] = v
                if method == "dmarc=pass":
                    hf = props.get("header.from")
                    if hf is None or _aligned(from_domain, hf):
                        return True
                if method == "dkim=pass" and _aligned(from_domain, props.get("header.d", "")):
                    return True
    except Exception:
        return False
    return False
