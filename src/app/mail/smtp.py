import html as html_lib
import logging
import os
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import List, Optional, Tuple

import aiosmtplib

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "postfix")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))

# Turn bare URLs into links; leave href="..." and text inside <a> alone
_URL_RE = re.compile(r'(?<![">])(https?://[^\s<>"]+)')

_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;padding:0;background-color:#f4f4f5;">
  <div style="max-width:560px;margin:0 auto;padding:32px 16px;">
    <div style="background:#ffffff;border-radius:12px;padding:32px 36px;
                font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                font-size:16px;line-height:1.65;color:#1f2328;">
{content}
{signature}
    </div>
    <div style="text-align:center;padding:16px 0 0;
                font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
                font-size:12px;color:#9ca3af;">
      {footer}
    </div>
  </div>
</body>
</html>
"""


def linkify(fragment: str) -> str:
    return _URL_RE.sub(
        r'<a href="\1" style="color:#2563eb;text-decoration:none;">\1</a>', fragment
    )


def paragraphize(fragment: str) -> str:
    """HTML fragment (with already escaped text) -> <p> paragraphs with <br> inside."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", fragment) if p.strip()]
    if not parts:
        return ""
    br = "<br>"
    return "\n".join(
        f'<p style="margin:0 0 16px;">{p.replace(chr(10), br)}</p>' for p in parts
    )


def build_html(content: str, footer: str, signature: str = "") -> str:
    sig_html = ""
    if signature:
        sig_text = html_lib.escape(signature).replace("\n", "<br>")
        sig_html = (
            '<div style="margin-top:28px;padding-top:16px;border-top:1px solid #e5e7eb;'
            f'color:#6b7280;font-size:14px;line-height:1.5;">{sig_text}</div>'
        )
    return _TEMPLATE.format(content=linkify(content), signature=sig_html, footer=footer)


async def send_email(
    from_addr: str,
    to_addrs: List[str],
    subject: str,
    body_text: str,
    html_content: Optional[str] = None,
    attachments: Optional[List[Tuple[str, bytes, str]]] = None,  # (name, data, mime)
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    from_name: Optional[str] = None,
) -> str:
    """Sends an email through Postfix (relay from mynetworks, rspamd signs DKIM).

    Returns the Message-ID of the sent email.
    """
    domain = from_addr.split("@")[-1]
    signature = os.getenv("MAIL_SIGNATURE", "")

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to

    text = body_text
    if signature:
        text += f"\n\n-- \n{signature}"
    msg.set_content(text)

    if html_content:
        msg.add_alternative(
            build_html(html_content, footer=domain, signature=signature),
            subtype="html",
        )

    for filename, data, mime in attachments or []:
        maintype, _, subtype = (mime or "").partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    # start_tls=False: the hop to postfix stays inside the docker network and never leaves the host,
    # and the postfix certificate is self-signed, so STARTTLS with verification fails on it
    # (CERTIFICATE_VERIFY_FAILED). Outbound encryption is done by postfix itself
    # (smtp_tls_security_level=may in main.cf).
    await aiosmtplib.send(
        msg,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        sender=from_addr,
        recipients=list(to_addrs),
        start_tls=False,
    )
    logger.info(f"Sent mail {message_id} from {from_addr} to {to_addrs}")
    return message_id
