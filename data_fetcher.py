"""Market data ingestion: Alpaca, then Massive (formerly Polygon.io), then yfinance.

All network and file I/O lives here. Every source is normalized to the same
chain schema (``quant_engine.CHAIN_COLUMNS``) and cached on disk as JSON.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import config
from quant_engine import CHAIN_COLUMNS, calculate_iv, time_to_expiry

logger = logging.getLogger(__name__)

YFINANCE_WARNING = "yfinance: unofficial source, data delayed ~15 min"

# Massive snapshot field -> chain column. Only these documented fields are read.
_MASSIVE_CHAIN_FIELDS: dict[str, str] = {
    "details.strike_price": "strike",
    "details.contract_type": "option_type",
    "details.expiration_date": "expiration",
    "open_interest": "open_interest",
    "implied_volatility": "implied_volatility",
}
_MASSIVE_SPOT_FIELD = "underlying_asset.price"
_MASSIVE_TIMEFRAME_FIELD = "underlying_asset.timeframe"


class DataUnavailableError(RuntimeError):
    """Raised when no data source could deliver an option chain."""


class _SourceError(RuntimeError):
    """A recoverable failure of a single data source."""


@dataclass
class ChainSnapshot:
    """A normalized option chain plus the metadata needed by the API."""

    ticker: str
    spot: float
    chain: pd.DataFrame
    source: Literal["alpaca", "massive", "yfinance"]
    timeframe: str | None
    fetched_at: datetime
    expirations: list[date]
    expiry_fallback: bool
    cached: bool = False
    warnings: list[str] = field(default_factory=list)


# --- Expiry selection ---------------------------------------------------------


def _market_now(now: datetime) -> tuple[date, bool]:
    """Return today's date in New York and whether the market close is still ahead."""
    local = now.astimezone(config.MARKET_TZ)
    return local.date(), local.time() < config.MARKET_CLOSE


def _is_live(expiration: date, today: date, before_close: bool) -> bool:
    """True if the expiry's 16:00 New York close has not passed yet."""
    return expiration > today or (expiration == today and before_close)


def select_expirations(
    available: Iterable[date], expiry_filter: str, now: datetime
) -> tuple[list[date], bool, list[str]]:
    """Pick the expiries for a filter.

    Returns ``(expirations, expiry_fallback, warnings)``. ``0dte`` uses today's
    expiry before the close, otherwise the next available one (flagged as a
    fallback). ``all`` keeps every live expiry with DTE <= ``MAX_DTE_ALL``.
    """
    today, before_close = _market_now(now)
    dates = set(available)
    live = sorted(d for d in dates if _is_live(d, today, before_close))
    if expiry_filter == "all":
        return [d for d in live if (d - today).days <= config.MAX_DTE_ALL], False, []
    if not live:
        return [], False, []
    if live[0] == today:
        return [today], False, []
    reason = "Today's expiry has closed" if today in dates else "No 0DTE expiry today"
    return [live[0]], True, [f"{reason}, using {live[0].isoformat()}"]


# --- Normalization ------------------------------------------------------------


def _normalize_chain(raw: pd.DataFrame) -> pd.DataFrame:
    """Coerce a raw chain (already renamed to CHAIN_COLUMNS) to the common dtypes."""
    chain = pd.DataFrame(
        {
            "strike": pd.to_numeric(raw["strike"], errors="coerce").astype(float),
            "option_type": raw["option_type"].astype(str).str.lower(),
            "expiration": pd.to_datetime(raw["expiration"], errors="coerce").dt.date,
            # A missing OI is treated as 0: such rows are dropped by the cleaning step.
            "open_interest": pd.to_numeric(raw["open_interest"], errors="coerce").fillna(0).astype("int64"),
            "implied_volatility": pd.to_numeric(raw["implied_volatility"], errors="coerce").astype(float),
        }
    )
    return chain.loc[:, list(CHAIN_COLUMNS)].reset_index(drop=True)


def _valid_price(price: float) -> bool:
    return math.isfinite(price) and price > 0


# --- Source A: Alpaca ---------------------------------------------------------

_OCC = re.compile(r"^[A-Z]{1,6}(\d{6})([CP])(\d{8})$")


def _alpaca_get(session: requests.Session, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET one Alpaca page; any failure becomes a ``_SourceError``."""
    try:
        response = session.get(url, params=params, timeout=config.HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise _SourceError(f"request failed ({type(exc).__name__})") from exc
    if response.status_code in (401, 403):
        raise _SourceError(f"HTTP {response.status_code}, check the Alpaca keys")
    if response.status_code != 200:
        raise _SourceError(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise _SourceError("invalid JSON") from exc
    if not isinstance(payload, dict):
        raise _SourceError("unexpected payload")
    return payload


def _alpaca_pages(session: requests.Session, url: str, params: dict[str, Any], key: str) -> Iterable[Any]:
    """Items under ``key`` across all pages (``next_page_token``)."""
    params = dict(params)
    for _ in range(config.ALPACA_MAX_PAGES):
        payload = _alpaca_get(session, url, params)
        yield from (payload.get(key) or {}).items() if key == "snapshots" else payload.get(key) or []
        token = payload.get("next_page_token")
        if not token:
            return
        params["page_token"] = token
    raise _SourceError(f"more than {config.ALPACA_MAX_PAGES} pages")


def _alpaca_price(snap: dict[str, Any]) -> float:
    """Option price: quote mid, else last trade, else the day's close; NaN if none (contract skipped)."""
    quote = snap.get("latestQuote") or {}
    bid, ask = quote.get("bp") or 0, quote.get("ap") or 0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    # One-sided or empty book (pre-market, weekend): fall back to the last known price.
    for bar, key in (("latestTrade", "p"), ("dailyBar", "c")):
        price = (snap.get(bar) or {}).get(key) or 0
        if price > 0:
            return float(price)
    return math.nan


def _parse_alpaca_chain(snapshots: dict[str, Any], open_interest: dict[str, Any]) -> pd.DataFrame:
    """Raw chain rows with the option ``price``, from snapshots (by OCC symbol) joined with open interest."""
    rows = []
    for symbol, snap in snapshots.items():
        match = _OCC.match(symbol)
        if not match:
            continue
        yymmdd, kind, strike = match.groups()
        rows.append(
            {
                "strike": int(strike) / 1000,
                "option_type": "call" if kind == "C" else "put",
                "expiration": datetime.strptime(yymmdd, "%y%m%d").date(),
                "open_interest": open_interest.get(symbol),
                "price": _alpaca_price(snap or {}),
            }
        )
    return pd.DataFrame(rows, columns=["strike", "option_type", "expiration", "open_interest", "price"])


def _fetch_alpaca(ticker: str, expiry_filter: str, now: datetime) -> ChainSnapshot:
    today, before_close = _market_now(now)
    first = today if before_close else today + timedelta(days=1)
    last = first + timedelta(days=config.MASSIVE_0DTE_LOOKAHEAD_DAYS) if expiry_filter == "0dte" else today + timedelta(days=config.MAX_DTE_ALL)
    window = {"expiration_date_gte": first.isoformat(), "expiration_date_lte": last.isoformat()}

    with requests.Session() as session:
        session.headers.update({"APCA-API-KEY-ID": config.ALPACA_KEY, "APCA-API-SECRET-KEY": config.ALPACA_SECRET})
        snapshots = dict(
            _alpaca_pages(
                session,
                f"{config.ALPACA_DATA_URL}/v1beta1/options/snapshots/{ticker}",
                {"feed": config.ALPACA_FEED, "limit": 1000, **window},
                "snapshots",
            )
        )
        if not snapshots:
            raise _SourceError("empty response")
        # The Trading API carries open interest (the data API does not), as strings.
        open_interest = {
            c["symbol"]: c.get("open_interest")
            for c in _alpaca_pages(
                session,
                f"{config.ALPACA_TRADING_URL}/v2/options/contracts",
                {"underlying_symbols": ticker, "limit": 10000, **window},
                "option_contracts",
            )
        }
        try:
            trade = _alpaca_get(session, f"{config.ALPACA_DATA_URL}/v2/stocks/{ticker}/trades/latest", {"feed": "iex"})
            spot: float | None = float(trade["trade"]["p"])
        except (_SourceError, KeyError, TypeError, ValueError):
            spot = None

    warnings: list[str] = []
    raw = _parse_alpaca_chain(snapshots, open_interest)
    expirations, fallback, expiry_warnings = select_expirations(raw["expiration"].dropna(), expiry_filter, now)
    if not expirations:
        raise _SourceError("no live expiry in response")
    warnings += expiry_warnings
    if spot is None or not _valid_price(spot):
        spot = _yfinance_spot(yf.Ticker(ticker))
        warnings.append("Alpaca: underlying price missing, spot taken from yfinance (delayed)")

    # The indicative feed has no IV: solve it from each price with the same BSM model as the Greeks.
    # Unsolved contracts keep a NaN IV and are dropped by quant_engine.clean_chain.
    raw = raw[raw["expiration"].isin(expirations)].reset_index(drop=True)
    r, q, t = config.RISK_FREE_RATE, config.DIVIDEND_YIELDS[ticker], time_to_expiry(raw["expiration"], now)
    raw["implied_volatility"] = [
        calculate_iv(price, spot, strike, t_i, r, kind, q)
        for price, strike, t_i, kind in zip(raw["price"], raw["strike"], t, raw["option_type"])
    ]

    return ChainSnapshot(
        ticker=ticker,
        spot=spot,
        chain=_normalize_chain(raw),
        source="alpaca",
        timeframe="REAL-TIME" if config.ALPACA_FEED == "opra" else "INDICATIVE",
        fetched_at=now.astimezone(timezone.utc),
        expirations=expirations,
        expiry_fallback=fallback,
        warnings=warnings,
    )


# --- Source B: Massive --------------------------------------------------------


def _parse_massive_chain(flat: pd.DataFrame) -> pd.DataFrame:
    """Chain columns from flattened Massive snapshot results."""
    raw = flat.reindex(columns=list(_MASSIVE_CHAIN_FIELDS)).rename(columns=_MASSIVE_CHAIN_FIELDS)
    return _normalize_chain(raw)


def _parse_massive_underlying(flat: pd.DataFrame) -> tuple[float | None, str | None]:
    """First valid ``underlying_asset.price`` and ``underlying_asset.timeframe``."""
    raw = flat.reindex(columns=[_MASSIVE_SPOT_FIELD, _MASSIVE_TIMEFRAME_FIELD])
    prices = pd.to_numeric(raw[_MASSIVE_SPOT_FIELD], errors="coerce")
    prices = prices[np.isfinite(prices) & (prices > 0)]
    timeframes = raw[_MASSIVE_TIMEFRAME_FIELD].dropna()
    return (
        float(prices.iloc[0]) if not prices.empty else None,
        str(timeframes.iloc[0]) if not timeframes.empty else None,
    )


def _massive_get(session: requests.Session, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """GET one Massive page; any failure becomes a ``_SourceError``."""
    try:
        response = session.get(url, params=params, timeout=config.HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise _SourceError(f"request failed ({type(exc).__name__})") from exc
    if response.status_code in (401, 403):
        raise _SourceError(f"HTTP {response.status_code}, check that the key includes options data")
    if response.status_code != 200:
        raise _SourceError(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise _SourceError("invalid JSON") from exc
    if not isinstance(payload, dict):
        raise _SourceError("unexpected payload")
    return payload


def _fetch_massive_results(ticker: str, first: date, last: date) -> list[dict[str, Any]]:
    """All snapshot results between two expiry dates, following ``next_url``."""
    url: str | None = f"{config.MASSIVE_BASE_URL}/v3/snapshot/options/{ticker}"
    params: dict[str, Any] | None = {
        "limit": config.MASSIVE_PAGE_LIMIT,
        "expiration_date.gte": first.isoformat(),
        "expiration_date.lte": last.isoformat(),
    }
    host = urlparse(config.MASSIVE_BASE_URL).netloc
    results: list[dict[str, Any]] = []
    with requests.Session() as session:
        # Header auth: the key never appears in a URL or a log line.
        session.headers["Authorization"] = f"Bearer {config.MASSIVE_API_KEY}"
        for _ in range(config.MASSIVE_MAX_PAGES):
            payload = _massive_get(session, url, params)
            results.extend(payload.get("results") or [])
            url, params = payload.get("next_url"), None
            if not url:
                return results
            if urlparse(url).netloc != host:  # never send the key to another host
                raise _SourceError("next_url points to an unexpected host")
    raise _SourceError(f"more than {config.MASSIVE_MAX_PAGES} pages")


def _fetch_massive(ticker: str, expiry_filter: str, now: datetime) -> ChainSnapshot:
    today, before_close = _market_now(now)
    first = today if before_close else today + timedelta(days=1)
    if expiry_filter == "0dte":
        last = first + timedelta(days=config.MASSIVE_0DTE_LOOKAHEAD_DAYS)
    else:
        last = today + timedelta(days=config.MAX_DTE_ALL)

    results = _fetch_massive_results(ticker, first, last)
    if not results:
        raise _SourceError("empty response")
    flat = pd.json_normalize(results)
    chain = _parse_massive_chain(flat)
    spot, timeframe = _parse_massive_underlying(flat)

    warnings: list[str] = []
    expirations, fallback, expiry_warnings = select_expirations(chain["expiration"].dropna(), expiry_filter, now)
    if not expirations:
        raise _SourceError("no live expiry in response")
    warnings += expiry_warnings
    if spot is None:
        spot = _yfinance_spot(yf.Ticker(ticker))
        warnings.append("Massive: underlying price missing, spot taken from yfinance (delayed)")

    return ChainSnapshot(
        ticker=ticker,
        spot=spot,
        chain=chain[chain["expiration"].isin(expirations)].reset_index(drop=True),
        source="massive",
        timeframe=timeframe,
        fetched_at=now.astimezone(timezone.utc),
        expirations=expirations,
        expiry_fallback=fallback,
        warnings=warnings,
    )


# --- Source C: yfinance -------------------------------------------------------


def _parse_yfinance_chain(option_chain: Any, expiration: date) -> pd.DataFrame:
    """Chain columns from a yfinance ``option_chain()`` result (``.calls`` / ``.puts``)."""
    frames = [
        pd.DataFrame(
            {
                "strike": side["strike"],
                "option_type": option_type,
                "expiration": expiration,
                "open_interest": side["openInterest"],
                "implied_volatility": side["impliedVolatility"],
            }
        )
        for option_type, side in (("call", option_chain.calls), ("put", option_chain.puts))
        if not side.empty
    ]
    if not frames:
        return _normalize_chain(pd.DataFrame(columns=list(CHAIN_COLUMNS)))
    return _normalize_chain(pd.concat(frames, ignore_index=True))


def _yfinance_spot(ticker: Any) -> float:
    """Spot from ``fast_info.last_price``, else the last 1-minute close of the day."""
    try:
        price = float(ticker.fast_info.last_price)
    except Exception as exc:  # yfinance raises many error types on bad Yahoo responses
        logger.info("fast_info unavailable (%s), using intraday history", exc)
        price = math.nan
    if not _valid_price(price):
        closes = ticker.history(period="1d", interval="1m")["Close"].dropna()
        price = float(closes.iloc[-1]) if not closes.empty else math.nan
    if not _valid_price(price):
        raise _SourceError("no valid spot price")
    return price


def _fetch_yfinance(ticker: str, expiry_filter: str, now: datetime) -> ChainSnapshot:
    try:
        yf_ticker = yf.Ticker(ticker)
        available = [date.fromisoformat(d) for d in yf_ticker.options]
        expirations, fallback, warnings = select_expirations(available, expiry_filter, now)
        if not expirations:
            raise _SourceError("no live expiry listed")
        chain = pd.concat(
            [_parse_yfinance_chain(yf_ticker.option_chain(d.isoformat()), d) for d in expirations],
            ignore_index=True,
        )
        spot = _yfinance_spot(yf_ticker)
    except Exception as exc:  # last source: any failure means no data at all
        logger.error("yfinance failed for %s: %s", ticker, exc)
        raise DataUnavailableError(f"yfinance failed for {ticker}") from exc

    return ChainSnapshot(
        ticker=ticker,
        spot=spot,
        chain=chain,
        source="yfinance",
        timeframe="DELAYED",
        fetched_at=now.astimezone(timezone.utc),
        expirations=expirations,
        expiry_fallback=fallback,
        warnings=[YFINANCE_WARNING, *warnings],
    )


def _fetch(ticker: str, expiry_filter: str, now: datetime) -> ChainSnapshot:
    """Try Alpaca, then Massive (each only if configured), then yfinance."""
    warnings: list[str] = []
    sources = []
    if config.ALPACA_KEY and config.ALPACA_SECRET:
        sources.append(("Alpaca", _fetch_alpaca))
    if config.MASSIVE_API_KEY:
        sources.append(("Massive", _fetch_massive))
    for name, fetch in sources:
        try:
            snapshot = fetch(ticker, expiry_filter, now)
            snapshot.warnings[:0] = warnings
            return snapshot
        except Exception as exc:  # any failure must fall back, never crash
            logger.warning("%s failed for %s (%s), falling back", name, ticker, exc)
            warnings.append(f"{name} unavailable ({exc})")
    snapshot = _fetch_yfinance(ticker, expiry_filter, now)
    snapshot.warnings[:0] = warnings
    return snapshot


# --- Cache --------------------------------------------------------------------

_LOCKS: dict[tuple[str, str], threading.Lock] = {
    (ticker, expiry_filter): threading.Lock()
    for ticker in config.TICKERS
    for expiry_filter in config.EXPIRY_FILTERS
}


def _cache_path(ticker: str, expiry_filter: str) -> Path:
    return config.BASE_DIR / config.CACHE_DIR / f"{ticker}_{expiry_filter}.json"


def _save_cache(snapshot: ChainSnapshot, expiry_filter: str) -> None:
    """Atomically write a snapshot as JSON (temp file + ``os.replace``)."""
    path = _cache_path(snapshot.ticker, expiry_filter)
    payload = {
        "ticker": snapshot.ticker,
        "spot": snapshot.spot,
        "source": snapshot.source,
        "timeframe": snapshot.timeframe,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "expirations": [d.isoformat() for d in snapshot.expirations],
        "expiry_fallback": snapshot.expiry_fallback,
        "warnings": snapshot.warnings,
        "chain": snapshot.chain.to_json(orient="split", date_format="iso"),
    }
    tmp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        ) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp)
        os.replace(tmp_name, path)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Could not write cache %s: %s", path.name, exc)
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)


def _load_cache(ticker: str, expiry_filter: str, now: datetime) -> ChainSnapshot | None:
    """Cached snapshot if younger than the TTL and none of its expiries has expired."""
    path = _cache_path(ticker, expiry_filter)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        expirations = [date.fromisoformat(d) for d in payload["expirations"]]
        today, before_close = _market_now(now)
        if (now - fetched_at).total_seconds() >= config.CACHE_TTL_SECONDS or not all(
            _is_live(d, today, before_close) for d in expirations
        ):
            return None
        chain = _normalize_chain(pd.read_json(io.StringIO(payload["chain"]), orient="split"))
        return ChainSnapshot(
            ticker=payload["ticker"],
            spot=float(payload["spot"]),
            chain=chain,
            source=payload["source"],
            timeframe=payload["timeframe"],
            fetched_at=fetched_at,
            expirations=expirations,
            expiry_fallback=bool(payload["expiry_fallback"]),
            cached=True,
            warnings=list(payload["warnings"]),
        )
    except FileNotFoundError:
        return None
    except (OSError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Ignoring unreadable cache %s: %s", path.name, exc)
        return None


# --- Public API ---------------------------------------------------------------


def get_chain(ticker: str, expiry_filter: str, now: datetime | None = None) -> ChainSnapshot:
    """Normalized option chain for a whitelisted ticker and expiry filter.

    Served from the disk cache when fresh, otherwise downloaded (Massive, then
    yfinance). Raises ``DataUnavailableError`` if every source fails.
    """
    ticker = ticker.upper()
    if ticker not in config.TICKERS or expiry_filter not in config.EXPIRY_FILTERS:
        raise ValueError(f"Unsupported ticker or expiry filter: {ticker!r}, {expiry_filter!r}")
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    with _LOCKS[(ticker, expiry_filter)]:  # one download per key at a time
        snapshot = _load_cache(ticker, expiry_filter, now)
        if snapshot is None:
            snapshot = _fetch(ticker, expiry_filter, now)
            _save_cache(snapshot, expiry_filter)
        return snapshot
