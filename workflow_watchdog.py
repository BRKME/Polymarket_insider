"""Сторож воркфлоу — следит за самими прогонами Actions.

Зачем (дефект найден 05.08.2026): daily_status падал 5 прогонов подряд, и
узнали об этом случайно — оператор заметил тишину. Режим отказа был НЕОТЛИЧИМ
от нормальной работы, потому что «сигналов нет» и «скрипт упал» выглядят
одинаково: молчащий Telegram. Вся обратная связь системы шла через тот же
канал, который и ломается.

Сторож спрашивает у GitHub API прогоны за сутки, находит красные и шлёт список.
Если всё зелено — МОЛЧИТ (иначе сам станет шумом, и его перестанут читать).

Сознательно независим: минимум импортов, не тянет ничего из логики проекта,
чтобы поломка в проекте не заглушила сторожа.
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import requests
except Exception:
    requests = None

REPO = os.getenv("GITHUB_REPOSITORY", "BRKME/Polymarket_insider")
WINDOW_HOURS = 24
# Воркфлоу, чьи провалы не будят: ручная диагностика, отключённое легаси.
IGNORE = {"whale scout (diagnostic)", "Keepalive"}


def find_failures(runs: list) -> list:
    """Красные прогоны из списка (успех/пропуск/в процессе — не в счёт)."""
    out = []
    for r in runs or []:
        if r.get("conclusion") != "failure":
            continue
        if r.get("name") in IGNORE:
            continue
        out.append(r)
    return out


def currently_failing(runs: list) -> list:
    """Красные прогоны только тех воркфлоу, чей ПОСЛЕДНИЙ завершённый прогон
    красный. Починившийся воркфлоу (после красного был зелёный) — молчим.

    Ложная тревога 06.10.2026: вечером 05.10 GitHub снимал задачи из очереди,
    не выдав машину (conclusion=failure); к утру те же воркфлоу были зелёными,
    но сторож видел «красное за сутки» и звал чинить уже работающее."""
    by_name: dict = {}
    for r in runs or []:
        if r.get("name") in IGNORE or r.get("conclusion") is None:
            continue
        by_name.setdefault(r.get("name"), []).append(r)
    out = []
    for name, rs in by_name.items():
        rs.sort(key=lambda r: str(r.get("created_at") or ""))
        if rs[-1].get("conclusion") == "failure":
            out.extend(r for r in rs if r.get("conclusion") == "failure")
    return out


def build_report(failures: list) -> Optional[str]:
    """Сообщение о красных воркфлоу. None, если чинить нечего (молчим)."""
    if not failures:
        return None
    by_name: dict = {}
    for f in failures:
        name = f.get("name", "?")
        rec = by_name.setdefault(name, {"count": 0, "url": f.get("html_url", "")})
        rec["count"] += 1
    lines = [f"🚨 Воркфлоу падают ({len(by_name)} шт. за {WINDOW_HOURS}ч)", ""]
    for name, rec in sorted(by_name.items(), key=lambda x: -x[1]["count"]):
        times = f"{rec['count']}× " if rec["count"] > 1 else ""
        lines.append(f"  ❌ {times}{name}")
    lines.append("")
    lines.append("Тишина в боте может быть ИМЕННО из-за этого, а не из-за "
                 "отсутствия сигналов.")
    first_url = next(iter(by_name.values()))["url"]
    if first_url:
        lines.append(f"🔗 {first_url}")
    return "\n".join(lines)


def _fetch_runs() -> list:
    """Прогоны за окно. Без токена/сети возвращает пусто (мягкий fail-safe)."""
    if requests is None:
        return []
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    since = (datetime.now(timezone.utc)
             - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/actions/runs",
            params={"created": f">{since}", "per_page": 100},
            headers=headers, timeout=25)
        if r.status_code != 200:
            print(f"  GitHub API {r.status_code}")
            return []
        return r.json().get("workflow_runs", [])
    except Exception as e:
        print(f"  ошибка запроса: {e}")
        return []


def _send(msg: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat and requests):
        print("  telegram не настроен, вывожу в лог:")
        print(msg)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=20)
        print("  отправлено в telegram")
    except Exception as e:
        print(f"  отправка не удалась: {e}")


def main() -> None:
    runs = _fetch_runs()
    print(f"сторож: прогонов за {WINDOW_HOURS}ч — {len(runs)}")
    failures = currently_failing(runs)
    report = build_report(failures)
    if report is None:
        print("  всё зелено — молчим")
        return
    print(f"  красных прогонов: {len(failures)}")
    _send(report)


if __name__ == "__main__":
    main()
