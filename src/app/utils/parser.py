import email
import html
import re
from email.message import Message
from email.policy import default
from email.utils import getaddresses
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

# Keywords near which we look for a verification code. The Russian/Ukrainian terms in the
# regexes below are intentional (they match non-English mails): "code", "one-time password", etc.
KEYWORDS_RE = re.compile(
    r"(?:\bcode\b|\bcodes\b|\botp\b|\bpasscode\b|\bpin\b|\b2fa\b|"
    r"one[- ]?time\s+(?:password|pass)|"
    r"\bкод\w*|одноразов\w+\s+парол\w*|\bпарол\w*\s+(?:для|подтвержд)\w*)",
    re.IGNORECASE,
)
# Context in which a "code" is not a one-time one (anti-phishing, promo codes); Cyrillic alternatives are intentional
_NOT_OTP_CTX_RE = re.compile(
    r"phishing|фишинг|фішинг|promo|coupon|voucher|gift|discount|"
    r"промо|купон|скидк|знижк|подар|сертификат|сертифікат|"
    # codes of organizations/payments (registration number, authorization code, tax code)
    r"registration|\breg\b|реєстрац|регистрац|company|авториз|authori[sz]|approval|"
    r"invoice|\border\b|receipt|заказ|замовлен|квитанц|"
    # postal code only together with the word "index": plain "postal" appears next to OTPs
    r"\bfi\b|\bvat\b|\btax\b|\bzip\b|postal\s+code|поштов\w*\s+індекс|почтов\w*\s+индекс",
    re.IGNORECASE,
)
# Order/invoice/receipt numbers, phones, amounts before the candidate (Cyrillic: order, number, receipt, phone, ...)
_NOT_OTP_PREFIX_RE = re.compile(
    r"(?:invoice|order|receipt|ref|reference|txid|tracking|ticket|phone|tel|"
    r"заказ\w*|замовлен\w*|номер|квитанц\w*|чек|счет|рахунок|телефон)\W{0,12}$",
    re.IGNORECASE,
)
_CURRENCY_SUFFIX_RE = re.compile(
    r"\s{0,2}(?:%|\$|€|£|₴|usd|eur|uah|usdt|грн|руб|коп|\bmin\b|мин|хв)",
    re.IGNORECASE,
)
# Candidates: 4-8 digits (incl. "123 456", "123-456") or 6-10 chars A-Z0-9
# with a digit and an uppercase letter at the same time (lowercase letters are words/promo)
_CANDIDATE_RE = re.compile(
    r"(?<![\w.,#$€£@/=+-])(?:(?:\d{4,8}|\d{3}[ -]\d{3})|"
    r"(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,10})(?![\w@/=+-]|[.,]\d)"
)

MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024


def _clean_for_otp(text: str) -> str:
    """Strips HTML tags, links and %-sequences, the main source of noise."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^<>]{0,3000}>", " ", text)
    text = re.sub(r"\]\([^)]*\)", " ", text)
    text = re.sub(r"(?:https?://|www\.)\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\S*%[0-9A-Fa-f]{2}\S*", " ", text)
    return text


def _looks_like_year(candidate: str) -> bool:
    return len(candidate) == 4 and candidate[:2] in ("19", "20")


def _valid_candidate(text: str, m: "re.Match[str]", adjacent: bool) -> bool:
    cand = re.sub(r"[ -]", "", m.group())
    if cand.isdigit():
        if len(set(cand)) == 1:  # 000000, 111111
            return False
        if _looks_like_year(cand) and not adjacent:
            return False
    if _NOT_OTP_PREFIX_RE.search(text[max(0, m.start() - 20): m.start()]):
        return False
    if _CURRENCY_SUFFIX_RE.match(text[m.end(): m.end() + 8]):
        return False
    return True


def _find_after_keyword(text: str) -> Optional[str]:
    """Candidate after the keyword (same line / nearest lines) or before it."""
    for kw in KEYWORDS_RE.finditer(text):
        if _NOT_OTP_CTX_RE.search(text[max(0, kw.start() - 40): kw.end() + 15]):
            continue
        tail = text[kw.end(): kw.end() + 160]
        # we do not look for a code beyond two line breaks
        parts = re.split(r"\n", tail)
        limited = "\n".join(parts[:4])
        for m in _CANDIDATE_RE.finditer(limited):
            gap = limited[: m.start()]
            adjacent = len(gap.strip(" :-*\n")) == 0
            # there must be no foreign phrases about phishing/promo between the keyword and the code
            if _NOT_OTP_CTX_RE.search(gap):
                break
            if _valid_candidate(limited, m, adjacent):
                return re.sub(r"[ -]", "", m.group())
        # code before the keyword on the same line: "123456 is your code"
        line_start = text.rfind("\n", 0, kw.start()) + 1
        head = text[max(line_start, kw.start() - 40): kw.start()]
        for m in _CANDIDATE_RE.finditer(head):
            if _valid_candidate(head, m, False):
                return re.sub(r"[ -]", "", m.group())
    return None


def extract_otp(text: str, subject: str = "") -> Optional[str]:
    """Looks for a one-time code; returns None when in doubt (precision beats recall).

    First the subject (keyword + code in it), then the body: a code next to a keyword,
    or, if the keyword is only in the subject, a code on a separate line.
    """
    subj = _clean_for_otp(subject)
    body = _clean_for_otp(text)
    if subj and KEYWORDS_RE.search(subj) and not _NOT_OTP_CTX_RE.search(subj):
        code = _find_after_keyword(subj)
        if code:
            return code
    code = _find_after_keyword(body)
    if code:
        return code
    if subj and KEYWORDS_RE.search(subj) and not _NOT_OTP_CTX_RE.search(subj):
        for line in body.splitlines():
            line = line.strip(" *\t")
            m = _CANDIDATE_RE.fullmatch(line) if line else None
            if m and _valid_candidate(line, m, True):
                return re.sub(r"[ -]", "", line)
    return None


def parse_email(content: bytes) -> Tuple[Message, str, List[Tuple[str, bytes, str]]]:
    """Returns (message, body text, attachments [(name, data, mime)])."""
    msg = email.message_from_bytes(content, policy=default)
    body = ""
    attachments: List[Tuple[str, bytes, str]] = []

    def payload_text(part) -> str:
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")

    def html_to_text(raw_html: str) -> str:
        return BeautifulSoup(raw_html, "lxml").get_text(separator="\n", strip=True)

    if msg.is_multipart():
        text_part = None
        html_part = None
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            cdispo = str(part.get("Content-Disposition") or "")
            if "attachment" in cdispo or part.get_filename():
                data = part.get_payload(decode=True) or b""
                if 0 < len(data) <= MAX_ATTACHMENT_SIZE:
                    attachments.append((part.get_filename() or "file.bin", data, ctype))
                continue
            if ctype == "text/plain" and text_part is None:
                text_part = part
            elif ctype == "text/html" and html_part is None:
                html_part = part

        if text_part is not None:
            body = payload_text(text_part)
        elif html_part is not None:
            body = html_to_text(payload_text(html_part))
    else:
        payload = payload_text(msg)
        body = html_to_text(payload) if msg.get_content_type() == "text/html" else payload

    return msg, body, attachments


def clean_local_part(address: str) -> str:
    """john+bank@domain -> john"""
    local = address.split("@")[0] if "@" in address else address
    local = local.lower()
    if "+" in local:
        local = local.split("+")[0]
    return local


async def match_route(db, local_part: str) -> Optional[int]:
    """Route selection: the longest matching prefix, otherwise the first matching regex."""
    routes = await db.get_all_routes()

    best_match = None
    best_len = -1
    for route in routes:
        if route.get("disabled"):
            continue
        prefix = route.get("prefix")
        if prefix and local_part.startswith(prefix) and len(prefix) > best_len:
            best_len = len(prefix)
            best_match = route.get("user_id")
    if best_match:
        return best_match

    for route in routes:
        if route.get("disabled"):
            continue
        pattern = route.get("regex")
        if not pattern:
            continue
        try:
            if re.match(pattern, local_part):
                return route.get("user_id")
        except re.error:
            continue

    return None


# Gmail auto-forwarding: envelope from = <local>+caf_=<recipient>@gmail.com
_CAF_RE = re.compile(r"^<?([^<>@\s]+?)\+caf_=[^<>\s]*@(gmail\.com|googlemail\.com)>?$", re.IGNORECASE)
_EMAIL_TOKEN_RE = re.compile(r"^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$")
_GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def _gmail_key(addr: str) -> str:
    """Normalization for comparison: for gmail, dots and +tag in the local-part are insignificant."""
    addr = addr.strip().lower()
    local, _, domain = addr.rpartition("@")
    if domain in _GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _forwarded_by(msg: Message, mail_from: str, rcpt: str) -> Optional[str]:
    m = _CAF_RE.match((mail_from or "").strip())
    if m:
        return f"{m.group(1)}@{m.group(2)}"
    for raw in msg.get_all("X-Forwarded-For") or []:
        for token in str(raw).replace(",", " ").split():
            token = token.strip("<>;")
            if _EMAIL_TOKEN_RE.match(token) and token.lower() != rcpt:
                return token
    return None


def original_recipient(msg: Message, mail_from: str, rcpt: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns (orig_to, forwarded_by): the address from To/Cc and the mailbox that forwarded the email."""
    try:
        rcpt_l = (rcpt or "").strip().lower()
        forwarded_by = _forwarded_by(msg, mail_from, rcpt_l)

        headers = [str(h) for name in ("To", "Cc") for h in (msg.get_all(name) or [])]
        addrs = [a.strip() for _, a in getaddresses(headers) if "@" in a]

        orig_to: Optional[str] = None
        if addrs:
            if rcpt_l in (a.lower() for a in addrs):
                return None, forwarded_by
            if forwarded_by:
                key = _gmail_key(forwarded_by)
                orig_to = next((a for a in addrs if _gmail_key(a) == key), None)
            if not orig_to:
                orig_to = ", ".join(addrs[:3]) + (", …" if len(addrs) > 3 else "")
        else:
            # To is empty/hidden: the lowest Delivered-To is the earliest along the mail path
            for raw in reversed(msg.get_all("Delivered-To") or []):
                cand = str(raw).strip()
                if "@" in cand and cand.lower() != rcpt_l:
                    orig_to = cand
                    break
            if not orig_to:
                orig_to = forwarded_by

        if orig_to and orig_to.lower() == rcpt_l:
            orig_to = None
        return orig_to, forwarded_by
    except Exception:
        return None, None


def extract_html(msg: Message) -> Optional[str]:
    """Original HTML part of the email (not an attachment) or None.

    Kept separate from parse_email so its contract (msg, body, attachments) stays unchanged.
    """
    for part in msg.walk():
        if part.is_multipart() or part.get_content_type() != "text/html":
            continue
        cdispo = str(part.get("Content-Disposition") or "")
        if "attachment" in cdispo or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    return None
