"""Pure quantitative engine: Black-Scholes-Merton Greeks, dealer exposures and key levels.

Nothing in this module touches the network or the file system: every input is
passed explicitly, which keeps the maths deterministic and easy to test.

Dealer assumption: dealers are long the calls and short the puts that make up
the open interest, so calls contribute positive and puts negative exposure.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike, NDArray
from scipy.optimize import brentq
from scipy.special import ndtr
from scipy.stats import norm

import config

CHAIN_COLUMNS: tuple[str, ...] = (
    "strike",
    "option_type",
    "expiration",
    "open_interest",
    "implied_volatility",
)
SECONDS_PER_YEAR: int = config.DAYS_PER_YEAR * 24 * 3600


def expiry_timestamps(expirations: pd.Series) -> pd.Series:
    """Convert expiration dates to tz-aware timestamps at the New York market close."""
    close = pd.Timedelta(hours=config.MARKET_CLOSE.hour, minutes=config.MARKET_CLOSE.minute)
    return (pd.to_datetime(expirations) + close).dt.tz_localize(config.MARKET_TZ)


def time_to_expiry(expirations: pd.Series, now: datetime) -> NDArray[np.float64]:
    """Year fraction until each expiry, floored at ``MIN_T_MINUTES``.

    The floor keeps Greeks finite for contracts that expire within minutes.
    """
    seconds = (expiry_timestamps(expirations) - pd.Timestamp(now)).dt.total_seconds()
    return np.maximum(seconds.to_numpy(dtype=float), config.MIN_T_MINUTES * 60) / SECONDS_PER_YEAR


def _d1_d2(
    spot: ArrayLike, strike: ArrayLike, t: ArrayLike, sigma: ArrayLike, r: float, q: float
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Black-Scholes-Merton d1 and d2 (broadcastable inputs)."""
    vol_sqrt_t = np.asarray(sigma) * np.sqrt(t)
    d1 = (np.log(np.divide(spot, strike)) + (r - q + 0.5 * np.square(sigma)) * np.asarray(t)) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def bs_gamma(
    spot: ArrayLike, strike: ArrayLike, t: ArrayLike, sigma: ArrayLike, r: float, q: float
) -> NDArray[np.float64]:
    """Gamma = e^(-qT) * phi(d1) / (S * sigma * sqrt(T)), identical for calls and puts."""
    d1, _ = _d1_d2(spot, strike, t, sigma, r, q)
    return np.exp(-q * np.asarray(t)) * norm.pdf(d1) / (np.asarray(spot) * np.asarray(sigma) * np.sqrt(t))


def bs_vanna(
    spot: ArrayLike, strike: ArrayLike, t: ArrayLike, sigma: ArrayLike, r: float, q: float
) -> NDArray[np.float64]:
    """Vanna = dDelta/dSigma = -e^(-qT) * phi(d1) * d2 / sigma, identical for calls and puts."""
    d1, d2 = _d1_d2(spot, strike, t, sigma, r, q)
    return -np.exp(-q * np.asarray(t)) * norm.pdf(d1) * d2 / np.asarray(sigma)


def bs_charm(
    spot: ArrayLike, strike: ArrayLike, t: ArrayLike, sigma: ArrayLike, r: float, q: float, is_call: ArrayLike
) -> NDArray[np.float64]:
    """Charm = dDelta/dt, the Delta decay per year as calendar time passes (T shrinks).

    Call: q*e^(-qT)*N(d1) - e^(-qT)*phi(d1)*(2(r-q)T - d2*sigma*sqrt(T)) / (2T*sigma*sqrt(T))
    Put:  same decay term, with -q*e^(-qT)*N(-d1) as carry (the call's charm minus q*e^(-qT)).
    """
    t = np.asarray(t, dtype=float)
    d1, d2 = _d1_d2(spot, strike, t, sigma, r, q)
    vol_sqrt_t = np.asarray(sigma) * np.sqrt(t)
    discount = np.exp(-q * t)
    pdf = np.exp(-0.5 * d1**2) / np.sqrt(2 * np.pi)  # phi(d1), cheaper than norm.pdf
    decay = discount * pdf * (2 * (r - q) * t - d2 * vol_sqrt_t) / (2 * t * vol_sqrt_t)
    carry = np.where(is_call, q * discount * ndtr(d1), -q * discount * ndtr(-d1))
    return carry - decay


def bs_price(spot: float, strike: float, t: float, sigma: float, r: float, q: float, option_type: str) -> float:
    """Black-Scholes-Merton price of a European call or put."""
    d1, d2 = _d1_d2(spot, strike, t, sigma, r, q)
    sign = 1.0 if option_type == "call" else -1.0
    # ndtr instead of norm.cdf: same normal CDF, ~200x faster on scalars (called in a solver loop).
    return float(sign * (spot * np.exp(-q * t) * ndtr(sign * d1) - strike * np.exp(-r * t) * ndtr(sign * d2)))


def calculate_iv(
    target_price: float, spot: float, strike: float, t: float, r: float, option_type: str, q: float = 0.0
) -> float:
    """Implied volatility: the sigma where ``bs_price`` equals ``target_price`` (Brent's method).

    NaN when the price is missing, at or below intrinsic value (no root for brentq), or
    not reachable within ``IV_SOLVER_BOUNDS`` (e.g. a stale price).
    """
    intrinsic = max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)
    if not target_price > intrinsic:  # also rejects NaN prices
        return np.nan

    def error(sigma: float) -> float:
        return bs_price(spot, strike, t, sigma, r, q, option_type) - target_price

    try:
        return float(brentq(error, *config.IV_SOLVER_BOUNDS))
    except (ValueError, RuntimeError):  # no sign change within the bounds, or no convergence
        return np.nan


def clean_chain(chain: pd.DataFrame, spot: float, now: datetime) -> pd.DataFrame:
    """Drop unusable contracts.

    Removes rows with NaN, non-positive open interest, an IV outside
    ``[IV_MIN, IV_MAX]``, a strike outside ``spot +/- COMPUTE_RANGE_PCT`` and
    contracts that have already expired at ``now``.
    """
    df = chain.loc[:, list(CHAIN_COLUMNS)].dropna()
    keep = (
        df["option_type"].isin(("call", "put"))
        & (df["open_interest"] > 0)
        & df["implied_volatility"].between(config.IV_MIN, config.IV_MAX)
        & df["strike"].between(spot * (1 - config.COMPUTE_RANGE_PCT), spot * (1 + config.COMPUTE_RANGE_PCT))
        & (expiry_timestamps(df["expiration"]) > pd.Timestamp(now))
    )
    return df[keep].reset_index(drop=True)


def _contract_inputs(
    chain: pd.DataFrame, ticker: str, now: datetime
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], float, float]:
    """Per-contract arrays: strike, T, sigma, signed contract weight, plus r and q."""
    sign = np.where(chain["option_type"].to_numpy() == "call", 1.0, -1.0)
    weight = sign * chain["open_interest"].to_numpy(dtype=float) * config.CONTRACT_MULTIPLIER
    return (
        chain["strike"].to_numpy(dtype=float),
        time_to_expiry(chain["expiration"], now),
        chain["implied_volatility"].to_numpy(dtype=float),
        weight,
        config.RISK_FREE_RATE,
        config.DIVIDEND_YIELDS[ticker],
    )


def compute_exposures(chain: pd.DataFrame, spot: float, ticker: str, now: datetime) -> pd.DataFrame:
    """Add per-contract ``gamma``, ``vanna``, ``charm``, ``gex``, ``vex`` and ``charm_exposure`` columns.

    GEX   = sign * Gamma * OI * 100 * S^2 * 0.01  ($ delta per 1 % move)
    VEX   = sign * Vanna * OI * 100 * S * 0.01    ($ delta per +1 vol point)
    Charm = -sign * Charm * OI * 100 * S / 365    ($ dealers buy (+) or sell (-) per calendar day)
    """
    strike, t, sigma, weight, r, q = _contract_inputs(chain, ticker, now)
    df = chain.copy()
    df["gamma"] = bs_gamma(spot, strike, t, sigma, r, q)
    df["vanna"] = bs_vanna(spot, strike, t, sigma, r, q)
    df["charm"] = bs_charm(spot, strike, t, sigma, r, q, chain["option_type"].to_numpy() == "call")
    df["gex"] = weight * df["gamma"] * spot**2 * config.SPOT_MOVE_PCT
    df["vex"] = weight * df["vanna"] * spot * config.VOL_POINT
    # Charm is per year of T (365-day basis): dividing by 365 gives the drift per calendar day. The minus sign
    # turns the dealers' delta drift into their hedge flow, so that a positive number reads as buying pressure.
    df["charm_exposure"] = -weight * df["charm"] * spot / config.DAYS_PER_YEAR
    return df


def aggregate_by_strike(exposures: pd.DataFrame) -> pd.DataFrame:
    """Sum exposures per strike across expiries: call_gex, put_gex, net_gex, net_vex."""
    is_call = exposures["option_type"] == "call"
    by_strike = (
        pd.DataFrame(
            {
                "strike": exposures["strike"],
                "call_gex": exposures["gex"].where(is_call, 0.0),
                "put_gex": exposures["gex"].where(~is_call, 0.0),
                "net_vex": exposures["vex"],
            }
        )
        .groupby("strike", as_index=False)
        .sum()
    )
    by_strike["net_gex"] = by_strike["call_gex"] + by_strike["put_gex"]
    return by_strike[["strike", "call_gex", "put_gex", "net_gex", "net_vex"]]


def find_walls(by_strike: pd.DataFrame) -> tuple[float | None, float | None]:
    """Call Wall = strike with the largest call GEX; Put Wall = largest |put GEX|."""
    calls = by_strike[by_strike["call_gex"] > 0]
    puts = by_strike[by_strike["put_gex"] < 0]
    call_wall = float(calls.loc[calls["call_gex"].idxmax(), "strike"]) if not calls.empty else None
    put_wall = float(puts.loc[puts["put_gex"].abs().idxmax(), "strike"]) if not puts.empty else None
    return call_wall, put_wall


def gex_profile(
    chain: pd.DataFrame, spot_grid: NDArray[np.float64], ticker: str, now: datetime
) -> NDArray[np.float64]:
    """Total GEX for each hypothetical spot S' (sticky strike: IV, T, r, q unchanged).

    Evaluated as a contracts x grid matrix, in blocks of ``ZERO_GAMMA_CHUNK_SIZE``
    contracts to bound memory.
    """
    strike, t, sigma, weight, r, q = _contract_inputs(chain, ticker, now)
    grid = np.asarray(spot_grid, dtype=float)
    total = np.zeros_like(grid)
    for start in range(0, strike.size, config.ZERO_GAMMA_CHUNK_SIZE):
        block = slice(start, start + config.ZERO_GAMMA_CHUNK_SIZE)
        gamma = bs_gamma(grid[None, :], strike[block, None], t[block, None], sigma[block, None], r, q)
        total += weight[block] @ gamma
    return total * grid**2 * config.SPOT_MOVE_PCT


def find_zero_gamma(spot_grid: NDArray[np.float64], profile: NDArray[np.float64], spot: float) -> float | None:
    """Linearly interpolated sign change of the GEX profile closest to ``spot``, or None."""
    nonzero = profile != 0  # exact zeros come from gamma underflow far from the strikes
    x, y = np.asarray(spot_grid)[nonzero], np.asarray(profile)[nonzero]
    idx = np.flatnonzero(np.sign(y[:-1]) != np.sign(y[1:]))
    if idx.size == 0:
        return None
    x0, x1, y0, y1 = x[idx], x[idx + 1], y[idx], y[idx + 1]
    roots = x0 - y0 * (x1 - x0) / (y1 - y0)
    return float(roots[np.argmin(np.abs(roots - spot))])


def build_report(
    chain: pd.DataFrame, spot: float, ticker: str, expiry_filter: str, now: datetime
) -> dict[str, Any]:
    """Compute exposures and key levels for a raw chain.

    Levels and totals use the whole cleaned chain; ``strikes`` only keeps the
    strikes inside ``spot +/- DISPLAY_RANGE_PCT[expiry_filter]``.
    """
    clean = clean_chain(chain, spot, now)
    exposures = compute_exposures(clean, spot, ticker, now)
    by_strike = aggregate_by_strike(exposures)
    call_wall, put_wall = find_walls(by_strike)

    grid = np.linspace(
        spot * (1 - config.ZERO_GAMMA_SEARCH_PCT),
        spot * (1 + config.ZERO_GAMMA_SEARCH_PCT),
        config.ZERO_GAMMA_GRID_POINTS,
    )
    zero_gamma = find_zero_gamma(grid, gex_profile(clean, grid, ticker, now), spot)
    iv = atm_iv(clean, spot, now)

    half_width = config.DISPLAY_RANGE_PCT[expiry_filter]
    low, high = spot * (1 - half_width), spot * (1 + half_width)
    shown = by_strike[by_strike["strike"].between(low, high)]
    return {
        "spot": float(spot),
        "levels": {"call_wall": call_wall, "put_wall": put_wall, "zero_gamma": zero_gamma},
        "totals": {
            "net_gex": float(by_strike["net_gex"].sum()),
            "net_vex": float(by_strike["net_vex"].sum()),
        },
        "total_charm": float(exposures["charm_exposure"].sum()),
        "display_range": {"min": float(low), "max": float(high)},
        "strikes": shown.to_dict(orient="records"),
        "atm_iv": iv,
        **expected_move(spot, iv),
    }


# --- Volatility analysis ------------------------------------------------------


def sessions_to_expiry(expiration: date, now: datetime) -> float:
    """Trading sessions left until the expiry close: the rest of today's session plus each later weekday.

    Floored like ``time_to_expiry``, at ``MIN_T_MINUTES`` of a session.
    shortcut: exchange holidays count as sessions and half days as full ones; add a holiday calendar
    if an expiry spanning a holiday matters (for daily expiries, only outside the session).
    """
    local = now.astimezone(config.MARKET_TZ)
    today = local.date()
    open_ = datetime.combine(today, config.MARKET_OPEN, tzinfo=config.MARKET_TZ)
    close = datetime.combine(today, config.MARKET_CLOSE, tzinfo=config.MARKET_TZ)
    session = (close - open_).total_seconds()
    left_today = (close - max(local, open_)).total_seconds() if np.is_busday(today) else 0.0
    today_part = min(max(left_today, 0.0) / session, 1.0)
    later = int(np.busday_count(today + timedelta(days=1), expiration + timedelta(days=1)))
    return max(today_part + later, config.MIN_T_MINUTES * 60 / session)


def atm_iv(chain: pd.DataFrame, spot: float, now: datetime) -> float | None:
    """Mean call/put IV at the strike closest to spot on the shortest expiry, on a 252-session basis.

    Chain IVs are calendar-time (T to the 16:00 close, 365-day basis): on a 0DTE they read ~2.3x a
    session-based IV, because the option prices a whole session in a few calendar hours. The option's
    total variance sigma^2 * T is therefore spread over the trading sessions left instead.
    """
    if chain.empty:
        return None
    expiry = chain["expiration"].min()
    nearest = chain[chain["expiration"] == expiry]
    strike = nearest["strike"].iloc[(nearest["strike"] - spot).abs().argmin()]
    iv = nearest.loc[nearest["strike"] == strike, "implied_volatility"].mean()
    t = time_to_expiry(pd.Series([expiry]), now)[0]
    return float(iv * np.sqrt(t * config.TRADING_DAYS_PER_YEAR / sessions_to_expiry(expiry, now)))


def expected_move(spot: float, iv: float | None) -> dict[str, float | None]:
    """1-day expected move ``spot * iv * sqrt(1 / 252)``: in dollars, in percent and as bounds around spot."""
    if iv is None:
        return dict.fromkeys(("expected_move", "expected_move_pct", "em_upper", "em_lower"))
    move = float(spot * iv * np.sqrt(1 / config.TRADING_DAYS_PER_YEAR))
    return {
        "expected_move": move,
        "expected_move_pct": move / spot * 100,
        "em_upper": spot + move,
        "em_lower": spot - move,
    }


# --- Vanna surface ------------------------------------------------------------


def calculate_vanna_surface(
    chain_df: pd.DataFrame,
    current_spot: float,
    option_type: str = "All",
    resolution: int = 20,
    *,
    ticker: str,
    now: datetime,
) -> dict[str, list[Any]]:
    """Net VEX of the cleaned chain over simulated spots and IV shocks.

    Spots span ``current_spot +/- VANNA_SURFACE_SPOT_PCT`` and IV multipliers span
    ``VANNA_SURFACE_IV_SHOCK``, ``resolution`` points each. Every contract keeps its strike, T and
    open interest (sticky strike); its IV is scaled by the shock and the spot replaced by the simulated
    one. ``option_type`` is ``All``, ``call`` or ``put``. Returns ``{"x": spots, "y": IV multipliers,
    "z": Net VEX}``, with one row of ``z`` per IV multiplier (the layout of a Plotly surface).
    """
    kind = option_type.lower()
    if kind not in ("all", "call", "put"):
        raise ValueError(f"option_type must be All, call or put, not {option_type!r}")
    chain = clean_chain(chain_df, current_spot, now)
    if kind != "all":
        chain = chain[chain["option_type"] == kind]
    strike, t, sigma, weight, r, q = _contract_inputs(chain, ticker, now)

    pct = config.VANNA_SURFACE_SPOT_PCT
    spots = np.linspace(current_spot * (1 - pct), current_spot * (1 + pct), resolution)
    shocks = np.linspace(*config.VANNA_SURFACE_IV_SHOCK, resolution)
    spot_grid, shock_grid = np.meshgrid(spots, shocks)  # shape (len(shocks), len(spots))
    s, m = spot_grid.ravel(), shock_grid.ravel()
    total = np.zeros(s.size)
    for start in range(0, strike.size, config.ZERO_GAMMA_CHUNK_SIZE):  # contracts x grid points, in blocks
        block = slice(start, start + config.ZERO_GAMMA_CHUNK_SIZE)
        vanna = bs_vanna(s[None, :], strike[block, None], t[block, None], sigma[block, None] * m[None, :], r, q)
        total += weight[block] @ vanna
    z = total * s * config.VOL_POINT  # same VEX as compute_exposures, at the simulated spot
    return {"x": spots.tolist(), "y": shocks.tolist(), "z": z.reshape(spot_grid.shape).tolist()}
