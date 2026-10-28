"""
Intraday XBI snapshot, run a few times during the trading day.

Same math as xbi_contrib.py (weight x return = contribution in bps), but using the
latest price so far today instead of the close. Each run adds a checkpoint, so the
dashboard can show how XBI has moved through the day and which stocks are driving it.

Output: data/intraday/today.json — a ready-to-use list of rows for the dashboard:
  kind "point": one per checkpoint  (time, xbi_price, xbi_change_pct)
  kind "mover": the current top contributors/detractors and % movers
                 (ticker, name, weight_pct, price, return_pct, contribution_bps, headline, link, publisher)
Prices are Yahoo Finance quotes as of each run (may be delayed up to ~15 minutes).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import newsrss
from xbi_contrib import load_holdings, load_returns, pick_movers, recent_headlines

OUT_DIR = Path(__file__).parent / "data" / "intraday"
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")


def main() -> int:
    now = datetime.now(timezone.utc)
    today_et = now.astimezone(ET).strftime("%Y-%m-%d")
    holdings = load_holdings()
    returns = load_returns(holdings["Ticker"].tolist())
    date = returns.attrs["date"]
    if date != today_et:
        print(f"Latest price bar is {date}, not today ({today_et}) — market closed or not open yet. Nothing to do.")
        return 0

    df = holdings.merge(returns, left_on="Ticker", right_index=True, how="left")
    df["contribution_bps"] = df["Weight"] * df["return_pct"]
    priced = df.dropna(subset=["return_pct"])
    xbi = returns.loc["XBI"] if "XBI" in returns.index else None
    if xbi is None or pd.isna(xbi["close"]):
        raise RuntimeError("No XBI price.")

    # Earlier checkpoints from today's file, if any.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "today.json"
    points = []
    if path.exists():
        try:
            points = [r for r in json.loads(path.read_text()) if r.get("kind") == "point" and r.get("date") == date]
        except Exception:
            points = []
    t_pt = now.astimezone(PT)
    stamp = now.isoformat(timespec="seconds")
    points.append({
        "kind": "point", "id": f"point-{stamp}", "date": date, "as_of": stamp,
        "time_label": t_pt.strftime("%-I:%M %p PT"),
        "xbi_price": round(float(xbi["close"]), 2), "xbi_prev_close": round(float(xbi["prev_close"]), 2),
        "xbi_change_pct": round(float(xbi["return_pct"]), 2),
        "sum_contribution_bps": round(float(priced["contribution_bps"].sum()), 1),
    })

    movers = []
    for r in pick_movers(priced, n_pct=6, n_contrib=5).itertuples():
        news = recent_headlines(r.Ticker, str(r.Name), date, limit=1)
        h = news[0] if news else {}
        movers.append({
            "kind": "mover", "id": f"mover-{r.Ticker}", "date": date, "as_of": stamp,
            "ticker": r.Ticker, "name": str(r.Name).title(),
            "weight_pct": round(float(r.Weight), 3), "price": round(float(r.close), 2),
            "return_pct": round(float(r.return_pct), 2), "contribution_bps": round(float(r.contribution_bps), 2),
            "headline": h.get("title", ""), "publisher": h.get("publisher") or "", "link": h.get("link") or "",
        })

    rows = points + movers
    path.write_text(json.dumps(rows, indent=1))
    (OUT_DIR / f"{date}.json").write_text(json.dumps(rows, indent=1))
    print(f"{date} {points[-1]['time_label']}: XBI {points[-1]['xbi_price']} ({points[-1]['xbi_change_pct']:+.2f}%), "
          f"{len(points)} checkpoints, {len(movers)} movers ({newsrss.summary()})")
    for old in OUT_DIR.glob("20*.json"):
        try:
            if (pd.Timestamp(date) - pd.Timestamp(old.stem)).days > 14:
                old.unlink()
        except ValueError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
