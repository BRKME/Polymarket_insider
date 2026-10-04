"""Подсветка позиций, открытых по «перевёрнутым» алертам (04.10.2026).

До фикса YES-гейт судил по откалиброванной оценке: Grok «против» выдавался
за «согласие». Реальные ставки по таким алертам (Maine 4%, Lamine Yamal 12%,
Variational 32%...) оператор решил закрыть — сигнал выхода должен их назвать.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grok_flip as gf
import mark_to_market as mtm


def _pos(cid="m", alerted="2026-10-02T03:00:00+00:00", grok=0.688, **kw):
    r = {"condition_id": cid, "question": "Will the Democrats win the Maine Senate race in 2026?",
         "side": "YES", "status": "open", "alerted_at": alerted,
         "ai_yes_estimate": grok, "market_yes_price": 0.6, "stake_actual": 15.25,
         "fill_source": "onchain", "entry_price_actual": 0.6}
    r.update(kw)
    return r


def _cal(cid, ts, raw):
    return {"condition_id": cid, "estimated_at": ts, "ai_yes_estimate": raw}


CALIB = [_cal("m", "2026-09-20T00:00:00+00:00", 0.70),   # старая — не та
         _cal("m", "2026-10-02T02:55:00+00:00", 0.04),   # та, что дала алерт
         _cal("m", "2026-10-03T00:00:00+00:00", 0.60)]   # после алерта — не та


def test_raw_at_alert_is_latest_before_alert():
    assert gf.raw_at_alert(_pos(), gf.index_calibration(CALIB)) == 0.04


def test_raw_missing_is_none():
    assert gf.raw_at_alert(_pos(cid="zzz"), gf.index_calibration(CALIB)) is None


def test_flip_detected_for_yes_against():
    assert gf.is_flipped(_pos(), gf.index_calibration(CALIB)) is True


def test_no_flip_when_raw_agrees():
    cal = [_cal("m", "2026-10-02T02:55:00+00:00", 0.75)]
    assert gf.is_flipped(_pos(), gf.index_calibration(cal)) is False


def test_no_side_never_flipped():
    assert gf.is_flipped(_pos(side="NO"), gf.index_calibration(CALIB)) is False


def test_mtm_emits_close_flip_even_without_price_trigger():
    # цена не дала ни TAKE, ни CUT (60→58¢), но позиция открыта по ошибке
    fetch = lambda cid: {"outcomes": '["Yes","No"]', "outcomePrices": '["0.58","0.42"]'}
    sig = mtm.scan_open_positions([_pos()], fetch_fn=fetch, calib_rows=CALIB)
    assert len(sig) == 1
    s = sig[0]
    assert s["action"] == "CLOSE_FLIP"
    assert s["grok_raw"] == 0.04
    text = mtm._format_signal(s)
    assert "ЗАКРОЙ" in text
    assert "4%" in text and "69%" in text


def test_mtm_without_calib_unchanged():
    fetch = lambda cid: {"outcomes": '["Yes","No"]', "outcomePrices": '["0.58","0.42"]'}
    assert mtm.scan_open_positions([_pos()], fetch_fn=fetch) == []
