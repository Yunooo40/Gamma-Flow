"""Tests for the Flask API, with the data layer mocked."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest
from flask.testing import FlaskClient

import app as app_module
import data_fetcher


def strict_json(text: str) -> Any:
    """Parse JSON, rejecting the non-standard NaN / Infinity tokens."""

    def reject(token: str) -> None:
        raise ValueError(f"invalid JSON constant {token}")

    return json.loads(text, parse_constant=reject)


def fake_snapshot(ticker: str, expiry_filter: str, now: datetime | None = None) -> data_fetcher.ChainSnapshot:
    expiration = date.today() + timedelta(days=10)
    chain = pd.DataFrame(
        {
            "strike": [590.0, 595.0, 600.0, 605.0, 610.0, 600.0],
            "option_type": ["put", "put", "call", "call", "call", "put"],
            "expiration": [expiration] * 6,
            "open_interest": [5000, 3000, 4000, 6000, 2000, 1000],
            "implied_volatility": [0.18, 0.17, 0.15, 0.14, 0.14, float("nan")],
        }
    )
    return data_fetcher.ChainSnapshot(
        ticker=ticker,
        spot=600.0,
        chain=chain,
        source="yfinance",
        timeframe="DELAYED",
        fetched_at=datetime.now(timezone.utc),
        expirations=[expiration],
        expiry_fallback=False,
        warnings=[data_fetcher.YFINANCE_WARNING],
    )


@pytest.fixture
def client() -> FlaskClient:
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture(autouse=True)
def fixed_realized_vol(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daily history must never be downloaded during tests."""
    monkeypatch.setattr(data_fetcher, "get_realized_vol", lambda ticker: 0.12)


def test_health(client: FlaskClient) -> None:
    assert client.get("/api/health").get_json() == {"status": "ok"}


@pytest.mark.parametrize("query", ["ticker=TSLA&expiry=0dte", "ticker=SPY&expiry=weekly", "expiry=0dte"])
def test_invalid_parameters_return_400(client: FlaskClient, query: str) -> None:
    response = client.get(f"/api/gex?{query}")
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_returns_502_when_every_source_fails(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise data_fetcher.DataUnavailableError("down")

    monkeypatch.setattr(data_fetcher, "get_chain", unavailable)
    response = client.get("/api/gex?ticker=SPY&expiry=0dte")
    assert response.status_code == 502
    assert response.get_json() == {"error": "Market data unavailable"}


def test_gex_payload(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_fetcher, "get_chain", fake_snapshot)
    response = client.get("/api/gex?ticker=spy&expiry=all")
    assert response.status_code == 200

    payload = strict_json(response.get_data(as_text=True))
    assert payload["ticker"] == "SPY" and payload["spot"] == 600.0
    assert payload["levels"]["call_wall"] == 605.0 and payload["levels"]["put_wall"] == 590.0
    assert payload["fetched_at"].endswith("Z")
    assert payload["strikes"] and set(payload["strikes"][0]) == {"strike", "call_gex", "put_gex", "net_gex", "net_vex"}
    assert payload["realized_vol"] == 0.12 and payload["atm_iv"] > 0
    move = payload["expected_move"]
    assert payload["em_upper"] == pytest.approx(600.0 + move)
    assert payload["em_lower"] == pytest.approx(600.0 - move)
    assert isinstance(payload["total_charm"], float) and payload["total_charm"] != 0
    assert payload["expected_move_pct"] == pytest.approx(move / 600.0 * 100)


def test_missing_realized_vol_is_flagged(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_fetcher, "get_chain", fake_snapshot)
    monkeypatch.setattr(data_fetcher, "get_realized_vol", lambda ticker: None)
    payload = strict_json(client.get("/api/gex?ticker=SPY&expiry=all").get_data(as_text=True))
    assert payload["realized_vol"] is None
    assert "Realized volatility unavailable (no daily history)" in payload["warnings"]


def test_nan_and_infinity_become_null(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_fetcher, "get_chain", fake_snapshot)
    monkeypatch.setattr(
        app_module.quant_engine,
        "build_report",
        lambda *args: {
            "spot": 600.0,
            "levels": {"call_wall": float("nan"), "put_wall": 590.0, "zero_gamma": None},
            "totals": {"net_gex": float("inf"), "net_vex": float("-inf")},
            "total_charm": float("nan"),
            "display_range": {"min": 540.0, "max": 660.0},
            "strikes": [{"strike": 600.0, "call_gex": float("nan"), "put_gex": 0.0, "net_gex": 0.0, "net_vex": 0.0}],
            "atm_iv": float("nan"),
            "expected_move": float("inf"),
            "expected_move_pct": None,
            "em_upper": None,
            "em_lower": None,
        },
    )
    payload = strict_json(client.get("/api/gex?ticker=SPY&expiry=0dte").get_data(as_text=True))
    assert payload["atm_iv"] is None and payload["expected_move"] is None
    assert payload["levels"]["call_wall"] is None
    assert payload["totals"] == {"net_gex": None, "net_vex": None} and payload["total_charm"] is None
    assert payload["strikes"][0]["call_gex"] is None


def test_vanna_surface(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[tuple[str, str]] = []

    def get_chain(ticker: str, expiry: str) -> data_fetcher.ChainSnapshot:
        requested.append((ticker, expiry))
        return fake_snapshot(ticker, expiry)

    monkeypatch.setattr(data_fetcher, "get_chain", get_chain)
    response = client.get("/api/vanna-surface?ticker=spy&expiry=all")
    assert response.status_code == 200
    assert requested == [("SPY", "all")]  # the expiry parameter is read, not the 0dte default

    payload = strict_json(response.get_data(as_text=True))
    assert set(payload) == {"x", "y", "z"}
    assert len(payload["x"]) == len(payload["y"]) == len(payload["z"]) == 20
    assert all(len(row) == 20 for row in payload["z"])
    assert payload["x"][0] == pytest.approx(540.0) and payload["x"][-1] == pytest.approx(660.0)
    assert payload["y"][0] == pytest.approx(0.8) and payload["y"][-1] == pytest.approx(1.2)


@pytest.mark.parametrize("query", ["ticker=TSLA&expiry=all", "ticker=SPY&expiry=weekly"])
def test_vanna_surface_rejects_invalid_parameters(client: FlaskClient, query: str) -> None:
    assert client.get(f"/api/vanna-surface?{query}").status_code == 400
