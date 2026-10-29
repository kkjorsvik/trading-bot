#!/usr/bin/env python3
"""
newswatch.py - scans market news, SEC 13D filings and Congress trades.
A local Ollama model reads each headline: what happened, which ticker,
good or bad news, how strong. Everything is logged with its price, and
1/3/5-day outcomes are filled in later, so each event type builds a
track record ("guidance raise: 18 cases, avg +2.1% vs SPY in 3 days").

Possible takeover targets go on the deal watch list, which is checked for
unusual volume and call buying. Alerts go to ntfy topic "alerts".

Run:   python3 newswatch.py           (service mode)
Test:  python3 newswatch.py --test    (checks feeds + model, sends nothing)
"""
import csv
import fcntl
import hashlib
import html
import json
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import time as dtime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests
from datetime import date
from market_extras import earnings_line, earnings_before, pick_call, quote_option
from market_extras import market_day, is_open_now, beat
FEED_STATS = {'fail': 0, 'total': 0}

# ---------------------------------------------------------------- settings
NTFY = "http://localhost:2586/alerts"
OLLAMA = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b"
SEC_UA = "newswatch YOUR_EMAIL"          # SEC requires a contact email here
TZ = ZoneInfo("America/Chicago")

MIN_MCAP = 300e6          # ignore companies under $300M
NEED_OPTIONS = True       # only tickers with listed options
WATCH_DAYS = 30           # drop after this many quiet days
ANNOUNCED_DAYS = 5        # keep announced deals this long, then drop
MAX_WATCH = 25
VOL_X = 3.0               # unusual volume: today vs normal, time-adjusted
CALL_MIN_VOL = 500        # unusual calls: volume at a strike at least this...
CALL_VS_OI = 2.0          # ...and this many times its open interest
MAX_ALERTS_DAY = 15       # news alerts per day before the rest go to the evening digest
HIST_MIN = 5              # cases needed before a track record is shown

HERE = os.path.dirname(os.path.abspath(__file__))
WATCH_FILE = os.path.join(HERE, "watchlist.json")
LOCK_FILE = os.path.join(HERE, "watchlist.lock")
SEEN_FILE = os.path.join(HERE, "newswatch_seen.json")
LOG_FILE = os.path.join(HERE, "newswatch_log.csv")
OUTCOMES = os.path.join(HERE, "news_outcomes.csv")
POS_FILE = os.path.join(HERE, "positions.json")
STATE_FILE = os.path.join(HERE, "newswatch_state.json")
TICKER_CACHE = os.path.join(HERE, "sec_tickers.json")


def gnews(q):
    return ("Google News", f"https://news.google.com/rss/search?q={quote_plus(q + ' when:1d')}&hl=en-US&gl=US&ceid=US:en")


FEEDS = [
    # takeovers
    gnews('"exploring a sale" OR "explores sale" OR "exploring sale" OR "weighs sale" OR "considering a sale"'),
    gnews('"strategic alternatives" OR "strategic review" OR "hired advisers" OR "hires advisers"'),
    gnews('"takeover interest" OR "takeover approach" OR "buyout offer" OR "takeover bid" OR "unsolicited offer"'),
    gnews('"in talks to acquire" OR "in talks to buy" OR "nears deal" OR "merger talks" OR "agrees to buy"'),
    gnews('"to be acquired by" OR "agreed to be acquired" OR "definitive agreement" acquire'),
    # earnings and guidance
    gnews('"raises guidance" OR "raises outlook" OR "raises forecast" OR "beats estimates" OR "record revenue"'),
    gnews('"cuts guidance" OR "lowers outlook" OR "cuts forecast" OR "misses estimates" OR "profit warning"'),
    # approvals, contracts, analysts, capital
    gnews('"FDA approval" OR "FDA approves" OR "complete response letter" OR "FDA rejects" OR "trial results"'),
    gnews('"wins contract" OR "awarded contract" OR "Pentagon contract" OR "billion contract" OR "selected to supply"'),
    gnews('stock "upgraded to buy" OR "upgrades to buy" OR "downgraded to sell" OR "downgrades to sell" OR "price target raised"'),
    gnews('"share buyback" OR "buyback program" OR "stock offering" OR "secondary offering" OR "dilution"'),
    gnews('"SEC investigation" OR "DOJ probe" OR "subpoena" OR "product recall" OR "recalls vehicles" OR "short seller report"'),
    gnews('"added to S&P 500" OR "join the S&P 500" OR "joins S&P 500"'),
    # wires
    ("PR Newswire", "https://www.prnewswire.com/rss/financial-services-latest-news/acquisitions-mergers-and-takeovers-list.rss"),
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/subjectcode/27-Mergers%20And%20Acquisitions/feedTitle/GlobeNewswire%20-%20Mergers%20And%20Acquisitions"),
]
YAHOO_TICKER_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={}&region=US&lang=en-US"
SEC_13D = [
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SCHEDULE%2013D&owner=include&count=40&output=atom",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC%2013D&owner=include&count=40&output=atom",
]

SPECULATIVE = {"exploring_sale", "takeover_interest", "deal_talks", "activist_stake"}
EVENT_LABEL = {
    # takeovers
    "exploring_sale": "exploring a sale",
    "takeover_interest": "takeover interest",
    "deal_talks": "in deal talks",
    "activist_stake": "activist stake (13D)",
    "deal_announced": "deal announced",
    "deal_terminated": "deal called off",
    # good news
    "earnings_beat": "earnings beat",
    "guidance_raise": "guidance raised",
    "fda_approval": "FDA/trial win",
    "big_contract": "big contract",
    "analyst_upgrade": "analyst upgrade",
    "buyback": "buyback",
    "partnership": "major partnership",
    "index_add": "added to index",
    # bad news
    "earnings_miss": "earnings miss",
    "guidance_cut": "guidance cut",
    "fda_setback": "FDA/trial setback",
    "analyst_downgrade": "analyst downgrade",
    "stock_offering": "stock offering (dilution)",
    "legal_probe": "lawsuit/probe",
    "recall_safety": "recall/safety issue",
    "exec_exit": "CEO/CFO exit",
}
DEFAULT_DIR = {"deal_terminated": "bearish", "earnings_miss": "bearish", "guidance_cut": "bearish",
               "fda_setback": "bearish", "analyst_downgrade": "bearish", "stock_offering": "bearish",
               "legal_probe": "bearish", "recall_safety": "bearish", "exec_exit": "bearish"}
TICKER_IN_TEXT = re.compile(r"\((?:NASDAQ|NYSE|NYSE American|Nasdaq|NasdaqGS|NasdaqGM|NasdaqCM|AMEX)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\)")

TEST = "--test" in sys.argv


# ---------------------------------------------------------------- small helpers
def now():
    return datetime.now(TZ)


CHECKINS = [(6, 0, "6 AM"), (8, 0, "8 AM"), (12, 0, "Noon"), (15, 0, "3 PM")]  # night digest v1: no 8 PM, added 6 AM


def _hdr(text):
    """Header-safe text: plain ASCII stays as is, anything else is RFC 2047 encoded (ntfy decodes it)."""
    import base64
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


# alert buttons v1
_PHONE_BASE = None


def _phone_base():
    """Address your phone can reach ntfy at (Tailscale). Set NTFY_PHONE_URL to override."""
    global _PHONE_BASE
    if _PHONE_BASE is not None:
        return _PHONE_BASE or None
    url = os.environ.get("NTFY_PHONE_URL", "").strip().rstrip("/")
    if not url:
        try:
            import subprocess
            ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
            url = f"http://{ip[0]}:2586" if ip else ""
        except Exception as e:
            print(f"[buttons] no tailscale ip: {e}")
            url = ""
    _PHONE_BASE = url
    return url or None


def check_actions(t, cp="C"):
    """ntfy buttons that send a command to the bot, so nothing has to be typed."""
    base = _phone_base()
    if not base:
        return None
    side = " put" if cp == "P" else ""
    parts = [f"http, Check 1, {base}/trade, method=POST, body=check {t} 1{side}",
             f"http, Buy, {base}/trade, method=POST, body=buy {t}{side}, clear=true",
             f"http, Skip, {base}/trade, method=POST, body=skip {t}{side}, clear=true"]
    return "; ".join(parts)


def alert(msg, title, priority=3, tags=None, now_please=False, actions=None):
    if priority < 5 and not now_please and not TEST:
        lines = [l for l in msg.split("\n") if l and not l.startswith(("Data:", "Track record: building"))]
        digest_add(title + "\n" + "\n".join("   " + l for l in lines[:6]))
        return
    if TEST:
        print(f"[ALERT p{priority}] {title}\n{msg}\n")
        return
    h = {"Title": _hdr(title), "Priority": str(priority)}
    if tags:
        h["Tags"] = tags
    if actions:
        h["Actions"] = actions
    try:
        requests.post(NTFY, data=msg.encode("utf-8"), headers=h, timeout=10)
    except Exception as e:
        print(f"[ntfy] {e}")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


@contextmanager
def watch_locked():
    """Shared with tradebot's watch commands."""
    with open(LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        wl = load_json(WATCH_FILE, {})
        yield wl
        save_json(WATCH_FILE, wl)
        fcntl.flock(lf, fcntl.LOCK_UN)


def log_item(item, price):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "ticker", "event", "source", "title", "price_at_flag"])
        w.writerow([now().isoformat(timespec="seconds"), item["ticker"], item["event"],
                    item["source"], item["title"], price if price else ""])


# ---------------------------------------------------------------- tickers (SEC list)
_tickers = None


def norm_name(s):
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    drop = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc",
            "holdings", "holding", "group", "the", "class", "a", "b", "llc", "lp", "sa", "nv", "ag", "se"}
    return " ".join(w for w in s.split() if w not in drop)


def tickers():
    global _tickers
    if _tickers is not None:
        return _tickers
    data = load_json(TICKER_CACHE, None)
    fresh = data and time.time() - os.path.getmtime(TICKER_CACHE) < 7 * 86400
    if not fresh:
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json",
                             headers={"User-Agent": SEC_UA}, timeout=30)
            r.raise_for_status()
            data = list(r.json().values())
            save_json(TICKER_CACHE, data)
        except Exception as e:
            print(f"[sec tickers] {e}")
            data = data or []
    by_ticker, by_name, by_cik = {}, {}, {}
    for d in data:
        t = d["ticker"].upper()
        by_ticker.setdefault(t, d["title"])
        by_name.setdefault(norm_name(d["title"]), t)
        by_cik.setdefault(int(d["cik_str"]), t)
    _tickers = {"ticker": by_ticker, "name": by_name, "cik": by_cik}
    return _tickers


def resolve(ticker_guess, company, text=""):
    T = tickers()
    m = TICKER_IN_TEXT.findall(text or "")
    for t in m:
        if t.upper() in T["ticker"]:
            if not company or norm_name(company).split()[:1] == norm_name(T["ticker"][t.upper()]).split()[:1]:
                return t.upper()
    if company:
        n = norm_name(company)
        if n in T["name"]:
            return T["name"][n]
        starts = {v for k, v in T["name"].items() if n and (k.startswith(n + " ") or k == n)}
        if len(starts) == 1:
            return starts.pop()
    if ticker_guess:
        t = ticker_guess.upper().strip().lstrip("$")
        if t in T["ticker"] and (not company or norm_name(company).split()[:1] == norm_name(T["ticker"][t]).split()[:1]):
            return t
    return None


# ---------------------------------------------------------------- feeds
def fetch(url, sec=False):
    h = {"User-Agent": SEC_UA if sec else "Mozilla/5.0 (newswatch)"}
    r = requests.get(url, headers=h, timeout=20)
    r.raise_for_status()
    return r.content


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


# news age v1
MAX_NEWS_AGE_H = 12   # a headline published longer ago than this is not "news" any more


def _parse_dt(txt):
    from datetime import timezone
    from email.utils import parsedate_to_datetime
    txt = (txt or "").strip()
    if not txt:
        return None
    try:
        dt = parsedate_to_datetime(txt)
    except Exception:
        try:
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_hours(item):
    from datetime import timezone
    dt = _parse_dt(item.get("published"))
    if not dt:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)


def _age_txt(h):
    return f"published {h * 60:.0f} min ago" if h < 1 else f"published {h:.0f}h ago"


def parse_feed(raw):
    """RSS 2.0 or Atom -> list of {title, summary, link, published}."""
    root = ET.fromstring(raw)
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        d = {}
        for ch in it:
            t = ch.tag.split("}")[-1]
            if t == "title":
                d["title"] = strip_tags(ch.text)
            elif t in ("description", "summary", "content") and "summary" not in d:
                d["summary"] = strip_tags(ch.text)[:600]
            elif t == "link":
                d["link"] = ch.attrib.get("href") or (ch.text or "").strip()
            elif t in ("published", "pubDate", "updated", "date") and ch.text:
                rank = {"published": 3, "pubDate": 3, "date": 2, "updated": 1}[t]
                if rank > d.get("_rank", 0) and _parse_dt(ch.text):
                    d["published"], d["_rank"] = ch.text.strip(), rank
        d.pop("_rank", None)
        if d.get("title"):
            items.append(d)
    return items


def collect():
    out = []
    FEED_STATS.update(fail=0, total=0)
    for source, url in FEEDS:
        FEED_STATS["total"] += 1
        try:
            for it in parse_feed(fetch(url)):
                src = source
                if source == "Google News" and " - " in it["title"]:
                    it["title"], src = it["title"].rsplit(" - ", 1)
                    it["summary"] = ""
                if source != "Google News" and not TICKER_IN_TEXT.search(it["title"] + " " + it.get("summary", "")):
                    continue  # wire release about a private company
                out.append({**it, "source": src, "kind": "news"})
        except Exception as e:
            print(f"[feed] {source}: {e}")
            FEED_STATS["fail"] += 1
    mine = sorted(my_tickers())
    for i in range(0, len(mine), 10):
        chunk = mine[i:i + 10]
        try:
            for it in parse_feed(fetch(YAHOO_TICKER_RSS.format(",".join(chunk)))):
                it["summary"] = it.get("summary", "")[:400]
                out.append({**it, "source": "Yahoo Finance", "kind": "news", "hint": chunk})
        except Exception as e:
            print(f"[feed] Yahoo {chunk}: {e}")
    for url in SEC_13D:
        try:
            for it in parse_feed(fetch(url, sec=True)):
                m = re.match(r"^(SCHEDULE 13D|SC 13D) - (.+?) \((\d+)\) \(Subject\)", it["title"])
                if m:
                    out.append({"title": it["title"], "summary": "", "link": it.get("link", ""),
                                "source": "SEC 13D", "kind": "13d", "company": m.group(2),
                                "cik": int(m.group(3))})
        except Exception as e:
            print(f"[sec 13d] {e}")
    return out


# ---------------------------------------------------------------- the LLM
PROMPT = """You read one financial news headline and decide if it can move a US-listed stock.
Reply with JSON only, keys in this order:
{"company": "the main company the headline is about - always fill this in when a company is named",
 "ticker": "its US stock ticker if you know it, else null",
 "event": one of EVENTS,
 "direction": "bullish" | "bearish" | "neutral",
 "strength": 1 | 2 | 3,
 "relevant": true or false,
 "summary": "one short plain sentence"}

EVENTS:
exploring_sale (considering a sale / strategic alternatives / hired bankers),
takeover_interest (a suitor approached or made an unagreed offer), deal_talks (in talks / nearing a deal),
deal_announced (definitive agreement to be acquired or merge), deal_terminated (deal or talks ended),
earnings_beat, guidance_raise, fda_approval (FDA approval or positive trial), big_contract,
analyst_upgrade, buyback, partnership, index_add (added to S&P 500 or similar),
earnings_miss, guidance_cut, fda_setback (rejection or failed trial), analyst_downgrade,
stock_offering (new shares sold, dilution), legal_probe (lawsuit, SEC/DOJ probe, short report),
recall_safety, exec_exit (CEO or CFO leaving), other

Rules:
- Raised/lifted forecast, outlook or guidance = guidance_raise (even if earnings are also mentioned).
  Cut/lowered forecast, outlook or guidance = guidance_cut.
- Takeovers: company = the company being BOUGHT, never the buyer. A big company buying a small
  private business or startup = event "other", relevant false.
- Being the target of a possible sale or offer is bullish for that company.
- relevant = false for market commentary, opinion, lists of stocks, private companies, or non-business news.
- strength 3 = big surprise or very large deal, 2 = meaningful, 1 = minor.

Examples:
"Nike lifts full-year outlook as sales rebound" ->
{"company": "Nike", "ticker": "NKE", "event": "guidance_raise", "direction": "bullish", "strength": 2, "relevant": true, "summary": "Nike raised its full-year outlook."}
"Intel slashes revenue forecast on weak PC demand" ->
{"company": "Intel", "ticker": "INTC", "event": "guidance_cut", "direction": "bearish", "strength": 2, "relevant": true, "summary": "Intel cut its revenue forecast."}
"Microsoft acquires small gaming studio" ->
{"company": "gaming studio", "ticker": null, "event": "other", "direction": "neutral", "strength": 1, "relevant": false, "summary": "Microsoft bought a small private studio."}
"Peloton weighs sale after buyout interest" ->
{"company": "Peloton", "ticker": "PTON", "event": "exploring_sale", "direction": "bullish", "strength": 2, "relevant": true, "summary": "Peloton is considering a sale."}

Headline: {title}
Details: {summary}
Source: {source}"""


def classify(item):
    msg = PROMPT.replace("{title}", item["title"]).replace("{summary}", item.get("summary") or "(none)") \
                .replace("{source}", item["source"])
    r = requests.post(OLLAMA, json={
        "model": MODEL, "stream": False, "format": "json", "keep_alive": "30m",
        "options": {"temperature": 0},
        "messages": [{"role": "user", "content": msg}],
    }, timeout=180)
    r.raise_for_status()
    d = json.loads(r.json()["message"]["content"])
    ev = d.get("event") if d.get("event") in EVENT_LABEL else "other"
    direction = d.get("direction") if d.get("direction") in ("bullish", "bearish", "neutral") else "neutral"
    if ev in DEFAULT_DIR:
        direction = DEFAULT_DIR[ev]
    try:
        strength = min(max(int(d.get("strength") or 1), 1), 3)
    except (TypeError, ValueError):
        strength = 1
    return {
        "relevant": bool(d.get("relevant")) and ev != "other",
        "event": ev,
        "company": d.get("company") or d.get("target_company") or None,
        "ticker": d.get("ticker") or d.get("target_ticker") or None,
        "direction": direction,
        "strength": strength,
        "summary": (d.get("summary") or item["title"]).strip(),
    }


# ---------------------------------------------------------------- market data
def quote(t):
    """price, market cap, has_options. Yahoo, ~15 min delayed."""
    import yfinance as yf
    tk = yf.Ticker(t)
    fi = tk.fast_info
    try:
        price = float(fi.last_price)
    except Exception:
        price = None
    try:
        mcap = float(fi.market_cap)
    except Exception:
        mcap = None
    try:
        has_opts = len(tk.options) > 0
    except Exception:
        has_opts = False
    return price, mcap, has_opts


def money(x):
    if x is None:
        return "?"
    return f"${x / 1e9:.1f}B" if x >= 1e9 else f"${x / 1e6:.0f}M"


def tradeable(t):
    price, mcap, has_opts = quote(t)
    if mcap is not None and mcap < MIN_MCAP:
        return False, price, mcap, "too small"
    if NEED_OPTIONS and not has_opts:
        return False, price, mcap, "no options"
    return True, price, mcap, ""


# ---------------------------------------------------------------- what you hold / watch
def my_tickers():
    held = {p["ticker"] for p in load_json(POS_FILE, [])}
    watched = set(load_json(WATCH_FILE, {}).keys())
    return (held | watched | {"NVDA", "AAPL", "SOFI", "HOOD"}) - {"SPY"}


def held_tickers():
    return {p["ticker"] for p in load_json(POS_FILE, [])}


# ---------------------------------------------------------------- track record
OUT_FIELDS = ["time", "date", "ticker", "event", "direction", "strength", "source", "title",
              "price0", "spy0", "r1", "r3", "r5", "x1", "x3", "x5"]


def record_outcome(t, info, item, price):
    spy = None
    try:
        spy, _, _ = quote("SPY")
    except Exception:
        pass
    new = not os.path.exists(OUTCOMES)
    with open(OUTCOMES, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        if new:
            w.writeheader()
        n = now()
        w.writerow({"time": n.isoformat(timespec="seconds"), "date": n.date().isoformat(), "ticker": t,
                    "event": info["event"], "direction": info["direction"], "strength": info["strength"],
                    "source": item["source"], "title": item["title"][:200],
                    "price0": price or "", "spy0": spy or ""})


def update_outcomes():
    """Fill in 1/3/5 trading-day returns, raw (r) and vs SPY (x)."""
    if not os.path.exists(OUTCOMES):
        return
    import yfinance as yf
    with open(OUTCOMES) as f:
        rows = list(csv.DictReader(f))
    todo = [r for r in rows if not r["r5"] and r["price0"]]
    if not todo:
        return
    start = min(r["date"] for r in todo)
    closes = {}

    def series(t):
        if t not in closes:
            try:
                h = yf.Ticker(t).history(start=start, interval="1d", auto_adjust=False)["Close"]
                h.index = [d.date().isoformat() for d in h.index]
                closes[t] = h
            except Exception:
                closes[t] = None
        return closes[t]

    spy = series("SPY")
    for r in todo:
        c = series(r["ticker"])
        if c is None or spy is None:
            continue
        after = [d for d in c.index if d > r["date"]]
        for k in (1, 3, 5):
            if r[f"r{k}"] or len(after) < k:
                continue
            d = after[k - 1]
            ret = c[d] / float(r["price0"]) - 1
            r[f"r{k}"] = f"{ret:.4f}"
            if r["spy0"] and d in spy.index:
                r[f"x{k}"] = f"{ret - (spy[d] / float(r['spy0']) - 1):.4f}"
    tmp = OUTCOMES + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, OUTCOMES)
    print(f"[outcomes] updated {len(todo)} rows")


def history(event):
    """(cases, avg 3-day return vs SPY, share that went up) for an event type."""
    if not os.path.exists(OUTCOMES):
        return 0, None, None
    xs = []
    with open(OUTCOMES) as f:
        for r in csv.DictReader(f):
            if r["event"] == event and r["x3"]:
                xs.append(float(r["x3"]))
    if not xs:
        return 0, None, None
    return len(xs), sum(xs) / len(xs), sum(1 for x in xs if x > 0) / len(xs)


def history_line(event):
    n, avg, up = history(event)
    if n < HIST_MIN:
        return f"Track record: building ({n} of {HIST_MIN} cases so far)"
    return f"Track record: {n} past cases, avg {avg:+.1%} vs SPY over 3 days, beat SPY {up:.0%} of the time"


def reaction(t):
    """Today's price move and time-adjusted volume vs normal."""
    import yfinance as yf
    try:
        h = yf.Ticker(t).history(period="1mo", interval="1d")
        n = now()
        if len(h) < 5 or h.index[-1].date() != n.date():
            return None, None
        open_min = max((n - n.replace(hour=8, minute=30, second=0, microsecond=0)).total_seconds() / 60, 0)
        frac = min(max(open_min / 390, 0.1), 1.0)
        avg = h["Volume"].iloc[:-1].tail(20).mean()
        return h["Close"].iloc[-1] / h["Close"].iloc[-2] - 1, (h["Volume"].iloc[-1] / (avg * frac) if avg else None)
    except Exception:
        return None, None


# ---------------------------------------------------------------- alert budget + evening digest
def state():
    st = load_json(STATE_FILE, {})
    today = now().date().isoformat()
    if st.get("day") != today:
        st = {"day": today, "alerts": 0, "digest": st.get("digest", [])}
    return st


def news_alert(msg, title, priority, tags, digest_line):
    """Send now if under today's budget (or important), else hold for the evening digest."""
    st = state()
    if priority >= 4 and st["alerts"] < MAX_ALERTS_DAY:
        st["alerts"] += 1
        save_json(STATE_FILE, st)
        alert(msg, title=title, priority=priority, tags=tags)
    else:
        digest_add(digest_line if priority >= 4 else title + "\n" + "\n".join("   " + l for l in msg.split("\n")[:4]))


def digest_add(line):
    st = state()
    st["digest"].append(line)
    save_json(STATE_FILE, st)


def check_in(label):
    st = state()
    items = st.get("digest", [])
    groups = {"Good news": [], "Bad news": [], "Deal watch": [], "Congress": [], "Other": []}
    for it in items:
        head = it.split("\n")[0]
        if it.startswith("+") or head.startswith("\U0001F4C8"):
            groups["Good news"].append(it.lstrip("+ "))
        elif it.startswith("-") or head.startswith("\u26A0"):
            groups["Bad news"].append(it.lstrip("- "))
        elif head.startswith("\U0001F440") or "deal" in head.lower():
            groups["Deal watch"].append(it)
        elif head.startswith("\U0001F3DB"):
            groups["Congress"].append(it)
        else:
            groups["Other"].append(it)
    watch_n = len(load_json(WATCH_FILE, {}))
    held_n = len(load_json(POS_FILE, []))
    footer = f"Deal watch: {watch_n} tickers | Open positions: {held_n}"
    if not items:
        if market_day(now().date())["open"]:
            alert(f"Nothing new since the last check-in.\n{footer}", title=f"{label} check-in: quiet",
                  priority=1, tags="zzz", now_please=True)
        return
    body = []
    for name, lst in groups.items():
        if lst:
            body.append(f"== {name} ({len(lst)})")
            body += lst[:8]
            if len(lst) > 8:
                body.append(f"   ...and {len(lst) - 8} more (send 'news')")
    body.append(footer)
    counts = ", ".join(f"{len(v)} {k.split()[0].lower()}" for k, v in groups.items() if v)
    alert("\n".join(body), title=f"\U0001F4CB {label} check-in: {counts}", priority=2, tags="clipboard",
          now_please=True)
    st["digest"] = []
    save_json(STATE_FILE, st)



BUYER_VERBS = re.compile(r"\b(to buy|buys|buying|to acquire|acquires|acquiring|agrees to buy|agrees to acquire|"
                         r"in talks to buy|in talks to acquire|nears deal to buy|bids for|makes offer for|"
                         r"offers to buy|to purchase|purchases)\b", re.I)


def is_buyer(t, company, title):
    """True when the headline's subject is BUYING something (the model sometimes flips this)."""
    names = {norm_name(company or "")} | {norm_name(tickers()["ticker"].get(t, ""))}
    words = {n.split()[0] for n in names if n} | {t.lower()}
    by = re.search(r"\b(acquired|bought|purchased|taken private) by\b(.{0,60})", title, re.I)
    if by and any(w in norm_name(by.group(2)).split() for w in words):
        return True
    m = BUYER_VERBS.search(title)
    if not m:
        return False
    before = norm_name(title[:m.start()]).split()
    return any(w in before for w in words)


# ---------------------------------------------------------------- handling news
MIN_SIGNAL_PRICE = 5.0
MIN_SIGNAL_VOLUME = 800_000


def signal_floor(t, price):
    """(ok, reason). A buy signal needs a $5+ stock that trades 800K+ shares a day."""
    try:
        import yfinance as yf
        vol = float(yf.Ticker(t).fast_info.three_month_average_volume or 0)
    except Exception as e:
        print(f"[floor] {t}: {e}")
        return True, ""
    if price is not None and price < MIN_SIGNAL_PRICE:
        return False, f"stock is under ${MIN_SIGNAL_PRICE:g}"
    if vol < MIN_SIGNAL_VOLUME:
        return False, f"only {vol / 1e6:.1f}M shares a day (want {MIN_SIGNAL_VOLUME / 1e6:g}M+)"
    return True, ""


def handle(item):
    if item["kind"] == "13d":
        t = tickers()["cik"].get(item["cik"])
        info = {"relevant": bool(t), "event": "activist_stake", "ticker": t, "company": item["company"],
                "direction": "bullish", "strength": 2,
                "summary": f"5%+ stake disclosed in {item['company']} (Schedule 13D)"}
    else:
        info = classify(item)
        if not info["relevant"]:
            return
        info["ticker"] = resolve(info["ticker"], info["company"], item["title"] + " " + item.get("summary", ""))
        if not info["ticker"] and len(item.get("hint") or []) == 1:
            info["ticker"] = item["hint"][0]
    t = info["ticker"]
    if not t:
        print(f"[skip] no US ticker: {item['title']}")
        return
    ok, price, mcap, why = tradeable(t)
    if not ok:
        print(f"[skip] {t} {why}: {item['title']}")
        return
    ev, direction, strength = info["event"], info["direction"], info["strength"]
    if ev in SPECULATIVE - {"activist_stake"} or ev == "deal_announced":
        if is_buyer(t, info.get("company"), item["title"]):
            print(f"[skip] {t} is the buyer, not the target: {item['title']}")
            return
    log_item({"ticker": t, "event": ev, "source": item["source"], "title": item["title"]}, price)
    record_outcome(t, info, item, price)

    px = f"${price:.2f}" if price else "?"
    held, watched = t in held_tickers(), t in load_json(WATCH_FILE, {})
    chg, volx = reaction(t)
    react = ""
    if chg is not None:
        react = f" | today {chg:+.1%}" + (f", volume {volx:.1f}x" if volx else "")
    line = f"{info['summary']}\n{t} {px}{react} | mkt cap {money(mcap)} | {item['source']}"
    if item.get("age_h") is not None:
        line += " | " + _age_txt(item["age_h"])
    hist = history_line(ev)
    short = f"{t} {EVENT_LABEL[ev]}: {info['summary'][:90]}"

    # takeovers keep their own handling (deal watch list)
    if ev in SPECULATIVE or ev in ("deal_announced", "deal_terminated"):
        with watch_locked() as wl:
            w = wl.get(t)
            stamp = now().isoformat(timespec="seconds")
            if ev in SPECULATIVE:
                if w is None:
                    if len(wl) >= MAX_WATCH:
                        wl.pop(min(wl, key=lambda k: wl[k]["last_event"]))
                    wl[t] = {"added": stamp, "last_event": stamp, "status": "watching", "reason": EVENT_LABEL[ev],
                             "price_added": price, "source": "news", "events": [], "alerted": {}}
                    info_alert(line + "\nAdded to deal watch. Speculative - many of these never become deals.\n" + hist,
                               title=f"\U0001F440 {t}: {EVENT_LABEL[ev]}", priority=3, tags="eyes", held=held)
                else:
                    w["last_event"] = stamp
                    info_alert(line + "\n" + hist, title=f"{t} follow-up: {EVENT_LABEL[ev]}", priority=4, tags="newspaper", held=held)
            elif ev == "deal_announced":
                if w is not None:
                    w["status"], w["last_event"] = "announced", stamp
                info_alert(line + "\nDon't chase: after a deal is announced the stock usually pins near the deal "
                                  "price, and calls lose value fast.",
                           title=f"{t}: deal announced", priority=4 if held else 3, tags="handshake", held=held)
            elif ev == "deal_terminated" and w is not None:
                wl.pop(t)
                info_alert(line + "\nRemoved from deal watch.", title=f"{t}: deal off", priority=4, tags="x", held=held)
            if t in wl:
                wl[t].setdefault("events", []).append({"time": stamp, "event": ev, "title": item["title"][:200]})
                wl[t]["events"] = wl[t]["events"][-10:]
        return

    # everything else: good news / bad news
    if direction == "bearish":
        n_, avg_, up_ = history(ev)
        proven_dn = n_ >= HIST_MIN and avg_ is not None and avg_ < -0.01 and up_ <= 0.4
        confirmed_dn = chg is not None and chg < -0.01 and (volx or 0) >= 1.5
        crushed = chg is not None and chg < -0.08
        floor_ok, floor_why = signal_floor(t, price)
        put_ok = floor_ok and confirmed_dn and not crushed and (proven_dn or strength == 3)
        st = state()
        if put_ok and _repeat_signal(t, ev, "P"):
            print(f"[dedupe] skipped repeat put signal {t} {ev}")
            return
        _extra = ""
        if put_ok and st["alerts"] < MAX_ALERTS_DAY:
            _send, _extra = pre_send(t, ev, "P", item["title"], info["summary"], chg, volx, held)
            if not _send:
                print(f"[trend] held back put signal {t} {ev}")
                digest_add(f"- held back (trend/model said no): {short}" + _extra.replace("\n", " | "))
                return
        if put_ok and st["alerts"] < MAX_ALERTS_DAY:
            st["alerts"] += 1
            save_json(STATE_FILE, st)
            why = [f"stock reacting: {chg:+.1%} on {volx:.1f}x volume"]
            if proven_dn:
                why.append("this news type has a losing track record for the stock")
            if strength == 3:
                why.append("major news")
            head = "\nYou HOLD this - consider your exit." if held else ""
            alert_signal(line + head + "\nPut signal: " + "; ".join(why) + _extra + earnings_line(t) + contract_text(t, cp="P") + "\n" + hist,
                  title=f"\U0001F53B PUT SIGNAL {t}: {EVENT_LABEL[ev]}", priority=5, tags="chart_with_downwards_trend",
                  actions=check_actions(t, "P"))
            record_signal(t, ev, "P")
            return
        if held or watched:
            where = "you HOLD this" if held else "it's on your watch list"
            info_alert(line + f"\nHeads up - {where}.\n" + hist, title=f"⚠️ {t}: {EVENT_LABEL[ev]}",
                       priority=5 if held else 4, tags="warning", held=held)
        else:
            digest_add(f"- {short}")
        return
    if direction == "bullish":
        n, avg, up = history(ev)
        proven = n >= HIST_MIN and avg is not None and avg > 0.01 and up >= 0.6
        confirmed = chg is not None and chg > 0.01 and (volx or 0) >= 1.5
        hot = chg is not None and chg > 0.08
        floor_ok, floor_why = signal_floor(t, price)
        opportunity = floor_ok and confirmed and not hot and (proven or strength == 3 or ((held or watched) and strength >= 2))
        st = state()
        if opportunity and _repeat_signal(t, ev, "C"):
            print(f"[dedupe] skipped repeat buy signal {t} {ev}")
            return
        _extra = ""
        if opportunity and st["alerts"] < MAX_ALERTS_DAY:
            _send, _extra = pre_send(t, ev, "C", item["title"], info["summary"], chg, volx, held)
            if not _send:
                print(f"[trend] held back buy signal {t} {ev}")
                digest_add(f"+ held back (trend/model said no): {short}" + _extra.replace("\n", " | "))
                return
        if opportunity and st["alerts"] < MAX_ALERTS_DAY:
            st["alerts"] += 1
            save_json(STATE_FILE, st)
            why = [f"stock reacting: {chg:+.1%} on {volx:.1f}x volume"]
            if proven:
                why.append("this news type has a winning track record")
            if strength == 3:
                why.append("major news")
            if held or watched:
                why.append("you hold/watch it")
            pp = paper_open(t, ev)
            alert_signal(line + "\nBuy signal: " + "; ".join(why) + _extra + earnings_line(t) + pp + contract_text(t) + "\n" + hist,
                  title=f"\U0001F525 BUY SIGNAL {t}: {EVENT_LABEL[ev]}", priority=5, tags="fire",
                  actions=check_actions(t))
            record_signal(t, ev)
        elif strength >= 2 or proven or held or watched:
            note = "\nAlready up a lot today - careful chasing." if hot else ""
            if not floor_ok:
                note += f"\nNot a buy signal: {floor_why}."
            info_alert(line + note + "\n" + hist, title=f"\U0001F4C8 {t}: {EVENT_LABEL[ev]}", priority=3, held=held)
        else:
            digest_add(f"+ {short}")


# quiet signals v1
# night digest v1
def _quiet_now():
    """True before 9:00 AM CT or whenever the market is closed: signals then are watch-only."""
    n = now()
    return (not is_open_now(n)) or n.time() < dtime(9, 0)


def _silent_now(n):
    """8 PM to 6 AM, weekends and holidays: signals are held for the next check-in, not pushed."""
    return n.time() >= dtime(20, 0) or n.time() < dtime(6, 0) or not market_day(n.date())["open"]


def alert_signal(msg, title, priority=5, tags=None, actions=None):
    if _silent_now(now()):
        mark = "-" if "PUT SIGNAL" in title else "+"
        digest_add(f"{mark} OVERNIGHT: {title} - {msg.split(chr(10))[0][:140]}")
        return
    if _quiet_now():
        why = "The market is closed." if not is_open_now(now()) else "The first 30 minutes after the open are too volatile."
        msg = (f"WATCH ONLY, NOT A BUY YET. {why} Re-check after 9:00 AM CT: tap Check 1 or 2 "
               "and see if the stock is still moving this way.\n\n" + msg)
        alert(msg, "WATCH: " + title, priority=4, tags=tags, now_please=True, actions=actions)
    else:
        alert(msg, title, priority=priority, tags=tags, actions=actions)


# trend gate v1
TREND_GATE = True      # signals whose trend disagrees go to the digest instead of your phone
LLM_VETO = False       # True = a 'WAIT' from the model also holds the signal back (leave False until it proves itself)
LLM_LOG = os.path.join(HERE, "llm_checks.csv")
_TREND_CACHE = {}


def _trend_hist(t):
    import yfinance as yf
    return yf.Ticker(t).history(period="8mo", auto_adjust=True, timeout=15)


def trend_check(t, side):
    """MACD histogram and 20-day average vs the signal's direction, from completed daily bars. None = unavailable."""
    key = (t, now().date().isoformat())
    if key not in _TREND_CACHE:
        try:
            import pandas as pd
            df = _trend_hist(t)
            h = df[df.index.tz_localize(None).normalize() < pd.Timestamp(now().date())]
            c = h["Close"]
            if len(c) < 40:
                raise ValueError("not enough history")
            macd = c.ewm(span=12).mean() - c.ewm(span=26).mean()
            hist = float((macd - macd.ewm(span=9).mean()).iloc[-1])
            _TREND_CACHE[key] = {"hist": hist, "close": float(c.iloc[-1]), "sma20": float(c.rolling(20).mean().iloc[-1])}
        except Exception as e:
            print(f"[trend] {t}: {e}")
            _TREND_CACHE[key] = None
    d = _TREND_CACHE[key]
    if not d:
        return None
    up = side == "C"
    macd_ok = (d["hist"] > 0) == up
    sma_ok = (d["close"] > d["sma20"]) == up
    return {"macd_ok": macd_ok, "sma_ok": sma_ok, "both": macd_ok and sma_ok, **d}


def trend_text(tr, side):
    if tr is None:
        return "Trend: couldn't be checked."
    way = "up" if side == "C" else "down"
    bits = [f"MACD {'agrees' if tr['macd_ok'] else 'DISAGREES'}",
            f"stock is {'above' if tr['close'] > tr['sma20'] else 'below'} its 20-day average ({tr['close']:.2f} vs {tr['sma20']:.2f})"]
    tail = "past cases when both agree: about 65% win vs 49%" if tr["both"] else "past cases when they don't both agree: about 49% win"
    return f"Trend ({way} signal): " + ", ".join(bits) + f". {tail}."


# llm checks v2
LLM_VETO_DUP = True    # the model says this is a rewrite of a story already sent (and names which one) -> hold it back
LLM_LOG = os.path.join(HERE, "llm_log2.csv")


def _recent_headlines(t, current_title, hours=48, n=8):
    out = []
    try:
        with open(OUTCOMES) as f:
            rows = [r for r in csv.DictReader(f) if r["ticker"] == t]
    except FileNotFoundError:
        return out
    cut = now() - timedelta(hours=hours)
    for r in reversed(rows):
        try:
            when = datetime.fromisoformat(r["time"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=now().tzinfo)
        except Exception:
            continue
        if when < cut:
            break
        if r["title"][:60] == current_title[:60]:
            continue
        out.append(f"{when.strftime('%a %-I:%M %p')}: {r['title'][:110]}")
        if len(out) >= n:
            break
    return out


def llm_lastlook(t, side, title, summary, chg, volx, tr):
    """The model's checks: same story as one already sent? supports the direction? priced in? contradicted?
    Python supplies the numbers. Fails open (returns None)."""
    way = "BUY CALLS (bet the stock goes up)" if side == "C" else "BUY PUTS (bet the stock goes down)"
    facts = f"today's move {chg:+.1%}" if chg is not None else "today's move unknown"
    if volx:
        facts += f", volume {volx:.1f}x normal"
    facts += ". " + trend_text(tr, side)
    recent = _recent_headlines(t, title)
    listing = "\n".join(f"{i}. {h}" for i, h in enumerate(recent, 1)) or "(none)"
    msg = ("You are a cautious options trader reviewing a signal before it is sent to a trader.\n"
           f"Proposed trade: {way} on {t}.\nNEW headline: {title}\nDetails: {summary}\nFacts: {facts}\n"
           f"Earlier headlines on {t} in the last 48 hours:\n{listing}\n\n"
           "Answer these:\n"
           "- same_story_as: the NUMBER of an earlier headline that reports the SAME specific event as the new headline "
           "(a rewrite or follow-up of one story), or 0 if the new headline is a different event.\n"
           "- supports_direction: true only if the new headline is really good news for the stock (calls) or bad news (puts).\n"
           "- priced_in: true if the stock has probably already moved on this news, given today's move.\n"
           "- conflict: true if an earlier headline points the other way.\n"
           "- verdict: GO or WAIT.\n"
           'Reply with JSON only: {"same_story_as": 0, "supports_direction": true, "priced_in": false, '
           '"conflict": false, "verdict": "GO", "reason": "under 15 words"}')
    try:
        r = requests.post(OLLAMA, json={"model": MODEL, "stream": False, "format": "json", "keep_alive": "30m",
                                        "options": {"temperature": 0}, "messages": [{"role": "user", "content": msg}]},
                          timeout=90)
        r.raise_for_status()
        d = json.loads(r.json()["message"]["content"])
        v = str(d.get("verdict", "")).upper()
        if v not in ("GO", "WAIT"):
            return None
        try:
            same = int(d.get("same_story_as") or 0)
        except (TypeError, ValueError):
            same = 0
        if not 1 <= same <= len(recent):
            same = 0   # it has to point at a headline that really exists
        return {"verdict": v, "reason": str(d.get("reason", ""))[:120], "same": recent[same - 1] if same else "",
                "supports": bool(d.get("supports_direction", True)), "priced": bool(d.get("priced_in", False)),
                "conflict": bool(d.get("conflict", False))}
    except Exception as e:
        print(f"[llm look] {e}")
        return None


def pre_send(t, ev, side, title, summary, chg, volx, held):
    """Runs just before a signal is pushed. Returns (send, extra_text)."""
    tr = trend_check(t, side)
    look = llm_lastlook(t, side, title, summary, chg, volx, tr)
    try:
        new = not os.path.exists(LLM_LOG)
        with open(LLM_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "ticker", "event", "side", "trend_both", "verdict", "same_story", "supports",
                            "priced_in", "conflict", "reason", "title"])
            L = look or {}
            w.writerow([now().isoformat(timespec="seconds"), t, ev, side, "" if tr is None else int(tr["both"]),
                        L.get("verdict", ""), L.get("same", ""), L.get("supports", ""), L.get("priced", ""),
                        L.get("conflict", ""), L.get("reason", ""), title[:120]])
    except Exception as e:
        print(f"[llm log] {e}")
    extra = "\n" + trend_text(tr, side)
    if look:
        flags = []
        if look["same"]:
            flags.append("SAME STORY as: " + look["same"])
        if not look["supports"]:
            flags.append("headline may not support this direction")
        if look["priced"]:
            flags.append("may be priced in")
        if look["conflict"]:
            flags.append("earlier news points the other way")
        extra += f"\nModel last look: {look['verdict']} - {look['reason']}"
        if flags:
            extra += "\nModel flags: " + "; ".join(flags)
    if held:
        return True, extra
    if TREND_GATE and tr is not None and not tr["both"]:
        return False, extra
    if look and LLM_VETO_DUP and look["same"]:
        return False, extra
    if look and LLM_VETO and look["verdict"] == "WAIT":
        return False, extra
    return True, extra


# event dedupe v1
DEDUPE_HOURS = 48      # same ticker + same news type + same direction inside this window = a repeat
MAX_SIGNALS_TICKER_DAY = 2


# skip suppress v1
def _skipped_today(t, side):
    """True if you tapped/typed Skip on this ticker and direction today."""
    try:
        import csv
        n = now()
        with open(os.path.join(HERE, "decisions.csv")) as f:
            for r in csv.DictReader(f):
                if r["decision"] == "skip" and r["ticker"] == t and r["side"] == side and r["time"][:10] == n.date().isoformat():
                    return True
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[skip check] {e}")
    return False


def _repeat_signal(t, ev, side="C"):
    """True if this is a re-report of a signal we already sent (or the ticker already had its fill today)."""
    if _skipped_today(t, side):
        return True
    try:
        with open(os.path.join(HERE, "buy_signals.json")) as f:
            sigs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    n = now()
    today = 0
    for x in sigs:
        if x.get("ticker") != t:
            continue
        try:
            when = datetime.fromisoformat(x["time"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=n.tzinfo)
        except Exception:
            continue
        hrs = (n - when).total_seconds() / 3600
        if x.get("event") == ev and x.get("side", "C") == side and hrs < DEDUPE_HOURS:
            return True
        if when.astimezone(n.tzinfo).date() == n.date():
            today += 1
    return today >= MAX_SIGNALS_TICKER_DAY


def record_signal(t, ev, side="C"):
    try:
        path = os.path.join(HERE, "buy_signals.json")
        try:
            with open(path) as f:
                sigs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            sigs = []
        sigs.append({"time": now().isoformat(timespec="seconds"), "ticker": t, "event": ev, "side": side,
                     "quiet": _quiet_now()})
        with open(path, "w") as f:
            json.dump(sigs[-200:], f)
    except Exception as e:
        print(f"[signal log] {e}")


from market_extras import contract_text


# info to digest v1
INFO_TO_DIGEST = True    # news heads-ups on tickers you do NOT hold go to the digest, not your phone


def info_alert(msg, title, priority=3, tags=None, held=False):
    """Unvalidated heads-ups. Held tickers still ping you (exit warnings); everything else waits for the digest."""
    if INFO_TO_DIGEST and not held:
        digest_add(f"~ {title}: " + msg.split("\n")[0][:140])
        return
    alert(msg, title=title, priority=priority, tags=tags)


def scan():
    first_run = not os.path.exists(SEEN_FILE)
    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)
    new = 0
    items = collect()
    if first_run:
        # don't make the model chew through a day of old headlines on first start
        for item in items:
            seen.append(hashlib.sha1(re.sub(r"\W+", "", item["title"].lower()).encode()).hexdigest()[:16])
        save_json(SEEN_FILE, seen)
        print(f"[scan] first run: marked {len(items)} existing headlines as seen")
        alert(f"News scanner is on. Skipped {len(items)} older headlines; new ones are read as they come in.",
              title="\U0001F4F0 News scanner on", priority=2, tags="newspaper")
        return
    for item in items:
        key = hashlib.sha1(re.sub(r"\W+", "", item["title"].lower()).encode()).hexdigest()[:16]
        if key in seen_set:
            continue
        seen_set.add(key)
        seen.append(key)
        age = _age_hours(item)
        if age is not None and age > MAX_NEWS_AGE_H:
            print(f"[stale] {age:.0f}h old, skipped: {item['title']}")
            continue
        item["age_h"] = age
        new += 1
        try:
            handle(item)
        except Exception:
            traceback.print_exc()
    save_json(SEEN_FILE, seen[-5000:])
    print(f"[scan] {now():%H:%M} {new} new items")


# ---------------------------------------------------------------- watching activity
def check_activity():
    import yfinance as yf
    n = now()
    today = n.date().isoformat()
    open_min = max((n - n.replace(hour=8, minute=30, second=0, microsecond=0)).total_seconds() / 60, 0)
    frac = min(max(open_min / 390, 0.1), 1.0)
    wl = load_json(WATCH_FILE, {})
    for t, w in wl.items():
        if w.get("status") == "announced":
            continue
        msgs = []
        vol_hit = call_hit = False
        chg = 0.0
        tk = yf.Ticker(t)
        try:
            h = tk.history(period="1mo", interval="1d")
            if len(h) > 5 and h.index[-1].date() == n.date():
                avg = h["Volume"].iloc[:-1].tail(20).mean()
                vol = h["Volume"].iloc[-1]
                ratio = vol / (avg * frac) if avg else 0
                chg = h["Close"].iloc[-1] / h["Close"].iloc[-2] - 1
                if ratio >= VOL_X and w.get("alerted", {}).get("vol") != today:
                    msgs.append(f"Volume {ratio:.1f}x normal for this time of day, price {chg:+.1%}")
                    w.setdefault("alerted", {})["vol"] = today
                    vol_hit = True
        except Exception as e:
            print(f"[vol] {t}: {e}")
        try:
            hits = []
            for e in tk.options[:2]:
                c = tk.option_chain(e).calls
                c = c[(c["volume"].fillna(0) >= CALL_MIN_VOL) &
                      (c["volume"].fillna(0) > CALL_VS_OI * c["openInterest"].fillna(0))]
                for _, r in c.iterrows():
                    hits.append((int(r["volume"]), float(r["strike"]), e, int(r["openInterest"] or 0)))
            if hits and w.get("alerted", {}).get("calls") != today:
                hits.sort(reverse=True)
                v, k, e, oi = hits[0]
                d = datetime.fromisoformat(e)
                msgs.append(f"Heavy call buying: {k:g}C {d.month}/{d.day} volume {v:,} vs open interest {oi:,}"
                            + (f" (+{len(hits) - 1} more strikes)" if len(hits) > 1 else ""))
                w.setdefault("alerted", {})["calls"] = today
                call_hit = True
        except Exception as e:
            print(f"[calls] {t}: {e}")
        if msgs:
            with watch_locked() as live:
                if t in live:
                    live[t]["alerted"] = w.get("alerted", {})
                    live[t]["last_event"] = now().isoformat(timespec="seconds")
            strong = (vol_hit and call_hit) or (vol_hit and chg > 0.02)
            if strong:
                pp = paper_open(t, "unusual_activity")
                alert("\n".join(msgs) + f"\nOn watch since {w['added'][:10]}: {w['reason']}.\n"
                      "Buy signal: unusual buying on a takeover-watch stock. Look now." + earnings_line(t) + pp,
                      title=f"\U0001F525 BUY SIGNAL {t}: unusual activity", priority=5, tags="fire",
                      actions=check_actions(t))
            else:
                info_alert("\n".join(msgs) + f"\nOn watch since {w['added'][:10]}: {w['reason']}.",
                           title=f"\U0001F6A8 {t}: unusual activity", priority=3, held=(t in held_tickers()))


def expire():
    n = now()
    with watch_locked() as wl:
        for t in list(wl):
            last = datetime.fromisoformat(wl[t]["last_event"])
            limit = ANNOUNCED_DAYS if wl[t].get("status") == "announced" else WATCH_DAYS
            if n - last > timedelta(days=limit):
                wl.pop(t)
                print(f"[expire] {t}")



# ---------------------------------------------------------------- Congress trades
# Data: Bargo free Congress API (bargo.ai) - House Clerk + Senate eFD disclosures.
# Free keyless limit is 30 requests/day, so we poll every 2 hours.
CONGRESS_URL = "https://www.bargo.ai/free-apis/congress/v1/trades"
CONGRESS_EVERY = 7200
CONG_MIN = 15000          # ignore buys under $15k (lower end of the disclosed range)
CONG_BIG = 250000         # a single buy this size goes on the watch list
CONG_CLUSTER = 2          # this many different members buying = watch list
CONG_SEEN = os.path.join(HERE, "congress_seen.json")
CONG_LOG = os.path.join(HERE, "congress_log.csv")
CONG_FIELDS = ["disclosed", "traded", "member", "chamber", "ticker", "type", "amount", "low",
               "price_then", "price_now"]


def _g(d, *names):
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return None


def _low(amount):
    nums = re.findall(r"[\d,]+", str(amount or ""))
    return int(nums[0].replace(",", "")) if nums else 0


def fetch_congress():
    r = requests.get(CONGRESS_URL, params={"limit": 100}, timeout=30,
                     headers={"User-Agent": "newswatch (personal use)"})
    r.raise_for_status()
    j = r.json()
    rows = j if isinstance(j, list) else (_g(j, "trades", "data", "results", "items") or [])
    out = []
    for d in rows:
        t = (_g(d, "ticker", "symbol") or "").upper().strip()
        typ = str(_g(d, "type", "trade_type", "transaction_type") or "").lower()
        out.append({
            "member": _g(d, "member", "name", "representative", "senator") or "?",
            "chamber": _g(d, "chamber") or "",
            "ticker": t if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", t) else "",
            "type": typ,
            "buy": ("purchase" in typ or "buy" in typ) and "sale" not in typ,
            "amount": _g(d, "amount_range", "amount") or "",
            "traded": str(_g(d, "transaction_date", "tx_date") or "")[:10],
            "disclosed": str(_g(d, "disclosure_date", "disclosed") or "")[:10],
            "price_then": _g(d, "estimated_price", "price_at_trade", "est_price"),
            "price_now": _g(d, "recent_price", "current_price"),
        })
    return out


def _short_date(iso):
    try:
        d = datetime.fromisoformat(iso)
        return f"{d.month}/{d.day}"
    except Exception:
        return iso or "?"


def congress_scan():
    first_run = not os.path.exists(CONG_SEEN)
    seen = load_json(CONG_SEEN, [])
    seen_set = set(seen)
    fresh = []
    for tr in fetch_congress():
        key = "|".join([tr["member"], tr["ticker"], tr["traded"], tr["type"], str(tr["amount"])])
        if key in seen_set:
            continue
        seen_set.add(key)
        seen.append(key)
        if not tr["buy"] or not tr["ticker"]:
            continue
        tr["low"] = _low(tr["amount"])
        if tr["low"] < CONG_MIN:
            continue
        fresh.append(tr)
    save_json(CONG_SEEN, seen[-20000:])
    if not fresh:
        print(f"[congress] {now():%H:%M} nothing new")
        return

    new_log = not os.path.exists(CONG_LOG)
    with open(CONG_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CONG_FIELDS, extrasaction="ignore")
        if new_log:
            w.writeheader()
        for tr in fresh:
            w.writerow(tr)

    # who has bought each ticker in the last 45 days (from our own log)
    buyers = {}
    cutoff = (now() - timedelta(days=45)).date().isoformat()
    with open(CONG_LOG) as f:
        for r in csv.DictReader(f):
            if (r["disclosed"] or r["traded"]) >= cutoff:
                buyers.setdefault(r["ticker"], set()).add(r["member"])

    recent = (now() - timedelta(days=14)).date().isoformat()
    watch_adds = []
    for tr in fresh:
        n_buyers = len(buyers.get(tr["ticker"], ()))
        if (tr["disclosed"] or tr["traded"]) >= recent and (n_buyers >= CONG_CLUSTER or tr["low"] >= CONG_BIG):
            why = (f"{n_buyers} members bought in 45 days" if n_buyers >= CONG_CLUSTER
                   else f"{tr['member']} bought {tr['amount']}")
            watch_adds.append((tr["ticker"], why))

    lines = []
    if first_run:
        alert(f"Loaded {len(fresh)} recent Congress buys (last ~3 months). From now on you'll get new "
              f"disclosures as they come in. Send 'congress' to see them.\nData: Bargo (bargo.ai)",
              title="\U0001F3DB Congress tracking on", priority=2, tags="classical_building")
        fresh_for_digest = []
    else:
        fresh_for_digest = fresh
    for tr in fresh_for_digest[:12]:
        lag = ""
        try:
            lag = f", {(datetime.fromisoformat(tr['disclosed']) - datetime.fromisoformat(tr['traded'])).days}d late"
        except Exception:
            pass
        since = ""
        try:
            pt, pn = float(tr["price_then"]), float(tr["price_now"])
            since = f" | {pn / pt - 1:+.0%} since"
        except Exception:
            pass
        lines.append(f"- {tr['member']} bought {tr['ticker']} {tr['amount']} on {_short_date(tr['traded'])}"
                     f" (disclosed {_short_date(tr['disclosed'])}{lag}){since}")
    if len(fresh_for_digest) > 12:
        lines.append(f"...and {len(fresh_for_digest) - 12} more (send 'congress')")
    if lines:
        alert("\n".join(lines) + "\nData: Bargo (bargo.ai). Disclosures lag the trade - check how far it already moved.",
              title=f"\U0001F3DB Congress buys: {len(fresh)} new", priority=3, tags="classical_building")

    for t, why in dict(watch_adds).items():
        ok, price, mcap, reason = tradeable(t)
        if not ok:
            print(f"[congress] skip {t}: {reason}")
            continue
        with watch_locked() as wl:
            stamp = now().isoformat(timespec="seconds")
            if t in wl:
                wl[t]["last_event"] = stamp
                continue
            wl[t] = {"added": stamp, "last_event": stamp, "status": "watching",
                     "reason": f"Congress: {why}", "price_added": price, "source": "congress",
                     "events": [], "alerted": {}}
        alert(f"{why}\n{t} ${price:.2f} | mkt cap {money(mcap)}\nAdded to deal watch - you'll get pinged on "
              f"unusual volume or call buying.", title=f"\U0001F3DB {t}: Congress buying", priority=4,
              tags="classical_building")



# ---------------------------------------------------------------- paper trading of buy signals
# Every BUY SIGNAL gets a pretend 1-contract call, managed by your rules:
# out at -20%, flat by Friday 2:30 PM, never held into the last day before expiry.
PAPER_FILE = os.path.join(HERE, "paper.json")
PAPER_LOG = os.path.join(HERE, "paper_log.csv")
PAPER_FIELDS = ["opened", "closed", "ticker", "signal", "expiry", "strike", "entry", "exit", "pct", "usd",
                "reason", "earnings_before_expiry"]


def paper_open(t, signal):
    return ""
    n = now()
    if n.weekday() > 4 or (n.weekday() == 4 and n.time() >= dtime(13, 0)):
        return ""
    book = load_json(PAPER_FILE, [])
    if any(p["ticker"] == t for p in book):
        return ""
    try:
        c = pick_call(t, n.date())
    except Exception as e:
        print(f"[paper] {t}: {e}")
        c = None
    if not c:
        return "\n\U0001F4DD Paper trade skipped: no liquid call 9-30 days out."
    eb = None
    try:
        eb = earnings_before(t, c["expiry"], n.date())
    except Exception:
        pass
    book.append({"ticker": t, "signal": signal, "opened": n.isoformat(timespec="seconds"),
                 "expiry": c["expiry"], "strike": c["strike"], "entry": c["mid"],
                 "earnings": eb.isoformat() if eb else ""})
    save_json(PAPER_FILE, book)
    d = date.fromisoformat(c["expiry"])
    return (f"\n\U0001F4DD Paper trade: 1x {c['strike']:g}C {d.month}/{d.day} @ {c['mid']:.2f}"
            + (" (earnings before expiry)" if eb else ""))


def paper_check():
    return
    book = load_json(PAPER_FILE, [])
    if not book:
        return
    n = now()
    keep = []
    for p in book:
        try:
            mid = quote_option(p["ticker"], p["expiry"], p["strike"])
        except Exception as e:
            print(f"[paper] quote {p['ticker']}: {e}")
            mid = None
        if mid is None:
            keep.append(p)
            continue
        pct = mid / p["entry"] - 1
        days_left = (date.fromisoformat(p["expiry"]) - n.date()).days
        reason = None
        if pct <= -0.20:
            reason = "stop -20%"
        elif n.weekday() == 4 and n.time() >= dtime(14, 30):
            reason = "Friday close"
        elif days_left <= 1:
            reason = "expiry"
        if not reason:
            keep.append(p)
            continue
        usd = (mid - p["entry"]) * 100
        new = not os.path.exists(PAPER_LOG)
        with open(PAPER_LOG, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=PAPER_FIELDS)
            if new:
                w.writeheader()
            w.writerow({"opened": p["opened"], "closed": n.isoformat(timespec="seconds"), "ticker": p["ticker"],
                        "signal": p["signal"], "expiry": p["expiry"], "strike": p["strike"],
                        "entry": p["entry"], "exit": round(mid, 2), "pct": f"{pct:.4f}", "usd": f"{usd:.2f}",
                        "reason": reason, "earnings_before_expiry": p.get("earnings", "")})
        alert(f"{p['ticker']} {p['strike']:g}C: {p['entry']:.2f} -> {mid:.2f} = {pct:+.0%} "
              f"({'+' if usd >= 0 else '-'}${abs(usd):,.0f} per contract)\nSignal: {p['signal']}",
              title=f"\U0001F4DD Paper trade closed ({reason})", priority=3)
    save_json(PAPER_FILE, keep)


# ---------------------------------------------------------------- main
def market_open(n):
    return is_open_now(n)


def busy_hours(n):
    return n.weekday() < 5 and dtime(6, 0) <= n.time() <= dtime(20, 0)


def selftest():
    print("Checking feeds...")
    for source, url in FEEDS:
        try:
            items = parse_feed(fetch(url))
            print(f"  OK   {source}: {len(items)} items" + (f" - e.g. {items[0]['title'][:70]}" if items else ""))
        except Exception as e:
            print(f"  FAIL {source}: {e}")
    for url in SEC_13D:
        try:
            items = parse_feed(fetch(url, sec=True))
            print(f"  OK   SEC 13D: {len(items)} filings")
        except Exception as e:
            print(f"  FAIL SEC 13D: {e}")
    print(f"SEC ticker list: {len(tickers()['ticker'])} tickers")
    try:
        ct = fetch_congress()
        buys = [c for c in ct if c["buy"]]
        print(f"  OK   Congress (Bargo): {len(ct)} trades, {len(buys)} buys"
              + (f" - e.g. {buys[0]['member']} {buys[0]['ticker']} {buys[0]['amount']}" if buys else ""))
    except Exception as e:
        print(f"  FAIL Congress (Bargo): {e}")
    print(f"\nAsking {MODEL} about sample headlines (first call loads the model, can take a minute)...")
    samples = [
        ("Bumble explores sale after takeover interest from private equity, sources say", "Reuters"),
        ("Apple to buy small AI startup for undisclosed sum", "Bloomberg"),
        ("Micron raises guidance as AI memory demand surges", "CNBC"),
        ("Boeing cuts delivery forecast, shares slide", "Reuters"),
        ("5 stocks to watch this week", "MarketWatch"),
    ]
    for title, src in samples:
        t0 = time.time()
        try:
            d = classify({"title": title, "summary": "", "source": src})
            d["resolved"] = resolve(d["ticker"], d["company"], title) if d["relevant"] else None
            print(f"  {title}\n    -> {d['event']} | {d['direction']} {d['strength']} | {d['company']} | "
                  f"ticker {d['resolved']} | {time.time() - t0:.0f}s")
        except Exception as e:
            print(f"  FAIL: {e}")


def main():
    if TEST:
        selftest()
        return
    if "YOUR_EMAIL" in SEC_UA:
        print("Set SEC_UA to include your email (SEC requires it). 13D feed will likely be blocked.")
    print(f"newswatch up, model {MODEL}")
    last_scan = last_act = last_cong = 0.0
    last_expire = None
    last_outcomes = last_digest = None
    sent_checkins = set()
    while True:
        n = now()
        try:
            beat("newswatch")
        except Exception:
            pass
        scan_every = 600 if busy_hours(n) else 3600
        if time.time() - last_scan >= scan_every:
            last_scan = time.time()
            try:
                scan()
                beat("newswatch", scan_ok=time.time(), feeds_failed=FEED_STATS["fail"],
                     feeds_total=FEED_STATS["total"])
            except Exception:
                traceback.print_exc()
        if time.time() - last_cong >= CONGRESS_EVERY:
            last_cong = time.time()
            try:
                congress_scan()
            except Exception:
                traceback.print_exc()
        if market_open(n) and time.time() - last_act >= 900:
            last_act = time.time()
            try:
                check_activity()
            except Exception:
                traceback.print_exc()
            try:
                paper_check()
            except Exception:
                traceback.print_exc()
        for hh, mm, label in CHECKINS:
            slot = f"{n.date()}-{hh}"
            if dtime(hh, mm) <= n.time() < dtime(hh, 59) and slot not in sent_checkins:
                sent_checkins.add(slot)
                if label in ("6 AM", "8 AM"):
                    try:
                        scan()
                        last_scan = time.time()
                    except Exception:
                        traceback.print_exc()
                try:
                    check_in(label)
                except Exception:
                    traceback.print_exc()
        if n.time() >= dtime(16, 0) and last_outcomes != n.date():
            last_outcomes = n.date()
            try:
                update_outcomes()
            except Exception:
                traceback.print_exc()
        if last_expire != n.date():
            last_expire = n.date()
            try:
                expire()
            except Exception:
                traceback.print_exc()
        time.sleep(30)


if __name__ == "__main__":
    main()
