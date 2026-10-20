# Eval results — baseline vs one improvement

All numbers below are measured from the eval runs named in each table
(`data/eval/<run_id>/summary.json`, recomputable from `results.jsonl`).
Suite: 40 frozen cases in 7 categories, k = 3 attempts each (120 attempts per run).
Decision settings pinned to defaults. LLM: `deepseek-chat`, temperature 0, agent
mode (tool calls, budget 3). Cost is not reported: no per-token price was configured.

## What the baseline exposed

Both decision paths failed the same two categories, every attempt:

| Category | Rules baseline | LLM baseline |
|---|---|---|
| `damaged_underprice`: cheapest listing is a Damaged / Heavily Played / "any condition" copy priced against Near Mint sales | 0.0% | 0.0% |
| `outlier_comps`: one far-off sale inflates `sold_avg`, so a normal listing looks like a big discount | 0.0% | 0.0% |

Both called these "buy". The plain edge, `(best_listing - sold_avg) / sold_avg`,
can't tell a like-for-like discount from a worse copy or a skewed average.

## Run B: prompt-only change (the 7.7 regression check)

I told the model, in the prompt, to compare `sold_avg` against the individual
recent sales and to ignore an outlier-inflated average. No code changes.

| Metric | LLM baseline `20261020-081046-llm-baseline` | Prompt v2 `20261020-081233-llm-prompt-v2` |
|---|---|---|
| Success rate | 74.2% | 76.7% |
| pass@3 | 75.0% | 77.5% |
| pass^3 | 72.5% | 75.0% |
| outlier_comps | 0.0% | 13.3% |
| damaged_underprice | 0.0% | 0.0% |

Regressed: 0 of 20 metrics. Asking the model to do the comparison itself barely helped.

## The improvement: "only buy on comparable evidence"

`src/decide.py` (`comparable_evidence`, `is_comparable_listing`, and the guardrail in `decide`):

1. **Facts (code).** When the plain edge is misleading, the facts gain a
   `comparable_evidence` block with:
   - the cheapest listing of the *same kind of copy* (same grade; for raw
     cards, the same condition or better, with no slabs and no "any condition" lows);
   - the typical (median) recent sale, used when there are at least 3 comps and
     `sold_avg` is more than 25% away from the median;
   - the like-for-like edge between them.

   The block is absent when nothing is misleading. In this suite it appears for
   exactly the 10 trap cases and none of the other 30.
2. **Prompt.** When that block is present, the model is told to judge the edge
   only from `comparable_evidence.edge_pct`. This replaces run B's wording.
3. **Guardrail (code, applies to the rules engine and the LLM alike).** If a
   "buy" isn't supported by the comparable edge (it clears the buy threshold,
   or it's under the buy target), the verdict becomes "hold" and the reason
   says why. The model's view is kept in the reason. The override is recorded
   in `Decision.guardrails`, and the report counts it so a pass earned by the
   guardrail isn't credited to the model.

The shown `best_listing`, `sold_avg` and `vs_market_pct` are unchanged, so the
arithmetic grader still checks them against the data.

### Rules engine (deterministic)

| Metric | Baseline `20261020-081027-rules-baseline` | Improved `20261020-081732-rules-comparable-evidence` |
|---|---|---|
| Success rate | 75.0% | 100.0% |
| pass@3 / pass^3 | 75.0% / 75.0% | 100.0% / 100.0% |
| damaged_underprice | 0.0% | 100.0% |
| outlier_comps | 0.0% | 100.0% |
| Other 5 categories | 100.0% | 100.0% |
| Guardrail changed the verdict | – | 25.0% of attempts (all 30 trap attempts) |

Regressed: 0 of 21 metrics. Reproducible: a repeat run
(`…-rules-comparable-evidence-repeat`) gave an identical summary, apart from
run id, variant and latency.

### LLM agent

| Metric | Baseline `20261020-081046-llm-baseline` | Improved `20261020-081739-llm-comparable-evidence` |
|---|---|---|
| Success rate | 74.2% | 100.0% |
| pass@3 | 75.0% | 100.0% |
| pass^3 | 72.5% | 100.0% |
| damaged_underprice | 0.0% | 100.0% |
| outlier_comps | 0.0% | 100.0% |
| clear_sell | 94.4% | 100.0% |
| Tool-call failure rate | 0.0% of 305 calls | 0.0% of 277 calls |
| LLM fallback to rules | 0.0% | 0.0% |
| Look-ups the model chose / attempt | 0.78 | 0.48 |
| Avg steps / tokens | 3.8 / 2,748 | 3.5 / 2,646 |
| Avg latency | 1,512 ms | 1,512 ms |
| Failure modes (all 5 detectors) | 0.0% | 0.0% |
| LLM judge: cites numbers / justified (0–2) | 1.78 / 1.92 | 1.75 / 1.98 |
| Guardrail changed the verdict | – | 8.3% of attempts |

Regressed: 0 of 20 metrics.

**Who earned the trap-case passes (30 attempts):**
- **20 by the model alone**, with the new facts and prompt: all 15 outlier attempts and 5 of 15 damaged attempts.
- **10 by the guardrail**: the other 10 damaged attempts, where the model still said "buy".

The clear_sell change (one attempt in the baseline) is within run-to-run noise.

## Limits — read before quoting these numbers

- **Not a held-out test.** I designed the improvement after seeing these two
  categories fail, and measured it on the same suite. 100% shows the targeted
  failure modes are fixed with no regressions elsewhere in the suite. It does
  not show general accuracy on cards it hasn't seen.
- The guardrail is narrow by design: it can only turn a "buy" into a "hold", and
  only when comparable evidence is present. It never creates a buy or a sell.
- Fixture data is synthetic. Live sources differ. For example, JustTCG reports
  no listings or comps, so this check never triggers there.
- The LLM is not deterministic: pass^3 measures consistency over 3 attempts,
  not over all possible runs.

## Reproduce

```bash
python -m src.eval.run_eval --k 3 --variant rules-comparable-evidence
python -m src.eval.run_eval --k 3 --llm on --judge --variant llm-comparable-evidence
python -m src.eval.regress --baseline rules --run <rules run_id>
python -m src.eval.regress --baseline llm --run <llm run_id>
```

The baselines were measured on the code before this change, with prompt sha
`bc25f9634002`. The improved runs used prompt sha `b6ca13f704b6`. Each run's
`meta.json` records its prompt sha.
