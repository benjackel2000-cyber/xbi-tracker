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
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).parent / "data"
OUT = ROOT / "ipo"
SEED = ROOT / "ipo_seed.json"
KNOWN = OUT / "known.json"          # IPOs the script found on its own in earlier runs
# Deals to leave out even though they look like biotech IPOs (relistings, non-drug companies). Names as filed.
EXCLUDE_FILE = ROOT / "ipo_exclude.json"
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


EXCLUDE = set()
CLASS_FILE = OUT / "deal_class.json"   # deal id -> {"bio": bool, "desc": "..."} so each deal is looked up once
DESC_BIO = re.compile(r"biotechnology company|biopharmaceutical company|biopharma company|clinical[- ]stage|pharmaceutical company|therapeutics company|biotherapeutics|life sciences company|drug discovery|generative biology|immunotherap", re.I)
DESC_NOT = re.compile(r"blank check|special purpose acquisition|business combination with|medical device company|diagnostic laboratory|cannabis|hemp|dietary supplement|cosmetic|insurance|bank holding", re.I)
SKIP_NAME = re.compile(r"acquisition|\bspac\b|bancorp|\bbank\b|capital corp|treasury|insurance|mining|gold|realty|\breit\b|data centers?", re.I)


def is_biotech(row: dict, cache: dict) -> bool:
    name, did = row.get("companyName") or "", row.get("dealID") or ""
    if SKIP_NAME.search(name):
        return False
    if did in cache:
        return classify(cache[did].get("desc", ""), name)
    desc = ""
    try:
        time.sleep(0.25)
        o = requests.get(f"https://api.nasdaq.com/api/ipo/overview/?dealId={did}", headers=UA, timeout=30).json().get("data") or {}
        desc = ((o.get("companyInformation") or {}).get("companyDescription") or "")[:1500]
    except Exception as exc:
        print(f"  overview failed for {name}: {exc}")
        return bool(BIO.search(name) and not NOT_BIO.search(name))  # fall back to the name, don't cache
    desc = re.sub(r"\s+", " ", desc)[:400]
    bio = classify(desc, name)
    cache[did] = {"bio": bio, "name": name, "desc": desc}
    return bio


def classify(desc: str, name: str) -> bool:
    """Biotech if the start of the company's own description says so; the name alone only when there's no description."""
    if norm(name) in EXCLUDE:
        return False
    if desc:
        return bool(DESC_BIO.search(desc[:400]) and not DESC_NOT.search(desc[:400]))
    return bool(BIO.search(name) and not NOT_BIO.search(name))


def from_nasdaq() -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (priced, upcoming, filed) biotech deals from Nasdaq's calendar."""
    priced, upcoming, filed, withdrawn = [], [], [], set()
    cache = json.loads(CLASS_FILE.read_text()) if CLASS_FILE.exists() else {}
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
        withdrawn |= {norm(r.get("companyName") or "") for r in rows_of(data.get("withdrawn"))}
        print(f"Nasdaq calendar {ym}: {len(p)} priced, {len(u)} upcoming, {len(f)} filed")
        for r in p:
            name = r.get("companyName") or ""
            if is_biotech(r, cache):
                priced.append({"company": name, "ticker": (r.get("proposedTickerSymbol") or "").strip(),
                               "ipo_date": _iso(r.get("pricedDate")), "offer_price": money(r.get("proposedSharePrice")),
                               "raised_musd": _m(r.get("dollarValueOfSharesOffered")), "exchange": r.get("proposedExchange") or ""})
        for r in u:
            name = r.get("companyName") or ""
            if is_biotech(r, cache):
                upcoming.append({"company": name, "ticker": (r.get("proposedTickerSymbol") or "").strip(),
                                 "expected_date": _iso(r.get("expectedPriceDate")), "price_range": r.get("proposedSharePrice") or "",
                                 "raised_musd": _m(r.get("dollarValueOfSharesOffered")), "exchange": r.get("proposedExchange") or ""})
        for r in f:
            name = r.get("companyName") or ""
            if is_biotech(r, cache):
                filed.append({"company": name, "ticker": (r.get("proposedTickerSymbol") or "").strip(),
                              "filed_date": _iso(r.get("filedDate")), "raised_musd": _m(r.get("dollarValueOfSharesOffered"))})
    CLASS_FILE.write_text(json.dumps(cache, indent=0))
    done = {norm(x["company"]) for x in priced} | withdrawn
    recent = (date.today() - timedelta(days=150)).isoformat()
    filed = [x for x in filed if norm(x["company"]) not in done and x["filed_date"] >= recent]
    upcoming = [x for x in upcoming if norm(x["company"]) not in done]
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


def traded_before(ticker: str, ipo_date: str) -> bool:
    """True if Yahoo has prices well before the 'IPO' date (an uplisting or relisting, not a new company)."""
    if not ticker:
        return False
    try:
        h = yf.Ticker(ticker).history(start=(pd.Timestamp(ipo_date) - pd.Timedelta(days=400)).date().isoformat(),
                                      end=(pd.Timestamp(ipo_date) - pd.Timedelta(days=20)).date().isoformat(), auto_adjust=False)
        if len(h) > 5:
            print(f"  {ticker} traded before {ipo_date}: treated as a relisting, not an IPO")
            return True
    except Exception:
        pass
    return False


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
    if EXCLUDE_FILE.exists():
        EXCLUDE.update(norm(n) for n in json.loads(EXCLUDE_FILE.read_text()))
    seed = json.loads(SEED.read_text())
    known = [k for k in (json.loads(KNOWN.read_text()) if KNOWN.exists() else [])
             if norm(k["company"]) not in EXCLUDE and not traded_before(k.get("ticker", ""), k["ipo_date"])]
    priced_nq, upcoming, filed = from_nasdaq()

    # Merge: seed first (hand-checked), then previously found, then new Nasdaq priced deals.
    ipos: dict[str, dict] = {}
    for r in seed + known:
        ipos.setdefault(norm(r["company"]), dict(r, source="seed" if r in seed else "nasdaq"))
    added = []
    for r in priced_nq:
        k = norm(r["company"])
        c = k.replace(" ", "")
        match = ipos.get(k) or next((v for kk, v in ipos.items()
                                     if (kk.replace(" ", "").startswith(c[:6]) or c.startswith(kk.replace(" ", "")[:6]))
                                     and abs((pd.Timestamp(v["ipo_date"]) - pd.Timestamp(r["ipo_date"] or v["ipo_date"])).days) <= 7), None)
        if match:
            if not match.get("ticker") and r["ticker"]:
                match["ticker"] = r["ticker"]
            match.setdefault("exchange", r["exchange"])
        elif r["ipo_date"] and r["ipo_date"] >= YEAR_START.isoformat() and r["offer_price"] and not traded_before(r["ticker"], r["ipo_date"]):
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

    # "$1,000 in every biotech IPO at its offer price" vs the same money put into XBI or the IPO ETF
    # on each IPO's first trading day. Buy and hold, no rebalancing; returns are on total money invested.
    xbi = px["XBI"].dropna()
    ipo_etf = px["IPO"].dropna() if "IPO" in px.columns else pd.Series(dtype=float)
    lots = []  # (ticker, first_day, offer, xbi_at_entry, etf_at_entry)
    for r in rows:
        t = r.get("ticker")
        if r.get("status") != "priced" or not t or t not in px.columns or not r.get("offer_price"):
            continue
        s_ = px[t].dropna()
        s_ = s_[s_.index >= pd.Timestamp(r["ipo_date"])]
        if not len(s_):
            continue
        d0 = s_.index[0]
        xb = xbi[xbi.index < d0]
        eb = ipo_etf[ipo_etf.index < d0]
        lots.append((t, d0, float(r["offer_price"]), float(xb.iloc[-1]) if len(xb) else float(xbi.iloc[0]),
                     float(eb.iloc[-1]) if len(eb) else (float(ipo_etf.iloc[0]) if len(ipo_etf) else None)))
    idx_rows = []
    last_px: dict[str, float] = {}
    for d in px.index:
        held = [l for l in lots if l[1] <= d]
        if not held:
            continue
        v_ipo = v_xbi = v_etf = 0.0
        etf_ok = True
        for t, d0, offer, xb0, eb0 in held:
            p = px.at[d, t]
            if not pd.isna(p):
                last_px[t] = float(p)
            v_ipo += 1000 * last_px.get(t, offer) / offer
            v_xbi += 1000 * float(xbi.loc[:d].iloc[-1]) / xb0
            if eb0 and len(ipo_etf.loc[:d]):
                v_etf += 1000 * float(ipo_etf.loc[:d].iloc[-1]) / eb0
            else:
                etf_ok = False
        inv = 1000 * len(held)
        idx_rows.append({"date": d.strftime("%Y-%m-%d"), "n_ipos": len(held), "invested": inv,
                         "ipo_return_pct": round((v_ipo / inv - 1) * 100, 2),
                         "xbi_return_pct": round((v_xbi / inv - 1) * 100, 2),
                         "ipo_etf_return_pct": round((v_etf / inv - 1) * 100, 2) if etf_ok else None})

    for name, data in (("ipos.json", rows), ("ipo_index.json", idx_rows), ("ipo_prices.json", price_rows)):
        (OUT / name).write_text(json.dumps(data, indent=1 if name == "ipos.json" else None))
    pr = [r for r in rows if r["status"] == "priced" and r.get("return_pct") is not None]
    above = sum(1 for r in pr if r["return_pct"] > 0)
    print(f"Prices through {last_day.date()}: {len(pr)} priced IPOs with prices ({above} above offer), "
          f"{sum(1 for r in rows if r['status'] == 'upcoming')} upcoming, {sum(1 for r in rows if r['status'] == 'filed')} filed. "
          f"Every IPO at offer: {idx_rows[-1]['ipo_return_pct']}% vs same money in XBI {idx_rows[-1]['xbi_return_pct']}%, IPO ETF {idx_rows[-1]['ipo_etf_return_pct']}%.")
    missing = [r["company"] for r in rows if r["status"] == "priced" and not r.get("ticker")]
    if missing:
        print("No ticker found for:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
