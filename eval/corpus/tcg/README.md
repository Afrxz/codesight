# TCG Portfolio & Market Agent

A decision-support agent for Pokémon cards. You keep two lists in a digital
binder:

- **My collection**: cards you own.
- **Watchlist**: cards you're thinking of buying.

Once a day the agent checks the market price of every card and gives each one a
**buy / hold / sell** recommendation, with a short reason that quotes the actual
numbers. For owned cards it also shows what they're worth and your profit or
loss against what you paid.

> ⚠️ **Decision support, not investment advice.** The agent recommends and
> explains; you decide and act. It never buys, sells or lists anything, and it
> has no access to any marketplace account.

![The binder: collection and watchlist in 9-pocket pages with verdict stickers](docs/binder.jpg)

![Card detail: verdict, market numbers, targets and price history](docs/binder-detail.jpg)

## Contents

1. [Install](#1-install)
2. [Try it in 2 minutes (demo, no keys)](#2-try-it-in-2-minutes-demo-no-keys)
3. [Set it up for your own cards](#3-set-it-up-for-your-own-cards)
4. [Using the binder](#4-using-the-binder)
5. [What the agent does on its own](#5-what-the-agent-does-on-its-own)
6. [Settings reference](#6-settings-reference)
7. [Price sources](#7-price-sources)
8. [Command-line reference](#8-command-line-reference)
9. [Measuring the agent (evaluation harness)](#9-measuring-the-agent-evaluation-harness)
10. [Crash-safe runs](#10-crash-safe-runs)
11. [MCP server](#11-mcp-server)
12. [Troubleshooting](#12-troubleshooting)
13. [How it works](#13-how-it-works)
14. [Project layout and tests](#14-project-layout-and-tests)

---

## 1. Install

**You need:** Python 3 (the project is developed and tested on Python 3.14),
and an internet connection for card art and live prices. Nothing else: no
database server, no Node.js, no build step.

```bash
git clone <this repo> TCG
cd TCG
python -m venv .venv
```

Activate the virtual environment:

- Windows (PowerShell): `.venv\Scripts\activate`
- macOS / Linux: `source .venv/bin/activate`

Then install the dependencies:

```bash
pip install -r requirements.txt
```

All your data lives in the `data/` folder inside the project (the database is
`data/app.db`). Your settings and API keys live in `.env`. Both are kept out of
git.

---

## 2. Try it in 2 minutes (demo, no keys)

The demo uses built-in sample prices, so it works offline and needs no
accounts:

```bash
python -m src.demo
```

```bash
python -m src.web --open
```

The first command loads 7 example cards, a month of sample price history and
one agent run. The second opens the binder at <http://127.0.0.1:8000>. Click any
card to see its recommendation and the evidence behind it.

> `python -m src.demo` **wipes the database** and reloads the examples. Don't
> run it once you've added your own cards.

Without an LLM key, recommendations come from the built-in rules (shown as
"rule-based"). They follow the same logic as the LLM prompt, just less
nuanced.

---

## 3. Set it up for your own cards

### Step 1: get free API keys

| Key | What it's for | Where to get it | Required? |
|---|---|---|---|
| **JustTCG** | Real prices by condition, including brand-new sets | [justtcg.com](https://justtcg.com) (free plan: 100 requests/day) | Recommended |
| **LLM** (DeepSeek by default) | LLM-written recommendations, and the agent's look-up tools | [platform.deepseek.com](https://platform.deepseek.com), or any OpenAI-compatible provider | Optional (rules are used without it) |
| pokemontcg.io | Higher rate limit for card search and art | [dev.pokemontcg.io](https://dev.pokemontcg.io) | Optional |
| tcgapi.net | Graded-card sold prices (Pro plan) | [tcgapi.net](https://tcgapi.net) | Only for graded cards |

### Step 2: open the binder and add your keys

```bash
python -m src.web --open
```

1. Click the **gear icon (top left)** to open **Settings**.
2. Under **API keys**, paste each key and press **Test**. That makes one live call to check the key.
3. Under **Agent runs → Market data**, choose **justtcg**.
4. Press **Save settings**.

Keys are saved to `.env` and never shown again (only "Saved · ends in …abcd").
Don't paste keys anywhere else.

### Step 3: clear the examples, add your cards

If you ran the demo, remove the example cards first: open each one and press
**Remove card**. Or start fresh by deleting `data/app.db`, which deletes
**everything**.

Then press **+ Add card** for each of your cards (see [Adding a card](#adding-a-card)).

### Step 4: run the agent

Press **Re-run agent**. Every card gets a recommendation in a few seconds. From
then on the agent runs itself once a day (see [Automatic daily run](#automatic-daily-run)).

---

## 4. Using the binder

Start it with `python -m src.web --open` (add `--port 8080` if 8000 is taken).
It only listens on your own computer (127.0.0.1).

### The top bar

| Control | What it does |
|---|---|
| **Gear (top left)** | Settings: keys, AI model, data source, decision rules, schedule, alerts |
| **Bell** | Alerts the agent raised (red badge = unread). Opening it marks them read; **Open** jumps to the card |
| **+ Add card** | Search the Pokémon card catalog and add a card |
| **Re-run agent** | Run the agent now on every card |

Under the bar:
- **Agent briefing**: the agent's summary of your whole collection. It also shows the schedule, whether the LLM can look things up, the agent's track record, and how the last run went (e.g. "Last run: 6/7 cards · couldn't price …").
- **Summary strip**: collection value, unrealized profit/loss, best performer, and how many buy/sell signals there are.
- **Tabs**: **All cards**, **Collection**, **Watchlist**. Use **‹ ›** to turn pages.

Each card shows a sticker: **BUY**, **HOLD** or **SELL**, always as a word plus
an icon, not just a color.

### Adding a card

1. Press **+ Add card** and type, for example:
   - a name: `charizard ex`, `umbreon vmax` (official names, not nicknames)
   - name and number: `pikachu 58`
   - number and set size: `199/165`
2. Results come from the pokemontcg.io catalog, newest sets first, with art and current prices. Pick the right printing.
3. Choose **My collection** or **Watchlist**, then fill in:
   - **Finish/printing** (Normal, Holofoil, Reverse Holofoil, …) and **Condition** (Near Mint to Damaged). Prices depend on both.
   - **Grade**: Raw (ungraded) or a slab grade like PSA 10.
   - **How you got it**:
     - **Bought it**: enter what you paid and the date.
     - **Pulled from a pack**: optional pack cost, which is usually $0, so its whole value is gain.
     - **Gift / trade**: optional value given up.
   - **Targets** (optional): a **buy target** for watched cards, a **sell target** for owned ones. The agent tells you when a price reaches them.
4. Press **Add to collection** / **Add to watchlist**. The card gets its recommendation right away.

If the catalog search is temporarily down, the box says so and offers **Try again**.

### The card detail view

Click a card. Use **← / →** to move between cards and **Esc** to close.
`#card-5` in the address bar opens card 5 directly.

| Section | What you see |
|---|---|
| **Recommendation** | The verdict, its confidence (high / medium / low) and the reason, with real numbers. Also whether it was written by the LLM or the rules |
| **How the agent decided** | What the agent looked up before deciding (if it used its tools) |
| **Market numbers** | Market price (sold average), cheapest listing, cheapest vs sold average, 30-day trend, where the price came from and when |
| **Your copy** | What you paid / how you got it, what it's worth now, profit or loss |
| **Your targets** | Your buy or sell target and how far the price is from it |
| **Price history** | Chart with **7d / 30d / 90d / 1y** ranges and hover values; **Show as table** for the numbers. With JustTCG you get full history from day one; otherwise it builds from the agent's daily checks |
| **Prices by condition** | The same card's price in every condition and printing (JustTCG) |
| **Recent sales / Cheapest listings** | The actual evidence behind the recommendation, when the source provides it |

Buttons in the detail view:
- **Edit card**: change condition, finish, grade, targets, what you paid or how you got it, notes, or move it between collection and watchlist. The recommendation updates right away.
- **Your price**: give a card your own value when no source has a price yet (common for brand-new sets). It's used only when there's no market price, labelled as your estimate, kept at low confidence, and never mixed into the price history.
- **Remove card**: deletes the card and its history. This can't be undone.

### Track record

**Track record →** (in the briefing) shows how the agent's past calls turned
out 7 and 30 days later:
- a **buy** was right if the price then rose 5% or more;
- a **sell** was right if it fell 5% or more;
- a **hold** was right if it stayed within ±10%.

It's broken down **by verdict**, **by confidence** and **LLM vs rules**. Grading
starts once calls are 7 days old.

### Simpler table view

`streamlit run app.py` opens the original table dashboard. It reads the same
data.

---

## 5. What the agent does on its own

### Automatic daily run

While the binder is open, the agent runs once a day at **Settings → Agent →
Daily run time** (default 09:00, your local time). If you open the binder after
that time and today's run hasn't happened, it runs straight away.

To run on schedule **without the binder open**:

```bash
python -m src.scheduler
```

Or use your operating system's scheduler (Windows Task Scheduler / cron) with
`python -m src.run`; see [Scheduled runs](#scheduled-runs-with-your-os).

Each run, for every card, it:
1. fetches today's price;
2. decides buy / hold / sell;
3. saves the price (so trends build up) and the recommendation (so it can be graded later);
4. writes a collection briefing;
5. raises alerts.

### Alerts

After each run the agent raises an alert when:
- a price moved **15% or more in a week** (adjustable);
- a verdict **changed** (e.g. HOLD → SELL);
- a price newly reached **your buy or sell target**;
- a watched card is **well below its 30-day average** (a dip).

The same alert isn't repeated within 7 days. Alerts appear under the bell.
They can also go to **Discord or Slack**: paste an incoming-webhook URL in
**Settings → Agent** and press **Send test**.

### Looking things up (with an LLM key)

When the facts are clear, the LLM decides straight away. When they conflict, it
can make up to 3 look-ups per card (adjustable, 0 turns this off):

| Tool | Answers |
|---|---|
| `price_history` | Is this drop unusual over a longer period? |
| `compare_variants` | What do other conditions and printings sell for? |
| `second_source` | Does another price source agree? |
| `set_movement` | Is the whole set falling, or just this card? |
| `web_search` (optional) | News such as reprint announcements, via your own SearXNG |

Every look-up shows under **How the agent decided**. Look-ups that hit an API
share a per-run budget, which protects free quotas.

**Web search (optional).** Run your own [SearXNG](https://docs.searxng.org/)
(e.g. with Docker) and enable JSON output in its `settings.yml`
(`search: formats: [html, json]`). Then put its URL (e.g. `http://localhost:8080`)
in **Settings → Agent**. Results are treated as untrusted text: they can never
change a computed number.

### Collection review

The briefing looks at your collection as a whole:
- concentration (too much value in one card);
- cards that are falling;
- owned cards with no purchase price recorded.

With a **Monthly buying budget** (Settings → Agent), it suggests how to spend it
across your watchlist, strongest buy signals first.

### Learning from its record

Once enough calls are graded, the agent's 30-day track record is fed back into
the LLM's instructions. The model is told to be more cautious with any verdict
that has been wrong more often than right.

---

## 6. Settings reference

Everything here is in the **Settings** panel and is saved to `.env`. Changes
apply immediately, with no restart needed. You can also edit `.env` by hand;
`.env.example` lists every setting. Every section except API keys has a
**Restore defaults** button.

| Group | Setting (`.env` name) | Default | What it does |
|---|---|---|---|
| API keys | LLM API key (`LLM_API_KEY`) | – | LLM-written recommendations. Without it, the rules are used |
| | JustTCG API key (`JUSTTCG_API_KEY`) | – | Prices by condition, incl. new sets |
| | pokemontcg.io key (`POKEMONTCG_API_KEY`) | – | Optional; raises the rate limit |
| | TCG API key (`TCGAPI_KEY`) | – | Only for the tcgapi.net source |
| AI model | Provider / base URL (`LLM_BASE_URL`) | `https://api.deepseek.com` | Any OpenAI-compatible endpoint |
| | Model (`LLM_MODEL`) | `deepseek-chat` | |
| | Price per 1M input / output tokens (`LLM_PRICE_IN_PER_M`, `LLM_PRICE_OUT_PER_M`) | – | Only used to report costs in evals |
| Agent runs | Market data (`RUN_SOURCE`) | `mock` | `mock`, `pokemontcg`, `justtcg`, `tcgapi` or `mcp` |
| | MCP server wraps (`MCP_SOURCE`) | `justtcg` | The source behind the MCP server |
| | Use the LLM (`RUN_LLM`) | `auto` | `auto` = if a key is set; `on` = fail without one; `off` = rules only |
| Decision rules | Buy when cheapest listing is under market by (`BUY_EDGE_PCT`) | 15% | |
| | …with high confidence when under by (`STRONG_EDGE_PCT`) | 20% | |
| | Consider selling once you're up (`BIG_GAIN_PCT`) | 50% | Sell if the trend also turns down |
| | Count a price move as up/down from (`TREND_THRESHOLD_PCT`) | 3% | Smaller 30-day moves count as flat |
| Agent | Run automatically (`AUTO_RUN`) | `on` | Daily run while the binder is open |
| | Daily run time (`RUN_TIME`) | `09:00` | Local time, 24-hour clock |
| | Let the LLM look things up (`AGENT_TOOLS`) | `on` | |
| | Look-ups per card, at most (`AGENT_TOOL_BUDGET`) | 3 | 0 = basic facts only |
| | Alert when a price moves by (`ALERT_MOVE_PCT`) | 15% | Over 7 days; also the dip threshold |
| | Send alerts to (`ALERT_WEBHOOK_URL`) | – | Discord or Slack webhook |
| | Monthly buying budget (`BUY_BUDGET`) | 0 | 0 = no budget |
| | SearXNG URL (`SEARXNG_URL`) | – | Enables `web_search` |
| Live data | Reuse live API responses for (`CACHE_TTL_HOURS`) | 24 h | Saves your quota; 0 = always fresh |
| | Wait between live requests (`REQUEST_INTERVAL_S`) | 1 s | Rate-limit spacing |

The rules engine and the LLM's instructions read the same decision-rule
numbers, so they always apply the same thresholds.

---

## 7. Price sources

| Source | Cost | Covers | Best for |
|---|---|---|---|
| `justtcg` | Free key, 100 requests/day | Raw cards, every condition and printing, **including new sets** | **Your real collection (recommended)** |
| `pokemontcg` | Free, no key | Raw cards (TCGplayer prices); newest sets can lag | A no-signup alternative |
| `tcgapi` | Free / Pro | Raw and graded; eBay sold comps need Pro | Graded slabs |
| `mock` | Free, offline | Only the 7 example cards | Demos and development |
| `mcp` | Same as what it wraps | Whatever `MCP_SOURCE` covers (JustTCG by default) | Getting prices through the project's MCP server ([section 11](#11-mcp-server)) |

What to know about each:

- **JustTCG.** It gives the price for your card's exact condition and
  printing, plus its 30-day change, so trends work from the first run. It
  reports no individual listings or sales count, so confidence tops out at
  "medium". It's built for the free quota: each card's id is looked up once
  and cached, prices are fetched 20 cards per request, and responses are reused
  for 24 hours. A 20-card collection costs about one request a day.
- **pokemontcg.io.**
  - The TCGplayer Market Price is used as the sold average.
  - The cheapest Near Mint TCGplayer Direct listing is the cheapest listing.
  - Prices are per finish, so set each card's finish.
  - It reports no sales count (confidence up to medium) and has no graded prices.
- **tcgapi.net.** Card search with recent sales is on the free plan. Graded
  sold comps need the Pro plan. Without it, the agent falls back to raw sales
  and says so.
- **History is kept per source.** Switching sources never mixes sample and real
  prices into one trend.

You can switch sources for one run from the command line:

```bash
python -m src.run --source justtcg
```

---

## 8. Command-line reference

Everything the binder does can also be done from a terminal (run from the
project folder, with the virtual environment active).

**Running the agent**

| Command | What it does |
|---|---|
| `python -m src.run` | One run with your saved settings |
| `python -m src.run --source justtcg --llm on` | Choose the source (`mock`, `pokemontcg`, `justtcg`, `tcgapi`, `mcp`) and LLM use (`auto`, `on`, `off`) |
| `python -m src.run --limit 3` | Only the first 3 cards |
| `python -m src.scheduler` | The daily schedule without the binder |
| `python -m mcp_server.server` | The MCP server (stdio); `tcg-mcp-server` after `pip install -e .` |
| `python -m src.web [--port 8000] [--open]` | The binder |
| `streamlit run app.py` | The table dashboard |
| `python -m src.demo [--days 30] [--llm off]` | Reload example cards + sample history (**wipes data**) |

**Managing cards** (`python -m src.manage …`)

| Command | What it does |
|---|---|
| `list [--status watching\|owned]` | List cards with their ids |
| `add --name "Pikachu 58/102" --set BS --grade Raw --status owned --purchase-price 8` | Add a card (also `--target-buy`, `--target-sell`, `--purchase-date`, `--notes`, `--variant`, `--image-url`) |
| `update-status --id 4 --status owned` | Move between watchlist and collection |
| `set-purchase --id 4 --price 98.50 --date 2026-10-19` | Record what you paid (marks it owned) |
| `set-variant --id 4 --variant reverseHolofoil` | Set the finish (`auto` = regular printing) |
| `set-price --id 4 --price 25` / `--clear` | Your own price, for cards with no market price |
| `set-image --id 4 --url …` / `fetch-images` | Set card art, or find art for cards without any |
| `remove --id 4` | Delete a card and its history |
| `seed [--force]` | Load the example cards (`--force` wipes first) |

A card added from the command line needs its collector number in the name
(e.g. `Snorlax 051/165`) so live sources match the right printing. Cards
added through the binder are matched exactly.

### Scheduled runs with your OS

- **Windows (Task Scheduler):**
  `schtasks /create /tn "TCG agent" /sc daily /st 09:00 /tr "cmd /c cd /d C:\path\to\TCG && .venv\Scripts\python.exe -m src.run"`
- **macOS / Linux (cron):**
  `0 9 * * * cd /path/to/TCG && .venv/bin/python -m src.run >> data/cron.log 2>&1`

---

## 9. Measuring the agent (evaluation harness)

The agent is tested against a fixed suite of **40 cases** in `src/eval/suite/`:
- clear buys and clear sells;
- holds;
- thin and missing data;
- averages inflated by one outlier sale;
- cheapest listings that are damaged copies.

Each case has frozen market data and the expected answer. The harness runs the
real decision code on every case k times, grades every attempt, and reports
the results.

```bash
python -m src.eval.run_eval --k 3
```

That runs the **rules engine**: free, offline and fully repeatable. To test the
real LLM agent (uses your LLM key: 120 attempts, a few LLM calls each):

```bash
python -m src.eval.run_eval --k 3 --llm on --judge --variant my-change
```

The console shows:
- **success rate**;
- **pass@3** (passed at least once) and **pass^3** (passed every time);
- results by category;
- failure-mode rates (made-up tool arguments, loops, giving up, invalid output, over budget);
- tool-call failure rate;
- how often the LLM silently fell back to the rules;
- how often a code guardrail changed the verdict;
- average steps, tokens and time.

`--judge` adds an LLM score for the reasons. It's kept separate from pass/fail.

Each run is saved in `data/eval/<run_id>/`: `results.jsonl` (every attempt,
with a full trace), `summary.json` and `meta.json`. To check a change didn't
make anything worse, compare the latest run to a named baseline:

```bash
python -m src.eval.regress --set-baseline main 20261020-081732-rules-comparable-evidence
```

```bash
python -m src.eval.regress --baseline main
```

This shows each metric as improved, regressed or the same, and exits with an
error if anything regressed. Measured results, including the one improvement
made from the eval, are in [data/eval/RESULTS.md](data/eval/RESULTS.md).

---

## 10. Crash-safe runs

Every run is recorded card by card in a run ledger in the database. If the
computer crashes, the window closes, or a run is killed halfway:

- **Nothing is lost or doubled.** A card's price, its recommendation and its
  "done" mark are saved together in one step, so either all of them are saved
  or none are.
- **It picks up where it stopped.** The next run (Re-run, the schedule, the
  command line, or simply opening the binder) finishes only the cards that
  weren't done yet.
- **One bad card doesn't stop the run.** A temporary error (timeout, dropped
  connection, outage) is retried twice, after 1 s and then 2 s. A card that
  still fails is skipped and named in the briefing ("couldn't price …"), and
  it's tried again on the next run.

To see it work, this test kills a real agent process halfway, restarts it, and
checks every card was done exactly once:

```bash
pytest tests/test_crash_resume.py -v -s
```

---

## 11. MCP server

`mcp_server/` exposes the agent's market-data layer as a standalone
[MCP](https://modelcontextprotocol.io) server. Any MCP client, such as Claude
Desktop, another agent or your own script, can look up Pokémon card prices in
the same normalized format the agent uses. The agent itself can also get its
prices through it. The server wraps the existing sources (JustTCG by default),
caches included; it doesn't re-implement them.

| Tool | What it does |
|---|---|
| `get_market_data` | One card's `MarketData`: market price, 30-day change and average, prices by condition, listings and sales when available, source note |
| `get_market_data_batch` | Up to 100 cards in one call (JustTCG then fetches 20 per request) |
| `search_card` | Find cards in the catalog; use a result's id as `catalog_id` |

The inputs and outputs have precise schemas. Errors come back as structured
tool errors: `invalid_argument` (bad input) or `not_found` (no price for this
card).

**Start it** (over stdio, which is how MCP clients launch it). It uses your
JustTCG key from `.env`:

```bash
python -m mcp_server.server
```

`MCP_SOURCE=mock` serves the offline sample data instead. After `pip install -e .`
it's also the command `tcg-mcp-server`.

**Use it from Claude Desktop:** add this to `claude_desktop_config.json`, with
your own paths:

```json
{
  "mcpServers": {
    "tcg-market-data": {
      "command": "C:\\path\\to\\TCG\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\TCG\\mcp_server\\server.py"]
    }
  }
}
```

**Let the agent use it:**

```bash
python -m src.run --source mcp
```

Or choose **Settings → Agent runs → Market data: mcp**, and pick what the
server wraps under **MCP server wraps** (`MCP_SOURCE`).

- MCP only changes how prices travel, not what they are, so the run continues
  the wrapped source's price history: trends, track record and charts carry on.
- On a copy of a real collection, `--source mcp` and `--source justtcg` gave
  identical market data and identical decisions, with no extra API requests.

Full details, a copy-paste Python client example and the Inspector command:
[mcp_server/README.md](mcp_server/README.md).

---

## 12. Troubleshooting

| Problem | What to do |
|---|---|
| A card says **"No market price yet"** | The source has no price for it (common for brand-new sets on pokemontcg.io). Switch to **justtcg**, or set **Your price** on the card |
| Recommendations say **"rule-based"** | No LLM key is set, or it failed. Check it with **Test** in Settings; the rules take over automatically if the LLM is unavailable |
| **"couldn't price …"** in the briefing | That card failed after retries (e.g. the source was down). Nothing to do: it's tried again next run |
| Card search says it's unavailable | pokemontcg.io has occasional outages; press **Try again** in a minute |
| The binder won't start ("address in use") | Another program uses port 8000: `python -m src.web --port 8080 --open` |
| JustTCG says the quota is low | The free plan allows 100 requests/day. Responses are reused for 24 h (`CACHE_TTL_HOURS`), so normal daily use needs very few |
| The daily run didn't happen | **Run automatically** must be on, and the binder (or `python -m src.scheduler`) must be running at that time. The briefing shows the next run time and any error |
| I want to start over | Delete `data/app.db` (this deletes all cards and history), or run `python -m src.demo` for the examples |
| Back up my data | Copy the `data/` folder and `.env` somewhere safe |

Logs from every run are in `data/run.log`.

---

## 13. How it works

```
                 ┌──────────── src/run.py (plain loop, one pass per card, checkpointed) ───────────┐
cards (SQLite) ─▶│ fetch (retry) ─▶ history ─▶ decide ─▶ snapshot + recommendation + checkpoint  │─▶ decisions.json ─▶ binder (src/web.py + web/)
                 └──────┬────────────────────────┬──────────────────────────┬────────────────────┘
               MarketSource (adapter)       src/decide.py               src/ledger.py
               mock · pokemontcg ·          facts in Python → LLM       runs + run_progress:
               justtcg · tcgapi ·           judgment (+ tools) →        resume, exactly-once
               mcp (→ mcp_server/)
                                            validation → guardrails
```

The key design decisions:

- **Python does the math, the LLM does the judgment.** The edge vs market, the
  trend, the price statistics and the P&L are computed in code and handed to
  the model as facts. The model returns only a verdict, a confidence and a
  reason, so every number shown comes from code and none can be made up.
- **All LLM output is validated.**
  - It's checked with Pydantic and against the verdicts allowed for the card (you can't "sell" a card you only watch).
  - A bad reply gets the error back once to fix.
  - If it's still bad, the rules decide, and the decision is labelled as rule-based.
- **Guardrails in code, not just in the prompt:**
  - Thin data (2 sales or fewer) is always low confidence, and no price at all means "hold".
  - No sell into the bottom of a crash on trend alone.
  - A "buy" needs comparable evidence. If the cheapest listing is a worse copy (damaged, another grade, an "any condition" price) or one outlier sale inflates the average, the like-for-like edge must clear the same bar, or it becomes "hold" with the reason stated.
  - Every override is recorded on the decision.
- **One abstraction: the market-source adapter.** Each source turns its API
  into the same `MarketData`, so everything downstream (memory, decisions,
  binder, MCP server) is source-agnostic. Adding a marketplace means writing one
  class.
- **Raw and graded are different markets.** Comps are kept only when the grade
  matches exactly.
- **No orchestration framework.** The agent is a plain loop. The resilience is
  hand-rolled, with two SQLite tables and one transaction per card, so all of
  it can be explained line by line.
- **The binder is safe by construction:**
  - It uses the standard-library HTTP server and binds to 127.0.0.1 only.
  - It serves a fixed whitelist of files.
  - Actions that change data need a custom header, which blocks cross-site requests.
  - All text is inserted as text, never as HTML.

| Module | Responsibility |
|---|---|
| `src/run.py` | The run loop, retry, logging, output |
| `src/decide.py` | Facts, LLM call and tool loop, validation, guardrails |
| `src/tools.py` | The agent's read-only tools and per-run budget |
| `src/ledger.py` | Run ledger: checkpoints, resume, exactly-once writes |
| `src/scheduler.py` | Daily schedule (in the binder or headless); resumes interrupted runs |
| `src/market/` | `MarketData` types, the `MarketSource` adapter and the four sources |
| `src/memory.py` | Daily price snapshots per source |
| `src/evaluate.py` | Records every call and grades it 7 / 30 days later |
| `src/alerts.py` | Change detection, cooldowns, webhook delivery |
| `src/portfolio.py` | Collection review, budget plan, briefing |
| `src/catalog.py` / `src/images.py` | Card search and art (pokemontcg.io) |
| `src/config.py` | Settings, `.env` handling, validation |
| `src/db.py` | SQLite schema and migrations |
| `src/manage.py` | Card management CLI and example data |
| `src/web.py` + `web/` | The binder: local server + plain HTML/CSS/JS, no build step |
| `src/eval/` | Evaluation harness: suite, trace, graders, detectors, runner, report, regression |
| `mcp_server/` | The MCP server over the market-data layer (its own [README](mcp_server/README.md)) |
| `src/market/mcp_source.py` | `McpSource`: the agent's client for that server |

---

## 14. Project layout and tests

```
TCG/
├── web/              binder UI (index.html, binder.css, binder.js)
├── app.py            Streamlit table dashboard
├── src/              the agent (see the module table above)
│   ├── market/       data sources
│   └── eval/         evaluation harness + suite/ (40 cases)
├── mcp_server/       MCP server + its README
├── pyproject.toml    packaging (installs the tcg-mcp-server command)
├── tests/            pytest suite: offline (fake LLM, fake HTTP, temp databases)
├── docs/             build plan + screenshots
├── .env.example      every setting with its default
└── data/             your data, kept out of git: app.db, decisions.json, cache/, run.log, eval/
                      (only data/eval/RESULTS.md is tracked)
```

Run the tests:

```bash
pytest
```

All tests run offline. They never use your real database or API keys, and
they include a real kill-and-resume test and MCP server/client tests
(including decision parity through MCP).
