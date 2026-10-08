from __future__ import annotations

import asyncio
import json
import os
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_TEXTS = {
    "start": (
        "📸 Фото-счётчик\n\n"
        "Отправляйте фотографии в рабочую группу. Бот автоматически пронумерует их и учтёт в статистике."
    ),
    "instruction": (
        "📸 В этом чате принимаются фотографии.\n\n"
        "Отправьте фото — бот зарегистрирует его и присвоит порядковый номер. "
        "Каждое принятое фото учитывается в вашей личной статистике."
    ),
    "photo_caption": "№{number} · {name}",
}

DEFAULT_STATE: dict[str, Any] = {
    "version": 2,
    "chats": {},
}


class JsonStorage:
    """JSON storage with serialized access, atomic writes and rotating backups."""

    def __init__(self, path: str, backup_keep: int = 20):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.path.parent / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.backup_keep = max(3, backup_keep)
        self.lock = asyncio.Lock()
        if not self.path.exists():
            self._write_sync(deepcopy(DEFAULT_STATE))

    def _read_sync(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("State root must be an object")
            data.setdefault("version", 2)
            data.setdefault("chats", {})
            return data
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            if self.path.exists():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                broken = self.backup_dir / f"state_broken_{stamp}.json"
                try:
                    shutil.copy2(self.path, broken)
                except OSError:
                    pass
            data = deepcopy(DEFAULT_STATE)
            self._write_sync(data)
            return data

    def _write_sync(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _backup_sync(self, reason: str = "manual") -> Path | None:
        if not self.path.exists():
            return None
        safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in reason)[:40]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        target = self.backup_dir / f"state_{stamp}_{safe_reason}.json"
        shutil.copy2(self.path, target)
        backups = sorted(self.backup_dir.glob("state_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[self.backup_keep:]:
            try:
                old.unlink()
            except OSError:
                pass
        return target

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return deepcopy(self._read_sync())

    async def mutate(
        self,
        fn: Callable[[dict[str, Any]], Any],
        *,
        backup: bool = False,
        reason: str = "mutation",
    ) -> Any:
        async with self.lock:
            data = self._read_sync()
            if backup:
                self._backup_sync(reason)
            result = fn(data)
            data["version"] = 2
            self._write_sync(data)
            return result

    async def create_backup(self, reason: str = "manual") -> Path | None:
        async with self.lock:
            return self._backup_sync(reason)

    async def latest_backup(self) -> Path | None:
        async with self.lock:
            backups = sorted(self.backup_dir.glob("state_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            return backups[0] if backups else None


def ensure_chat(state: dict[str, Any], chat_id: int, title: str | None = None) -> dict[str, Any]:
    chats = state.setdefault("chats", {})
    key = str(chat_id)
    chat = chats.setdefault(
        key,
        {
            "title": title or "",
            "photos": [],
            "users": {},
            "milestones": {},
            "triggered_milestones": [],
            "personal_milestones": {},
            "triggered_personal_milestones": {},
            "texts": deepcopy(DEFAULT_TEXTS),
        },
    )
    if title:
        chat["title"] = title
    chat.setdefault("photos", [])
    chat.setdefault("users", {})
    chat.setdefault("milestones", {})
    chat.setdefault("triggered_milestones", [])
    chat.setdefault("personal_milestones", {})
    chat.setdefault("triggered_personal_milestones", {})
    texts = chat.setdefault("texts", {})
    for key_, value in DEFAULT_TEXTS.items():
        texts.setdefault(key_, value)
    return chat
