# Open-GEX

Open-source dashboard that estimates **dealer positioning in options** for day trading
**SPY, QQQ and IWM**: Gamma Exposure (GEX), Vanna Exposure (VEX), the Zero Gamma
(gamma flip) level and the Call / Put Walls.

![Open-GEX screenshot: SPY 0DTE during the session](docs/screenshot.png)

## Features

- Net GEX and net Vanna by strike, as a horizontal bar chart (Plotly), with Spot,
  Zero Gamma, Call Wall and Put Wall lines.
- KPI cards: spot, Zero Gamma, Call Wall, Put Wall (each with its distance to spot)
  and total net GEX (positive / negative gamma regime).
- Two expiry filters: **0DTE** (today's expiry, or the next one after the close, on
  weekends and holidays) and **All** (every expiry up to 45 days).
- Data from **Massive** (formerly Polygon.io) when an API key is configured, with an
  automatic, clearly flagged fallback to **yfinance**.
- 15-minute disk cache, auto-refresh every 5 minutes, dark "trading desk" theme.
- Greeks recomputed for every contract with the same Black-Scholes-Merton
  assumptions, whatever the data source.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # optional: add your Massive API key
python app.py
```

Then open <http://127.0.0.1:5000>.

## Data sources

| Source | When | Notes |
|---|---|---|
| [Massive](https://massive.com) (formerly Polygon.io) | `MASSIVE_API_KEY` is set | Requires a plan **with options data** (Options Starter or higher). The `timeframe` reported by the API (`REAL-TIME` or `DELAYED`) is shown in the UI. |
| [yfinance](https://github.com/ranaroussi/yfinance) | No key, or Massive failed | **Unofficial** Yahoo Finance scraper, data **delayed ~15 min**, may break without notice. One request per expiry, so the first load of the *All* filter takes a few seconds. |

If Massive fails (invalid key, no options plan, rate limit, timeout, bad response),
the app logs a warning, shows it in the UI and falls back to yfinance. If yfinance
fails too, the API returns `502` and the UI shows the error. No synthetic data is
ever generated.

## Methodology

### Greeks

Black-Scholes-Merton with a continuous dividend yield `q` (per ticker, see
`config.py`), risk-free rate `r`, implied volatility `σ` from the source and
standard normal density `φ`:

```
d1    = (ln(S/K) + (r − q + σ²/2)·T) / (σ·√T)
d2    = d1 − σ·√T
Gamma = e^(−q·T) · φ(d1) / (S · σ · √T)          (same for calls and puts)
Vanna = −e^(−q·T) · φ(d1) · d2 / σ               (∂Delta/∂σ, same for calls and puts)
```

`T` is the time to the 16:00 New York close of the expiry day, in years
(365-day basis), floored at 15 minutes so that Greeks stay finite near expiry.
Greeks are always recomputed from the IV, even when the source provides its own.

### Exposures

**Dealer assumption:** dealers are long the calls and short the puts held in the
open interest. `sign = +1` for calls, `−1` for puts.

```
GEX = sign × Gamma × OI × 100 × S² × 0.01   → $ of delta per 1 % move of the underlying
VEX = sign × Vanna × OI × 100 × S  × 0.01   → $ of delta per +1 volatility point
```

Exposures are summed per strike across all selected expiries (`call_gex`,
`put_gex`, `net_gex`, `net_vex`), plus chain-wide totals.

### Key levels

Computed on the whole cleaned chain (strikes within spot ± 30 %), not only on the
displayed range:

- **Call Wall:** strike with the largest call GEX.
- **Put Wall:** strike with the largest absolute put GEX.
- **Zero Gamma:** computed from a **profile**, not from cumulative strikes. Total
  GEX is re-evaluated on a grid of 201 hypothetical spots between −10 % and +10 %,
  recomputing every contract's Gamma at each spot with IV, T, r and q held
  constant (**sticky strike**). Sign changes are linearly interpolated and the one
  closest to spot is kept. If the profile never changes sign, the level is reported
  as *Out of range*.

### Cleaning

Contracts are dropped if they have missing values, open interest ≤ 0, an IV outside
[1 %, 300 %], a strike outside spot ± 30 %, or have already expired.

## Known limitations

- **Open interest is published once a day** and reflects the previous close.
  Positions opened during the session, which dominate 0DTE flows, are invisible.
- The **dealer long calls / short puts** assumption is a simplification: real
  dealer inventory is unknown and varies by strike and over time.
- **European** Black-Scholes is applied to **American** options on ETFs (early
  exercise is ignored).
- Rates and dividend yields are **static** values from `config.py` / `.env`.
- Outside regular trading hours (pre-market in particular), Yahoo often reports
  zero open interest and placeholder IVs: the yfinance chart is then empty and the
  UI says so. Use the dashboard during the session.
- yfinance data is unofficial and delayed; Massive data may be delayed depending
  on your plan.

## Configuration

Environment variables (`.env`):

| Variable | Default | Description |
|---|---|---|
| `MASSIVE_API_KEY` | *(empty)* | Massive API key with options access. Sent only in the `Authorization` header. |
| `MASSIVE_BASE_URL` | `https://api.massive.com` | Massive API base URL. |
| `RISK_FREE_RATE` | `0.04` | Annual risk-free rate (decimal). |
| `FLASK_DEBUG` | `0` | Set to `1` to enable the Flask debugger (local development only). |

Every other parameter (tickers, cache TTL, strike ranges, Zero Gamma grid, IV
bounds, dividend yields, host and port...) lives in [`config.py`](config.py).

### API

- `GET /api/gex?ticker=SPY&expiry=0dte` (`expiry` is `0dte` or `all`): exposures,
  levels and metadata as JSON. Returns `400` on invalid parameters and `502` if no
  data source is available.
- `GET /api/health`: `{"status": "ok"}`.

## Tests

```bash
pytest
```

The tests never touch the network: data sources are mocked.

## License

[MIT](LICENSE)

## Disclaimer

Open-GEX is provided for **educational and informational purposes only**. It is
**not financial or investment advice**, nor a recommendation to buy or sell any
security. The data and calculations are provided "as is", with **no guarantee of
accuracy, completeness or timeliness**. Trading options involves a **substantial
risk of loss**. You alone are responsible for your trading decisions and for
complying with the terms of use of the data providers you connect (Massive, Yahoo
Finance).
