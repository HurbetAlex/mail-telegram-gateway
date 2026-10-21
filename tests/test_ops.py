import asyncio
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bson import ObjectId

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_ID", "1")

from src.app import monitor
from src.app.db.storage import Storage, cutoff_oid


def test_cutoff_oid():
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    oid = cutoff_oid(180, now)
    assert oid.generation_time == (now - timedelta(days=180))
    assert ObjectId.from_datetime(now - timedelta(days=181)) < oid
    assert ObjectId.from_datetime(now - timedelta(days=179)) > oid


def test_parse_meminfo_and_problems():
    text = "MemTotal: 1000000 kB\nMemAvailable: 51200 kB\nSwapTotal: 1000 kB\nSwapFree: 100 kB\n"
    info = monitor.parse_meminfo(text)
    assert info["MemAvailable"] == 51200
    probs = monitor.memory_problems(info)
    assert len(probs) == 2  # 50 MB < 100 and swap 90% > 85
    ok = monitor.parse_meminfo("MemAvailable: 512000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    assert monitor.memory_problems(ok) == []


def test_next_run_daily_and_weekly():
    tz = ZoneInfo("Europe/Kyiv")
    now = datetime(2026, 10, 19, 10, 0, tzinfo=tz)  # Monday
    assert monitor.next_run(now, 4, 30) == datetime(2026, 10, 20, 4, 30, tzinfo=tz)
    assert monitor.next_run(now, 12, 0) == datetime(2026, 10, 19, 12, 0, tzinfo=tz)
    sun = monitor.next_run(now, 5, 0, weekday=6)
    assert sun == datetime(2026, 10, 25, 5, 0, tzinfo=tz) and sun.weekday() == 6
    # exactly at the run moment: next week
    at = datetime(2026, 10, 25, 5, 0, tzinfo=tz)
    assert monitor.next_run(at, 5, 0, weekday=6) == at + timedelta(days=7)


def test_latest_backup_and_limit(tmp_path):
    assert monitor.latest_backup(str(tmp_path)) is None
    old = tmp_path / "mail_2026-10-01.gz"
    new = tmp_path / "mail_2026-10-02.gz"
    old.write_bytes(b"a")
    new.write_bytes(b"b")
    os.utime(old, (1, 1))
    assert monitor.latest_backup(str(tmp_path)) == str(new)
    assert monitor.backup_fits(49 * 1024 * 1024)
    assert not monitor.backup_fits(49 * 1024 * 1024 + 1)


def test_diff_alerts_transitions():
    active = set()
    new, rec, active = monitor.diff_alerts(active, {"disk": "d"})
    assert new == {"disk": "d"} and rec == []
    new, rec, active = monitor.diff_alerts(active, {"disk": "d"})  # no repeat
    assert new == {} and rec == []
    new, rec, active = monitor.diff_alerts(active, {"disk": "d", "mem": "m"})
    assert new == {"mem": "m"} and rec == []
    new, rec, active = monitor.diff_alerts(active, {"mem": "m"})
    assert new == {} and rec == ["disk"]
    new, rec, active = monitor.diff_alerts(active, {})
    assert rec == ["mem"] and active == set()


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self.docs)


class _Res:
    def __init__(self, n):
        self.deleted_count = n


class FakeMails:
    def __init__(self, docs):
        self.docs = docs
        self.find_filters = []
        self.delete_sizes = []

    def _match(self, flt):
        lt = flt["_id"]["$lt"]
        out = []
        for d in self.docs:
            if not d["_id"] < lt:
                continue
            cond = flt.get("starred")
            if isinstance(cond, dict) and "$ne" in cond and d.get("starred") is True:
                continue
            if cond is True and d.get("starred") is not True:
                continue
            out.append(d)
        return out

    def find(self, flt, proj=None):
        self.find_filters.append(flt)
        if "_id" not in flt:  # look up the remaining mails by message_id
            wanted = set(flt["message_id"]["$in"])
            return _Cursor([d for d in self.docs if d.get("message_id") in wanted])
        return _Cursor(
            [{"_id": d["_id"], "message_id": d.get("message_id")} for d in self._match(flt)]
        )

    async def delete_many(self, flt):
        ids = set(flt["_id"]["$in"])
        self.delete_sizes.append(len(ids))
        before = len(self.docs)
        self.docs = [d for d in self.docs if d["_id"] not in ids]
        return _Res(before - len(self.docs))

    async def count_documents(self, flt):
        return len(self._match(flt))


class FakeDeliveries:
    def __init__(self):
        self.removed = []
        self.docs = []

    async def delete_many(self, flt):
        ids = set(flt["mail_id"]["$in"])
        self.removed.extend(ids)
        self.docs = [d for d in self.docs if d["mail_id"] not in ids]
        return _Res(0)


def test_cleanup_old_mails_batches_and_starred():
    now = datetime.now(timezone.utc)
    old = lambda i: ObjectId.from_datetime(now - timedelta(days=200, seconds=i))
    docs = [{"_id": old(i)} for i in range(1200)]
    docs += [{"_id": old(5000 + i), "starred": True} for i in range(3)]
    docs += [{"_id": ObjectId.from_datetime(now - timedelta(days=10))}]
    st = Storage.__new__(Storage)
    st.db = type("DB", (), {})()
    st.db.mails = FakeMails(docs)
    st.db.deliveries = FakeDeliveries()

    res = asyncio.run(st.cleanup_old_mails(180))

    assert res == {"deleted": 1200, "kept_starred": 3}
    assert st.db.mails.delete_sizes == [500, 500, 200]
    assert len(st.db.deliveries.removed) == 1200
    assert len(st.db.mails.docs) == 4  # 3 starred + a fresh one
    assert st.db.mails.find_filters[0]["starred"] == {"$ne": True}


def test_cleanup_deletes_both_delivery_formats_and_shared_message_id():
    now = datetime.now(timezone.utc)
    o = lambda days: ObjectId.from_datetime(now - timedelta(days=days))
    a, b, c, d = o(300), o(290), o(280), o(5)
    docs = [
        {"_id": a, "message_id": "<only-old>"},
        {"_id": b, "message_id": "<shared>"},  # gets deleted
        {"_id": c, "message_id": "<shared>", "starred": True},  # stays
        {"_id": d},  # fresh, no message_id
    ]
    st = Storage.__new__(Storage)
    st.db = type("DB", (), {})()
    st.db.mails = FakeMails(docs)
    st.db.deliveries = FakeDeliveries()
    st.db.deliveries.docs = [
        {"mail_id": str(a)},  # new format
        {"mail_id": "<only-old>"},  # old format, no mails left
        {"mail_id": "<shared>"},  # old format, but a starred mail remains
        {"mail_id": str(b)},
        {"mail_id": str(c)},
        {"mail_id": str(d)},
    ]

    res = asyncio.run(st.cleanup_old_mails(180))

    assert res == {"deleted": 2, "kept_starred": 1}
    left = {x["mail_id"] for x in st.db.deliveries.docs}
    assert left == {"<shared>", str(c), str(d)}


# --- Grace period before the first deletion and get_expiring ---


class _MailsX:
    """Fake mails collection: find/sort/skip/limit, count_documents, aggregate."""

    def __init__(self, docs):
        self.docs = docs

    def _match(self, flt):
        out = []
        for d in self.docs:
            if not d["_id"] < flt["_id"]["$lt"]:
                continue
            if d.get("starred") is True:
                continue
            if "user_id" in flt and d.get("user_id") != flt["user_id"]:
                continue
            out.append(d)
        return out

    async def count_documents(self, flt):
        return len(self._match(flt))

    def find(self, flt):
        self._cur = sorted(self._match(flt), key=lambda d: d["_id"])
        return self

    def sort(self, key, direction):
        self._cur.sort(key=lambda d: d[key], reverse=direction < 0)
        return self

    def skip(self, n):
        self._cur = self._cur[n:]
        return self

    def limit(self, n):
        self._cur = self._cur[:n]
        return self

    async def to_list(self, length=None):
        return list(self._cur)

    async def aggregate(self, pipeline):
        groups = {}
        for d in self._match(pipeline[0]["$match"]):
            groups.setdefault(d.get("user_id"), []).append(d["_id"])
        for uid, ids in groups.items():
            yield {"_id": uid, "n": len(ids), "oldest": min(ids), "newest": max(ids)}


def _oid(days_ago, extra=0):
    return ObjectId.from_datetime(
        datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=extra)
    )


def _storage_with(docs):
    st = Storage.__new__(Storage)
    st.db = type("DB", (), {})()
    st.db.mails = _MailsX(docs)
    return st


def test_get_expiring_filters_sorts_and_totals():
    a, b, c = _oid(200), _oid(175), _oid(176)
    docs = [
        {"_id": a, "user_id": 1},
        {"_id": b, "user_id": 2},
        {"_id": c, "user_id": 1},
        {"_id": _oid(100), "user_id": 1},  # newer than the boundary
        {"_id": _oid(190), "user_id": 1, "starred": True},  # starred
        {"_id": _oid(172), "user_id": 1},  # younger than the 173-day boundary
    ]
    st = _storage_with(docs)
    mails, total = asyncio.run(st.get_expiring(180))
    assert total == 3
    assert [m["_id"] for m in mails] == sorted(m["_id"] for m in mails)
    ids = {m["_id"] for m in mails}
    assert a in ids and b in ids and c in ids
    assert all(not m.get("starred") for m in mails)
    mails1, total1 = asyncio.run(st.get_expiring(180, user_id=1))
    assert {m["_id"] for m in mails1} == {a, c} and total1 == 2
    page, total2 = asyncio.run(st.get_expiring(180, skip=1, limit=1))
    assert len(page) == 1 and total2 == total
    assert asyncio.run(st.get_expiring(0)) == ([], 0)
    assert asyncio.run(st.get_expiring(-5)) == ([], 0)


class _Meta:
    def __init__(self):
        self.data = {}

    async def get_meta(self, key):
        v = self.data.get(key)
        return dict(v) if v else None

    async def set_meta(self, key, value):
        self.data.setdefault(key, {}).update(value)


class _FakeBot:
    def __init__(self, fail=()):
        self.sent = []
        self.fail = set(fail)

    async def send_message(self, uid, text, **kw):
        if uid in self.fail:
            raise RuntimeError("chat not found")
        self.sent.append((uid, text, kw))


def _setup_cleanup(monkeypatch, docs, fail=()):
    st = _storage_with(docs)
    meta = _Meta()
    st.get_meta, st.set_meta = meta.get_meta, meta.set_meta
    deleted = []

    async def fake_cleanup(days):
        deleted.append(days)
        return {"deleted": 5, "kept_starred": 1}

    st.cleanup_old_mails = fake_cleanup
    holder = type("H", (), {"db": st})()
    fb = _FakeBot(fail)
    monkeypatch.setattr(monitor, "storage", holder)
    monkeypatch.setattr(monitor, "bot", fb)
    monkeypatch.setattr(monitor, "ADMIN_ID", 1)
    monkeypatch.setattr(monitor, "MAIL_TTL_DAYS", 180)
    monkeypatch.setattr(monitor, "CLEANUP_GRACE_DAYS", 3)
    return st, meta, fb, deleted


def test_cleanup_grace_flow(monkeypatch):
    docs = [
        {"_id": _oid(200, 1), "user_id": 7},
        {"_id": _oid(190, 2), "user_id": 7},
        {"_id": _oid(185, 3)},  # no user_id -> admin
        {"_id": _oid(10), "user_id": 7},
    ]
    st, meta, fb, deleted = _setup_cleanup(monkeypatch, docs)
    t0 = datetime.now(timezone.utc)

    # 1) first run: warnings, no deletion
    delay = asyncio.run(monitor.cleanup_step(t0))
    assert delay is not None and delay > 0
    assert deleted == []
    assert {u for u, _, _ in fb.sent} == {7, 1}
    texts = {u: t for u, t, _ in fb.sent}
    assert "delete 2 emails" in texts[7] and "delete 1 emails" in texts[1]
    assert all("disable_notification" not in kw for _, _, kw in fb.sent)
    assert meta.data["cleanup_warn"]["warned_at"] == t0

    # 2) repeat before the grace period ends: no deletion, no repeated messages
    n_sent = len(fb.sent)
    delay = asyncio.run(monitor.cleanup_step(t0 + timedelta(days=1)))
    assert delay is not None and deleted == [] and len(fb.sent) == n_sent

    # 3) after the grace period: deletion and first_done
    delay = asyncio.run(monitor.cleanup_step(t0 + timedelta(days=3, minutes=1)))
    assert delay is None and deleted == [180]
    assert meta.data["cleanup_warn"]["first_done"] is True
    assert fb.sent[-1][0] == 1 and "Deleted 5" in fb.sent[-1][1]

    # 4) afterwards no warnings
    sent_before = len(fb.sent)
    assert asyncio.run(monitor.cleanup_step(t0 + timedelta(days=4))) is None
    assert deleted == [180, 180]
    assert all("I will delete" not in t for _, t, _ in fb.sent[sent_before:])


def test_cleanup_no_candidates_marks_first_done(monkeypatch):
    st, meta, fb, deleted = _setup_cleanup(monkeypatch, [{"_id": _oid(5), "user_id": 7}])
    assert asyncio.run(monitor.cleanup_step()) is None
    assert meta.data["cleanup_warn"] == {"first_done": True}
    assert fb.sent == [] and deleted == []


def test_cleanup_warning_survives_telegram_errors(monkeypatch):
    docs = [{"_id": _oid(200), "user_id": 7}, {"_id": _oid(201)}]
    # owner did not press /start: the admin still gets it, warned_at is set
    st, meta, fb, deleted = _setup_cleanup(monkeypatch, docs, fail={7})
    asyncio.run(monitor.cleanup_step())
    assert [u for u, _, _ in fb.sent] == [1] and "warned_at" in meta.data["cleanup_warn"]
    # admin unavailable: warned_at is not set, retry in an hour
    st, meta, fb, deleted = _setup_cleanup(monkeypatch, docs, fail={1})
    assert asyncio.run(monitor.cleanup_step()) == 3600
    assert "cleanup_warn" not in meta.data and deleted == []
