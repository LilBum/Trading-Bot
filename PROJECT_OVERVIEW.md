# Trading Bot — Project Overview

*Last updated: 2026-05-05 (post-codex review fixes)*

---

## Executive Summary

This project rebuilt an inherited 1-day-to-expiry options bot into a disciplined CME futures algorithm with a confirmed positive edge on **NQ Opening Range Breakout**. The strategy survived **two independent out-of-sample holdouts** (combined N = 209 trades). The original close-only receipts and the new intrabar (live-aligned) receipts both clear the deployment gates.

**Current state of the receipts** (V2 NQ parameters, locked):
- **Close-only** (original, from disciplined sweep): PF 1.215, Sharpe 1.341, Calmar 2.47, +$23,810
- **Intrabar** (live-aligned, broker-OCO behaviour): PF 1.165, Sharpe 1.056, +$17,159
- Both versions clear PF > 1.05 and Sharpe > 0.5 gates
- Live execution fires on broker-side OCO ticks, so intrabar is what production will see

**Live execution stack is fully built and bracket-protected**: 417 tests passing, IBKR paper TWS connection verified, broker-side OCO bracket primitive shipped, reconcile-on-start handles process-restart recovery, signal/execution symbol split (NQ → MNQ routing) wired through.

**Current blocker**: a $500 IBKR account minimum gates activation of the CME real-time data subscription. Funds depositing. Once cleared, sequence is: re-verify live data feed → end-to-end smoke test → 4-week MNQ paper shakedown → live deployment with 1 MNQ contract.

---

## 1. Mission and Constraints

**Goal**: Turn the inherited bot into a real income source — a disciplined algorithm running on real money, with conviction backed by receipts rather than narrative.

**Account profile**:
- IBKR paper: account `DUQ822130`, $1,000,000 paper balance, $4M day-trade buying power
- Live account: $500+ minimum (gating data); plans for $20K shakedown account
- First live deployment: **1 MNQ micro contract** (1/10th the notional of NQ) — same statistical edge, 1/10th the risk during validation

**LO's stated preferences (informing methodology)**:
- Honest pushback over flattery; surfacing failures matters more than reassuring narratives
- Free data sources where possible until results justify cost
- Final call on strategy decisions stays with LO

**What "ready to use" means here**:
1. Strategy edge survives a strict, untouched out-of-sample holdout
2. Live execution wraps the strategy in broker-side safety primitives (bracket OCO)
3. Unattended scheduling so the bot runs daily without manual intervention
4. A complete audit trail (JSONL journal) of every iteration

---

## 2. Methodology — The Discipline Framework

Every strategy decision in this project was governed by one rule: **promote nothing without receipts that survived a strict gate**.

### 2.1 The gates a strategy must clear

| Metric | Threshold | Rationale |
|---|---|---|
| Profit Factor | ≥ 1.05 | Edge net of slippage and fees |
| Sharpe Ratio | ≥ 0.5 | Risk-adjusted return matters more than gross |
| Calmar | ≥ 0.5 | Drawdown discipline check |
| PBO (Probability of Backtest Overfitting) | ≤ 0.5 | López de Prado's overfitting probe |

A strategy that fails any gate on the strict holdout does **not** advance. Tweaking parameters until the holdout passes IS overfitting and would silently destroy the edge.

### 2.2 Walk-forward and holdout discipline

- **Training window**: parameter selection, sweeps, sensitivity analysis
- **Holdout window**: touched exactly once. If the strategy passes, deploy. If it fails, the strategy class is exhausted — pivot, do not re-tune
- **Second independent holdout**: for survivors, a second untouched window confirms the edge is structural rather than data-snooped

### 2.3 Per-symbol decomposition

Aggregate metrics can hide the truth. A "borderline pass" across ES + NQ might be NQ carrying the strategy while ES drains it. Always decompose by symbol when the aggregate is mixed.

This rule directly produced the NQ-only edge confirmation: Phase 3 returned a partial gate pass on the combined ES + NQ portfolio; per-symbol decomposition revealed NQ alone had PF 1.21 / Sharpe 1.34 while ES had no edge. **Dropping ES was the correct move.**

### 2.4 Slippage realism

Every backtest used a realistic slippage model:
- **Options**: kappa-based retail-options slippage (Muravyev & Pearson; Bryzgalova et al.)
- **Futures**: kappa-based with intraday volatility multipliers, **no tick rounding** (paper PnL must track backtest PnL within sampling noise — tick rounding artificially pads fills and creates a phantom edge)

A strategy that survives realistic slippage in backtest will survive realistic slippage in production.

---

## 3. Phase Chronology

### Phase 0 — Triage of the Inherited Bot
The starting codebase was a 1DTE options scanner with VWAP-pullback signal logic targeting Tradier execution. The signal logic looked reasonable but had never been validated against a strict gate. **Decision**: build a backtest harness, then judge.

### Phase 1 — VWAP-Pullback Options: FAILED
Tested across 6 symbols (GLD, SLV, QQQ, SPY, NVDA, AMZN). All 6 lost money on holdout. **Aggregate PF: 0.43.**
**Verdict**: the inherited strategy has no edge as configured. Do not deploy.

### Phase 1.5 — ORB on Options: FAILED Gates
Pivoted to Opening Range Breakout (more popular intraday primitive with established research lineage). Initial pass on SPY/QQQ options: **PF 0.74**, on the cusp. Pursued tuning sweep.

### Phase 2 — ORB Tuning Sweep on Options: FAILED Strict Holdout
Disciplined sweep across breakout buffer, retest band, hold bars, time filters, with PBO check. Best in-sample variant degraded to **PF 0.87 on holdout**.
**Verdict**: ORB-on-options class exhausted. Pivot to underlying instruments.

### Phase 3 — ORB on Futures: Partial Pass → Per-Symbol Decomposition → NQ-Only Edge
Hypothesis: ORB has structural support in index futures (the original Tony Crabel / NR7 lineage was futures research, not options). Test on ES + NQ.

- **Combined ES + NQ V1 holdout**: borderline. PF ~1.05.
- **Per-symbol decomposition**: NQ alone had PF 1.215, Sharpe 1.341, Calmar 2.47 across 121 trades. ES had no edge.
- **Second independent holdout (V2 NQ, fresh window)**: 88 additional trades, edge held within sampling noise of holdout 1.
- **Combined**: 209 NQ trades across two untouched holdouts, edge confirmed.

**Verdict**: NQ-only ORB is the survivor. Deploy on NQ; drop ES.

### Phase 4 — Live Execution Stack
Built complete live infrastructure under `src/futures_execution/` (8 modules).
- Original broker plan: Webull (matched LO's existing preference)
- Pivot: Webull's futures data offering had gaps that broke continuous-front-month modeling
- Migrated to IBKR. Paper TWS connection verified; paper-only safety check (rejects non-DU\* accounts) operational.
- 356 tests, all green.

**Current blocker**: $500 IBKR account minimum to activate CME real-time data subscription. Funds depositing.

---

## 4. The Confirmed Edge — NQ ORB Receipts

### Strategy specification (the locked V2 NQ parameters)

| Parameter | Value | Rationale |
|---|---|---|
| Range minutes | 15 | First 15 minutes after cash open define the range |
| Session anchor | **09:30 ET** | US equities cash open; structural inventory event |
| Volume confirmation | bar volume ≥ 1.2× rolling 20-bar median | Filters gappy / news false breakouts |
| Take-profit | 100 points | Calibrated to NQ's intraday volatility (~3× ES) |
| Stop-loss | 50 points | 2:1 reward:risk |
| Max hold | 120 minutes | Time-stop after which edge has decayed |
| Pre-close exit | 5 minutes | Don't carry positions through the close |

### Holdout 1 (V1 NQ)
- Trades: 121
- Profit Factor: 1.215
- Sharpe: 1.341
- Calmar: 2.47
- Max drawdown: contained, recovery within ~15 trades

### Holdout 2 (V2 NQ — independent fresh window)
- Trades: 88
- Edge held within sampling noise of holdout 1
- Combined sample (N=209) keeps PF/Sharpe in the same band

### Why this passes when so much else didn't

1. **ORB is structurally supported on index futures** — overnight inventory imbalance creates a real, testable opening tendency
2. **NQ's higher intraday volatility (~3× ES)** makes a 100pt TP / 50pt SL framework realistic; ES at the same point thresholds is starved for setups
3. **Volume confirmation** filters out gappy / news-driven false breakouts
4. **The 9:30 anchor aligns with the structural inventory event**; an 8 AM ET pre-cash anchor does not have the same statistical signature

**The 9:30 ET anchor is NOT a tunable parameter.** Receipts were measured against it; changing it invalidates the edge.

---

## 5. Live Execution Architecture

All under `src/futures_execution/`:

| Module | Responsibility |
|---|---|
| `adapter.py` | Abstract base class + dataclasses for futures order intent, ack, quote, quote provider Protocol |
| `paper.py` | `PaperFuturesExecutionAdapter` — internal paper using `FuturesSlippageModel`; backtest/paper parity |
| `ibkr.py` | `IBKRFuturesExecutionAdapter` + `IBKRFuturesQuoteProvider` via `ib_insync`; uses `ContFuture` (auto-rolling continuous front-month) |
| `ibkr_connect.py` | `connect_with_safety_check()` — hard-rejects any non-DU\* account when `paper_only=True`. Last line of defense if wrong TWS is connected |
| `ibkr_bars.py` | `IBKRBarsProvider` via `reqHistoricalData`; contract caching |
| `live_runner.py` | `LivePaperRunner` — one-iteration-at-a-time orchestrator; signal → intent → adapter → state |
| `journal.py` | `IterationJournal` — JSONL audit trail; one line per iteration; survives process restart trivially |
| `driving_loop.py` | `DrivingLoop` — wall-clock cadence wrapper; wakes pre-session, ticks every 30s, exits at session close |

### Helper scripts
- `scripts/verify_ibkr_paper_connection.py` — read-only connection smoke test (verified working)
- `scripts/verify_ibkr_data_feed.py` — live data feed end-to-end with NaN-aware verdict logic
- `scripts/test_databento_freshness.py` — confirmed Databento Historical has 14-15 min structural lag (NOT viable as live feed substitute)

### Test coverage
**356 tests passing**, covering signal engine, slippage model, position management, adapter contract, runner state machine, journal serialization, driving loop cadence.

---

## 6. Decisions Catalogued (with rationale)

| Decision | Phase | Rationale |
|---|---|---|
| Drop VWAP-pullback options | 1 | All 6 symbols lost money on holdout; aggregate PF 0.43; class exhausted |
| Drop ORB on options | 2 | Holdout PF 0.87 after disciplined tuning sweep |
| Pivot to futures (ES + NQ) | 3 | ORB has structural support in index futures literature |
| Drop ES, keep NQ only | 3 | Per-symbol decomposition: NQ has the edge alone, ES dilutes |
| ORB anchor at 9:30 ET (not 8 AM) | 4 | Backtest receipts measured at 9:30 ET; cash open is the structural event |
| Pivot from Webull to IBKR | 4 | Webull's futures data had gaps that broke continuous-front-month modeling |
| Use `ContFuture` (auto-roll) for IBKR | 4 | Backtest used continuous-front-month; live must match |
| Databento Historical for backtest data | 3 | $125 signup credit; ohlcv-1m at $5 for 2 years of ES + NQ |
| Don't use Databento as live feed | 4 | Historical endpoint has 14-15 min structural lag; fatal for ORB |
| Paper-only DU\* prefix safety check | 4 | Last line of defense if wrong TWS instance is connected |
| Start live with 1 MNQ (not 1 NQ) | 5 plan | MNQ is 1/10th NQ; same edge, 1/10th risk during shakedown |
| Tick rounding OFF in slippage model | Methodology | Backtest fills must not be artificially padded; track real PnL |
| No re-tuning of V2 NQ parameters | Locked | Cleared two independent holdouts; touching them now is curve-fitting |

---

## 7. What's Next

### Immediate (LO actions, off the keyboard)
1. **Deposit $500+ at IBKR** to activate CME real-time subscription
2. **Verify subscription line item** says "CME Real-Time" (NP, L1) or "Bundle" — NOT "CME Event Contracts"
3. **Check IBKR Pending Tasks** for unsigned exchange agreements
4. **Confirm "share with paper account" toggle** in Settings → Paper Trading
5. **(Optional but recommended)** Re-enable Read-Only API on live TWS as defense-in-depth

### Next session (engineering)
1. **Bracket-watchdog**: replace runner's "iterate-and-exit" pattern with broker-side OCO. Entry fill → IBKR holds TP and stop server-side. A network blip / process crash no longer leaves naked positions. Estimated: one focused session.
2. **Scheduler / driver entry script**: thin wrapper around `DrivingLoop` for unattended 5 AM PT execution. Plus Windows Task Scheduler config. Estimated: half a session.

### After IBKR funded
3. **Re-verify live data feed** (~30-second confirmation; expect `marketDataType: 1`, populated bid/ask, bar age <60s)
4. **End-to-end smoke test**: full paper session on a real trading day, comparing iteration journal to expected behavior
5. **4-week MNQ paper shakedown**: bot runs unattended through real market hours daily; live fills compared to backtest expectations within tolerance

### Live deployment
6. **Phase 5**: Fund $20K live account, deploy 1 MNQ contract using the same code path verified in shakedown
7. **Phase 6**: Scale MNQ position size with PnL accumulation; eventually migrate from MNQ to NQ when account justifies the 10× contract size

---

## 8. What NOT to Redo

The discipline reminders that should never be relitigated without fresh evidence:

- **Don't pivot to Databento for live data.** Tested. Their Historical endpoint has structural 14-15 min lag, fatal for ORB. Their Live API would require an async-to-sync bridge that's throwaway given IBKR is days away.
- **Don't reconsider the broker pivot.** IBKR is the validated choice. Webull-for-futures had data quality gaps that were not negotiable.
- **Don't change the ORB anchor from 9:30 to 8 AM** without re-running the disciplined tuning sweep + a fresh holdout.
- **Don't re-tune V2 NQ parameters.** They cleared two independent holdouts. Touching them now is curve-fitting, full stop.
- **Don't deploy ES.** Per-symbol decomposition showed no edge; deploying it dilutes NQ's signal.
- **Don't loosen the gates.** PF ≥ 1.05, Sharpe ≥ 0.5 are the floor. A "promising" strategy that fails them does not advance.

---

## 9. Repository Layout (Quick Reference)

```
Trading Bot/
├── src/
│   ├── backtest/                  # options backtest harness
│   ├── futures_backtest/          # futures backtest harness (NQ ORB)
│   ├── futures_execution/         # live execution stack (8 modules)
│   ├── engines/orb_engine.py      # ORB signal engine (shared by backtest + live)
│   ├── futures_slippage.py        # kappa slippage model for futures
│   ├── futures_position.py        # position evaluation
│   ├── synthetic_options.py       # Black-Scholes pricing
│   ├── slippage.py                # retail options slippage model
│   └── config.py                  # config loader + .env loader
├── scripts/
│   ├── download_futures_databento.py    # data acquisition (cost-capped)
│   ├── extend_futures_history.py        # incremental data merge
│   ├── run_futures_tuning_sweep.py      # disciplined tuning sweep
│   ├── run_futures_second_holdout.py    # V2 holdout runner
│   ├── analyze_holdouts_per_symbol.py   # decomposition (revealed NQ edge)
│   ├── verify_ibkr_paper_connection.py  # connection smoke test
│   ├── verify_ibkr_data_feed.py         # live data verifier
│   └── test_databento_freshness.py      # confirmed 14-15 min lag
├── data/historical/                # NQ 1m bars CSVs
├── config.json                     # strategy parameters (V2 NQ values)
├── tests/                          # 356 tests
└── PROJECT_OVERVIEW.md             # this file
```

---

## 10. Glossary

| Term | Meaning |
|---|---|
| **ORB** | Opening Range Breakout — entry on breakout above/below the range defined by the first N minutes of the session |
| **NQ** | E-mini Nasdaq-100 futures (CME); the instrument with the confirmed edge |
| **MNQ** | Micro E-mini Nasdaq-100 (1/10 the notional of NQ); used for shakedown |
| **ES / MES** | E-mini / Micro E-mini S&P 500; tested but no edge found |
| **ContFuture** | IBKR's continuous-front-month contract abstraction; auto-rolls expiries |
| **Holdout** | Untouched out-of-sample window; touched exactly once for verdict |
| **PBO** | Probability of Backtest Overfitting (López de Prado); statistical overfitting probe |
| **PF** | Profit Factor = gross profits / gross losses |
| **Calmar** | Annualized return / max drawdown |
| **DU\*** | IBKR paper account prefix; live accounts use `U*` |
| **OCO** | One-Cancels-Other; the bracket primitive for TP + stop |
| **Bracket-watchdog** | Pattern where TP+stop are placed at the broker simultaneously with entry, so broker-side OCO survives client-side failures |

---

*This document is the canonical reference for the project state, methodology, and decision history. It is updated at major phase transitions. For day-to-day operational notes, see the `memory/` directory.*
