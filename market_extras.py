"""
market_extras.py - shared helpers for tradebot.py and newswatch.py:
  - next earnings date (with a daily cache)
  - picking a call contract the way you trade (9+ days out, at/just above the money)
  - option quotes
Data from Yahoo via yfinance, ~15 minutes delayed.
"""
import json
import os
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
EARN_CACHE = os.path.join(HERE, "earnings_cache.json")
NO_EARNINGS = {"SPY", "QQQ", "IWM", "DIA", "DRAM", "SOXL", "TQQQ", "VTI", "VOO"}


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _to_date(x):
    try:
        if isinstance(x, datetime):
            return x.date()
        if isinstance(x, date):
            return x
        if hasattr(x, "to_pydatetime"):
            return x.to_pydatetime().date()
        return date.fromisoformat(str(x)[:10])
    except Exception:
        return None


def next_earnings(ticker, today=None):
    """Next earnings date on or after today, or None (ETFs, unknown)."""
    t = ticker.upper()
    today = today or date.today()
    if t in NO_EARNINGS:
        return None
    cache = _load(EARN_CACHE, {})
    hit = cache.get(t)
    if hit and hit.get("checked") == today.isoformat():
        return date.fromisoformat(hit["date"]) if hit.get("date") else None
    found = None
    try:
        import yfinance as yf
        tk = yf.Ticker(t)
        dates = []
        try:
            cal = tk.calendar
            if isinstance(cal, dict):
                dates = cal.get("Earnings Date") or []
            elif cal is not None and hasattr(cal, "loc") and "Earnings Date" in getattr(cal, "index", []):
                dates = list(cal.loc["Earnings Date"].values)
        except Exception:
            dates = []
        if not dates:
            try:
                ed = tk.get_earnings_dates(limit=8)
                if ed is not None:
                    dates = list(ed.index)
            except Exception:
                pass
        future = sorted(d for d in (_to_date(x) for x in dates) if d and d >= today)
        found = future[0] if future else None
    except Exception:
        found = None
    cache[t] = {"checked": today.isoformat(), "date": found.isoformat() if found else None}
    _save(EARN_CACHE, cache)
    return found


def earnings_before(ticker, expiry, today=None):
    """Earnings date if it lands between today and the option's expiry, else None."""
    today = today or date.today()
    if isinstance(expiry, str):
        expiry = date.fromisoformat(expiry)
    d = next_earnings(ticker, today)
    return d if d and today <= d <= expiry else None


def earnings_line(ticker, today=None, within=21):
    today = today or date.today()
    d = next_earnings(ticker, today)
    if not d or (d - today).days > within:
        return ""
    days = (d - today).days
    when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
    return f"\n\U0001F4C5 {ticker} reports earnings {d.month}/{d.day} ({when}) - options get repriced after."


def quote_option(ticker, expiry_iso, strike, cp="C"):
    """Mid of bid/ask (falls back to last trade)."""
    import yfinance as yf
    ch = yf.Ticker(ticker).option_chain(expiry_iso)
    df = ch.calls if cp == "C" else ch.puts
    row = df[(df["strike"] - float(strike)).abs() < 1e-6]
    if row.empty:
        return None
    r = row.iloc[0]
    bid, ask, last = _nz(r.get("bid")), _nz(r.get("ask")), _nz(r.get("lastPrice"))
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return last or None


def pick_call(ticker, today=None, min_dte=9, max_dte=30, max_spread=0.15):
    """The call you'd likely buy: first expiry 9+ days out, first strike at/above the price,
    with a tradeable bid/ask. Returns dict or None."""
    import yfinance as yf
    today = today or date.today()
    tk = yf.Ticker(ticker)
    try:
        spot = float(tk.fast_info.last_price)
    except Exception:
        return None
    for e in tk.options:
        dte = (date.fromisoformat(e) - today).days
        if dte < min_dte:
            continue
        if dte > max_dte:
            break
        calls = tk.option_chain(e).calls
        calls = calls[calls["strike"] >= spot].sort_values("strike")
        for _, r in calls.head(3).iterrows():
            bid, ask = _nz(r.get("bid")), _nz(r.get("ask"))
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2
            if (ask - bid) / mid > max_spread:
                continue
            return {"ticker": ticker, "expiry": e, "dte": dte, "strike": float(r["strike"]),
                    "mid": round(mid, 2), "spot": spot}
    return None


# ---------------------------------------------------------------- market calendar (NYSE)
# Uses exchange_calendars for holidays and early closes. Falls back to plain
# weekdays 8:30-3:00 CT if it isn't installed.
from datetime import time as _dtime
from datetime import timedelta as _td

_CAL = {"day": None, "cal": None}
_CT = "America/Chicago"


def _xnys():
    today = date.today()
    if _CAL["day"] != today:
        try:
            import exchange_calendars as xc
            import pandas as pd
            _CAL["cal"] = xc.get_calendar("XNYS", start=pd.Timestamp(today - _td(days=30)),
                                          end=pd.Timestamp(today + _td(days=500)))
        except Exception as e:
            print(f"[calendar] falling back to weekdays: {e}")
            _CAL["cal"] = None
        _CAL["day"] = today
    return _CAL["cal"]


def market_day(d=None):
    """{'open': bool, 'name': holiday name or '', 'close': time CT, 'early': bool}"""
    d = d or date.today()
    cal = _xnys()
    if cal is None:
        return {"open": d.weekday() < 5, "name": "Weekend" if d.weekday() > 4 else "",
                "close": _dtime(15, 0), "early": False}
    import pandas as pd
    ts = pd.Timestamp(d)
    if not cal.is_session(ts):
        name = "Weekend" if d.weekday() > 4 else "Market holiday"
        try:
            h = cal.regular_holidays.holidays(ts, ts, return_name=True)
            if len(h):
                name = str(h.iloc[0])
        except Exception:
            pass
        return {"open": False, "name": name, "close": None, "early": False}
    close = cal.session_close(ts).tz_convert(_CT).time()
    return {"open": True, "name": "", "close": close, "early": close < _dtime(15, 0)}


def is_open_now(n):
    md = market_day(n.date())
    return md["open"] and _dtime(8, 30) <= n.time() <= md["close"]


def last_session_of_week(d=None):
    """True if d is a trading day and no other trading day follows it this week."""
    d = d or date.today()
    if not market_day(d)["open"]:
        return False
    nxt = d + _td(days=1)
    while nxt.weekday() < 5:
        if market_day(nxt)["open"]:
            return False
        nxt += _td(days=1)
    return True


# ---------------------------------------------------------------- specific call picks
def _account_budget():
    try:
        a = _load(os.path.join(HERE, "account.json"), {})
        if not a.get("balance"):
            return None
        tp = a.get("trade_pct") or (1 - a.get("cash_pct", 0.5)) / a.get("slots", 2)
        return round(a["balance"] * tp, -1)
    except Exception:
        return None


def _nz(x):  # nan guard v1
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x else 0.0


# thin filter v1
MIN_OPT_OI = 250   # skip options with fewer open contracts than this


def contract_options(ticker, today=None, min_dte=9, max_spread=0.15, cp="C"):
    """Two calls on the first expiry 9+ days out: at-the-money and one strike higher. Returns (spot, exp, rows)."""
    import yfinance as yf
    today = today or date.today()
    tk = yf.Ticker(ticker)
    spot = float(tk.fast_info.last_price)
    for e in tk.options:
        dte = (date.fromisoformat(e) - today).days
        if dte < min_dte:
            continue
        ch = tk.option_chain(e)
        if cp == "P":
            calls = ch.puts
            calls = calls[calls["strike"] <= spot * 1.005].sort_values("strike", ascending=False)
        else:
            calls = ch.calls
            calls = calls[calls["strike"] >= spot * 0.995].sort_values("strike")
        rows = []
        for _, r in calls.head(4).iterrows():
            bid, ask = _nz(r.get("bid")), _nz(r.get("ask"))
            oi = int(_nz(r.get("openInterest")))
            if oi < MIN_OPT_OI:
                continue
            stale = False
            if bid <= 0 or ask <= 0:
                last = _nz(r.get("lastPrice"))
                if last <= 0 or oi < 100:
                    continue
                mid, spread, stale = last, 0.0, True
            else:
                mid = (bid + ask) / 2
                spread = (ask - bid) / mid
                if spread > max_spread:
                    continue
            vol = int(_nz(r.get("volume")))
            rows.append({"strike": float(r["strike"]), "mid": mid, "spread": spread, "oi": oi, "vol": vol,
                         "dte": dte, "exp": e, "iv": _nz(r.get("impliedVolatility")), "stale": stale})
            if len(rows) == 2:
                break
        if rows:
            return spot, e, rows
        # nothing tradeable on this expiry: try the next one
    return spot, None, []


def contract_text(ticker, budget=None, today=None):
    """Lines describing the specific calls to look at, sized to the budget."""
    try:
        budget = budget or _account_budget()
        spot, exp, rows = contract_options(ticker, today)
    except Exception as e:
        return f"\n(couldn't pull {ticker} options: {e})"
    if not rows:
        return f"\nNo liquid calls 9+ days out on {ticker} right now - skip it."
    d = date.fromisoformat(exp)
    lines = [f"\n{ticker} ${spot:.2f}. Calls {d.month}/{d.day} ({rows[0]['dte']}d):"]
    for i, k in enumerate(rows, 1):
        tag = "closest to the money" if i == 1 else "one strike higher, cheaper"
        per = k["mid"] * 100
        if budget and per > budget:
            size = f"1 contract is ${per:,.0f}, over your ${budget:,.0f} budget"
        else:
            qty = max(1, int(budget // per)) if budget else 1
            size = f"x{qty} = ${qty * per:,.0f}, stop {k['mid'] * 0.8:.2f} (risk ${qty * per * 0.2:,.0f})"
        thin = " THIN" if k["oi"] < 100 else ""
        lines.append(f"{i}. {k['strike']:g}C ~${k['mid']:.2f} ({tag}) | {size} | "
                     f"breakeven {k['strike'] + k['mid']:.2f} | spread {k['spread']:.0%}, OI {k['oi']:,}{thin}")
    return "\n".join(lines)


def beat(name, **fields):
    """Heartbeat file read by healthcheck.py."""
    import time as _t
    path = os.path.join(HERE, f"heartbeat_{name}.json")
    hb = _load(path, {})
    hb["time"] = _t.time()
    hb.update(fields)
    _save(path, hb)


# contract_text v3 (later definition replaces earlier ones)
def _ncdf(x):
    import numpy as np
    t = 1.0 / (1.0 + 0.2316419 * np.abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    c = 1.0 - np.exp(-x * x / 2.0) / np.sqrt(2 * np.pi) * poly
    return np.where(x >= 0, c, 1.0 - c)


def _bs_call(S, K, tau, iv, r=0.04):
    import numpy as np
    tau = np.maximum(tau, 1e-6)
    d1 = (np.log(S / K) + (r + 0.5 * iv * iv) * tau) / (iv * np.sqrt(tau))
    d2 = d1 - iv * np.sqrt(tau)
    return S * _ncdf(d1) - K * np.exp(-r * tau) * _ncdf(d2)


def _bs_put(S, K, tau, iv, r=0.04):
    import numpy as np
    tau = np.maximum(tau, 1e-6)
    d1 = (np.log(S / K) + (r + 0.5 * iv * iv) * tau) / (iv * np.sqrt(tau))
    d2 = d1 - iv * np.sqrt(tau)
    return K * np.exp(-r * tau) * _ncdf(-d2) - S * _ncdf(-d1)


def win_odds(spot, strike, mid, dte, iv, n=4000, cp="C"):
    """Odds from a random-walk simulation (no edge assumed, zero drift, implied vol).
    Exits like you do: -20% stop, +20% target, out a day before expiry."""
    import numpy as np
    if not iv or iv < 0.05 or iv > 3:
        return None
    days = max(2, round(dte * 5 / 7))
    per = 13
    steps = days * per
    yr = 252.0 * per
    rng = np.random.default_rng(7)
    z = rng.standard_normal((n, steps))
    logs = np.cumsum((-0.5 * iv * iv) / yr + iv * np.sqrt(1 / yr) * z, axis=1)
    S = spot * np.exp(logs)
    tau = (steps - np.arange(1, steps + 1)) / yr
    f = _bs_put if cp == "P" else _bs_call
    P = f(S, strike, tau[None, :], iv)
    P0 = float(f(np.array(spot), strike, np.array(steps / yr), iv))
    if P0 <= 0:
        return None
    ratio = P / P0
    horizon = max(1, steps - per)
    r = ratio[:, :horizon]
    up = r >= 1.2
    dn = r <= 0.8
    first_up = np.where(up.any(axis=1), up.argmax(axis=1), horizon + 1)
    first_dn = np.where(dn.any(axis=1), dn.argmax(axis=1), horizon + 1)
    win = float(np.mean(first_up < first_dn))
    loss = float(np.mean(first_dn < first_up))
    big = ratio[:, :horizon] >= 1.4
    first_big = np.where(big.any(axis=1), big.argmax(axis=1), horizon + 1)
    win40 = float(np.mean(first_big < first_dn))
    above = float(np.mean(S[:, -1] < strike - mid)) if cp == "P" else float(np.mean(S[:, -1] > strike + mid))
    return {"win": win, "loss": loss, "neither": 1 - win - loss, "win40": win40, "above": above}


def contract_text(ticker, budget=None, today=None, cp="C"):
    """Readable call or put picks with dollars and win odds."""
    word = "Puts" if cp == "P" else "Calls"
    try:
        budget = budget or _account_budget()
        spot, exp, rows = contract_options(ticker, today, cp=cp)
    except Exception as e:
        return f"\n(couldn't pull {ticker} options: {e})"
    if not rows:
        return f"\nNo liquid {word.lower()} 9+ days out on {ticker} right now - skip it."
    d = date.fromisoformat(exp)
    lines = [f"\n{ticker} is ${spot:.2f}. {word} expiring {d.month}/{d.day} ({rows[0]['dte']} days):"]
    if cp == "P":
        lines.append("A put makes money when the stock FALLS.")
    any_odds = False
    for i, k in enumerate(rows, 1):
        per = k["mid"] * 100
        lines.append("")
        lines.append(f"{i}) {ticker} {k['strike']:g}{cp}  {d.month}/{d.day}   ~${k['mid']:.2f} each")
        if budget and per > budget:
            lines.append(f"   1 contract = ${per:,.0f}, over your ${budget:,.0f} budget")
        else:
            qty = max(1, int(budget // per)) if budget else 1
            cost = qty * per
            lines.append(f"   Buy x{qty} = ${cost:,.0f}")
            lines.append(f"   LOSS at stop (-20%): -${cost * 0.2:,.0f}  (sell at {k['mid'] * 0.8:.2f})")
            lines.append(f"   WIN at +20%: +${cost * 0.2:,.0f}   at +40%: +${cost * 0.4:,.0f}")
            lines.append(f"   Worst case: -${cost:,.0f} (expires worthless)")
        o = None
        try:
            o = win_odds(spot, k["strike"], k["mid"], k["dte"], k.get("iv"), cp=cp)
        except Exception as e:
            print(f"[odds] {e}")
        if o:
            any_odds = True
            lines.append(f"   ODDS: +20% before the stop {o['win']:.0%} | stopped out first {o['loss']:.0%} "
                         f"| neither {o['neither']:.0%}")
            lines.append(f"   +40% before the stop {o['win40']:.0%} | ends past breakeven {o['above']:.0%}")
        if cp == "P":
            be = k["strike"] - k["mid"]
            lines.append(f"   Profit at expiry only if {ticker} is below {be:.2f} ({be / spot - 1:+.1%})")
        else:
            be = k["strike"] + k["mid"]
            lines.append(f"   Profit at expiry only if {ticker} is above {be:.2f} ({be / spot - 1:+.1%})")
        if k["oi"] < 100 or k["spread"] > 0.15:
            ease = "HARD to sell - thin"
        elif k["oi"] >= 500 and k["spread"] <= 0.10:
            ease = "easy to sell"
        else:
            ease = "OK to sell"
        if k.get("stale"):
            lines.append(f"   No live bid/ask yet - price is the last trade ({k['oi']:,} open contracts)")
        else:
            lines.append(f"   {ease} (bid/ask gap {k['spread']:.0%}, {k['oi']:,} open contracts)")
    if any(k.get("stale") for k in rows):
        try:
            from zoneinfo import ZoneInfo
            open_now = is_open_now(datetime.now(ZoneInfo("America/Chicago")))
        except Exception:
            open_now = False
        if open_now:
            lines.append("\nThe market is open, but Yahoo's option quotes run about 15 minutes behind and haven't "
                         "filled in yet, so these are last-trade prices. Send 'pick' again in a few minutes.")
        else:
            lines.append("\nThe market isn't open, so these are last-trade prices, not live bid/ask. "
                         "Planning only - send 'pick' again after the open.")
    if any_odds:
        lines.append("\nOdds are math from option prices (no edge assumed), not a prediction. "
                     "Good news and momentum can move them.")
    return "\n".join(lines)
