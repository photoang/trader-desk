#!/usr/bin/env python3
"""
自營交易台 資料抓取程式

抓取台股盤後籌碼、國際行情與各國財經新聞，輸出到 site/data/：
  latest.json  行情、三大法人、融資融券、台指期法人未平倉、P/C Ratio
  series.json  每日序列（外資期貨淨未平倉、P/C Ratio、融資餘額），保留最近 250 筆
  news.json    各國財經新聞頭條（只存標題、來源、時間與連結）

任何一個來源失敗都不會中斷，會沿用上一次成功的資料。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

TPE = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "data"
SERIES_KEEP = 250

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
})


# ---------------------------------------------------------------- 工具
def now() -> dt.datetime:
    return dt.datetime.now(TPE)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def num(s):
    """'1,234.5' -> 1234.5；空值或 '--' -> None"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "---"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def idx(fields, key, default=-1):
    for i, f in enumerate(fields):
        if key in str(f):
            return i
    return default


def load(name, default):
    try:
        return json.loads((OUT / name).read_text("utf-8"))
    except Exception:
        return default


def save(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")


def recent_weekdays(n=10):
    d = now().date()
    for i in range(n):
        x = d - dt.timedelta(days=i)
        if x.weekday() < 5:
            yield x


def safe(fn, label):
    try:
        v = fn()
        log(f"[ok]   {label}" if v else f"[none] {label}")
        return v
    except Exception as e:  # noqa: BLE001
        log(f"[fail] {label}: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------- 證交所
def twse(path, params):
    r = S.get(f"https://www.twse.com.tw/rwd/zh/{path}",
              params={**params, "response": "json"}, timeout=25)
    r.raise_for_status()
    j = r.json()
    return j if j.get("stat") == "OK" else None


def twse_tables(j):
    if j.get("tables"):
        return [t for t in j["tables"] if t.get("data")]
    if j.get("data"):
        return [{"title": j.get("title", ""), "fields": j.get("fields", []), "data": j["data"]}]
    if j.get("creditList"):  # 舊版融資融券格式
        return [{"title": "信用交易統計", "fields": j.get("creditFields", []), "data": j["creditList"]}]
    return []


def parse_bfi82u(j):
    """三大法人買賣金額（元）-> 億元"""
    t = twse_tables(j)
    if not t:
        return None
    rows = {}
    for r in t[0]["data"]:
        rows[str(r[0]).strip()] = num(r[3])
    if not rows:
        return None

    def total(pred):
        vals = [v for k, v in rows.items() if pred(k) and v is not None]
        return round(sum(vals) / 1e8, 2) if vals else None

    return {
        "foreign": total(lambda k: k.startswith("外資")),
        "trust": total(lambda k: k.startswith("投信")),
        "dealer": total(lambda k: k.startswith("自營商")),
        "total": total(lambda k: k.startswith("合計")),
    }


def institutional():
    for d in recent_weekdays():
        j = twse("fund/BFI82U", {"type": "day", "dayDate": d.strftime("%Y%m%d")})
        time.sleep(2)
        if j and (v := parse_bfi82u(j)):
            return {"date": d.isoformat(), **v}
    return None


def parse_margin(j):
    table = None
    for t in twse_tables(j):
        if any("融資" in str(r[0]) for r in t["data"]):
            table = t
            break
    if not table:
        return None
    f = table["fields"]
    ip, it = idx(f, "前日餘額", 4), idx(f, "今日餘額", 5)
    out = {}
    for r in table["data"]:
        k = str(r[0])
        if "融資金額" in k:
            key = "margin_amt"     # 仟元
        elif "融資" in k:
            key = "margin_lots"    # 張
        elif "融券" in k:
            key = "short_lots"     # 張
        else:
            continue
        out[key] = {"prev": num(r[ip]), "today": num(r[it])}
    return out or None


def margin():
    for d in recent_weekdays():
        j = twse("marginTrading/MI_MARGN", {"date": d.strftime("%Y%m%d"), "selectType": "MS"})
        time.sleep(2)
        if j and (v := parse_margin(j)):
            return {"date": d.isoformat(), **v}
    return None


# ---------------------------------------------------------------- 期交所
def taifex_csv(path, data):
    r = S.post(f"https://www.taifex.com.tw/cht/3/{path}", data=data, timeout=30)
    r.raise_for_status()
    txt = r.content.decode("cp950", errors="replace")
    if "<html" in txt[:800].lower():
        return []
    rows = [[c.strip() for c in row] for row in csv.reader(io.StringIO(txt))]
    return [row for row in rows if any(row)]


def tfx_date(s):
    return dt.datetime.strptime(s.strip(), "%Y/%m/%d").date().isoformat()


def date_range(days):
    end = now().date()
    return {"queryStartDate": (end - dt.timedelta(days=days)).strftime("%Y/%m/%d"),
            "queryEndDate": end.strftime("%Y/%m/%d")}


def parse_fut_inst(rows):
    """臺股期貨 三大法人 -> [{date, foreign, trust, dealer, foreign_trade}]，單位：口"""
    if len(rows) < 2:
        return []
    h = rows[0]
    i_name, i_who = idx(h, "商品名稱"), idx(h, "身份別")
    i_oi, i_tr = idx(h, "多空未平倉口數淨額"), idx(h, "多空交易口數淨額")
    if min(i_who, i_oi) < 0:
        return []
    by = {}
    for r in rows[1:]:
        if len(r) <= max(i_oi, i_who):
            continue
        if i_name >= 0 and "股期貨" not in r[i_name]:
            continue
        try:
            d = tfx_date(r[0])
        except ValueError:
            continue
        who = r[i_who]
        key = "foreign" if "外資" in who else "trust" if "投信" in who else "dealer" if "自營" in who else None
        if not key:
            continue
        rec = by.setdefault(d, {"date": d})
        rec[key] = num(r[i_oi])
        if key == "foreign" and i_tr >= 0:
            rec["foreign_trade"] = num(r[i_tr])
    return sorted(by.values(), key=lambda x: x["date"])


def futures_inst():
    rows = taifex_csv("futContractsDateDown", {**date_range(20), "commodityId": "TXF"})
    return parse_fut_inst(rows) or None


def parse_pcr(rows):
    if len(rows) < 2:
        return []
    h = rows[0]
    i_vol, i_oi = idx(h, "成交量比率"), idx(h, "未平倉量比率")
    out = []
    for r in rows[1:]:
        try:
            d = tfx_date(r[0])
        except (ValueError, IndexError):
            continue
        out.append({"date": d,
                    "pcr_vol": num(r[i_vol]) if i_vol >= 0 else None,
                    "pcr_oi": num(r[i_oi]) if i_oi >= 0 else None})
    return sorted(out, key=lambda x: x["date"])


def pcr():
    return parse_pcr(taifex_csv("pcRatioDown", date_range(20))) or None


# ---------------------------------------------------------------- 國際行情
TICKERS = [
    ("^TWII", "加權指數", "台股"), ("2330.TW", "台積電", "台股"), ("TWD=X", "美元/台幣", "台股"),
    ("^GSPC", "S&P 500", "美股"), ("^IXIC", "那斯達克", "美股"), ("^DJI", "道瓊", "美股"),
    ("^SOX", "費城半導體", "美股"), ("TSM", "台積電 ADR", "美股"), ("^VIX", "VIX", "美股"),
    ("^N225", "日經 225", "亞股"), ("^KS11", "KOSPI", "亞股"), ("^HSI", "恒生", "亞股"),
    ("000001.SS", "上證", "亞股"), ("^GDAXI", "德國 DAX", "歐股"), ("^FTSE", "英國 FTSE", "歐股"),
    ("^TNX", "美債 10 年殖利率", "利率商品"), ("DX-Y.NYB", "美元指數", "利率商品"),
    ("CL=F", "WTI 原油", "利率商品"), ("GC=F", "黃金", "利率商品"),
]


def quotes():
    import yfinance as yf  # 延後載入，避免本機測試時必須安裝

    syms = [t[0] for t in TICKERS]
    df = yf.download(syms, period="10d", interval="1d", progress=False,
                     group_by="ticker", auto_adjust=False, threads=True)
    out = []
    for sym, name, grp in TICKERS:
        try:
            c = df[sym]["Close"].dropna()
            if len(c) < 2:
                continue
            last, prev = float(c.iloc[-1]), float(c.iloc[-2])
            out.append({"sym": sym, "name": name, "grp": grp,
                        "last": round(last, 4), "chg": round(last - prev, 4),
                        "pct": round((last / prev - 1) * 100, 2),
                        "date": c.index[-1].strftime("%Y-%m-%d")})
        except Exception as e:  # noqa: BLE001
            log(f"       quote {sym}: {e}")
    return out or None


# ---------------------------------------------------------------- 新聞
def gnews(q, lang="zh-TW", gl="TW", ceid="TW:zh-Hant"):
    from urllib.parse import quote
    return f"https://news.google.com/rss/search?q={quote(q)}+when:2d&hl={lang}&gl={gl}&ceid={ceid}"


FEEDS = [
    ("台灣", "中央社財經", "https://feeds.feedburner.com/rsscna/finance"),
    ("台灣", "Yahoo 股市", "https://tw.stock.yahoo.com/rss?category=tw-market"),
    ("台灣", "Google 新聞", gnews("台股 OR 台積電 OR 外資")),
    ("美國", "CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("美國", "MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("美國", "Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("中國香港", "SCMP", "https://www.scmp.com/rss/92/feed"),
    ("中國香港", "Google 新聞", gnews("中國經濟 OR 人民銀行 OR 港股 OR A股")),
    ("日本", "Nikkei Asia", "https://asia.nikkei.com/rss/feed/nar"),
    ("日本", "Google 新聞", gnews("日本央行 OR 日股 OR 日圓")),
    ("韓國", "Yonhap", "https://en.yna.co.kr/RSS/economy.xml"),
    ("韓國", "Google 新聞", gnews("韓股 OR 三星 OR SK海力士 OR 韓國央行")),
    ("歐洲", "BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("歐洲", "DW Business", "https://rss.dw.com/rdf/rss-en-bus"),
    ("歐洲", "Google 新聞", gnews("歐洲央行 OR 歐股 OR 歐元區")),
]
MAX_AGE = dt.timedelta(days=3)
PER_REGION = 30


def entry_time(e):
    for k in ("published_parsed", "updated_parsed"):
        t = e.get(k)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    return None


def parse_feed(content, region, src, cutoff):
    items = []
    for e in feedparser.parse(content).entries[:40]:
        title, link = (e.get("title") or "").strip(), (e.get("link") or "").strip()
        if not title or not link:
            continue
        ts = entry_time(e)
        if ts and ts < cutoff:
            continue
        s = src
        if src.startswith("Google") and " - " in title:   # Google 新聞標題結尾是「 - 媒體名」
            title, s = title.rsplit(" - ", 1)
        items.append({"region": region, "src": s, "title": title, "link": link,
                      "ts": ts.isoformat() if ts else None})
    return items


def cnyes():
    r = S.get("https://api.cnyes.com/media/api/v1/newslist/category/headline",
              params={"limit": 30}, timeout=20)
    r.raise_for_status()
    out = []
    for it in r.json().get("items", {}).get("data", []):
        ts = dt.datetime.fromtimestamp(it["publishAt"], dt.timezone.utc)
        out.append({"region": "台灣", "src": "鉅亨網", "title": it["title"].strip(),
                    "link": f"https://news.cnyes.com/news/id/{it['newsId']}", "ts": ts.isoformat()})
    return out


def news(prev):
    cutoff = dt.datetime.now(dt.timezone.utc) - MAX_AGE
    got = {}
    ok = 0
    for region, src, url in FEEDS:
        try:
            r = S.get(url, timeout=20)
            r.raise_for_status()
            items = parse_feed(r.content, region, src, cutoff)
            got.setdefault(region, []).extend(items)
            ok += 1
            log(f"       news {region}/{src}: {len(items)}")
        except Exception as e:  # noqa: BLE001
            log(f"       news {region}/{src} failed: {e}")
        time.sleep(0.5)
    if (c := safe(cnyes, "news 鉅亨網")):
        got.setdefault("台灣", []).extend(c)
        ok += 1
    if not ok:
        return None

    regions = {}
    for region, items in got.items():
        seen, uniq = set(), []
        for it in sorted(items, key=lambda x: x["ts"] or "", reverse=True):
            key = it["title"][:40]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        regions[region] = uniq[:PER_REGION]
    # 這次抓不到的地區沿用上一次
    for region, items in (prev.get("regions") or {}).items():
        regions.setdefault(region, items)
    return regions


# ---------------------------------------------------------------- 主程式
def merge_series(old, new, key):
    by = {r["date"]: r for r in old.get(key, [])}
    for r in new or []:
        by[r["date"]] = {**by.get(r["date"], {}), **r}
    return sorted(by.values(), key=lambda x: x["date"])[-SERIES_KEEP:]


def main():
    prev = load("latest.json", {})
    series = load("series.json", {})
    prev_news = load("news.json", {})

    q = safe(quotes, "國際行情")
    inst = safe(institutional, "證交所 三大法人")
    mg = safe(margin, "證交所 融資融券")
    fut = safe(futures_inst, "期交所 台指期三大法人")
    pc = safe(pcr, "期交所 P/C Ratio")

    series["futures"] = merge_series(series, fut, "futures")
    series["pcr"] = merge_series(series, pc, "pcr")
    if mg and mg.get("margin_amt"):
        series["margin"] = merge_series(series, [{"date": mg["date"],
                                                  "margin_amt": mg["margin_amt"]["today"]}], "margin")
    else:
        series.setdefault("margin", [])

    stamp = now().isoformat(timespec="minutes")
    latest = {
        "updated": stamp,
        "quotes": q or prev.get("quotes"),
        "inst": inst or prev.get("inst"),
        "margin": mg or prev.get("margin"),
        "futures": series["futures"][-1] if series["futures"] else prev.get("futures"),
        "futures_prev": series["futures"][-2] if len(series["futures"]) > 1 else prev.get("futures_prev"),
        "pcr": series["pcr"][-1] if series["pcr"] else prev.get("pcr"),
        "status": {k: bool(v) for k, v in
                   {"quotes": q, "inst": inst, "margin": mg, "futures": fut, "pcr": pc}.items()},
    }
    save("latest.json", latest)
    save("series.json", series)

    regions = news(prev_news)
    if regions:
        save("news.json", {"updated": stamp, "regions": regions})

    failed = [k for k, v in latest["status"].items() if not v]
    log(f"完成 {stamp}；本次未取得：{', '.join(failed) or '無'}")


if __name__ == "__main__":
    main()
