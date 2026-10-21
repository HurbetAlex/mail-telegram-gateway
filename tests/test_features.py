import asyncio
import email
import os
import socket
import sys
from email.policy import default
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ["ADMIN_ID"] = "1"
os.environ["DOMAIN_OWNERS"] = "team.example.com:2"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from src.app.bot import auth  # noqa: E402
from src.app.bot.sender import format_mail, mail_keyboard, recipient_lines  # noqa: E402
from src.app.db.storage import build_whereis_pipeline  # noqa: E402
from src.app.mail import unsubscribe as unsub  # noqa: E402
from src.app.utils.headers import collect_headers, is_bulk, parse_list_unsubscribe  # noqa: E402


def mk(headers: str):
    raw = (headers.strip() + "\nSubject: hi\n\nbody\n").encode()
    return email.message_from_bytes(raw, policy=default)


# --- headers ---

def test_collect_headers_filters_heavy():
    msg = mk("From: a@b.c\nDKIM-Signature: v=1\nx-received: foo\nX-Gm-Gg: 1\nTo: x@y.z")
    names = [h[0] for h in collect_headers(msg)]
    assert names == ["From", "To", "Subject"]


def test_collect_headers_truncates_value():
    msg = mk("X-Big: " + "a" * 3000)
    name, value = collect_headers(msg)[0]
    assert len(value) == 1001 and value.endswith("…")


def test_collect_headers_total_limit():
    lines = "\n".join(f"X-H{i}: {'a' * 900}" for i in range(60))
    res = collect_headers(mk(lines))
    assert res[-1] == ["X-Truncated", "…"]
    assert sum(len(n) + len(v) for n, v in res[:-1]) <= 32 * 1024


def test_collect_headers_never_raises():
    assert collect_headers(None) == []


def test_unsubscribe_both():
    msg = mk(
        "List-Unsubscribe: <https://x.com/u?id=1>, <mailto:unsub@x.com?subject=bye>\n"
        "List-Unsubscribe-Post: List-Unsubscribe=One-Click"
    )
    assert parse_list_unsubscribe(msg) == {
        "http": "https://x.com/u?id=1",
        "mailto": "mailto:unsub@x.com?subject=bye",
        "one_click": True,
    }


def test_unsubscribe_one_click_needs_https():
    msg = mk(
        "List-Unsubscribe: <http://x.com/u>\nList-Unsubscribe-Post: List-Unsubscribe=One-Click"
    )
    assert parse_list_unsubscribe(msg)["one_click"] is False


def test_unsubscribe_no_post_header():
    msg = mk("List-Unsubscribe: <https://x.com/u>")
    assert parse_list_unsubscribe(msg) == {"http": "https://x.com/u", "mailto": None, "one_click": False}


def test_unsubscribe_mailto_only_and_none():
    msg = mk("List-Unsubscribe: <mailto:a@b.c>")
    assert parse_list_unsubscribe(msg) == {"http": None, "mailto": "mailto:a@b.c", "one_click": False}
    assert parse_list_unsubscribe(mk("From: a@b.c")) is None
    assert parse_list_unsubscribe(mk("List-Unsubscribe: <ftp://x>")) is None


def test_is_bulk():
    assert is_bulk(mk("List-Unsubscribe: <https://x>"))
    assert is_bulk(mk("List-Id: <l.x.com>"))
    assert is_bulk(mk("Precedence: Bulk"))
    assert is_bulk(mk("Precedence: junk"))
    assert not is_bulk(mk("Precedence: first-class"))
    assert not is_bulk(mk("From: a@b.c"))


# --- unsubscribe: SSRF ---

def patch_dns(monkeypatch, ip):
    loop_cls = type(asyncio.new_event_loop())

    async def _gai(self, host, port, **kw):
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(loop_cls, "getaddrinfo", _gai)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "192.168.0.1", "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1"])
def test_ssrf_blocks_private(monkeypatch, ip):
    patch_dns(monkeypatch, ip)
    with pytest.raises(unsub.UnsafeURL):
        asyncio.run(unsub.check_url("https://evil.example/u"))


def test_ssrf_allows_public(monkeypatch):
    patch_dns(monkeypatch, "93.184.216.34")
    asyncio.run(unsub.check_url("https://good.example/u"))


@pytest.mark.parametrize("url", ["http://good.example/u", "https://good.example:8443/u", "ftp://x/u", "https:///x"])
def test_ssrf_scheme_and_port(monkeypatch, url):
    patch_dns(monkeypatch, "93.184.216.34")
    with pytest.raises(unsub.UnsafeURL):
        asyncio.run(unsub.check_url(url))


def test_ssrf_literal_ip(monkeypatch):
    # without DNS substitution: a literal resolves to itself
    with pytest.raises(unsub.UnsafeURL):
        asyncio.run(unsub.check_url("https://127.0.0.1/u"))


# --- unsubscribe: order of methods ---

def run_unsub(monkeypatch, doc, post_ok=True):
    calls = []

    async def fake_post(url):
        calls.append(("post", url))
        if not post_ok:
            raise RuntimeError("HTTP 500")

    monkeypatch.setattr(unsub, "_post_one_click", fake_post)
    return asyncio.run(unsub.unsubscribe(doc)), calls


def ud(http=None, mailto=None, one_click=False):
    return {"to": "me@example.com", "unsubscribe": {"http": http, "mailto": mailto, "one_click": one_click}}


def test_unsub_posted_beats_link_and_mailto(monkeypatch):
    res, calls = run_unsub(monkeypatch, ud("https://x/u", "mailto:u@x.com", True))
    assert res == ("posted", None) and calls == [("post", "https://x/u")]


def test_unsub_post_fails_falls_to_link(monkeypatch):
    assert run_unsub(monkeypatch, ud("https://x/u", "mailto:u@x.com", True), post_ok=False)[0] == ("link", "https://x/u")


def test_unsub_link_beats_manual(monkeypatch):
    assert run_unsub(monkeypatch, ud("http://l/u", "mailto:u@x.com"))[0] == ("link", "http://l/u")


def test_unsub_manual_never_sends(monkeypatch):
    assert not hasattr(unsub, "send_email")
    res, calls = run_unsub(monkeypatch, ud(mailto="mailto:u@x.com?subject=a&body=EVIL"))
    assert res == ("manual", "u@x.com") and calls == []


def test_unsub_none(monkeypatch):
    assert run_unsub(monkeypatch, {"to": "a@b"})[0] == ("none", None)
    assert run_unsub(monkeypatch, ud(mailto="mailto:a@b.com,c@d.com"))[0] == ("none", None)


def test_unsub_failed_only_without_fallback(monkeypatch):
    # POST failed, link exists (one_click => https) -> link; failed is impossible with http
    doc = {"to": "a@b", "unsubscribe": {"http": None, "mailto": None, "one_click": True}}
    assert run_unsub(monkeypatch, doc)[0] == ("none", None)


def test_parse_mailto_address_valid():
    assert unsub.parse_mailto_address("mailto:u@x.com") == "u@x.com"
    assert unsub.parse_mailto_address("mailto:un%2Bsub@x.co.uk?subject=1") == "un+sub@x.co.uk"


@pytest.mark.parametrize("raw", [
    "mailto:a@b.com,c@d.com", "mailto:a@b.com;c@d.com", "mailto:<a@b.com>", "mailto:a@b.com%20c@d.com",
    "mailto:a b@c.com", "mailto:a@b@c.com", "mailto:a@b", "mailto:", "mailto:a@b.com%0Abcc:x@y.com",
    "mailto:%22a%22@b.com", "mailto:a@b.c",
])
def test_parse_mailto_address_invalid(raw):
    assert unsub.parse_mailto_address(raw) is None


# --- keyboard / formatting ---

def flat(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_keyboard_star_toggle():
    off = flat(mail_keyboard("abc", {}))
    on = flat(mail_keyboard("abc", {"starred": True}))
    assert any(b.text == "⭐ Star" and b.callback_data == "star:abc" for b in off)
    assert any(b.text == "🌟 Starred" and b.callback_data == "star:abc" for b in on)


def test_keyboard_unsubscribe_button():
    assert not any("Unsubscribe" in b.text for b in flat(mail_keyboard("abc", {})))
    doc = {"unsubscribe": {"http": "https://x"}}
    assert any(b.callback_data == "unsub:abc" for b in flat(mail_keyboard("abc", doc)))
    doc["unsubscribed_at"] = 1
    texts = [b.text for b in flat(mail_keyboard("abc", doc))]
    assert "✅ Unsubscribe sent" in texts and "🚫 Unsubscribe" not in texts


def test_keyboard_copy_button():
    doc = {"otp": "123456"}
    btn = flat(mail_keyboard("abc", doc))[0]
    assert btn.text == "📋 123456" and btn.copy_text.text == "123456"
    assert not any(b.copy_text for b in flat(mail_keyboard("abc", doc, otp_masked=True)))
    assert not any(b.copy_text for b in flat(mail_keyboard("abc", {})))


def test_format_mail_mask():
    doc = {"otp": "482913", "from": "a@b.c", "to": "x@y.z", "subject": "Code 482913", "body_preview": "Your code is 482913."}
    assert "482913" in format_mail(doc)
    masked = format_mail(doc, mask_otp=True)
    assert "482913" not in masked and "••••••" in masked


def test_recipient_lines_alias_note():
    assert "🏷 Netflix" in recipient_lines({"to": "n@x.z", "alias_note": "Netflix"})
    assert "🏷" not in recipient_lines({"to": "n@x.z"})


# --- aliases ---

def test_alias_validation():
    ok = auth.validate_alias_address
    assert ok(1, "Netflix") == (f"netflix@{auth.MAIN_DOMAIN.lower()}", None)
    assert ok(2, "shop") == ("shop@team.example.com", None)
    assert ok(2, "shop@team.example.com") == ("shop@team.example.com", None)
    assert ok(2, "shop@example.com")[0] is None  # someone else's domain
    assert ok(1, "a.b-c_1")[1] is None
    # "а" below is a Cyrillic letter: non-Latin names must be rejected
    for bad in ["", "-x", ".x", "a b", "a+tag", "а", "x" * 65, "a@"]:
        assert ok(1, bad)[0] is None, bad


# --- whereis ---

def test_whereis_pipeline():
    p = build_whereis_pipeline("pro.host+", None)
    assert p[0]["$match"] == {"from": {"$regex": r"pro\.host\+", "$options": "i"}}
    grp = p[1]["$group"]["_id"]
    assert grp == {"to": {"$ifNull": ["$orig_to", "$to"]}, "via": "$forwarded_by"}
    scoped = build_whereis_pipeline("x", 42)
    assert scoped[0]["$match"]["user_id"] == 42
