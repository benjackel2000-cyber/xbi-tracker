"""
XBI contribution tracker.

Each trading day this script:
  1. Downloads XBI's daily holdings file from State Street (SSGA), which lists
     every stock in the fund and its weight.
  2. Pulls the last two daily closes for each holding (via yfinance).
  3. Computes each stock's contribution to XBI's move:
         contribution (bps) = weight (%) x daily return (%)
     e.g. a 0.8% holding that rises 10% adds about +8 bps to XBI.
  4. Saves the results to data/latest.json and data/daily/<date>.json.

The weights in the holdings file are as of the prior close, which is the
right starting point for measuring the day's contribution.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

HOLDINGS_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-xbi.xlsx"
)
DATA_DIR = Path(__file__).parent / "data"


def load_holdings() -> pd.DataFrame:
    """Download and parse SSGA's XBI holdings spreadsheet."""
    resp = requests.get(HOLDINGS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    resp.raise_for_status()
    raw = pd.read_excel(io.BytesIO(resp.content), header=None)

    # The sheet has a few title rows on top; find the row that holds the column names.
    header_row = next(i for i, row in raw.iterrows() if "Ticker" in row.astype(str).tolist())
    df = raw.iloc[header_row + 1 :].copy()
    df.columns = raw.iloc[header_row].astype(str).str.strip()

    # Pull the as-of date from the title rows if it is there (e.g. "As of 26-Oct-2026").
    as_of = None
    for cell in raw.iloc[:header_row].astype(str).values.ravel():
        if "as of" in cell.lower():
            as_of = cell.split("of", 1)[-1].strip()

    df = df[["Name", "Ticker", "Weight"]].dropna(subset=["Ticker", "Weight"])
    df["Ticker"] = df["Ticker"].astype(str).str.strip()
    df = df[df["Ticker"].str.fullmatch(r"[A-Z][A-Z.\-]{0,6}")]  # drop cash / footnote rows
    df["Weight"] = pd.to_numeric(df["Weight"], errors="coerce")
    df = df.dropna(subset=["Weight"])
    df.attrs["as_of"] = as_of
    return df.reset_index(drop=True)


def load_returns(tickers: list[str]) -> pd.DataFrame:
    """Last two daily closes for each ticker -> daily % return."""
    yf_tickers = [t.replace(".", "-") for t in tickers] + ["XBI"]
    prices = yf.download(yf_tickers, period="7d", interval="1d", auto_adjust=False, progress=False)["Close"]
    prices = prices.dropna(how="all")
    if len(prices) < 2:
        raise RuntimeError("Not enough price history returned.")
    last, prev = prices.iloc[-1], prices.iloc[-2]
    out = pd.DataFrame({"close": last, "prev_close": prev})
    out["return_pct"] = (out["close"] / out["prev_close"] - 1) * 100
    out.index = [t.replace("-", ".") for t in out.index]
    out.attrs["date"] = prices.index[-1].strftime("%Y-%m-%d")
    return out


def main() -> int:
    holdings = load_holdings()
    returns = load_returns(holdings["Ticker"].tolist())
    date = returns.attrs["date"]

    df = holdings.merge(returns, left_on="Ticker", right_index=True, how="left")
    df["contribution_bps"] = df["Weight"] * df["return_pct"]
    priced = df.dropna(subset=["return_pct"])
    missing = sorted(set(df["Ticker"]) - set(priced["Ticker"]))

    xbi = returns.loc["XBI"] if "XBI" in returns.index else None
    rows = [
        {
            "id": f"{r.Ticker}-{date}",
            "date": date,
            "ticker": r.Ticker,
            "name": str(r.Name).title(),
            "weight_pct": round(float(r.Weight), 4),
            "close": round(float(r.close), 2),
            "return_pct": round(float(r.return_pct), 2),
            "contribution_bps": round(float(r.contribution_bps), 2),
        }
        for r in priced.sort_values("contribution_bps", ascending=False).itertuples()
    ]
    result = {
        "date": date,
        "holdings_as_of": holdings.attrs.get("as_of"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "xbi_close": round(float(xbi["close"]), 2) if xbi is not None else None,
        "xbi_return_pct": round(float(xbi["return_pct"]), 2) if xbi is not None else None,
        "sum_contribution_bps": round(float(priced["contribution_bps"].sum()), 1),
        "holdings_count": len(df),
        "priced_count": len(priced),
        "missing_tickers": missing,
        "source": {"holdings": HOLDINGS_URL, "prices": "Yahoo Finance via yfinance"},
        "rows": rows,
    }

    (DATA_DIR / "daily").mkdir(parents=True, exist_ok=True)
    for path in (DATA_DIR / "latest.json", DATA_DIR / "daily" / f"{date}.json"):
        path.write_text(json.dumps(result, indent=1))

    print(
        f"{date}: {len(priced)}/{len(df)} holdings priced; "
        f"sum of contributions {result['sum_contribution_bps']} bps vs XBI {result['xbi_return_pct']}%"
    )
    if missing:
        print("No price for:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
