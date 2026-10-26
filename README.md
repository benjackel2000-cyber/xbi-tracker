# XBI contribution tracker

Which stocks are actually moving the SPDR S&P Biotech ETF (XBI) each day — ranked by **contribution**, not by % move.

**Contribution (bps) = a holding's weight in XBI (%) × its daily return (%).**
A 0.8% holding that rises 10% adds about +8 bps to XBI's move.

## How it works

1. `xbi_contrib.py` downloads XBI's daily holdings and weights from State Street, pulls each holding's last two closes from Yahoo Finance, and computes each stock's contribution. For the day's biggest movers it also collects recent news headlines, so each move can be explained.
2. A GitHub Actions workflow (`.github/workflows/daily.yml`) runs it every weekday after the market closes and commits the results.
3. Results land in `data/latest.json` (most recent day) and `data/daily/YYYY-MM-DD.json` (history), which feed the XBI tab of a Claude dashboard.

## Run it yourself

```bash
pip install -r requirements.txt
python xbi_contrib.py
```

Or open the **Actions** tab on GitHub and run **Daily XBI contribution** by hand.

## Notes

- Weights come from the prior day's holdings file, the right basis for measuring a day's contribution.
- The sum of all contributions should land close to XBI's actual daily return; small gaps come from cash, fees and timing.
- Prices come from Yahoo Finance via the unofficial `yfinance` library, which is free but can occasionally fail or lag.

## FDA decision outcomes

`fda_outcomes.py` checks each FDA decision (PDUFA) date in `data/fda_events.json`. It searches the news from 10 days before to 3 days after the date to find what happened (Approved / CRL / Extended), uses the first headline to pin down the actual decision day, and measures the stock's reaction that day and over the whole window. Results go to `data/fda_outcomes.json`.

## Deal & funding news feed

`news_feed.py` runs every morning (`.github/workflows/news.yml`). It collects the last three days of headlines from BioPharma Dive, Fierce Biotech, Endpoints, GlobeNewswire, PR Newswire and a few Google News searches, keeps the ones that look like deals or financings, and saves them to `data/news/latest.json` for the dashboard's morning update.
