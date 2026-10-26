"""
FDA decision outcome tracker.

For each FDA decision (PDUFA) date in data/fda_events.json that falls in a recent
window, this script:
  1. Searches Google News from 10 days before the PDUFA date to 3 days after for
     headlines about the decision, and classifies the outcome:
       Approved / CRL (rejected) / Extended / No news found.
  2. Uses the first such headline to find the actual decision day (decisions
     often come a few days early, or after the market closes).
  3. Pulls daily prices and measures the stock's reaction:
       - reaction_pct: the move on the first trading day the market could react
       - window_pct:   the move from 10 days before the PDUFA date to now / 3 days after
  4. Saves everything to data/fda_outcomes.json (one entry per event, kept over time).
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests

import newsrss
import yfinance as yf

DATA_DIR = Path(__file__).parent / "data"
EVENTS = DATA_DIR / "fda_events.json"
OUT = DATA_DIR / "fda_outcomes.json"

BEFORE_DAYS, AFTER_DAYS = 10, 3   # search window around the PDUFA date
LOOKAHEAD_DAYS = 10               # also check upcoming dates this close, for early decisions
FINAL_AFTER_DAYS = 14             # stop re-checking an event this long after its date
LOOKBACK_DAYS = int(__import__("os").environ.get("LOOKBACK_DAYS") or FINAL_AFTER_DAYS)  # widen for a one-off backfill

APPROVED = re.compile(r"\b(fda (approv\w*|clears|grants approval)|approv(es|ed|al) (of|for)|wins (fda )?approval|gets (fda )?approval|receives (fda )?approval|secures (fda )?approval|green ?light)", re.I)
CRL = re.compile(r"complete response letter|\bCRL\b|rejects|declines to approve|turns down", re.I)
EXTENDED = re.compile(r"(extend|extension|pushes back|delays?) .*(review|PDUFA|decision|goal date)|PDUFA .*(extended|extension)", re.I)
NOT_DECISION = re.compile(r"accepts|acceptance|seeks|submits|filing|PDUFA date set|ahead of|awaits|could|will the|what to expect", re.I)


def headlines(company: str, ticker: str, drug: str, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    short = re.sub(r"\b(Inc|Corp|Ltd|Plc|N\.?V\.?|S\.?A\.?|SE|AG|Holdings?|Co)\b\.?", "", company, flags=re.I).strip()
    terms = [f'"{short}"', f'"{ticker}"']
    if drug and not re.search(r"see filing|partner drug", drug, re.I):
        terms.append(f'"{drug.split(" ")[0]}"')
    query = f'({" OR ".join(terms)}) FDA after:{(start - pd.Timedelta(days=1)).date()} before:{(end + pd.Timedelta(days=1)).date()}'
    try:
        items = newsrss.search(query)
    except Exception as exc:
        print(f"News lookup failed for {ticker}: {exc}")
        return []
    out = []
    for item in items:
        title, publisher = item["title"], item["publisher"]
        if item["published"] is None:
            continue
        ts = pd.Timestamp(item["published"]).tz_convert("UTC")
        if not (start <= ts <= end + pd.Timedelta(days=1)):
            continue
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(" - " + publisher)]
        named = re.search(rf"\b{re.escape(ticker)}\b", title) or short.lower() in title.lower() or (drug and drug.split(" ")[0].lower() in title.lower())
        if named:
            out.append({"title": title, "publisher": publisher, "link": item["link"], "published": ts})
    return sorted(out, key=lambda h: h["published"])


def classify(items: list[dict]) -> tuple[str, dict | None]:
    for label, pat in (("CRL", CRL), ("Approved", APPROVED), ("Extended", EXTENDED)):
        for h in items:
            if pat.search(h["title"]) and not (label == "Approved" and NOT_DECISION.search(h["title"])):
                return label, h
    return "No news found", None


def prices(ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    try:
        df = yf.download(ticker.replace(".", "-"), start=(start - pd.Timedelta(days=7)).date(), end=(end + pd.Timedelta(days=2)).date(),
                         interval="1d", auto_adjust=False, progress=False, threads=False)
        s = df["Close"].squeeze().dropna()
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s
    except Exception as exc:
        print(f"Price lookup failed for {ticker}: {exc}")
        return pd.Series(dtype=float)


def reaction(px: pd.Series, published: pd.Timestamp | None, pdufa: pd.Timestamp, window_start: pd.Timestamp, window_end: pd.Timestamp) -> dict:
    out = {"reaction_date": None, "reaction_pct": None, "window_pct": None, "biggest_day": None, "biggest_day_pct": None}
    if px.empty or len(px) < 2:
        return out
    rets = px.pct_change() * 100
    inwin = rets[(rets.index >= window_start) & (rets.index <= window_end)].dropna()
    if published is not None:
        et = published.tz_convert("America/New_York")
        day = pd.Timestamp(et.date())
        if et.hour >= 16:  # after the close -> next trading day reacts
            day += pd.Timedelta(days=1)
        after = rets[rets.index >= day].dropna()
        if not after.empty:
            out["reaction_date"] = after.index[0].strftime("%Y-%m-%d")
            out["reaction_pct"] = round(float(after.iloc[0]), 2)
    if not inwin.empty:
        big = inwin.abs().idxmax()
        out["biggest_day"] = big.strftime("%Y-%m-%d")
        out["biggest_day_pct"] = round(float(inwin[big]), 2)
    base = px[px.index < window_start]
    last = px[px.index <= window_end]
    if not base.empty and not last.empty:
        out["window_pct"] = round(float((last.iloc[-1] / base.iloc[-1] - 1) * 100), 2)
    return out


def main() -> int:
    events = json.loads(EVENTS.read_text())
    saved = {o["id"]: o for o in json.loads(OUT.read_text())} if OUT.exists() else {}
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    checked = 0
    for ev in events:
        pdufa = pd.Timestamp(ev["date"])
        if not (today - pd.Timedelta(days=LOOKBACK_DAYS) <= pdufa <= today + pd.Timedelta(days=LOOKAHEAD_DAYS)):
            continue
        prev = saved.get(ev["id"])
        if prev and prev.get("final"):
            continue
        start = (pdufa - pd.Timedelta(days=BEFORE_DAYS)).tz_localize("UTC")
        end = min(pdufa + pd.Timedelta(days=AFTER_DAYS), today).tz_localize("UTC")
        items = headlines(ev["company"], ev["ticker"], ev["drug"], start, end)
        outcome, hit = classify(items)
        if pdufa > today and outcome == "No news found":
            continue  # upcoming and nothing early yet
        px = prices(ev["ticker"], start.tz_localize(None), end.tz_localize(None))
        rx = reaction(px, hit["published"] if hit else None, pdufa, start.tz_localize(None), end.tz_localize(None))
        final = today >= pdufa + pd.Timedelta(days=FINAL_AFTER_DAYS - 1) or outcome in ("Approved", "CRL")
        saved[ev["id"]] = {
            "id": ev["id"], "ticker": ev["ticker"], "company": ev["company"], "drug": ev["drug"],
            "indication": ev.get("indication"), "pdufa_date": ev["date"],
            "outcome": outcome,
            "decision_date": hit["published"].tz_convert("America/New_York").strftime("%Y-%m-%d") if hit else None,
            "headline": hit["title"] if hit else None,
            "source_name": hit["publisher"] if hit else None,
            "source_url": hit["link"] if hit else None,
            **rx,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "final": bool(final),
        }
        checked += 1
        print(f"{ev['ticker']:6} {ev['date']}  {outcome:14} reaction {rx['reaction_pct']}%  window {rx['window_pct']}%  | {hit['title'][:80] if hit else ''}")
    OUT.write_text(json.dumps(sorted(saved.values(), key=lambda o: o["pdufa_date"], reverse=True), indent=1))
    print(f"Checked {checked} events; {len(saved)} outcomes saved. ({newsrss.summary()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
