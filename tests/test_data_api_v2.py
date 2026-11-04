"""Переезд на Polymarket Data API v2 (v1 выключен 02.11.2026).

С 02.11 все v1-маршруты отвечают «Data API v1 has been retired»: кошелёк в
дневном статусе, сверка входов (fill_matcher), синхронизация позиций,
копи-трейд и скауты китов молча получали пустоту. v2: префикс /v2, ответ в
{"data", "pagination"}, курсор вместо offset, snake_case вместо camelCase.
Адаптер data_api отдаёт строки в прежнем camelCase — логика модулей не трогается.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_api as da


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._b = body
        self.text = str(body)

    def json(self):
        return self._b


def _fake(pages, calls):
    """pages: {cursor|None: (rows, next_cursor)}"""
    def get(url, params=None, timeout=None):
        calls.append((url, dict(params or {})))
        rows, nxt = pages[(params or {}).get("cursor")]
        return _Resp(200, {"data": rows, "pagination": {"next_cursor": nxt}})
    return get


def test_route_and_params_translated():
    calls = []
    get = _fake({None: ([], None)}, calls)
    da.fetch("activity", {"user": "w", "limit": 500, "offset": 0,
                          "sortBy": "TIMESTAMP", "sortDirection": "DESC",
                          "market": "0xc"}, get=get)
    url, p = calls[0]
    assert url.endswith("/v2/activity")
    assert "offset" not in p and "market" not in p      # v2 отвечает 400 на них
    assert p["condition"] == "0xc"
    assert p["sort_by"] == "TIMESTAMP" and p["sort_direction"] == "DESC"


def test_cursor_paging_until_null():
    calls = []
    get = _fake({None: ([{"condition_id": "a"}], "c1"),
                 "c1": ([{"condition_id": "b"}], None)}, calls)
    rows = da.fetch("activity", {"user": "w"}, max_rows=1000, get=get)
    assert [r["conditionId"] for r in rows] == ["a", "b"]
    assert calls[1][1]["cursor"] == "c1"


def test_paging_stops_at_max_rows():
    calls = []
    get = _fake({None: ([{"x": 1}] * 3, "c1"), "c1": ([{"x": 2}] * 3, None)}, calls)
    assert len(da.fetch("trades", {}, max_rows=3, get=get)) == 3
    assert len(calls) == 1


def test_snake_rows_get_camel_aliases():
    r = da.legacy_row({"condition_id": "c", "usdc_size": 1.5,
                       "transaction_hash": "0xt", "proxy_wallet": "w"})
    assert r["conditionId"] == "c" and r["usdcSize"] == 1.5
    assert r["transactionHash"] == "0xt" and r["proxyWallet"] == "w"
    assert r["condition_id"] == "c"                    # исходное поле сохраняется


def test_position_row_has_v1_fields():
    r = da.legacy_position({"current_size": 20.0, "avg_price": 0.5,
                            "current_price": 0.6, "current_value": 12.0,
                            "entry_cost_usdc": 10.0, "unrealized_pnl": 2.0,
                            "percent_pnl": 20.0, "redeemable": False,
                            "end_date": "2026-11-03", "title": "T"})
    assert r["size"] == 20.0 and r["avgPrice"] == 0.5 and r["curPrice"] == 0.6
    assert r["currentValue"] == 12.0 and r["initialValue"] == 10.0
    assert r["cashPnl"] == 2.0 and r["percentPnl"] == 20.0
    assert r["endDate"] == "2026-11-03" and r["redeemable"] is False


def test_leaderboard_row_has_v1_fields():
    r = da.legacy_leader({"user_id": "0xw", "user_name": "n", "pnl": 5.0, "volume": 9.0})
    assert r["proxyWallet"] == "0xw" and r["userName"] == "n" and r["vol"] == 9.0


def test_http_error_is_none_and_recorded():
    def get(url, params=None, timeout=None):
        return _Resp(410, {"error": "Data API v1 has been retired"})
    assert da.fetch("positions", {"user": "w"}, get=get) is None
    assert da.LAST_ERROR and "410" in da.LAST_ERROR


def test_value_returns_object():
    def get(url, params=None, timeout=None):
        return _Resp(200, {"data": {"proxy_wallet": "w", "value": 12.5}})
    assert da.fetch("value", {"user": "w"}, get=get)["value"] == 12.5


def test_positions_merges_open_and_redeemable():
    def get(url, params=None, timeout=None):
        st = params.get("status")
        rows = {"OPEN": [{"token_id": "1", "current_size": 5}],
                "REDEEMABLE": [{"token_id": "2", "current_size": 3, "redeemable": True},
                               {"token_id": "1", "current_size": 5}]}[st]
        return _Resp(200, {"data": rows, "pagination": {"next_cursor": None}})
    rows = da.wallet_positions("w", get=get)
    assert sorted(r["size"] for r in rows) == [3, 5]          # без дублей


def test_wallet_positions_none_when_api_down():
    def get(url, params=None, timeout=None):
        return _Resp(500, {})
    assert da.wallet_positions("w", get=get) is None


def test_no_v1_urls_left_in_live_code():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    live = ["fill_matcher.py", "daily_status.py", "collector.py", "whale_scout.py",
            "leaderboard_scout.py", "copy_monitor.py", "position_sync.py"]
    for f in live:
        src = open(os.path.join(root, f), encoding="utf-8").read()
        assert "DATA_API}/" not in src and "DATA_API_URL}/" not in src, f
        assert "/v1/leaderboard" not in src, f


# ── закрытые позиции (status=CLOSED) — новое в v2 ───────────────────────────
# v1 /positions терял проданные и выплаченные позиции: «Реализовано за 7д»
# 04.11 показало −$86 из одних проигрышей, а выигрыши Texas/Nevada/Maine/Ohio
# (забраны) в блок не попали. И журнал не узнавал о продаже, если оператор
# закрыл ВСЁ: «пустой портфель — не повод закрывать».

from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def _closed(title, pnl, ts, cid="c1", end="2026-12-31"):
    return da.legacy_position({"status": "CLOSED", "title": title, "condition_id": cid,
                               "current_size": 0.006, "current_value": 0.0001,
                               "realized_pnl": pnl, "total_pnl": pnl,
                               "last_event_at": ts, "end_date": end, "redeemable": False})


def test_wallet_positions_can_include_closed():
    def get(url, params=None, timeout=None):
        rows = {"OPEN": [], "REDEEMABLE": [],
                "CLOSED": [{"token_id": "9", "status": "CLOSED", "current_size": 0.01}]}[params["status"]]
        return _Resp(200, {"data": rows, "pagination": {"next_cursor": None}})
    assert da.wallet_positions("w", get=get) == []
    assert len(da.wallet_positions("w", include_closed=True, get=get)) == 1


def test_closed_dust_is_not_open():
    import daily_status as ds
    assert ds.filter_open_positions([_closed("Vučić", -54.5, int(NOW.timestamp()))]) == []


def test_realized_counts_closed_by_exit_time():
    import daily_status as ds
    ts = int(NOW.timestamp()) - 86400
    pos = [_closed("Will the Democrats win the Texas Senate race?", 17.9, ts, end="2026-11-03"),
           _closed("Will J.D. Vance win the 2028 nomination?", -3.3, ts, cid="c2", end="2028-11-08"),
           _closed("Old", 5.0, ts - 30 * 86400, cid="c3", end="2026-11-03")]
    block = ds.realized_block(ds.resolved_positions(pos, now=NOW), now=NOW)
    assert "$+15" in block and "(2 позиц.)" in block       # Vance — продан, хоть рынок до 2028


def test_sold_detected_when_everything_closed():
    import position_sync as ps
    rows = [{"condition_id": "c1", "status": "open", "stake_actual": 30},
            {"condition_id": "zz", "status": "open", "stake_actual": 30}]
    rows, n = ps.apply_sold(rows, [_closed("T", 1.0, int(NOW.timestamp()))])
    assert n == 1 and rows[0]["status"] == "closed" and rows[1]["status"] == "open"


def test_closed_dust_not_pending_payout():
    # 04.11: «🏆 К выплате $0 (26 поз.)» — остатки 0.009 доли у CLOSED-позиций
    import daily_status as ds
    p = _closed("Will PSG win the 2025–26 Champions League?", 30.0, int(NOW.timestamp()))
    p["redeemable"] = True
    assert ds.pending_payout_block([p]) is None


def test_no_open_positions_says_so():
    import daily_status as ds
    msg = ds.build_status_from_wallet([_closed("T", 1.0, int(NOW.timestamp()))], [])
    assert "позиции зреют" not in msg and "Открытых позиций нет" in msg
