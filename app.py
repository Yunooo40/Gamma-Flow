"""Flask server: the dashboard page and its JSON API."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, abort, render_template, request

import config
import data_fetcher
import quant_engine

logger = logging.getLogger(__name__)
app = Flask(__name__)


def _finite(value: Any) -> Any:
    """Recursively replace NaN / Infinity with None: they are not valid JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def _json_response(payload: dict[str, Any], status: int = 200) -> Response:
    return Response(json.dumps(_finite(payload), allow_nan=False), status=status, mimetype="application/json")


@app.get("/")
def index() -> str:
    return render_template(
        "index.html",
        tickers=config.TICKERS,
        max_dte_all=config.MAX_DTE_ALL,
        refresh_seconds=config.AUTO_REFRESH_SECONDS,
    )


@app.get("/api/health")
def health() -> Response:
    return _json_response({"status": "ok"})


def _requested_chain() -> tuple[str, str, data_fetcher.ChainSnapshot]:
    """The request's ``ticker`` and ``expiry`` and their chain; aborts with a JSON 400 or 502."""
    ticker = request.args.get("ticker", "").strip().upper()
    expiry = request.args.get("expiry", "0dte").strip().lower()
    if ticker not in config.TICKERS:
        abort(_json_response({"error": f"Invalid ticker, expected one of: {', '.join(config.TICKERS)}"}, 400))
    if expiry not in config.EXPIRY_FILTERS:
        abort(_json_response({"error": f"Invalid expiry, expected one of: {', '.join(config.EXPIRY_FILTERS)}"}, 400))
    try:
        return ticker, expiry, data_fetcher.get_chain(ticker, expiry)
    except data_fetcher.DataUnavailableError:
        abort(_json_response({"error": "Market data unavailable"}, 502))


@app.get("/api/gex")
def gex() -> Response:
    ticker, expiry, snapshot = _requested_chain()
    report = quant_engine.build_report(snapshot.chain, snapshot.spot, ticker, expiry, datetime.now(timezone.utc))
    realized_vol = data_fetcher.get_realized_vol(ticker)
    warnings = list(snapshot.warnings)
    if not report["strikes"]:
        warnings.append("No contract with valid open interest and IV in the displayed range")
    if realized_vol is None:
        warnings.append("Realized volatility unavailable (no daily history)")

    return _json_response(
        {
            "ticker": ticker,
            "expiry_filter": expiry,
            "source": snapshot.source,
            "timeframe": snapshot.timeframe,
            "fetched_at": snapshot.fetched_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cached": snapshot.cached,
            "expirations": [d.isoformat() for d in snapshot.expirations],
            "expiry_fallback": snapshot.expiry_fallback,
            **report,  # levels, totals, strikes, volatility: a new metric of build_report reaches the UI as is
            "realized_vol": realized_vol,
            "warnings": warnings,
        }
    )


@app.get("/api/vanna-surface")
def vanna_surface() -> Response:
    """Net VEX over simulated spots and IV shocks, computed on demand from the cached chain."""
    ticker, _, snapshot = _requested_chain()
    surface = quant_engine.calculate_vanna_surface(
        snapshot.chain, snapshot.spot, ticker=ticker, now=datetime.now(timezone.utc)
    )
    return _json_response(surface)


@app.errorhandler(500)
def internal_error(_error: Exception) -> Response:
    # The traceback goes to the server log only, never to the client.
    return _json_response({"error": "Internal server error"}, 500)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG)


if __name__ == "__main__":
    main()
