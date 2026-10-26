"""
Catalyst news: FDA decisions and Phase 2 / Phase 3 trial readouts.

Runs after the market close. Two kinds of searches (Google News RSS):

  1. Broad searches for the last ~2 days: "FDA approves", "complete response letter",
     "Phase 3 topline results", "Phase 2 data", etc. Only stories that name a public
     company we can price (XBI holdings + the catalyst list + a few big pharmas) are kept.
  2. Targeted readout checks for trials on the catalyst calendar (data/catalysts.json)
     whose estimated completion date is close or has passed. About 30 trials are checked
     per run, rotating, so every due/overdue trial gets looked at every few days.
     When a results headline is found the trial is recorded in data/trial_results.json
     and is no longer re-checked.

Each story gets a rough type guess (FDA approval / CRL / Phase 3 readout / Phase 2
readout) and the stock's move on the reaction day. The dashboard's evening update reads
data/catalyst_news/latest.json and has Claude write the final classification
(positive / negative / mixed) and one-line summary.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).parent / "data"
OUT_DIR = ROOT / "catalyst_news"
RESULTS_FILE = ROOT / "trial_results.json"
DAYS_BACK = 2
CHECKS_PER_RUN = 30
DUE_AHEAD_DAYS = 30      # start checking a trial this long before its estimated completion
DUE_BEHIND_DAYS = 365    # keep checking overdue trials for up to a year

BROAD = [
    "FDA approves",
    "FDA approval biotech",
    "complete response letter FDA",
    "Phase 3 topline results",
    "Phase 3 trial met primary endpoint",
    "Phase 3 did not meet primary endpoint",
    "pivotal trial results biotech",
    "Phase 2 topline data",
    "Phase 2 trial results biotech",
    "Phase 2b results primary endpoint",
    "site:globenewswire.com topline results",
    "site:businesswire.com topline results",
    "site:prnewswire.com topline results",
    "site:biospace.com Phase 3 results",
    "site:fiercebiotech.com phase data",
    "site:endpts.com data",
]

BIG = {
    "MRK": "Merck", "BMY": "Bristol Myers", "RHHBY": "Roche", "SNY": "Sanofi", "GILD": "Gilead",
    "PFE": "Pfizer", "LLY": "Eli Lilly", "ABBV": "AbbVie", "AZN": "AstraZeneca", "NVS": "Novartis",
    "NVO": "Novo Nordisk", "JNJ": "Johnson & Johnson", "GSK": "GSK", "AMGN": "Amgen", "BIIB": "Biogen",
    "REGN": "Regeneron", "VRTX": "Vertex", "TAK": "Takeda", "BAYRY": "Bayer",
}

APPROVED = re.compile(r"\b(fda (approv\w*|clears|grants (accelerated )?approval)|approv(es|ed|al) (of|for)|wins (fda )?approval|gets (fda )?(accelerated )?approval|receives (fda )?(accelerated )?approval|secures (fda )?approval)", re.I)
CRL = re.compile(r"complete response letter|\bCRL\b|rejects|declines to approve|turns down", re.I)
READOUT = re.compile(r"topline|top-line|primary endpoint|met (its|the) |fail(s|ed)? to|did not meet|missed|positive (results|data)|results from|data from|readout|reads? out|efficacy|statistically significant|interim analysis", re.I)
PH3 = re.compile(r"phase (3|iii)\b|phase 2/3|pivotal|registrational", re.I)
PH2 = re.compile(r"phase (2|ii)(b|a)?\b", re.I)
NOT_EVENT = re.compile(r"accepts|acceptance|seeks|submits|submission|filing|PDUFA date|ahead of|awaits|could|will the|what to expect|preview|to present|will present|to host|conference call|enrol|initiat|first patient|doses first|begins|starts|launch(es)? (a )?phase", re.I)
NEG = re.compile(r"fail|did not meet|missed|discontinu|halt|crl|complete response|rejects|setback|disappoint|plunge|tumble|sink|tank", re.I)
POS = re.compile(r"\bmet\b|positive|success|approv|statistically significant|soar|surge|jump|beats", re.I)


def fetch_rss(query: str) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (xbi-tracker catalyst news)"}, timeout=30)
    resp.raise_for_status()
    out = []
    for it in ET.fromstring(resp.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        publisher = it.findtext("source")
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(" - " + publisher)]
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except Exception:
            ts = None
        out.append({"title": title, "publisher": publisher, "link": (it.findtext("link") or "").strip(),
                    "published": ts, "summary": re.sub(r"<[^>]+>", " ", it.findtext("description") or "")})
    return out


def short_name(name: str) -> str:
    s = re.sub(r",|\.", "", name)
    s = re.sub(r"\b(Inc|Corp|Corporation|Ltd|Limited|Plc|NV|SA|SE|AG|Holdings?|Co|Company|Class [A-C]|ADR|Sponsored|Group)\b", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def build_names(catalysts: list[dict]) -> dict[str, list[str]]:
    """ticker -> names to look for in headlines."""
    names: dict[str, set[str]] = {}

    def add(t: str, n: str) -> None:
        n = short_name(n)
        if len(n) >= 4:
            names.setdefault(t, set()).add(n)
            first = n.split(" ")[0]
            # "Ionis Pharmaceuticals" -> also "Ionis" when the first word is distinctive enough
            if len(first) >= 5 and first.lower() not in {"united", "summit", "global", "first", "general", "international", "american", "applied", "precision", "biohaven"}:
                names[t].add(first)

    try:
        for r in json.loads((ROOT / "latest.json").read_text())["rows"]:
            add(r["ticker"], r["name"])
    except Exception as exc:
        print(f"Could not read XBI holdings: {exc}")
    for r in catalysts:
        for t, n in zip(r["ticker"].split("/"), r["company"].split(" / ")):
            add(t, n)
    for t, n in BIG.items():
        add(t, n)
    return {t: sorted(v, key=len, reverse=True) for t, v in names.items()}


def match_ticker(text: str, names: dict[str, list[str]]) -> str | None:
    m = re.search(r"\((?:NASDAQ|NYSE|Nasdaq|NYSE American|OTC\w*)\s*:\s*([A-Z.]{1,6})\)", text)
    if m and m.group(1) in names:
        return m.group(1)
    best = None
    for t, ns in names.items():
        for n in ns:
            i = re.search(rf"\b{re.escape(n)}\b", text)
            if i and (best is None or i.start() < best[1]):
                best = (t, i.start())
    return best[0] if best else None


def guess_type(title: str) -> str | None:
    if CRL.search(title) and not NOT_EVENT.search(title):
        return "CRL"
    if APPROVED.search(title) and not NOT_EVENT.search(title):
        return "FDA approval"
    if READOUT.search(title) and not NOT_EVENT.search(title):
        if PH3.search(title):
            return "Phase 3 readout"
        if PH2.search(title):
            return "Phase 2 readout"
        return "Trial readout"
    return None


def guess_result(title: str, typ: str) -> str:
    if typ == "CRL":
        return "negative"
    if typ == "FDA approval":
        return "positive"
    n, p = bool(NEG.search(title)), bool(POS.search(title))
    return "mixed" if n == p else ("negative" if n else "positive")


def reaction(ticker: str, published: datetime | None) -> dict:
    out = {"move_date": None, "move_pct": None, "close": None}
    try:
        df = yf.download(ticker.replace(".", "-"), period="15d", interval="1d", auto_adjust=False, progress=False, threads=False)
        px = df["Close"].squeeze().dropna()
        px.index = pd.to_datetime(px.index).tz_localize(None)
    except Exception as exc:
        print(f"Price lookup failed for {ticker}: {exc}")
        return out
    if len(px) < 2:
        return out
    rets = px.pct_change() * 100
    day = None
    if published is not None:
        et = pd.Timestamp(published).tz_convert("America/New_York")
        day = pd.Timestamp(et.date())
        if et.hour >= 16:
            day += pd.Timedelta(days=1)
    after = rets[rets.index >= day].dropna() if day is not None else rets.dropna().tail(1)
    if after.empty:
        after = rets.dropna().tail(1)
    out["move_date"] = after.index[0].strftime("%Y-%m-%d")
    out["move_pct"] = round(float(after.iloc[0]), 2)
    out["close"] = round(float(px.iloc[-1]), 2)
    return out


def drug_terms(drug: str) -> list[str]:
    words = [w for w in re.split(r"[\s/,+()]+", drug or "") if len(w) >= 6 and not re.search(r"^(placebo|injection|tablet|capsule|matched|standard|biopsy|procedure|biospecimen|collection|therapy|radiation|chemotherapy|infusion|vaccine|dose)$", w, re.I)]
    return words[:2]


def main() -> int:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=DAYS_BACK)
    catalysts = json.loads((ROOT / "catalysts.json").read_text())
    results = json.loads(RESULTS_FILE.read_text()) if RESULTS_FILE.exists() else {}
    names = build_names(catalysts)
    print(f"Watching {len(names)} tickers, {len(catalysts)} catalyst rows, {len(results)} trials already reported.")

    items: list[dict] = []
    for q in BROAD:
        try:
            got = fetch_rss(f"{q} when:{DAYS_BACK}d")
        except Exception as exc:
            print(f"Broad '{q}': failed ({exc})")
            continue
        kept = 0
        for it in got:
            if it["published"] and it["published"] < since:
                continue
            typ = guess_type(it["title"])
            if not typ:
                continue
            t = match_ticker(it["title"] + " " + it["summary"], names)
            if not t:
                continue
            items.append({**it, "ticker": t, "type_guess": typ, "found_by": f"search: {q}", "catalyst_id": None})
            kept += 1
        print(f"Broad '{q}': {len(got)} items, {kept} kept")

    # Targeted readout checks, rotating through due and overdue trials.
    today = pd.Timestamp(now.date())
    due = [r for r in catalysts if "readout" in r["kind"] and r["id"] not in results
           and -DUE_BEHIND_DAYS <= (pd.Timestamp(r["date"]) - today).days <= DUE_AHEAD_DAYS]
    due.sort(key=lambda r: r["date"])
    if due:
        start = (now.timetuple().tm_yday * CHECKS_PER_RUN) % len(due)
        batch = (due[start:] + due[:start])[:CHECKS_PER_RUN]
    else:
        batch = []
    print(f"{len(due)} trials due or overdue; checking {len(batch)} this run.")
    for r in batch:
        tickers = r["ticker"].split("/")
        comp = [short_name(c) for c in r["company"].split(" / ")]
        terms = [f'"{c}"' for c in comp] + [f'"{d}"' for d in drug_terms(r["drug"])]
        after = (pd.Timestamp(r["date"]) - pd.Timedelta(days=120)).date()
        q = f'({" OR ".join(terms)}) (topline OR "primary endpoint" OR results OR data) phase after:{after}'
        try:
            got = fetch_rss(q)
        except Exception as exc:
            print(f"Check {r['id']}: failed ({exc})")
            continue
        dterms = [d.lower() for d in drug_terms(r["drug"])]
        hit = None
        for it in got:
            title = it["title"]
            typ = guess_type(title)
            if not typ or "readout" not in typ.lower():
                continue
            named_co = any(re.search(rf"\b{re.escape(c)}\b", title, re.I) for c in comp) or any(re.search(rf"\b{re.escape(t)}\b", title) for t in tickers)
            named_drug = any(d in title.lower() for d in dterms) or (r["indication"] and r["indication"].split(" ")[0].lower() in title.lower())
            if named_co and named_drug:
                hit = (it, typ)
                break
        if hit:
            it, typ = hit
            results[r["id"]] = {"found": now.date().isoformat(), "title": it["title"], "link": it["link"],
                                "published": it["published"].isoformat() if it["published"] else None,
                                "type_guess": typ, "result_guess": guess_result(it["title"], typ)}
            items.append({**it, "ticker": tickers[0], "type_guess": typ, "found_by": "trial check", "catalyst_id": r["id"]})
            print(f"Check {r['id']} ({r['ticker']}): reported -> {it['title'][:90]}")
        else:
            print(f"Check {r['id']} ({r['ticker']}): nothing yet")

    # De-duplicate and add stock moves.
    seen, unique = set(), []
    for it in sorted(items, key=lambda x: (x["catalyst_id"] is None, -(x["published"].timestamp() if x["published"] else 0))):
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    cache: dict[str, dict] = {}
    out = []
    for it in unique:
        t = it["ticker"]
        if (t, it["published"]) not in cache:
            cache[(t, it["published"])] = reaction(t, it["published"])
        typ = it["type_guess"]
        out.append({
            "ticker": t, "title": it["title"], "publisher": it["publisher"], "link": it["link"],
            "published": it["published"].isoformat() if it["published"] else None,
            "type_guess": typ, "result_guess": guess_result(it["title"], typ),
            "catalyst_id": it["catalyst_id"], "found_by": it["found_by"],
            **cache[(t, it["published"])],
        })

    result = {"generated_at": now.isoformat(timespec="seconds"), "covers_since": since.isoformat(timespec="seconds"),
              "trials_checked": [r["id"] for r in batch], "count": len(out), "items": out}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in (OUT_DIR / "latest.json", OUT_DIR / f"{now.date()}.json"):
        path.write_text(json.dumps(result, indent=1))
    RESULTS_FILE.write_text(json.dumps(results, indent=1))
    for old in OUT_DIR.glob("20*.json"):
        try:
            if datetime.fromisoformat(old.stem).date() < (now - timedelta(days=30)).date():
                old.unlink()
        except ValueError:
            pass
    print(f"Saved {len(out)} catalyst stories; {len(results)} trials marked reported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
