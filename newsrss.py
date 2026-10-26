"""
Shared news search used by all the tracker scripts.

Google News RSS is tried first. GitHub's servers sometimes get temporarily blocked by
Google (HTTP 503/429), so each request is retried with a pause, and if Google still
refuses, the same search goes to Bing News RSS instead. Search operators Bing doesn't
understand (when:, after:, before:, site:) are stripped for Bing; the calling script
already filters stories by date.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlparse, parse_qs

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
PAUSE = 1.0                # seconds between requests, to stay polite
_google_down_until = 0.0   # after repeated Google failures, go straight to Bing for a while
stats = {"google": 0, "bing": 0, "failed": 0}


def _parse(xml: bytes) -> list[dict]:
    out = []
    for it in ET.fromstring(xml).iter("item"):
        title = (it.findtext("title") or "").strip()
        publisher = it.findtext("source") or it.findtext("{http://www.bing.com:80/news/search}Source") \
            or it.findtext("{https://www.bing.com/news/search}Source")
        if not publisher:
            for child in it:
                if child.tag.endswith("}Source"):
                    publisher = child.text
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(" - " + publisher)]
        link = (it.findtext("link") or "").strip()
        if "bing.com/news/apiclick" in link:  # unwrap Bing's redirect link
            u = parse_qs(urlparse(link).query).get("url")
            if u:
                link = u[0]
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except Exception:
            ts = None
        out.append({"title": title, "publisher": publisher, "link": link, "published": ts,
                    "pubDate": it.findtext("pubDate"),
                    "summary": re.sub(r"<[^>]+>", " ", it.findtext("description") or "").strip()[:400]})
    return out


def _get(url: str) -> bytes:
    resp = requests.get(url, headers=UA, timeout=30)
    resp.raise_for_status()
    return resp.content


def bing_query(q: str) -> str:
    q = re.sub(r"\b(when|after|before):\S+", "", q)
    q = re.sub(r"\bsite:(\S+)", lambda m: m.group(1).split(".")[0], q)
    return re.sub(r"\s+", " ", q).strip()


def search(q: str) -> list[dict]:
    """Return news items for a search, as dicts with title, publisher, link, published (UTC datetime or None), summary."""
    global _google_down_until
    time.sleep(PAUSE)
    if time.time() >= _google_down_until:
        url = "https://news.google.com/rss/search?" + urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        for wait in (0, 4, 12):
            if wait:
                time.sleep(wait)
            try:
                items = _parse(_get(url))
                stats["google"] += 1
                return items
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else None
                if code not in (429, 500, 502, 503, 504):
                    break
            except Exception:
                break
        _google_down_until = time.time() + 120  # skip Google for 2 minutes
    try:
        url = "https://www.bing.com/news/search?" + urlencode({"q": bing_query(q), "format": "rss", "setlang": "en-US", "cc": "US"})
        items = _parse(_get(url))
        stats["bing"] += 1
        return items
    except Exception as exc:
        stats["failed"] += 1
        raise RuntimeError(f"Google and Bing both failed: {exc}") from exc


def summary() -> str:
    return f"news searches: {stats['google']} via Google, {stats['bing']} via Bing, {stats['failed']} failed"
