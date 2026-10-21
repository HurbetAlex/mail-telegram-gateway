"""Regression tests: a stored mail's recipient must not become an arbitrary From address."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.app.bot import auth
from src.app.mail import composer


@pytest.fixture(autouse=True)
def users(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_ID", 1)
    monkeypatch.setattr(auth, "MAIN_DOMAIN", "example.com")
    monkeypatch.setattr(auth, "DOMAIN_OWNERS", {"team.example.com": 2})


# --- 1. sender spoofing through reply/forward ---

def test_reply_state_ignores_foreign_to():
    st = composer.reply_state({"from": "x@y.com", "to": "ceo@other.example"}, 2)
    assert st["from_addr"] == "team@team.example.com"


def test_reply_state_keeps_own_address():
    st = composer.reply_state({"from": "x@y.com", "to": "Shop@team.example.com"}, 2)
    assert st["from_addr"] == "shop@team.example.com"


def test_fwd_state_ignores_other_users_domain():
    # user 2 must not send from the admin's apex domain
    st = composer.fwd_state({"to": "boss@example.com"}, 2)
    assert st["from_addr"] == "team@team.example.com"


def test_send_and_record_rejects_foreign_from(monkeypatch):
    send = AsyncMock(return_value="<id@x>")
    monkeypatch.setattr(composer, "send_email", send)
    data = {"from_addr": "ceo@other.example", "to": ["a@b.com"], "subject": "s", "mode": "new", "text": "t"}
    with pytest.raises(composer.InvalidSender):
        asyncio.run(composer.send_and_record(2, data))
    send.assert_not_awaited()
