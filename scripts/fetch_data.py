#!/usr/bin/env python3
"""Fetch market data server-side and write JSON snapshots for the static site.

Runs in GitHub Actions (no CORS limits there). Output:
  data/market.json   - dashboard quotes, VIX, fear & greed, news
  data/history.json  - 5y daily history for preset tickers (종목 분석 빠른선택)
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DASHBOARD_SYMS = [
    "^GSPC", "^IXIC", "^DJI", "^KS11", "^KQ11", "^N225", "^VIX",
    "CL=F", "BZ=F", "GC=F", "SI=F", "HG=F", "NG=F",
    "^IRX", "^TNX", "^TYX",
    "KRW=X", "EURUSD=X", "JPY=X", "GBPUSD=X", "CNY=X",
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD",
]
PRESET_SYMS = ["AAPL", "TSLA", "NVDA", "AMZN", "MSFT", "ORCL", "GOOGL", "META",
               "005930.KS", "000660.KS", "BTC-USD"]

NEWS_FEEDS = {
    "us": ["https://finance.yahoo.com/news/rssindex",
           "https://feeds.content.dowjones.io/public/rss/mw_topstories",
           "https://www.cnbc.com/id/10001147/device/rss/rss.html"],
    "kr": ["https://www.hankyung.com/feed/economy",
           "https://www.yna.co.kr/rss/economy.xml",
           "https://www.mk.co.kr/rss/30100041/"],
}


def get(url, timeout=20, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def r4(v):
    return None if v is None else round(float(v), 4)


def chart(sym, rng):
    q = urllib.parse.quote(sym)
    last_err = None
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?interval=1d&range={rng}"
        try:
            j = json.loads(get(url))
            res = j["chart"]["result"][0]
            qd = res["indicators"]["quote"][0]
            rows = [(c, h, l, v) for c, h, l, v in zip(
                qd.get("close") or [], qd.get("high") or [],
                qd.get("low") or [], qd.get("volume") or [])
                if c is not None]
            m = res.get("meta", {})
            return {
                "closes": [r4(r[0]) for r in rows],
                "highs": [r4(r[1]) for r in rows if r[1] is not None],
                "lows": [r4(r[2]) for r in rows if r[2] is not None],
                "vols": [int(r[3]) for r in rows if r[3]],
                "meta": {k: m.get(k) for k in (
                    "symbol", "longName", "shortName", "currency",
                    "regularMarketPrice", "chartPreviousClose",
                    "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "exchangeName")},
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1)
    print(f"  ! {sym} {rng}: {last_err}")
    return None


def fear_greed():
    # 1) CNN Fear & Greed (stock market)
    try:
        j = json.loads(get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"Referer": "https://edition.cnn.com/", "Origin": "https://edition.cnn.com"}))
        fg = j["fear_and_greed"]
        hist = j.get("fear_and_greed_historical", {}).get("data", [])
        items = [{"value": round(fg["score"]), "label": fg["rating"],
                  "ts": int(time.time())}]
        # one point per day, newest first
        seen = set()
        for p in reversed(hist):
            d = datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc).date()
            if d in seen:
                continue
            seen.add(d)
            items.append({"value": round(p["y"]), "label": p.get("rating", ""),
                          "ts": int(p["x"] / 1000)})
            if len(items) >= 8:
                break
        return {"source": "CNN", "items": items}
    except Exception as e:  # noqa: BLE001
        print(f"  ! CNN fear&greed: {e}")
    # 2) Fallback: alternative.me (crypto fear & greed)
    try:
        j = json.loads(get("https://api.alternative.me/fng/?limit=8"))
        items = [{"value": int(d["value"]), "label": d["value_classification"],
                  "ts": int(d["timestamp"])} for d in j["data"]]
        return {"source": "alternative.me (암호화폐)", "items": items}
    except Exception as e:  # noqa: BLE001
        print(f"  ! alternative.me: {e}")
    return None


def news(feeds):
    for url in feeds:
        try:
            root = ET.fromstring(get(url))
            ch = root.find("channel")
            src = (ch.findtext("title") or "뉴스").strip() if ch is not None else "뉴스"
            out = []
            for it in root.iter("item"):
                title = re.sub(r"\s+", " ", (it.findtext("title") or "")).strip()
                link = (it.findtext("link") or "").strip()
                if not title or not link.startswith("http"):
                    continue
                out.append({"title": title, "link": link,
                            "pubDate": (it.findtext("pubDate") or "").strip(),
                            "source": src})
                if len(out) >= 6:
                    break
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            print(f"  ! news {url}: {e}")
    return []


def main():
    os.makedirs("data", exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    quotes = {}
    for s in DASHBOARD_SYMS:
        d = chart(s, "5d")
        if d:
            quotes[s] = d
    market = {
        "updated": now,
        "quotes": quotes,
        "fng": fear_greed(),
        "news": {k: news(v) for k, v in NEWS_FEEDS.items()},
    }
    print(f"quotes ok: {len(quotes)}/{len(DASHBOARD_SYMS)}")
    if len(quotes) < len(DASHBOARD_SYMS) // 2 and os.path.exists("data/market.json"):
        print("too many failures; keeping previous market.json")
    else:
        with open("data/market.json", "w", encoding="utf-8") as f:
            json.dump(market, f, ensure_ascii=False, separators=(",", ":"))

    # Preset history refreshes at most every ~3h to keep commits small.
    hp = "data/history.json"
    stale = True
    if os.path.exists(hp):
        try:
            with open(hp, encoding="utf-8") as f:
                old = json.load(f)
            age = datetime.now(timezone.utc) - datetime.fromisoformat(old["updated"])
            stale = age.total_seconds() > 3 * 3600
        except Exception:  # noqa: BLE001
            stale = True
    if stale or os.environ.get("FORCE_HISTORY"):
        hist = {}
        for s in PRESET_SYMS:
            d = chart(s, "5y")
            if d:
                hist[s] = d
        print(f"history ok: {len(hist)}/{len(PRESET_SYMS)}")
        if hist:
            with open(hp, "w", encoding="utf-8") as f:
                json.dump({"updated": now, "symbols": hist}, f,
                          ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
