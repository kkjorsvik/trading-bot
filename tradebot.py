#!/usr/bin/env python3
"""
tradebot.py - Tim's trade log + alert bot, talking over self-hosted ntfy.

Phone -> topic "trade":   buy / sell / list / nvda / rules / drop / help
Bot   -> topic "trade":   confirmations and answers
Bot   -> topic "alerts":  tiered alerts (urgent / action / info)

Rulebook it enforces (warns, never blocks):
  1. Open trades Wed-Fri only; be flat by Friday close
  2. NVDA calls only in the pullback zone (not stretched above 10-day avg, RSI < 60)
  3. SPY entries as usual
  4. 9+ days to expiry; out at -20%

Prices come from Yahoo and are delayed ~15 minutes. Treat alerts as
"go look now", not as exact fills.
"""
import csv
import json
import os
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from market_extras import earnings_line, earnings_before, next_earnings
from market_extras import contract_text
from market_extras import market_day, is_open_now, last_session_of_week, beat
LAST_QUOTE_OK = 0.0

# ---------------------------------------------------------------- settings
NTFY = "http://localhost:2586"
CMD_TOPIC = "trade"
ALERT_TOPIC = "alerts"
BOT_TITLE = "tradebot"
TZ = ZoneInfo("America/Chicago")

STOP = -0.20               # urgent alert at or below this
TARGETS = [0.20, 0.40]     # action alerts at these gains
SOFT = 0.10                # heads-up at +10% and -10%
STOP_REPEAT_MIN = 15       # re-ping while still below the stop
MIN_DTE = 9                # rule: days to expiry at entry
NVDA_MAX_EXT10 = 0.02      # pullback zone: <= 2% above 10-day avg
NVDA_MAX_RSI = 60          # ...and RSI(14) under 60
TRADE_DAYS = {0, 1, 2, 3, 4}  # Mon-Fri (Mon = 0)  # puts+monday v1
DAY_START = {0: dtime(11, 0), 1: dtime(9, 0), 2: dtime(9, 0), 3: dtime(9, 0), 4: dtime(9, 0)}  # tuesday open v1: Mon 11:00, Tue-Fri 9:00 (the open is too volatile)
HOT_DAYS = {2, 3, 4}       # the 12-for-12 SPY setup was Wed-Fri


def can_trade(n):
    return market_day(n.date())["open"] and n.weekday() in TRADE_DAYS and n.time() >= DAY_START.get(n.weekday(), dtime(0, 0))


def window_msg(n):
    md = market_day(n.date())
    if not md["open"] and n.weekday() < 5:
        return f"Market closed today ({md['name']})."
    if n.weekday() in DAY_START and n.time() < DAY_START[n.weekday()]:
        return f"{n:%A} trading opens at {DAY_START[n.weekday()].strftime('%H:%M').lstrip('0')}."
    return "Weekend - market's closed." if n.weekday() > 4 else "Outside your trading window."

WATCHLIST = {"SPY", "NVDA", "AAPL", "SOFI", "HOOD"}

POLL_SEC = 120             # position check interval during market hours
NVDA_POLL_MIN = 15         # pullback-zone check interval
MARKET_OPEN = dtime(8, 30)
MARKET_CLOSE = dtime(15, 0)

HERE = os.path.dirname(os.path.abspath(__file__))
POS_FILE = os.path.join(HERE, "positions.json")
LOG_FILE = os.path.join(HERE, "trade_log.csv")

PRI_URGENT, PRI_ACTION, PRI_INFO = 5, 4, 2
lock = threading.RLock()


# ---------------------------------------------------------------- ntfy
def _hdr(text):
    """Header-safe text: plain ASCII stays as is, anything else is RFC 2047 encoded (ntfy decodes it)."""
    import base64
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def send(topic, msg, title=None, priority=3, tags=None, actions=None):
    headers = {"Priority": str(priority)}
    if actions:
        headers["Actions"] = actions
    if title:
        headers["Title"] = _hdr(title)
    if tags:
        headers["Tags"] = tags
    try:
        requests.post(f"{NTFY}/{topic}", data=msg.encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        print(f"[send failed] {e}")


# reply spacing v1
_LAST_REPLY = [0.0]


def reply(msg):
    # ntfy can show same-second messages out of order, so keep replies at least 1.2s apart
    gap = 1.2 - (time.time() - _LAST_REPLY[0])
    if gap > 0:
        time.sleep(gap)
    send(CMD_TOPIC, msg, title=BOT_TITLE, priority=3)
    _LAST_REPLY[0] = time.time()


def alert(msg, title, priority, tags=None, actions=None):
    send(ALERT_TOPIC, msg, title=title, priority=priority, tags=tags, actions=actions)


# ---------------------------------------------------------------- storage
def now():
    return datetime.now(TZ)


def load_positions():
    try:
        with open(POS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_positions(pos):
    tmp = POS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pos, f, indent=2)
    os.replace(tmp, POS_FILE)


def log_trade(action, pid, qty, price, pnl=""):
    new = not os.path.exists(LOG_FILE)  # log guard v1
    if not new:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            if f.tell():
                f.seek(-1, 2)
                if f.read(1) not in (b"\n", b"\r"):
                    with open(LOG_FILE, "a", newline="") as g:
                        g.write("\n")
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "action", "contract", "qty", "price", "realized_pnl"])
        w.writerow([now().isoformat(timespec="seconds"), action, pid, qty, price, pnl])


# ---------------------------------------------------------------- parsing
RE_CONTRACT = re.compile(r"^(\d+(?:\.\d+)?)([cp])$", re.I)
RE_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$")
RE_QTY = re.compile(r"^(?:x(\d+)|(\d+)x)$", re.I)
RE_PRICE = re.compile(r"^@(\d*\.?\d+)$")


def parse_expiry(m, today):
    mo, dy, yr = int(m.group(1)), int(m.group(2)), m.group(3)
    if yr:
        yr = int(yr)
        yr = yr + 2000 if yr < 100 else yr
        return date(yr, mo, dy)
    d = date(today.year, mo, dy)
    return d if d >= today else date(today.year + 1, mo, dy)


def parse_trade(words, today):
    """words after the verb: TICKER STRIKE[C|P] [M/D] [xQTY] [@PRICE] in any order after ticker."""
    if not words:
        raise ValueError("missing ticker")
    out = {"ticker": words[0].upper(), "strike": None, "cp": None,
           "expiry": None, "qty": None, "price": None}
    for w in words[1:]:
        if m := RE_CONTRACT.match(w):
            out["strike"], out["cp"] = float(m.group(1)), m.group(2).upper()
        elif m := RE_DATE.match(w):
            out["expiry"] = parse_expiry(m, today)
        elif m := RE_QTY.match(w):
            out["qty"] = int(m.group(1) or m.group(2))
        elif m := RE_PRICE.match(w):
            out["price"] = float(m.group(1))
        else:
            raise ValueError(f"didn't understand '{w}'")
    if out["strike"] is None:
        raise ValueError("missing strike, like 750C")
    return out


def fmt_strike(s):
    return f"{s:g}"


FEE_PER_CONTRACT = 0.65   # Fidelity commission per contract, each way - change if yours differs


def net_pct_usd(entry, price, qty):
    """P/L after fees on both the buy and the sell: (pct, dollars)."""
    cost = entry * qty * 100 + FEE_PER_CONTRACT * qty
    proceeds = price * qty * 100 - FEE_PER_CONTRACT * qty
    return (proceeds / cost - 1 if cost else 0.0), proceeds - cost


def level_price(entry, pct):
    """Sell price (per share) at which your P/L after fees equals pct."""
    return ((1 + pct) * (entry * 100 + FEE_PER_CONTRACT) + FEE_PER_CONTRACT) / 100


def pid_of(t, strike, cp, expiry):
    return f"{t} {fmt_strike(strike)}{cp} {expiry.month}/{expiry.day}"


# ---------------------------------------------------------------- market data
def option_mid(ticker, expiry_iso, strike, cp, cache=None):
    """Bid (the price you would sell at); last trade only when the market is closed. Yahoo, ~15 min delayed."""
    try:
        import alpaca_live
        q = alpaca_live.option_quote(ticker, expiry_iso, strike, cp)
        if q and q["bid"] > 0 and q["ask"] > 0 and q["age_min"] is not None and q["age_min"] <= 20:
            return q["bid"]
    except Exception as e:
        print(f"[alpaca option] {e}")
    import yfinance as yf
    key = (ticker, expiry_iso)
    if cache is not None and key in cache:
        chain = cache[key]
    else:
        chain = yf.Ticker(ticker).option_chain(expiry_iso)
        if cache is not None:
            cache[key] = chain
    df = chain.calls if cp == "C" else chain.puts
    row = df[(df["strike"] - strike).abs() < 1e-6]
    if row.empty:
        return None
    r = row.iloc[0]
    bid, ask, last = float(r.get("bid") or 0), float(r.get("ask") or 0), float(r.get("lastPrice") or 0)
    if bid > 0 and ask > 0:
        return bid          # what you would get selling, not the middle of the quote
    if not market_open(now()):
        return last or None  # market closed: show the last trade
    return None             # market open but no bid yet: do not guess


def rsi(series, n=14):
    d = series.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return float((100 - 100 / (1 + g / l)).iloc[-1])


def nvda_state():
    import yfinance as yf
    t = yf.Ticker("NVDA")
    closes = t.history(period="3mo", interval="1d", auto_adjust=True)["Close"].dropna()
    try:
        live = float(t.fast_info.last_price)
    except Exception:
        live = float(closes.iloc[-1])
    closes.index = closes.index.tz_convert(TZ) if closes.index.tz else closes.index.tz_localize(TZ)
    if closes.index[-1].date() == now().date():
        closes.iloc[-1] = live
    else:
        closes = pd.concat([closes, pd.Series([live], index=[pd.Timestamp(now())])])
    ext10 = live / closes.iloc[-10:].mean() - 1
    ext20 = live / closes.iloc[-20:].mean() - 1
    r = rsi(closes)
    zone = ext10 <= NVDA_MAX_EXT10 and r < NVDA_MAX_RSI
    avg10 = float(closes.iloc[-10:].mean())
    return {"price": live, "ext10": ext10, "ext20": ext20, "rsi": r, "zone": zone, "avg10": avg10}


def nvda_text(s):
    if s["zone"]:
        return f"\u2705 NVDA: OK to buy. It has cooled off.\nPrice ${s['price']:.2f}"
    why = []
    if s["ext10"] > NVDA_MAX_EXT10:
        why.append(f"it's {s['ext10']:.1%} above its 10-day average")
    if s["rsi"] >= NVDA_MAX_RSI:
        why.append(f"it's running hot (RSI {s['rsi']:.0f})")
    msg = f"\u274c NVDA: Don't buy. Not ready - {' and '.join(why)}.\nPrice ${s['price']:.2f}"
    if s["ext10"] > NVDA_MAX_EXT10 and "avg10" in s:
        msg += f" | ready around ${s['avg10'] * (1 + NVDA_MAX_EXT10):.2f} or lower"
    return msg



# ---------------------------------------------------------------- SPY hot buy
HOT_DTE = (9, 14)        # expiries to consider
HOT_OTM = 0.01           # strikes from at-the-money up to 1% above
HOT_MAX_SPREAD = 0.05    # skip contracts with bid/ask spread over 5%
HOT_MIN_OI = 500         # skip thinly traded strikes


def _spy_hot_base(n=None):
    n = n or now()
    if not can_trade(n):
        return False, window_msg(n)
    if n.weekday() not in HOT_DAYS:
        return False, "Mon/Tue is OK to trade, but your hot setup is Wed-Fri."
    if n.weekday() == 4 and n.time() >= dtime(13, 0):
        return False, "Too late Friday - no time to work before the close."
    return True, "Wed-Fri, 9-14 days out, out by Friday. Your setup went 12 for 12."


# spy trend v1
HOT_MIN_DAY = -0.002    # SPY below yesterday's close by more than this -> not hot
HOT_MIN_NOW = -0.003    # down this much since the open, or over the last hour -> not hot
_SPY_TREND = {"t": 0.0, "d": None}


def _spy_trend():
    if _SPY_TREND["d"] is not None and time.time() - _SPY_TREND["t"] < 60:
        return _SPY_TREND["d"]
    import alpaca_live
    d = alpaca_live.trend(["SPY"]).get("SPY")
    _SPY_TREND.update(t=time.time(), d=d)
    return d


def spy_hot(n=None):
    n = n or now()
    ok, why = _spy_hot_base(n)
    if not ok:
        return ok, why
    try:
        d = _spy_trend()
    except Exception as e:
        print(f"[spy] trend: {e}")
        return ok, why + " (couldn't read today's SPY trend, check it yourself)"
    if not d:
        return ok, why + " (no SPY trend data yet today)"
    live = market_open(n)
    day = d.get("day")
    opn = d.get("open") if live else None
    m60 = d.get("m60") if live else None
    bits = []
    if day is not None:
        bits.append(f"{day:+.1%} vs yesterday's close" + ("" if live else " (pre-market)"))
    if opn is not None:
        bits.append(f"{opn:+.1%} since the open")
    if m60 is not None:
        bits.append(f"{m60:+.1%} in the last hour")
    now_txt = "SPY today: " + ", ".join(bits) + "." if bits else ""
    bad = []
    if day is not None and day < HOT_MIN_DAY:
        bad.append("SPY is below yesterday's close")
    if opn is not None and opn < HOT_MIN_NOW:
        bad.append("it's falling since the open")
    if m60 is not None and m60 < HOT_MIN_NOW:
        bad.append("it's dropping over the last hour")
    if bad:
        return False, "Not now: " + "; ".join(bad) + f". {now_txt} The day of the week fits, but the market isn't cooperating."
    return True, why + " " + now_txt


def spy_picks(top=3):
    """Near-the-money SPY calls 9-14 days out with tight spreads. Yahoo, ~15 min delayed."""
    import yfinance as yf
    t = yf.Ticker("SPY")
    try:
        spot = float(t.fast_info.last_price)
    except Exception:
        spot = float(t.history(period="5d")["Close"].iloc[-1])
    today = now().date()
    rows = []
    for e in t.options:
        dte = (date.fromisoformat(e) - today).days
        if not HOT_DTE[0] <= dte <= HOT_DTE[1]:
            continue
        c = t.option_chain(e).calls
        c = c[(c["strike"] >= spot * 0.998) & (c["strike"] <= spot * (1 + HOT_OTM))]
        for _, r in c.iterrows():
            bid, ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid
            oi = int(r.get("openInterest") or 0)
            if spread > HOT_MAX_SPREAD or oi < HOT_MIN_OI:
                continue
            score = abs(dte - 10) + abs(r["strike"] / spot - 1.003) * 300
            rows.append({"exp": date.fromisoformat(e), "dte": dte, "strike": float(r["strike"]),
                         "mid": mid, "spread": spread, "oi": oi, "score": score})
    rows.sort(key=lambda x: x["score"])
    return spot, rows[:top]


def spy_text(with_picks=True, budget=None):
    hot, why = spy_hot()
    head = f"\U0001F525 SPY: HOT BUY day. {why}" if hot else f"SPY: not a hot buy. {why}"
    if not with_picks or not can_trade(now()):
        return head
    try:
        spot, picks = spy_picks()
    except Exception as e:
        return head + f"\n(couldn't pull the option chain: {e})"
    if not picks:
        return head + f"\nSPY ${spot:.2f} - no liquid 9-14 day calls near the money right now."
    per = f" with ${budget:,.0f}" if budget else ""
    lines = [head, f"SPY ${spot:.2f}. Best fits for your setup{per}:"]
    first_qty = 5
    for i, k in enumerate(picks, 1):
        qty = int(budget // (k["mid"] * 100)) if budget else 5
        if i == 1:
            first_qty = max(qty, 1)
        if budget and qty < 1:
            lines.append(f"{i}. {fmt_strike(k['strike'])}C {k['exp'].month}/{k['exp'].day} ~${k['mid']:.2f} - "
                         f"one contract is ${k['mid'] * 100:,.0f}, over budget")
            continue
        cost = qty * k["mid"] * 100
        risk = cost * -STOP
        lines.append(f"{i}. {fmt_strike(k['strike'])}C {k['exp'].month}/{k['exp'].day} ({k['dte']}d) "
                     f"~${k['mid']:.2f} | x{qty} = ${cost:,.0f} | stop {k['mid'] * (1 + STOP):.2f} "
                     f"(risk ${risk:,.0f})")
    lines.append(f"To log: buy SPY {fmt_strike(picks[0]['strike'])}C {picks[0]['exp'].month}/{picks[0]['exp'].day} x{first_qty} @price")
    return "\n".join(lines)


# ---------------------------------------------------------------- rules
MIN_STOCK_PRICE = 5.0
MIN_AVG_VOLUME = 800_000


def catalyst(ticker, cp="C"):
    """Fresh news in your direction (bullish for calls, bearish for puts) in the last 3 days."""
    want = "bullish" if cp == "C" else "bearish"
    cutoff = (now() - timedelta(days=3)).date().isoformat()
    try:
        for r in _outcomes():
            if r["ticker"] == ticker and r["date"] >= cutoff and r["direction"] == want:
                return r["event"].replace("_", " ")
    except Exception:
        pass
    try:
        with open(os.path.join(HERE, "buy_signals.json")) as f:
            for x in json.load(f):
                if x["ticker"] == ticker and x.get("side", "C") == cp and x["time"][:10] >= cutoff:
                    return x["event"].replace("_", " ")
    except Exception:
        pass
    return None


def oneoff_problems(t):
    """Conditions an off-list ticker must meet. Returns the ones that failed."""
    bad = []
    try:
        import yfinance as yf
        fi = yf.Ticker(t["ticker"]).fast_info
        px = float(fi.last_price)
        vol = float(fi.three_month_average_volume or 0)
        if px < MIN_STOCK_PRICE:
            bad.append(f"stock is ${px:.2f}, under your ${MIN_STOCK_PRICE:g} floor")
        if vol < MIN_AVG_VOLUME:
            bad.append(f"only {vol / 1e6:.1f}M shares a day (want {MIN_AVG_VOLUME / 1e6:g}M+)")
    except Exception as e:
        bad.append(f"couldn't check stock price/volume ({e})")
    try:
        import yfinance as yf
        ch = yf.Ticker(t["ticker"]).option_chain(t["expiry"])
        df = ch.calls if t["cp"] == "C" else ch.puts
        row = df[(df["strike"] - float(t["strike"])).abs() < 1e-6]
        if row.empty:
            bad.append("couldn't find that contract to check liquidity")
        else:
            r = row.iloc[0]
            bid, ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
            oi = int(r.get("openInterest") or 0)
            mid = (bid + ask) / 2
            if bid <= 0 or ask <= 0 or (ask - bid) / mid > 0.15:
                bad.append("wide bid/ask gap (hard to sell)")
            if oi < 100:
                bad.append(f"thin options (only {oi} open contracts)")
    except Exception as e:
        bad.append(f"couldn't check liquidity ({e})")
    if not catalyst(t["ticker"], t["cp"]):
        bad.append("no fresh " + ("good" if t["cp"] == "C" else "bad") + " news in the last 3 days")
    return bad


def rule_warnings(t, n):
    w = []
    dte = (date.fromisoformat(t["expiry"]) - n.date()).days
    if not can_trade(n):
        w.append(window_msg(n))
    if n.weekday() == 4 and n.time() >= dtime(14, 0):
        w.append("Late Friday - you need to be flat by the close.")
    if dte < MIN_DTE:
        w.append(f"Only {dte} days to expiry (rule is {MIN_DTE}+).")
    px = t.get("price")
    if px and px < 0.50:
        w.append(f"Cheap option (${px:.2f}): fees eat about {200 * FEE_PER_CONTRACT / (px * 100):.0f}% round trip "
                 f"and one 1-cent tick moves it {1 / (px * 100) * 100:.0f}%. Stops will whipsaw.")
    if t["ticker"] not in WATCHLIST:
        bad = oneoff_problems(t)
        if bad:
            w.append(f"{t['ticker']} isn't SPY or NVDA and doesn't meet the conditions: " + "; ".join(bad) + ".")
    if t["ticker"] == "SPY" and t["cp"] == "C" and spy_hot(n)[0] and HOT_DTE[0] <= dte <= HOT_DTE[1]:
        w.append("\U0001F525 Matches your hot SPY setup. Plan to be out by Friday.")
    if t["cp"] == "P" and t["ticker"] in WATCHLIST and not catalyst(t["ticker"], "P"):
        w.append(f"Put without bad news: nothing bearish on {t['ticker']} in the last 3 days. "
                 "Puts fight the market's upward drift, so they need a reason.")
    if t["ticker"] == "NVDA" and t["cp"] == "C":
        try:
            s = nvda_state()
            if not s["zone"]:
                w.append("NVDA: don't buy, not ready. Send 'nvda' for why.")
        except Exception as e:
            w.append(f"(couldn't check NVDA zone: {e})")
    try:
        eb = earnings_before(t["ticker"], t["expiry"], n.date())
        if eb:
            ex = date.fromisoformat(t["expiry"])
            w.append(f"\U0001F4C5 {t['ticker']} reports earnings {eb.month}/{eb.day}, before your {ex.month}/{ex.day} "
                     "expiry - options often drop after earnings even when you're right.")
    except Exception:
        pass
    return w



# ---------------------------------------------------------------- deal watch (shared with newswatch.py)
import fcntl
from contextlib import contextmanager
WATCH_FILE = os.path.join(HERE, "watchlist.json")
WATCH_LOCK = os.path.join(HERE, "watchlist.lock")


@contextmanager
def watch_locked():
    with open(WATCH_LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(WATCH_FILE) as f:
                wl = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            wl = {}
        yield wl
        tmp = WATCH_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(wl, f, indent=2)
        os.replace(tmp, WATCH_FILE)


def cmd_watch(words):
    sub = words[0].lower() if words else "list"
    if sub == "add":
        if len(words) < 2:
            raise ValueError("which ticker? like: watch add BMBL")
        t = words[1].upper()
        note = " ".join(words[2:]) or "added by you"
        import yfinance as yf
        try:
            price = float(yf.Ticker(t).fast_info.last_price)
        except Exception:
            price = None
        with watch_locked() as wl:
            stamp = now().isoformat(timespec="seconds")
            wl[t] = {"added": stamp, "last_event": stamp, "status": "watching", "reason": note,
                     "price_added": price, "source": "manual", "events": [], "alerted": {}}
        reply(f"Watching {t}" + (f" from ${price:.2f}" if price else "") +
              ". You'll get pinged on unusual volume, heavy call buying, or deal news.")
        return
    if sub in ("drop", "remove", "rm"):
        if len(words) < 2:
            raise ValueError("which ticker? like: watch drop BMBL")
        t = words[1].upper()
        with watch_locked() as wl:
            found = wl.pop(t, None)
        reply(f"Dropped {t} from deal watch." if found else f"{t} wasn't on the watch list.")
        return
    with watch_locked() as wl:
        items = dict(wl)
    if not items:
        reply("Deal watch is empty. The news scanner adds tickers as it finds deal chatter, "
              "or use: watch add XYZ")
        return
    import yfinance as yf
    lines = ["Deal watch:"]
    for t, w in sorted(items.items(), key=lambda kv: kv[1]["added"]):
        days = (now() - datetime.fromisoformat(w["added"])).days
        chg = ""
        try:
            px = float(yf.Ticker(t).fast_info.last_price)
            if w.get("price_added"):
                chg = f" | ${px:.2f} ({px / w['price_added'] - 1:+.0%} since added)"
            else:
                chg = f" | ${px:.2f}"
        except Exception:
            pass
        flag = " [DEAL ANNOUNCED]" if w.get("status") == "announced" else ""
        lines.append(f"{t}{flag}: {w['reason']} | {days}d{chg}")
    reply("\n".join(lines))



def cmd_congress(words):
    path = os.path.join(HERE, "congress_log.csv")
    if not os.path.exists(path):
        reply("No Congress trades logged yet. They're checked every 2 hours.")
        return
    with open(path) as f:
        rows = list(csv.DictReader(f))
    if words:
        t = words[0].upper()
        rows = [r for r in rows if r["ticker"] == t or t.lower() in r["member"].lower()]
    rows = rows[-12:][::-1]
    if not rows:
        reply("Nothing logged for that.")
        return
    lines = ["Recent Congress buys (newest first):"]
    for r in rows:
        lines.append(f"{r['member']}: {r['ticker']} {r['amount']} traded {r['traded']} disclosed {r['disclosed']}")
    lines.append("Data: Bargo (bargo.ai)")
    reply("\n".join(lines))



def _outcomes():
    path = os.path.join(HERE, "news_outcomes.csv")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def cmd_news(words):
    rows = _outcomes()
    if words:
        t = words[0].upper()
        rows = [r for r in rows if r["ticker"] == t]
    rows = rows[-12:][::-1]
    if not rows:
        reply("No news logged yet" + (" for that ticker." if words else "."))
        return
    lines = ["Recent news (newest first):"]
    for r in rows:
        arrow = "+" if r["direction"] == "bullish" else ("-" if r["direction"] == "bearish" else " ")
        x3 = f" | 3d vs SPY {float(r['x3']):+.1%}" if r.get("x3") else ""
        lines.append(f"{arrow} {r['ticker']} {r['event'].replace('_', ' ')}: {r['title'][:80]}{x3}")
    reply("\n".join(lines))


def cmd_scoreboard(_):
    rows = [r for r in _outcomes() if r.get("x3")]
    if not rows:
        reply("No track record yet. Outcomes fill in after the close, 3 trading days after each event.")
        return
    stats = {}
    for r in rows:
        stats.setdefault(r["event"], []).append(float(r["x3"]))
    lines = ["Track record: avg 3-day move vs SPY after each news type", "(n = cases, % = beat SPY)"]
    for ev, xs in sorted(stats.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        avg = sum(xs) / len(xs)
        up = sum(1 for x in xs if x > 0) / len(xs)
        flag = "" if len(xs) >= 5 else " (thin)"
        lines.append(f"{ev.replace('_', ' ')}: {avg:+.1%} | {up:.0%} | n={len(xs)}{flag}")
    reply("\n".join(lines))



# ---------------------------------------------------------------- earnings + paper trades
_nvda_text_base = nvda_text


def nvda_text(s):
    try:
        return _nvda_text_base(s) + earnings_line("NVDA", now().date())
    except Exception:
        return _nvda_text_base(s)


def earnings_watch():
    today = now().date()
    for p in load_positions():
        try:
            d = next_earnings(p["ticker"], today)
        except Exception:
            continue
        if d and 0 <= (d - today).days <= 2 and d <= date.fromisoformat(p["expiry"]):
            when = "TODAY" if d == today else ("tomorrow" if (d - today).days == 1 else f"{d.month}/{d.day}")
            alert(f"{p['id']} x{p['qty']}: {p['ticker']} reports earnings {when}.\n"
                  "Options usually lose value right after earnings (IV crush), even if the stock moves your way. "
                  "Decide now: take it off, or hold on purpose.",
                  title=f"\U0001F4C5 Earnings {when}: {p['ticker']}", priority=PRI_ACTION, tags="calendar")


def cmd_paper(_):
    book_path, log_path = os.path.join(HERE, "paper.json"), os.path.join(HERE, "paper_log.csv")
    try:
        with open(book_path) as f:
            book = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        book = []
    rows = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            rows = list(csv.DictReader(f))
    lines = ["Paper trades of buy signals (1 contract each, your rules)"]
    if rows:
        pcts = [float(r["pct"]) for r in rows]
        usd = sum(float(r["usd"]) for r in rows)
        wins = sum(1 for x in pcts if x > 0)
        lines.append(f"Closed: {len(rows)} | won {wins / len(rows):.0%} | avg {sum(pcts) / len(pcts):+.0%} | "
                     f"total {'+' if usd >= 0 else '-'}${abs(usd):,.0f}")
        by = {}
        for r in rows:
            by.setdefault(r["signal"], []).append(float(r["usd"]))
        for sig, v in sorted(by.items(), key=lambda kv: -sum(kv[1])):
            lines.append(f"  {sig.replace('_', ' ')}: {len(v)} trades, {'+' if sum(v) >= 0 else '-'}${abs(sum(v)):,.0f}")
        for r in rows[-3:][::-1]:
            lines.append(f"  last: {r['ticker']} {float(r['pct']):+.0%} ({r['reason']})")
    else:
        lines.append("No closed paper trades yet.")
    if book:
        lines.append(f"Open: " + ", ".join(f"{p['ticker']} {p['strike']:g}C" for p in book))
    lines.append("Quotes are Yahoo mid prices, ~15 min delayed - real fills would be a bit worse.")
    reply("\n".join(lines))



# ---------------------------------------------------------------- Friday scorecard
RULES_LOG = os.path.join(HERE, "rules_log.csv")
BREAK_KEYS = {
    "hands-off": "traded on a hands-off day",
    "opens at 11": "traded before 11 AM (Mon/Tue)",
    "Put without bad news": "put with no bad news",
    "AVERAGING DOWN": "averaged down on a loser",
    "Outside your trading window": "outside trading window",
    "Weekend": "outside trading window",
    "Late Friday": "opened late Friday",
    "days to expiry": "too few days to expiry",
    "isn't SPY or NVDA": "one-off ticker",
    "not ready": "NVDA bought while extended",
    "held past stop": "held past the -20% stop",
    "Cheap option": "cheap (<$0.50) option",
}


def log_rules(contract, notes, kind="buy"):
    breaks = []
    for n in notes:
        for key, label in BREAK_KEYS.items():
            if key in n and label not in breaks:
                breaks.append(label)
    new = not os.path.exists(RULES_LOG)
    with open(RULES_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "contract", "kind", "breaks"])
        w.writerow([now().isoformat(timespec="seconds"), contract, kind, "; ".join(breaks)])


def scorecard(since=None, title="Scorecard"):
    trades = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            trades = list(csv.DictReader(f))
    rules = []
    if os.path.exists(RULES_LOG):
        with open(RULES_LOG) as f:
            rules = list(csv.DictReader(f))
    sells = [r for r in trades if r["action"] == "sell" and r["realized_pnl"]
             and (since is None or r["time"] >= since)]
    if not sells:
        held = load_positions()
        extra = f"\nStill open: {', '.join(p['id'] for p in held)}" if held else ""
        return f"{title}: no closed trades yet." + extra
    good, bad, reasons = [], [], {}
    for r in sells:
        pnl = float(r["realized_pnl"])
        br = set()
        for x in rules:
            if x["contract"] == r["contract"] and x["time"] <= r["time"] and x["breaks"]:
                # buys of this contract in the 10 days before the sale, and the sale itself
                if (datetime.fromisoformat(r["time"]) - datetime.fromisoformat(x["time"])).days <= 10:
                    br.update(b for b in x["breaks"].split("; ") if b)
        if br:
            bad.append(pnl)
            for b in br:
                reasons[b] = reasons.get(b, 0) + pnl
        else:
            good.append(pnl)

    def money(v):
        return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"

    total = sum(good) + sum(bad)
    wins = sum(1 for v in good + bad if v > 0)
    lines = [f"{title}: {len(sells)} closed, won {wins / len(sells):.0%}, {money(total)}",
             f"✅ Followed the rules: {len(good)} trades, {money(sum(good))}"
             + (f" (avg {money(sum(good) / len(good))})" if good else ""),
             f"❌ Broke a rule: {len(bad)} trades, {money(sum(bad))}"
             + (f" (avg {money(sum(bad) / len(bad))})" if bad else "")]
    for b, v in sorted(reasons.items(), key=lambda kv: kv[1]):
        lines.append(f"   - {b}: {money(v)}")
    held = load_positions()
    if held and now().weekday() == 4:
        lines.append("⚠️ Holding into the weekend: " + ", ".join(p["id"] for p in held)
                     + " (rule: flat by Friday)")
    return "\n".join(lines)


def week_start():
    n = now()
    monday = (n - timedelta(days=n.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.isoformat(timespec="seconds")


def friday_scorecard():
    body = scorecard(week_start(), "This week")
    body += "\n\n" + scorecard(None, "All time (since logging)")
    alert(body, title="\U0001F4CA Friday scorecard", priority=PRI_INFO, tags="bar_chart")


def cmd_scorecard(words):
    if words and words[0].lower() == "all":
        reply(scorecard(None, "All time (since logging)"))
    else:
        reply(scorecard(week_start(), "This week") + "\n(send 'scorecard all' for all time)")



# ---------------------------------------------------------------- account balance + sizing
ACCOUNT = os.path.join(HERE, "account.json")


def load_account():
    try:
        with open(ACCOUNT) as f:
            a = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        a = {}
    # trade pct v1
    cash, slots = a.get("cash_pct", 0.5), a.get("slots", 2)
    tp = a.get("trade_pct") or (1 - cash) / slots
    return {"balance": a.get("balance"), "cash_pct": cash, "slots": slots, "trade_pct": tp,
            "restricted_until": a.get("restricted_until")}


def save_account(a):
    tmp = ACCOUNT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(a, f)
    os.replace(tmp, ACCOUNT)


def auto_budget():
    a = load_account()
    if not a["balance"]:
        return None
    b = a["balance"] * a["trade_pct"]
    try:
        sc = settled_cash()
        if sc is not None:
            b = min(b, max(sc, 0))
    except Exception:
        pass
    return round(b, -1)


def bump_balance(pnl):
    a = load_account()
    if a["balance"] is not None:
        a["balance"] = round(a["balance"] + pnl, 2)
        save_account(a)


def sizing_line():
    a = load_account()
    b = auto_budget()
    if not b:
        return ""
    line = f"\nBudget per trade: ${b:,.0f} = {a['trade_pct']:.0%} of your ${a['balance']:,.0f} balance"
    if b < round(a["balance"] * a["trade_pct"], -1) - 5:
        line += " (cut down because that's all the settled cash you have right now)"
    return line + "\nChange it with: balance pct 25"


def cmd_balance(words):
    a = load_account()
    if words and words[0].lower() in ("pct", "percent", "trade"):
        if len(words) < 2:
            raise ValueError("like: balance pct 25   (25% of your account per trade)")
        v = float(words[1].replace("%", ""))
        v = v / 100 if v > 1 else v
        if not 0 < v <= 1:
            raise ValueError("percent should be between 1 and 100")
        a["trade_pct"] = v
    elif words and words[0].lower() in ("cash", "slots"):
        if len(words) < 2:
            raise ValueError("like: balance cash 50   or   balance slots 2")
        v = float(words[1].replace("%", ""))
        a.pop("trade_pct", None)
        if words[0].lower() == "cash":
            a["cash_pct"] = v / 100 if v > 1 else v
        else:
            a["slots"] = max(1, int(v))
    elif words:
        a["balance"] = float(words[0].replace("$", "").replace(",", ""))
    else:
        if not a["balance"]:
            reply("No balance set. Send: balance 4500")
            return
    save_account(a)
    reply(f"Balance ${a['balance']:,.0f}. It moves up and down with each sell you log "
          "(before fees) - send 'balance 4720' any time to match Fidelity." + sizing_line())


# ---------------------------------------------------------------- commands
HELP = """Commands:
buy SPY 750C 5/22 x5 @4.10
sell SPY 750C @4.80        (all, or add x2 for part)
list    - open positions + P/L
nvda    - NVDA buy / don't buy
spy     - hot buy check + best contracts
pick AAPL - the specific call to buy (strike, date, size, stop)
spy 2500 - same, sized to your budget (plain 'spy' uses your balance)
balance 4500 - set balance; 'balance pct 25' = 25% of account per trade
rules   - the rulebook
cash    - settled cash you can use now and what settles when
stats   - profit/loss, win rate, profit factor, worst streak and drop
scorecard - this week: rule-following vs rule-breaking P&L
news    - recent news hits (news NVDA)
dip     - preview the 'market is down, buy SPY?' alert
restricted from 10/23 - set the 90-day settled-cash-only restriction (also: until 1/21, off)
hold SPY - mark a SPY position as a 3-day dip hold (skips Friday get-flat)
swing LLY - hold a position to the next close: -30% stop, no +20% prompt (not on Fridays)
sameday LLY - undo a swing, back to the normal same-day rules
buy LLY / skip LLY - say what you did with an alert (plain 'buy' or 'skip' = the latest one); 'decisions' shows how each did
check   - pre-trade check, logs nothing: check LLY 1 (pick #1 or #2; add put, or x3), or paste a Buy to Open order
signal LLY - is the 🔥/🔻 signal still strong, fading or gone
scoreboard - which news types actually moved stocks
watch   - deal watch list (watch add XYZ / watch drop XYZ)
congress - recent Congress buys (congress NVDA / congress pelosi)
drop SPY 750C   - remove a mistaken entry (no P/L logged)"""

RULES = f"""Rulebook
1. Mon after 11 AM. Tue-Fri from 9 AM. Flat by Friday close.
2. NVDA calls only in the pullback zone.
3. SPY entries as usual.
4. {MIN_DTE}+ days to expiry. Out at {STOP:.0%}.
5. Any ticker is fine if the stock is $5+ and heavily traded, its options are liquid, and there is fresh news your way. Otherwise you get a warning.
6. Puts are single long puts only (same stop, same size) and need fresh bad news. 'pick TICKER put' shows them."""


def find_position(pos, t):
    hits = [p for p in pos if p["ticker"] == t["ticker"] and p["strike"] == t["strike"]
            and p["cp"] == t["cp"] and (t["expiry"] is None or p["expiry"] == t["expiry"].isoformat())]
    return hits


def cmd_buy(words):
    if not words or all(re.fullmatch(r"[A-Za-z.]{1,6}", w) for w in words):
        return cmd_buy_decision(words)
    n = now()
    t = parse_trade(words, n.date())
    for k, label in [("expiry", "expiry like 5/22"), ("qty", "quantity like x5"), ("price", "price like @4.10")]:
        if t[k] is None:
            raise ValueError(f"missing {label}")
    t["expiry"] = t["expiry"].isoformat()
    exp = date.fromisoformat(t["expiry"])
    pid = pid_of(t["ticker"], t["strike"], t["cp"], exp)
    warnings = rule_warnings(t, n)
    warnings += cash_warning(t)
    _rt = restriction_text()
    if _rt:
        warnings.append(_rt)
    with lock:
        pos = load_positions()
        existing = [p for p in pos if p["id"] == pid]
        if existing:
            p = existing[0]
            if t["price"] < p["entry"]:
                warnings.append(f"AVERAGING DOWN: you are buying more of a loser (avg {p['entry']:.2f}, now {t['price']:.2f}). "
                                "This is the hold-and-hope pattern. If you did not mean to, sell the add now.")
            total = p["qty"] + t["qty"]
            p["entry"] = round((p["entry"] * p["qty"] + t["price"] * t["qty"]) / total, 4)
            p["qty"] = total
            p["stop_last"], p["targets_hit"] = None, []
            note = f"Added to {pid}: now x{total} @ avg {p['entry']:.2f}"
        else:
            p = {"id": pid, "ticker": t["ticker"], "strike": t["strike"], "cp": t["cp"],
                 "expiry": t["expiry"], "qty": t["qty"], "entry": t["price"],
                 "opened": n.isoformat(timespec="seconds"),
                 "stop_last": None, "targets_hit": [], "expiry_warned": None}
            pos.append(p)
            note = f"Logged: {pid} x{t['qty']} @ {t['price']:.2f}"
            if t["ticker"] == "SPY" and t["cp"] == "C" and _dip_alert_today(n):
                p["hold_until"] = _add_trading_days(n.date(), 3).isoformat()
                _hu = date.fromisoformat(p["hold_until"])
                note += (f"\nDip trade: hold until at least {_hu.strftime('%a')} {_hu.month}/{_hu.day}. "
                         "The Friday get-flat reminder is skipped until then.")
            elif SWING_DEFAULT and n.weekday() != 4 and _latest_signal(t["ticker"]):
                _auto_swing(p, n)
                _hu = date.fromisoformat(p["hold_until"])
                note += (f"\nSWING (default for a bot signal): holding to the close of {_hu.strftime('%a')} {_hu.month}/{_hu.day}. "
                         "No +20% prompt. Tap Same day if you want the normal same-day rules instead.")
        save_positions(pos)
        log_trade("buy", pid, t["qty"], t["price"])
        log_rules(pid, warnings)
    _stop = p.get("stop", STOP)
    _tg = (p.get("targets") or [0.20])[0] if p.get("swing") else 0.20
    msg = (f"{note}\nStop alert at {level_price(p['entry'], _stop):.2f} ({_stop:.0%} after fees)"
           f" | +{_tg:.0%} at {level_price(p['entry'], _tg):.2f}")
    if warnings:
        msg += "\n\nRULE CHECK:\n- " + "\n- ".join(warnings)
    if any("hot SPY" in w for w in warnings) and len(warnings) == 1:
        msg = msg.replace("RULE CHECK:", "SETUP:")
    _btn = _sameday_actions(t["ticker"]) if p.get("swing") else _swing_actions(t["ticker"])
    if t["ticker"] and datetime.now(TZ).weekday() != 4 and _btn:
        send(CMD_TOPIC, msg, title=BOT_TITLE, priority=3, actions=_btn)  # swing / same-day button
    else:
        reply(msg)


def cmd_sell(words):
    t = parse_trade(words, now().date())
    if t["price"] is None:
        raise ValueError("missing price like @4.80")
    with lock:
        pos = load_positions()
        hits = find_position(pos, t)
        if not hits:
            raise ValueError("no open position matches that")
        if len(hits) > 1:
            raise ValueError("more than one expiry matches - add the date, like 5/22")
        p = hits[0]
        qty = t["qty"] or p["qty"]
        if qty > p["qty"]:
            raise ValueError(f"you only hold x{p['qty']}")
        pct, pnl = net_pct_usd(p["entry"], t["price"], qty)
        p["qty"] -= qty
        if p["qty"] == 0:
            pos.remove(p)
        save_positions(pos)
        log_trade("sell", p["id"], qty, t["price"], round(pnl, 2))
        bump_balance(pnl)
        add_unsettled(t["price"] * qty * 100)
        log_rules(p["id"], [f"held past stop: closed at {pct:.0%}"] if pct <= -0.25 else [], kind="sell")
    left = f" ({p['qty']} left)" if p["qty"] else ""
    reply(f"Closed {p['id']} x{qty} @ {t['price']:.2f}{left}\n{'+' if pnl >= 0 else '-'}${abs(pnl):,.0f} ({pct:+.0%}) after fees"
          + sizing_line() + settle_note(t["price"] * qty * 100))


def cmd_drop(words):
    t = parse_trade(words, now().date())
    with lock:
        pos = load_positions()
        hits = find_position(pos, t)
        if len(hits) != 1:
            raise ValueError("need exactly one match - add the date if you hold two expiries")
        pos.remove(hits[0])
        save_positions(pos)
        log_trade("drop", hits[0]["id"], hits[0]["qty"], "")
    reply(f"Dropped {hits[0]['id']} (no P/L logged)")


def position_lines(pos, cache):
    lines = []
    for p in pos:
        try:
            mid = option_mid(p["ticker"], p["expiry"], p["strike"], p["cp"], cache)
        except Exception:
            mid = None
        dte = (date.fromisoformat(p["expiry"]) - now().date()).days
        if mid:
            pct, usd = net_pct_usd(p["entry"], mid, p["qty"])
            lines.append(f"{p['id']} x{p['qty']} @ {p['entry']:.2f} -> {mid:.2f} bid "
                         f"{pct:+.0%} ({'+' if usd >= 0 else '-'}${abs(usd):,.0f}) | {dte}d left")
        else:
            lines.append(f"{p['id']} x{p['qty']} @ {p['entry']:.2f} -> no quote | {dte}d left")
    return lines


def cmd_list(_):
    pos = load_positions()
    if not pos:
        reply("No open positions.")
        return
    reply("Open positions (sell price = bid):\n" + "\n".join(position_lines(pos, {})))


def cmd_nvda(_):
    reply(nvda_text(nvda_state()) + sizing_line())


def cmd_spy(words):
    budget = auto_budget()
    if words:
        try:
            budget = float(words[0].replace("$", "").replace(",", "").lower().replace("k", "")) * \
                (1000 if words[0].lower().endswith("k") else 1)
        except ValueError:
            raise ValueError("budget should be a number, like: spy 2500")
    reply(spy_text(budget=budget))


def _history(rows, ev):
    xs = [float(r["x3"]) for r in rows if r["event"] == ev and r.get("x3")]
    if len(xs) < 3:
        return "no track record yet"
    avg = sum(xs) / len(xs)
    up = sum(1 for x in xs if x > 0) / len(xs)
    return f"past cases: {avg:+.1%} vs SPY after 3d, {up:.0%} beat it (n={len(xs)})"


def _nd(r):  # news dates v1
    try:
        d = datetime.strptime(r["date"][:10], "%Y-%m-%d")
        return f"{d:%b} {d.day}"
    except Exception:
        return "?"


def cmd_news_verdicts(words):
    rows = _outcomes()
    if not rows:
        reply("No news logged yet.")
        return
    if words:
        t = words[0].upper()
        mine = [r for r in rows if r["ticker"] == t][-6:][::-1]
        if not mine:
            reply(f"No news logged for {t}.")
            return
        lines = [f"{t} news (newest first):"]
        for r in mine:
            mark = {"bullish": "GOOD", "bearish": "BAD"}.get(r["direction"], "neutral")
            lines.append(f"{_nd(r)} - {mark}: {r['event'].replace('_', ' ')} - {r['title'][:80]}")
            lines.append(f"   {_history(rows, r['event'])}")
        reply("\n".join(lines))
        return
    cutoff = (now() - timedelta(days=3)).date().isoformat()
    recent = [r for r in rows if r["date"] >= cutoff] or rows[-12:]
    try:
        with open(os.path.join(HERE, "buy_signals.json")) as f:
            sigs = [x for x in json.load(f) if x["time"][:10] >= cutoff]
            fired = {x["ticker"] for x in sigs if x.get("side", "C") == "C"}
            fired_p = {x["ticker"] for x in sigs if x.get("side") == "P"}
    except (FileNotFoundError, json.JSONDecodeError):
        fired, fired_p = set(), set()
    by = {}
    for r in recent:
        by.setdefault(r["ticker"], []).append(r)
    out = []
    for t, rs in by.items():
        score = 0
        for r in rs:
            st = int(float(r.get("strength") or 1))
            score += st if r["direction"] == "bullish" else (-st if r["direction"] == "bearish" else 0)
        has_up = any(r["direction"] == "bullish" for r in rs)
        has_dn = any(r["direction"] == "bearish" for r in rs)
        if has_up and has_dn and abs(score) <= 1:
            verdict, icon = "MIXED", "\U0001F7E1"
        elif t in fired and score > 0:
            verdict, icon = "BUY SIGNAL", "\U0001F525"
        elif score > 0:
            verdict, icon = "GOOD", "\U0001F7E2"
        elif t in fired_p and score < 0:
            verdict, icon = "PUT SIGNAL", "\U0001F53B"
        elif score < 0:
            verdict, icon = "BAD", "\U0001F534"
        else:
            verdict, icon = "NEUTRAL", "\u26AA"
        top = max(rs, key=lambda r: (abs(int(float(r.get("strength") or 1))), r["time"]))
        out.append((abs(score), t, verdict, icon, top, len(rs)))
    out.sort(key=lambda x: -x[0])
    good = sum(1 for o in out if o[2] in ("GOOD", "BUY SIGNAL"))
    bad = sum(1 for o in out if o[2] in ("BAD", "PUT SIGNAL"))
    lines = [f"Last 3 days: {good} good, {bad} bad, {len(out) - good - bad} mixed/neutral"]  # no legend v1
    hot = [o for o in out if o[2] in ("BUY SIGNAL", "PUT SIGNAL")]
    lines.append("Buy/put signals: " + (", ".join(f"{o[3]} {o[1]}" for o in hot) if hot else "none"))
    for _, t, verdict, icon, top, n in out[:8]:
        more = f" (+{n - 1} more)" if n > 1 else ""
        lines.append(f"{icon} {t} - {verdict}: {top['event'].replace('_', ' ')}{more} ({_nd(top)})")
        lines.append(f"   {top['title'][:80]}")
        lines.append(f"   {_history(rows, top['event'])}")
    lines.append("'news NVDA' = one stock.")
    reply("\n".join(lines))


def cmd_pick(words):
    if not words:
        raise ValueError("like: pick AAPL   or   pick AAPL 800")
    t = words[0].upper()
    budget, cp = None, "C"
    for w in words[1:]:
        if w.lower() in ("put", "puts", "p"):
            cp = "P"
        elif w.lower() in ("call", "calls", "c"):
            cp = "C"
        else:
            budget = float(w.replace("$", "").replace(",", ""))
    txt = contract_text(t, budget, cp=cp)
    notes = []
    if t not in WATCHLIST or cp == "P":
        c = catalyst(t, cp)
        kind = "bad" if cp == "P" else "good"
        notes.append(f"\nNews check: {c} in the last 3 days." if c else
                     f"\nNo fresh {kind} news on {t} in the last 3 days - this is a no-catalyst trade.")
    try:
        eb = next_earnings(t, now().date())
        if eb and 0 <= (eb - now().date()).days <= 21:
            notes.append(f"\nEarnings {eb.month}/{eb.day} - options get repriced after.")
            _el = earnings_followthrough_line(t)
            if _el:
                notes.append("\n" + _el)
    except Exception:
        pass
    if not can_trade(now()):
        notes.append("\n" + window_msg(now()))
    reply(txt.lstrip("\n") + "".join(notes) + "\nTo log: buy " + t + " <strike>" + cp + " <date> x<qty> @price")


def cmd_stats(_):
    rows = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            rows = [r for r in csv.DictReader(f) if r["action"] == "sell" and r["realized_pnl"]]
    if not rows:
        reply("No closed trades logged yet. Stats appear after your first 'sell'.")
        return
    rows.sort(key=lambda r: r["time"])
    pnl = [float(r["realized_pnl"]) for r in rows]
    n = len(pnl)
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    total = sum(pnl)
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = sum(losses) / len(losses) if losses else 0
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
    cum = peak = dd = 0.0
    streak = worst = 0
    for x in pnl:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)

    def m(v):
        return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"

    lines = [f"Stats: {n} closed trades since {rows[0]['time'][:10]}",
             f"Profit/loss: {m(total)} | avg per trade {m(total / n)}",
             f"Win rate {len(wins) / n:.0%} | avg win {m(avg_w)} | avg loss {m(avg_l)}"]
    if pf is not None:
        lines.append(f"Profit factor {pf:.2f} (over 1.0 = winners outweigh losers)")
    lines.append(f"Best {m(max(pnl))} | worst {m(min(pnl))}")
    lines.append(f"Longest losing streak {worst} | biggest drop from a high {m(dd)}")
    a = load_account() if "load_account" in globals() else {}
    if a.get("balance"):
        lines.append(f"Balance ${a['balance']:,.0f}")
    if n < 30:
        lines.append(f"Too early to judge: {n} of 30+ trades. Small samples swing wildly.")
    elif total > 0 and (pf or 0) >= 1.2:
        lines.append("After 30+ trades: positive with winners outweighing losers.")
    elif total > 0:
        lines.append("After 30+ trades: positive but thin. Keep going.")
    else:
        lines.append("After 30+ trades: negative. Check 'scorecard all' - are you following the rules?")
    reply("\n".join(lines))


# ---------------------------------------------------------------- settled cash (cash account)
UNSETTLED = os.path.join(HERE, "unsettled.json")


def _load_unsettled():
    try:
        with open(UNSETTLED) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def settle_date(d):
    """Options sales settle the next business day."""
    d = d + timedelta(days=1)
    while not market_day(d)["open"]:
        d += timedelta(days=1)
    return d


def add_unsettled(amount):
    today = now().date()
    items = [i for i in _load_unsettled() if i["settles"] > today.isoformat()]
    items.append({"amount": round(amount, 2), "settles": settle_date(today).isoformat()})
    with open(UNSETTLED, "w") as f:
        json.dump(items, f)


def settle_note(amount):
    d = settle_date(now().date())
    return f"\nProceeds ${amount:,.0f} settle {d.strftime('%a')} {d.month}/{d.day}."


def cash_status():
    a = load_account()
    if not a["balance"]:
        return None
    today = now().date().isoformat()
    unsettled = [i for i in _load_unsettled() if i["settles"] > today]
    invested = sum(p["entry"] * p["qty"] * 100 for p in load_positions())
    uns_total = sum(i["amount"] for i in unsettled)
    return {"balance": a["balance"], "invested": invested, "unsettled": unsettled,
            "unsettled_total": uns_total, "settled": a["balance"] - invested - uns_total}


def settled_cash():
    st = cash_status()
    return None if st is None else st["settled"]


def cash_warning(t):
    st = cash_status()
    if st is None or not st["unsettled"]:
        return []
    cost = t["qty"] * t["price"] * 100
    if cost <= max(st["settled"], 0):
        return []
    d = date.fromisoformat(min(i["settles"] for i in st["unsettled"]))
    return [f"Cash check: this costs ${cost:,.0f} but only ${max(st['settled'], 0):,.0f} is settled. "
            f"${st['unsettled_total']:,.0f} settles {d.strftime('%a')} {d.month}/{d.day}. Selling this before "
            "that money settles can be a good faith violation."]


def cmd_cash(_):
    st = cash_status()
    if st is None:
        reply("No balance set. Send: balance 4500")
        return
    lines = [f"Balance ${st['balance']:,.0f} | in open positions ${st['invested']:,.0f}",
             f"Settled cash you can use now: ${max(st['settled'], 0):,.0f}"]
    for i in sorted(st["unsettled"], key=lambda x: x["settles"]):
        d = date.fromisoformat(i["settles"])
        lines.append(f"  ${i['amount']:,.0f} from a sale settles {d.strftime('%a')} {d.month}/{d.day}")
    if not st["unsettled"]:
        lines.append("Everything is settled.")
    _rt = restriction_text()
    if _rt:
        lines.append(_rt)
    reply("\n".join(lines))


COMMANDS = {"buy": cmd_buy, "sell": cmd_sell, "drop": cmd_drop, "list": cmd_list,
            "status": cmd_list, "cash": cmd_cash, "stats": cmd_stats, "pick": cmd_pick, "live": lambda w: cmd_live(w), "signal": lambda w: cmd_signal(w), "restricted": lambda w: cmd_restricted(w), "hold": lambda w: cmd_hold(w), "swing": lambda w: cmd_swing(w), "sameday": lambda w: cmd_sameday(w), "skip": lambda w: cmd_skip(w), "decisions": lambda w: cmd_decisions(w), "dip": lambda w: cmd_dip(w), "balance": cmd_balance, "scorecard": cmd_scorecard, "scoreboard": cmd_scoreboard, "news": cmd_news_verdicts, "nvda": cmd_nvda, "spy": cmd_spy, "watch": cmd_watch, "congress": cmd_congress,
            "rules": lambda _: reply(RULES), "help": lambda _: reply(HELP)}


FID_RE = re.compile(
    r"(buy|sell)\s+to\s+(open|close)\D*?(\d+)\s+contracts?\s+([A-Za-z.]+)\s+([A-Za-z]{3})[a-z]*\s+(\d{1,2})\s+(\d{4})"
    r"\s+(\d[\d,]*(?:\.\d+)?)\s+(calls?|puts?)", re.I)  # fid comma v1
FID_PRICE = re.compile(r"(?:limit\s+at|filled\s+at|fill\s+at|@)\s*\$?(\d*\.?\d+)", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_fidelity(text):
    """Turn a pasted Fidelity order into (command, words), or None if it is not one."""
    m = FID_RE.search(text)
    if not m:
        if re.search(r"(buy|sell)\s+to\s+(open|close)", text, re.I):
            raise ValueError("I couldn't read that order. Paste the whole line, like: "
                             "Sell to Close 62 Contracts SOFI Oct 23 2026 16 Calls Limit at $0.15")
        return None
    side, oc, qty, tk, mon, day, _yr, strike, cp = m.groups()
    side, oc = side.lower(), oc.lower()
    if (side, oc) not in (("buy", "open"), ("sell", "close")):
        raise ValueError("The bot only tracks long options (Buy to Open, Sell to Close).")
    prices = FID_PRICE.findall(text)
    if not prices:
        raise ValueError("No price in that order (market order?). Add what it filled at, like: @0.15 at the end.")
    mo = MONTHS.get(mon.lower())
    if not mo:
        raise ValueError(f"Didn't understand the month '{mon}'.")
    words = [tk.upper(), f"{float(strike.replace(',', '')):g}{cp[0].upper()}", f"{mo}/{int(day)}", f"x{int(qty)}", f"@{prices[-1]}"]
    return side, words


def handle(text):
    if re.match(r"\s*check\b", text, re.I):
        try:
            cmd_check(text)
        except ValueError as e:
            reply(f"Couldn't check that: {e}")
        except Exception as e:
            traceback.print_exc()
            reply(f"Check failed: {e}")
        return
    try:
        fid = parse_fidelity(text)
    except ValueError as e:
        reply(f"Couldn't do that: {e}")
        return
    if fid:
        text = fid[0] + " " + " ".join(fid[1])
    words = text.replace(",", " ").split()
    if not words:
        return
    fn = COMMANDS.get(words[0].lower())
    if not fn:
        reply(f"Didn't recognize '{words[0]}'.\n\n{HELP}")
        return
    try:
        fn(words[1:])
    except ValueError as e:
        reply(f"Couldn't do that: {e}\n\nExample: buy SPY 750C 5/22 x5 @4.10")
    except Exception as e:
        traceback.print_exc()
        reply(f"Error: {e}")


# ---------------------------------------------------------------- listener
def listen():
    while True:
        try:
            with requests.get(f"{NTFY}/{CMD_TOPIC}/json", stream=True, timeout=(10, 90)) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    m = json.loads(line)
                    if m.get("event") != "message" or m.get("title") == BOT_TITLE:
                        continue
                    print(f"[cmd] {m.get('message')}")
                    handle(m.get("message", ""))
        except Exception as e:
            print(f"[listen] reconnecting: {e}")
            time.sleep(5)


# ---------------------------------------------------------------- scheduled checks
def market_open(n):
    return is_open_now(n)


def check_positions():
    n = now()
    cache = {}
    with lock:
        pos = load_positions()
        changed = False
        for p in pos:
            try:
                mid = option_mid(p["ticker"], p["expiry"], p["strike"], p["cp"], cache)
            except Exception as e:
                print(f"[quote] {p['id']}: {e}")
                continue
            if not mid:
                continue
            globals()["LAST_QUOTE_OK"] = time.time()
            pct, usd = net_pct_usd(p["entry"], mid, p["qty"])
            stop = p.get("stop", STOP)            # swing v1: swing trades use a wider stop
            targets = p.get("targets", TARGETS)
            swing = bool(p.get("swing"))
            soft_dn = abs(stop) / 2
            if pct <= stop:
                last = datetime.fromisoformat(p["stop_last"]) if p["stop_last"] else None
                if not last or n - last >= timedelta(minutes=STOP_REPEAT_MIN):
                    again = " (still)" if last else ""
                    alert(f"{p['id']} is {pct:+.0%}{again}  (${abs(usd):,.0f} down)\n"
                          f"{p['entry']:.2f} -> {mid:.2f} (bid). Rule says sell now.",
                          title=f"SELL: {p['ticker']} hit {stop:.0%}", priority=PRI_URGENT,
                          tags="rotating_light")
                    p["stop_last"] = n.isoformat(timespec="seconds")
                    changed = True
            elif p["stop_last"] and pct > stop:
                p["stop_last"] = None
                changed = True
            soft = p.setdefault("soft", [])
            if pct >= SOFT and "up" not in soft:
                if pct < targets[0] and not swing:
                    alert(f"{p['id']} is {pct:+.0%} (+${usd:,.0f})\n{p['entry']:.2f} -> {mid:.2f} (bid). "
                          "Maybe time to sell, or at least decide where you'd get out.",
                          title=f"{p['ticker']} up {pct:.0%}", priority=PRI_ACTION, tags="chart_with_upwards_trend")
                soft.append("up")
                changed = True
            elif pct < SOFT / 2 and "up" in soft:
                soft.remove("up")
                changed = True
            if pct <= -soft_dn and "down" not in soft:
                if pct > stop:
                    alert(f"{p['id']} is {pct:+.0%} (-${abs(usd):,.0f})\n{p['entry']:.2f} -> {mid:.2f} (bid). "
                          "Halfway to your stop. Maybe time to sell. Hold only if you would buy it again today.",
                          title=f"{p['ticker']} down {abs(pct):.0%}", priority=PRI_ACTION, tags="warning")
                soft.append("down")
                changed = True
            elif pct > -soft_dn / 2 and "down" in soft:
                soft.remove("down")
                changed = True
            new_hits = [tgt for tgt in targets if pct >= tgt and tgt not in p["targets_hit"]]
            if new_hits:
                top = max(new_hits)
                alert(f"{p['id']} is {pct:+.0%} (+${usd:,.0f})\n{p['entry']:.2f} -> {mid:.2f} (bid). " +
                      ("Second target hit. Take it, or decide your exit now." if top >= 0.4 else
                       "First target hit. Take it, or decide your exit now."),
                      title=f"Profit: {p['ticker']} +{top:.0%}", priority=PRI_ACTION,
                      tags="moneybag")
                p["targets_hit"].extend(new_hits)
                changed = True
        if changed:
            save_positions(pos)


def live_watch():
    import alpaca_live
    alpaca_live.watch(load_positions(), alert, now())


def cmd_live(words):
    import alpaca_live
    pos = load_positions()
    extra = [w.upper() for w in (words or []) if w.isalpha()]
    syms = sorted({p["ticker"] for p in pos} | set(extra or ["SPY"]))
    thr = 0.0015

    def arrow(x):
        return "?" if x is None else ("\u25B2" if x > thr else "\u25BC" if x < -thr else "\u25AC")

    def pct(x):
        return "n/a" if x is None else f"{x:+.1%}"

    try:
        tr = alpaca_live.trend(syms)
    except Exception as e:
        print(f"[live] trend: {e}")
        tr = {}
    if tr:
        lines = ["Source: Alpaca (live)"]
        for s in syms:
            d = tr.get(s)
            if not d:
                lines.append(f"{s}: no data")
                continue
            lines.append(f"{s} ${d['price']:.2f}  {arrow(d['day'])} {pct(d['day'])} vs yesterday's close")
            lines.append(f"   15 min {arrow(d['m15'])} {pct(d['m15'])} | 1 hr {arrow(d['m60'])} {pct(d['m60'])}"
                         f" | since open {arrow(d['open'])} {pct(d['open'])}")
            m15, m60 = d["m15"], d["m60"]
            if m15 is not None and m60 is not None and m15 > thr and m60 > thr:
                lines.append("   Trend: UP")
            elif m15 is not None and m60 is not None and m15 < -thr and m60 < -thr:
                lines.append("   Trend: DOWN")
            elif m60 is None:
                lines.append("   Trend: not enough recent data")
            else:
                lines.append("   Trend: choppy / flat")
        reply("\n".join(lines))
        return
    px = alpaca_live.prices(syms)
    if not px:
        reply("Couldn't get any prices (Alpaca and Yahoo both failed).")
        return
    lines = [f"Source: {alpaca_live.LAST_SOURCE['name']} (no trend data)"]
    lines += [f"{k} ${v:.2f}" for k, v in px.items()]
    reply("\n".join(lines))


# signal cmd v1
def cmd_signal(words):
    import alpaca_live
    pos = load_positions()
    tickers = [w.upper() for w in (words or []) if w.isalpha()] or sorted({p["ticker"] for p in pos})
    if not tickers:
        reply("Usage: signal LLY   (plain 'signal' checks the tickers you hold)")
        return
    try:
        with open(os.path.join(HERE, "buy_signals.json")) as f:
            sigs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sigs = []
    rows = _outcomes()
    try:
        tr = alpaca_live.trend(tickers)
    except Exception as e:
        print(f"[signal] trend: {e}")
        tr = {}
    thr = 0.0015
    is_open = market_open(now())
    out = []
    for t in tickers:
        mine = [x for x in sigs if x.get("ticker") == t]
        if not mine:
            out.append(f"{t}: no signal logged. Try 'news {t}' or 'live {t}'.")
            continue
        sg = mine[-1]
        side = sg.get("side", "C")
        sign = 1 if side == "C" else -1
        word = "CALL" if side == "C" else "PUT"
        when = datetime.fromisoformat(sg["time"])
        if when.tzinfo is None:
            when = when.replace(tzinfo=now().tzinfo)
        mins = max(0, int((now() - when).total_seconds() // 60))
        age = f"{mins} min" if mins < 90 else (f"{mins // 60} hr" if mins < 2880 else f"{mins // 1440} days")
        ev = str(sg.get("event", "")).replace("_", " ")
        lines = [f"{t} {word} signal fired {age} ago ({ev})"]
        sd = when.date().isoformat()
        fav = against = 0
        for r in rows:
            if r["ticker"] == t and r["date"] >= sd:
                st = int(float(r.get("strength") or 1))
                if r["direction"] == ("bullish" if side == "C" else "bearish"):
                    fav += st
                elif r["direction"] in ("bullish", "bearish"):
                    against += st
        d = tr.get(t)
        if not d:
            lines.append("   Couldn't get a live price right now.")
            out.append("\n".join(lines))
            continue
        day = None if d["day"] is None else d["day"] * sign
        m15 = 0.0 if d["m15"] is None else d["m15"] * sign
        m60 = 0.0 if d["m60"] is None else d["m60"] * sign
        lines.append(f"   ${d['price']:.2f}, {'' if day is None else f'{day:+.1%}'} on the day in the signal's direction")
        lines.append(f"   last hour {m60:+.1%} | last 15 min {m15:+.1%}")
        lines.append(f"   news since: {fav} for, {against} against")
        if day is None:
            verdict = "UNKNOWN"
        elif day >= 0.01 and m60 >= 0 and m15 >= -thr and against <= fav:
            verdict = "STRONG"
        elif day <= 0 or (against > fav and day < 0.01) or m60 < -0.005 and day < 0.01:
            verdict = "GONE"
        else:
            verdict = "FADING"
        icon = {"STRONG": "\U0001F525", "FADING": "\U0001F7E1", "GONE": "\U0001F6D1"}.get(verdict, "?")
        lines.append(f"   {icon} {verdict}" + ("" if is_open else " (market closed - last session's picture)"))
        held = [q for q in pos if q["ticker"] == t]
        if held:
            lines.append(f"   You hold {held[0]['id']} - 'list' for P/L. Exits are still your targets and stop, not this.")
        out.append("\n".join(lines))
    reply("\n\n".join(out))


# pretrade check v1
def _check_parse(text):
    body = re.sub(r"^\s*check\b", "", text, flags=re.I).strip()
    # check shortcut v1
    toks = body.replace(",", " ").split()
    if not toks:
        raise ValueError("like: check LLY 1   (1 or 2 = the numbered pick; add put, or x3 for quantity)")
    if len(toks) >= 2 and toks[0].replace(".", "").isalpha() and toks[1] in ("1", "2"):
        from market_extras import contract_options
        tk, k, cp, qty = toks[0].upper(), int(toks[1]) - 1, "C", None
        for w in toks[2:]:
            wl = w.lower()
            if wl in ("put", "puts", "p"):
                cp = "P"
            elif wl in ("call", "calls", "c"):
                cp = "C"
            elif re.fullmatch(r"x?\d+", wl):
                qty = int(wl.lstrip("x"))
            else:
                raise ValueError(f"didn't understand '{w}'")
        spot, exp, rows = contract_options(tk, now().date(), cp=cp)
        if k >= len(rows):
            raise ValueError(f"no pick #{k + 1} for {tk} right now (the bot found {len(rows)} liquid {'puts' if cp == 'P' else 'calls'}).")
        r = rows[k]
        price = round(r["mid"], 2)
        if qty is None:
            a = load_account()
            bal = a.get("balance") if a else None
            qty = max(1, int(bal * a["trade_pct"] // (price * 100))) if bal else 1
        return {"ticker": tk, "strike": float(r["strike"]), "cp": cp, "expiry": exp, "qty": qty, "price": price}
    m = FID_RE.search(body)
    if m:
        side, oc, qty, tk, mon, day, yr, strike, cp = m.groups()
        if (side.lower(), oc.lower()) != ("buy", "open"):
            raise ValueError("check is for Buy to Open orders, before you buy.")
        mo = MONTHS.get(mon.lower())
        if not mo:
            raise ValueError(f"didn't understand the month '{mon}'")
        px = FID_PRICE.findall(body)
        return {"ticker": tk.upper(), "strike": float(strike.replace(",", "")), "cp": cp[0].upper(),
                "expiry": date(int(yr), mo, int(day)).isoformat(), "qty": int(qty),
                "price": float(px[-1]) if px else None}
    t = parse_trade(body.replace(",", "").split(), now().date())
    t["qty"] = t["qty"] or 1
    if t["expiry"] is not None and not isinstance(t["expiry"], str):
        t["expiry"] = t["expiry"].isoformat()
    return t


def check_text(text):
    t = _check_parse(text)
    if not t["price"]:
        raise ValueError("no price. Paste the order with its limit, or add @61.64")
    if not t["expiry"] or not t["cp"]:
        raise ValueError("need the full contract, like: check ASML 1680C 11/6 x1 @61.64")
    n = now()
    tk, cp, qty = t["ticker"], t["cp"], t["qty"]
    sign = 1 if cp == "C" else -1
    cost = qty * t["price"] * 100 + FEE_PER_CONTRACT * qty
    flags = []  # (2 = stop, 1 = caution, text)

    a = load_account()
    bal = a.get("balance") if a else None
    if bal:
        budget = bal * a["trade_pct"]
        fits = int(budget // (t["price"] * 100))
        buys = f"Your limit buys {fits} at this price." if fits else "Even 1 contract is over your limit."
        if cost > budget * 1.25:
            flags.append((2, f"SIZE: ${cost:,.0f} is {cost / bal:.0%} of your ${bal:,.0f} account. Your {a['trade_pct']:.0%} limit is about ${budget:,.0f}. {buys}"))
        elif cost > budget:
            flags.append((1, f"SIZE: ${cost:,.0f} is a bit over your ${budget:,.0f} limit ({cost / bal:.0%} of account). {buys}"))
    try:
        for w in cash_warning(t):
            flags.append((2, w.replace("Cash check: ", "CASH: ")))
    except Exception as e:
        print(f"[check] cash: {e}")

    rule_lines = []
    try:
        rule_lines = rule_warnings(t, n)
    except Exception as e:
        print(f"[check] rules: {e}")
    try:
        _win = window_msg(n) if not can_trade(n) else None
    except Exception:
        _win = None
    if _win:
        flags.append((2, "WINDOW: " + _win + " No buying before 9:00 AM CT, the open is too volatile."))
    _rt = restriction_text()
    if _rt:
        flags.append((1, _rt))
    flags += [(1, "RULE: " + w) for w in rule_lines if w != _win]
    rules_txt = " ".join(rule_lines)

    bid = ask = oi = None
    try:
        import alpaca_live
        q = alpaca_live.option_quote(tk, t["expiry"], t["strike"], cp)
        if q and q["bid"] > 0 and q["ask"] > 0:
            bid, ask = q["bid"], q["ask"]
    except Exception as e:
        print(f"[check] alpaca option: {e}")
    try:
        import yfinance as yf
        from market_extras import _nz
        ch = yf.Ticker(tk).option_chain(t["expiry"])
        df = ch.calls if cp == "C" else ch.puts
        row = df[(df["strike"] - float(t["strike"])).abs() < 1e-6]
        if not row.empty:
            r = row.iloc[0]
            oi = int(_nz(r.get("openInterest")))
            if bid is None and _nz(r.get("bid")) > 0 and _nz(r.get("ask")) > 0:
                bid, ask = _nz(r.get("bid")), _nz(r.get("ask"))
    except Exception as e:
        print(f"[check] chain: {e}")
    if oi is not None and "thin options" not in rules_txt:
        if oi < 250:
            flags.append((2, f"THIN: only {oi} open contracts. Hard to sell at a fair price."))
        elif oi < 500:
            flags.append((1, f"LIGHT: {oi} open contracts. Spreads can be wide."))
    if bid and ask:
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid
        if "wide bid/ask" not in rules_txt:
            if spread > 0.15:
                flags.append((2, f"SPREAD: bid {bid:.2f} / ask {ask:.2f} is a {spread:.0%} gap. You lose that just getting in and out."))
            elif spread > 0.05:
                flags.append((1, f"SPREAD: bid {bid:.2f} / ask {ask:.2f} ({spread:.0%} gap)."))
        over = (t["price"] - mid) * 100 * qty
        if over > max(0.01 * cost, 5):
            flags.append((1, f"PRICE: your {t['price']:.2f} is ${over:,.0f} above the middle ({mid:.2f}). Try a lower limit."))

    _px_now = None
    try:
        import alpaca_live
        d = alpaca_live.trend([tk]).get(tk)
        _px_now = (d or {}).get("price")
        if d:
            word = "up" if cp == "C" else "down"
            day, m60 = d.get("day"), d.get("m60")
            if day is not None:
                if day * sign >= 0.05:
                    flags.append((2, f"CHASE: {tk} is already {word} {abs(day):.1%} today. Much of the move may be gone."))
                elif day * sign >= 0.03:
                    flags.append((1, f"CHASE: {tk} is already {word} {abs(day):.1%} today."))
                elif day * sign <= -0.01:
                    flags.append((1, f"AGAINST: {tk} is moving the other way today ({day:+.1%})."))
            if m60 is not None and m60 * sign >= 0.02:
                flags.append((1, f"SPIKE: {abs(m60):.1%} {word} in the last hour. You may be buying the top."))
    except Exception as e:
        print(f"[check] trend: {e}")

    if not any(f[1].startswith("CHASE:") for f in flags):
        try:
            rr = _recent_run(tk, _px_now, sign)
            if rr:
                mv, refd = rr
                word = "up" if mv > 0 else "down"
                msg = (f"RUN: {tk} is already {word} {abs(mv):.1%} from {refd.strftime('%A')}'s close "
                       "(last 3 sessions). The move may already be priced in.")
                if abs(mv) >= RUN_STOP:
                    flags.append((2, msg))
                elif abs(mv) >= RUN_CAUTION:
                    flags.append((1, msg))
        except Exception as e:
            print(f"[check] recent run: {e}")
    try:
        od = _opt_day(tk, t["expiry"], t["strike"], cp)
        if od is None:
            flags.append((1, "OPTION MOVE: couldn't read this option's move today, so check its day change yourself."))
        else:
            up = t["price"] / od["ref"] - 1
            base = "yesterday's close" if od["prev"] else "today's open"
            if up >= OPT_CHASE_STOP:
                flags.append((2, f"LATE: this option is already up {up:.0%} from {base} ({od['ref']:.2f} to {t['price']:.2f}). You'd be buying after the pop."))
            elif up >= OPT_CHASE_CAUTION:
                flags.append((1, f"LATE: this option is already up {up:.0%} from {base} ({od['ref']:.2f} to {t['price']:.2f})."))
            if od["high"] and od["low"] and od["high"] > od["low"] * 1.3 and t["price"] >= od["high"] * 0.9:
                flags.append((1, f"PEAK: it ran {od['low']:.2f} to {od['high']:.2f} today and you're paying near the high."))
    except Exception as e:
        print(f"[check] option move: {e}")
        flags.append((1, "OPTION MOVE: couldn't read this option's move today, so check its day change yourself."))

    try:
        with open(os.path.join(HERE, "buy_signals.json")) as f:
            sigs = [x for x in json.load(f) if x.get("ticker") == tk and x.get("side", "C") == cp]
    except (FileNotFoundError, json.JSONDecodeError):
        sigs = []
    if not sigs:
        flags.append((1, f"NO SIGNAL: the bot hasn't flagged {tk} lately. This is your own idea, not a bot signal."))
    else:
        when = datetime.fromisoformat(sigs[-1]["time"])
        if when.tzinfo is None:
            when = when.replace(tzinfo=n.tzinfo)
        when = when.astimezone(n.tzinfo)
        ref = when
        if sigs[-1].get("quiet"):
            open9 = n.replace(hour=9, minute=0, second=0, microsecond=0)
            if when < open9 <= n:
                ref = open9
            flags.append((1, f"OVERNIGHT: this signal fired at {when.strftime('%-I:%M %p')} CT, before the market was open for you. "
                             "Make sure the stock is still moving this way now (see CHASE and AGAINST)."))
        mins = int((n - ref).total_seconds() // 60)
        age = f"{mins} min" if mins < 90 else (f"{mins // 60} hr" if mins < 2880 else f"{mins // 1440} days")
        if mins > 60:
            flags.append((1, f"STALE: the signal fired {age} ago. The move may already be over."))
    if can_trade(n) and not market_open(n):
        flags.append((1, "CLOSED: the market is closed right now, so these prices are old."))

    flags.sort(key=lambda f: -f[0])
    top = max([f[0] for f in flags], default=0)
    ex = date.fromisoformat(t["expiry"])
    head = f"CHECK {tk} {t['strike']:g}{cp} {ex.month}/{ex.day} x{qty} @ {t['price']:.2f} = ${cost:,.0f}"
    verdict = {2: "\U0001F6D1 STOP - fix these before you buy",
               1: "\U0001F7E1 CAUTION - read these first",
               0: "\U0001F7E2 No flags"}[top]
    lines = [head, verdict] + ["- " + f[1] for f in flags]
    try:
        _eb = next_earnings(tk, n.date())
        if _eb and 0 <= (_eb - n.date()).days <= 21:
            _el = earnings_followthrough_line(tk)
            if _el:
                lines.append("- INFO: " + _el)
    except Exception as e:
        print(f"[check] er info: {e}")
    # check news v1
    try:
        _nr = [r for r in _outcomes() if r["ticker"] == tk][-3:][::-1]
        if _nr:
            lines.append("NEWS:")
            for r in _nr:
                _mk = {"bullish": "GOOD", "bearish": "BAD"}.get(r["direction"], "neutral")
                lines.append(f"  {_nd(r)} {_mk}: {r['title'][:90]}")
        else:
            lines.append(f"NEWS: nothing logged for {tk} lately.")
    except Exception as e:
        print(f"[check] news: {e}")
    lines.append("Nothing was logged. After you buy, paste the order with its 'Filled at' line.")
    return "\n".join(lines)


def cmd_check(text):
    reply(check_text(text))


# option chase v1
OPT_CHASE_CAUTION = 0.50   # option already up this much today -> caution
OPT_CHASE_STOP = 1.00      # option already up this much today -> stop


def _opt_day_alpaca(tk, expiry, strike, cp):
    """Option's own move today: {ref, low, high, last} or None."""
    import alpaca_live
    sym = alpaca_live.occ(tk, expiry, strike, cp)
    data = alpaca_live._get("/v1beta1/options/snapshots", {"symbols": sym, "feed": "indicative"})
    sn = (data.get("snapshots") or {}).get(sym)
    if not sn:
        return None
    day = sn.get("dailyBar") or {}
    prev = sn.get("prevDailyBar") or {}
    ref = prev.get("c") or day.get("o")
    if not ref:
        return None
    return {"ref": float(ref), "low": float(day.get("l") or 0) or None,
            "high": float(day.get("h") or 0) or None, "prev": bool(prev.get("c"))}


# option chase yahoo v1
def _opt_day(tk, expiry, strike, cp):
    """Alpaca first, Yahoo (about 15 min delayed) if Alpaca has nothing."""
    try:
        od = _opt_day_alpaca(tk, expiry, strike, cp)
        if od:
            return od
    except Exception as e:
        print(f"[check] alpaca option day: {e}")
    import yfinance as yf
    from market_extras import _nz
    ch = yf.Ticker(tk).option_chain(expiry)
    df = ch.calls if cp == "C" else ch.puts
    row = df[(df["strike"] - float(strike)).abs() < 1e-6]
    if row.empty:
        return None
    r = row.iloc[0]
    last, chg = _nz(r.get("lastPrice")), r.get("change")
    try:
        chg = float(chg)
    except (TypeError, ValueError):
        return None
    if chg != chg or last <= 0 or last - chg <= 0:
        return None
    return {"ref": last - chg, "low": None, "high": None, "prev": True}


# earnings followthrough v1
ER_CACHE = os.path.join(HERE, "earnings_hist.json")
ER_MIN_MOVE = 0.015
ER_TTL_DAYS = 14


def _er_stats(tk):
    """How often the stock kept going in its first-day direction over the next 10 days after past reports."""
    import yfinance as yf
    t = yf.Ticker(tk)
    ed = t.get_earnings_dates(limit=16)
    h = t.history(period="6y", auto_adjust=False)
    h.index = h.index.tz_localize(None).normalize()
    today = pd.Timestamp.today().normalize()
    res, seen = [], set()
    for stamp in ed.index:
        s_ = stamp.tz_convert("America/New_York") if stamp.tzinfo else stamp
        d = s_.tz_localize(None).normalize() if s_.tzinfo else s_.normalize()
        if d >= today or d in seen:
            continue
        seen.add(d)
        i = h.index.searchsorted(d)
        if i < 1 or i + 11 >= len(h):
            continue
        if s_.hour >= 12:
            j = i + 1
        elif s_.hour > 0:
            j = i
        else:
            m0 = abs(h["Close"].iloc[i] / h["Close"].iloc[i - 1] - 1)
            m1 = abs(h["Close"].iloc[i + 1] / h["Close"].iloc[i] - 1)
            j = i if m0 >= m1 else i + 1
        r1 = float(h["Close"].iloc[j] / h["Close"].iloc[j - 1] - 1)
        if abs(r1) < ER_MIN_MOVE:
            continue
        entry = float(h["Open"].iloc[j + 1])
        f10 = float(h["Close"].iloc[j + 10] / entry - 1) * (1 if r1 > 0 else -1)
        res.append(f10)
    if not res:
        return None
    res.sort()
    return {"n": len(res), "k": sum(1 for x in res if x > 0), "med": res[len(res) // 2]}


def earnings_followthrough_line(tk):
    """One info line for pick/check. None if unavailable. Cached for ER_TTL_DAYS."""
    try:
        with open(ER_CACHE) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    hit = cache.get(tk)
    fresh = hit and (date.today() - date.fromisoformat(hit["day"])).days < ER_TTL_DAYS
    if not fresh:
        try:
            st = _er_stats(tk)
        except Exception as e:
            print(f"[er] {tk}: {e}")
            return None
        if not st:
            return None
        hit = dict(st, day=date.today().isoformat())
        cache[tk] = hit
        try:
            with open(ER_CACHE, "w") as f:
                json.dump(cache, f)
        except OSError:
            pass
    few = " (few reports, weak evidence)" if hit["n"] < 12 else ""
    return (f"Past {tk} reports: stock kept going the same way as its first-day move after 10 days "
            f"{hit['k']}/{hit['n']} times, median {hit['med']:+.1%}{few}. Info only, not a signal.")


# dip alert v1
DIP_PCT = -0.005                          # SPY down at least this much vs yesterday's close
DIP_WINDOW = (dtime(13, 30), dtime(14, 45))
DIP_DAYS = {0, 1, 2, 3}                  # Mon-Thu. Fridays you need to be flat by the close.
_DIP = {"last": 0.0}


def _phone_actions(tk):
    base = os.environ.get("NTFY_PHONE_URL", "").strip().rstrip("/")
    if not base:
        try:
            import subprocess
            ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
            base = f"http://{ip[0]}:2586" if ip else ""
        except Exception:
            base = ""
    if not base:
        return None
    return "; ".join([f"http, Check 1, {base}/trade, method=POST, body=check {tk} 1",
                      f"http, Buy, {base}/trade, method=POST, body=buy {tk}, clear=true",
                      f"http, Skip, {base}/trade, method=POST, body=skip {tk}, clear=true"])


# dip hold note v1
def _add_trading_days(d, k):
    while k > 0:
        d += timedelta(days=1)
        if market_day(d)["open"]:
            k -= 1
    return d


def hold_note(n):
    sell = _add_trading_days(n.date(), 3)
    txt = (f"IF YOU BUY: hold it at least 3 trading days (earliest sell {sell.strftime('%a')} {sell.month}/{sell.day}). "
           "In SPY's history, buying after a red day paid off over 3 to 5 days, not overnight.")
    friday = n.date() + timedelta(days=4 - n.weekday())
    if sell > friday:
        txt += ("\nHeads up: that runs past this Friday. Your flat-by-Friday reminder will fire, and holding over "
                "the weekend is a different bet. Decide before you buy.")
    txt += "\nThe -20% stop alert can fire before day 3. Decide now whether you will honor it."
    return txt


def dip_text(day, n):
    head = (f"The market is down: SPY {day:+.1%} today ({n.strftime('%-I:%M %p')} CT).\n"
            "Want to buy SPY calls on the dip? Here are the two picks, already checked:")
    parts = [head]
    for k in (1, 2):
        try:
            parts.append(check_text(f"check SPY {k}"))
        except Exception as e:
            parts.append(f"(couldn't check SPY pick {k}: {e})")
    parts.append(hold_note(n))
    parts.append("Not a signal, just a reminder. If you buy, paste the order with its Filled at line.")
    return "\n\n".join(parts)


def dip_alert_tick(n):
    if n.weekday() not in DIP_DAYS or not (DIP_WINDOW[0] <= n.time() <= DIP_WINDOW[1]):
        return
    if not (can_trade(n) and market_open(n)) or time.time() - _DIP["last"] < 300:
        return
    _DIP["last"] = time.time()
    state_path = os.path.join(HERE, "daily_state.json")
    try:
        with open(state_path) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    today = n.date().isoformat()
    if st.get("dip_alert") == today:
        return
    import alpaca_live
    d = alpaca_live.trend(["SPY"]).get("SPY")
    day = d.get("day") if d else None
    if day is None or day > DIP_PCT:
        return
    st["dip_alert"] = today
    try:
        with open(state_path, "w") as f:
            json.dump(st, f)
    except OSError:
        pass
    alert(dip_text(day, n), title=f"SPY is down {abs(day):.1%} - buy the dip?", priority=PRI_ACTION,
          tags="chart_with_downwards_trend", actions=_phone_actions("SPY"))


def cmd_dip(_):
    import alpaca_live
    d = alpaca_live.trend(["SPY"]).get("SPY")
    if not d or d.get("day") is None:
        raise ValueError("couldn't read SPY right now")
    n = now()
    reply(dip_text(d["day"], n) + f"\n\n(Preview. The real alert fires once a day, Mon-Thu, "
          f"{DIP_WINDOW[0].strftime('%-I:%M')}-{DIP_WINDOW[1].strftime('%-I:%M')} PM CT, if SPY is down {abs(DIP_PCT):.1%} or more.)")


# multi-day run v1
RUN_CAUTION = 0.03    # already moved this much in your direction over the last 3 sessions
RUN_STOP = 0.06


def _recent_run(tk, price, sign):
    """Biggest move in the trade's direction from any of the last 4 completed closes. (move, ref_date) or None."""
    import yfinance as yf
    h = yf.Ticker(tk).history(period="15d", auto_adjust=False)
    c = h["Close"].dropna()
    if not len(c):
        return None
    last = float(c.iloc[-1])
    c = c[[i.date() < now().date() for i in c.index]].iloc[-4:]
    if not len(c):
        return None
    px = float(price) if price else last
    moves = [(px / float(v) - 1, i.date()) for i, v in c.items()]
    best = max(moves, key=lambda m: m[0] * sign)
    return best if best[0] * sign > 0 else None


# restriction v1
def _md(txt, future):
    m = RE_DATE.match(txt)
    if not m:
        raise ValueError("date like 10/23")
    mo, d = int(m.group(1)), int(m.group(2))
    y = int(m.group(3)) if m.group(3) else now().year
    if y < 100:
        y += 2000
    dt = date(y, mo, d)
    today = now().date()
    if not m.group(3):
        if future and dt < today:
            dt = date(y + 1, mo, d)
        if not future and dt > today:
            dt = date(y - 1, mo, d)
    return dt


def restriction_end():
    try:
        v = load_account().get("restricted_until")
        end = date.fromisoformat(v) if v else None
    except (ValueError, TypeError):
        return None
    return end if end and end >= now().date() else None


def restriction_text():
    end = restriction_end()
    if not end:
        return None
    left = (end - now().date()).days
    return (f"RESTRICTED (good faith violations) until {end.strftime('%a')} {end.month}/{end.day}/{end.year}, "
            f"{left} days left. Buy only with cash that was already settled before you place the order.")


def cmd_restricted(words):
    a = load_account()
    if not words:
        reply(restriction_text() or "No restriction set. Send: restricted from 10/23  (90 days later) or restricted until 1/21")
        return
    w = words[0].lower()
    if w in ("off", "none", "clear"):
        a["restricted_until"] = None
    elif w == "from" and len(words) > 1:
        a["restricted_until"] = (_md(words[1], future=False) + timedelta(days=90)).isoformat()
    elif w == "until" and len(words) > 1:
        a["restricted_until"] = _md(words[1], future=True).isoformat()
    else:
        raise ValueError("like: restricted from 10/23   or   restricted until 1/21   or   restricted off")
    save_account(a)
    reply(restriction_text() or "Restriction cleared.")


# decide v1
DEC_FILE = os.path.join(HERE, "decisions.csv")


def _latest_signal(tk=None):
    try:
        with open(os.path.join(HERE, "buy_signals.json")) as f:
            sigs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    for sg in reversed(sigs):
        if tk and sg.get("ticker") != tk:
            continue
        try:
            if (now() - datetime.fromisoformat(sg["time"])).total_seconds() > 24 * 3600:
                continue
        except Exception:
            continue
        return sg
    return None


def _decide(decision, words):
    """Log 'I'm buying this' / 'skip this' for the latest alert (or the ticker named)."""
    side, tk = None, None
    for w in words:
        wl = w.lower()
        if wl in ("put", "puts"):
            side = "P"
        elif wl in ("call", "calls"):
            side = "C"
        elif re.fullmatch(r"[A-Za-z.]{1,6}", w):
            tk = w.upper()
    sg = _latest_signal(tk)
    if tk is None:
        if not sg:
            raise ValueError("no alert in the last 24 hours to answer. Name it: skip LLY")
        tk = sg["ticker"]
    if side is None:
        side = (sg or {}).get("side", "C")
    price = None
    try:
        import alpaca_live
        price = alpaca_live.prices([tk]).get(tk)
    except Exception as e:
        print(f"[decide] price: {e}")
    new = not os.path.exists(DEC_FILE)
    with open(DEC_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "decision", "ticker", "side", "stock_price"])
        w.writerow([now().isoformat(timespec="seconds"), decision, tk, side, f"{price:.2f}" if price else ""])
    return tk, side, price


def cmd_skip(words):
    tk, side, price = _decide("skip", words)
    word = "put" if side == "P" else "call"
    px = f" (stock ${price:,.2f})" if price else ""
    reply(f"SKIPPED {tk} {word}{px}.\nI'll track what the stock does after this, so you can see what you passed on. Type 'decisions' any time.")


def cmd_buy_decision(words):
    # buy shows picks v2
    tk, side, price = _decide("buy", words)
    word = "put" if side == "P" else "call"
    px = f" (stock ${price:,.2f})" if price else ""
    reply(f"BUY noted: {tk} {word}{px}. Here are the suggested contracts and prices:")
    cmd_pick([tk] + (["put"] if side == "P" else []))
    reply(f"Want a last look before ordering? check {tk} 1" + (" put" if side == "P" else "")
          + "\nWhen it fills, paste the order text with the 'Filled at' line and I'll log it.")


def cmd_decisions(words):
    import alpaca_live
    if not os.path.exists(DEC_FILE):
        reply("No decisions yet. Tap Buy or Skip on an alert, or type skip LLY / buy LLY.")
        return
    cutoff = now() - timedelta(days=int(words[0]) if words and words[0].isdigit() else 14)
    rows = []
    with open(DEC_FILE) as f:
        for r in csv.DictReader(f):
            try:
                if datetime.fromisoformat(r["time"]) >= cutoff:
                    rows.append(r)
            except Exception:
                pass
    if not rows:
        reply("No decisions in that window.")
        return
    try:
        cur = alpaca_live.prices(sorted({r["ticker"] for r in rows}))
    except Exception as e:
        print(f"[decisions] {e}")
        cur = {}
    out, agg = [], {"buy": [], "skip": []}
    for r in rows:
        d = datetime.fromisoformat(r["time"])
        sign = 1 if r["side"] == "C" else -1
        try:
            p0, p1 = float(r["stock_price"]), cur.get(r["ticker"])
            mv = sign * (p1 / p0 - 1) if p1 else None
        except (ValueError, TypeError):
            mv = None
        if mv is not None:
            agg[r["decision"]].append(mv)
        out.append(f"{d.strftime('%a %-m/%-d')} {r['decision'].upper():4} {r['ticker']} {'put' if sign < 0 else 'call'}  "
                   + (f"{mv:+.1%}" if mv is not None else "n/a"))
    summ = []
    for k in ("buy", "skip"):
        if agg[k]:
            summ.append(f"{k}: {len(agg[k])}, avg {sum(agg[k]) / len(agg[k]):+.1%}, "
                        f"{sum(1 for x in agg[k] if x > 0)} went your way")
    reply("\n".join(out[-15:]) + "\n\n" + "\n".join(summ)
          + "\n(Stock move in your direction since the alert, not option P/L. Real results are in 'stats'.)")


def check_expiries():
    today = now().date()
    with lock:
        pos = load_positions()
        for p in pos:
            dte = (date.fromisoformat(p["expiry"]) - today).days
            if dte <= 3 and p.get("expiry_warned") != today.isoformat():
                alert(f"{p['id']} expires in {dte} day(s). Time decay speeds up from here.",
                      title="Expiry close", priority=PRI_ACTION, tags="hourglass")
                p["expiry_warned"] = today.isoformat()
        save_positions(pos)


def morning_brief():
    n = now()
    pos = load_positions()
    held = f"\nOpen positions: {len(pos)}" if pos else ""
    md = market_day(n.date())
    if not md["open"]:
        if n.weekday() < 5:
            alert(f"Market closed today ({md['name']}). No trading.{held}", title=md["name"],
                  priority=PRI_INFO, tags="palm_tree")
        return
    if md["early"]:
        alert(f"Early close today: market closes at {md['close'].strftime('%-I:%M %p')} CT. "
              f"Plan to be out before then.", title="Short day", priority=PRI_INFO, tags="hourglass")
    if n.weekday() == 0:
        alert(f"Trading opens at 11:00. No new trades before then.{held}",
              title=f"{n:%A}", priority=PRI_INFO, tags="clock11")
        return
    if n.weekday() in TRADE_DAYS:
        try:
            s = nvda_state()
            body = spy_text() + "\n\n" + nvda_text(s) + held
            pri = PRI_ACTION
        except Exception as e:
            body, pri = spy_text() + f"\n\nNVDA check failed: {e}{held}", PRI_ACTION
        alert(body, title=f"{n:%A} - trading day", priority=pri, tags="sunrise")
    else:
        alert(f"Hands-off day. No new trades.{held}", title=f"{n:%A}", priority=PRI_INFO, tags="zzz")


def tuesday_open():
    if not market_day(now().date())["open"]:
        return
    pos = load_positions()
    held = f"\nOpen positions: {len(pos)}" if pos else ""
    try:
        nv = nvda_text(nvda_state())
    except Exception as e:
        nv = f"NVDA check failed: {e}"
    alert(spy_text() + "\n\n" + nv + held, title=f"{now():%A} - trading open", priority=PRI_ACTION, tags="bell")


_FLAT_SENT = {"day": None}


# dip hold v1
def _dip_alert_today(n):
    try:
        with open(os.path.join(HERE, "daily_state.json")) as f:
            return json.load(f).get("dip_alert") == n.date().isoformat()
    except (OSError, ValueError):
        return False


def friday_flat():
    pos = load_positions()
    n = now()
    today = n.date().isoformat()
    held = [p for p in pos if p.get("hold_until") and p["hold_until"] > today]
    due = [p for p in pos if p not in held]
    md = market_day(n.date())
    why = "last trading day this week" if n.weekday() != 4 else "Friday"
    if md["early"]:
        why += f", early close at {md['close'].strftime('%-I:%M %p')}"
    if due:
        alert("Still holding:\n" + "\n".join(p["id"] + f" x{p['qty']}" for p in due) +
              f"\nRule: flat before the weekend ({why}). 30 minutes left.",
              title="Get flat - market closes soon", priority=PRI_URGENT, tags="warning")
    if held:
        lines = []
        for p in held:
            d = date.fromisoformat(p["hold_until"])
            lines.append(f"{p['id']} x{p['qty']} - {'swing' if p.get('swing') else 'dip'} trade, hold until at least {d.strftime('%a')} {d.month}/{d.day}")
        alert("Holding over the weekend on purpose:\n" + "\n".join(lines) +
              "\nThe get-flat rule is skipped for these. Stop and target alerts still run.",
              title="Holding over the weekend", priority=PRI_INFO, tags="information_source")


def hold_due():
    today = now().date().isoformat()
    with lock:
        pos = load_positions()
        due = [p for p in pos if p.get("hold_until") == today]
        swings = [p for p in due if p.get("swing")]
        for p in swings:
            p["swing"] = False
            p.pop("stop", None)
            p.pop("targets", None)
            p["targets_hit"] = []
        if swings:
            save_positions(pos)
    if due:
        if swings:
            alert("Last day of the swing. Plan to sell by the close today:\n" + "\n".join(p["id"] + f" x{p['qty']}" for p in due) +
                  "\nThe normal rules are back on (stop -20%, +20% and +40% alerts, flat by Friday).",
                  title="Swing ends today", priority=PRI_ACTION, tags="alarm_clock")
        else:
            alert("The 3-day hold is up today:\n" + "\n".join(p["id"] + f" x{p['qty']}" for p in due) +
                  "\nYou can sell any time now. The normal rules (stop, targets, flat by Friday) apply again.",
                  title="Dip hold is over", priority=PRI_ACTION, tags="alarm_clock")


def cmd_hold(words):
    if not words:
        raise ValueError("like: hold SPY   (marks your SPY position as a 3-day dip hold)")
    tk = words[0].upper()
    with lock:
        pos = load_positions()
        hits = [p for p in pos if p["ticker"] == tk]
        if not hits:
            raise ValueError(f"no open {tk} position")
        for p in hits:
            opened = datetime.fromisoformat(p["opened"]).date()
            p["hold_until"] = _add_trading_days(opened, 3).isoformat()
        save_positions(pos)
    d = date.fromisoformat(hits[0]["hold_until"])
    if d <= now().date():
        reply(f"{tk} is already past 3 trading days, so the normal rules apply.")
    else:
        reply(f"{tk}: holding until at least {d.strftime('%a')} {d.month}/{d.day}. The Friday get-flat reminder is skipped until then.")


# swing v1
SWING_STOP = -0.30
SWING_TARGETS = [0.40]
SWING_LOG = os.path.join(HERE, "swing_log.csv")


def _swing_actions(tk):
    base = os.environ.get("NTFY_PHONE_URL", "").strip().rstrip("/")
    if not base:
        try:
            import subprocess
            ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
            base = f"http://{ip[0]}:2586" if ip else ""
        except Exception:
            base = ""
    if not base:
        return None
    return f"http, Swing, {base}/trade, method=POST, body=swing {tk}, clear=true"


# swing default v1
SWING_DEFAULT = True      # a new position bought after a bot signal (Mon-Thu) starts as a swing; the "Same day" button undoes it
SAMEDAY_LOG = os.path.join(HERE, "sameday_log.csv")


def _auto_swing(p, n):
    """Caller holds the lock and saves positions."""
    opened = datetime.fromisoformat(p["opened"]).date()
    p["swing"] = True
    p["stop"] = SWING_STOP
    p["targets"] = SWING_TARGETS
    p["targets_hit"] = []
    p["stop_last"] = None
    p["hold_until"] = _add_trading_days(opened, 1).isoformat()
    try:
        new = not os.path.exists(SWING_LOG)
        with open(SWING_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "contract", "qty", "entry"])
            w.writerow([n.isoformat(timespec="seconds"), p["id"], p["qty"], p["entry"]])
    except Exception as e:
        print(f"[swing log] {e}")


def _sameday_actions(tk):
    a = _swing_actions(tk)
    return a.replace("Swing,", "Same day,").replace(f"body=swing {tk}", f"body=sameday {tk}") if a else None


def cmd_sameday(words):
    if not words:
        raise ValueError("like: sameday LLY   (go back to the normal same-day rules for that position)")
    tk = words[0].upper()
    n = now()
    with lock:
        pos = load_positions()
        hits = [p for p in pos if p["ticker"] == tk and p.get("swing")]
        if not hits:
            raise ValueError(f"no swing {tk} position")
        for p in hits:
            p["swing"] = False
            for k in ("stop", "targets", "hold_until"):
                p.pop(k, None)
            p["targets_hit"] = []
            p["stop_last"] = None
        save_positions(pos)
        try:
            new = not os.path.exists(SAMEDAY_LOG)
            with open(SAMEDAY_LOG, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "contract", "qty", "entry"])
                for p in hits:
                    w.writerow([n.isoformat(timespec="seconds"), p["id"], p["qty"], p["entry"]])
        except Exception as e:
            print(f"[sameday log] {e}")
    reply(f"SAME DAY: {', '.join(p['id'] for p in hits)}\n"
          f"Back to the normal rules: stop alert {STOP:.0%}, +20% prompts, flat by Friday.")


def cmd_swing(words):
    if not words:
        raise ValueError("like: swing LLY   (hold that position to the next close with a -30% stop)")
    tk = words[0].upper()
    n = now()
    if n.weekday() == 4:
        reply("Not on a Friday: a swing would carry the option over the weekend (two extra days of time decay). "
              "Plan to be flat today, or start a swing on Monday to Thursday.")
        return
    with lock:
        pos = load_positions()
        hits = [p for p in pos if p["ticker"] == tk]
        if not hits:
            raise ValueError(f"no open {tk} position")
        for p in hits:
            opened = datetime.fromisoformat(p["opened"]).date()
            p["swing"] = True
            p["stop"] = SWING_STOP
            p["targets"] = SWING_TARGETS
            p["targets_hit"] = []
            p["stop_last"] = None
            p["hold_until"] = _add_trading_days(opened, 1).isoformat()
        save_positions(pos)
        try:
            new = not os.path.exists(SWING_LOG)
            with open(SWING_LOG, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "contract", "qty", "entry"])
                for p in hits:
                    w.writerow([n.isoformat(timespec="seconds"), p["id"], p["qty"], p["entry"]])
        except Exception as e:
            print(f"[swing log] {e}")
    d = date.fromisoformat(hits[0]["hold_until"])
    reply(f"SWING: {', '.join(p['id'] for p in hits)}\n"
          f"Holding to the close of {d.strftime('%a')} {d.month}/{d.day}. Stop alert at {SWING_STOP:.0%} (wider than your usual -20%), "
          f"no +20% sell prompt, the +40% alert is still on. The Friday get-flat reminder is skipped until then, "
          f"and I'll remind you on the morning of the last day.")


def flat_check(n):
    md = market_day(n.date())
    if not md["open"] or _FLAT_SENT["day"] == n.date() or not last_session_of_week(n.date()):
        return
    mins = (md["close"].hour * 60 + md["close"].minute) - (n.hour * 60 + n.minute)
    if 0 < mins <= 30:
        _FLAT_SENT["day"] = n.date()
        friday_flat()


def end_of_day():
    pos = load_positions()
    today = now().date().isoformat()
    trades = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            trades = [r for r in csv.DictReader(f) if r["time"].startswith(today)]
    if not pos and not trades:
        return
    realized = sum(float(r["realized_pnl"]) for r in trades if r["realized_pnl"])
    body = f"Trades today: {len(trades)} | realized {'+' if realized >= 0 else '-'}${abs(realized):,.0f}"
    if pos:
        body += "\n" + "\n".join(position_lines(pos, {}))
    alert(body, title="End of day", priority=PRI_INFO, tags="memo")


class Daily:
    """Run fn once per day at/after hh:mm on the given weekdays."""
    STATE = os.path.join(HERE, "daily_state.json")

    def __init__(self, hh, mm, fn, days=range(5)):
        self.at, self.fn, self.days, self.last = dtime(hh, mm), fn, set(days), None

    def _sent_today(self, n):
        # remembered on disk so a restart does not resend today's alert
        try:
            with open(self.STATE) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        today = n.date().isoformat()
        if st.get(self.fn.__name__) == today:
            return True
        st[self.fn.__name__] = today
        try:
            with open(self.STATE, "w") as f:
                json.dump(st, f)
        except OSError:
            pass
        return False

    def tick(self, n):
        if n.weekday() in self.days and n.time() >= self.at and self.last != n.date():
            self.last = n.date()
            if self._sent_today(n):
                return
            if n.time() < (datetime.combine(n.date(), self.at) + timedelta(hours=2)).time():
                run(self.fn)


def run(fn):
    try:
        fn()
    except Exception:
        traceback.print_exc()


def main():
    print(f"tradebot up. commands on '{CMD_TOPIC}', alerts on '{ALERT_TOPIC}'")
    threading.Thread(target=listen, daemon=True).start()
    reply("tradebot online. Send 'help' for commands.")
    daily = [Daily(8, 0, morning_brief), Daily(8, 35, check_expiries), Daily(15, 30, friday_scorecard, days=[4]), Daily(8, 40, earnings_watch), Daily(11, 0, tuesday_open, days=[0, 1]),
             Daily(15, 10, end_of_day), Daily(8, 45, hold_due)]
    last_pos = last_nvda = last_live = 0.0
    nvda_zone = None
    while True:
        n = now()
        for d in daily:
            d.tick(n)
        run(lambda: flat_check(n))
        run(lambda: beat("tradebot", quote_ok=LAST_QUOTE_OK))
        if market_open(n):
            run(lambda: dip_alert_tick(n))
            if time.time() - last_pos >= POLL_SEC:
                last_pos = time.time()
                run(check_positions)
            if time.time() - last_live >= 60:
                last_live = time.time()
                run(live_watch)
            if can_trade(n) and time.time() - last_nvda >= NVDA_POLL_MIN * 60:
                last_nvda = time.time()
                try:
                    s = nvda_state()
                    if nvda_zone is False and s["zone"]:
                        alert(nvda_text(s), title="NVDA: ready to buy",
                              priority=PRI_ACTION, tags="chart_with_downwards_trend")
                    nvda_zone = s["zone"]
                except Exception:
                    traceback.print_exc()
        time.sleep(20)


if __name__ == "__main__":
    main()
