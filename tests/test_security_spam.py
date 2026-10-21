"""Regression test: forged X-Spam-Status header must not hide a spam verdict."""
import email
from email.policy import default

from src.app.utils.headers import is_spam_verdict


def mk(headers: str):
    raw = (headers.strip() + "\nSubject: hi\n\nbody\n").encode()
    return email.message_from_bytes(raw, policy=default)


# --- 2. forged X-Spam-Status ---

def test_forged_no_before_real_yes_is_spam():
    msg = mk("X-Spam-Status: No\nX-Spam-Status: Yes, score=15.0")
    assert is_spam_verdict(msg) is True


def test_clean_mail_not_spam():
    assert is_spam_verdict(mk("X-Spam-Status: No, score=0.1")) is False
    assert is_spam_verdict(mk("From: a@b.c")) is False
