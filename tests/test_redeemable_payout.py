"""Расхождение 04.09.2026: бот показывал позиции на $140, у оператора ~$200.

Причина: filter_open_positions выбрасывает redeemable=True — это выигранные
рынки, по которым выплата ещё не забрана. Polymarket считает их в Portfolio
(они стоят $1 за долю), бот молчал. Проигранные при этом показывались в
«Реализовано», то есть картина была асимметричной: убытки видны, выигрыши нет.

Плюс /positions отдаёт 100 записей по умолчанию — с ростом истории хвост
отрезается без пагинации."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_status import pending_payout_block, build_status_from_wallet


def _won(cid, title, value):
    return {"conditionId": cid, "title": title, "size": 10, "currentValue": value,
            "initialValue": 5.0, "redeemable": True, "avgPrice": 0.5,
            "curPrice": 1.0, "endDate": "2026-09-01T00:00:00Z"}


def _open(cid, title, invested, current):
    return {"conditionId": cid, "title": title, "size": 10,
            "initialValue": invested, "currentValue": current,
            "redeemable": False, "avgPrice": 0.54, "curPrice": 0.60,
            "endDate": "2026-12-31T00:00:00Z"}


def test_payout_block_sums_unredeemed_wins():
    out = pending_payout_block([_won("0xA", "Won A", 20.0), _won("0xB", "Won B", 15.0)])
    assert out is not None and "35" in out


def test_payout_block_none_when_nothing_pending():
    assert pending_payout_block([_open("0xC", "Open", 10.0, 11.0)]) is None


def test_status_shows_pending_payout():
    pos = [_open("0xC", "Open", 10.0, 11.0), _won("0xA", "Won A", 20.0)]
    out = build_status_from_wallet(pos, [], now=None)
    assert "выплате" in out.lower()


def test_open_count_still_excludes_won():
    pos = [_open("0xC", "Open", 10.0, 11.0), _won("0xA", "Won A", 20.0)]
    out = build_status_from_wallet(pos, [], now=None)
    assert "Позиций: 1" in out        # выигранные не считаются открытыми
