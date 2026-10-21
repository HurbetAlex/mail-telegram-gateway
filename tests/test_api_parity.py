"""API Mini App: parity with the bot (sending, templates, routes, rules, stats, whereis)."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
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

ADMIN, USER = 1, 2
OID = "64b000000000000000000001"
TID = "64b000000000000000000002"


def hdr(uid: int):
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", b"123:test", hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Init-Data": urlencode(pairs)}


def mail(user_id=USER, **kw):
    m = {
        "_id": ObjectId(OID), "user_id": user_id, "from": "Bob <bob@ext.com>",
        "to": "shop@team.example.com", "subject": "Hello", "full_body": "line1\nline2",
        "message_id": "<orig@ext.com>", "status": "delivered",
    }
    m.update(kw)
    return m


def run(coro_fn):
    async def main():
        api._send_log.clear()
        db = SimpleNamespace(
            get_mail=AsyncMock(return_value=mail()),
            get_all_routes=AsyncMock(return_value=[
                {"prefix": "shop", "user_id": USER},
                {"prefix": "off", "user_id": USER, "disabled": True},
                {"prefix": "adm", "user_id": ADMIN},
                {"domain": "x.top", "user_id": USER},
            ]),
            list_aliases=AsyncMock(return_value=[
                {"address": "al@team.example.com", "user_id": USER},
                {"address": "dead@team.example.com", "user_id": USER, "disabled": True},
            ]),
            save_mail=AsyncMock(),
            log_sent=AsyncMock(),
            get_sent_from_addresses=AsyncMock(return_value=[]),
            get_recipient_addresses=AsyncMock(return_value=[]),
            count_unread=AsyncMock(return_value=0),
            get_stats=AsyncMock(return_value={"delivered": 3, "muted": 1}),
            get_counts_since=AsyncMock(return_value={"delivered": 2}),
            count_starred=AsyncMock(return_value=5),
            count_aliases=AsyncMock(return_value=2),
            whereis=AsyncMock(return_value=[{
                "to": "a@b.c", "forwarded_by": None, "count": 2,
                "first": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "last": datetime(2026, 1, 2, tzinfo=timezone.utc), "sender": "bob@ext.com",
            }]),
            get_unassigned=AsyncMock(return_value=[mail(None, status="unassigned")]),
            set_mail_status=AsyncMock(),
            add_delivery=AsyncMock(),
            add_rule=AsyncMock(),
            get_rules=AsyncMock(return_value=[{"_id": ObjectId(TID), "pattern": "spam.com", "action": "mute"}]),
            delete_rule_by_oid=AsyncMock(return_value=True),
            get_templates=AsyncMock(return_value=[{"_id": ObjectId(TID), "name": "OK", "text": "Accepted"}]),
            get_template=AsyncMock(return_value={"_id": ObjectId(TID), "user_id": USER, "name": "OK", "text": "t"}),
            add_template=AsyncMock(return_value={"_id": ObjectId(TID), "name": "OK", "text": "t"}),
            delete_template=AsyncMock(return_value=True),
            add_route=AsyncMock(), add_domain_route=AsyncMock(),
            toggle_route=AsyncMock(return_value=True), toggle_domain_route=AsyncMock(return_value=None),
            delete_route=AsyncMock(return_value=True), delete_domain_route=AsyncMock(return_value=False),
        )
        api.storage.db = db
        send = AsyncMock(return_value="<new@team.example.com>")
        tg = AsyncMock(return_value=77)
        with patch("src.app.mail.composer.send_email", send), \
                patch.object(api, "send_telegram_message", tg):
            async with TestClient(TestServer(api.make_app())) as client:
                await coro_fn(client, db, send, tg)
    asyncio.run(main())


def send_body(**kw):
    b = {"from": "shop@team.example.com", "to": "x@example.com", "subject": "S", "body": "B"}
    b.update(kw)
    return b


def test_me_user_and_admin():
    async def sc(c, db, send, tg):
        r = await c.get("/api/me", headers=hdr(USER))
        d = await r.json()
        assert d["user_id"] == USER and d["is_admin"] is False
        assert d["from_addresses"] == ["shop@team.example.com", "al@team.example.com", "team@team.example.com"]
        assert d["users"] == [] and d["ttl_days"] == 180
        d = await (await c.get("/api/me", headers=hdr(ADMIN))).json()
        assert d["is_admin"] is True
        assert {"user_id": 2, "domain": "team.example.com"} in d["users"]
        assert any(u["user_id"] == ADMIN for u in d["users"])
    run(sc)


def test_stats_scope():
    async def sc(c, db, send, tg):
        d = await (await c.get("/api/stats", headers=hdr(USER))).json()
        assert d == {"total": 4, "by_status": {"delivered": 3, "muted": 1},
                     "last_24h": {"delivered": 2}, "starred": 5, "aliases": 2, "routes": None}
        db.get_stats.assert_awaited_with(user_id=USER)
        assert db.get_counts_since.await_args.kwargs == {"user_id": USER}
        d = await (await c.get("/api/stats", headers=hdr(ADMIN))).json()
        assert d["routes"] == 4
        db.get_stats.assert_awaited_with(user_id=None)
    run(sc)


def test_whereis():
    async def sc(c, db, send, tg):
        r = await c.get("/api/whereis", headers=hdr(USER))
        assert r.status == 400
        r = await c.get("/api/whereis?q=bob", headers=hdr(USER))
        item = (await r.json())["items"][0]
        assert item == {"to": "a@b.c", "via": None, "count": 2, "first": "2026-01-01T00:00:00+00:00",
                        "last": "2026-01-02T00:00:00+00:00", "sample_from": "bob@ext.com"}
        db.whereis.assert_awaited_with("bob", USER)
    run(sc)


def test_unassigned_and_assign():
    async def sc(c, db, send, tg):
        assert (await c.get("/api/unassigned", headers=hdr(USER))).status == 403
        assert (await c.post(f"/api/mails/{OID}/assign", headers=hdr(USER))).status == 403
        d = await (await c.get("/api/unassigned", headers=hdr(ADMIN))).json()
        assert d["items"][0]["id"] == OID
        db.get_mail.return_value = mail(None, status="unassigned")
        r = await c.post(f"/api/mails/{OID}/assign", headers=hdr(ADMIN))
        assert r.status == 200 and (await r.json()) == {"ok": True}
        db.set_mail_status.assert_awaited_with(OID, "delivered")
        db.add_delivery.assert_awaited_with(OID, ADMIN, 77)
        db.get_mail.return_value = mail(None, status="delivered")
        assert (await c.post(f"/api/mails/{OID}/assign", headers=hdr(ADMIN))).status == 409
        db.get_mail.return_value = mail(None, status="unassigned")
        tg.return_value = None
        assert (await c.post(f"/api/mails/{OID}/assign", headers=hdr(ADMIN))).status == 502
    run(sc)


def test_mail_rule():
    async def sc(c, db, send, tg):
        url = f"/api/mails/{OID}/rule"
        body = {"action": "mute", "target": "address"}
        # non-admin: personal rule for their own email
        r = await c.post(url, json=body, headers=hdr(USER))
        assert r.status == 200
        db.add_rule.assert_awaited_with("bob@ext.com", "mute", user_id=USER)
        db.get_mail.return_value = mail(user_id=99)
        assert (await c.post(url, json=body, headers=hdr(USER))).status == 403
        db.get_mail.return_value = mail()
        r = await c.post(url, json=body, headers=hdr(ADMIN))
        assert (await r.json()) == {"ok": True, "pattern": "bob@ext.com"}
        db.add_rule.assert_awaited_with("bob@ext.com", "mute", user_id=None)
        r = await c.post(url, json={"action": "allow", "target": "domain"}, headers=hdr(ADMIN))
        assert (await r.json())["pattern"] == "ext.com"
        db.add_rule.assert_awaited_with("ext.com", "allow", user_id=None)
        assert (await c.post(url, json={"action": "x", "target": "domain"}, headers=hdr(ADMIN))).status == 400
        db.get_mail.return_value = None
        assert (await c.post(url, json=body, headers=hdr(ADMIN))).status == 404
    run(sc)


def test_get_mail_extra_fields():
    async def sc(c, db, send, tg):
        d = await (await c.get(f"/api/mails/{OID}", headers=hdr(USER))).json()
        assert d["message_id"] == "<orig@ext.com>" and d["can_rule"] is True
        d = await (await c.get(f"/api/mails/{OID}", headers=hdr(ADMIN))).json()
        assert d["can_rule"] is True
    run(sc)


def test_send_ok_and_save():
    async def sc(c, db, send, tg):
        r = await c.post("/api/send", json=send_body(to="a@example.com; b@example.org"), headers=hdr(USER))
        assert r.status == 200 and (await r.json()) == {"ok": True}
        kw = send.await_args.kwargs
        assert kw["from_addr"] == "shop@team.example.com"
        assert kw["to_addrs"] == ["a@example.com", "b@example.org"]
        assert kw["subject"] == "S" and kw["in_reply_to"] is None
        saved = db.save_mail.await_args.args[0]
        assert saved["status"] == "sent" and saved["user_id"] == USER
    run(sc)


def test_send_any_local_part_on_own_domain():
    async def sc(c, db, send, tg):
        r = await c.post("/api/send", json=send_body(**{"from": "Anything@team.example.com"}), headers=hdr(USER))
        assert r.status == 200
        assert send.await_args.kwargs["from_addr"] == "anything@team.example.com"
        r = await c.post("/api/send", json=send_body(**{"from": "anything@example.com"}), headers=hdr(ADMIN))
        assert r.status == 200
    run(sc)


def test_send_validation():
    async def sc(c, db, send, tg):
        h = hdr(USER)
        post = lambda **kw: c.post("/api/send", json=send_body(**kw), headers=h)  # noqa: E731
        assert (await post(**{"from": "x@example.com"})).status == 400  # foreign domain
        assert (await post(**{"from": "x@evil.com"})).status == 400
        assert (await post(**{"from": "bad name@team.example.com"})).status == 400
        assert (await post(**{"from": "-x@team.example.com"})).status == 400
        assert (await post(**{"from": "noat"})).status == 400
        assert (await post(to=",".join(f"u{i}@example.com" for i in range(11)))).status == 400
        assert (await post(to="not-an-email")).status == 400
        assert (await post(to="a@example.com\r\nBcc: z@z.zz")).status == 400
        assert (await post(subject="x" * 301)).status == 400
        assert (await post(body="x" * 50_001)).status == 400
        assert (await post(body="")).status == 400
        big = base64.b64encode(b"0" * (15 * 1024 * 1024 + 1)).decode()
        assert (await post(attachments=[{"name": "a", "mime": "a/b", "data": big}])).status == 400
        assert (await post(attachments=[{"name": "a", "mime": "a/b", "data": "!!!"}])).status == 400
        assert (await post(reply_to=OID, forward_of=OID)).status == 400
        send.assert_not_awaited()
    run(sc)


def test_send_attachment_name_cleaned():
    async def sc(c, db, send, tg):
        att = {"name": "../../etc/passwd", "mime": "text/plain", "data": base64.b64encode(b"hi").decode()}
        r = await c.post("/api/send", json=send_body(attachments=[att]), headers=hdr(USER))
        assert r.status == 200
        assert send.await_args.kwargs["attachments"] == [("passwd", b"hi", "text/plain")]
    run(sc)


def test_send_reply_headers():
    async def sc(c, db, send, tg):
        r = await c.post("/api/send", json=send_body(to="bob@ext.com", subject="", reply_to=OID), headers=hdr(USER))
        assert r.status == 200
        kw = send.await_args.kwargs
        assert kw["subject"] == "Re: Hello" and kw["in_reply_to"] == "<orig@ext.com>"
        assert "> line1" in kw["body_text"] and "bob@ext.com" in kw["body_text"]
        # someone else's email
        db.get_mail.return_value = mail(user_id=99)
        r = await c.post("/api/send", json=send_body(reply_to=OID), headers=hdr(USER))
        assert r.status == 403
        db.get_mail.return_value = None
        r = await c.post("/api/send", json=send_body(reply_to=OID), headers=hdr(USER))
        assert r.status == 404
    run(sc)


def test_send_forward():
    async def sc(c, db, send, tg):
        r = await c.post("/api/send", json=send_body(subject="", body="", forward_of=OID), headers=hdr(USER))
        assert r.status == 200
        kw = send.await_args.kwargs
        assert kw["subject"] == "Fwd: Hello" and kw["in_reply_to"] is None
        assert "Forwarded message" in kw["body_text"] and "line2" in kw["body_text"]
        db.get_mail.return_value = mail(user_id=99)
        r = await c.post("/api/send", json=send_body(forward_of=OID), headers=hdr(USER))
        assert r.status == 403
    run(sc)


def test_send_rate_limit_and_smtp_error():
    async def sc(c, db, send, tg):
        h = hdr(USER)
        for _ in range(20):
            assert (await c.post("/api/send", json=send_body(), headers=h)).status == 200
        r = await c.post("/api/send", json=send_body(), headers=h)
        assert r.status == 429 and "error" in await r.json()
        assert send.await_count == 20
        # per-user limit: admin is not affected
        send.side_effect = RuntimeError("smtp down")
        r = await c.post("/api/send", json=send_body(**{"from": "adm@example.com"}), headers=hdr(ADMIN))
        assert r.status == 502 and "smtp down" in (await r.json())["error"]
    run(sc)


def test_templates():
    async def sc(c, db, send, tg):
        h = hdr(USER)
        d = await (await c.get("/api/templates", headers=h)).json()
        assert d["items"] == [{"id": TID, "name": "OK", "text": "Accepted"}]
        db.get_templates.assert_awaited_with(USER)
        r = await c.post("/api/templates", json={"name": "OK", "text": "t"}, headers=h)
        assert r.status == 201 and (await r.json())["item"]["id"] == TID
        assert (await c.post("/api/templates", json={"name": "x" * 65, "text": "t"}, headers=h)).status == 400
        assert (await c.post("/api/templates", json={"name": "n", "text": "t" * 4001}, headers=h)).status == 400
        assert (await c.delete(f"/api/templates/{TID}", headers=h)).status == 200
        db.get_template.return_value = {"_id": ObjectId(TID), "user_id": 99}
        assert (await c.delete(f"/api/templates/{TID}", headers=h)).status == 404
        assert db.delete_template.await_count == 1
    run(sc)


def test_routes_admin_only_and_crud():
    async def sc(c, db, send, tg):
        u = hdr(USER)
        assert (await c.get("/api/routes", headers=u)).status == 403
        assert (await c.post("/api/routes", json={"kind": "prefix", "value": "a", "user_id": 2}, headers=u)).status == 403
        assert (await c.post("/api/routes/toggle", json={"kind": "prefix", "value": "a"}, headers=u)).status == 403
        assert (await c.post("/api/routes/delete", json={"kind": "prefix", "value": "a"}, headers=u)).status == 403
        a = hdr(ADMIN)
        items = (await (await c.get("/api/routes", headers=a)).json())["items"]
        assert {"kind": "domain", "value": "x.top", "user_id": USER, "disabled": False} in items
        assert {"kind": "prefix", "value": "off", "user_id": USER, "disabled": True} in items
        r = await c.post("/api/routes", json={"kind": "prefix", "value": "New", "user_id": 2}, headers=a)
        assert r.status == 201
        db.add_route.assert_awaited_with("new", 2)
        r = await c.post("/api/routes", json={"kind": "domain", "value": "@Ex.COM", "user_id": 2}, headers=a)
        db.add_domain_route.assert_awaited_with("ex.com", 2)
        assert (await c.post("/api/routes", json={"kind": "domain", "value": "bad", "user_id": 2}, headers=a)).status == 400
        assert (await c.post("/api/routes", json={"kind": "prefix", "value": "a", "user_id": "x"}, headers=a)).status == 400
        assert (await c.post("/api/routes", json={"kind": "regex", "value": "a", "user_id": 2}, headers=a)).status == 400
        d = await (await c.post("/api/routes/toggle", json={"kind": "prefix", "value": "a"}, headers=a)).json()
        assert d == {"disabled": False}
        assert (await c.post("/api/routes/toggle", json={"kind": "domain", "value": "a.top"}, headers=a)).status == 404
        r = await c.post("/api/routes/delete", json={"kind": "prefix", "value": "a"}, headers=a)
        assert (await r.json()) == {"ok": True}
        assert (await c.post("/api/routes/delete", json={"kind": "domain", "value": "a.top"}, headers=a)).status == 404
    run(sc)


def test_rules_admin_global_user_personal():
    async def sc(c, db, send, tg):
        u = hdr(USER)
        d = await (await c.get("/api/rules", headers=u)).json()
        assert d["items"][0]["pattern"] == "spam.com" and "user_id" in d["items"][0]
        db.get_rules.assert_awaited_with(USER)
        r = await c.post("/api/rules", json={"pattern": "a.com", "action": "mute"}, headers=u)
        assert r.status == 201
        db.add_rule.assert_awaited_with("a.com", "mute", user_id=USER)
        db.delete_rule_by_oid.return_value = False  # storage does not delete someone else's rule
        assert (await c.delete(f"/api/rules/{TID}", headers=u)).status == 404
        db.delete_rule_by_oid.assert_awaited_with(TID, USER)
        a = hdr(ADMIN)
        r = await c.post("/api/rules", json={"pattern": " Spam.COM ", "action": "allow"}, headers=a)
        assert r.status == 201
        db.add_rule.assert_awaited_with("spam.com", "allow", user_id=None)
        assert (await c.post("/api/rules", json={"pattern": "", "action": "mute"}, headers=a)).status == 400
        assert (await c.post("/api/rules", json={"pattern": "a.com", "action": "x"}, headers=a)).status == 400
        db.delete_rule_by_oid.return_value = True
        assert (await c.delete(f"/api/rules/{TID}", headers=a)).status == 200
        db.delete_rule_by_oid.assert_awaited_with(TID, None)
    run(sc)
