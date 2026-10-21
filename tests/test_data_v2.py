"""Data layer v2: search, blob store, rules, settings, quiet hours, storage filters.
No network and no real Mongo: fake collections and a tmpdir."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from bson import ObjectId

os.environ.setdefault("BOT_TOKEN", "123:test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app.db import storage as st  # noqa: E402
from src.app.mail import blobstore  # noqa: E402
from src.app.utils.parser import extract_html, parse_email  # noqa: E402
from src.app.utils.search import parse_search  # noqa: E402
from src.app.utils import settings as cfg  # noqa: E402

UTC = timezone.utc


def run(coro):
    return asyncio.run(coro)


# --- fake collection ---

def _match(doc, flt):
    for k, v in flt.items():
        if k == "$or":
            if not any(_match(doc, f) for f in v):
                return False
            continue
        have = doc.get(k)
        if isinstance(v, dict) and any(x.startswith("$") for x in v):
            for op, arg in v.items():
                if op == "$in" and have not in arg:
                    return False
                if op == "$lt" and not (have is not None and have < arg):
                    return False
                if op == "$gte" and not (have is not None and have >= arg):
                    return False
        elif have != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction=1):
        self.docs.sort(key=lambda d: d[key], reverse=direction < 0)
        return self

    async def to_list(self, length=None):
        return self.docs


class FakeColl:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def find_one(self, flt):
        return next((d for d in self.docs if _match(d, flt)), None)

    def find(self, flt=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, flt or {})])

    async def delete_many(self, flt):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _match(d, flt)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    async def update_one(self, flt, upd, upsert=False):
        doc = await self.find_one(flt)
        if doc is None and upsert:
            doc = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            self.docs.append(doc)
        if doc is not None:
            doc.update(upd.get("$set", {}))


# --- parse_search ---

def test_search_plain_text():
    p = parse_search("hello world")
    assert p["text"] == "hello world" and not p["from"]


def test_search_all_operators():
    p = parse_search(
        'from:amazon to:me@x.io alias:shop has:code has:attachment is:unread '
        'is:starred is:bulk after:2025-01-02 before:2025-02-03 invoice'
    )
    assert p["from"] == ["amazon"] and p["to"] == ["me@x.io"] and p["alias"] == ["shop"]
    assert p["has_code"] and p["has_attachment"] and p["is_unread"] and p["is_starred"] and p["is_bulk"]
    assert p["after"] == datetime(2025, 1, 2, tzinfo=UTC)
    assert p["before"] == datetime(2025, 2, 3, tzinfo=UTC)
    assert p["text"] == "invoice"


def test_search_quotes_and_garbage():
    p = parse_search('from:"john petrov" "exact phrase" after:вчера unknown:val has:foo from:')  # "вчера" = "yesterday": functional Russian date word
    assert p["from"] == ["john petrov"]
    assert p["after"] is None
    assert "exact phrase" in p["text"] and "unknown:val" in p["text"]
    assert parse_search("")["text"] == ""
    assert parse_search('"unclosed')["text"]  # does not raise


def test_history_filter_escapes_regex():
    flt = st.build_history_filter("from:a.b+c (x", user_id=5, status="delivered")
    rxs = [c for c in flt["$and"] if "from" in c]
    assert rxs[0]["from"]["$regex"] == r"a\.b\+c"
    assert {"user_id": 5} in flt["$and"] and {"status": "delivered"} in flt["$and"]
    text = [c for c in flt["$and"] if "$or" in c][0]
    assert text["$or"][0]["from"]["$regex"] == r"\(x"


def test_history_filter_flags_and_dates():
    flt = st.build_history_filter("is:unread has:code has:attachment is:starred is:bulk after:2025-01-01 before:2025-01-10 to:z")
    cl = flt["$and"]
    assert {"read": False} in cl and {"starred": True} in cl and {"bulk": True} in cl
    assert {"otp": {"$nin": [None, ""]}} in cl
    assert {"attachments.0": {"$exists": True}} in cl
    assert {"_id": {"$gte": ObjectId.from_datetime(datetime(2025, 1, 1, tzinfo=UTC))}} in cl
    assert {"_id": {"$lt": ObjectId.from_datetime(datetime(2025, 1, 10, tzinfo=UTC))}} in cl
    to = [c for c in cl if "$or" in c][0]["$or"]
    assert [list(x)[0] for x in to] == ["to", "orig_to", "alias"]
    assert st.build_history_filter("") == {}


# --- blob store ---

@pytest.fixture
def blobs(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOB_DIR", str(tmp_path / "b"))
    coll_b, coll_m = FakeColl(), FakeColl()
    monkeypatch.setattr(st, "db", SimpleNamespace(db=SimpleNamespace(blobs=coll_b, mails=coll_m)))
    blobstore.reset_counter()
    monkeypatch.setattr(blobstore, "_free_bytes", lambda: 10 ** 12)
    yield SimpleNamespace(b=coll_b, m=coll_m, dir=tmp_path / "b")
    blobstore.reset_counter()


def test_blob_save_read_delete(blobs):
    mid = str(ObjectId())

    async def go():
        hid = await blobstore.save_blob(b"<p>hello</p>", mail_id=mid,
                                        user_id=1, kind="html", name="mail.html", mime="text/html")
        aid = await blobstore.save_blob(b"PDF", mail_id=mid, user_id=1, kind="att", name="a.pdf", mime="application/pdf")
        assert await blobstore.read_html(hid) == "<p>hello</p>"
        assert await blobstore.read_html(aid) is None
        # html is stored gzipped, the attachment as is
        assert open(blobstore.blob_path(hid), "rb").read()[:2] == b"\x1f\x8b"
        assert open(blobstore.blob_path(aid), "rb").read() == b"PDF"
        assert (await blobstore.get_blob_meta(aid))["name"] == "a.pdf"
        assert await blobstore.delete_blobs_for_mails([mid]) == 2
        assert not os.path.exists(blobstore.blob_path(hid)) and blobs.b.docs == []
        assert await blobstore.read_html(hid) is None

    run(go())


def test_blob_limits_and_bad_id(blobs, monkeypatch):
    monkeypatch.setenv("BLOB_ATTACH_MAX_MB", "1")
    monkeypatch.setenv("BLOB_HTML_MAX_KB", "1")
    with pytest.raises(ValueError):
        run(blobstore.save_blob(b"x" * 1025, mail_id="m", user_id=1, kind="html", name="h", mime="text/html"))
    with pytest.raises(ValueError):
        run(blobstore.save_blob(b"x" * (1024 * 1024 + 1), mail_id="m", user_id=1, kind="att", name="h", mime="a/b"))
    with pytest.raises(ValueError):
        blobstore.blob_path("../../etc/passwd")
    assert run(blobstore.get_blob_meta("../x")) is None


def _meta(i, mail_id, age_days, size, now):
    return {"_id": f"{i:032x}", "mail_id": mail_id, "created_at": now - timedelta(days=age_days),
            "size": size, "disk": size, "kind": "att"}


def test_select_expired_and_limit_protect_starred():
    now = datetime(2025, 6, 1, tzinfo=UTC)
    metas = [_meta(1, "a", 40, 10, now), _meta(2, "s", 90, 10, now), _meta(3, "b", 5, 10, now)]
    assert blobstore.select_expired(metas, {"s"}, now, 30) == [metas[0]["_id"]]
    assert blobstore.select_expired(metas, set(), now, 0) == []
    # limit 15 with a total of 30: the oldest (starred) one is skipped, the rest go by age
    over = blobstore.select_over_limit(metas, {"s"}, 15)
    assert over == [metas[0]["_id"], metas[2]["_id"]]
    assert metas[1]["_id"] not in over
    assert blobstore.select_over_limit(metas, set(), 100) == []


def test_enforce_blob_limits_ttl_and_cap(blobs, monkeypatch):
    now = datetime(2025, 6, 1, tzinfo=UTC)
    starred, old, mid, new = ObjectId(), ObjectId(), ObjectId(), ObjectId()
    blobs.m.docs = [{"_id": starred, "starred": True}, {"_id": old}, {"_id": mid}, {"_id": new}]
    monkeypatch.setenv("BLOB_TTL_DAYS", "30")
    monkeypatch.setenv("BLOB_MAX_MB", "100")  # the limit does not interfere with writes

    async def add(mail, age, size):
        bid = await blobstore.save_blob(b"z" * size, mail_id=str(mail), user_id=1, kind="att", name="f", mime="a/b")
        next(d for d in blobs.b.docs if d["_id"] == bid)["created_at"] = now - timedelta(days=age)
        return bid

    async def go():
        monkeypatch.setenv("BLOB_ATTACH_MAX_MB", "5")
        b_star = await add(starred, 100, 1000)  # expired, but starred
        b_old = await add(old, 50, 1000)  # expired
        await add(mid, 10, 700 * 1024)
        b_new = await add(new, 1, 700 * 1024)  # together with mid > 1 MB
        monkeypatch.setenv("BLOB_MAX_MB", "1")
        res = await blobstore.enforce_blob_limits(now=now)
        assert res == {"expired": 1, "over_limit": 1}
        left = {d["_id"] for d in blobs.b.docs}
        assert left == {b_star, b_new}
        assert not os.path.exists(blobstore.blob_path(b_old))
        assert os.path.exists(blobstore.blob_path(b_star))

    run(go())


def test_extract_html_and_parse_email_compat():
    raw = (b"From: a@b.c\nTo: d@e.f\nSubject: s\nMIME-Version: 1.0\n"
           b"Content-Type: multipart/alternative; boundary=XX\n\n"
           b"--XX\nContent-Type: text/plain\n\nplain\n--XX\n"
           b"Content-Type: text/html; charset=utf-8\n\n<b>hi</b>\n--XX--\n")
    msg, body, atts = parse_email(raw)
    assert body.strip() == "plain" and atts == []
    assert extract_html(msg).strip() == "<b>hi</b>"
    assert extract_html(parse_email(b"Subject: s\n\nx\n")[0]) is None


# --- rules ---

def test_pick_rule_own_beats_global():
    g_mute = {"pattern": "x.io", "action": "mute", "user_id": None}
    own_allow = {"pattern": "x.io", "action": "allow", "user_id": 7}
    other = {"pattern": "a@x.io", "action": "mute", "user_id": 8}
    assert st.pick_rule([g_mute, own_allow, other], 7, "a@x.io") == "allow"
    assert st.pick_rule([g_mute, own_allow, other], 9, "a@x.io") == "mute"
    assert st.pick_rule([own_allow], None, "a@x.io") is None
    # an address is more specific than a domain within the same level
    addr = {"pattern": "a@x.io", "action": "allow", "user_id": None}
    assert st.pick_rule([g_mute, addr], None, "a@x.io") == "allow"


def _storage_rules(docs):
    s = st.Storage.__new__(st.Storage)
    s.db = SimpleNamespace(rules=RulesColl(docs))
    return s


class RulesColl(FakeColl):
    def find(self, flt=None, proj=None):
        flt = dict(flt or {})
        # fake: pattern.$in and $or are supported by _match, None matches a missing field
        return _Cursor([d for d in self.docs if _match(_norm(d), flt)])

    async def delete_one(self, flt):
        for d in self.docs:
            if _match(_norm(d), flt):
                self.docs.remove(d)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


def _norm(d):
    d = dict(d)
    d.setdefault("user_id", None)
    return d


def test_match_rule_and_delete_scope():
    g, own, foreign = ObjectId(), ObjectId(), ObjectId()
    s = _storage_rules([
        {"_id": g, "pattern": "spam.io", "action": "mute"},  # old rule without user_id = global
        {"_id": own, "pattern": "spam.io", "action": "allow", "user_id": 7},
        {"_id": foreign, "pattern": "z.io", "action": "mute", "user_id": 8},
    ])
    assert run(s.match_rule("a@spam.io", 7)) == "allow"
    assert run(s.match_rule("a@spam.io", 9)) == "mute"
    assert run(s.match_rule("a@z.io", 7)) is None
    # delete scope: a non-admin cannot delete a global or someone else's rule
    assert run(s.delete_rule_by_oid(str(g), 7)) is False
    assert run(s.delete_rule_by_oid(str(foreign), 7)) is False
    assert run(s.delete_rule_by_oid(str(own), 7)) is True
    assert run(s.delete_rule_by_oid(str(g), None)) is True  # admin
    assert run(s.delete_rule_by_oid("not-an-id", None)) is False


# --- settings ---

def test_settings_defaults_and_validation(monkeypatch):
    monkeypatch.setenv("DIGEST_TZ", "Europe/Kyiv")
    monkeypatch.setenv("DIGEST_TIME", "08:30")
    monkeypatch.setenv("OTP_HIDE_MINUTES", "20")
    d = cfg.merge_settings(None)
    assert d == {"quiet_from": None, "quiet_to": None, "tz": "Europe/Kyiv", "digest_enabled": True,
                 "digest_time": "08:30", "otp_hide_minutes": 20}
    assert cfg.merge_settings({"otp_hide_minutes": 0, "junk": 1})["otp_hide_minutes"] == 0
    ok = cfg.validate_settings_patch({"quiet_from": "23:00", "quiet_to": "", "tz": "Asia/Tokyo",
                                      "digest_enabled": False, "digest_time": "07:05", "otp_hide_minutes": 1440})
    assert ok["quiet_to"] is None and ok["tz"] == "Asia/Tokyo"
    for bad in ({"foo": 1}, {"quiet_from": "25:00"}, {"digest_time": "9:00"}, {"digest_time": None},
                {"tz": "Mars/Base"}, {"otp_hide_minutes": -1}, {"otp_hide_minutes": 1441},
                {"otp_hide_minutes": True}, {"otp_hide_minutes": "5"}, {"digest_enabled": "yes"}, "x"):
        with pytest.raises(ValueError):
            cfg.validate_settings_patch(bad)


def test_get_set_settings_roundtrip():
    s = st.Storage.__new__(st.Storage)
    s.db = SimpleNamespace(user_settings=SettingsColl())
    out = run(s.set_settings(5, {"quiet_from": "22:00", "quiet_to": "07:00"}))
    assert out["quiet_from"] == "22:00" and out["digest_time"]
    assert run(s.get_settings(6))["quiet_from"] is None
    with pytest.raises(ValueError):
        run(s.set_settings(5, {"bad": 1}))


class SettingsColl(FakeColl):
    async def find_one(self, flt):
        return await super().find_one(flt)


# --- quiet hours / digest ---

def _t(h, m=0, tz=UTC):
    return datetime(2025, 3, 10, h, m, tzinfo=tz)


def test_quiet_hours_over_midnight():
    s = {"quiet_from": "23:00", "quiet_to": "08:00", "tz": "UTC"}
    assert cfg.in_quiet_hours(s, _t(23, 0)) and cfg.in_quiet_hours(s, _t(3)) and cfg.in_quiet_hours(s, _t(7, 59))
    assert not cfg.in_quiet_hours(s, _t(8, 0)) and not cfg.in_quiet_hours(s, _t(12)) and not cfg.in_quiet_hours(s, _t(22, 59))


def test_quiet_hours_same_day_tz_and_off():
    s = {"quiet_from": "13:00", "quiet_to": "15:00", "tz": "Asia/Tokyo"}  # UTC+9
    assert cfg.in_quiet_hours(s, _t(5, 0))  # 14:00 in Tokyo
    assert not cfg.in_quiet_hours(s, _t(13, 0))
    assert not cfg.in_quiet_hours({"quiet_from": None, "quiet_to": None, "tz": "UTC"}, _t(3))
    assert not cfg.in_quiet_hours({"quiet_from": "10:00", "quiet_to": "10:00", "tz": "UTC"}, _t(10))


def test_is_silent_code_keeps_sound():
    quiet = {"quiet_from": "23:00", "quiet_to": "08:00", "tz": "UTC"}
    now = _t(2)
    assert cfg.is_silent(False, None, quiet, now) is True
    assert cfg.is_silent(False, "123456", quiet, now) is False  # code: with sound
    assert cfg.is_silent(True, "123456", quiet, now) is False
    assert cfg.is_silent(True, None, {"tz": "UTC"}, _t(12)) is True  # newsletter without a code
    assert cfg.is_silent(False, None, {"tz": "UTC"}, _t(12)) is False


def test_digest_due_and_users():
    s = cfg.merge_settings({"tz": "UTC", "digest_time": "09:00", "digest_enabled": True})
    assert cfg.digest_due(s, _t(9, 0)) and not cfg.digest_due(s, _t(9, 1)) and not cfg.digest_due(s, _t(8, 59))
    assert cfg.digest_due(s, _t(9, 1), window_minutes=2)
    assert not cfg.digest_due({**s, "digest_enabled": False}, _t(9, 0))


def test_get_digest_users():
    class Coll(FakeColl):
        def find(self, flt=None, proj=None):
            return _Cursor([d for d in self.docs if d["_id"] in flt["_id"]["$in"]])

    s = st.Storage.__new__(st.Storage)
    s.db = SimpleNamespace(user_settings=Coll([
        {"_id": 1, "tz": "UTC", "digest_time": "09:00", "digest_enabled": True},
        {"_id": 2, "tz": "UTC", "digest_time": "09:00", "digest_enabled": False},
        {"_id": 3, "tz": "Asia/Tokyo", "digest_time": "18:00", "digest_enabled": True},
    ]))
    assert run(s.get_digest_users([1, 2, 3, 4], now=_t(9, 0))) == [1, 3]


# --- storage method filters and pipelines ---

def test_ids_unread_codes_filters():
    a, b = ObjectId(), ObjectId()
    assert st.build_ids_filter(["bad", None], 1) is None
    assert st.build_ids_filter([str(a), str(b), "x"], None) == {"_id": {"$in": [a, b]}}
    assert st.build_ids_filter([str(a)], 5)["user_id"] == 5
    u = st.build_unread_filter(3)
    assert u["read"] is False and u["user_id"] == 3 and "spam" in u["status"]["$nin"]
    assert "user_id" not in st.build_unread_filter(None)
    now = datetime(2025, 6, 10, tzinfo=UTC)
    c = st.build_codes_filter(2, days=7, now=now)
    assert c["_id"]["$gte"] == ObjectId.from_datetime(now - timedelta(days=7)) and c["user_id"] == 2


def test_newsletters_pipeline_and_merge():
    p = st.build_newsletters_pipeline(4)
    assert p[0]["$match"] == {"bulk": True, "status": {"$nin": ["spam", "blocked"]}, "user_id": 4}
    assert "user_id" not in st.build_newsletters_pipeline(None)[0]["$match"]
    o_old = ObjectId.from_datetime(datetime(2025, 1, 1, tzinfo=UTC))
    o_new = ObjectId.from_datetime(datetime(2025, 2, 1, tzinfo=UTC))
    un_old, un_new = {"https": "http://old"}, {"https": "http://new"}
    rows = [
        {"_id": "Shop <News@Shop.io>", "n": 3, "latest": o_old, "unsubscribe": un_old, "unsubscribed_at": None},
        {"_id": "news@shop.io", "n": 2, "latest": o_new, "unsubscribe": un_new, "unsubscribed_at": datetime.now(UTC)},
        {"_id": "Other <o@z.io>", "n": 9, "latest": o_old, "unsubscribe": None, "unsubscribed_at": None},
        {"_id": "", "n": 1, "latest": o_old},
    ]
    rules = [{"pattern": "z.io", "action": "mute", "user_id": None},
             {"pattern": "shop.io", "action": "mute", "user_id": 99}]
    out = st.merge_newsletter_rows(rows, rules, 4)
    assert [i["sender"] for i in out] == ["o@z.io", "news@shop.io"]
    shop = out[1]
    assert shop["count"] == 5 and shop["unsubscribe"] == un_new and shop["unsubscribed"] is True
    assert shop["latest_mail_id"] == str(o_new) and shop["last"] == o_new.generation_time
    assert shop["name"] == "Shop" and shop["domain"] == "shop.io"
    assert out[0]["muted"] is True and shop["muted"] is False  # someone else's rule does not count
    assert len(st.merge_newsletter_rows(rows, rules, 4, limit=1)) == 1


def test_merge_contacts():
    sent = [{"_id": "bob@x.io", "n": 2}, {"_id": "amy@y.io", "n": 1}]
    mails = [{"_id": "Bob <BOB@x.io>", "n": 1}, {"_id": "Carl <carl@x.io>", "n": 5}, {"_id": "", "n": 1}]
    assert st.merge_contacts(sent, mails, "", 10) == ["carl@x.io", "bob@x.io", "amy@y.io"]
    assert st.merge_contacts(sent, mails, "X.IO", 10) == ["carl@x.io", "bob@x.io"]
    assert st.merge_contacts(sent, mails, "", 1) == ["carl@x.io"]


def test_delete_mails_returns_deliveries_and_ids():
    m1, m2 = ObjectId(), ObjectId()

    class Mails(FakeColl):
        def find(self, flt=None, proj=None):
            return _Cursor([d for d in self.docs if _match(d, flt)])

        async def delete_many(self, flt):
            return await super().delete_many(flt)

    mails = Mails([{"_id": m1, "user_id": 1, "message_id": "<a>"}, {"_id": m2, "user_id": 2}])
    deliveries = FakeColl([{"mail_id": str(m1), "user_id": 1, "tg_message_id": 77},
                           {"mail_id": str(m2), "user_id": 2, "tg_message_id": 88}])
    s = st.Storage.__new__(st.Storage)
    s.db = SimpleNamespace(mails=mails, deliveries=deliveries)
    res = run(s.delete_mails([str(m1), str(m2)], 1))  # someone else's mail m2 is left alone
    assert res == {"deleted": 1, "deliveries": [{"user_id": 1, "tg_message_id": 77}], "mail_ids": [str(m1)]}
    assert [d["_id"] for d in mails.docs] == [m2]
    assert run(s.delete_mails(["bad"], None)) == {"deleted": 0, "deliveries": [], "mail_ids": []}


# --- blob write limits, dedup, masking, digest ---

@pytest.fixture
def blobs2(blobs, monkeypatch):
    blobstore.reset_counter()
    monkeypatch.setattr(blobstore, "_free_bytes", lambda: 10 ** 12)
    monkeypatch.setenv("BLOB_MIN_FREE_MB", "500")
    yield blobs
    blobstore.reset_counter()


def _save(mail, data=b"x" * 100, kind="att"):
    return blobstore.save_blob(data, mail_id=mail, user_id=1, kind=kind, name="f", mime="a/b")


def test_save_blob_total_cap_rejects(blobs2, monkeypatch):
    monkeypatch.setenv("BLOB_MAX_MB", "1")
    monkeypatch.setenv("BLOB_ATTACH_MAX_MB", "5")
    monkeypatch.setenv("BLOB_PER_MAIL_MB", "50")

    keep = ObjectId()
    blobs2.m.docs = [{"_id": keep, "starred": True}]

    async def go():
        await _save(str(keep), b"a" * 700 * 1024)
        # does not fit, starred is protected -> rejected
        with pytest.raises(blobstore.BlobRejected):
            await _save("m2", b"b" * 700 * 1024)
        assert len(blobs2.b.docs) == 1

    run(go())


def test_save_blob_cap_evicts_old_first(blobs2, monkeypatch):
    monkeypatch.setenv("BLOB_MAX_MB", "1")
    monkeypatch.setenv("BLOB_PER_MAIL_MB", "50")
    old = ObjectId()
    blobs2.m.docs = [{"_id": old}]

    async def go():
        first = await _save(str(old), b"a" * 700 * 1024)
        await _save("m2", b"b" * 700 * 1024)  # evicts old (non-starred) and writes
        assert first not in {d["_id"] for d in blobs2.b.docs}
        assert not os.path.exists(blobstore.blob_path(first))

    run(go())
    assert len(blobs2.b.docs) == 1


def test_save_blob_low_disk(blobs2, monkeypatch):
    monkeypatch.setattr(blobstore, "_free_bytes", lambda: 400 * 1024 * 1024)
    with pytest.raises(blobstore.BlobRejected):
        run(_save("m"))
    assert blobs2.b.docs == []


def test_save_blob_per_mail_limit(blobs2, monkeypatch):
    monkeypatch.setenv("BLOB_PER_MAIL_MB", "1")
    monkeypatch.setenv("BLOB_ATTACH_MAX_MB", "5")

    async def go():
        await _save("m", b"a" * 600 * 1024)
        with pytest.raises(blobstore.BlobRejected):
            await _save("m", b"b" * 600 * 1024)
        await _save("other", b"b" * 600 * 1024)  # another mail: allowed

    run(go())


def test_save_blob_dedup_hardlink_and_independent_delete(blobs2):
    async def go():
        a = await _save("m1", b"same-content")
        b = await _save("m2", b"same-content")
        pa, pb = blobstore.blob_path(a), blobstore.blob_path(b)
        assert os.path.samefile(pa, pb) and a != b
        await blobstore.delete_blobs_for_mails(["m1"])
        assert not os.path.exists(pa) and open(pb, "rb").read() == b"same-content"
        assert blobstore._total == len(b"same-content")

    run(go())


def test_counter_tracks_delete(blobs2):
    async def go():
        await _save("m", b"x" * 50)
        await _save("m", b"y" * 70)
        assert blobstore._total == 120
        await blobstore.delete_blobs_for_mails(["m"])
        assert blobstore._total == 0

    run(go())


def test_due_mask_filter_and_legacy_marker():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    f = st.build_due_mask_filter(now)
    assert f["mask_at"] == {"$lte": now} and f["otp_masked"] == {"$ne": True}


def test_mask_once_uses_mask_at_and_marks_disabled(monkeypatch):
    from src.app.bot import otp_mask

    now = datetime(2025, 1, 1, 12, tzinfo=UTC)
    edited, marked = [], []

    class FakeDb:
        async def get_mails_due_for_mask(self, n, limit=100):
            assert n == now
            return [{"_id": 1, "user_id": 10, "mask_at": now, "otp": "1"},
                    {"_id": 2, "user_id": 20, "mask_at": now, "otp": "2"}]

        async def get_mails_for_otp_mask(self, older_than, newer_than, limit=100):
            return [
                {"_id": 3, "user_id": 10, "timestamp": now - timedelta(minutes=5), "otp": "3"},   # not due yet
                {"_id": 4, "user_id": 10, "timestamp": now - timedelta(minutes=30), "otp": "4"},  # due
                {"_id": 5, "user_id": 20, "timestamp": now - timedelta(minutes=30), "otp": "5"},  # disabled
            ]

        async def get_settings(self, uid):
            return {"otp_hide_minutes": 15 if uid == 10 else 0}

        async def get_deliveries(self, oid):
            return [{"user_id": 1, "tg_message_id": 1}]

        async def set_otp_masked(self, oid):
            marked.append(oid)

    async def fake_mask(d, mail, oid):
        edited.append(oid)
        return True

    monkeypatch.setattr(otp_mask.storage, "db", FakeDb())
    monkeypatch.setattr(otp_mask, "_mask_delivery", fake_mask)
    assert run(otp_mask.mask_once(now)) == 2
    assert edited == ["1", "4"]
    assert sorted(marked) == ["1", "2", "4", "5"]


def test_digest_tick_per_user(monkeypatch):
    from src.app import monitor

    now = datetime(2025, 3, 10, 9, 0, 5, tzinfo=UTC)
    sent, asked = [], []

    class FakeDb:
        async def get_digest_users(self, ids, now=None, window_minutes=1):
            return [1, 2]

        async def get_counts_since(self, since, user_id=None):
            asked.append(user_id)
            return {"delivered": 3} if user_id else {}

    class FakeBot:
        async def send_message(self, uid, text, parse_mode=None):
            if uid == 2:
                raise RuntimeError("blocked")
            sent.append((uid, text))

    monkeypatch.setattr(monitor.storage, "db", FakeDb())
    monkeypatch.setattr(monitor, "bot", FakeBot())
    monkeypatch.setattr(monitor, "scope", lambda uid: None if uid == 1 else uid)
    last = {}
    assert run(monitor.digest_tick(now, last)) == [1]
    assert asked == [None, 2]
    assert sent[0][1].startswith("📮")  # "no mails" digest message
    # repeat within the same minute: no duplicate
    assert run(monitor.digest_tick(now + timedelta(seconds=20), last)) == []
