import os
import re
from typing import Dict, Optional, Set, Tuple

ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# Legacy guest variables: predate DOMAIN_OWNERS and keep working
GUEST_ID = int(os.getenv("GUEST_ID", 0))
GUEST_DOMAIN = os.getenv("GUEST_DOMAIN", "guest.example.com")

MAIN_DOMAIN = os.getenv("MAIL_DOMAIN", "example.com")


def _parse_domain_owners(raw: str) -> Dict[str, int]:
    """Parses DOMAIN_OWNERS="domain:tg_id,domain:tg_id" into {domain: tg_id}.

    This is how a new person with their own subdomain gets added: one entry in
    .env, no code changes. A malformed entry is skipped silently: a typo in one
    element must not bring down the whole service on startup.
    """
    owners: Dict[str, int] = {}
    for chunk in raw.split(","):
        domain, _, uid = chunk.strip().partition(":")
        domain = domain.strip().lower()
        uid = uid.strip()
        if not domain or not uid.isdigit():
            continue
        owners[domain] = int(uid)
    return owners


# Domain owners: domain -> recipient's Telegram ID
DOMAIN_OWNERS: Dict[str, int] = _parse_domain_owners(os.getenv("DOMAIN_OWNERS", ""))

if GUEST_ID and GUEST_DOMAIN:
    DOMAIN_OWNERS.setdefault(GUEST_DOMAIN.lower(), GUEST_ID)

# Only these users can control the bot and send mail
ALLOWED_IDS = {uid for uid in (ADMIN_ID, *DOMAIN_OWNERS.values()) if uid}


def owner_of(domain: str) -> Optional[int]:
    """Who owns the recipient's domain (None: the caller decides, DB/catch-all)."""
    return DOMAIN_OWNERS.get((domain or "").lower())


def domain_of(user_id: int) -> str:
    """Domain this user sends mail from."""
    for domain, uid in DOMAIN_OWNERS.items():
        if uid == user_id:
            return domain
    return MAIN_DOMAIN


ALIAS_LOCAL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def user_domains(user_id: int) -> Set[str]:
    """Domains on which the user can create aliases."""
    domains = {d for d, uid in DOMAIN_OWNERS.items() if uid == user_id}
    if user_id == ADMIN_ID:
        domains.add(MAIN_DOMAIN.lower())
    return domains


def validate_alias_address(user_id: int, raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Name or full address -> (lowercase address, None) or (None, error).

    Without a domain, use the user's main domain (admin: MAIL_DOMAIN).
    Pure function: the Mini App API uses it too.
    """
    raw = (raw or "").strip().lower()
    if not raw:
        return None, "Empty name"
    if "@" in raw:
        local, _, domain = raw.partition("@")
    else:
        local = raw
        domain = MAIN_DOMAIN.lower() if user_id == ADMIN_ID else domain_of(user_id).lower()
    if not ALIAS_LOCAL_RE.match(local):
        return None, "Name: Latin letters, digits, . _ - (up to 64 chars, must start with a letter/digit)"
    if domain not in user_domains(user_id):
        return None, f"Domain {domain} does not belong to you"
    return f"{local}@{domain}", None


def can_access(user_id: int, mail) -> bool:
    """A mail is accessible to its owner or the admin; a mail without an owner, to the admin only."""
    return user_id == ADMIN_ID or (mail.get("user_id") is not None and mail["user_id"] == user_id)


def scope(user_id: int) -> Optional[int]:
    """Owner filter for DB queries: None = no filter (admin sees everything)."""
    return None if user_id == ADMIN_ID else user_id
