"""API Mini App: star/unsubscribe/aliases/filters, permissions and validation (storage is mocked)."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

os.environ["BOT_TOKEN"] = "123:test"
os.environ["ADMIN_ID"] = "1"
os.environ["DOMAIN_OWNERS"] = "team.example.com:2"
os.environ["MAIL_TTL_DAYS"] = "180"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from bson import ObjectId  # noqa: E402

from src.app.web import api  # noqa: E402

api.BOT_TOKEN = "123:test"


def init_data(uid: int) -> str:
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", b"123:test", hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def hdr(uid: int):
    return {"X-Init-Data": init_data(uid)}


def run(coro_fn):
    """Starts the app with a mocked storage and runs the scenario."""
    async def main():
        db = SimpleNamespace(
            get_history=AsyncMock(return_value=([], 0)),
            get_mail=AsyncMock(),
            count_unread=AsyncMock(return_value=0),
            get_starred=AsyncMock(return_value=[]),
            get_expiring=AsyncMock(return_value=([], 0)),
            set_starred=AsyncMock(return_value=True),
            set_unsubscribed=AsyncMock(),
            list_aliases=AsyncMock(return_value=[]),
            create_alias=AsyncMock(),
            set_alias_disabled=AsyncMock(return_value=True),
            delete_alias=AsyncMock(return_value=True),
        )
        api.storage.db = db
        async with TestClient(TestServer(api.make_app())) as client:
            await coro_fn(client, db)
    asyncio.run(main())


def mail(owner=2, **kw):
    m = {"_id": ObjectId(), "user_id": owner, "from": "a@b.c", "to": "x@y.z",
         "subject": "s", "full_body": "body", "starred": False}
    m.update(kw)
    return m


def test_unauthorized():
    async def sc(c, db):
        r = await c.get("/api/mails")
        assert r.status == 401
    run(sc)


def test_list_fields_and_scope():
    async def sc(c, db):
        m = mail(bulk=True, alias_note="shop", starred=True, unsubscribe={"http": "https://x"})
        db.get_history.return_value = ([m], 1)
        r = await c.get("/api/mails", headers=hdr(2))
        d = await r.json()
        it = d["items"][0]
        assert it["starred"] and it["bulk"] and it["alias_note"] == "shop"
        assert it["unsubscribe"] is True and it["unsubscribed"] is False
        assert db.get_history.call_args.kwargs["user_id"] == 2
        await c.get("/api/mails", headers=hdr(1))
        assert db.get_history.call_args.kwargs["user_id"] is None
    run(sc)


def test_starred_filter_pagination():
    async def sc(c, db):
        db.get_starred.return_value = [mail() for _ in range(api.PAGE_SIZE + 1)]
        r = await c.get("/api/mails?starred=1", headers=hdr(2))
        d = await r.json()
        assert len(d["items"]) == api.PAGE_SIZE and d["has_more"] is True
        assert db.get_starred.call_args.args[0] == 2
        assert db.get_starred.call_args.kwargs["limit"] == api.PAGE_SIZE + 1
    run(sc)


def test_expiring_filter_uses_scope():
    async def sc(c, db):
        db.get_expiring.return_value = ([mail()], 5)
        r = await c.get("/api/mails?expiring=1", headers=hdr(2))
        d = await r.json()
        assert d["total"] == 5
        assert db.get_expiring.call_args.args[0] == 180
        assert db.get_expiring.call_args.kwargs["user_id"] == 2
        await c.get("/api/mails?expiring=1", headers=hdr(1))
        assert db.get_expiring.call_args.kwargs["user_id"] is None
    run(sc)


def test_expiring_disabled_when_ttl_zero():
    async def sc(c, db):
        os.environ["MAIL_TTL_DAYS"] = "0"
        try:
            r = await c.get("/api/mails?expiring=1", headers=hdr(1))
            assert (await r.json())["items"] == []
            db.get_expiring.assert_not_called()
        finally:
            os.environ["MAIL_TTL_DAYS"] = "180"
    run(sc)


def test_detail_fields_and_access():
    async def sc(c, db):
        m = mail(headers=[["Subject", "hi"], ["X", "y"]], envelope_from="bounce@b.c",
                 alias_note="n", unsubscribe={"mailto": "u@b.c"})
        db.get_mail.return_value = m
        r = await c.get(f"/api/mails/{m['_id']}", headers=hdr(2))
        d = await r.json()
        assert d["headers"][0] == ["Subject", "hi"] and d["envelope_from"] == "bounce@b.c"
        assert d["expires_at"] and d["alias_note"] == "n"
        # starred emails are not deleted
        m["starred"] = True
        d = await (await c.get(f"/api/mails/{m['_id']}", headers=hdr(2))).json()
        assert d["expires_at"] is None
        # someone else's email and an email without an owner
        m2 = mail(owner=99)
        db.get_mail.return_value = m2
        assert (await c.get(f"/api/mails/{m2['_id']}", headers=hdr(2))).status == 403
        assert (await c.get(f"/api/mails/{m2['_id']}", headers=hdr(1))).status == 200
        m3 = mail(owner=None)
        db.get_mail.return_value = m3
        assert (await c.get(f"/api/mails/{m3['_id']}", headers=hdr(2))).status == 403
        db.get_mail.return_value = None
        assert (await c.get(f"/api/mails/{ObjectId()}", headers=hdr(2))).status == 404
    run(sc)


def test_invalid_oid_400():
    async def sc(c, db):
        for path in ("", "/star", "/unsubscribe"):
            url = f"/api/mails/zzz{path}"
            r = await (c.get(url, headers=hdr(1)) if not path else c.post(url, headers=hdr(1), json={"starred": True}))
            assert r.status == 400
        assert (await c.post("/api/aliases/zzz/toggle", headers=hdr(1))).status == 400
        assert (await c.delete("/api/aliases/zzz", headers=hdr(1))).status == 400
    run(sc)


def test_star():
    async def sc(c, db):
        m = mail()
        db.get_mail.return_value = m
        url = f"/api/mails/{m['_id']}/star"
        r = await c.post(url, headers=hdr(2), json={"starred": True})
        assert r.status == 200
        db.set_starred.assert_awaited_with(str(m["_id"]), True, user_id=2)
        assert (await c.post(url, headers=hdr(2), json={"starred": "yes"})).status == 400
        assert (await c.post(url, headers=hdr(2), data="not json")).status == 400
        assert (await c.post(url, headers=hdr(2), json=[1])).status == 400
        assert (await c.post(url, headers=hdr(3), json={"starred": True})).status == 401
        db.get_mail.return_value = mail(owner=99)
        assert (await c.post(url, headers=hdr(2), json={"starred": True})).status == 403
    run(sc)


def test_unsubscribe():
    orig = api.unsubscribe_mod.unsubscribe

    async def sc(c, db):
        m = mail(unsubscribe={"http": "https://x"})
        db.get_mail.return_value = m
        url = f"/api/mails/{m['_id']}/unsubscribe"
        api.unsubscribe_mod.unsubscribe = AsyncMock(return_value=("posted", None))
        d = await (await c.post(url, headers=hdr(2))).json()
        assert d == {"status": "posted", "info": None}
        db.set_unsubscribed.assert_awaited_once()
        db.set_unsubscribed.reset_mock()
        api.unsubscribe_mod.unsubscribe = AsyncMock(return_value=("link", "https://u"))
        d = await (await c.post(url, headers=hdr(2))).json()
        assert d["status"] == "link" and d["info"] == "https://u"
        db.set_unsubscribed.assert_not_called()
        # someone else's
        db.get_mail.return_value = mail(owner=99, unsubscribe={"http": "https://x"})
        assert (await c.post(url, headers=hdr(2))).status == 403
        # no unsubscribe option
        db.get_mail.return_value = mail()
        assert (await c.post(url, headers=hdr(2))).status == 400
    try:
        run(sc)
    finally:
        api.unsubscribe_mod.unsubscribe = orig


def test_aliases():
    async def sc(c, db):
        aid = ObjectId()
        db.list_aliases.return_value = [
            {"_id": aid, "address": "shop@team.example.com", "note": "n", "disabled": False,
             "mail_count": 3, "sender_domains": ["a.com", "b.com"]}
        ]
        d = await (await c.get("/api/aliases", headers=hdr(2))).json()
        assert d["items"][0]["mail_count"] == 3 and len(d["items"][0]["sender_domains"]) == 2
        db.list_aliases.assert_awaited_with(2)

        db.create_alias.return_value = {"_id": aid, "address": "shop@team.example.com", "note": "n"}
        r = await c.post("/api/aliases", headers=hdr(2), json={"name": "shop", "note": "n"})
        assert r.status == 201
        db.create_alias.assert_awaited_with(2, "shop@team.example.com", "n")
        # invalid name / foreign domain / types
        assert (await c.post("/api/aliases", headers=hdr(2), json={"name": "!!bad"})).status == 400
        assert (await c.post("/api/aliases", headers=hdr(2), json={"name": "a@other.com"})).status == 400
        assert (await c.post("/api/aliases", headers=hdr(2), json={"name": 5})).status == 400
        assert (await c.post("/api/aliases", headers=hdr(2), json={"name": "ok", "note": 1})).status == 400
        # duplicate
        db.create_alias.side_effect = ValueError("dup")
        assert (await c.post("/api/aliases", headers=hdr(2), json={"name": "shop"})).status == 409

        # toggle
        r = await c.post(f"/api/aliases/{aid}/toggle", headers=hdr(2))
        assert (await r.json())["disabled"] is True
        db.set_alias_disabled.assert_awaited_with(str(aid), 2, True)
        assert (await c.post(f"/api/aliases/{ObjectId()}/toggle", headers=hdr(2))).status == 404
        # delete
        assert (await c.delete(f"/api/aliases/{aid}", headers=hdr(2))).status == 200
        db.delete_alias.assert_awaited_with(str(aid), 2)
        db.delete_alias.return_value = False
        assert (await c.delete(f"/api/aliases/{aid}", headers=hdr(2))).status == 404
        # admin: no scope
        await c.get("/api/aliases", headers=hdr(1))
        db.list_aliases.assert_awaited_with(None)
    run(sc)


def test_internal_error_hidden():
    async def sc(c, db):
        db.get_history.side_effect = RuntimeError("secret trace")
        r = await c.get("/api/mails", headers=hdr(1))
        assert r.status == 500
        assert "secret" not in await r.text()
    run(sc)
