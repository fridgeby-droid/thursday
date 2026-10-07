from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, Message
from dotenv import load_dotenv

from .storage import JsonStorage, ensure_chat

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATA_FILE = os.getenv("DATA_FILE", "/app/data/state.json").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("photo-counter-bot")

router = Router()
storage = JsonStorage(DATA_FILE)

# Serializes photo numbering/deletion across handlers. This MVP is a single process.
operation_lock = asyncio.Lock()


def display_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Пользователь"
    return user.full_name.strip() or user.username or str(user.id)


def photo_caption(number: int, name: str) -> str:
    return f"№{number} · {name}"


def active_count(chat: dict[str, Any]) -> int:
    return len(chat.get("photos", []))


def resolve_chat_from_state(state: dict[str, Any], message: Message) -> tuple[int | None, dict[str, Any] | None]:
    """Group commands target their group; private commands target the only known group."""
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        chat = state.get("chats", {}).get(str(message.chat.id))
        return message.chat.id, chat

    chats = state.get("chats", {})
    if len(chats) == 1:
        chat_id_str, chat = next(iter(chats.items()))
        return int(chat_id_str), chat
    return None, None


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


@router.message(F.photo, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def handle_photo(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.is_bot:
        return

    async with operation_lock:
        name = display_name(message)
        user_id = message.from_user.id
        username = message.from_user.username
        chat_title = message.chat.title or ""

        # Reserve a number before deleting the original. The lock prevents duplicates.
        def reserve(state: dict[str, Any]) -> int:
            chat = ensure_chat(state, message.chat.id, chat_title)
            return len(chat["photos"]) + 1

        number = await storage.mutate(reserve)

        try:
            # Re-send by Telegram file_id; no photo bytes are stored locally.
            sent = await bot.send_photo(
                chat_id=message.chat.id,
                photo=message.photo[-1].file_id,
                caption=photo_caption(number, name),
            )
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            logger.exception("Failed to replace photo message")
            try:
                if "sent" in locals():
                    await bot.delete_message(message.chat.id, sent.message_id)
            except Exception:
                pass
            return

        now = datetime.now(timezone.utc).isoformat()

        def commit(state: dict[str, Any]) -> tuple[str | None, int]:
            chat = ensure_chat(state, message.chat.id, chat_title)
            # Number can only drift if state was manually edited; normalize from current length.
            final_number = len(chat["photos"]) + 1
            entry = {
                "number": final_number,
                "message_id": sent.message_id,
                "source_message_id": message.message_id,
                "user_id": user_id,
                "name": name,
                "username": username,
                "created_at": now,
            }
            chat["photos"].append(entry)

            users = chat["users"]
            user = users.setdefault(
                str(user_id),
                {"name": name, "username": username, "count": 0},
            )
            user["name"] = name
            user["username"] = username
            user["count"] = int(user.get("count", 0)) + 1

            milestone_text = None
            key = str(final_number)
            triggered = {int(x) for x in chat.get("triggered_milestones", [])}
            if key in chat.get("milestones", {}) and final_number not in triggered:
                milestone_text = chat["milestones"][key]
                chat["triggered_milestones"].append(final_number)

            return milestone_text, final_number

        milestone_text, final_number = await storage.mutate(commit)

        if final_number != number:
            await safe_edit_caption(bot, message.chat.id, sent.message_id, photo_caption(final_number, name))

        if milestone_text:
            await bot.send_message(message.chat.id, milestone_text)


@router.message(Command("me"))
async def cmd_me(message: Message) -> None:
    if not message.from_user:
        return
    state = await storage.snapshot()
    _, chat = resolve_chat_from_state(state, message)
    if not chat:
        await message.answer("Не могу определить рабочую группу. Сначала отправьте в неё хотя бы одно фото.")
        return
    user = chat.get("users", {}).get(str(message.from_user.id), {})
    await message.answer(f"📷 Ваш результат: {int(user.get('count', 0))} фото")


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    state = await storage.snapshot()
    _, chat = resolve_chat_from_state(state, message)
    if not chat:
        await message.answer("Статистика пока пуста.")
        return
    users = sorted(
        chat.get("users", {}).values(),
        key=lambda x: int(x.get("count", 0)),
        reverse=True,
    )
    if not users:
        await message.answer("Статистика пока пуста.")
        return
    lines = ["🏆 <b>Рейтинг</b>"]
    medals = ["🥇", "🥈", "🥉"]
    for idx, user in enumerate(users[:20], start=1):
        prefix = medals[idx - 1] if idx <= 3 else f"{idx}."
        lines.append(f"{prefix} {html.escape(str(user.get('name', 'Пользователь')))} — {int(user.get('count', 0))}")
    lines.append(f"\nВсего: <b>{active_count(chat)}</b> фото")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("total"))
async def cmd_total(message: Message) -> None:
    state = await storage.snapshot()
    _, chat = resolve_chat_from_state(state, message)
    total = active_count(chat) if chat else 0
    await message.answer(f"📷 Всего учтено: {total} фото")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "📸 <b>Фото-счётчик</b>\n\n"
        "Отправьте фотографию в рабочую группу — бот заменит её сообщением вида «№127 · Иван Петров» и учтёт в статистике.\n\n"
        "/me — мой результат\n"
        "/top — рейтинг\n"
        "/total — общее количество\n\n"
        "Администратору:\n"
        "/deletephoto N — удалить фото №N и перенумеровать следующие\n"
        "/milestones — список праздничных сообщений\n"
        "/addmilestone N текст — добавить\n"
        "/editmilestone N текст — изменить\n"
        "/delmilestone N — удалить"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await cmd_help(message)


@router.message(Command("deletephoto"))
async def cmd_delete_photo(message: Message, bot: Bot, command: CommandObject) -> None:
    state = await storage.snapshot()
    chat_id, chat = resolve_chat_from_state(state, message)
    if chat_id is None or chat is None:
        await message.answer("Не могу определить рабочую группу.")
        return
    if not await is_admin(bot, message, chat_id):
        await message.answer("Эта команда доступна только администраторам.")
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Использование: /deletephoto 146")
        return
    target = int(arg)

    async with operation_lock:
        state = await storage.snapshot()
        chat = state.get("chats", {}).get(str(chat_id))
        photos = chat.get("photos", []) if chat else []
        victim = next((p for p in photos if int(p.get("number", 0)) == target), None)
        if not victim:
            await message.answer(f"Фото №{target} не найдено.")
            return

        try:
            await bot.delete_message(chat_id, int(victim["message_id"]))
        except TelegramBadRequest as exc:
            await message.answer(f"Не удалось удалить сообщение Telegram: {exc.message}")
            return

        def remove_and_renumber(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
            target_chat = ensure_chat(data, chat_id)
            current = target_chat["photos"]
            removed = next(p for p in current if int(p["number"]) == target)
            current.remove(removed)

            uid = str(removed["user_id"])
            if uid in target_chat["users"]:
                target_chat["users"][uid]["count"] = max(
                    0, int(target_chat["users"][uid].get("count", 0)) - 1
                )

            affected = []
            for idx, photo in enumerate(current, start=1):
                old = int(photo["number"])
                photo["number"] = idx
                if old != idx:
                    affected.append(dict(photo))

            return affected, str(removed.get("name", "Пользователь"))

        affected, removed_name = await storage.mutate(remove_and_renumber)

        # All re-sent photos belong to the bot, so their captions are editable later.
        for photo in affected:
            await safe_edit_caption(
                bot,
                chat_id,
                int(photo["message_id"]),
                photo_caption(int(photo["number"]), str(photo.get("name", "Пользователь"))),
            )
            await asyncio.sleep(0.04)

        await message.answer(
            f"Фото №{target} ({html.escape(removed_name)}) удалено. "
            f"Перенумеровано: {len(affected)}.",
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("milestones"))
async def cmd_milestones(message: Message, bot: Bot) -> None:
    state = await storage.snapshot()
    chat_id, chat = resolve_chat_from_state(state, message)
    if chat_id is None or chat is None:
        await message.answer("Не могу определить рабочую группу.")
        return
    if not await is_admin(bot, message, chat_id):
        await message.answer("Эта команда доступна только администраторам.")
        return
    milestones = chat.get("milestones", {})
    if not milestones:
        await message.answer("Пороговых сообщений пока нет.")
        return
    lines = ["🎉 <b>Пороговые сообщения</b>"]
    for key in sorted(milestones, key=lambda x: int(x)):
        lines.append(f"{key} → {html.escape(str(milestones[key]))}")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


async def upsert_milestone(message: Message, bot: Bot, command: CommandObject, edit_only: bool) -> None:
    state = await storage.snapshot()
    chat_id, chat = resolve_chat_from_state(state, message)
    if chat_id is None or chat is None:
        await message.answer("Не могу определить рабочую группу.")
        return
    if not await is_admin(bot, message, chat_id):
        await message.answer("Эта команда доступна только администраторам.")
        return

    args = (command.args or "").strip().split(maxsplit=1)
    if len(args) != 2 or not args[0].isdigit() or int(args[0]) < 1:
        await message.answer("Формат: /addmilestone 100 Текст сообщения")
        return
    threshold, text = int(args[0]), args[1].strip()
    if not text:
        await message.answer("Текст сообщения не может быть пустым.")
        return

    def mutate(data: dict[str, Any]) -> bool:
        target = ensure_chat(data, chat_id)
        exists = str(threshold) in target["milestones"]
        if edit_only and not exists:
            return False
        target["milestones"][str(threshold)] = text
        return True

    ok = await storage.mutate(mutate)
    if not ok:
        await message.answer(f"Порог {threshold} не найден.")
        return
    await message.answer(f"Готово: {threshold} → {text}")


@router.message(Command("addmilestone"))
async def cmd_add_milestone(message: Message, bot: Bot, command: CommandObject) -> None:
    await upsert_milestone(message, bot, command, edit_only=False)


@router.message(Command("editmilestone"))
async def cmd_edit_milestone(message: Message, bot: Bot, command: CommandObject) -> None:
    await upsert_milestone(message, bot, command, edit_only=True)


@router.message(Command("delmilestone"))
async def cmd_del_milestone(message: Message, bot: Bot, command: CommandObject) -> None:
    state = await storage.snapshot()
    chat_id, chat = resolve_chat_from_state(state, message)
    if chat_id is None or chat is None:
        await message.answer("Не могу определить рабочую группу.")
        return
    if not await is_admin(bot, message, chat_id):
        await message.answer("Эта команда доступна только администраторам.")
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Формат: /delmilestone 100")
        return
    threshold = int(arg)

    def mutate(data: dict[str, Any]) -> bool:
        target = ensure_chat(data, chat_id)
        return target["milestones"].pop(str(threshold), None) is not None

    deleted = await storage.mutate(mutate)
    await message.answer("Удалено." if deleted else f"Порог {threshold} не найден.")


async def main() -> None:
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands(
        [
            BotCommand(command="me", description="Мой результат"),
            BotCommand(command="top", description="Рейтинг участников"),
            BotCommand(command="total", description="Всего фотографий"),
            BotCommand(command="help", description="Помощь"),
        ]
    )

    me = await bot.get_me()
    logger.info("Starting @%s", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
