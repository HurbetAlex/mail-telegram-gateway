"""Suggestions for the From field: sent -> routes/aliases -> received -> default."""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ["ADMIN_ID"] = "1"
os.environ["DOMAIN_OWNERS"] = "team.example.com:2"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app.mail import composer  # noqa: E402


def addrs(uid, domain, *, sent=(), routes=(), aliases=(), recv=()):
    db = SimpleNamespace(
        get_sent_from_addresses=AsyncMock(return_value=list(sent)),
        get_all_routes=AsyncMock(return_value=list(routes)),
        list_aliases=AsyncMock(return_value=list(aliases)),
        get_recipient_addresses=AsyncMock(return_value=list(recv)),
    )
    composer.storage.db = db
    return asyncio.run(composer.user_from_addresses(uid, domain))


def test_empty_user_gets_default():
    assert addrs(2, "team.example.com") == ["team@team.example.com"]


def test_apex_without_anything_gets_mail():
    assert addrs(1, "example.com") == ["mail@example.com"]


def test_apex_not_added_when_not_empty():
    assert addrs(1, "example.com", recv=["a@example.com"]) == ["a@example.com"]


def test_order_and_dedup_and_foreign_domains():
    res = addrs(
        2, "team.example.com",
        sent=["Sent@team.example.com", "x@evil.com"],
        routes=[{"prefix": "shop", "user_id": 2}, {"prefix": "off", "user_id": 2, "disabled": True},
                {"prefix": "other", "user_id": 9}],
        aliases=[{"address": "al@team.example.com"}, {"address": "dead@team.example.com", "disabled": True}],
        recv=["popular@team.example.com", "shop@team.example.com", "z@example.com", "bad name@team.example.com"],
    )
    assert res == ["sent@team.example.com", "shop@team.example.com", "al@team.example.com",
                   "popular@team.example.com", "team@team.example.com"]


def test_bot_aliases_param_uses_same_list():
    from src.app.bot import compose
    composer.storage.db = SimpleNamespace(
        get_sent_from_addresses=AsyncMock(return_value=[]),
        get_all_routes=AsyncMock(return_value=[{"prefix": "shop", "user_id": 2}]),
        list_aliases=AsyncMock(return_value=[]),
        get_recipient_addresses=AsyncMock(return_value=[f"r{i}@team.example.com" for i in range(10)]),
    )
    got = asyncio.run(compose._user_aliases(2))
    assert got[0] == "shop" and "team" in got and len(got) <= 20
