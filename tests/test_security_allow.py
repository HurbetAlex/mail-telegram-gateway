"""Regression tests: the allow rule needs an authenticated From."""
import asyncio
import email
from email.policy import default
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.app.bot import auth
from src.app.mail import lmtp
from src.app.utils.headers import is_spam_verdict, sender_authenticated

AUTHSERV = "mail.example.com"


@pytest.fixture(autouse=True)
def users(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_ID", 1)
    monkeypatch.setattr(auth, "MAIN_DOMAIN", "example.com")
    monkeypatch.setattr(auth, "DOMAIN_OWNERS", {"team.example.com": 2})
    monkeypatch.setattr(lmtp, "AUTHSERV_ID", AUTHSERV)


def mk(headers: str):
    raw = (headers.strip() + "\nSubject: hi\n\nbody\n").encode()
    return email.message_from_bytes(raw, policy=default)


# --- 3. allow rule needs authenticated From ---

def test_sender_authenticated_requires_our_authserv_id():
    ar = "Authentication-Results: {}; dmarc=pass header.from=bank.com; dkim=pass header.d=bank.com"
    assert sender_authenticated(mk(ar.format(AUTHSERV)), "a@bank.com", AUTHSERV)
    assert not sender_authenticated(mk(ar.format("evil.example")), "a@bank.com", AUTHSERV)


def test_sender_authenticated_needs_alignment_and_pass():
    other = "Authentication-Results: mail.example.com; dkim=pass header.d=attacker.net"
    assert not sender_authenticated(mk(other), "ceo@bank.com", AUTHSERV)
    failed = "Authentication-Results: mail.example.com; dkim=fail header.d=bank.com; spf=pass"
    assert not sender_authenticated(mk(failed), "ceo@bank.com", AUTHSERV)
    sub = "Authentication-Results: mail.example.com; dkim=pass header.d=bank.com"
    assert sender_authenticated(mk(sub), "ceo@mail.bank.com", AUTHSERV)


def run_handler(monkeypatch, raw: bytes, rule="allow"):
    db = SimpleNamespace(
        get_domain_route=AsyncMock(return_value=None),
        match_rule=AsyncMock(return_value=rule),
        get_alias=AsyncMock(return_value=None),
        save_mail=AsyncMock(return_value="oid1"),
        get_settings=AsyncMock(return_value={}),
        add_delivery=AsyncMock(),
        set_mail_status=AsyncMock(),
        set_mail_blobs=AsyncMock(),
    )
    monkeypatch.setattr(lmtp.storage, "db", db, raising=False)
    monkeypatch.setattr(lmtp, "send_telegram_message", AsyncMock(return_value=10))
    monkeypatch.setattr(lmtp, "send_attachments", AsyncMock())
    env = SimpleNamespace(mail_from="a@bank.com", rcpt_tos=["me@team.example.com"], content=raw)
    res = asyncio.run(lmtp.Handler().handle_DATA(None, None, env))
    assert res == "250 OK"
    return db


BASE = b"From: Bank <ceo@bank.com>\r\nTo: me@team.example.com\r\nSubject: hello\r\nMessage-ID: <m1@bank.com>\r\n"


def test_forged_from_in_allow_list_stays_spam(monkeypatch):
    raw = BASE + b"X-Spam-Status: No\r\nX-Spam-Status: Yes, score=14\r\n\r\nbuy now\r\n"
    db = run_handler(monkeypatch, raw)
    docs = [c.args[0] for c in db.save_mail.await_args_list]
    assert docs and docs[0]["status"] == "spam"
    

def test_allow_rule_works_with_authenticated_sender(monkeypatch):
    raw = (
        BASE
        + b"X-Spam-Status: Yes, score=6\r\n"
        + b"Authentication-Results: mail.example.com; dmarc=pass header.from=bank.com\r\n\r\nhi\r\n"
    )
    db = run_handler(monkeypatch, raw)
    db.save_mail.assert_awaited_once()
    assert db.save_mail.await_args.args[0]["status"] == "delivered"
