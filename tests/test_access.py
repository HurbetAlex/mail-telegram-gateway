import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ["ADMIN_ID"] = "1"
os.environ["DOMAIN_OWNERS"] = "team.example.com:2"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app.bot import auth, handlers  # noqa: E402
from src.app.db import storage  # noqa: E402


def test_can_access():
    assert auth.can_access(1, {"user_id": 2})  # admin sees everything
    assert auth.can_access(1, {})
    assert auth.can_access(2, {"user_id": 2})
    assert not auth.can_access(2, {"user_id": 1})
    assert not auth.can_access(2, {})  # mail without an owner: admin only
    assert not auth.can_access(2, {"user_id": None})


def test_scope():
    assert auth.scope(1) is None
    assert auth.scope(2) == 2


class FakeDB:
    def __init__(self):
        self.calls = []

    async def get_history(self, query, skip=0, limit=10, user_id=None, status=None):
        self.calls.append(user_id)
        return [], 0


def run_history(uid):
    fake = FakeDB()
    old = storage.db
    storage.db = fake
    try:
        asyncio.run(handlers._history_view("", 0, auth.scope(uid)))
    finally:
        storage.db = old
    return fake.calls


def test_history_scoped_to_user():
    assert run_history(2) == [2]
    assert run_history(1) == [None]


class FakeCall:
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid)
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))


def test_admin_call_guard():
    c = FakeCall(2)
    assert asyncio.run(handlers._admin_call(c)) is False
    assert c.answers == [("No access", True)]
    assert asyncio.run(handlers._admin_call(FakeCall(1))) is True
