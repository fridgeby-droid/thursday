from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "chats": {},
}


class JsonStorage:
    """Small JSON storage with serialized access and atomic file replacement."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        if not self.path.exists():
            self._write_sync(deepcopy(DEFAULT_STATE))

    def _read_sync(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("State root must be an object")
            data.setdefault("version", 1)
            data.setdefault("chats", {})
            return data
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            # Keep the broken copy for inspection and recover with a clean state.
            if self.path.exists():
                backup = self.path.with_suffix(self.path.suffix + ".broken")
                try:
                    os.replace(self.path, backup)
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

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return deepcopy(self._read_sync())

    async def mutate(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        async with self.lock:
            data = self._read_sync()
            result = fn(data)
            self._write_sync(data)
            return result


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
        },
    )
    if title:
        chat["title"] = title
    chat.setdefault("photos", [])
    chat.setdefault("users", {})
    chat.setdefault("milestones", {})
    chat.setdefault("triggered_milestones", [])
    return chat
