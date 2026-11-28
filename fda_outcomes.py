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

# Decision language only. "FDA approval" as a noun ("approval odds", "approval hinges on")
# is not a decision, so the approval pattern needs a verb: approves / approved / wins approval.
APPROVED = re.compile(
    r"\bfda\b.{0,20}\b(approves|approved|clears|cleared|grants (\w+ )?approval)\b"
    r"|\bapproved by (the )?(u\.?s\.? )?fda\b"
    r"|\b(wins?|gets?|receives?|secures?|earns?|scores?|lands?|nabs?|bags?|grabs?)\b.{0,30}\b(approval|nod|green ?light)\b"
    r"|\bfda\b.{0,20}\b(expands|broadens)\b.{0,40}\b(approval|label|indication)\b"
    r"|\bfda (approval|nod) (for|of)\b.{0,60}\b(granted|secured|received|won)\b",
    re.I,
)
CRL = re.compile(r"complete response letter|\bCRL\b|\bfda\b.{0,20}\b(rejects|rejected|declines to approve|turns down|spurns|snubs)\b|\b(rejected|turned down) by (the )?(u\.?s\.? )?fda\b", re.I)
EXTENDED = re.compile(r"new (pdufa|target action|goal) (\w+ )?date|(extend|extension|pushes back|delays?|postpones?) .*(review|PDUFA|decision|goal date|action date)|(PDUFA|goal date|action date|decision) .*(extended|extension|pushed|delayed|moved)", re.I)
# Speculation, previews, roundups and calendars. Any headline matching this is skipped for
# every label, because it talks about a decision without reporting one.
NOT_DECISION = re.compile(
    r"\b(odds|chances?|likel(y|ihood)|probabilit\w*|hinges?|could|may|might|would|should|will the|if approved|potential(ly)?|"
    r"expected|expects?|anticipat\w*|ahead of|awaits?|awaiting|looms?|nears?|upcoming|pending|preview|what to expect|"
    r"bets?|betting|options? (traders?|activity)|stocks? to (watch|buy)|catalysts? (to watch|calendar|this (week|month))|"
    r"pdufa dates\b|fda calendar|drug alerts?|approvals?,|countdown|path to|set for|slated|scheduled|"
    r"accepts|acceptance|seeks|submits|submission|filing|resubmi\w*|advisory (committee|panel)|adcom|panel vote)\b|\?",
    re.I,
)
# A decided outcome (Approved / CRL) counts as confirmed when a primary or trade source reports
# it, or two different publishers do. Anything less stays unconfirmed and gets re-checked.
TRUSTED = re.compile(
    r"globenewswire|business ?wire|pr ?newswire|accesswire|reuters|associated press|\bap news\b|stat\b|statnews|fierce|endpoints|biospace|"
    r"biopharma dive|bloomberg|wall street journal|wsj|financial times|cnbc|fda\.gov|u\.s\. food and drug|onclive|cancer ?network|targeted onc|"
    r"hcplive|medpage|healio|neurology ?live|ajmc|pharmaceutical technology|pharmaphorum|evaluate|the pharma letter|in vivo|scrip",
    re.I,
)


STOP = {"and", "the", "for", "with", "of", "in", "or", "to", "a", "an", "plus", "disease", "patients", "treatment", "therapy",
        "cancer", "tumor", "tumors", "adult", "adults", "pediatric", "fda", "sbla", "snda", "bla", "nda"}
SYNONYMS = {"early": ["early", "adjuvant", "early-stage"], "advanced": ["advanced", "metastatic", "late-stage"],
            "metastatic": ["metastatic", "advanced"], "adjuvant": ["adjuvant", "early", "early-stage"]}


def words(text: str | None) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9-]+", (text or "").lower()) if len(w) >= 3 and w not in STOP}


def distinct_terms(ev: dict, events: list[dict]) -> tuple[set[str], set[str]]:
    """Words that tell this event apart from the same sponsor's other FDA events (same ticker),
    e.g. Roche's giredestrant in early breast cancer vs giredestrant + everolimus in advanced
    disease. Returns (this event's distinctive words, the siblings' distinctive words)."""
    mine = words(ev.get("drug")) | words(ev.get("indication"))
    others: set[str] = set()
    for o in events:
        if o is not ev and o["ticker"] == ev["ticker"]:
            others |= words(o.get("drug")) | words(o.get("indication"))
    return mine - others, others - mine


def about_this_event(title: str, own: set[str], sibling: set[str]) -> bool:
    """False when a headline names a sibling event's drug/indication and nothing specific to this
    one, so one sponsor's decision is not credited to its other application."""
    if not sibling:
        return True
    t = title.lower()
    has = lambda terms: any(re.search(rf"\b{re.escape(x)}\b", t) for w in terms for x in SYNONYMS.get(w, [w]))
    return not has(sibling) or has(own)


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


def classify(items: list[dict]) -> tuple[str, dict | None, bool]:
    """Return (outcome, first matching headline, confirmed).

    Speculative or roundup headlines are ignored for every label. Approved / CRL need
    confirmation (a trusted publisher, or two different publishers); without it the event
    is reported as "No news found" so it is not shown as decided, and is checked again."""
    reports = [h for h in items if not NOT_DECISION.search(h["title"])]
    for label, pat in (("CRL", CRL), ("Approved", APPROVED), ("Extended", EXTENDED)):
        hits = [h for h in reports if pat.search(h["title"])]
        if not hits:
            continue
        if label == "Extended":
            return label, hits[0], True
        publishers = {(h["publisher"] or "").strip().lower() for h in hits}
        trusted = [h for h in hits if TRUSTED.search(h["publisher"] or "")]
        if trusted or len(publishers - {""}) >= 2:
            return label, hits[0], True
        return "No news found", hits[0], False
    return "No news found", None, False


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
        own, sibling = distinct_terms(ev, events)
        if prev and (prev.get("final") or prev.get("manual")):
            continue  # manual = corrected by hand; never overwritten
        start = (pdufa - pd.Timedelta(days=BEFORE_DAYS)).tz_localize("UTC")
        end = min(pdufa + pd.Timedelta(days=AFTER_DAYS), today).tz_localize("UTC")
        items = headlines(ev["company"], ev["ticker"], ev["drug"], start, end)
        items = [h for h in items if about_this_event(h["title"], own, sibling)]
        outcome, hit, confirmed = classify(items)
        decided = outcome != "No news found"
        if pdufa > today and not decided:
            if hit:
                print(f"{ev['ticker']:6} {ev['date']}  unconfirmed    | {hit['title'][:80]} ({hit['publisher']})")
            continue  # upcoming and nothing confirmed yet
        px = prices(ev["ticker"], start.tz_localize(None), end.tz_localize(None))
        rx = reaction(px, hit["published"] if decided else None, pdufa, start.tz_localize(None), end.tz_localize(None))
        # Final only once a decision is confirmed, or the search window has closed.
        final = today >= pdufa + pd.Timedelta(days=FINAL_AFTER_DAYS - 1) or (outcome in ("Approved", "CRL") and confirmed)
        saved[ev["id"]] = {
            "id": ev["id"], "ticker": ev["ticker"], "company": ev["company"], "drug": ev["drug"],
            "indication": ev.get("indication"), "pdufa_date": ev["date"],
            "outcome": outcome,
            "confirmed": bool(decided and confirmed),
            "decision_date": hit["published"].tz_convert("America/New_York").strftime("%Y-%m-%d") if decided else None,
            "headline": hit["title"] if decided else None,
            "source_name": hit["publisher"] if decided else None,
            "source_url": hit["link"] if decided else None,
            "unconfirmed_headline": hit["title"] if hit and not decided else None,
            **rx,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "final": bool(final),
        }
        checked += 1
        tail = hit["title"][:80] if hit else ""
        print(f"{ev['ticker']:6} {ev['date']}  {outcome:14} {'' if confirmed or not hit else '(unconfirmed) '}reaction {rx['reaction_pct']}%  window {rx['window_pct']}%  | {tail}")
    OUT.write_text(json.dumps(sorted(saved.values(), key=lambda o: o["pdufa_date"], reverse=True), indent=1))
    print(f"Checked {checked} events; {len(saved)} outcomes saved. ({newsrss.summary()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
