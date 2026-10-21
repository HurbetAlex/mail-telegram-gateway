"""Newsletter unsubscribe: One-Click POST (RFC 8058), an email to mailto, or a link.

The URL comes from the email, so the sender controls it; therefore the POST goes
only to https and only to public addresses (SSRF protection for the internal network).
"""
import asyncio
import ipaddress
import logging
import re
import socket
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver


logger = logging.getLogger(__name__)

POST_TIMEOUT = 10


class UnsafeURL(Exception):
    pass


def _ip_is_public(ip: "ipaddress._BaseAddress") -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped  # otherwise ::ffff:127.0.0.1 would slip through as a "regular" IPv6
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False
    return ip.is_global


async def _resolve_public(host: str, port: int) -> List[str]:
    """Host addresses; UnsafeURL if at least one is not public."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError as e:
        raise UnsafeURL(f"could not resolve {host}: {e}")
    addrs = []
    for info in infos:
        addr = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise UnsafeURL(f"odd address {addr}")
        if not _ip_is_public(ip):
            raise UnsafeURL(f"{host} points to a non-public address {addr}")
        addrs.append(addr)
    if not addrs:
        raise UnsafeURL(f"{host} does not resolve")
    return addrs


async def check_url(url: str) -> None:
    """Raises UnsafeURL if this URL must not be fetched."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise UnsafeURL("only https is allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURL("no host")
    try:
        port = parts.port or 443
    except ValueError:
        raise UnsafeURL("invalid port")
    if port != 443:
        raise UnsafeURL("only port 443 is allowed")
    await _resolve_public(host, port)


class _SafeResolver(AbstractResolver):
    """Checks addresses at connection time so DNS rebinding cannot bypass check_url."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        addrs = await _resolve_public(host, port)
        return [
            {
                "hostname": host,
                "host": a,
                "port": port,
                "family": socket.AF_INET6 if ":" in a else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for a in addrs
        ]

    async def close(self):
        pass


async def _post_one_click(url: str) -> None:
    await check_url(url)
    timeout = aiohttp.ClientTimeout(total=POST_TIMEOUT)
    connector = aiohttp.TCPConnector(resolver=_SafeResolver())
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async with session.post(
            url,
            data="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        ) as resp:
            if not 200 <= resp.status < 400:
                raise RuntimeError(f"HTTP {resp.status}")


_MAILTO_ADDR = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def parse_mailto_address(mailto: str) -> Optional[str]:
    """Address from mailto: exactly one and valid, otherwise None.

    The address is set by the newsletter sender, so no lists, <>, spaces and
    other tricks; parsed once, subject and body are not needed: the person sends the email themselves.
    """
    path = unquote(urlsplit(mailto).path)
    if path.count("@") != 1 or any(c in path for c in "<>\"' ,;\r\n\t"):
        return None
    return path if _MAILTO_ADDR.fullmatch(path) else None


async def unsubscribe(mail_doc: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Unsubscribe. Statuses: posted, link (+URL), manual (+mailto address), none, failed (+error).

    Order: One-Click POST -> link -> manual mailto. We do not send the mailto email ourselves:
    From/envelope can be forged, and the address is chosen by the newsletter sender.
    """
    unsub = mail_doc.get("unsubscribe") or {}
    http, mailto = unsub.get("http"), unsub.get("mailto")
    error: Optional[str] = None

    if unsub.get("one_click") and http:
        try:
            await _post_one_click(http)
            return "posted", None
        except Exception as e:
            logger.warning(f"One-click unsubscribe failed for {http}: {e}")
            error = str(e) or type(e).__name__

    if http:
        return "link", http
    addr = parse_mailto_address(mailto) if mailto else None
    if addr:
        return "manual", addr
    if error:
        return "failed", error
    return "none", None
