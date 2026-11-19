import requests, json
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
o = requests.get("https://api.nasdaq.com/api/ipo/overview/?dealId=1407588-119709", headers=UA, timeout=30).json()["data"]
print(list(o.keys()))
for k, v in o.items():
    if k != "poOverview":
        print(k, json.dumps(v)[:800])
print(list(o["poOverview"].keys()))
for m in ["2026-11", "2026-10"]:
    d = requests.get(f"https://api.nasdaq.com/api/ipo/calendar?date={m}", headers=UA, timeout=30).json()["data"]
    f = (d.get("filed") or {}).get("rows") or []
    print(m, "filed:", [(r["companyName"], r.get("filedDate"), r.get("dealID")) for r in f])
    w = (d.get("withdrawn") or {}).get("rows") or []
    print(m, "withdrawn:", [(r["companyName"]) for r in w], list(d.keys()))
