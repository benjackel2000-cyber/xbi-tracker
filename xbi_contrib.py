"""
XBI contribution tracker.

Each trading day this script:
  1. Downloads XBI's daily holdings file from State Street (SSGA), which lists
     every stock in the fund and its weight.
  2. Pulls the last two daily closes for each holding (via yfinance).
  3. Computes each stock's contribution to XBI's move:
         contribution (bps) = weight (%) x daily return (%)
     e.g. a 0.8% holding that rises 10% adds about +8 bps to XBI.
  4. Collects recent Yahoo Finance headlines for the biggest movers.
  5. Saves the results to data/latest.json and data/daily/<date>.json.

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
DEBUG_TICKERS = {"ORIC", "ORKA"}


def load_holdings() -> pd.DataFrame:
    """Download and parse SSGA's XBI holdings spreadsheet."""
    resp = requests.get(HOLDINGS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    print(f"Holdings download: HTTP {resp.status_code}, {resp.headers.get('content-type')}, {len(resp.content)} bytes")
    resp.raise_for_status()
    if not resp.content.startswith(b"PK"):  # .xlsx files are zip archives starting with "PK"
        raise RuntimeError("Holdings file is not a spreadsheet. First bytes: " + resp.content[:300].decode("utf-8", "replace"))
    raw = pd.read_excel(io.BytesIO(resp.content), header=None)
    print("First rows of holdings sheet:")
    print(raw.head(8).to_string())

    # The sheet has a few title rows on top; find the row that holds the column names.
    header_row = next(
        (i for i, row in raw.iterrows() if any(str(v).strip().lower() == "ticker" for v in row)), None
    )
    if header_row is None:
        raise RuntimeError("Could not find a 'Ticker' column in the holdings sheet.")
    df = raw.iloc[header_row + 1 :].copy()
    df.columns = raw.iloc[header_row].astype(str).str.strip()

    # Pull the as-of date from the title rows if it is there (e.g. "As of 26-Oct-2026").
    as_of = None
    for cell in raw.iloc[:header_row].values.ravel():
        text = str(cell)
        if "as of" in text.lower():
            as_of = text.lower().split("as of", 1)[-1].strip()

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


def pick_movers(priced: pd.DataFrame, n_pct: int = 8, n_contrib: int = 3) -> pd.DataFrame:
    """The day's biggest % movers plus the biggest contributors/detractors."""
    by_move = priced.reindex(priced["return_pct"].abs().sort_values(ascending=False).index).head(n_pct)
    top = priced.sort_values("contribution_bps", ascending=False).head(n_contrib)
    bottom = priced.sort_values("contribution_bps").head(n_contrib)
    return pd.concat([by_move, top, bottom]).drop_duplicates("Ticker")


def recent_headlines(ticker: str, date: str, limit: int = 5) -> list[dict]:
    """Yahoo Finance headlines for a ticker published from the prior evening through `date`."""
    start = pd.Timestamp(date, tz="UTC") - pd.Timedelta(hours=28)  # ~prior day's close (ET) onward
    end = pd.Timestamp(date, tz="UTC") + pd.Timedelta(hours=30)  # through the evening after the close
    try:
        items = yf.Ticker(ticker.replace(".", "-")).news or []
    except Exception as exc:  # news is best-effort; never fail the run over it
        print(f"News lookup failed for {ticker}: {exc}")
        return []
    if ticker in DEBUG_TICKERS:
        print(f"[news debug] {ticker}: {len(items)} items from Yahoo")
        for it in items[:3]:
            c = it.get("content", it)
            print("   keys:", sorted(c.keys())[:15], "| pub:", c.get("pubDate") or c.get("displayTime") or c.get("providerPublishTime"), "|", str(c.get("title"))[:70])
    out = []
    for item in items:
        c = item.get("content", item)  # yfinance has used both a flat and a nested shape
        title = c.get("title")
        published = c.get("pubDate") or c.get("displayTime") or c.get("providerPublishTime")
        if isinstance(published, (int, float)):
            ts = pd.Timestamp(published, unit="s", tz="UTC")
        else:
            ts = pd.to_datetime(published, utc=True, errors="coerce")
        link = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or c.get("link")
        publisher = (c.get("provider") or {}).get("displayName") or c.get("publisher")
        if not title or ts is pd.NaT or ts is None or not (start <= ts <= end):
            continue
        out.append({"title": title, "publisher": publisher, "link": link, "published": ts.isoformat()})
    return out[:limit]


def main() -> int:
    holdings = load_holdings()
    print(f"Parsed {len(holdings)} holdings; weights sum to {holdings['Weight'].sum():.1f}%")
    returns = load_returns(holdings["Ticker"].tolist())
    date = returns.attrs["date"]

    df = holdings.merge(returns, left_on="Ticker", right_index=True, how="left")
    df["contribution_bps"] = df["Weight"] * df["return_pct"]
    priced = df.dropna(subset=["return_pct"])
    missing = sorted(set(df["Ticker"]) - set(priced["Ticker"]))

    movers = []
    for r in pick_movers(priced).itertuples():
        news = recent_headlines(r.Ticker, date)
        movers.append({
            "ticker": r.Ticker,
            "name": str(r.Name).title(),
            "return_pct": round(float(r.return_pct), 2),
            "contribution_bps": round(float(r.contribution_bps), 2),
            "headlines": news,
        })
    print(f"Movers with headlines: {sum(1 for m in movers if m['headlines'])}/{len(movers)}")

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
        "source": {"holdings": HOLDINGS_URL, "prices": "Yahoo Finance via yfinance", "news": "Yahoo Finance news via yfinance"},
        "movers": movers,
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
