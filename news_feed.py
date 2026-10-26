"""
Biotech deal & funding news feed.

Collects the last few days of headlines from biotech news sites and press-release
wires, keeps the ones that look like deals (licensing, M&A, partnerships) or
financings (venture rounds, IPOs, offerings), and saves them to
data/news/latest.json (plus a dated copy). The Biotech Tracker's morning update
reads this list instead of running lots of web searches.
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

import requests

DATA_DIR = Path(__file__).parent / "data" / "news"
DAYS_BACK = 3

FEEDS = {
    "BioPharma Dive": "https://www.biopharmadive.com/feeds/news/",
    "Fierce Biotech": "https://www.fiercebiotech.com/rss/xml",
    "PR Newswire (health)": "https://www.prnewswire.com/rss/health-latest-news/health-latest-news-list.rss",
}

# Google News searches catch announcements the feeds above miss, including sites that
# block direct feed requests (Endpoints, GlobeNewswire, Business Wire).
SEARCHES = [
    "site:endpts.com deal OR licensing OR acquire OR raises",
    "site:globenewswire.com therapeutics license agreement OR acquisition OR financing",
    "site:businesswire.com therapeutics license agreement OR acquisition OR financing",
    "site:biospace.com deal OR acquisition OR financing",
    "biotech license agreement upfront milestone",
    "pharma to acquire biotech per share",
    "biotech collaboration agreement upfront",
    "biotech Series A financing",
    "biotech Series B financing",
    "biotech Series C financing",
    "biotech IPO prices",
    "biotech raises million financing",
]

DEAL = re.compile(r"\b(acquir\w*|acquisition|merger|merge|buyout|take ?over|licen[cs]\w*|in-licens\w*|collaborat\w*|partner\w*|option|upfront|up-front|milestone|royalt\w*|rights to|deal)\b", re.I)
FUND = re.compile(r"\b(series [a-h]\b|seed|financing|raises?|raised|funding|ipo|initial public offering|public offering|private placement|pipe|venture|round)\b", re.I)
BIO = re.compile(r"\b(bio\w*|pharma\w*|therapeutic\w*|drug\w*|medicine\w*|oncology|cancer|tumou?r|antibod\w*|clinical|FDA|phase [1-3i]+|gene|cell therap\w*|rna\w*|sirna|vaccine\w*|obesity|glp-1|adc|bispecific|biologic\w*|molecule\w*|disease|rare|neuro\w*|immun\w*|life sciences?)\b", re.I)
BIO_OUTLETS = ("BioPharma Dive", "Fierce Biotech", "Endpoints", "BioSpace", "Fierce Pharma", "Genetic Engineering", "GEN", "BioWorld", "STAT", "Pharmaceutical Executive", "pharmaphorum", "The Pharma Letter", "PharmaShots", "MedCity News", "Labiotech")
NOT_BIO = re.compile(r"\b(crypto|bitcoin|real estate|bank|insurance|mining|oil|gas|solar|retail|restaurant|robotic\w*|cyber\w*|wealth|spacex|anthropic|openai|sheriff|county)\b", re.I)


def parse_feed(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for it in root.iter("item"):
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "published": it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"),
            "summary": re.sub(r"<[^>]+>", " ", it.findtext("description") or "").strip()[:400],
            "publisher": it.findtext("source"),
        })
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for it in root.findall(".//a:entry", ns):  # Atom feeds
        link = it.find("a:link", ns)
        items.append({
            "title": (it.findtext("a:title", default="", namespaces=ns) or "").strip(),
            "link": link.get("href") if link is not None else "",
            "published": it.findtext("a:updated", default=None, namespaces=ns) or it.findtext("a:published", default=None, namespaces=ns),
            "summary": re.sub(r"<[^>]+>", " ", it.findtext("a:summary", default="", namespaces=ns) or "").strip()[:400],
            "publisher": None,
        })
    return items


def to_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None


def fetch(url: str) -> bytes:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (xbi-tracker news feed)"}, timeout=30)
    resp.raise_for_status()
    return resp.content


def main() -> int:
    since = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    collected, status = [], {}
    sources = list(FEEDS.items()) + [
        (f"Google News: {q}", "https://news.google.com/rss/search?" + urlencode({"q": f"{q} when:{DAYS_BACK}d", "hl": "en-US", "gl": "US", "ceid": "US:en"}))
        for q in SEARCHES
    ]
    for name, url in sources:
        try:
            items = parse_feed(fetch(url))
        except Exception as exc:
            status[name] = f"failed: {type(exc).__name__}"
            print(f"{name}: failed ({exc})")
            continue
        kept = 0
        for it in items:
            ts = to_utc(it["published"])
            if not it["title"] or (ts and ts < since):
                continue
            text = it["title"] + " " + it["summary"]
            kinds = [k for k, pat in (("deal", DEAL), ("funding", FUND)) if pat.search(text)]
            publisher = it["publisher"] or (name if not name.startswith("Google News") else None)
            from_bio_outlet = any(o.lower() in (publisher or name).lower() for o in BIO_OUTLETS)
            if not kinds or NOT_BIO.search(it["title"]) or not (from_bio_outlet or BIO.search(text)):
                continue
            title = it["title"]
            if it["publisher"] and title.endswith(" - " + it["publisher"]):
                title = title[: -len(" - " + it["publisher"])]
            collected.append({
                "title": title,
                "publisher": publisher,
                "feed": name,
                "link": it["link"],
                "published": ts.isoformat() if ts else None,
                "summary": it["summary"],
                "kinds": kinds,
            })
            kept += 1
        status[name] = f"{len(items)} items, {kept} kept"
        print(f"{name}: {len(items)} items, {kept} kept")

    # De-duplicate near-identical headlines (same story on several feeds).
    seen, unique = set(), []
    for it in sorted(collected, key=lambda x: x["published"] or "", reverse=True):
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:70]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    now = datetime.now(timezone.utc)
    result = {
        "generated_at": now.isoformat(timespec="seconds"),
        "covers_since": since.isoformat(timespec="seconds"),
        "sources": status,
        "count": len(unique),
        "items": unique,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in (DATA_DIR / "latest.json", DATA_DIR / f"{now.date()}.json"):
        path.write_text(json.dumps(result, indent=1))
    # Keep the repo small: drop dated snapshots older than 30 days.
    for old in DATA_DIR.glob("20*.json"):
        try:
            if datetime.fromisoformat(old.stem).date() < (now - timedelta(days=30)).date():
                old.unlink()
        except ValueError:
            pass
    print(f"Saved {len(unique)} deal/funding headlines from the last {DAYS_BACK} days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
