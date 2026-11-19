import requests, json
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
d = requests.get("https://api.nasdaq.com/api/ipo/calendar?date=2026-11", headers=UA, timeout=30).json()["data"]
print(json.dumps(d.get("upcoming"))[:1500])
print(json.dumps(d.get("priced"))[:1500])
rows = (d.get("upcoming") or {}).get("upcomingTable", {}).get("rows") or []
pr = (d.get("priced") or {}).get("rows") or []
for r in (rows + pr)[:4]:
    did = r.get("dealID")
    o = requests.get(f"https://api.nasdaq.com/api/ipo/overview/?dealId={did}", headers=UA, timeout=30).json()
    print(did, json.dumps(o)[:1500])
