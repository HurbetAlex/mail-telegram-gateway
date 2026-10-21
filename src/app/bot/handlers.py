import html
import logging
from email.utils import parseaddr
from urllib.parse import urlsplit
from typing import Optional, Tuple

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.app.bot.auth import ADMIN_ID, ALLOWED_IDS, can_access, scope, validate_alias_address
from src.app.bot.compose import main_keyboard
from src.app.bot.sender import mail_keyboard, recipient_lines, send_telegram_message
from src.app.db import storage
from src.app.mail.unsubscribe import unsubscribe as run_unsubscribe

logger = logging.getLogger(__name__)

router = Router()

if ALLOWED_IDS:
    router.message.filter(F.from_user.id.in_(ALLOWED_IDS))
    router.callback_query.filter(F.from_user.id.in_(ALLOWED_IDS))

PAGE_SIZE = 8
STATUS_ICONS = {
    "delivered": "✅",
    "sent": "📤",
    "unassigned": "📥",
    "spam": "🚫",
    "failed": "⚠️",
    "blocked": "⛔",
}

HELP = (
    "📬 <b>Email Gateway</b>\n"
    "📬 Mail: the menu button left of the input field (mails, starred, addresses)\n\n"
    "<b>Mail</b>\n"
    "/send: new mail\n"
    "/history [text]: recent mails, filter by sender/subject\n"
    "/unassigned: mails without a route\n"
    "/stats: statistics\n"
    "/starred: starred mails\n"
    "/whereis text: which address a sender wrote to (where you signed up)\n\n"
    "<b>Aliases</b>\n"
    "/alias name [note]: new address for a service\n"
    "/aliases: aliases, counters, disable/delete\n\n"
    "<b>Routes</b>\n"
    "/add_route prefix user_id: route by address prefix\n"
    "/add_domain domain user_id: route by domain\n"
    "/del_route prefix: delete a route\n"
    "/list_routes: list with management buttons\n\n"
    "<b>Rules and templates</b>\n"
    "/mute address|domain: mute a sender\n"
    "/allow address|domain: allowlist (for forwarded mail)\n"
    "/rules: rules with delete buttons (each user has their own; the admin sees the shared ones)\n"
    "/template_add Name | text: save a reply template\n"
    "/templates: list templates\n\n"
    "Quiet hours, digest and code hiding are configured in \"📬 Mail → More → Settings\".\n\n"
    "/cancel: abort the current dialog"
)


NO_ACCESS = "No access"


async def _admin_msg(message: Message) -> bool:
    """True if the command is available (admin); otherwise replies with a refusal."""
    if message.from_user.id == ADMIN_ID:
        return True
    await message.answer("⛔ Admin only")
    return False


async def _admin_call(call: CallbackQuery) -> bool:
    if call.from_user.id == ADMIN_ID:
        return True
    await call.answer(NO_ACCESS, show_alert=True)
    return False


def _esc(s) -> str:
    return html.escape(str(s or ""))


@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        HELP,
        parse_mode="HTML",
        reply_markup=await main_keyboard(message.from_user.id),
    )


# --- Routes ---

@router.message(Command("add_route"))
async def cmd_add_route(message: Message, command: CommandObject):
    if not await _admin_msg(message):
        return
    args = (command.args or "").split()
    if len(args) != 2:
        await message.answer("Usage: /add_route prefix user_id")
        return
    prefix = args[0].lower()
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("user_id must be a number")
        return
    await storage.db.add_route(prefix, user_id)
    await message.answer(f"✅ Route added: <code>{_esc(prefix)}</code> → <code>{user_id}</code>", parse_mode="HTML")


@router.message(Command("add_domain"))
async def cmd_add_domain(message: Message, command: CommandObject):
    if not await _admin_msg(message):
        return
    args = (command.args or "").split()
    if len(args) != 2:
        await message.answer("Usage: /add_domain domain user_id")
        return
    domain = args[0].lower().lstrip("@")
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("user_id must be a number")
        return
    await storage.db.add_domain_route(domain, user_id)
    await message.answer(f"✅ Domain route: <code>@{_esc(domain)}</code> → <code>{user_id}</code>", parse_mode="HTML")


@router.message(Command("del_route"))
async def cmd_del_route(message: Message, command: CommandObject):
    if not await _admin_msg(message):
        return
    prefix = (command.args or "").strip().lower()
    if not prefix:
        await message.answer("Usage: /del_route prefix")
        return
    deleted = await storage.db.delete_route(prefix) or await storage.db.delete_domain_route(prefix.lstrip("@"))
    await message.answer("🗑 Deleted" if deleted else "Route not found")


def _routes_view(routes) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    lines = ["📋 <b>Routes</b>:"]
    rows = []
    for r in routes:
        uid = r.get("user_id")
        if r.get("prefix"):
            label = r["prefix"]
            cb = f"rt:{r['prefix']}"
        elif r.get("domain"):
            label = f"@{r['domain']}"
            cb = f"rd:{r['domain']}"
        else:
            continue
        enabled = not r.get("disabled")
        lines.append(
            f"{'✅' if enabled else '⏸'} <code>{_esc(label)}</code> → <code>{uid}</code>"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'⏸ Off' if enabled else '▶️ On'} {label}",
                    callback_data=f"{cb}:t",
                ),
                InlineKeyboardButton(text=f"🗑 {label}", callback_data=f"{cb}:d"),
            ]
        )
    if len(lines) == 1:
        return "No routes. Add one: /add_route prefix user_id", None
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("list_routes"))
async def cmd_list_routes(message: Message):
    if not await _admin_msg(message):
        return
    text, kb = _routes_view(await storage.db.get_all_routes())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith(("rt:", "rd:")))
async def cb_route_action(call: CallbackQuery):
    if not await _admin_call(call):
        return
    kind, payload = call.data.split(":", 1)
    key, _, action = payload.rpartition(":")
    if action == "t":
        if kind == "rt":
            state = await storage.db.toggle_route(key)
        else:
            state = await storage.db.toggle_domain_route(key)
        note = "enabled" if state else "disabled"
        await call.answer(f"Route {note}" if state is not None else "Not found")
    elif action == "d":
        if kind == "rt":
            await storage.db.delete_route(key)
        else:
            await storage.db.delete_domain_route(key)
        await call.answer("Deleted")
    else:
        await call.answer()
        return

    text, kb = _routes_view(await storage.db.get_all_routes())
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass  # text unchanged


# --- Unassigned mails ---

@router.message(Command("unassigned"))
async def cmd_unassigned(message: Message):
    if not await _admin_msg(message):
        return
    mails = await storage.db.get_unassigned()
    if not mails:
        await message.answer("📭 No unassigned mails.")
        return

    for m in mails[:20]:
        oid = str(m["_id"])
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="👤 Mine", callback_data=f"assign:{oid}"),
                    InlineKeyboardButton(text="📄 Full text", callback_data=f"full:{oid}"),
                ]
            ]
        )
        await message.answer(
            f"📥 <b>From:</b> {_esc(m.get('from'))}\n"
            f"{recipient_lines(m, icon=False)}\n"
            f"<b>Subject:</b> {_esc(m.get('subject') or 'no subject')}",
            parse_mode="HTML",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("assign:"))
async def cb_assign(call: CallbackQuery):
    if not await _admin_call(call):
        return
    oid = call.data.split(":", 1)[1]
    mail = await storage.db.get_mail(oid)
    if not mail:
        await call.answer("Mail not found", show_alert=True)
        return

    uid = call.from_user.id
    tg_msg_id = await send_telegram_message(uid, mail, oid=oid)
    if not tg_msg_id:
        await call.answer("Failed to forward the mail", show_alert=True)
        return

    await storage.db.set_mail_status(oid, "delivered")
    await storage.db.add_delivery(oid, uid, tg_msg_id)
    await call.answer("Assigned ✅")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# --- History ---

def _mail_list(mails) -> Tuple[list, list]:
    """Lines and number buttons for a mail list (shared by /history and /starred)."""
    lines = []
    number_buttons = []
    for i, m in enumerate(mails, 1):
        icon = STATUS_ICONS.get(m.get("status"), "·")
        star = "⭐ " if m.get("starred") else ""
        ts = m.get("timestamp")
        date = ts.strftime("%d.%m %H:%M") if hasattr(ts, "strftime") else ""
        sender = str(m.get("from") or "")[:40]
        subject = str(m.get("subject") or "no subject")[:60]
        lines.append(f"{i}. {icon} {star}<i>{date}</i> {_esc(sender)}")
        lines.append(f"    {_esc(subject)}")
        number_buttons.append(
            InlineKeyboardButton(text=str(i), callback_data=f"full:{m['_id']}")
        )
    return lines, number_buttons


async def _history_view(query: str, page: int, user_id: Optional[int] = None) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    mails, total = await storage.db.get_history(
        query, skip=page * PAGE_SIZE, limit=PAGE_SIZE, user_id=user_id
    )
    header = "🗂 <b>History</b>"
    if query:
        header += f" · filter: <i>{_esc(query)}</i>"
    header += f" · total {total}"

    if not mails:
        return header + "\n\nNo mails found.", None

    lines, number_buttons = _mail_list(mails)
    lines.insert(0, "")
    lines.insert(0, header)

    rows = [number_buttons[i:i + 4] for i in range(0, len(number_buttons), 4)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"h:{page - 1}:{query}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"h:{page + 1}:{query}"))
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("history"))
async def cmd_history(message: Message, command: CommandObject):
    query = (command.args or "").strip()[:30]
    text, kb = await _history_view(query, 0, scope(message.from_user.id))
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("h:"))
async def cb_history_page(call: CallbackQuery):
    _, page_s, query = call.data.split(":", 2)
    text, kb = await _history_view(query, int(page_s), scope(call.from_user.id))
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await call.answer()


# --- Full mail text ---

@router.callback_query(F.data.startswith("full:"))
async def cb_full(call: CallbackQuery):
    oid = call.data.split(":", 1)[1]
    mail = await storage.db.get_mail(oid)
    if not mail:
        await call.answer("Mail not found", show_alert=True)
        return
    if not can_access(call.from_user.id, mail):
        await call.answer(NO_ACCESS, show_alert=True)
        return

    body = mail.get("full_body") or "(empty)"
    header = (
        f"📧 <b>From:</b> {_esc(mail.get('from'))}\n"
        f"{recipient_lines(mail, icon=False)}\n"
        f"<b>Subject:</b> {_esc(mail.get('subject') or 'no subject')}\n\n"
    )
    if len(body) <= 3500:
        await call.message.answer(header + _esc(body), parse_mode="HTML")
    else:
        await call.message.answer_document(
            BufferedInputFile(body.encode("utf-8"), filename="mail.txt"),
            caption=str(mail.get("subject") or "")[:100],
        )
    await call.answer()


# --- Statistics ---

@router.message(F.text == "🗂 History")
async def btn_history(message: Message):
    text, kb = await _history_view("", 0, scope(message.from_user.id))
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("stats"))
@router.message(F.text == "📊 Statistics")
async def cmd_stats(message: Message):
    uid = scope(message.from_user.id)
    stats = await storage.db.get_stats(user_id=uid)
    routes = await storage.db.get_all_routes() if uid is None else []
    total = sum(stats.values())
    lines = [f"📊 <b>Statistics</b>\n\nTotal mails: <b>{total}</b>"]
    for status, n in sorted(stats.items(), key=lambda kv: -kv[1]):
        icon = STATUS_ICONS.get(status, "·")
        lines.append(f"{icon} {status}: {n}")
    if uid is None:
        lines.append(f"\nRoutes: {len(routes)}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# --- Rules (mute a sender) ---

def _rule_owner(user_id: int) -> Optional[int]:
    """Whose rules: admin gets global ones (None), everyone else their own."""
    return None if user_id == ADMIN_ID else user_id


@router.callback_query(F.data.startswith("mute:"))
async def cb_mute(call: CallbackQuery):
    oid = call.data.split(":", 1)[1]
    mail = await storage.db.get_mail(oid)
    if not mail:
        await call.answer("Mail not found", show_alert=True)
        return
    if not can_access(call.from_user.id, mail):
        await call.answer(NO_ACCESS, show_alert=True)
        return
    sender = parseaddr(str(mail.get("from") or ""))[1]
    if not sender:
        await call.answer("Could not parse the sender address", show_alert=True)
        return
    await storage.db.add_rule(sender, "mute", user_id=_rule_owner(call.from_user.id))
    await call.answer(f"Muted: {sender}")
    await call.message.answer(
        f"🔕 <code>{_esc(sender)}</code> muted: its mails will be saved "
        "without notifications (status muted).\nManage: /rules",
        parse_mode="HTML",
    )


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    pattern = (command.args or "").strip().lower()
    if not pattern:
        await message.answer("Usage: /mute address@domain or /mute domain")
        return
    await storage.db.add_rule(pattern, "mute", user_id=_rule_owner(message.from_user.id))
    await message.answer(f"🔕 Muted: <code>{_esc(pattern)}</code>", parse_mode="HTML")


@router.message(Command("allow"))
async def cmd_allow(message: Message, command: CommandObject):
    pattern = (command.args or "").strip().lower()
    if not pattern:
        await message.answer(
            "Usage: /allow address@domain or /allow domain\n"
            "The sender's mails will be delivered even with a spam flag: "
            "useful for forwarding from external mailboxes (SPF breaks)."
        )
        return
    await storage.db.add_rule(pattern, "allow", user_id=_rule_owner(message.from_user.id))
    await message.answer(f"✅ Allowlisted: <code>{_esc(pattern)}</code>", parse_mode="HTML")


RULE_ICONS = {"mute": "🔕", "allow": "✅"}


@router.message(Command("rules"))
async def cmd_rules(message: Message):
    uid = message.from_user.id
    is_admin = uid == ADMIN_ID
    # Admin sees all rules (None), everyone else sees global + their own
    rules = await storage.db.get_rules(None if is_admin else uid)
    if not rules:
        await message.answer("No rules. Add one: /mute address|domain or /allow address|domain")
        return

    def deletable(r) -> bool:
        return is_admin or r.get("user_id") == uid

    def tag(r) -> str:
        if r.get("user_id") is None:
            return " (shared)" if not is_admin else ""
        return f" (👤 {r['user_id']})" if is_admin else ""

    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 {RULE_ICONS.get(r.get('action'), '·')} {r['pattern']}",
                callback_data=f"ruledel:{r['_id']}",
            )
        ]
        for r in rules
        if deletable(r)
    ][:30]
    lines = ["<b>Sender rules</b> (🔕 no notifications, ✅ allowlist):"] + [
        f"{RULE_ICONS.get(r.get('action'), '·')} <code>{_esc(r['pattern'])}</code>{tag(r)}"
        for r in rules
    ]
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@router.callback_query(F.data.startswith("ruledel:"))
async def cb_rule_delete(call: CallbackQuery):
    deleted = await storage.db.delete_rule_by_oid(
        call.data.split(":", 1)[1], _rule_owner(call.from_user.id)
    )
    await call.answer("Deleted" if deleted else "Not found")
    try:
        await call.message.delete()
    except Exception:
        pass


# --- Templates ---

@router.message(Command("template_add"))
async def cmd_template_add(message: Message, command: CommandObject):
    args = command.args or ""
    if "|" not in args:
        await message.answer(
            "Usage: /template_add Name | template text\n"
            "Example: /template_add OK | Got it, thanks!"
        )
        return
    name, _, text = args.partition("|")
    name, text = name.strip()[:40], text.strip()
    if not name or not text:
        await message.answer("Both a name and a text are required.")
        return
    await storage.db.add_template(message.from_user.id, name, text)
    await message.answer(f"💬 Template \"{_esc(name)}\" saved.", parse_mode="HTML")


@router.message(Command("templates"))
async def cmd_templates(message: Message):
    templates = await storage.db.get_templates(message.from_user.id)
    if not templates:
        await message.answer(
            "No templates. Add one: /template_add Name | text\n"
            "Templates appear as buttons when replying to a mail."
        )
        return
    rows = [
        [InlineKeyboardButton(text=f"🗑 {t['name']}", callback_data=f"tpldel:{t['_id']}")]
        for t in templates
    ]
    lines = ["💬 <b>Templates</b>:"] + [
        f"· <b>{_esc(t['name'])}</b>: {_esc(t['text'][:80])}" for t in templates
    ]
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("tpldel:"))
async def cb_template_delete(call: CallbackQuery):
    tpl = await storage.db.get_template(call.data.split(":", 1)[1])
    if not tpl or tpl.get("user_id") != call.from_user.id:
        await call.answer(NO_ACCESS, show_alert=True)
        return
    deleted = await storage.db.delete_template(call.data.split(":", 1)[1])
    await call.answer("Deleted" if deleted else "Not found")
    try:
        await call.message.delete()
    except Exception:
        pass


# --- Starred ---

async def _safe_answer(call: CallbackQuery, text: str = "", alert: bool = False):
    # A callback answer lives for ~a minute; after a long unsubscribe it may expire
    try:
        await call.answer(text, show_alert=alert)
    except TelegramBadRequest:
        pass


async def _refresh_keyboard(call: CallbackQuery, oid: str, mail):
    try:
        await call.message.edit_reply_markup(
            reply_markup=mail_keyboard(oid, mail, otp_masked=bool(mail.get("otp_masked")))
        )
    except TelegramBadRequest:
        pass  # keyboard unchanged


@router.callback_query(F.data.startswith("star:"))
async def cb_star(call: CallbackQuery):
    oid = call.data.split(":", 1)[1]
    mail = await storage.db.get_mail(oid)
    if not mail or not can_access(call.from_user.id, mail):
        await call.answer("Mail not found", show_alert=True)
        return
    starred = not mail.get("starred")
    # Permissions were already checked above
    await storage.db.set_starred(oid, starred)
    mail["starred"] = starred
    await _refresh_keyboard(call, oid, mail)
    await call.answer("⭐ Starred" if starred else "Removed from starred")


async def _starred_view(user_id: int, page: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    # One extra record signals a next page, no separate count needed
    mails = await storage.db.get_starred(scope(user_id), skip=page * PAGE_SIZE, limit=PAGE_SIZE + 1)
    has_more = len(mails) > PAGE_SIZE
    mails = mails[:PAGE_SIZE]
    if not mails:
        return "⭐ No starred mails. Tap \"⭐ Star\" under a mail.", None
    lines, buttons = _mail_list(mails)
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"st:{page - 1}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"st:{page + 1}"))
    if nav:
        rows.append(nav)
    return "\n".join(["⭐ <b>Starred</b>", "", *lines]), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("starred"))
async def cmd_starred(message: Message):
    text, kb = await _starred_view(message.from_user.id, 0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("st:"))
async def cb_starred_page(call: CallbackQuery):
    text, kb = await _starred_view(call.from_user.id, int(call.data.split(":", 1)[1]))
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        pass
    await call.answer()


# --- Unsubscribe from a mailing list ---

@router.callback_query(F.data == "unsub_done")
async def cb_unsub_done(call: CallbackQuery):
    await call.answer("Unsubscribe already sent")


@router.callback_query(F.data.startswith("unsub:"))
async def cb_unsub(call: CallbackQuery):
    oid = call.data.split(":", 1)[1]
    mail = await storage.db.get_mail(oid)
    if not mail or not can_access(call.from_user.id, mail):
        await call.answer("Mail not found", show_alert=True)
        return
    if mail.get("unsubscribed_at"):
        await call.answer("Unsubscribe already sent")
        return

    status, extra = await run_unsubscribe(mail)
    if status == "posted":
        await storage.db.set_unsubscribed(oid)
        mail["unsubscribed_at"] = True
        await _refresh_keyboard(call, oid, mail)
        await _safe_answer(
            call, "Unsubscribe sent"
        )
    elif status == "link":
        host = urlsplit(extra).hostname or ""
        await call.message.answer(
            f"🚫 Unsubscribe is only possible via a web page ({_esc(host)}):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Open the unsubscribe page", url=extra)]]
            ),
        )
        await _safe_answer(call)
    elif status == "manual":
        await call.message.answer(
            f"🚫 Unsubscribe is only possible by mail to <code>{_esc(extra)}</code>. "
            "Not sent automatically: the address is set by the mail's sender, "
            "send it yourself if you trust them.",
            parse_mode="HTML",
        )
        await _safe_answer(call)
    elif status == "failed":
        await _safe_answer(call, f"Failed to unsubscribe: {extra}"[:190], alert=True)
    else:
        await _safe_answer(call, "This mail has no unsubscribe method", alert=True)


# --- Aliases ---

ALIAS_HELP = (
    "🏷 <b>Aliases</b>: a separate address for each service.\n"
    "/alias name [note], e.g. <code>/alias netflix Netflix</code>\n"
    "You can give a full address on your own domain. Mail to an alias arrives as usual "
    "marked with 🏷; if other domains write to the address, it has leaked: disable it in /aliases."
)


@router.message(Command("alias"))
async def cmd_alias(message: Message, command: CommandObject):
    args = (command.args or "").split(maxsplit=1)
    if not args:
        await message.answer(ALIAS_HELP, parse_mode="HTML")
        return
    uid = message.from_user.id
    address, err = validate_alias_address(uid, args[0])
    if err:
        await message.answer(f"⚠️ {_esc(err)}", parse_mode="HTML")
        return
    note = args[1].strip()[:100] if len(args) > 1 else ""
    try:
        await storage.db.create_alias(uid, address, note)
    except ValueError as e:
        await message.answer(f"⚠️ {_esc(e)}", parse_mode="HTML")
        return
    await message.answer(
        f"✅ Alias created:\n<code>{_esc(address)}</code>" + (f"\n🏷 {_esc(note)}" if note else ""),
        parse_mode="HTML",
    )


async def _aliases_view(user_id: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    aliases = await storage.db.list_aliases(scope(user_id))
    if not aliases:
        return "No aliases. Create one: /alias name [note]", None
    lines = ["🏷 <b>Aliases</b>:"]
    rows = []
    for a in aliases[:30]:
        enabled = not a.get("disabled")
        line = f"{'✅' if enabled else '⏸'} <code>{_esc(a['address'])}</code>"
        if a.get("note"):
            line += f" — {_esc(a['note'])}"
        line += f"\n    mails: {a.get('mail_count', 0)}"
        domains = a.get("sender_domains") or []
        if len(domains) > 1:
            line += f"\n    ⚠️ {len(domains)} different domains write to it"
        lines.append(line)
        local = a["address"].split("@")[0][:20]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'⏸ Off' if enabled else '▶️ On'} {local}",
                    callback_data=f"alias_t:{a['_id']}",
                ),
                InlineKeyboardButton(text=f"🗑 {local}", callback_data=f"alias_d:{a['_id']}"),
            ]
        )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("aliases"))
async def cmd_aliases(message: Message):
    text, kb = await _aliases_view(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith(("alias_t:", "alias_d:")))
async def cb_alias_action(call: CallbackQuery):
    kind, alias_id = call.data.split(":", 1)
    uid = call.from_user.id
    owner = scope(uid)
    if kind == "alias_t":
        aliases = await storage.db.list_aliases(owner)
        cur = next((a for a in aliases if str(a["_id"]) == alias_id), None)
        if not cur:
            await call.answer("Not found", show_alert=True)
            return
        disabled = not cur.get("disabled")
        await storage.db.set_alias_disabled(alias_id, owner, disabled)
        await call.answer("Disabled: mails are blocked" if disabled else "Enabled")
    else:
        ok = await storage.db.delete_alias(alias_id, owner)
        await call.answer("Deleted" if ok else "Not found")

    text, kb = await _aliases_view(uid)
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        pass


# --- Where did I sign up ---

def _fmt_day(dt) -> str:
    return dt.strftime("%d.%m.%Y") if hasattr(dt, "strftime") else "?"


def format_whereis(rows) -> str:
    lines = []
    for r in rows:
        sender = parseaddr(str(r.get("sender") or ""))[1]
        domain = sender.split("@")[-1] if sender else "?"
        via = f" via <code>{_esc(r['forwarded_by'])}</code>" if r.get("forwarded_by") else ""
        n = r["count"]
        lines.append(
            f"<b>{_esc(domain)}</b> → To: <code>{_esc(r.get('to'))}</code>{via} — "
            f"{n} {_plural(n)}, {_fmt_day(r.get('first'))}–{_fmt_day(r.get('last'))}"
        )
    return "\n".join(lines)


def _plural(n: int) -> str:
    return "mail" if n == 1 else "mails"


@router.message(Command("whereis"))
async def cmd_whereis(message: Message, command: CommandObject):
    query = (command.args or "").strip()[:60]
    if not query:
        await message.answer("Usage: /whereis service, e.g. /whereis prohoster")
        return
    rows = await storage.db.whereis(query, scope(message.from_user.id))
    if not rows:
        await message.answer("No mails from such a sender.")
        return
    await message.answer(
        f"🔎 <b>{_esc(query)}</b>\n\n" + format_whereis(rows), parse_mode="HTML"
    )


# --- Deleting a notification ---

@router.callback_query(F.data.startswith("del_msg"))
async def cb_delete(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("Deleted")
