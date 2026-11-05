"""Доставка ежедневного брифинга в Telegram (04.11.2026).

Разбор делает запланированная сессия Claude (.claude/skills/daily-brief): она
пишет research/briefs/ГГГГ-ММ-ДД.md и пушит в main. Токен Telegram живёт
только в секретах Actions — в сессию он не передаётся; отправляет воркфлоу
brief_send.yml по пушу файла брифинга.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brief_send as bs


def test_only_brief_files_selected():
    changed = ["research/briefs/2026-11-05.md", "research/brief_log.jsonl",
               "event_journal.jsonl", "research/briefs/README.txt", ""]
    assert bs.brief_files(changed) == ["research/briefs/2026-11-05.md"]


def test_short_message_single_chunk():
    assert bs.split_message("a\n\nb") == ["a\n\nb"]


def test_long_message_split_on_paragraphs():
    para = "x" * 1500
    text = "\n\n".join([para] * 5)               # 7500+ символов
    parts = bs.split_message(text, limit=4000)
    assert len(parts) >= 2
    assert all(len(p) <= 4000 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == "x" * 7500


def test_giant_paragraph_hard_split():
    parts = bs.split_message("y" * 9000, limit=4000)
    assert all(len(p) <= 4000 for p in parts) and sum(map(len, parts)) == 9000


def test_send_all_parts(tmp_path):
    f = tmp_path / "2026-11-05.md"
    f.write_text("📋 брифинг\n\nтекст", encoding="utf-8")
    sent = []
    assert bs.send_file(str(f), send_fn=lambda t: sent.append(t) or True) is True
    assert sent == ["📋 брифинг\n\nтекст"]


def test_send_failure_reported(tmp_path):
    f = tmp_path / "b.md"
    f.write_text("t", encoding="utf-8")
    assert bs.send_file(str(f), send_fn=lambda t: False) is False


def test_empty_file_not_sent(tmp_path):
    f = tmp_path / "b.md"
    f.write_text("   \n", encoding="utf-8")
    sent = []
    assert bs.send_file(str(f), send_fn=lambda t: sent.append(t) or True) is True
    assert sent == []


def test_skill_writes_brief_file():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, ".claude/skills/daily-brief/SKILL.md"),
                encoding="utf-8").read()
    assert "research/briefs/" in text and "[skip ci]" in text


def test_notes_never_sent():
    # 05.11: полный разбор — в research/brief_notes/, в канал только короткий файл
    assert bs.brief_files(["research/brief_notes/2026-11-05.md",
                           "research/briefs/2026-11-05.md"]) == ["research/briefs/2026-11-05.md"]


def test_skill_compact_format():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, ".claude/skills/daily-brief/SKILL.md"),
                encoding="utf-8").read()
    assert "research/brief_notes/" in text and "Почему:" in text
