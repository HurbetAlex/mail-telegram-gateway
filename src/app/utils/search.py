"""Parsing a search string with operators (from:, to:, has:code, after: ...).

A pure function: query -> structure of conditions; storage.build_history_filter turns it
into a Mongo filter. Regexes are built only via re.escape (there as well).
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

# operator:value (value quoted or without spaces) or just a word/quoted phrase
_TOKEN_RE = re.compile(r'(?:(?P<op>[A-Za-z]+):)?(?:"(?P<q>[^"]*)"|(?P<w>\S+))')

_VALUE_OPS = ("from", "to", "alias")
_FLAGS = {
    ("has", "code"): "has_code",
    ("has", "attachment"): "has_attachment",
    ("is", "unread"): "is_unread",
    ("is", "starred"): "is_starred",
    ("is", "bulk"): "is_bulk",
}


def _parse_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_search(query: str) -> Dict[str, Any]:
    """Returns {"from": [..], "to": [..], "alias": [..], "has_code": bool,
    "has_attachment": bool, "is_unread": bool, "is_starred": bool, "is_bulk": bool,
    "after": datetime|None, "before": datetime|None, "text": str}.

    Unknown operators and invalid dates do not break the query: the former stay
    as free text, the latter are ignored.
    """
    res: Dict[str, Any] = {
        "from": [], "to": [], "alias": [],
        "has_code": False, "has_attachment": False,
        "is_unread": False, "is_starred": False, "is_bulk": False,
        "after": None, "before": None, "text": "",
    }
    free: List[str] = []
    for m in _TOKEN_RE.finditer(query or ""):
        op = (m.group("op") or "").lower()
        quoted, word = m.group("q"), m.group("w")
        value = quoted if quoted is not None else word
        if op in _VALUE_OPS and value.strip():
            res[op].append(value.strip())
        elif op in ("has", "is") and (op, (value or "").lower()) in _FLAGS:
            res[_FLAGS[(op, value.lower())]] = True
        elif op in ("after", "before"):
            dt = _parse_date(value.strip())
            if dt is not None:
                res[op] = dt
        elif op in _VALUE_OPS or op in ("has", "is", "after", "before"):
            continue  # operator with an empty/unknown value: ignore
        else:
            # plain text; an "unknown:operator" is returned as it was
            raw = m.group(0)
            free.append(quoted if (not op and quoted is not None) else raw)
    res["text"] = " ".join(t for t in free if t).strip()
    return res
