import email
import os
import sys
from email.policy import default
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123:test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app.bot.sender import recipient_lines  # noqa: E402
from src.app.utils.parser import original_recipient, parse_email  # noqa: E402

RCPT = "admin@example.com"
PLAIN = "admin@example.org"


def mk(headers: str):
    raw = (headers.strip() + "\nSubject: hi\n\nbody\n").encode()
    return email.message_from_bytes(raw, policy=default)


def test_gmail_forward():
    msg = mk("To: avmhmc74@gmail.com\nX-Forwarded-For: a.v.m.h.mc74@gmail.com admin@example.com")
    got = original_recipient(msg, "<a.v.m.h.mc74+caf_=admin=example.com@gmail.com>", RCPT)
    assert got == ("avmhmc74@gmail.com", "a.v.m.h.mc74@gmail.com")


def test_gmail_envelope_without_header():
    msg = mk("To: avmhmc74@gmail.com")
    got = original_recipient(msg, "a.v.m.h.mc74+caf_=admin=example.com@gmail.com", RCPT)
    assert got == ("avmhmc74@gmail.com", "a.v.m.h.mc74@gmail.com")


def test_only_x_forwarded_for():
    msg = mk("X-Forwarded-For: box@mail.ru Admin@Example.com")
    assert original_recipient(msg, PLAIN, RCPT) == ("box@mail.ru", "box@mail.ru")


def test_x_forwarded_for_skips_rcpt():
    msg = mk("To: a@b.c\nX-Forwarded-For: ADMIN@example.com box@mail.ru")
    assert original_recipient(msg, PLAIN, RCPT) == ("a@b.c", "box@mail.ru")


def test_direct():
    msg = mk("To: admin@example.com")
    assert original_recipient(msg, PLAIN, RCPT) == (None, None)


def test_display_name_and_case():
    msg = mk('To: "Name" <Some.User@Example.com>')
    assert original_recipient(msg, PLAIN, RCPT) == ("Some.User@Example.com", None)
    msg = mk('To: "Name" <ADMIN@EXAMPLE.COM>')
    assert original_recipient(msg, PLAIN, RCPT) == (None, None)


def test_many_addresses_truncated():
    msg = mk("To: a@x.io, b@x.io, c@x.io, d@x.io")
    assert original_recipient(msg, PLAIN, RCPT)[0] == "a@x.io, b@x.io, c@x.io, …"


def test_cc_counts_as_direct():
    msg = mk("To: a@x.io\nCc: admin@example.com")
    assert original_recipient(msg, PLAIN, RCPT) == (None, None)


def test_undisclosed_uses_lowest_delivered_to():
    msg = mk(
        "Delivered-To: admin@example.com\nDelivered-To: middle@x.io\n"
        "To: undisclosed-recipients:;\nDelivered-To: first@gmail.com"
    )
    # get_all preserves order of appearance; the last in the list is the earliest
    assert original_recipient(msg, PLAIN, RCPT)[0] == "first@gmail.com"


def test_delivered_to_skips_rcpt():
    msg = mk("To: undisclosed-recipients:;\nDelivered-To: first@gmail.com\nDelivered-To: ADMIN@example.com")
    assert original_recipient(msg, PLAIN, RCPT)[0] == "first@gmail.com"


def test_encoded_header():
    msg = mk("To: =?utf-8?b?0JjQvNGP?= <x@y.z>")
    assert original_recipient(msg, PLAIN, RCPT) == ("x@y.z", None)


def test_gmail_dots_and_tag_match():
    msg = mk("To: other@x.io, avmhmc74@gmail.com")
    got = original_recipient(msg, "a.v.m.h.mc74+caf_=admin=example.com@googlemail.com", RCPT)
    assert got == ("avmhmc74@gmail.com", "a.v.m.h.mc74@googlemail.com")


def test_broken_and_empty_never_raise():
    assert original_recipient(mk("X-Foo: bar"), PLAIN, RCPT) == (None, None)
    assert original_recipient(mk("To: <<<>>>, ;;;, @\nDelivered-To: \x00"), None, None) is not None
    assert original_recipient(None, None, None) == (None, None)
    assert original_recipient(mk("To: a@b.c"), "", "") == ("a@b.c", None)


def test_parse_email_roundtrip():
    msg, _, _ = parse_email(b"To: z@z.io\nSubject: s\n\nhello\n")
    assert original_recipient(msg, PLAIN, RCPT) == ("z@z.io", None)


def test_lines_old_mail():
    assert recipient_lines({"to": "a@b.c"}) == "📨 <b>To:</b> <code>a@b.c</code>"
    assert recipient_lines({"to": "a@b.c"}, icon=False) == "<b>To:</b> <code>a@b.c</code>"


def test_lines_forwarded():
    out = recipient_lines({"to": RCPT, "orig_to": "x@y.z", "forwarded_by": "f@gmail.com"}).split("\n")
    assert out == [
        "📨 <b>To:</b> <code>x@y.z</code>",
        f"↪️ <b>Delivered to:</b> <code>{RCPT}</code>",
        "🔁 <b>Forwarded by:</b> <code>f@gmail.com</code>",
    ]


def test_lines_forwarder_same_as_orig_hidden():
    out = recipient_lines({"to": RCPT, "orig_to": "f@gmail.com", "forwarded_by": "F@gmail.com"})
    assert len(out.split("\n")) == 2


def test_lines_escape():
    out = recipient_lines({"to": "a&b@x.io", "orig_to": "<i>@y.z", "forwarded_by": "<b>@g.com"})
    assert "<i>@" not in out and "&lt;i&gt;@y.z" in out and "a&amp;b@x.io" in out and "&lt;b&gt;@g.com" in out
