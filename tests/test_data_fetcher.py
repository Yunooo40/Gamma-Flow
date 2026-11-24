"""Tests for data ingestion. The network is always mocked."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import config
import data_fetcher
from quant_engine import CHAIN_COLUMNS, bs_price, time_to_expiry

NY = ZoneInfo("America/New_York")
MONDAY = date(2026, 11, 23)
TUESDAY = date(2026, 11, 24)
MONDAY_MORNING = datetime(2026, 11, 23, 10, 0, tzinfo=NY)
BASE_URL = "https://api.massive.com"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MASSIVE_API_KEY", "")
    monkeypatch.setattr(config, "ALPACA_KEY", "")  # real keys from .env must never reach the network
    monkeypatch.setattr(config, "ALPACA_SECRET", "")
    monkeypatch.setattr(config, "MASSIVE_BASE_URL", BASE_URL)


# --- Fakes --------------------------------------------------------------------


def massive_contract(strike: float, kind: str, expiration: date, oi: int, iv: float | None) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "details": {"strike_price": strike, "contract_type": kind, "expiration_date": expiration.isoformat()},
        "open_interest": oi,
        "greeks": {"gamma": 0.05},  # provided by the source but ignored
        "underlying_asset": {"price": 600.0, "timeframe": "DELAYED"},
    }
    if iv is not None:
        contract["implied_volatility"] = iv
    return contract


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def install_massive(monkeypatch: pytest.MonkeyPatch, responses: list[FakeResponse]) -> list[dict[str, Any]]:
    """Replace ``requests.Session.get`` with a queue of canned responses."""
    calls: list[dict[str, Any]] = []

    def fake_get(session: Any, url: str, params: Any = None, timeout: Any = None) -> FakeResponse:
        calls.append({"url": url, "params": params, "headers": dict(session.headers)})
        return responses[len(calls) - 1]

    monkeypatch.setattr(data_fetcher.requests.Session, "get", fake_get)
    return calls


def two_massive_pages() -> list[FakeResponse]:
    page1 = {
        "results": [
            massive_contract(600, "call", MONDAY, 1500, 0.15),
            massive_contract(595, "put", MONDAY, 2000, 0.17),
        ],
        "next_url": f"{BASE_URL}/v3/snapshot/options/SPY?cursor=page2",
    }
    page2 = {
        "results": [
            massive_contract(605, "call", MONDAY, 900, 0.14),
            massive_contract(590, "put", MONDAY, 1200, None),  # deep ITM without IV
            massive_contract(600, "call", TUESDAY, 800, 0.16),  # not the nearest expiry
        ]
    }
    return [FakeResponse(200, page1), FakeResponse(200, page2)]


class FakeYfTicker:
    """Minimal stand-in for ``yfinance.Ticker``."""

    options = (MONDAY.isoformat(), TUESDAY.isoformat(), "2027-03-19")
    fast_info = SimpleNamespace(last_price=600.0)
    chain_requests: list[str] = []

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def option_chain(self, expiration: str) -> SimpleNamespace:
        FakeYfTicker.chain_requests.append(expiration)
        side = {
            "contractSymbol": ["X1", "X2"],
            "lastPrice": [1.0, 2.0],
            "strike": [600.0, 605.0],
            "openInterest": [100, float("nan")],
            "impliedVolatility": [0.2, 0.18],
        }
        return SimpleNamespace(calls=pd.DataFrame(side), puts=pd.DataFrame(side))


@pytest.fixture
def fake_yfinance(monkeypatch: pytest.MonkeyPatch) -> type[FakeYfTicker]:
    FakeYfTicker.chain_requests = []
    monkeypatch.setattr(data_fetcher.yf, "Ticker", FakeYfTicker)
    return FakeYfTicker


# --- Sources ------------------------------------------------------------------


def test_massive_and_yfinance_share_the_same_schema(
    monkeypatch: pytest.MonkeyPatch, fake_yfinance: type[FakeYfTicker]
) -> None:
    monkeypatch.setattr(config, "MASSIVE_API_KEY", "secret-key")
    calls = install_massive(monkeypatch, two_massive_pages())
    massive = data_fetcher.get_chain("SPY", "0dte", MONDAY_MORNING)

    assert massive.source == "massive" and massive.timeframe == "DELAYED" and massive.spot == 600.0
    assert len(calls) == 2 and calls[1]["url"].endswith("cursor=page2")
    assert calls[0]["params"]["expiration_date.gte"] == MONDAY.isoformat()
    assert all(c["headers"]["Authorization"] == "Bearer secret-key" for c in calls)
    assert all("secret-key" not in c["url"] for c in calls)
    assert massive.expirations == [MONDAY] and not massive.expiry_fallback
    assert len(massive.chain) == 4  # the Tuesday contract is dropped

    monkeypatch.setattr(config, "MASSIVE_API_KEY", "")
    yfin = data_fetcher.get_chain("QQQ", "0dte", MONDAY_MORNING)
    assert yfin.source == "yfinance" and yfin.timeframe == "DELAYED"
    assert data_fetcher.YFINANCE_WARNING in yfin.warnings

    for snapshot in (massive, yfin):
        assert tuple(snapshot.chain.columns) == CHAIN_COLUMNS
        assert all(type(d) is date for d in snapshot.chain["expiration"])
    pd.testing.assert_series_equal(massive.chain.dtypes, yfin.chain.dtypes)


def test_falls_back_to_yfinance_on_403(
    monkeypatch: pytest.MonkeyPatch, fake_yfinance: type[FakeYfTicker]
) -> None:
    monkeypatch.setattr(config, "MASSIVE_API_KEY", "no-options-plan")
    install_massive(monkeypatch, [FakeResponse(403, {"status": "NOT_AUTHORIZED"})])
    snapshot = data_fetcher.get_chain("SPY", "0dte", MONDAY_MORNING)
    assert snapshot.source == "yfinance"
    assert any("Massive unavailable (HTTP 403" in w for w in snapshot.warnings)


def test_raises_when_every_source_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(symbol: str) -> None:
        raise ConnectionError("offline")

    monkeypatch.setattr(data_fetcher.yf, "Ticker", broken)
    with pytest.raises(data_fetcher.DataUnavailableError):
        data_fetcher.get_chain("SPY", "0dte", MONDAY_MORNING)


# --- Expiry selection ---------------------------------------------------------


def test_0dte_uses_today_before_the_close() -> None:
    assert data_fetcher.select_expirations([MONDAY, TUESDAY], "0dte", MONDAY_MORNING) == ([MONDAY], False, [])


def test_0dte_falls_back_after_the_close() -> None:
    after_close = datetime(2026, 11, 23, 16, 30, tzinfo=NY)
    expirations, fallback, warnings = data_fetcher.select_expirations([MONDAY, TUESDAY], "0dte", after_close)
    assert expirations == [TUESDAY] and fallback
    assert warnings and TUESDAY.isoformat() in warnings[0]


def test_0dte_falls_back_on_weekends() -> None:
    saturday = datetime(2026, 11, 21, 12, 0, tzinfo=NY)
    friday = date(2026, 11, 20)
    assert data_fetcher.select_expirations([friday, MONDAY], "0dte", saturday) == (
        [MONDAY],
        True,
        ["No 0DTE expiry today, using 2026-11-23"],
    )


def test_all_keeps_live_expiries_within_max_dte() -> None:
    far = MONDAY + timedelta(days=config.MAX_DTE_ALL + 1)
    expirations, fallback, _ = data_fetcher.select_expirations(
        [date(2026, 11, 20), MONDAY, TUESDAY, far], "all", MONDAY_MORNING
    )
    assert expirations == [MONDAY, TUESDAY] and not fallback


# --- Cache --------------------------------------------------------------------


def test_cache_round_trip_and_expiry(fake_yfinance: type[FakeYfTicker]) -> None:
    first = data_fetcher.get_chain("SPY", "all", MONDAY_MORNING)
    requests_after_first = len(fake_yfinance.chain_requests)

    second = data_fetcher.get_chain("SPY", "all", MONDAY_MORNING + timedelta(minutes=5))
    assert second.cached and not first.cached
    assert len(fake_yfinance.chain_requests) == requests_after_first
    pd.testing.assert_frame_equal(first.chain, second.chain)
    assert second.expirations == first.expirations and second.spot == first.spot

    stale = data_fetcher.get_chain("SPY", "all", MONDAY_MORNING + timedelta(seconds=config.CACHE_TTL_SECONDS))
    assert not stale.cached

    # Monday's expiry closes at 16:00: a younger cache entry must not survive it.
    closing = datetime(2026, 11, 23, 15, 55, tzinfo=NY)
    data_fetcher.get_chain("SPY", "0dte", closing)
    assert not data_fetcher.get_chain("SPY", "0dte", closing + timedelta(minutes=6)).cached


def test_alpaca_solves_iv_from_option_prices(
    monkeypatch: pytest.MonkeyPatch, fake_yfinance: type[FakeYfTicker]
) -> None:
    monkeypatch.setattr(config, "ALPACA_KEY", "k")
    monkeypatch.setattr(config, "ALPACA_SECRET", "s")
    spot, r, q = 601.5, config.RISK_FREE_RATE, config.DIVIDEND_YIELDS["SPY"]
    t = time_to_expiry(pd.Series([MONDAY]), MONDAY_MORNING)[0]
    call, put = bs_price(spot, 600, t, 0.15, r, q, "call"), bs_price(spot, 595, t, 0.17, r, q, "put")
    snaps1 = {
        "snapshots": {"SPY261123C00600000": {"latestQuote": {"bp": call - 0.05, "ap": call + 0.05}}},
        "next_page_token": "p2",
    }
    snaps2 = {
        "snapshots": {
            # No bid: the book is ignored and the last trade is used.
            "SPY261123P00595000": {"latestQuote": {"bp": 0, "ap": put + 1}, "latestTrade": {"p": put}},
            "SPY261123C00650000": {"latestQuote": {"bp": 0, "ap": 0}},  # no price at all
            "SPY261124C00600000": {"latestQuote": {"bp": 1.0, "ap": 1.2}},  # not the nearest expiry
        }
    }
    contracts = {"option_contracts": [
        {"symbol": "SPY261123C00600000", "open_interest": "1500"},
        {"symbol": "SPY261123P00595000", "open_interest": "2000"},
    ]}
    responses = [FakeResponse(200, p) for p in (snaps1, snaps2, contracts, {"trade": {"p": 601.5}})]
    calls = install_massive(monkeypatch, responses)

    snap = data_fetcher.get_chain("SPY", "0dte", MONDAY_MORNING)

    assert snap.source == "alpaca" and snap.spot == 601.5 and snap.expirations == [MONDAY]
    assert calls[1]["params"]["page_token"] == "p2"
    assert calls[0]["headers"]["APCA-API-KEY-ID"] == "k"
    assert sorted(zip(snap.chain["strike"], snap.chain["open_interest"])) == [(595.0, 2000), (600.0, 1500), (650.0, 0)]
    iv = dict(zip(snap.chain["strike"], snap.chain["implied_volatility"]))
    assert iv[600.0] == pytest.approx(0.15, rel=1e-6) and iv[595.0] == pytest.approx(0.17, rel=1e-6)
    assert math.isnan(iv[650.0])
    assert tuple(snap.chain.columns) == CHAIN_COLUMNS
