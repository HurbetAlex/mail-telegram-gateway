"""API Mini App v2: read state, blobs, batch actions, newsletters, settings, server."""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api_parity import ADMIN, OID, USER, api, hdr, mail  # noqa: E402

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

BID = "a" * 32


def run(coro_fn, tmp_path=None):
    async def main():
        api._send_log.clear()
        db = SimpleNamespace(
            get_mail=AsyncMock(return_value=mail()),
            count_unread=AsyncMock(return_value=3),
            get_all_routes=AsyncMock(return_value=[]),
            list_aliases=AsyncMock(return_value=[]),
            get_sent_from_addresses=AsyncMock(return_value=[]),
            get_recipient_addresses=AsyncMock(return_value=[]),
            get_history=AsyncMock(return_value=([mail(read=False, html_id="h", attachments=[{"id": BID}])], 1)),
            mark_read=AsyncMock(return_value=2),
            set_starred=AsyncMock(return_value=True),
            delete_mails=AsyncMock(return_value={
                "deleted": 1, "mail_ids": [OID], "deliveries": [{"user_id": USER, "tg_message_id": 5}]}),
            get_newsletters=AsyncMock(return_value=[{
                "sender": "news@ext.com", "name": "N", "domain": "ext.com", "count": 4,
                "last": datetime(2026, 1, 1, tzinfo=timezone.utc), "latest_mail_id": OID,
                "unsubscribe": {"http": "https://x"}, "unsubscribed": False, "muted": False}]),
            add_rule=AsyncMock(),
            set_unsubscribed=AsyncMock(),
            get_codes=AsyncMock(return_value=[{"mail_id": OID, "otp": "123456", "from": "a", "subject": "s",
                                               "at": datetime(2026, 1, 1, tzinfo=timezone.utc)}]),
            get_contacts=AsyncMock(return_value=["a@b.c"]),
            get_settings=AsyncMock(return_value={"tz": "UTC"}),
            set_settings=AsyncMock(return_value={"tz": "Europe/Kyiv"}),
        )
        api.storage.db = db
        tgbot = SimpleNamespace(delete_message=AsyncMock())
        with patch.object(api, "bot", tgbot):
            async with TestClient(TestServer(api.make_app())) as client:
                await coro_fn(client, db, tgbot)
    asyncio.run(main())


def test_list_unread_bulk_and_flags():
    async def sc(c, db, bot):
        d = await (await c.get("/api/mails?bulk=1&query=hi", headers=hdr(USER))).json()
        assert d["unread"] == 3
        it = d["items"][0]
        assert it["read"] is False and it["has_html"] is True and it["has_attachments"] is True
        assert db.get_history.await_args.args[0] == "hi is:bulk"
        db.count_unread.assert_awaited_with(USER)
        await c.get("/api/mails?query=from:x", headers=hdr(USER))
        assert db.get_history.await_args.args[0] == "from:x"
        # old email without a read field counts as read
        db.get_history.return_value = ([mail()], 1)
        assert (await (await c.get("/api/mails", headers=hdr(USER))).json())["items"][0]["read"] is True
        assert (await (await c.get("/api/me", headers=hdr(USER))).json())["unread"] == 3
    run(sc)


def test_mail_card_fields():
    async def sc(c, db, bot):
        db.get_mail.return_value = mail(html_id="h", read=False,
                                        attachments=[{"id": BID, "name": "a.pdf", "mime": "application/pdf", "size": 9}])
        d = await (await c.get(f"/api/mails/{OID}", headers=hdr(USER))).json()
        assert d["attachments"] == [{"id": BID, "name": "a.pdf", "mime": "application/pdf", "size": 9}]
        assert d["has_html"] is True and d["read"] is False and d["can_rule"] is True
    run(sc)


def test_mark_read():
    async def sc(c, db, bot):
        r = await c.post("/api/mails/read", json={"ids": [OID], "read": True}, headers=hdr(USER))
        assert (await r.json()) == {"updated": 2}
        db.mark_read.assert_awaited_with([OID], True, USER)
        assert (await c.post("/api/mails/read", json={"ids": [OID] * 2 + [str(i) for i in range(101)], "read": True},
                             headers=hdr(USER))).status == 400
        assert (await c.post("/api/mails/read", json={"ids": [OID], "read": "x"}, headers=hdr(USER))).status == 400
    run(sc)


def test_html_endpoint():
    async def sc(c, db, bot):
        url = f"/api/mails/{OID}/html"
        assert (await c.get(url, headers=hdr(USER))).status == 404  # no html_id
        db.get_mail.return_value = mail(html_id=BID)
        with patch.object(api.blobstore, "read_html", AsyncMock(return_value="<b>x</b>")):
            assert (await (await c.get(url, headers=hdr(USER))).json()) == {"html": "<b>x</b>"}
        db.get_mail.return_value = mail(user_id=99, html_id=BID)
        assert (await c.get(url, headers=hdr(USER))).status == 403
    run(sc)


def test_blob_headers_access_and_active_mime(tmp_path):
    async def sc(c, db, bot):
        meta = {"_id": BID, "mail_id": OID, "kind": "att", "name": "report ../x.pdf", "mime": "application/pdf"}
        (tmp_path / BID).write_bytes(b"%PDF")
        with patch.dict(os.environ, {"BLOB_DIR": str(tmp_path)}), \
                patch.object(api.blobstore, "get_blob_meta", AsyncMock(return_value=meta)):
            r = await c.get(f"/api/blobs/{BID}", headers=hdr(USER))
            assert r.status == 200 and await r.read() == b"%PDF"
            assert r.headers["Content-Type"].startswith("application/pdf")
            assert r.headers["Content-Disposition"].startswith("attachment; filename*=UTF-8''")
            assert "/" not in r.headers["Content-Disposition"].split("''", 1)[1]
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["Cache-Control"] == "private"
            assert r.headers["Content-Security-Policy"] == "sandbox"
            for bad in ("text/html", "image/svg+xml", "application/xhtml+xml", "text/xml"):
                meta["mime"] = bad
                r = await c.get(f"/api/blobs/{BID}", headers=hdr(USER))
                assert r.headers["Content-Type"].startswith("application/octet-stream"), bad
            # someone else's email
            db.get_mail.return_value = mail(user_id=99)
            assert (await c.get(f"/api/blobs/{BID}", headers=hdr(USER))).status == 403
            # no metadata / email
            db.get_mail.return_value = None
            assert (await c.get(f"/api/blobs/{BID}", headers=hdr(USER))).status == 404
        with patch.object(api.blobstore, "get_blob_meta", AsyncMock(return_value=None)):
            assert (await c.get(f"/api/blobs/{BID}", headers=hdr(USER))).status == 404
    run(sc)


def test_delete_mail_with_tg_and_blobs():
    async def sc(c, db, bot):
        dbl = AsyncMock(return_value=1)
        with patch.object(api.blobstore, "delete_blobs_for_mails", dbl):
            r = await c.delete(f"/api/mails/{OID}", headers=hdr(USER))
            assert r.status == 200 and (await r.json()) == {"ok": True}
            db.delete_mails.assert_awaited_with([OID], USER)
            bot.delete_message.assert_awaited_with(chat_id=USER, message_id=5)
            dbl.assert_awaited_with([OID])
            # a Telegram failure does not break deletion
            bot.delete_message.side_effect = RuntimeError("old")
            assert (await c.delete(f"/api/mails/{OID}", headers=hdr(USER))).status == 200
            db.get_mail.return_value = mail(user_id=99)
            db.delete_mails.reset_mock()
            assert (await c.delete(f"/api/mails/{OID}", headers=hdr(USER))).status == 403
            db.delete_mails.assert_not_awaited()
    run(sc)


def test_batch():
    async def sc(c, db, bot):
        h = hdr(USER)
        post = lambda **kw: c.post("/api/mails/batch", json=kw, headers=h)  # noqa: E731
        assert (await post(ids=[OID], action="boom")).status == 400
        assert (await post(ids=[f"{i:024x}" for i in range(101)], action="star")).status == 400
        d = await (await post(ids=[OID], action="star")).json()
        assert d == {"done": 1, "results": {OID: "ok"}}
        db.set_starred.assert_awaited_with(OID, True, user_id=USER)
        await post(ids=[OID], action="unread")
        db.mark_read.assert_awaited_with([OID], False, USER)
        # someone else's email is untouched
        other = "64b000000000000000000009"
        db.get_mail.side_effect = lambda i: mail(user_id=99) if i == other else mail()
        with patch.object(api.blobstore, "delete_blobs_for_mails", AsyncMock()):
            d = await (await post(ids=[OID, other, "zzz"], action="delete")).json()
        assert d["results"] == {OID: "ok", other: "forbidden", "zzz": "invalid"} and d["done"] == 1
        db.delete_mails.assert_awaited_with([OID], USER)
        # unsubscribe
        db.get_mail.side_effect = None
        db.get_mail.return_value = mail(unsubscribe={"http": "https://x"})
        with patch.object(api.unsubscribe_mod, "unsubscribe", AsyncMock(return_value=("posted", None))):
            d = await (await post(ids=[OID], action="unsubscribe")).json()
        assert d == {"done": 1, "results": {OID: "posted"}}
        db.set_unsubscribed.assert_awaited_with(OID)
    run(sc)


def test_newsletters():
    async def sc(c, db, bot):
        d = await (await c.get("/api/newsletters", headers=hdr(USER))).json()
        assert d["items"][0]["last"] == "2026-01-01T00:00:00+00:00"
        db.get_newsletters.assert_awaited_with(USER)
        db.get_mail.return_value = mail(unsubscribe={"http": "https://x"})
        with patch.object(api.unsubscribe_mod, "unsubscribe", AsyncMock(return_value=("link", "u"))):
            r = await c.post("/api/newsletters/unsubscribe",
                             json={"senders": ["news@ext.com", "ghost@x.com"]}, headers=hdr(USER))
        assert (await r.json())["results"] == {"news@ext.com": "link", "ghost@x.com": "not_found"}
        assert (await c.post("/api/newsletters/unsubscribe", json={"senders": ["a@b.c"] * 1 + [f"{i}@b.c" for i in range(51)]},
                             headers=hdr(USER))).status == 400
        r = await c.post("/api/newsletters/mute", json={"senders": ["News@Ext.com"], "target": "domain"}, headers=hdr(USER))
        assert r.status == 200
        db.add_rule.assert_awaited_with("ext.com", "mute", user_id=USER)
        await c.post("/api/newsletters/mute", json={"senders": ["news@ext.com"], "target": "address"}, headers=hdr(ADMIN))
        db.add_rule.assert_awaited_with("news@ext.com", "mute", user_id=None)
        assert (await c.post("/api/newsletters/mute", json={"senders": ["a@b.c"], "target": "x"}, headers=hdr(USER))).status == 400
    run(sc)


def test_codes_contacts_settings():
    async def sc(c, db, bot):
        h = hdr(USER)
        it = (await (await c.get("/api/codes", headers=h)).json())["items"][0]
        assert it == {"mail_id": OID, "otp": "123456", "from": "a", "subject": "s", "at": "2026-01-01T00:00:00+00:00"}
        assert (await (await c.get("/api/contacts?q=a", headers=h)).json()) == {"items": ["a@b.c"]}
        db.get_contacts.assert_awaited_with(USER, "a")
        assert (await (await c.get("/api/settings", headers=h)).json()) == {"tz": "UTC"}
        r = await c.post("/api/settings", json={"tz": "Europe/Kyiv"}, headers=h)
        assert (await r.json()) == {"tz": "Europe/Kyiv"}
        db.set_settings.assert_awaited_with(USER, {"tz": "Europe/Kyiv"})
        await c.post("/api/settings", json={"patch": {"quiet_from": "22:00"}}, headers=h)
        db.set_settings.assert_awaited_with(USER, {"quiet_from": "22:00"})
        db.set_settings.side_effect = ValueError("Invalid tz")
        r = await c.post("/api/settings", json={"tz": "x"}, headers=h)
        assert r.status == 400 and (await r.json())["error"] == "Invalid tz"
    run(sc)


def test_server_admin_only_and_unban():
    async def sc(c, db, bot):
        assert (await c.get("/api/server", headers=hdr(USER))).status == 403
        assert (await c.post("/api/server/unban", json={"ip": "1.2.3.4"}, headers=hdr(USER))).status == 403
        with patch.object(api.monitor, "server_status", AsyncMock(return_value={"failed_24h": 0})), \
                patch.object(api.monitor, "request_unban") as ub:
            assert (await (await c.get("/api/server", headers=hdr(ADMIN))).json()) == {"failed_24h": 0}
            r = await c.post("/api/server/unban", json={"ip": " 1.2.3.4 "}, headers=hdr(ADMIN))
            assert (await r.json()) == {"ok": True}
            ub.assert_called_once_with("1.2.3.4")
            for bad in ("1.2.3", "a; rm -rf", "", None, 5):
                r = await c.post("/api/server/unban", json={"ip": bad}, headers=hdr(ADMIN))
                assert r.status == 400, bad
            assert ub.call_count == 1
    run(sc)
