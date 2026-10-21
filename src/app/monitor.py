import asyncio
import glob
import ipaddress
import json
import logging
import os
import shutil
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import aiosmtplib
from aiogram.types import FSInputFile

from src.app.bot.auth import ADMIN_ID, ALLOWED_IDS, scope
from src.app.bot.sender import bot
from src.app.db import storage

logger = logging.getLogger(__name__)

DIGEST_TIME = os.getenv("DIGEST_TIME", "09:00")  # empty = disabled
DIGEST_TZ = os.getenv("DIGEST_TZ", "Europe/Kyiv")
HEALTH_CHECK_MINUTES = int(os.getenv("HEALTH_CHECK_MINUTES", "5"))
SMTP_HOST = os.getenv("SMTP_HOST", "postfix")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))

MAIL_TTL_DAYS = int(os.getenv("MAIL_TTL_DAYS", "180"))  # 0 = cleanup disabled
CLEANUP_TIME = os.getenv("CLEANUP_TIME", "04:30")
CLEANUP_FIRST_RUN_SEC = 300
CLEANUP_GRACE_DAYS = int(os.getenv("CLEANUP_GRACE_DAYS", "3"))  # grace period before the first deletion
DISK_ALERT_PCT = int(os.getenv("DISK_ALERT_PCT", "90"))
MEM_ALERT_MB = int(os.getenv("MEM_ALERT_MB", "100"))
SWAP_ALERT_PCT = int(os.getenv("SWAP_ALERT_PCT", "85"))
CERT_ALERT_DAYS = int(os.getenv("CERT_ALERT_DAYS", "14"))
CERT_CHECK_SEC = 6 * 3600
WEBAPP_DOMAIN = os.getenv("WEBAPP_DOMAIN", "")
WEB_HOST = os.getenv("WEB_HOST", "web")
BACKUP_DIR = os.getenv("BACKUP_DIR", "/backups")
BACKUP_TG_WEEKDAY = int(os.getenv("BACKUP_TG_WEEKDAY", "6"))  # 0=Mon ... 6=Sun
BACKUP_TG_TIME = os.getenv("BACKUP_TG_TIME", "05:00")  # empty = disabled
BACKUP_TG_MAX_BYTES = 49 * 1024 * 1024  # Bot API limit is 50 MB, keep a margin

STATUS_LABELS = [
    ("delivered", "✅ delivered"),
    ("sent", "📤 sent"),
    ("spam", "🚫 spam"),
    ("muted", "🔕 muted"),
    ("unassigned", "📥 unrouted"),
    ("failed", "⚠️ errors"),
]


async def send_digest(uid: int, since: datetime) -> None:
    """One user's digest: the admin gets all mail, everyone else only their own."""
    counts = await storage.db.get_counts_since(since, user_id=scope(uid))
    total = sum(counts.values())
    if total == 0:
        text = "📮 No emails in the last 24 hours."
    else:
        parts = [
            f"{label}: {counts[key]}"
            for key, label in STATUS_LABELS
            if counts.get(key)
        ]
        text = f"📮 <b>Last 24 hours: {total}</b>\n" + "\n".join(parts)
    await bot.send_message(uid, text, parse_mode="HTML")


async def digest_tick(now: datetime, last_sent: Dict[int, object]) -> List[int]:
    """One tick: who is due for a digest (per-user settings), with protection against duplicates
    within a day. Returns the ids the digest was sent to."""
    sent: List[int] = []
    due = await storage.db.get_digest_users(ALLOWED_IDS, now=now, window_minutes=1)
    for uid in due:
        # last_sent[uid] is the minute of sending; a repeated tick in the same minute is skipped
        marker = now.replace(second=0, microsecond=0)
        if last_sent.get(uid) == marker:
            continue
        last_sent[uid] = marker
        try:
            await send_digest(uid, now - timedelta(days=1))
            sent.append(uid)
        except Exception as e:
            logger.error(f"Digest to {uid} failed: {e}")
    return sent


async def digest_loop():
    """Checks once a minute who is due: time, timezone and flag come from the user settings."""
    last_sent: Dict[int, object] = {}
    while True:
        try:
            await digest_tick(datetime.now(timezone.utc), last_sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Digest failed")
        await asyncio.sleep(60)


# --- Pure functions (covered by tests/test_ops.py) ---


def parse_meminfo(text: str) -> Dict[str, int]:
    """/proc/meminfo -> {key: KB}."""
    res: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            res[key.strip()] = int(parts[0])
    return res


def memory_problems(info: Dict[str, int]) -> List[str]:
    out = []
    avail_mb = info.get("MemAvailable", 0) // 1024
    if "MemAvailable" in info and avail_mb < MEM_ALERT_MB:
        out.append(f"Low memory: {avail_mb} MB available (threshold {MEM_ALERT_MB})")
    swap_total = info.get("SwapTotal", 0)
    if swap_total > 0:
        used_pct = (swap_total - info.get("SwapFree", 0)) * 100 // swap_total
        if used_pct > SWAP_ALERT_PCT:
            out.append(f"Swap is {used_pct}% used (threshold {SWAP_ALERT_PCT}%)")
    return out


def next_run(
    now: datetime, hh: int, mm: int, weekday: Optional[int] = None
) -> datetime:
    """Next run strictly after now (now is tz-aware): daily at hh:mm
    or on the given weekday (0=Mon)."""
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    while target <= now or (weekday is not None and target.weekday() != weekday):
        target += timedelta(days=1)
    return target


def latest_backup(directory: str) -> Optional[str]:
    """The newest mail_*.gz (the name contains a date, but we go by mtime)."""
    files = glob.glob(os.path.join(directory, "mail_*.gz"))
    return max(files, key=os.path.getmtime) if files else None


def backup_fits(size_bytes: int) -> bool:
    return size_bytes <= BACKUP_TG_MAX_BYTES


def diff_alerts(
    active: Set[str], problems: Dict[str, str]
) -> Tuple[Dict[str, str], List[str], Set[str]]:
    """State transitions: (new problems, recovered keys, new active).
    The same problem is not reported twice."""
    new = {k: v for k, v in problems.items() if k not in active}
    recovered = sorted(k for k in active if k not in problems)
    return new, recovered, set(problems)


ALERT_TITLES = {
    "postfix": "Postfix",
    "mongo": "MongoDB",
    "disk": "Disk",
    "mem": "Memory",
    "cert": "Certificate",
    "failed": "Delivery",
}


# --- Checks ---


def _check_cert_sync(host: str, domain: str) -> Optional[str]:
    """None = certificate is fine, otherwise the problem text."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        left = (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400
    except Exception as e:
        return f"Certificate {domain}: check failed: {e}"
    if left < CERT_ALERT_DAYS:
        return f"Certificate {domain} expires in {int(left)} days"
    return None


async def _smtp_probe() -> Optional[str]:
    try:
        # Without STARTTLS and with a proper QUIT: otherwise postfix logs
        # "lost connection after STARTTLS" on every check.
        smtp = aiosmtplib.SMTP(
            hostname=SMTP_HOST, port=SMTP_PORT, timeout=10, start_tls=False
        )
        await smtp.connect()
        await smtp.quit()
    except Exception as e:
        return f"Postfix ({SMTP_HOST}:{SMTP_PORT}): {e}"
    return None


_cert_state = {"at": 0.0, "problem": None}


async def _check_health() -> Dict[str, str]:
    """Returns {check key: problem text}; empty = everything is fine."""
    problems: Dict[str, str] = {}
    p = await _smtp_probe()
    if p:
        problems["postfix"] = p
    try:
        await storage.db.client.admin.command("ping")
    except Exception as e:
        problems["mongo"] = f"MongoDB: {e}"

    try:
        du = shutil.disk_usage("/")
        pct = du.used * 100 // du.total
        if pct >= DISK_ALERT_PCT:
            problems["disk"] = (
                f"Disk is {pct}% full ({du.free // 2**20} MB free, threshold {DISK_ALERT_PCT}%)"
            )
    except Exception as e:
        logger.warning(f"Disk check failed: {e}")

    try:
        with open("/proc/meminfo") as f:
            mem = memory_problems(parse_meminfo(f.read()))
        if mem:
            problems["mem"] = "; ".join(mem)
    except Exception as e:
        logger.warning(f"Memory check failed: {e}")

    if WEBAPP_DOMAIN:
        if time.monotonic() - _cert_state["at"] >= CERT_CHECK_SEC or not _cert_state["at"]:
            _cert_state["problem"] = await asyncio.to_thread(
                _check_cert_sync, WEB_HOST, WEBAPP_DOMAIN
            )
            _cert_state["at"] = time.monotonic()
        if _cert_state["problem"]:
            problems["cert"] = _cert_state["problem"]

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        n = await storage.db.count_failed_since(since)
        if n > 0:
            problems["failed"] = f"Emails with status failed in the last hour: {n}"
    except Exception as e:
        logger.warning(f"Failed-mails check failed: {e}")
    return problems


async def health_loop():
    """Alerts to the admin: on entering a bad state and on recovery
    (per check, without repeats). Also starts the cleanup and
    Telegram backup loops."""
    extra = [
        asyncio.create_task(cleanup_loop()),
        asyncio.create_task(backup_tg_loop()),
    ]
    try:
        await _health_loop()
    finally:
        for t in extra:
            t.cancel()


async def _health_loop():
    if HEALTH_CHECK_MINUTES <= 0 or not ADMIN_ID:
        await asyncio.Event().wait()  # do not exit: otherwise finally would cancel the cleanup loop
    active: Set[str] = set()
    while True:
        await asyncio.sleep(HEALTH_CHECK_MINUTES * 60)
        try:
            problems = await _check_health()
        except Exception:
            logger.exception("Health check crashed")
            continue

        new, recovered, active = diff_alerts(active, problems)
        if new:
            text = "⚠️ <b>Server problems:</b>\n" + "\n".join(
                f"· {p}" for p in new.values()
            )
            try:
                await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Health alert failed: {e}")
        if recovered:
            names = ", ".join(ALERT_TITLES.get(k, k) for k in recovered)
            suffix = "All services are back to normal." if not active else "The rest is unchanged."
            try:
                await bot.send_message(ADMIN_ID, f"✅ Back to normal: {names}. {suffix}")
            except Exception as e:
                logger.error(f"Health recovery alert failed: {e}")


# --- Old mail cleanup ---


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _warn_text(n: int, oldest: datetime, newest: datetime) -> str:
    return (
        f"🗑 In {CLEANUP_GRACE_DAYS} days I will delete {n} emails older than {MAIL_TTL_DAYS} days "
        f"(from {oldest:%d.%m.%Y} to {newest:%d.%m.%Y}). "
        "Star the ones you need: the Mini App has a \"🗑 Expiring soon\" filter, "
        "or use the ⭐ button under the email. Starred emails are never deleted."
    )


async def _send_cleanup_warnings(groups: Dict[Optional[int], Dict]) -> bool:
    """Warns the owners (the admin for mails without user_id). True if the admin received
    a message; errors for individual recipients are only logged."""
    by_user: Dict[int, Dict] = {}
    for uid, g in groups.items():
        key = uid if uid else ADMIN_ID
        cur = by_user.get(key)
        if cur:
            cur["n"] += g["n"]
            cur["oldest"] = min(cur["oldest"], g["oldest"])
            cur["newest"] = max(cur["newest"], g["newest"])
        else:
            by_user[key] = dict(g)
    admin_ok = not ADMIN_ID
    for uid, g in by_user.items():
        try:
            await bot.send_message(uid, _warn_text(g["n"], g["oldest"], g["newest"]))
            if uid == ADMIN_ID:
                admin_ok = True
        except Exception as e:
            logger.error(f"Cleanup warning to {uid} failed: {e}")
    if ADMIN_ID and ADMIN_ID not in by_user:
        # The admin has no candidates of their own: send them the overall summary
        total = sum(g["n"] for g in groups.values())
        oldest = min(g["oldest"] for g in groups.values())
        newest = max(g["newest"] for g in groups.values())
        try:
            await bot.send_message(ADMIN_ID, _warn_text(total, oldest, newest))
            admin_ok = True
        except Exception as e:
            logger.error(f"Cleanup warning to admin failed: {e}")
    return admin_ok


async def _delete_and_report() -> None:
    res = await storage.db.cleanup_old_mails(MAIL_TTL_DAYS)
    logger.info(f"Cleanup: {res}")
    if res["deleted"] > 0 and ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"🧹 Deleted {res['deleted']} emails older than {MAIL_TTL_DAYS} days. "
            f"Starred emails kept: {res['kept_starred']}",
            disable_notification=True,
        )


async def cleanup_step(now: Optional[datetime] = None) -> Optional[float]:
    """One cleanup run. Returns the delay (sec) until the next check
    or None, in which case the next run follows the CLEANUP_TIME schedule.

    The first deletion is postponed: first a warning to the owners and
    CLEANUP_GRACE_DAYS days to star what they need."""
    now = now or datetime.now(timezone.utc)
    state = await storage.db.get_meta("cleanup_warn") or {}
    if state.get("first_done"):
        await _delete_and_report()
        return None

    groups = await storage.db.cleanup_candidates_by_user(MAIL_TTL_DAYS)
    if not groups:
        await storage.db.set_meta("cleanup_warn", {"first_done": True})
        logger.info("Cleanup: no candidates, first run done")
        return None

    grace = timedelta(days=CLEANUP_GRACE_DAYS)
    warned_at = state.get("warned_at")
    if warned_at is None:
        if await _send_cleanup_warnings(groups):
            await storage.db.set_meta("cleanup_warn", {"warned_at": now})
            logger.info(f"Cleanup: warned about {sum(g['n'] for g in groups.values())} mails")
            return max(grace.total_seconds(), 60) + 5
        logger.error("Cleanup: admin warning not delivered, will retry")
        return 3600

    deadline = _aware(warned_at) + grace
    if now < deadline:
        left = (deadline - now).total_seconds()
        logger.info(f"Cleanup: grace period, {left / 3600:.1f} h left")
        return min(left + 5, 3600)

    await _delete_and_report()
    await storage.db.set_meta("cleanup_warn", {"first_done": True})
    return None


async def cleanup_loop():
    """First run 5 minutes after startup, then daily at CLEANUP_TIME
    (until the first deletion, see cleanup_step)."""
    if MAIL_TTL_DAYS <= 0:
        return
    try:
        hh, mm = (int(x) for x in CLEANUP_TIME.split(":"))
        tz = ZoneInfo(DIGEST_TZ)
    except (ValueError, KeyError) as e:
        logger.error(f"Cleanup disabled, bad config: {e}")
        return

    delay: float = CLEANUP_FIRST_RUN_SEC
    while True:
        await asyncio.sleep(delay)
        try:
            override = await cleanup_step()
        except Exception:
            logger.exception("Cleanup failed")
            override = None
        if override is not None:
            delay = override
        else:
            now = datetime.now(tz)
            delay = (next_run(now, hh, mm) - now).total_seconds()


# --- Weekly Telegram backup ---


async def _send_backup_once():
    path = latest_backup(BACKUP_DIR)
    if not path:
        await bot.send_message(
            ADMIN_ID, f"⚠️ Backup not sent: no mail_*.gz files in {BACKUP_DIR}"
        )
        return
    size = os.path.getsize(path)
    date = os.path.basename(path)[len("mail_"):-len(".gz")]
    mb = size / 1024 / 1024
    if not backup_fits(size):
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Backup {date} ({mb:.1f} MB) exceeds the Telegram limit and was not sent. "
            f"Fetch it manually from ./backups.",
        )
        return
    await bot.send_document(
        ADMIN_ID,
        FSInputFile(path),
        caption=f"🗄 Database backup {date}, {mb:.1f} MB",
        disable_notification=True,
    )


async def backup_tg_loop():
    if not BACKUP_TG_TIME or not ADMIN_ID:
        return
    try:
        hh, mm = (int(x) for x in BACKUP_TG_TIME.split(":"))
        tz = ZoneInfo(DIGEST_TZ)
        if not 0 <= BACKUP_TG_WEEKDAY <= 6:
            raise ValueError("BACKUP_TG_WEEKDAY must be 0..6")
    except (ValueError, KeyError) as e:
        logger.error(f"Telegram backup disabled, bad config: {e}")
        return

    while True:
        now = datetime.now(tz)
        target = next_run(now, hh, mm, BACKUP_TG_WEEKDAY)
        await asyncio.sleep((target - now).total_seconds())
        try:
            # Protection against resending after a restart within the same window
            last = await storage.db.get_meta("tg_backup")
            last_dt = last.get("sent_at") if isinstance(last, dict) else None
            if last_dt is not None:
                last_dt = last_dt.replace(tzinfo=last_dt.tzinfo or timezone.utc)
                if abs((datetime.now(timezone.utc) - last_dt).total_seconds()) < 3600:
                    continue
            await _send_backup_once()
            await storage.db.set_meta(
                "tg_backup", {"sent_at": datetime.now(timezone.utc)}
            )
        except Exception:
            logger.exception("Telegram backup failed")


# --- Server status for the Mini App (admin) ---

STATUS_DIR = os.getenv("STATUS_DIR", "/status")
_cert_days_cache = {"at": 0.0, "days": None}


def _cert_days_sync(host: str, domain: str) -> Optional[int]:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        return int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) // 86400)
    except Exception as e:
        logger.warning(f"Cert days check failed: {e}")
        return None


def _read_host_status() -> Dict:
    """status.json from the host exporter (scripts/status-exporter.sh); {} if missing or corrupt."""
    try:
        with open(os.path.join(STATUS_DIR, "out", "status.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


async def server_status() -> Dict:
    """Summary for the admin tab. Host data (fail2ban, queue, containers)
    is taken from status.json as is: if it is stale, the frontend sees it from
    status_updated_at (older than ~5 minutes = "data is stale")."""
    res: Dict = {
        "disk": None, "mem": None, "cert_days": None, "last_backup": None,
        "failed_24h": 0, "postfix_queue": None, "fail2ban": None,
        "containers": None, "status_updated_at": None,
    }
    try:
        du = shutil.disk_usage("/")
        res["disk"] = {"pct": du.used * 100 // du.total, "free_mb": du.free // 2**20}
    except Exception as e:
        logger.warning(f"Disk status failed: {e}")

    try:
        with open("/proc/meminfo") as f:
            info = parse_meminfo(f.read())
        swap_total = info.get("SwapTotal", 0)
        swap_pct = (
            (swap_total - info.get("SwapFree", 0)) * 100 // swap_total if swap_total else 0
        )
        res["mem"] = {"avail_mb": info.get("MemAvailable", 0) // 1024, "swap_pct": swap_pct}
    except Exception as e:
        logger.warning(f"Memory status failed: {e}")

    if WEBAPP_DOMAIN:
        if not _cert_days_cache["at"] or time.monotonic() - _cert_days_cache["at"] >= CERT_CHECK_SEC:
            _cert_days_cache["days"] = await asyncio.to_thread(
                _cert_days_sync, WEB_HOST, WEBAPP_DOMAIN
            )
            _cert_days_cache["at"] = time.monotonic()
        res["cert_days"] = _cert_days_cache["days"]

    try:
        path = latest_backup(BACKUP_DIR)
        if path:
            st = os.stat(path)
            res["last_backup"] = {
                "file": os.path.basename(path),
                "date": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "size_mb": round(st.st_size / 1024 / 1024, 1),
            }
    except Exception as e:
        logger.warning(f"Backup status failed: {e}")

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        res["failed_24h"] = await storage.db.count_failed_since(since)
    except Exception as e:
        logger.warning(f"Failed-24h status failed: {e}")

    host = _read_host_status()
    for key in ("postfix_queue", "fail2ban", "containers"):
        res[key] = host.get(key)
    res["status_updated_at"] = host.get("updated_at")
    return res


def request_unban(ip: str) -> None:
    """Writes an unban request for the host exporter. ValueError on an invalid IP.
    In the file name an IPv6 ":" is replaced with "_" (the script reverses the replacement)."""
    addr = ipaddress.ip_address(ip.strip())
    req_dir = os.path.join(STATUS_DIR, "requests")
    os.makedirs(req_dir, exist_ok=True)
    with open(os.path.join(req_dir, f"unban-{addr.compressed.replace(':', '_')}"), "w"):
        pass
