import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("BOT_TOKEN", "123:test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app.utils.parser import extract_otp  # noqa: E402


@pytest.mark.parametrize("text,subject,expected", [
    # real codes
    ("Your login code: 482915. It expires soon.", "", "482915"),
    ("Ваш код подтверждения: 4821", "", "4821"),  # Russian: "Your confirmation code"
    ("Please enter the code: *7687* or click below", "", "7687"),
    ("Hello!\nOne-time password 12345678", "", "12345678"),
    ("123456 is your verification code", "", "123456"),
    ("Your code is 123 456", "", "123456"),
    ("Please sign in.", "Confirm Your Email. Code: 8632", "8632"),
    ("Dear Customer, your verification code is:\n F854AT2FP3 \nThis code will be active for 10 minutes.", "", "F854AT2FP3"),
    ("Enter the code below on the page:\n\n0152\n\nIgnore if not you.", "Verification Code", "0152"),
    ("Your verification code is:\n482915\nThanks", "", "482915"),
])
def test_real_codes(text, subject, expected):
    assert extract_otp(text, subject) == expected


@pytest.mark.parametrize("text", [
    # fragments of URL-encoded links
    "Verify here https://x.example/a?code=abc%2Foverview&u=2FPO5V5 thanks",
    "Your code link: 252B1W2ro7 %3Dfooter 2Fcitrus",
    "Use code <https://x.example/track/2FsSTrUXtA> now",
    # promo words and promo codes
    "Use promo code autumn10 and month1 for url319",
    "Promo code SAVE2024X gives discount",
    "Код на знижку 1PLUS1 діє до кінця місяця",  # Ukrainian: "Discount code 1PLUS1 valid until end of month"
    # ids, prices, amounts
    "Your code price is $14999 only", "Total code 5000 грн",  # "грн" = UAH
    "Your order code: 1791889867",
    # year
    "Security code updates for 2026 are here",
    # anti-phishing code
    "Anti-Phishing Code: Qwerty77\nWithdrawal completed",
    "Код защиты от фишинга\nPHISH22\nВывод выполнен",  # Russian: "Anti-phishing code ... Withdrawal completed"
    # order / invoice / receipt numbers
    "Receipt #2856-8615-8330 Invoice code 2856",
    "Order code: 482915 has shipped",
    "Код авторизации:\n582241",  # Russian: "Authorization code" (a payment auth code, not an OTP)
    # placeholder
    "Your code 000000",
    # lowercase words
    "Verification code: sendibm1",
])
def test_garbage_not_otp(text):
    assert extract_otp(text) is None


def test_html_and_urls_are_stripped():
    text = '<a href="https://x.example/?code=482915">Your code</a> <b>go</b>'
    assert extract_otp(text) is None


def test_subject_keyword_standalone_line():
    assert extract_otp("Hi!\n\n931204\n\nBye", "Your security code") == "931204"


def test_subject_keyword_does_not_accept_arbitrary_number():
    assert extract_otp("Total 931204 items", "Weekly update") is None


def test_mail_word_near_code_is_not_postal_context():
    # Russian prefix: "A letter after the mail gateway update."
    text = "Письмо после обновления почтового шлюза.\n\nYour verification code: 482915\n"
    assert extract_otp(text) == "482915"


def test_postal_index_is_not_otp():
    # Russian: "Your postal index and code: 65000"
    assert extract_otp("Ваш почтовый индекс и код: 65000") is None
