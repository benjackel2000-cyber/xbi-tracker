"""
Biotech IPO tracker.

1. Finds biotech IPOs from Nasdaq's public IPO calendar (priced, upcoming and filed deals,
   month by month) and merges them with data/ipo_seed.json (this year's IPOs, hand-checked).
2. Fills in missing tickers from the calendar, or by searching Yahoo Finance and checking
   that the stock's first trading day matches the IPO date.
3. Downloads daily prices since each IPO (Yahoo Finance) and works out, per IPO:
   day-1 pop, return since the offer price, XBI's return over the same period, and the gap.
4. Builds a "2026 biotech IPO class" index: an equal-weight basket that buys each IPO at its
   offer price on its IPO day, compared with XBI and the Renaissance IPO ETF (ticker IPO),
   all rebased to 100 on the first trading day of the year.

Outputs (ready-to-use row lists for the dashboard):
  data/ipo/ipos.json        one row per IPO (status priced / upcoming / filed)
  data/ipo/ipo_index.json   one row per trading day: basket, XBI and IPO ETF index levels
  data/ipo/ipo_prices.json  one row per IPO per trading day: return since offer
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).parent / "data"
OUT = ROOT / "ipo"
SEED = ROOT / "ipo_seed.json"
KNOWN = OUT / "known.json"          # IPOs the script found on its own in earlier runs
YEAR_START = date(date.today().year, 1, 1)

BIO = re.compile(r"therapeutic|\bbio|pharma|medicine|oncology|genetic|genomic|immun|\brx\b|neuro|cell|gene|vaccin|antibod|biosciences|biotherapeutics|health sciences", re.I)
NOT_BIO = re.compile(r"acquisition|capital|holdings? corp|spac|merger|bank|bancorp|energy|mining|realty|reit|trust\b", re.I)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def norm(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(r"\b(inc|corp|corporation|ltd|limited|plc|nv|sa|ag|co|holdings?|group|the|class a|ordinary shares|ads)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def money(v) -> float | None:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def nasdaq_month(ym: str) -> dict:
    r = requests.get(f"https://api.nasdaq.com/api/ipo/calendar?date={ym}", headers=UA, timeout=30)
    r.raise_for_status()
    return r.json().get("data") or {}


def rows_of(block) -> list[dict]:
    if not block:
        return []
    if isinstance(block, dict):
        if "rows" in block and isinstance(block["rows"], list):
            return block["rows"]
        for v in block.values():
            if isinstance(v, dict) and isinstance(v.get("rows"), list):
                return v["rows"]
    return []


def from_nasdaq() -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (priced, upcoming, filed) biotech deals from Nasdaq's calendar."""
    priced, upcoming, filed = [], [], []
    today = date.today()
    months = []
    d = YEAR_START.replace(day=1)
    while d <= today.replace(day=1) + timedelta(days=40):
        months.append(d.strftime("%Y-%m"))
        d = (d + timedelta(days=32)).replace(day=1)
    for ym in months:
        try:
            data = nasdaq_month(ym)
        except Exception as exc:
            print(f"Nasdaq calendar {ym}: failed ({exc})")
            continue
        p, u, f = rows_of(data.get("priced")), rows_of(data.get("upcoming")), rows_of(data.get("filed"))
        print(f"Nasdaq calendar {ym}: {len(p)} priced, {len(u)} upcoming, {len(f)} filed")
        for r in p:
            name = r.get("companyName") or ""
            if BIO.search(name) and not NOT_BIO.search(name):
                priced.append({"company": name, "ticker": (r.get("proposedTickerSymbol") or "").strip(),
                               "ipo_date": _iso(r.get("pricedDate")), "offer_price": money(r.get("proposedSharePrice")),
                               "raised_musd": _m(r.get("dollarValueOfSharesOffered")), "exchange": r.get("proposedExchange") or ""})
        for r in u:
            name = r.get("companyName") or ""
            if BIO.search(name) and not NOT_BIO.search(name):
                upcoming.append({"company": name, "ticker": (r.get("proposedTickerSymbol") or "").strip(),
                                 "expected_date": _iso(r.get("expectedPriceDate")), "price_range": r.get("proposedSharePrice") or "",
                                 "raised_musd": _m(r.get("dollarValueOfSharesOffered")), "exchange": r.get("proposedExchange") or ""})
        for r in f:
            name = r.get("companyName") or ""
            if BIO.search(name) and not NOT_BIO.search(name):
                filed.append({"company": name, "ticker": (r.get("proposedTickerSymbol") or "").strip(),
                              "filed_date": _iso(r.get("filedDate")), "raised_musd": _m(r.get("dollarValueOfSharesOffered"))})
    return priced, upcoming, filed


def _iso(v) -> str:
    if not v:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _m(v) -> float | None:
    x = money(v)
    return round(x / 1e6, 1) if x and x > 10000 else x


def yahoo_ticker(company: str, ipo_date: str) -> str:
    """Search Yahoo for the company and accept a ticker only if its trading starts near the IPO date."""
    try:
        quotes = yf.Search(company, max_results=6).quotes
    except Exception as exc:
        print(f"  Yahoo search failed for {company}: {exc}")
        return ""
    want = norm(company).split(" ")[0]
    for q in quotes:
        sym, nm = q.get("symbol", ""), q.get("shortname") or q.get("longname") or ""
        if not sym or "." in sym or q.get("quoteType") != "EQUITY" or want not in norm(nm):
            continue
        try:
            h = yf.Ticker(sym).history(start=ipo_date, end=(pd.Timestamp(ipo_date) + pd.Timedelta(days=10)).date().isoformat(), auto_adjust=False)
            first = h.index.min()
            older = yf.Ticker(sym).history(start=(pd.Timestamp(ipo_date) - pd.Timedelta(days=30)).date().isoformat(), end=ipo_date, auto_adjust=False)
        except Exception:
            continue
        if len(h) and older.empty and (first.tz_localize(None) - pd.Timestamp(ipo_date)).days <= 4:
            print(f"  {company}: matched {sym} ({nm}), first trade {first.date()}")
            return sym
    print(f"  {company}: no ticker match on Yahoo")
    return ""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    seed = json.loads(SEED.read_text())
    known = json.loads(KNOWN.read_text()) if KNOWN.exists() else []
    priced_nq, upcoming, filed = from_nasdaq()

    # Merge: seed first (hand-checked), then previously found, then new Nasdaq priced deals.
    ipos: dict[str, dict] = {}
    for r in seed + known:
        ipos.setdefault(norm(r["company"]), dict(r, source="seed" if r in seed else "nasdaq"))
    added = []
    for r in priced_nq:
        k = norm(r["company"])
        match = ipos.get(k) or next((v for kk, v in ipos.items() if kk.split(" ")[0] == k.split(" ")[0] and abs((pd.Timestamp(v["ipo_date"]) - pd.Timestamp(r["ipo_date"] or v["ipo_date"])).days) <= 7), None)
        if match:
            if not match.get("ticker") and r["ticker"]:
                match["ticker"] = r["ticker"]
            match.setdefault("exchange", r["exchange"])
        elif r["ipo_date"] and r["ipo_date"] >= YEAR_START.isoformat() and r["offer_price"]:
            ipos[k] = dict(r, source="nasdaq")
            added.append(r)
    if added:
        print("New IPOs from Nasdaq:", ", ".join(f"{r['company']} ({r['ticker']})" for r in added))

    for r in ipos.values():
        if not r.get("ticker"):
            r["ticker"] = yahoo_ticker(r["company"], r["ipo_date"])
    KNOWN.write_text(json.dumps([r for r in ipos.values() if r.get("source") == "nasdaq"], indent=1))

    tickers = sorted({r["ticker"] for r in ipos.values() if r.get("ticker")})
    print(f"{len(ipos)} IPOs, {len(tickers)} with tickers: {' '.join(tickers)}")
    px = yf.download(tickers + ["XBI", "IPO"], start=YEAR_START.isoformat(), interval="1d", auto_adjust=False, progress=False, threads=False)["Close"]
    px.index = pd.to_datetime(px.index).tz_localize(None)
    px = px.dropna(how="all")
    last_day = px.index.max()

    rows, price_rows = [], []
    for r in sorted(ipos.values(), key=lambda x: x["ipo_date"], reverse=True):
        t, offer = r.get("ticker"), r.get("offer_price")
        row = {"id": f"ipo-{norm(r['company']).replace(' ', '-')}", "status": "priced", "company": r["company"], "ticker": t or "",
               "ipo_date": r["ipo_date"], "offer_price": offer, "raised_musd": r.get("raised_musd"), "exchange": r.get("exchange", ""),
               "first_close": None, "day1_pct": None, "last_close": None, "last_date": None, "return_pct": None,
               "xbi_same_period_pct": None, "vs_xbi_pct": None, "high_close": None, "low_close": None, "days_trading": None}
        if t and t in px.columns and offer:
            s = px[t].dropna()
            s = s[s.index >= pd.Timestamp(r["ipo_date"])]
            if len(s):
                first_day = s.index[0]
                xbi = px["XBI"].dropna()
                xbi_base = xbi[xbi.index < first_day]
                xbi_base = float(xbi_base.iloc[-1]) if len(xbi_base) else float(xbi.iloc[0])
                ret = (float(s.iloc[-1]) / offer - 1) * 100
                xret = (float(xbi.iloc[-1]) / xbi_base - 1) * 100
                row.update({"first_close": round(float(s.iloc[0]), 2), "day1_pct": round((float(s.iloc[0]) / offer - 1) * 100, 1),
                            "last_close": round(float(s.iloc[-1]), 2), "last_date": s.index[-1].strftime("%Y-%m-%d"),
                            "return_pct": round(ret, 1), "xbi_same_period_pct": round(xret, 1), "vs_xbi_pct": round(ret - xret, 1),
                            "high_close": round(float(s.max()), 2), "low_close": round(float(s.min()), 2), "days_trading": int(len(s))})
                for n, (d, v) in enumerate(s.items()):
                    price_rows.append({"id": f"{t}-{d.strftime('%Y-%m-%d')}", "ticker": t, "date": d.strftime("%Y-%m-%d"), "day_n": n + 1,
                                       "close": round(float(v), 2), "return_pct": round((float(v) / offer - 1) * 100, 1)})
        rows.append(row)

    for r in upcoming:
        rows.append({"id": f"up-{norm(r['company']).replace(' ', '-')}", "status": "upcoming", "company": r["company"], "ticker": r["ticker"],
                     "expected_date": r["expected_date"], "price_range": r["price_range"], "raised_musd": r["raised_musd"], "exchange": r["exchange"]})
    seen = {norm(r["company"]) for r in rows}
    for r in filed:
        if norm(r["company"]) in seen:
            continue
        seen.add(norm(r["company"]))
        rows.append({"id": f"filed-{norm(r['company']).replace(' ', '-')}", "status": "filed", "company": r["company"], "ticker": r["ticker"],
                     "filed_date": r["filed_date"], "raised_musd": r["raised_musd"]})

    # Equal-weight basket: each IPO enters at its offer price on its first trading day.
    idx_rows, level = [], 100.0
    xbi = px["XBI"].dropna()
    ipo_etf = px["IPO"].dropna() if "IPO" in px.columns else pd.Series(dtype=float)
    held_prev: dict[str, float] = {}
    offer_by = {r["ticker"]: r["offer_price"] for r in rows if r.get("status") == "priced" and r.get("ticker") and r.get("offer_price")}
    first_by = {r["ticker"]: r["ipo_date"] for r in rows if r.get("status") == "priced" and r.get("ticker")}
    for d in px.index:
        rets = []
        for t, offer in offer_by.items():
            if t not in px.columns or pd.isna(px.at[d, t]):
                continue
            if pd.Timestamp(first_by[t]) > d:
                continue
            prev = held_prev.get(t, offer)
            rets.append(float(px.at[d, t]) / prev - 1)
            held_prev[t] = float(px.at[d, t])
        if rets:
            level *= 1 + sum(rets) / len(rets)
        idx_rows.append({"date": d.strftime("%Y-%m-%d"), "basket": round(level, 2), "n_ipos": len(rets),
                         "xbi": round(float(xbi.loc[:d].iloc[-1]) / float(xbi.iloc[0]) * 100, 2) if len(xbi.loc[:d]) else None,
                         "ipo_etf": round(float(ipo_etf.loc[:d].iloc[-1]) / float(ipo_etf.iloc[0]) * 100, 2) if len(ipo_etf.loc[:d]) else None})

    for name, data in (("ipos.json", rows), ("ipo_index.json", idx_rows), ("ipo_prices.json", price_rows)):
        (OUT / name).write_text(json.dumps(data, indent=1 if name == "ipos.json" else None))
    pr = [r for r in rows if r["status"] == "priced" and r.get("return_pct") is not None]
    above = sum(1 for r in pr if r["return_pct"] > 0)
    print(f"Prices through {last_day.date()}: {len(pr)} priced IPOs with prices ({above} above offer), "
          f"{sum(1 for r in rows if r['status'] == 'upcoming')} upcoming, {sum(1 for r in rows if r['status'] == 'filed')} filed. "
          f"Basket {idx_rows[-1]['basket']}, XBI {idx_rows[-1]['xbi']}, IPO ETF {idx_rows[-1]['ipo_etf']}.")
    missing = [r["company"] for r in rows if r["status"] == "priced" and not r.get("ticker")]
    if missing:
        print("No ticker found for:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
