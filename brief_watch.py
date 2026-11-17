"""Сторож ежедневного брифинга (воркфлоу brief_watch.yml).

16.11.2026 брифинг не пришёл: сессия Claude по расписанию упёрлась в
недельный лимит. Расписание отчиталось «доставлено», все воркфлоу были
зелёными, workflow_watchdog молчал — он следит за прогонами Actions, а
разбор идёт вне Actions. Оператор не мог отличить «очередь пуста» (об этом
брифинг пишет сам) от «разбор не запускался».

Проверка простая: к 11:00 МСК в репозитории должен лежать файл брифинга за
сегодня (research/briefs/ГГГГ-ММ-ДД*.md). Нет файла — сообщение в канал.
Есть — молчим. Независим от логики проекта, как и основной сторож.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

BRIEF_DIR = "research/briefs"


def msk_date(now: datetime) -> str:
    return (now.astimezone(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")


def check(brief_dir: str, now: datetime) -> Optional[str]:
    """Текст тревоги, если брифинга за сегодня (по Москве) нет; иначе None."""
    day = msk_date(now)
    try:
        names = os.listdir(brief_dir)
    except OSError:
        names = []
    if any(n.startswith(day) and n.endswith(".md") for n in names):
        return None
    return (f"⚠️ Брифинг за {day[8:10]}.{day[5:7]} не пришёл: разбор по расписанию не "
            "запустился или не дошёл до пуша (лимит Claude, нет права пуша, сбой "
            "сессии). Это не «рынков нет» — о пустой очереди брифинг пишет сам. "
            "Запустить вручную: «сделай брифинг» в сессии с репозиторием.")


def main() -> int:
    msg = check(BRIEF_DIR, datetime.now(timezone.utc))
    if not msg:
        print("брифинг за сегодня на месте")
        return 0
    print(msg)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return 1
    import requests
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=15)
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
