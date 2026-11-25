"""Tests for the pure quantitative engine."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import config
import quant_engine as qe

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)  # Monday 10:00 New York
EXPIRY = date(2026, 12, 23)  # 30 days out
SPOT = 100.0


def make_chain(rows: list[tuple[float, str, int, float]], expiration: date = EXPIRY) -> pd.DataFrame:
    """Build a chain from (strike, option_type, open_interest, implied_volatility) rows."""
    strikes, types, ois, ivs = zip(*rows)
    return pd.DataFrame(
        {
            "strike": [float(s) for s in strikes],
            "option_type": list(types),
            "expiration": [expiration] * len(rows),
            "open_interest": list(ois),
            "implied_volatility": list(ivs),
        }
    )


def test_black_scholes_reference_values() -> None:
    assert qe.bs_gamma(100, 100, 1, 0.20, 0.05, 0.0) == pytest.approx(0.018762, abs=1e-5)
    assert qe.bs_vanna(100, 100, 1, 0.20, 0.05, 0.0) == pytest.approx(-0.281430, abs=1e-5)


def test_gamma_identical_for_call_and_put() -> None:
    chain = make_chain([(100, "call", 10, 0.2), (100, "put", 10, 0.2)])
    exposures = qe.compute_exposures(chain, SPOT, "SPY", NOW)
    assert exposures["gamma"][0] == exposures["gamma"][1]
    assert exposures["gex"][0] == -exposures["gex"][1]


def test_expiry_in_one_minute_stays_finite() -> None:
    now = datetime(2026, 11, 23, 15, 59, tzinfo=NY)
    chain = make_chain([(100, "call", 10, 0.2), (101, "put", 10, 0.2)], expiration=now.date())
    t = qe.time_to_expiry(chain["expiration"], now)
    assert t == pytest.approx(config.MIN_T_MINUTES * 60 / qe.SECONDS_PER_YEAR)

    report = qe.build_report(chain, SPOT, "SPY", "0dte", now)
    values = [*report["totals"].values(), *(v for row in report["strikes"] for v in row.values())]
    values += [report[key] for key in ("atm_iv", "expected_move", "expected_move_upper", "expected_move_lower")]
    assert report["strikes"] and all(math.isfinite(v) for v in values)


def test_exposure_signs() -> None:
    calls = qe.build_report(make_chain([(100, "call", 10, 0.2)]), SPOT, "SPY", "all", NOW)
    puts = qe.build_report(make_chain([(100, "put", 10, 0.2)]), SPOT, "SPY", "all", NOW)
    assert calls["totals"]["net_gex"] > 0
    assert puts["totals"]["net_gex"] < 0


def test_call_and_put_walls() -> None:
    chain = make_chain(
        [
            (100, "call", 100, 0.2),
            (105, "call", 5000, 0.2),
            (110, "call", 200, 0.2),
            (95, "put", 8000, 0.2),
            (90, "put", 100, 0.2),
        ]
    )
    levels = qe.build_report(chain, SPOT, "SPY", "all", NOW)["levels"]
    assert levels["call_wall"] == 105.0
    assert levels["put_wall"] == 95.0


def test_zero_gamma_between_put_and_call_concentrations(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = make_chain([(95, "put", 10_000, 0.2), (105, "call", 10_000, 0.2)])
    report = qe.build_report(chain, SPOT, "SPY", "all", NOW)
    assert 95.0 < report["levels"]["zero_gamma"] < 105.0

    # The profile at S' = spot matches the per-strike total, whatever the block size.
    clean = qe.clean_chain(chain, SPOT, NOW)
    grid = np.array([90.0, SPOT, 110.0])
    profile = qe.gex_profile(clean, grid, "SPY", NOW)
    assert profile[1] == pytest.approx(report["totals"]["net_gex"])
    monkeypatch.setattr(config, "ZERO_GAMMA_CHUNK_SIZE", 1)
    assert qe.gex_profile(clean, grid, "SPY", NOW) == pytest.approx(profile)


def test_zero_gamma_is_none_for_calls_only() -> None:
    chain = make_chain([(95, "call", 1000, 0.2), (105, "call", 1000, 0.2)])
    assert qe.build_report(chain, SPOT, "SPY", "all", NOW)["levels"]["zero_gamma"] is None


def test_clean_chain_drops_invalid_rows() -> None:
    chain = make_chain(
        [
            (100, "call", 10, 0.2),  # the only valid row
            (101, "call", 10, 5.0),  # IV too high
            (102, "call", 10, 0.001),  # IV too low
            (103, "put", 0, 0.2),  # no open interest
            (104, "put", 10, float("nan")),  # missing IV
            (200, "call", 10, 0.2),  # strike outside the compute range
        ]
    )
    expired = make_chain([(100, "put", 10, 0.2)], expiration=date(2026, 11, 20))
    cleaned = qe.clean_chain(pd.concat([chain, expired], ignore_index=True), SPOT, NOW)
    assert cleaned["strike"].tolist() == [100.0]
    assert cleaned["option_type"].tolist() == ["call"]


@pytest.mark.parametrize("option_type", ["call", "put"])
@pytest.mark.parametrize("strike", [90.0, 100.0, 110.0])
def test_calculate_iv_inverts_bs_price(option_type: str, strike: float) -> None:
    t, r, q = 30 / 365, 0.04, 0.012
    price = qe.bs_price(SPOT, strike, t, 0.25, r, q, option_type)
    assert qe.calculate_iv(price, SPOT, strike, t, r, option_type, q) == pytest.approx(0.25, rel=1e-6)


@pytest.mark.parametrize(
    ("price", "strike", "option_type"),
    [
        (5.0, 90.0, "call"),  # below intrinsic value (10): brentq would crash
        (10.0, 110.0, "put"),  # exactly intrinsic value
        (math.nan, 100.0, "call"),  # no price
        (150.0, 100.0, "call"),  # above the price at sigma = 1000 %: no root in the bounds
    ],
)
def test_calculate_iv_returns_nan_when_unsolvable(price: float, strike: float, option_type: str) -> None:
    assert math.isnan(qe.calculate_iv(price, SPOT, strike, 30 / 365, 0.04, option_type))


# --- Volatility analysis ------------------------------------------------------


@pytest.mark.parametrize(
    ("expiration", "now", "expected"),
    [
        (date(2026, 11, 23), datetime(2026, 11, 23, 10, 0, tzinfo=NY), 6 / 6.5),  # 0DTE during the session
        (date(2026, 11, 23), datetime(2026, 11, 23, 8, 0, tzinfo=NY), 1.0),  # 0DTE before the open
        (date(2026, 11, 24), datetime(2026, 11, 23, 17, 0, tzinfo=NY), 1.0),  # next day, after the close
        (date(2026, 11, 30), datetime(2026, 11, 28, 12, 0, tzinfo=NY), 1.0),  # Saturday to Monday
        (date(2026, 12, 4), datetime(2026, 11, 30, 10, 0, tzinfo=NY), 6 / 6.5 + 4),  # Monday to Friday
        (date(2026, 11, 23), datetime(2026, 11, 23, 15, 58, tzinfo=NY), 15 / 390),  # floored at 15 minutes
    ],
)
def test_sessions_to_expiry(expiration: date, now: datetime, expected: float) -> None:
    assert qe.sessions_to_expiry(expiration, now) == pytest.approx(expected)


def test_atm_iv_uses_the_nearest_strike_of_the_shortest_expiry_on_a_session_basis() -> None:
    today = date(2026, 11, 23)  # NOW is Monday 10:00 New York
    chain = pd.concat(
        [
            make_chain([(99, "call", 10, 0.9), (100, "call", 10, 0.30), (100, "put", 10, 0.34)], expiration=today),
            make_chain([(100, "call", 10, 0.5), (100, "put", 10, 0.5)], expiration=date(2026, 11, 27)),
        ],
        ignore_index=True,
    )
    t = qe.time_to_expiry(pd.Series([today]), NOW)[0]
    expected = 0.32 * math.sqrt(t * config.TRADING_DAYS_PER_YEAR / (6 / 6.5))  # mean of 0.30 and 0.34
    assert qe.atm_iv(chain, 100.2, NOW) == pytest.approx(expected)
    assert expected == pytest.approx(0.32 / 2.31, rel=0.01)  # a 0DTE calendar IV reads ~2.3x a session IV
    assert qe.atm_iv(chain.iloc[0:0], 100.0, NOW) is None


def test_expected_move() -> None:
    em = qe.expected_move(SPOT, 0.16)
    assert em["expected_move"] == pytest.approx(SPOT * 0.16 * math.sqrt(1 / 252))
    assert em["expected_move_pct"] == pytest.approx(em["expected_move"] / SPOT * 100)
    assert em["expected_move_upper"] == pytest.approx(SPOT + em["expected_move"])
    assert em["expected_move_lower"] == pytest.approx(SPOT - em["expected_move"])
    assert set(qe.expected_move(SPOT, None).values()) == {None}


# --- Vanna surface ------------------------------------------------------------


def test_vanna_surface_is_consistent_with_the_dashboard() -> None:
    chain = make_chain([(95, "put", 300, 0.22), (100, "call", 500, 0.20), (100, "put", 200, 0.21), (105, "call", 400, 0.19)])
    surface = qe.calculate_vanna_surface(chain, SPOT, resolution=21, ticker="SPY", now=NOW)
    z = np.array(surface["z"])
    assert z.shape == (21, 21)
    assert surface["x"][0] == pytest.approx(90.0) and surface["x"][-1] == pytest.approx(110.0)
    assert surface["y"][0] == pytest.approx(0.8) and surface["y"][-1] == pytest.approx(1.2)

    # Centre of the grid (current spot, unshocked IV): the dashboard's total net VEX.
    assert z[10, 10] == pytest.approx(qe.build_report(chain, SPOT, "SPY", "all", NOW)["totals"]["net_vex"])
    # One row per IV shock, one column per spot: corner = IV x0.8 at spot +10 %.
    shocked = chain.assign(implied_volatility=chain["implied_volatility"] * 0.8)
    assert z[0, -1] == pytest.approx(qe.compute_exposures(shocked, 110.0, "SPY", NOW)["vex"].sum())

    calls = qe.calculate_vanna_surface(chain, SPOT, "call", 21, ticker="SPY", now=NOW)["z"]
    puts = qe.calculate_vanna_surface(chain, SPOT, "put", 21, ticker="SPY", now=NOW)["z"]
    np.testing.assert_allclose(np.array(calls) + np.array(puts), z)


def test_vanna_surface_rejects_an_unknown_option_type() -> None:
    with pytest.raises(ValueError):
        qe.calculate_vanna_surface(make_chain([(100, "call", 10, 0.2)]), SPOT, "straddle", ticker="SPY", now=NOW)
