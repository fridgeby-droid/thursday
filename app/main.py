from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

from .storage import DEFAULT_TEXTS, JsonStorage, ensure_chat

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATA_FILE = os.getenv("DATA_FILE", "/app/data/state.json").strip()
BOT_TZ = os.getenv("BOT_TZ", "Asia/Yekaterinburg").strip()
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "20"))
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

try:
    LOCAL_TZ = ZoneInfo(BOT_TZ)
except Exception:
    LOCAL_TZ = timezone.utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("photo-counter-bot")

router = Router()
storage = JsonStorage(DATA_FILE, backup_keep=BACKUP_KEEP)
operation_lock = asyncio.Lock()


class AdminFlow(StatesGroup):
    waiting_delete_number = State()
    waiting_milestone_threshold = State()
    waiting_milestone_content = State()
    waiting_personal_threshold = State()
    waiting_personal_content = State()
    waiting_text_value = State()


def full_name_from_user(user: Any) -> str:
    if not user:
        return "Пользователь"
    return (getattr(user, "full_name", "") or getattr(user, "username", "") or str(user.id)).strip()


def render_photo_caption(chat: dict[str, Any], number: int, name: str) -> str:
    template = chat.get("texts", {}).get("photo_caption", DEFAULT_TEXTS["photo_caption"])
    try:
        return str(template).format(number=number, name=name)
    except Exception:
        return f"№{number} · {name}"


def active_count(chat: dict[str, Any] | None) -> int:
    return len(chat.get("photos", [])) if chat else 0


def resolve_chat_from_state(state: dict[str, Any], message: Message) -> tuple[int | None, dict[str, Any] | None]:
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return message.chat.id, state.get("chats", {}).get(str(message.chat.id))
    chats = state.get("chats", {})
    if len(chats) == 1:
        chat_id_str, chat = next(iter(chats.items()))
        return int(chat_id_str), chat
    return None, None


async def resolve_private_admin_chat(bot: Bot, user_id: int) -> tuple[int | None, dict[str, Any] | None]:
    state = await storage.snapshot()
    valid: list[tuple[int, dict[str, Any]]] = []
    for cid, chat in state.get("chats", {}).items():
        try:
            member = await bot.get_chat_member(int(cid), user_id)
            if member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR} or user_id in ADMIN_IDS:
                valid.append((int(cid), chat))
        except (TelegramBadRequest, TelegramForbiddenError):
            if user_id in ADMIN_IDS:
                valid.append((int(cid), chat))
    if len(valid) == 1:
        return valid[0]
    return (None, None)


async def is_admin(bot: Bot, message: Message, target_chat_id: int | None = None) -> bool:
    if not message.from_user:
        return False
    if message.from_user.id in ADMIN_IDS:
        return True
    chat_id = target_chat_id
    if chat_id is None and message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        chat_id = message.chat.id
    if chat_id is None:
        return False
    try:
        member = await bot.get_chat_member(chat_id, message.from_user.id)
        return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def safe_edit_caption(bot: Bot, chat_id: int, message_id: int, caption: str) -> None:
    while True:
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption)
            return
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.1)
        except TelegramBadRequest as exc:
            logger.warning("Could not edit caption for %s/%s: %s", chat_id, message_id, exc)
            return


def normalize_media_entry(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"text": value, "photo_file_id": None}
    if isinstance(value, dict):
        return {
            "text": value.get("text") or "",
            "photo_file_id": value.get("photo_file_id"),
        }
    return {"text": "", "photo_file_id": None}


async def send_media_message(bot: Bot, chat_id: int, payload: Any, *, name: str | None = None, count: int | None = None) -> None:
    item = normalize_media_entry(payload)
    text = str(item.get("text") or "")
    if name is not None:
        try:
            text = text.format(name=name, count=count if count is not None else "")
        except Exception:
            pass
    photo_id = item.get("photo_file_id")
    if photo_id:
        await bot.send_photo(chat_id, photo=photo_id, caption=(text[:1024] if text else None))
    elif text:
        await bot.send_message(chat_id, text)


def photo_time(photo: dict[str, Any]) -> datetime | None:
    raw = photo.get("created_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ)
    except ValueError:
        return None


def counts_for_period(chat: dict[str, Any], start: datetime | None = None, end: datetime | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for photo in chat.get("photos", []):
        dt = photo_time(photo)
        if start and (not dt or dt < start):
            continue
        if end and dt and dt >= end:
            continue
        uid = str(photo.get("user_id"))
        counts[uid] = counts.get(uid, 0) + 1
    return counts


def ranking_text(chat: dict[str, Any], counts: dict[str, int], title: str, limit: int = 20) -> str:
    rows = []
    for uid, count in counts.items():
        user = chat.get("users", {}).get(uid, {})
        rows.append((uid, str(user.get("name", "Пользователь")), count))
    rows.sort(key=lambda r: (-r[2], r[1].lower()))
    lines = [f"🏆 <b>{html.escape(title)}</b>"]
    medals = ["🥇", "🥈", "🥉"]
    for idx, (_, name, count) in enumerate(rows[:limit], start=1):
        prefix = medals[idx - 1] if idx <= 3 else f"{idx}."
        lines.append(f"{prefix} {html.escape(name)} — {count}")
    lines.append(f"\nВсего: <b>{sum(counts.values())}</b> фото")
    return "\n".join(lines)


def user_rank_info(chat: dict[str, Any], user_id: int) -> tuple[int, int, int | None]:
    users = [(uid, int(u.get("count", 0))) for uid, u in chat.get("users", {}).items()]
    users.sort(key=lambda x: (-x[1], x[0]))
    current_count = int(chat.get("users", {}).get(str(user_id), {}).get("count", 0))
    for idx, (uid, count) in enumerate(users, start=1):
        if uid == str(user_id):
            gap = None
            if idx > 1:
                above_count = users[idx - 2][1]
                gap = max(0, above_count - count + 1)
            return idx, current_count, gap
    return len(users) + 1, 0, None


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="🎉 Milestones", callback_data="adm:milestones"), InlineKeyboardButton(text="🏅 Личные достижения", callback_data="adm:personal")],
        [InlineKeyboardButton(text="✏️ Тексты бота", callback_data="adm:texts")],
        [InlineKeyboardButton(text="🗑 Удалить фото", callback_data="adm:delete")],
        [InlineKeyboardButton(text="💾 Резервные копии", callback_data="adm:backups")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← В меню", callback_data="adm:home")]])


def milestone_menu(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"{prefix}:add"), InlineKeyboardButton(text="📋 Список", callback_data=f"{prefix}:list")],
        [InlineKeyboardButton(text="← В меню", callback_data="adm:home")],
    ])


def texts_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👋 Приветствие", callback_data="txt:start")],
        [InlineKeyboardButton(text="ℹ️ Инструкция", callback_data="txt:instruction")],
        [InlineKeyboardButton(text="🏷 Подпись к фото", callback_data="txt:photo_caption")],
        [InlineKeyboardButton(text="← В меню", callback_data="adm:home")],
    ])


@router.message(F.photo, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def handle_photo(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.is_bot:
        return
    async with operation_lock:
        state_before = await storage.snapshot()
        chat_before = state_before.get("chats", {}).get(str(message.chat.id)) or ensure_chat(state_before, message.chat.id, message.chat.title or "")
        number = len(chat_before.get("photos", [])) + 1
        name = full_name_from_user(message.from_user)
        caption = render_photo_caption(chat_before, number, name)
        try:
            sent = await bot.send_photo(message.chat.id, photo=message.photo[-1].file_id, caption=caption)
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            logger.exception("Failed to replace photo message")
            try:
                await bot.delete_message(message.chat.id, sent.message_id)
            except Exception:
                pass
            return

        now = datetime.now(timezone.utc).isoformat()
        user_id = message.from_user.id
        username = message.from_user.username

        def commit(state: dict[str, Any]):
            chat = ensure_chat(state, message.chat.id, message.chat.title or "")
            final_number = len(chat["photos"]) + 1
            chat["photos"].append({
                "number": final_number,
                "message_id": sent.message_id,
                "source_message_id": message.message_id,
                "user_id": user_id,
                "name": name,
                "username": username,
                "created_at": now,
            })
            user = chat["users"].setdefault(str(user_id), {"name": name, "username": username, "count": 0})
            user["name"] = name
            user["username"] = username
            user["count"] = int(user.get("count", 0)) + 1

            global_payload = None
            key = str(final_number)
            triggered = {int(x) for x in chat.get("triggered_milestones", [])}
            if key in chat["milestones"] and final_number not in triggered:
                global_payload = chat["milestones"][key]
                chat["triggered_milestones"].append(final_number)

            personal_payload = None
            personal_count = int(user["count"])
            pkey = str(personal_count)
            user_triggered = set(int(x) for x in chat["triggered_personal_milestones"].get(str(user_id), []))
            if pkey in chat["personal_milestones"] and personal_count not in user_triggered:
                personal_payload = chat["personal_milestones"][pkey]
                chat["triggered_personal_milestones"].setdefault(str(user_id), []).append(personal_count)
            return final_number, global_payload, personal_payload, personal_count, dict(chat)

        final_number, global_payload, personal_payload, personal_count, chat_after = await storage.mutate(commit)
        if final_number != number:
            await safe_edit_caption(bot, message.chat.id, sent.message_id, render_photo_caption(chat_after, final_number, name))
        if global_payload:
            await send_media_message(bot, message.chat.id, global_payload)
        if personal_payload:
            await send_media_message(bot, message.chat.id, personal_payload, name=name, count=personal_count)


@router.message(Command("me"))
async def cmd_me(message: Message) -> None:
    if not message.from_user:
        return
    state = await storage.snapshot()
    _, chat = resolve_chat_from_state(state, message)
    if not chat:
        await message.answer("Не могу определить рабочую группу.")
        return
    rank, count, gap = user_rank_info(chat, message.from_user.id)
    text = f"📷 Ваш результат: <b>{count}</b> фото\n🏆 Место в рейтинге: <b>{rank}</b>"
    if gap is not None:
        text += f"\nДо следующего места: <b>{gap}</b> фото"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    state = await storage.snapshot()
    _, chat = resolve_chat_from_state(state, message)
    if not chat:
        await message.answer("Статистика пока пуста.")
        return
    counts = {uid: int(u.get("count", 0)) for uid, u in chat.get("users", {}).items() if int(u.get("count", 0)) > 0}
    await message.answer(ranking_text(chat, counts, "Общий рейтинг"), parse_mode=ParseMode.HTML)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    state = await storage.snapshot(); _, chat = resolve_chat_from_state(state, message)
    if not chat: return await message.answer("Статистика пока пуста.")
    now = datetime.now(LOCAL_TZ); start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await message.answer(ranking_text(chat, counts_for_period(chat, start), "Сегодня"), parse_mode=ParseMode.HTML)


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    state = await storage.snapshot(); _, chat = resolve_chat_from_state(state, message)
    if not chat: return await message.answer("Статистика пока пуста.")
    now = datetime.now(LOCAL_TZ); day = now.replace(hour=0, minute=0, second=0, microsecond=0); start = day - timedelta(days=day.weekday())
    await message.answer(ranking_text(chat, counts_for_period(chat, start), "Эта неделя"), parse_mode=ParseMode.HTML)


@router.message(Command("month"))
async def cmd_month(message: Message) -> None:
    state = await storage.snapshot(); _, chat = resolve_chat_from_state(state, message)
    if not chat: return await message.answer("Статистика пока пуста.")
    now = datetime.now(LOCAL_TZ); start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await message.answer(ranking_text(chat, counts_for_period(chat, start), "Этот месяц"), parse_mode=ParseMode.HTML)


@router.message(Command("total"))
async def cmd_total(message: Message) -> None:
    state = await storage.snapshot(); _, chat = resolve_chat_from_state(state, message)
    await message.answer(f"📷 Всего учтено: {active_count(chat)} фото")


@router.message(Command("info"))
async def cmd_info(message: Message) -> None:
    state = await storage.snapshot(); _, chat = resolve_chat_from_state(state, message)
    text = (chat or {}).get("texts", {}).get("instruction", DEFAULT_TEXTS["instruction"])
    await message.answer(text)


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot) -> None:
    if message.chat.type == ChatType.PRIVATE and message.from_user:
        chat_id, chat = await resolve_private_admin_chat(bot, message.from_user.id)
        if chat_id and chat:
            text = chat.get("texts", {}).get("start", DEFAULT_TEXTS["start"])
            await message.answer(text + "\n\nПанель администратора:", reply_markup=admin_menu())
            return
    state = await storage.snapshot(); _, chat = resolve_chat_from_state(state, message)
    text = (chat or {}).get("texts", {}).get("start", DEFAULT_TEXTS["start"])
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📸 /me — мой результат\n/top — общий рейтинг\n/today — сегодня\n/week — неделя\n/month — месяц\n/total — всего\n/info — инструкция\n/admin — админ-панель"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, bot: Bot) -> None:
    if message.chat.type != ChatType.PRIVATE or not message.from_user:
        await message.answer("Админ-панель открывается в личном чате с ботом.")
        return
    chat_id, chat = await resolve_private_admin_chat(bot, message.from_user.id)
    if not chat_id or not chat:
        await message.answer("Не удалось определить единственную рабочую группу, где вы администратор.")
        return
    await message.answer(f"⚙️ Админ-панель\nГруппа: {html.escape(chat.get('title',''))}", reply_markup=admin_menu(), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "adm:home")
async def cb_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear(); await call.message.edit_text("⚙️ Админ-панель", reply_markup=admin_menu()); await call.answer()


@router.callback_query(F.data == "adm:stats")
async def cb_stats(call: CallbackQuery, bot: Bot) -> None:
    if not call.from_user: return
    chat_id, chat = await resolve_private_admin_chat(bot, call.from_user.id)
    if not chat: return await call.answer("Группа не найдена", show_alert=True)
    now = datetime.now(LOCAL_TZ); day = now.replace(hour=0, minute=0, second=0, microsecond=0); week = day - timedelta(days=day.weekday()); month = day.replace(day=1)
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"Всего: <b>{active_count(chat)}</b>\n"
        f"Сегодня: <b>{sum(counts_for_period(chat, day).values())}</b>\n"
        f"Неделя: <b>{sum(counts_for_period(chat, week).values())}</b>\n"
        f"Месяц: <b>{sum(counts_for_period(chat, month).values())}</b>\n"
        f"Участников: <b>{len([u for u in chat.get('users',{}).values() if int(u.get('count',0))>0])}</b>"
    )
    await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_menu()); await call.answer()


@router.callback_query(F.data == "adm:milestones")
async def cb_milestones(call: CallbackQuery) -> None:
    await call.message.edit_text("🎉 Milestones — общие сообщения по общему числу фото.", reply_markup=milestone_menu("ms")); await call.answer()


@router.callback_query(F.data == "adm:personal")
async def cb_personal(call: CallbackQuery) -> None:
    await call.message.edit_text("🏅 Личные достижения — сообщение, когда конкретный пользователь достигает своего порога.", reply_markup=milestone_menu("pm")); await call.answer()


@router.callback_query(F.data.in_({"ms:add", "pm:add"}))
async def cb_add_threshold(call: CallbackQuery, state: FSMContext) -> None:
    kind = "milestone" if call.data.startswith("ms:") else "personal"
    await state.update_data(kind=kind)
    await state.set_state(AdminFlow.waiting_milestone_threshold if kind == "milestone" else AdminFlow.waiting_personal_threshold)
    await call.message.edit_text("Введите число — порог срабатывания. Например: 100", reply_markup=back_menu()); await call.answer()


@router.message(AdminFlow.waiting_milestone_threshold)
@router.message(AdminFlow.waiting_personal_threshold)
async def state_threshold(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        return await message.answer("Нужно целое число больше 0.")
    data = await state.get_data(); kind = data.get("kind", "milestone")
    await state.update_data(threshold=int(raw))
    await state.set_state(AdminFlow.waiting_milestone_content if kind == "milestone" else AdminFlow.waiting_personal_content)
    hint = "Теперь отправьте текст, фото или фото с подписью."
    if kind == "personal": hint += " В тексте можно использовать {name} и {count}."
    await message.answer(hint)


@router.message(AdminFlow.waiting_milestone_content)
@router.message(AdminFlow.waiting_personal_content)
async def state_milestone_content(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user: return
    chat_id, chat = await resolve_private_admin_chat(bot, message.from_user.id)
    if not chat_id or not chat: return await message.answer("Рабочая группа не определена.")
    data = await state.get_data(); kind = data.get("kind", "milestone"); threshold = int(data["threshold"])
    text = message.caption or message.text or ""
    photo_file_id = message.photo[-1].file_id if message.photo else None
    if not text and not photo_file_id:
        return await message.answer("Отправьте текст или фотографию.")
    payload = {"text": text, "photo_file_id": photo_file_id}
    def mutate(s: dict[str, Any]):
        target = ensure_chat(s, chat_id)
        key = "milestones" if kind == "milestone" else "personal_milestones"
        target[key][str(threshold)] = payload
    await storage.mutate(mutate, backup=True, reason=f"add_{kind}_{threshold}")
    await state.clear()
    await message.answer(f"✅ Сохранено для порога {threshold}.", reply_markup=admin_menu())


@router.callback_query(F.data.in_({"ms:list", "pm:list"}))
async def cb_list_thresholds(call: CallbackQuery, bot: Bot) -> None:
    chat_id, chat = await resolve_private_admin_chat(bot, call.from_user.id)
    if not chat: return await call.answer("Группа не найдена", show_alert=True)
    prefix = "ms" if call.data.startswith("ms:") else "pm"
    key = "milestones" if prefix == "ms" else "personal_milestones"
    items = chat.get(key, {})
    rows = []
    if not items:
        text = "Список пуст."
    else:
        lines = []
        for threshold in sorted(items, key=lambda x: int(x)):
            item = normalize_media_entry(items[threshold]); marker = "🖼 " if item.get("photo_file_id") else ""
            preview = str(item.get("text") or "(без текста)").replace("\n", " ")[:80]
            lines.append(f"{threshold} → {marker}{preview}")
            if len(rows) < 20:
                rows.append([InlineKeyboardButton(text=f"🗑 {threshold}", callback_data=f"{prefix}:del:{threshold}")])
        text = "\n".join(lines)
    rows.append([InlineKeyboardButton(text="➕ Добавить / заменить", callback_data=f"{prefix}:add")])
    rows.append([InlineKeyboardButton(text="← В меню", callback_data="adm:home")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await call.answer()


@router.callback_query(F.data.regexp(r"^(ms|pm):del:\d+$"))
async def cb_delete_threshold(call: CallbackQuery, bot: Bot) -> None:
    prefix, _, raw = call.data.split(":", 2)
    chat_id, chat = await resolve_private_admin_chat(bot, call.from_user.id)
    if not chat_id or not chat: return await call.answer("Группа не найдена", show_alert=True)
    key = "milestones" if prefix == "ms" else "personal_milestones"
    def mutate(s: dict[str, Any]) -> bool:
        target = ensure_chat(s, chat_id)
        return target[key].pop(raw, None) is not None
    deleted = await storage.mutate(mutate, backup=True, reason=f"delete_{key}_{raw}")
    await call.answer("Удалено" if deleted else "Уже отсутствует", show_alert=True)
    title = "🎉 Milestones" if prefix == "ms" else "🏅 Личные достижения"
    await call.message.edit_text(f"{title}\n\nПорог {raw} удалён." if deleted else f"{title}\n\nПорог {raw} уже отсутствует.", reply_markup=milestone_menu(prefix))


@router.callback_query(F.data == "adm:texts")
async def cb_texts(call: CallbackQuery) -> None:
    await call.message.edit_text("✏️ Выберите текст для изменения:", reply_markup=texts_menu()); await call.answer()


@router.callback_query(F.data.startswith("txt:"))
async def cb_edit_text(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    key = call.data.split(":", 1)[1]
    if key not in DEFAULT_TEXTS: return await call.answer()
    _, chat = await resolve_private_admin_chat(bot, call.from_user.id)
    current = (chat or {}).get("texts", {}).get(key, DEFAULT_TEXTS[key])
    await state.update_data(text_key=key)
    await state.set_state(AdminFlow.waiting_text_value)
    extra = "\n\nОбязательно оставьте {number} и {name}." if key == "photo_caption" else ""
    await call.message.edit_text(f"Текущее значение:\n\n{current}{extra}\n\nОтправьте новый текст.", reply_markup=back_menu()); await call.answer()


@router.message(AdminFlow.waiting_text_value)
async def state_text_value(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user: return
    chat_id, chat = await resolve_private_admin_chat(bot, message.from_user.id)
    if not chat_id: return await message.answer("Группа не определена.")
    data = await state.get_data(); key = data.get("text_key"); value = message.text or ""
    if key == "photo_caption" and ("{number}" not in value or "{name}" not in value):
        return await message.answer("В шаблоне подписи должны остаться {number} и {name}.")
    def mutate(s: dict[str, Any]): ensure_chat(s, chat_id)["texts"].__setitem__(key, value)
    await storage.mutate(mutate, backup=True, reason=f"edit_text_{key}")
    await state.clear(); await message.answer("✅ Текст сохранён.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:delete")
async def cb_delete(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.waiting_delete_number)
    await call.message.edit_text("Введите номер фотографии, которую нужно удалить. После удаления последующие номера сдвинутся.", reply_markup=back_menu()); await call.answer()


async def delete_photo_number(bot: Bot, chat_id: int, target: int) -> tuple[bool, str]:
    async with operation_lock:
        state = await storage.snapshot(); chat = state.get("chats", {}).get(str(chat_id)); photos = chat.get("photos", []) if chat else []
        victim = next((p for p in photos if int(p.get("number", 0)) == target), None)
        if not victim: return False, f"Фото №{target} не найдено."
        try: await bot.delete_message(chat_id, int(victim["message_id"]))
        except TelegramBadRequest as exc: return False, f"Не удалось удалить сообщение Telegram: {exc.message}"
        def mutate(data: dict[str, Any]):
            target_chat = ensure_chat(data, chat_id); current = target_chat["photos"]
            removed = next(p for p in current if int(p["number"]) == target); current.remove(removed)
            uid = str(removed["user_id"])
            if uid in target_chat["users"]: target_chat["users"][uid]["count"] = max(0, int(target_chat["users"][uid].get("count", 0)) - 1)
            affected = []
            for idx, photo in enumerate(current, start=1):
                old = int(photo["number"]); photo["number"] = idx
                if old != idx: affected.append(dict(photo))
            return affected, str(removed.get("name", "Пользователь")), dict(target_chat)
        affected, removed_name, chat_after = await storage.mutate(mutate, backup=True, reason=f"delete_photo_{target}")
        for photo in affected:
            await safe_edit_caption(bot, chat_id, int(photo["message_id"]), render_photo_caption(chat_after, int(photo["number"]), str(photo.get("name", "Пользователь"))))
            await asyncio.sleep(0.04)
        return True, f"Фото №{target} ({removed_name}) удалено. Перенумеровано: {len(affected)}."


@router.message(AdminFlow.waiting_delete_number)
async def state_delete_number(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit(): return await message.answer("Введите номер цифрами.")
    if not message.from_user: return
    chat_id, _ = await resolve_private_admin_chat(bot, message.from_user.id)
    if not chat_id: return await message.answer("Группа не определена.")
    ok, text = await delete_photo_number(bot, chat_id, int(raw)); await state.clear(); await message.answer(("✅ " if ok else "⚠️ ") + text, reply_markup=admin_menu())


@router.message(Command("deletephoto"))
async def cmd_delete_photo(message: Message, bot: Bot, command: CommandObject) -> None:
    state = await storage.snapshot(); chat_id, chat = resolve_chat_from_state(state, message)
    if chat_id is None or chat is None or not await is_admin(bot, message, chat_id): return await message.answer("Команда доступна администратору рабочей группы.")
    arg = (command.args or "").strip()
    if not arg.isdigit(): return await message.answer("Использование: /deletephoto 146")
    ok, text = await delete_photo_number(bot, chat_id, int(arg)); await message.answer(("✅ " if ok else "⚠️ ") + text)


@router.callback_query(F.data == "adm:backups")
async def cb_backups(call: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Создать копию", callback_data="backup:create")],
        [InlineKeyboardButton(text="📥 Скачать последнюю", callback_data="backup:download")],
        [InlineKeyboardButton(text="← В меню", callback_data="adm:home")],
    ])
    await call.message.edit_text("💾 Резервные копии state.json\n\nАвтокопия создаётся перед удалением фото и изменением настроек.", reply_markup=kb); await call.answer()


@router.callback_query(F.data == "backup:create")
async def cb_backup_create(call: CallbackQuery) -> None:
    path = await storage.create_backup("manual")
    await call.answer("Копия создана" if path else "Нет данных", show_alert=True)


@router.callback_query(F.data == "backup:download")
async def cb_backup_download(call: CallbackQuery) -> None:
    path = await storage.latest_backup()
    if not path: return await call.answer("Копий пока нет", show_alert=True)
    data = Path(path).read_bytes()
    await call.message.answer_document(BufferedInputFile(data, filename=Path(path).name), caption="Последняя резервная копия")
    await call.answer()


# Backward-compatible milestone commands (text only)
async def command_milestone_upsert(message: Message, bot: Bot, command: CommandObject, personal: bool = False) -> None:
    state = await storage.snapshot(); chat_id, chat = resolve_chat_from_state(state, message)
    if chat_id is None or chat is None or not await is_admin(bot, message, chat_id): return await message.answer("Команда доступна администратору.")
    args = (command.args or "").strip().split(maxsplit=1)
    if len(args) != 2 or not args[0].isdigit(): return await message.answer("Формат: /addmilestone 100 Текст")
    threshold, text = int(args[0]), args[1]
    def mutate(s: dict[str, Any]): ensure_chat(s, chat_id)["personal_milestones" if personal else "milestones"][str(threshold)] = {"text": text, "photo_file_id": None}
    await storage.mutate(mutate, backup=True, reason="command_milestone"); await message.answer("Сохранено.")


@router.message(Command("addmilestone"))
async def cmd_add_milestone(message: Message, bot: Bot, command: CommandObject) -> None: await command_milestone_upsert(message, bot, command, False)


@router.message(Command("milestones"))
async def cmd_milestones(message: Message, bot: Bot) -> None:
    state = await storage.snapshot(); chat_id, chat = resolve_chat_from_state(state, message)
    if chat_id is None or chat is None or not await is_admin(bot, message, chat_id): return await message.answer("Команда доступна администратору.")
    items = chat.get("milestones", {})
    if not items: return await message.answer("Пороговых сообщений пока нет.")
    lines = ["🎉 Milestones"]
    for threshold in sorted(items, key=lambda x: int(x)):
        item = normalize_media_entry(items[threshold]); marker = "🖼 " if item.get("photo_file_id") else ""
        lines.append(f"{threshold} → {marker}{str(item.get('text') or '(без текста)')[:120]}")
    await message.answer("\n".join(lines))


@router.message(Command("editmilestone"))
async def cmd_edit_milestone(message: Message, bot: Bot, command: CommandObject) -> None:
    await command_milestone_upsert(message, bot, command, False)


@router.message(Command("delmilestone"))
async def cmd_del_milestone(message: Message, bot: Bot, command: CommandObject) -> None:
    state = await storage.snapshot(); chat_id, chat = resolve_chat_from_state(state, message)
    if chat_id is None or chat is None or not await is_admin(bot, message, chat_id): return await message.answer("Команда доступна администратору.")
    raw = (command.args or "").strip()
    if not raw.isdigit(): return await message.answer("Формат: /delmilestone 100")
    def mutate(s: dict[str, Any]) -> bool: return ensure_chat(s, chat_id)["milestones"].pop(raw, None) is not None
    deleted = await storage.mutate(mutate, backup=True, reason=f"del_milestone_{raw}")
    await message.answer("Удалено." if deleted else "Порог не найден.")


async def main() -> None:
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="me", description="Мой результат и место"),
        BotCommand(command="top", description="Общий рейтинг"),
        BotCommand(command="today", description="Статистика за сегодня"),
        BotCommand(command="week", description="Статистика за неделю"),
        BotCommand(command="month", description="Статистика за месяц"),
        BotCommand(command="total", description="Всего фотографий"),
        BotCommand(command="info", description="Инструкция"),
        BotCommand(command="admin", description="Панель администратора"),
    ])
    me = await bot.get_me(); logger.info("Starting @%s", me.username)
    try: await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally: await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
