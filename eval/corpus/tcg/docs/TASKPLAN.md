# TCG Portfolio & Market Agent — Claude Code Task Plan (Phase 1)

A phased, verifiable build plan for the MVP. Companion to `SPEC.md` (the design
rationale). This file is self-contained: the concrete contracts are restated
inline so it can be built from directly.

## Decisions already locked (do not re-litigate)
- **Scope:** Pokémon cards only, one marketplace/data source, for now.
- **Data source:** `tcgapi.net` (its `/v1/comps` endpoint gives real eBay sold
  comps on the free tier). `pokemontcg.io` is the fallback for card metadata.
- **No orchestration framework.** A plain loop + one structured LLM call per
  card. Do not add LangGraph/CrewAI/etc. — simplicity is intentional.
- **Storage:** SQLite (single file).
- **UI:** Streamlit.
- **Safety framing:** decision *support* only. The agent recommends and
  explains; the human acts. Never auto-buy/sell.
- **One data model, two views**, separated by a `status` flag (`watching` /
  `owned`).

## How to work this plan
- Do **one task at a time, in order.** Do not skip ahead or scaffold future
  phases early.
- After each task, run its **Acceptance check** and confirm it passes before
  moving on.
- **Build against the mock data source first** (Phase 2). Only wire the live API
  once the whole loop works on mock data.
- Stop at each **REVIEW GATE** and let the human read the code before continuing.
- Prefer clear, idiomatic, commented code over clever code — this is a portfolio
  project the human must be able to explain.

---

## Phase 0 — Project setup

### T0.1 — Repo scaffold
- Create: `src/`, `tests/`, `data/` (gitignored), `README.md`, `.gitignore`,
  `.env.example`, `requirements.txt`.
- Python 3.11+, a virtualenv, `requirements.txt` with: `requests`, `pydantic`,
  `python-dotenv`, `streamlit`, and an LLM SDK (`anthropic` or `openai`).
- `.env.example` lists `TCGAPI_KEY=` and `LLM_API_KEY=`. Real `.env` is gitignored.
- **Acceptance:** `pip install -r requirements.txt` succeeds; `python -c "import
  streamlit, pydantic, requests"` runs clean.

### T0.2 — Config module
- `src/config.py` loads env vars via `python-dotenv`, exposes typed settings,
  fails loudly with a clear message if a required key is missing.
- **Acceptance:** importing config with a missing key raises a readable error.

---

## Phase 1 — Data model

### T1.1 — SQLite schema
- `src/db.py`: create two tables and a connection helper.
- `cards`: `id` (pk), `name`, `set_code`, `grade`, `status` (`watching`|`owned`),
  `target_buy` (nullable), `target_sell` (nullable), `purchase_price` (nullable),
  `purchase_date` (nullable), `notes`.
- `price_history`: `id` (pk), `card_id` (fk), `run_at` (datetime),
  `best_listing`, `sold_avg`, `sold_count`.
- **Acceptance:** running an init function creates `data/app.db` with both tables;
  a schema inspection shows the expected columns.

### T1.2 — Card management CLI
- `src/manage.py`: add a card, list cards, update status, set purchase info.
  e.g. `python -m src.manage add --name "Charizard ex 199/165" --set 151 --grade "PSA 10" --status watching`
- Seed 5–10 real cards (mix of `watching` and `owned`, with purchase prices on
  the owned ones).
- **Acceptance:** `list` shows the seeded cards; an owned card has a
  purchase_price; a card can be flipped `watching` → `owned`.

**REVIEW GATE 1** — human confirms the schema and seed data look right.

---

## Phase 2 — Ingestion (adapter pattern)

### T2.1 — Types + source interface
- `src/market/types.py`: Pydantic models for `Listing`, `SoldComp`, `MarketData`.
- `MarketData` normalized shape (the contract the rest of the system depends on):
```json
{
  "best_listing": 412.00,
  "listings": [{"price": 412.00, "condition": "PSA 10", "url": "..."}],
  "sold_comps": [{"price": 598.00, "date": "2026-09-24"}],
  "sold_avg": 598.00,
  "sold_count": 5
}
```
- `src/market/base.py`: `MarketSource` abstract base with
  `fetch_market_data(name, set_code, grade) -> MarketData`.
- **Acceptance:** models validate the example payload; the ABC can't be
  instantiated directly.

### T2.2 — Mock source (build everything against this first)
- `src/market/mock.py`: `MockSource(MarketSource)` returning deterministic canned
  `MarketData` for the seeded cards (include at least one clear under-market buy,
  one over-market sell, and one thin-data case with `sold_count` ≤ 2).
- **Acceptance:** `MockSource().fetch_market_data(...)` returns valid `MarketData`
  for each seeded card.

### T2.3 — Real source (tcgapi.net)
- `src/market/tcgapi.py`: `TcgApiSource(MarketSource)` calling the real API —
  listings/market price plus the `/v1/comps` sold data — and mapping the response
  onto `MarketData`.
- Handle: auth header from config, HTTP errors, empty results, **rate limiting**
  (respect the free-tier limit; add a small delay / simple retry with backoff).
- Match the right card variant and grade (raw vs graded are different markets).
- Do **not** call the live API in a loop during development — cache responses to
  `data/` while iterating.
- **Acceptance:** a single real call for one seeded card returns a populated
  `MarketData`; a bad card name degrades gracefully (empty, not a crash).

**REVIEW GATE 2** — human reads the normalization + grade-matching logic and can
explain how a listing is matched to a comp. (This is interview-critical code.)

---

## Phase 3 — Memory

### T3.1 — Snapshot writer + history reader
- `src/memory.py`: `append_snapshot(card_id, market_data)` writes a
  `price_history` row; `get_history(card_id, days=30)` returns recent snapshots.
- **Acceptance:** two runs produce two history rows per card; `get_history`
  returns them in time order.

---

## Phase 4 — Decision loop

### T4.1 — Decision function + prompt
- `src/decide.py`: `decide(card, market, history) -> Decision` makes one LLM call
  with a tight prompt and returns validated structured output.
- Decision JSON (validate with Pydantic; reject/repair malformed output):
```json
{
  "card_id": 12,
  "name": "Charizard ex 199/165",
  "grade": "PSA 10",
  "verdict": "buy",
  "best_listing": 412.00,
  "sold_avg": 598.00,
  "vs_market_pct": -31.1,
  "trend_30d": "up",
  "confidence": "high",
  "reason": "Cheapest listing $412; last 5 PSA 10 sales avg $598 (~31% under). Sold price trended up while supply thinned.",
  "unrealized_pnl_pct": null
}
```
- Prompt must: compute the edge (listing vs sold avg), read the trend from
  history, weigh supply, and — for owned cards — factor cost basis. It must
  justify the verdict in 1–2 sentences using the real numbers, and lower
  confidence when data is thin/contradictory. Return JSON only.
- **Acceptance:** run against `MockSource` — the under-market card yields `buy`,
  the over-market owned card yields `sell`, the thin-data card yields lower
  `confidence`. Output always validates.

### T4.2 — Run loop
- `src/run.py`: iterate active cards → fetch (via the configured source) →
  get history → decide → append snapshot → collect decisions → write results to
  `data/decisions.json`. Source is swappable (mock vs real) via config/flag.
- **Acceptance:** `python -m src.run --source mock` prints/writes a decision per
  seeded card end-to-end.

### T4.3 — P&L for owned cards
- In the run (or a helper): for `owned` cards compute
  `unrealized_pnl_pct = (sold_avg - purchase_price) / purchase_price * 100`
  and an absolute figure; leave `null` for watched cards.
- **Acceptance:** owned cards show a sensible P&L; watched cards show `null`.

**REVIEW GATE 3** — human reviews the decision prompt and P&L math, runs the
loop on mock data, and confirms the verdicts are defensible. Then switch
`--source real` for a live smoke test on 2–3 cards.

---

## Phase 5 — Dashboard (Streamlit)

### T5.1 — Watchlist tab
- `app.py`: read `data/decisions.json` (+ history), filter `status = watching`.
- Table: card, verdict badge (buy/hold/sell), best listing, sold avg,
  vs-market %, 30-day sparkline. Below the table, an expandable "Why" per card.
- **Acceptance:** `streamlit run app.py` shows the watched cards with verdicts.

### T5.2 — Portfolio tab + summary strip
- Second tab, filter `status = owned`. Lead with a **summary strip**: total
  collection value, total unrealized P&L ($ and %), best/worst performer. Table
  adds a P&L column; verdict emphasis on sell/hold.
- **Acceptance:** the strip totals match the per-card numbers; P&L column populated.

### T5.3 — Sparklines
- Render the 30-day trend per card from `price_history` (small line per row).
- **Acceptance:** cards with ≥2 snapshots show a trend line.

**REVIEW GATE 4** — human looks at both tabs and sanity-checks against reality.

---

## Phase 6 — Polish
- `README.md`: what it is (decision *support*, not investment advice), setup,
  how to run, architecture summary, and a note on the adapter pattern.
- Basic error handling + logging on the run.
- (Optional) a scheduled run so history accumulates automatically.
- **Acceptance:** a fresh clone can be set up and run from the README alone.

---

## Standing rules for the agent (apply throughout)
- Recommend-and-explain only; never place trades or add write-actions to a
  marketplace.
- Build against `MockSource` before touching the live API; never hammer the live
  API in a dev loop (cache responses).
- All LLM output is validated against the Pydantic schema before use.
- Keep it simple: no orchestration framework, no multi-agent split, no premature
  abstraction beyond the `MarketSource` adapter.
- Stop at each REVIEW GATE; don't proceed past it without the human.
- Comment non-obvious decisions so the human can learn and defend the code.
