"""Предохранитель на исчерпанные кредиты xAI + журнал расхода estimator'а."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import ai_context


class _Resp:
    def __init__(self, status, body="", data=None):
        self.status_code = status
        self.text = body
        self._data = data or {}

    def json(self):
        return self._data


def _setup(monkeypatch, tmp_path, responses):
    calls = []

    def fake_post(*a, **kw):
        calls.append(kw.get("json"))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(ai_context, "XAI_API_KEY", "t")
    monkeypatch.setattr(ai_context, "_BILLING_BLOCKED", False)
    monkeypatch.setattr(ai_context, "USAGE_LOG", str(tmp_path / "usage.jsonl"))
    return calls


def test_billing_block_stops_further_calls(monkeypatch, tmp_path):
    blocked = _Resp(403, "Your team has either used all available credits "
                         "or reached its monthly spending limit.")
    calls = _setup(monkeypatch, tmp_path, [blocked])
    assert ai_context.estimate_probability("Q1?") is None
    assert ai_context.estimate_probability("Q2?") is None
    assert ai_context.estimate_probability("Q3?", use_search=False) is None
    assert len(calls) == 1


def test_other_errors_do_not_trip_breaker(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, [_Resp(403, "forbidden"), _Resp(500, "oops")])
    ai_context.estimate_probability("Q1?")
    ai_context.estimate_probability("Q2?")
    assert len(calls) == 2
    assert ai_context._BILLING_BLOCKED is False


def test_success_is_parsed_and_usage_logged(monkeypatch, tmp_path):
    data = {
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "PROB: 30\nCONF: medium\nWHY: факт"}]}],
        "usage": {"input_tokens": 1000, "output_tokens": 200},
    }
    _setup(monkeypatch, tmp_path, [_Resp(200, data=data)])
    est = ai_context.estimate_probability("Q?", use_search=False)
    assert est["prob"] == 0.3 and est["conf"] == "medium"
    rows = [json.loads(l) for l in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert rows == [{**rows[0], "call": "estimator_screen", "status": 200,
                     "usage": {"input_tokens": 1000, "output_tokens": 200}}]
