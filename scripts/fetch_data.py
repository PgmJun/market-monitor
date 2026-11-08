#!/usr/bin/env python3
"""Fetch market data server-side and write JSON snapshots for the static site.

Runs in GitHub Actions (no CORS limits there). Output:
  data/market.json   - dashboard quotes, VIX, fear & greed, news
  data/universe.json - 나스닥100 + 코스피 시총 상위 100 종목 목록 (매일 갱신)
  data/history/*.json - 종목별 5y daily history (종목 분석)
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
    if v is None:
        return None
    v = float(v)
    return round(v, 2 if abs(v) >= 1 else 4)


# ── 종목 유니버스 (나스닥100 + 코스피·코스닥 시총 상위 100) ──────────────
def nasdaq100():
    j = json.loads(get("https://api.nasdaq.com/api/quote/list-type/nasdaq100",
                       headers={"Accept": "application/json"}))
    d = j.get("data") or {}
    rows = (d.get("data") or {}).get("rows") or d.get("rows") or []
    out = [{"s": r["symbol"].strip().upper().replace("/", "-"),
            "n": r.get("companyName", "").strip()} for r in rows if r.get("symbol")]
    if len(out) < 90:
        raise ValueError(f"nasdaq100 rows={len(out)}")
    return out


def _krx_pairs_api(market):
    """네이버 모바일 증권 JSON (시가총액 순)."""
    for page in range(1, 4):
        j = json.loads(get(
            f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page={page}&pageSize=100",
            headers={"Referer": "https://m.stock.naver.com/", "Accept": "application/json"}))
        rows = j.get("stocks") or j.get("result") or (j if isinstance(j, list) else [])
        if not rows:
            print(f"    naver api {market} p{page}: keys={list(j)[:8] if isinstance(j, dict) else type(j)}")
            return
        for r in rows:
            code = str(r.get("itemCode") or r.get("reutersCode") or r.get("code") or "")
            name = r.get("stockName") or r.get("itemName") or r.get("name") or ""
            # ETF/ETN 등은 제외
            if r.get("stockEndType") not in (None, "stock"):
                continue
            yield code, name


def _krx_pairs_html(sosok):
    """네이버 금융 시가총액 페이지 (예비)."""
    for page in range(1, 5):
        raw = get(f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}",
                  headers={"Referer": "https://finance.naver.com/"})
        html = raw.decode("euc-kr", "replace")
        pairs = re.findall(r'main\.naver\?code=(\d{6})"[^>]*>\s*([^<]+?)\s*</a>', html)
        if not pairs:
            snippet = re.sub(r"\s+", " ", html)[:200]
            print(f"    naver html sosok={sosok} p{page}: len={len(raw)} head={snippet!r}")
            return
        yield from pairs


def krx_top(sosok=0, suffix="KS", n=100):
    market = "KOSPI" if sosok == 0 else "KOSDAQ"
    last = None
    for src in (lambda: _krx_pairs_api(market), lambda: _krx_pairs_html(sosok)):
        out, seen = [], set()
        try:
            for code, name in src():
                name = (name or "").strip()
                # 우선주 제외 (삼성전자우, 현대차2우B 등)
                if not re.fullmatch(r"\d{6}", code) or code in seen \
                        or re.search(r"\d?우[A-C]?$|\(전환\)$", name):
                    continue
                seen.add(code)
                out.append({"s": f"{code}.{suffix}", "n": name})
                if len(out) >= n:
                    return out
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"    krx {market} source failed: {e}")
        if len(out) >= 90:
            return out
        last = last or ValueError(f"rows={len(out)}")
    raise ValueError(f"krx {market}: {last}")


def load_universe(now):
    up = "data/universe.json"
    old = {}
    if os.path.exists(up):
        try:
            with open(up, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:  # noqa: BLE001
            old = {}
    fresh = False
    if old.get("updated"):
        age = datetime.now(timezone.utc) - datetime.fromisoformat(old["updated"])
        fresh = age.total_seconds() < 20 * 3600
    # 비어 있는 목록이 있으면 시간과 관계없이 다시 시도
    if any(not old.get(k) for k in ("nasdaq100", "kospi100", "kosdaq100")):
        fresh = False
    if fresh and not os.environ.get("FORCE_UNIVERSE"):
        return old
    uni = {"updated": now}
    for key, fn in (("nasdaq100", nasdaq100), ("kospi100", lambda: krx_top(0, "KS")),
                    ("kosdaq100", lambda: krx_top(1, "KQ"))):
        try:
            uni[key] = fn()
            print(f"universe {key}: {len(uni[key])}")
        except Exception as e:  # noqa: BLE001
            print(f"  ! universe {key}: {e} (이전 목록 유지)")
            uni[key] = old.get(key, [])
    with open(up, "w", encoding="utf-8") as f:
        json.dump(uni, f, ensure_ascii=False, separators=(",", ":"))
    return uni


def chart(sym, rng):
    q = urllib.parse.quote(sym)
    last_err = None
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?interval=1d&range={rng}"
        try:
            j = json.loads(get(url))
            res = j["chart"]["result"][0]
            qd = res["indicators"]["quote"][0]
            ts = res.get("timestamp") or []
            col = lambda k: qd.get(k) or []  # noqa: E731
            O, H, L, C, V = col("open"), col("high"), col("low"), col("close"), col("volume")
            at = lambda a, i: a[i] if i < len(a) else None  # noqa: E731
            rows = []  # aligned per bar: (t, o, h, l, c, v)
            for i in range(len(C)):
                c = C[i]
                if c is None:
                    continue
                o, h, lo = at(O, i), at(H, i), at(L, i)
                rows.append((at(ts, i), o if o is not None else c,
                             h if h is not None else c, lo if lo is not None else c,
                             c, at(V, i) or 0))
            m = res.get("meta", {})
            return {
                "t": [r[0] for r in rows],
                "opens": [r4(r[1]) for r in rows],
                "highs": [r4(r[2]) for r in rows],
                "lows": [r4(r[3]) for r in rows],
                "closes": [r4(r[4]) for r in rows],
                "vols": [int(r[5]) for r in rows],
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

    # 종목별 5년 일봉: data/history/<SYM>.json
    # 빠른선택 종목은 3시간마다, 나머지(나스닥100·코스피100)는 20시간마다 갱신.
    uni = load_universe(now)
    universe = list(dict.fromkeys(
        PRESET_SYMS + [x["s"] for x in uni.get("nasdaq100", [])]
        + [x["s"] for x in uni.get("kospi100", [])]
        + [x["s"] for x in uni.get("kosdaq100", [])]))
    hdir = "data/history"
    os.makedirs(hdir, exist_ok=True)
    ip = os.path.join(hdir, "_index.json")
    idx = {}
    if os.path.exists(ip):
        try:
            with open(ip, encoding="utf-8") as f:
                idx = json.load(f)
        except Exception:  # noqa: BLE001
            idx = {}
    force = bool(os.environ.get("FORCE_HISTORY"))
    t_now = time.time()
    ok = fail = skip = 0
    started = time.time()
    for s in universe:
        path = os.path.join(hdir, f"{s}.json")
        max_age = 3 * 3600 if s in PRESET_SYMS else 20 * 3600
        if not force and os.path.exists(path) and t_now - idx.get(s, 0) < max_age:
            skip += 1
            continue
        if time.time() - started > 15 * 60:  # 워크플로 시간 초과 방지, 나머지는 다음 실행에서
            print("  history time budget reached; continuing next run")
            break
        d = chart(s, "5y")
        time.sleep(0.3)
        if not d:
            fail += 1
            continue
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        idx[s] = int(t_now)
        ok += 1
    # 유니버스에서 빠진 종목 파일 정리
    keep = set(universe)
    for fn in os.listdir(hdir):
        if fn.endswith(".json") and fn != "_index.json" and fn[:-5] not in keep:
            os.remove(os.path.join(hdir, fn))
            idx.pop(fn[:-5], None)
    with open(ip, "w", encoding="utf-8") as f:
        json.dump(idx, f, separators=(",", ":"))
    print(f"history: updated {ok}, failed {fail}, fresh {skip}, total {len(universe)}")


if __name__ == "__main__":
    main()
