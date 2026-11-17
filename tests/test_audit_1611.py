"""Аудит 16.11.2026 — три тихие поломки.

1. Брифинг 16.11 не пришёл: сессия по расписанию упёрлась в недельный лимит.
   Расписание отчиталось «доставлено», воркфлоу зелёные, сторож молчал —
   оператор не мог отличить «очередь пуста» от «разбор не запускался».
2. briefing score считал открытый рынок с ценой ≥99% зарезолвленным: рынок
   «… by December 31», упавший до 1% в ноябре, попал бы в итог до срока.
3. historical_collector и backtest ходили в Data API v1 (выключен 02.11):
   сборщик каждые 10 минут получал пустоту и оставался зелёным.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brief_watch as bw
import briefing as br

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 1. сторож брифинга ──────────────────────────────────────────────────────

def test_msk_date_used():
    # 22:30 UTC 15.11 — в Москве уже 16.11
    assert bw.msk_date(datetime(2026, 11, 15, 22, 30, tzinfo=timezone.utc)) == "2026-11-16"


def test_missing_brief_alerts(tmp_path):
    (tmp_path / "2026-11-15.md").write_text("x")
    msg = bw.check(str(tmp_path), datetime(2026, 11, 16, 8, tzinfo=timezone.utc))
    assert msg and "16.11" in msg and "не пришёл" in msg


def test_present_brief_silent(tmp_path):
    (tmp_path / "2026-11-16.md").write_text("x")
    assert bw.check(str(tmp_path), datetime(2026, 11, 16, 8, tzinfo=timezone.utc)) is None


def test_second_brief_of_day_counts(tmp_path):
    (tmp_path / "2026-11-16-2.md").write_text("x")
    assert bw.check(str(tmp_path), datetime(2026, 11, 16, 8, tzinfo=timezone.utc)) is None


def test_brief_watch_scheduled_after_brief():
    yml = open(os.path.join(ROOT, ".github/workflows/brief_watch.yml"), encoding="utf-8").read()
    assert "brief_watch.py" in yml and "cron" in yml
    # 17.11: брифинг три раза в неделю — сторож проверяет те же дни
    assert "'10 8 * * 1,3,5'" in yml


# ── 2. резолв только после закрытия или срока ───────────────────────────────

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def _mkt(yes, closed=False, end="2026-12-31T00:00:00Z"):
    return {"outcomes": '["Yes","No"]', "outcomePrices": f'["{yes}","{1-yes}"]',
            "closed": closed, "endDate": end}


def test_open_market_before_deadline_not_resolved():
    assert br.outcome_of(_mkt(0.005), NOW) is None


def test_closed_market_resolved():
    assert br.outcome_of(_mkt(0.0, closed=True), NOW) == 0
    assert br.outcome_of(_mkt(1.0, closed=True), NOW) == 1


def test_past_deadline_decisive_price_resolved():
    # Лейбористы НЗ: срок 08.11 прошёл, цена 0.5%, рынок ещё не закрыт оракулом
    assert br.outcome_of(_mkt(0.0055, end="2026-11-08T00:00:00Z"), NOW) == 0


def test_past_deadline_undecided_not_resolved():
    assert br.outcome_of(_mkt(0.8, end="2026-11-08T00:00:00Z"), NOW) is None


# ── 3. в боевом расписании не осталось v1 ───────────────────────────────────

def test_scheduled_legacy_uses_v2():
    for f in ("historical_collector.py", "backtest.py"):
        src = open(os.path.join(ROOT, f), encoding="utf-8").read()
        assert "DATA_API_URL}/" not in src, f
        assert "data_api" in src, f


def test_historical_trades_parsed_from_v2(monkeypatch):
    import data_api
    import historical_collector as hc
    now_ts = int(datetime.now(timezone.utc).timestamp())
    rows = [data_api.legacy_row({"transaction_hash": "0xt", "proxy_wallet": "0xw",
                                 "timestamp": now_ts - 60, "outcome": "Yes",
                                 "price": 0.5, "size": 10 * hc.MIN_TRADE_AMOUNT})]
    monkeypatch.setattr(data_api, "fetch", lambda route, params=None, **kw: rows)
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    got = hc.fetch_recent_trades("0xc")
    assert len(got) == 1 and got[0]["wallet"] == "0xw" and got[0]["trade_hash"] == "0xt"


def test_historical_api_failure_counted(monkeypatch):
    import data_api
    import historical_collector as hc
    monkeypatch.setattr(data_api, "fetch", lambda route, params=None, **kw: None)
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    before = hc.API_ERRORS
    assert hc.fetch_recent_trades("0xc") == []
    assert hc.API_ERRORS == before + 1


# ── 4. сборщик истории: окно и резолв (проверка 17.11 после аудита) ─────────
# После перевода на v2 прогон был зелёным, но «+0 trades» на 125 рынках:
# окно 15 минут при фактическом запуске раз в ~8 часов (GitHub душит cron
# */10). И за всё время 0 резолвов из 193: поиск по `conditionId` Gamma
# игнорирует и отдаёт 20 случайных рынков — сборщик брал первый, чужой.

import sqlite3  # noqa: E402


def _log_db(tmp_path, run_at):
    db = tmp_path / "h.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE collection_log (id INTEGER PRIMARY KEY, run_at TEXT, "
                 "markets_checked INTEGER, new_trades INTEGER, new_markets INTEGER, "
                 "resolutions_found INTEGER, duration_seconds REAL)")
    if run_at:
        conn.execute("INSERT INTO collection_log (run_at) VALUES (?)", (run_at,))
    conn.commit()
    return conn


def test_window_covers_gap_since_last_run(tmp_path):
    import historical_collector as hc
    now = datetime(2026, 11, 17, 9, 54, tzinfo=timezone.utc)
    conn = _log_db(tmp_path, "2026-11-17T01:55:16+00:00")
    w = hc.collection_window_minutes(conn, now)
    assert 8 * 60 <= w <= 8 * 60 + 15                 # ~8ч разрыв + запас


def test_window_capped_and_has_floor(tmp_path):
    import historical_collector as hc
    now = datetime(2026, 11, 17, 9, 54, tzinfo=timezone.utc)
    assert hc.collection_window_minutes(_log_db(tmp_path, None), now) == hc.COLLECTION_WINDOW_MINUTES
    db2 = tmp_path / "x"; db2.mkdir()
    old = _log_db(db2, "2026-11-01T00:00:00+00:00")
    assert hc.collection_window_minutes(old, now) == hc.MAX_WINDOW_MINUTES


def test_resolution_lookup_rejects_foreign_market(monkeypatch):
    import historical_collector as hc
    import resolution_tracker as rt
    monkeypatch.setattr(hc.requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(rt, "fetch_market_by_condition_id",
                        lambda cid: {"conditionId": "0xOTHER", "closed": True})
    assert hc.lookup_market("0xmine", slug="") is None


def test_resolution_lookup_accepts_own_market(monkeypatch):
    import historical_collector as hc
    import resolution_tracker as rt
    monkeypatch.setattr(hc.requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(rt, "fetch_market_by_condition_id",
                        lambda cid: {"conditionId": cid, "closed": True})
    assert hc.lookup_market("0xmine", slug="")["conditionId"] == "0xmine"


def test_resolution_does_not_require_resolution_source():
    import historical_collector as hc
    m = {"closed": True, "resolutionSource": "", "outcomes": '["Yes","No"]',
         "outcomePrices": '["0","1"]'}
    assert hc.winning_outcome(m) == "No"
    assert hc.winning_outcome(dict(m, closed=False)) is None
