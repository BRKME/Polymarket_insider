"""Отправка ежедневного брифинга в Telegram (воркфлоу brief_send.yml).

Брифинг пишет запланированная сессия Claude (.claude/skills/daily-brief) в
research/briefs/ГГГГ-ММ-ДД.md и пушит в main. Токен бота есть только в
секретах Actions, поэтому отправляет воркфлоу, а не сессия.

    python3 brief_send.py <изменённые файлы...>
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, List

TG_LIMIT = 4000          # у Telegram 4096; запас на всякий случай


def brief_files(changed: List[str]) -> List[str]:
    return sorted(p for p in changed
                  if p.startswith("research/briefs/") and p.endswith(".md"))


def split_message(text: str, limit: int = TG_LIMIT) -> List[str]:
    """Режет по абзацам; абзац длиннее лимита — по символам."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:limit])
            para = para[limit:]
        cand = f"{cur}\n\n{para}" if cur else para
        if len(cand) > limit:
            parts.append(cur)
            cur = para
        else:
            cur = cand
    if cur:
        parts.append(cur)
    return parts


def _telegram(text: str) -> bool:
    import requests
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram creds missing")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                                "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            print(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        print(f"Telegram error: {e}")
        return False


def send_file(path: str, send_fn: Callable[[str], bool] = _telegram) -> bool:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return True
    return all([send_fn(part) for part in split_message(text)])


def main(argv: List[str]) -> int:
    files = brief_files(argv)
    if not files:
        print("брифингов в пуше нет")
        return 0
    ok = True
    for f in files:
        if Path(f).exists():
            sent = send_file(f)
            print(f"{f}: {'отправлен' if sent else 'НЕ отправлен'}")
            ok &= sent
    return 0 if ok else 1          # красный прогон → сторож сообщит


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
