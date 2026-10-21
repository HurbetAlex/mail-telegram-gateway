import asyncio
import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("ADMIN_ID", "1")

from src.app import monitor


class _FakeDb:
    async def count_failed_since(self, since):
        return 3


@pytest.fixture
def status_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "STATUS_DIR", str(tmp_path))
    monkeypatch.setattr(monitor, "WEBAPP_DOMAIN", "")
    monkeypatch.setattr(monitor, "BACKUP_DIR", str(tmp_path / "nobackups"))
    monkeypatch.setattr(monitor.storage, "db", _FakeDb())
    (tmp_path / "out").mkdir()
    return tmp_path


def _run():
    return asyncio.run(monitor.server_status())


def test_fresh_status(status_dir):
    data = {
        "updated_at": "2026-10-19T10:00:00+00:00",
        "fail2ban": {"banned": ["1.2.3.4"], "total_failed": 7},
        "postfix_queue": 2,
        "containers": [{"name": "postfix", "state": "running", "status": "Up"}],
    }
    (status_dir / "out" / "status.json").write_text(json.dumps(data))
    res = _run()
    assert res["fail2ban"]["banned"] == ["1.2.3.4"]
    assert res["postfix_queue"] == 2
    assert res["containers"][0]["name"] == "postfix"
    assert res["status_updated_at"] == "2026-10-19T10:00:00+00:00"
    assert res["failed_24h"] == 3
    assert res["disk"]["pct"] >= 0 and res["last_backup"] is None


def test_stale_status_still_returned(status_dir):
    (status_dir / "out" / "status.json").write_text(
        json.dumps({"updated_at": "2020-01-01T00:00:00+00:00", "postfix_queue": 0})
    )
    res = _run()
    assert res["postfix_queue"] == 0  # 0 is not turned into None
    assert res["status_updated_at"] == "2020-01-01T00:00:00+00:00"


@pytest.mark.parametrize("content", ["{broken", "[1, 2]", ""])
def test_broken_status(status_dir, content):
    (status_dir / "out" / "status.json").write_text(content)
    res = _run()
    assert res["fail2ban"] is None and res["containers"] is None
    assert res["status_updated_at"] is None


def test_missing_status(status_dir):
    res = _run()
    assert res["postfix_queue"] is None and res["status_updated_at"] is None


def test_last_backup(status_dir, monkeypatch):
    bdir = status_dir / "b"
    bdir.mkdir()
    (bdir / "mail_2026-10-18.gz").write_bytes(b"x" * 2048)
    monkeypatch.setattr(monitor, "BACKUP_DIR", str(bdir))
    res = _run()
    assert res["last_backup"]["file"] == "mail_2026-10-18.gz"


@pytest.mark.parametrize(
    "ip,fname",
    [
        ("1.2.3.4", "unban-1.2.3.4"),
        (" 5.6.7.8 ", "unban-5.6.7.8"),
        ("2001:db8::1", "unban-2001_db8__1"),
        ("2001:0db8:0:0:0:0:0:1", "unban-2001_db8__1"),
    ],
)
def test_request_unban_valid(status_dir, ip, fname):
    monitor.request_unban(ip)
    assert (status_dir / "requests" / fname).is_file()


@pytest.mark.parametrize(
    "ip", ["", "999.1.1.1", "1.2.3.4;reboot", "../x", "1.2.3.4/24", "abc", "-h"]
)
def test_request_unban_invalid(status_dir, ip):
    with pytest.raises(ValueError):
        monitor.request_unban(ip)
    assert not (status_dir / "requests").exists() or not list(
        (status_dir / "requests").iterdir()
    )
