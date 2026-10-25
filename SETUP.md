# Setup guide (about 45 minutes)

## READ THIS FIRST - use at your own risk

This is a personal hobby project, shared as-is. It is **not financial advice**, it is not a
recommendation to buy or sell anything, and nobody has audited it. It can have bugs. Options
can lose 100% of what you put in. Only use money you can afford to lose, and paper-trade
(log pretend trades) for a while before using real money. The author is not responsible for
any losses or damages from using this software.

**The settings are the author's, not yours.** The rules, tickers, trading window, stops and
sizing in this code reflect one person's account and habits. Before you rely on it, go through
and change them to fit YOU:

- **Tickers:** `WATCHLIST` in tradebot.py and `my_tickers` in newswatch.py
- **Rules and trading window:** `RULES`, `TRADE_DAYS` and `DAY_START` in tradebot.py
  (the default is: Mon and Tue start at 11 AM Central, Wed-Fri start at 9 AM, flat by Friday, long calls and puts only)
- **Stops and targets:** `STOP`, `TARGETS` and `SOFT` in tradebot.py (-20%, +20%/+40%, +/-10%)
- **Position size:** set with `balance` and the cash/slots settings (default 50% cash, 2 positions)
- **Fees:** `FEE_PER_CONTRACT` in tradebot.py ($0.65 each way is a Fidelity-style number)
- **Time zone:** `America/Chicago` in tradebot.py, newswatch.py and market_extras.py
- **Account type:** it assumes a cash account (T+1 settlement). Margin accounts work differently.

The bot only advises. It never places orders. You are responsible for every trade you make.

Works on Ubuntu / Debian / Pop!_OS. macOS works for the Python parts but the systemd steps
(Step 8) are Linux only. Total disk: about 6 GB, mostly the Ollama model.

What runs where:

    your phone (ntfy app) <--> ntfy server <--> tradebot.py  (commands + alerts + position watcher)
                                            \--> newswatch.py (news scanner, uses Ollama)

The bot never trades for you. You place orders at your broker, then tell the bot.

## Step 1. Python and the code

    sudo apt update && sudo apt install -y git python3 python3-venv python3-pip
    git clone <your repo url> ~/trading
    cd ~/trading
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

## Step 2. Ollama (reads the news headlines)

    curl -fsSL https://ollama.com/install.sh | sh
    ollama pull qwen2.5:7b
    ollama run qwen2.5:7b "say hi"     # type /bye to leave

A 7B model wants roughly 8 GB of RAM (or a GPU with 6 GB+). If it is too slow, you can still
use the bot without newswatch.py; you just will not get news signals.

## Step 3. ntfy (the messaging between your phone and the bot)

Install ntfy on the same machine by following https://docs.ntfy.sh/install/ (Debian/Ubuntu
package). Then make it listen on port 2586:

    sudo nano /etc/ntfy/server.yml

Set (uncomment) this line, save, and restart:

    listen-http: ":2586"

    sudo systemctl enable --now ntfy
    sudo systemctl restart ntfy
    curl -d "hello" localhost:2586/alerts     # should print JSON, no error

## Step 4. Reach it from your phone (Tailscale, recommended)

Tailscale gives your machine a private address your phone can reach from anywhere, without
opening any ports to the internet.

    curl -fsSL https://tailscale.com/install.sh | sh
    sudo tailscale up
    tailscale ip -4                            # note this address, like 100.x.y.z

Install Tailscale on your phone too and sign in to the same account.

IMPORTANT: anyone who can reach port 2586 can send the bot commands. Keep it on Tailscale or
your home network. Never forward that port on your router.

## Step 5. Phone app

1. Install the ntfy app (Android: Play Store / F-Droid. iPhone: App Store, see the ntfy docs
   about instant delivery on iOS).
2. In the app settings, add your server: http://100.x.y.z:2586  (your Tailscale address)
3. Subscribe to two topics on that server: `alerts` and `trade`.
   - `alerts` is where the bot talks to you.
   - `trade` is where you type commands to the bot.

Test: publish "help" in the `trade` topic (the message box) once the bot is running (Step 7).

## Step 6. Optional: Alpaca keys for near-real-time prices

Without them the bot uses Yahoo, which is about 15 minutes delayed and often empty right
at the open. Free keys fix that.

1. Make a free account at https://alpaca.markets (a paper account is fine, no money needed).
2. Generate an API key and secret in the dashboard.
3. Put them in the file. Type them yourself; never paste secrets into chats or commit them:

       cp alpaca.env.example alpaca.env
       chmod 600 alpaca.env
       nano alpaca.env

## Step 7. First run (manual test)

Open two terminals in ~/trading:

    source venv/bin/activate && python3 tradebot.py
    source venv/bin/activate && python3 newswatch.py

From your phone, send these to the `trade` topic:

    help
    balance 4500
    pick SPY

If `help` answers on the `alerts` topic, you are connected. Stop both with Ctrl+C.

## Step 8. Run it all the time (Linux, systemd)

    bash install_services.sh

This installs and starts `tradebot` and `newswatch` as services, plus a 10-minute
`healthcheck` timer that alerts you if something stops. Check with:

    systemctl is-active tradebot newswatch ntfy ollama
    journalctl -u tradebot -n 30 --no-pager

## Using it day to day

    pick SPY                 the specific call to buy, sized to your balance
    buy SPY 750C 5/22 x5 @4.10      log a buy (use the price you actually paid)
    list                     open positions with live bid and P/L after fees
    live NVDA                the stock right now with trend arrows
    signal NVDA              is a news signal still strong, fading or gone
    news / news NVDA         what the news scanner found
    sell SPY 750C @4.80      log a sell
    cash                     settled cash you can use today (cash-account rules)
    stats                    win rate, profit factor, streaks
    rules                    the rulebook

You can paste an order from Fidelity instead. Always include the "Filled at $..." line,
because the bot needs your real fill price:

    Buy to Open
    5 Contracts SPY Oct 26 2026 775 Calls Limit at $3.79 (Day)
    Filled at $2.72

## Make it yours

- Time zone: `America/Chicago` in tradebot.py, newswatch.py, market_extras.py
- Rules and trading window: RULES and TRADE_DAYS in tradebot.py
- Watchlist: WATCHLIST in tradebot.py and my_tickers in newswatch.py
- Fees: FEE_PER_CONTRACT in tradebot.py

After editing code: `sudo systemctl restart tradebot newswatch`

## Troubleshooting

- Bot silent: `systemctl is-active tradebot`, then `journalctl -u tradebot -n 50 --no-pager`
- "activating" over and over: a Python error on startup; the journal shows it
- No news alerts: `systemctl is-active ollama` and `ollama list` (model must be installed)
- Prices look stale or "no quote": normal before 8:30 AM Central; also check alpaca.env
- Phone not receiving: check the app is pointed at your Tailscale address and both topics
  are subscribed; `curl -d test http://100.x.y.z:2586/alerts` from another device

## Updating

    cd ~/trading && git pull && sudo systemctl restart tradebot newswatch

## Disclaimer

Educational and personal use. Not financial advice. Options can expire worthless; only use
money you can afford to lose, and paper-trade first.

## Enjoying it?

It's free. If it saved you time, an optional tip is welcome: https://venmo.com/u/chipps2069 (voluntary, for the software only, not for advice) <!-- tip -->
