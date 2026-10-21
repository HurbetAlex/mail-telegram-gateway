import html
import json
import logging
import os
import re
from typing import List, Optional
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from src.app.bot.auth import ALLOWED_IDS, can_access, domain_of
from src.app.bot.sender import bot
from src.app.db import storage
from src.app.mail.composer import (
    MAX_ATTACHMENT_SIZE,
    fwd_state,
    reply_state,
    send_and_record,
    user_from_addresses,
)

logger = logging.getLogger(__name__)

router = Router()

if ALLOWED_IDS:
    router.message.filter(F.from_user.id.in_(ALLOWED_IDS))
    router.callback_query.filter(F.from_user.id.in_(ALLOWED_IDS))

MAIN_DOMAIN = os.getenv("MAIL_DOMAIN", "example.com")
# HTTPS URL of the Mini App form (web/compose.html); empty = the button starts a chat dialog
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

COMPOSE_BTN = "✉️ Compose"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOCAL_RE = re.compile(r"^[a-zA-Z0-9._+-]+$")


class Compose(StatesGroup):
    from_addr = State()
    to = State()
    subject = State()
    body = State()
    preview = State()


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _user_domain(user_id: int) -> str:
    # Subdomain owners are defined in DOMAIN_OWNERS (see bot/auth.py);
    # users without their own domain send from the main one
    return domain_of(user_id)


async def _user_aliases(user_id: int) -> List[str]:
    """Local parts of sender addresses on the user's main domain (at most 20: URL length).

    The same list as "From" in the Mini App (composer.user_from_addresses).
    """
    domain = _user_domain(user_id).lower()
    out: List[str] = []
    for addr in await user_from_addresses(user_id, domain):
        local, _, dom = addr.partition("@")
        if dom == domain and local not in out:
            out.append(local)
    return out[:20]


async def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Persistent keyboard. With WEBAPP_URL the button opens the form window (Mini App)."""
    if WEBAPP_URL:
        qs = urlencode(
            {
                "domain": _user_domain(user_id),
                "aliases": ",".join(await _user_aliases(user_id)),
            }
        )
        compose_btn = KeyboardButton(
            text=COMPOSE_BTN, web_app=WebAppInfo(url=f"{WEBAPP_URL}?{qs}")
        )
    else:
        compose_btn = KeyboardButton(text=COMPOSE_BTN)
    return ReplyKeyboardMarkup(
        keyboard=[
            [compose_btn],
            [KeyboardButton(text="🗂 History"), KeyboardButton(text="📊 Statistics")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def _templates_kb(user_id: int) -> Optional[InlineKeyboardMarkup]:
    templates = await storage.db.get_templates(user_id)
    if not templates:
        return None
    rows = [
        [InlineKeyboardButton(text=f"💬 {t['name']}", callback_data=f"tpl:{t['_id']}")]
        for t in templates[:6]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    body = data.get("body_text") or ""
    lines = [
        "📤 <b>Preview</b>",
        "",
        f"<b>From:</b> <code>{_esc(data['from_addr'])}</code>",
        f"<b>To:</b> <code>{_esc(', '.join(data['to']))}</code>",
        f"<b>Subject:</b> {_esc(data['subject'])}",
        "",
        _esc(body[:1500]) if body else "<i>(no text)</i>",
    ]
    if data.get("quote_note"):
        lines.append(f"\n<i>{data['quote_note']}</i>")
    atts = data.get("attachments") or []
    if atts:
        names = ", ".join(_esc(a[0]) for a in atts)
        lines.append(f"\n📎 {len(atts)}: {names}")
    lines.append("\n<i>Send a file to attach it. New text replaces the mail body.</i>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Send", callback_data="csend"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="ccancel"),
            ]
        ]
    )
    await state.set_state(Compose.preview)
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


# --- Cancel (registered before the state handlers so it intercepts /cancel) ---

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Nothing to cancel.")
        return
    await state.clear()
    await message.answer("❌ Cancelled.")


# --- New mail ---

async def start_compose(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    domain = _user_domain(uid)

    prefixes = await _user_aliases(uid)
    buttons = [
        [InlineKeyboardButton(text=f"{p}@{domain}", callback_data=f"cfrom:{p}")]
        for p in prefixes[:8]
    ]

    await state.set_state(Compose.from_addr)
    await state.update_data(mode="new", domain=domain, attachments=[])
    await message.answer(
        "📤 <b>New mail</b>\n\n"
        "Pick a sender address or type your own; the local part alone is enough, "
        f"e.g. <code>sam</code> (domain <code>{domain}</code>):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )


@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext):
    await start_compose(message, state)


@router.message(F.text == COMPOSE_BTN)
async def btn_compose(message: Message, state: FSMContext):
    # Without WEBAPP_URL the button arrives as plain text: start the dialog in the chat
    await start_compose(message, state)


@router.message(F.web_app_data)
async def webapp_compose(message: Message, state: FSMContext):
    """Data from the Mini App form: show the preview right away."""
    try:
        payload = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        await message.answer("⚠️ Could not parse the form data.")
        return

    uid = message.from_user.id
    domain = _user_domain(uid)

    from_local = str(payload.get("from") or "").strip().lower()
    if "@" in from_local:
        from_local = from_local.partition("@")[0]  # the domain is always forced to our own
    if not LOCAL_RE.match(from_local):
        await message.answer("⚠️ Invalid sender address.")
        return

    rcpts = [
        p
        for p in re.split(r"[,;\s]+", str(payload.get("to") or ""))
        if EMAIL_RE.match(p)
    ]
    if not rcpts:
        await message.answer("⚠️ Invalid recipient address.")
        return

    body = str(payload.get("body") or "")
    await state.clear()
    await state.update_data(
        mode="new",
        domain=domain,
        from_addr=f"{from_local}@{domain}",
        to=rcpts,
        subject=str(payload.get("subject") or "").strip() or "(no subject)",
        body_text=body,
        body_html="",
        attachments=[],
    )
    await _show_preview(message, state)


@router.callback_query(Compose.from_addr, F.data.startswith("cfrom:"))
async def cb_choose_from(call: CallbackQuery, state: FSMContext):
    prefix = call.data.split(":", 1)[1]
    data = await state.get_data()
    await state.update_data(from_addr=f"{prefix}@{data['domain']}")
    await state.set_state(Compose.to)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("👤 To whom? Enter the recipient address:")
    await call.answer()


@router.message(Compose.from_addr, F.text)
async def msg_from(message: Message, state: FSMContext):
    raw = message.text.strip().lower()
    data = await state.get_data()
    domain = data["domain"]
    if "@" in raw:
        local, _, dom = raw.partition("@")
        if dom != domain:
            await message.answer(
                f"You can only send from the domain <code>{domain}</code>.",
                parse_mode="HTML",
            )
            return
    else:
        local = raw
    if not LOCAL_RE.match(local):
        await message.answer("Invalid address. Example: <code>sam</code>", parse_mode="HTML")
        return
    await state.update_data(from_addr=f"{local}@{domain}")
    await state.set_state(Compose.to)
    await message.answer("👤 To whom? Enter the recipient address:")


@router.message(Compose.to, F.text)
async def msg_to(message: Message, state: FSMContext):
    parts = re.split(r"[,;\s]+", message.text.strip())
    rcpts = [p for p in parts if EMAIL_RE.match(p)]
    if not rcpts:
        await message.answer("Does not look like an email address, try again:")
        return
    await state.update_data(to=rcpts)
    data = await state.get_data()
    if data.get("mode") == "fwd":
        await state.set_state(Compose.body)
        await message.answer("💬 Comment for the forward (or \"-\" to forward without one):")
    else:
        await state.set_state(Compose.subject)
        await message.answer("📝 Mail subject:")


@router.message(Compose.subject, F.text)
async def msg_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(Compose.body)
    await message.answer(
        "✍️ Mail text (bold, italic and links from Telegram are preserved):",
        reply_markup=await _templates_kb(message.from_user.id),
    )


@router.message(Compose.body, F.text)
async def msg_body(message: Message, state: FSMContext):
    text = "" if message.text.strip() == "-" else message.text
    await state.update_data(
        body_text=text,
        body_html=message.html_text if text else "",
    )
    await _show_preview(message, state)


@router.callback_query(Compose.body, F.data.startswith("tpl:"))
async def cb_template_body(call: CallbackQuery, state: FSMContext):
    template = await storage.db.get_template(call.data.split(":", 1)[1])
    if not template or template.get("user_id") != call.from_user.id:
        await call.answer("Template not found", show_alert=True)
        return
    await state.update_data(body_text=template["text"], body_html="")
    await call.answer(f"Template \"{template['name']}\"")
    await _show_preview(call.message, state)


# --- Preview: attachments, text editing, sending ---

async def _attach(message: Message, state: FSMContext, file_id: str, name: str, mime: str, size: int):
    if size and size > MAX_ATTACHMENT_SIZE:
        await message.answer("⚠️ File is larger than 15 MB, not attaching.")
        return
    file = await bot.get_file(file_id)
    buf = await bot.download_file(file.file_path)
    data = await state.get_data()
    atts = data.get("attachments") or []
    atts.append((name, buf.read(), mime))
    await state.update_data(attachments=atts)
    await _show_preview(message, state)


@router.message(Compose.preview, F.document)
async def preview_attach_doc(message: Message, state: FSMContext):
    doc = message.document
    await _attach(
        message, state, doc.file_id,
        doc.file_name or "file.bin",
        doc.mime_type or "application/octet-stream",
        doc.file_size or 0,
    )


@router.message(Compose.preview, F.photo)
async def preview_attach_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    data = await state.get_data()
    n = len(data.get("attachments") or []) + 1
    await _attach(message, state, photo.file_id, f"photo_{n}.jpg", "image/jpeg", photo.file_size or 0)


@router.message(Compose.preview, F.text)
async def preview_edit_body(message: Message, state: FSMContext):
    await state.update_data(body_text=message.text, body_html=message.html_text)
    await _show_preview(message, state)


@router.callback_query(Compose.preview, F.data == "ccancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Cancelled.")
    await call.answer()


@router.callback_query(Compose.preview, F.data == "csend")
async def cb_send(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await call.answer("Sending…")
    try:
        await send_and_record(call.from_user.id, data)
    except Exception as e:
        logger.exception("Failed to send mail")
        await call.message.answer(f"⚠️ Failed to send: {e}")
        return

    await state.clear()
    await call.message.edit_text(
        "✅ <b>Sent</b>\n\n"
        f"<b>To:</b> <code>{_esc(', '.join(data['to']))}</code>\n"
        f"<b>Subject:</b> {_esc(data['subject'])}",
        parse_mode="HTML",
    )


# --- Reply and forward (buttons under an incoming mail) ---

@router.callback_query(F.data.startswith("reply:"))
async def cb_reply_start(call: CallbackQuery, state: FSMContext):
    oid = call.data.split(":", 1)[1]
    mail = await storage.db.get_mail(oid)
    if not mail or not can_access(call.from_user.id, mail):
        await call.answer("Mail not found in the database", show_alert=True)
        return

    st = reply_state(mail)
    if st is None:
        await call.answer("Could not parse the sender address", show_alert=True)
        return
    orig_from_addr = st["to"][0]

    await state.clear()
    await state.update_data(**st)
    await state.set_state(Compose.body)
    await call.message.answer(
        f"↩️ Reply to <code>{_esc(orig_from_addr)}</code>\n\n"
        "✍️ Write the reply text or pick a template:",
        parse_mode="HTML",
        reply_markup=await _templates_kb(call.from_user.id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("fwd:"))
async def cb_fwd_start(call: CallbackQuery, state: FSMContext):
    oid = call.data.split(":", 1)[1]
    mail = await storage.db.get_mail(oid)
    if not mail or not can_access(call.from_user.id, mail):
        await call.answer("Mail not found in the database", show_alert=True)
        return

    await state.clear()
    await state.update_data(**fwd_state(mail))
    await state.set_state(Compose.to)
    await call.message.answer("➡️ Forward to whom? Enter the address:")
    await call.answer()
