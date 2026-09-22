"""Наличные USDC не читались в бою: 04.09.2026 бот показывал «нет источника»,
хотя на кошельке лежало $123.49 — почти половина портфеля ($153.96 позиции +
$123.49 наличные = $277.45, ровно Portfolio на Polymarket).

Причина не в расчёте: _fetch_cash_value ходит по трём ЗАХАРДКОЖЕННЫМ публичным
RPC и игнорирует POLYGON_RPC. Публичные RPC массово блокируют IP датацентров, в
том числе GitHub Actions, поэтому в бою все три молчали.

Свой ключевой endpoint должен идти первым и браться из переменной."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_status as ds


def test_env_rpc_is_tried_first(monkeypatch):
    monkeypatch.setenv("POLYGON_RPC", "https://my-keyed-endpoint.example/abc")
    rpcs = ds._rpc_endpoints()
    assert rpcs[0] == "https://my-keyed-endpoint.example/abc"


def test_public_rpcs_remain_as_fallback(monkeypatch):
    monkeypatch.setenv("POLYGON_RPC", "https://my-keyed-endpoint.example/abc")
    rpcs = ds._rpc_endpoints()
    assert len(rpcs) > 1
    assert any("polygon-rpc.com" in r for r in rpcs)


def test_without_env_only_public(monkeypatch):
    monkeypatch.delenv("POLYGON_RPC", raising=False)
    rpcs = ds._rpc_endpoints()
    assert rpcs and all(r.startswith("https://") for r in rpcs)


def test_no_duplicate_when_env_matches_public(monkeypatch):
    monkeypatch.setenv("POLYGON_RPC", "https://polygon-rpc.com")
    rpcs = ds._rpc_endpoints()
    assert len(rpcs) == len(set(rpcs))
