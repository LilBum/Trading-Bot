Deep review of your current 1DTE options planner and answers to your open questions
Overall assessment of the current capabilities
Your current system description reads like a “production-shaped” trading stack even though you’re explicitly running paper-only (decisioning + simulation + audit). Architecturally, the most important thing you’ve done is keep the system as a pipeline with independent gates (data → signal → instrument selection → risk approval → plan/execution simulation → journal), because that mirrors how professional market-access and automated-trading risk guidance frames safe operation: layered pre-trade controls, monitoring/audit trails, and emergency safeguards rather than any single “alpha” component.

The controls you list—kill switch, duplicate-order windows, throttling, per-symbol caps, total caps, daily loss lockout, cooldown—are directly aligned with the intent of widely cited risk-control regimes: preventing erroneous/duplicative behavior, limiting financial exposure, and enabling rapid intervention when a system is misbehaving.

Where “top-tier” still hinges (especially for 1DTE) is the data truth you build decisions on, and the execution realism of your paper simulator. Listed options are structurally quote-driven and depend heavily on continuous market maker quoting; if you lack reliable quote timestamps/NBBO and consistent chain fields, the smartest scoring logic won’t save you from garbage-in/garbage-out.

Data layer realism and what “live-grade” should mean
Provider redundancy is good, but “fall-open” is risky
You’ve improved your market-data resilience by using a primary provider and a fallback chain (primary: Massive; fallback: Finnhub → Yahoo). That’s directionally good for uptime, but top-tier trading systems treat provider switches as risk events—because each provider can differ in timing, symbology, adjustment conventions, and quote freshness.

Two concrete implications:

Fallback-to-Yahoo is not live-grade. Yahoo’s own help text states its finance data is for informational purposes only and not intended for trading or investing purposes; that’s fundamentally incompatible with “live decisioning,” especially for short-dated options.
Fallback-to-Finnhub is “use with caution”.
explicitly states it does not guarantee the accuracy or completeness of data accessed through its service, and that data may be inaccurate or unfit for a particular purpose.
It’s still useful (and common) to keep fallbacks for paper/research mode, but for “live-grade mode,” your best practice is fail-closed unless your live-grade feed is healthy and passing your own quality gates. This aligns with broad regulatory/industry intent: controls should meaningfully limit exposure and prevent unsafe states when conditions degrade.

“NBBO + quote timestamp required” is exactly right for options
Your hard reject on missing quote timestamps and NBBO is a strong design decision. Options are quote-driven, and each options series has its own NBBO; reliable best-bid/offer and freshness are core to tradeability.

It’s also helpful that you are explicitly leaning on consolidated options data concepts (OPRA/NBBO).
explains its role as disseminating consolidated last sale and quotation information from the options exchanges approved by the SEC.

Quote-age: why seconds matter (especially for 1DTE)
You asked what max quote age is acceptable (1 minute? 10 seconds?). The research evidence about options data flow strongly suggests that “minutes” is not a meaningful recency unit for live options decisioning:

OPRA publishes SIP metrics where latency is measured in microseconds, reflecting high-frequency dissemination and the reality that quotes update continuously.
Independent microstructure analysis from
notes OPRA message microbursts can exceed tens of millions of messages per second and that OPRA latencies (post-upgrades) can be extremely low—reinforcing how quickly the top-of-book can change.
That does not mean your retail system must operate at microsecond scales. It does mean that allowing 10–60 seconds of quote staleness for 1DTE options can be functionally equivalent to using the wrong market, particularly around fast moves.

A practical “top-tier planner” policy (inferred from how fast the quote-driven market updates and how costly stale quotes are) is:

Paper mode / research: allow up to ~10–30 seconds but clearly label “stale-ish” and degrade confidence.
Live-grade decisioning: aim for ≤2–5 seconds hard limit for the option quote timestamp; treat anything above as REJECT unless you explicitly enable a degraded mode.
Chain-source availability: why “public-only” is a single point of failure
Your current design uses a “Public API” for the options chain, while requiring NBBO and quote timestamps. That can work in paper mode, but it leaves you exposed to outages/rate limits as well as licensing/redistribution constraints.

Two relevant data-market realities:

Cboe has highlighted that real-time OPRA access can be delivered via different commercial structures (streaming subscription vs per-query/usage), and that costs can be a barrier for providing full streaming OPRA to retail participants.
A number of “serious” options data offerings (including OPRA-derived datasets) exist specifically because reliable NBBO/top-of-book for all series is operationally heavy; vendors differentiate on latency, normalization, coverage, and delivery mechanics.
Given that, a top-tier reliability posture is:

In live-grade mode, if the chain/NBBO feed becomes unavailable, fail closed (no new positions) rather than silently switching to a less reliable provider.
In paper mode, you can fall back to a secondary chain source or cached snapshots, but you should label it clearly in logs and the dashboard as “degraded chain.”
One extra observation: your “primary bars provider” (Massive) appears to offer an options API and options-chain snapshot capabilities in addition to stocks, which may give you a cleaner single-vendor path than “public chain + separate bar vendor,” depending on your plan and licensing.

(Verification is still required in your environment because vendor docs don’t automatically equal reliable coverage for your tickers and usage patterns.)

Paper execution simulation and exit logic
Why execution realism matters more for options than for stocks
In options, execution costs (spread + slippage) can be large enough to dominate outcomes. Academic work on retail option trading finds option bid–ask spreads can often be wide (the paper explicitly references spreads in the 5–10%+ range) and notes retail traders may use limit orders to avoid paying the spread.

Related work on retail trading costs (stocks and options) finds that realized option costs are materially affected by order type usage and that limit orders play an important role in reducing retail trading costs.

That evidence is directly relevant to your simulator: a spread-based slippage model is a good baseline, but if your simulated exits/entries always “get filled at mid,” your performance will often look better than reality—especially when your liquidity gates are near threshold.

Market vs limit exits: what research and broker tooling imply
You asked whether exits should always be market or limit at mid. In real options trading:

Market orders maximize certainty of execution but can suffer severe slippage in wide or unstable spreads.
Midpoint/inside-the-spread limit logic is widely supported in professional brokerage tooling.
documents MidPrice and midpoint-oriented order types and tools designed to trade between bid/ask and seek price improvement.
The Options Industry Council’s educational material emphasizes understanding bid/ask dynamics and practical handling (e.g., using limit orders to protect against “bad prints”).
A “top-tier” policy for your specific program (planner + paper exec) is typically:

Default to a marketable limit approach for exits: start at mid (or mid ± small offset), then step toward a more aggressive limit if not filled within a short timeout.
Reserve true MARKET exits for “must exit” conditions (kill switch / catastrophic data degradation / position limit breach), and log them as such.
Trailing stop: aggressive vs loose
You asked about aggressive vs loose trailing. The research above implies a key tradeoff:

Frequent trailing stop-outs increase turnover and therefore spread/slippage costs (which can be large for options).
At the same time, 1DTE positions can deteriorate quickly when the underlying reverses, so trailing can reduce tail losses.
Given your program already measures liquidity gates and can track slippage, the best “top-tier” approach is regime-dependent trailing, not one global behavior:

In choppy regimes (high VWAP cross frequency), use tighter trailing or faster profit-taking, because continuation probability is lower and churn risk is high.
In trending regimes (EMA alignment + stable VWAP slope), use looser trailing to avoid getting shaken out by noise, because options spreads can make frequent exits expensive.
This is also consistent with intraday liquidity patterns: spreads are often widest at/near open and improve later, and short-dated option-implied volatility has a familiar intraday pattern with higher volatility at open/close than midday.

Risk controls and operational safety
Your controls match “market access” intent, which is the right bar
Even though you are not a broker-dealer, the SEC’s Market Access Rule materials are a useful engineering checklist because they emphasize preventing erroneous/duplicative orders and controlling exposure in real time.

Your list of implemented controls (whether hard or warning-level configurable) is aligned with what exchanges and industry guides emphasize:

Kill/stop functionality as a backstop.
Throttles and duplicate detection to prevent runaway order behavior.
Daily loss limits and lockouts as an operator-level safety control, especially for day trading risk.
Event-risk days: why “warn-only” is often insufficient for 1DTE
You already implement “event-risk day blocking.” The key question is whether to block or warn.

From a risk-controls perspective, major volatility events are exactly when liquidity and spreads can change abruptly—and when automated safeguards are most valuable. Industry and regulatory discussions about electronic trading risk controls and volatility control mechanisms emphasize tools like price validations, volatility controls, message throttles, and kill switches to preserve orderly trading during stress.

For 1DTE options specifically, event-driven volatility can also interact with wide effective spreads and poor retail execution, as research on retail options around high expected volatility announcements suggests.

A strong compromise policy is:

Default is warn + automatically tighten risk and liquidity constraints (reduced sizing, stricter spread%, stricter quote age, more conservative exit policy).
Support a config flag for hard block for specific event categories (e.g., “Fed decision minutes only,” “CPI first 10 minutes,” etc.), based on your own observed slippage and “stale quote %” metrics.
Backtesting, replay, and “second strategy” without fooling yourself
Your replay/backtest capability (CSV bars + optional chain snapshot) is a strong start. The main research gap you correctly identified is that a fixed chain snapshot is not the same as a chain evolving over time; that can lead to materially optimistic fills, especially near rapid regime changes.

If you add a second signal strategy, you should treat the research process as a multiple-testing problem. Work on backtest overfitting shows that the probability of “discovering” something that looks great historically but fails out-of-sample grows quickly with the number of trials/variations.
formalizes this with the “probability of backtest overfitting” (PBO) framework.

Work on false discoveries in financial research similarly emphasizes that when many tests are run, many apparent “edges” can be luck without proper adjustment.
discusses this multiple testing problem and calibration of false discoveries.

The “top-tier” implication for your roadmap is:

Add a second strategy only if you can validate it with walk-forward / out-of-sample checks, and keep a strict paper trail of “what changed” in config hashes and versioning (which you already log).
Answers to your open questions with recommended defaults
These answers assume your goal is a safe, realistic paper-trading system now, with a path toward “small live trades” later.

Do you want the Public options chain to be the only chain source if it becomes temporarily unavailable?
For live-grade mode, no: a single public chain source is a single point of failure. If the chain source is down or cannot supply NBBO + timestamps reliably, fail closed (no new positions) and log a data-unavailable incident. This is consistent with risk-control philosophy: when safety inputs degrade, reduce function rather than reduce safety.

For paper mode, you can allow fallback to a secondary chain source or cached snapshots, but label it clearly (“degraded chain”) and treat it as lower confidence.

Practical note: since you already use
for bars, it may be worth evaluating whether its options endpoints can serve as a more stable “non-public” chain provider for your use cases (subject to plan/coverage/licensing).

What max quote age is acceptable during live market hours?
For 1DTE options, the research and microstructure evidence supports keeping this in seconds, not minutes, because the quote-driven market updates continuously and can change extremely rapidly.

Recommended defaults:

Paper mode: hard reject >30s; warn >10s; tighten if you find slippage spikes.
Live-grade mode: hard reject >5s; warn >2s.
Should Yahoo fallback be blocked for options chain in live-grade mode?
Yes. The public statement that Yahoo Finance data is informational and not intended for trading/investing is incompatible with live-grade options decisioning.

Trailing stop: aggressive or loose?
Default to regime-dependent trailing:

Trend regime → looser trailing (avoid churn costs), because option spread costs can be large.
Chop regime → tighter trailing or faster profit-taking.
A simple implementation that tends to behave well in practice is: “no trailing until TP1; after TP1, trail either by underlying structure or a volatility-scaled rule.” That reduces unnecessary churn during the noisiest phase.

Exits: MARKET when triggered, or LIMIT at mid?
Default to LIMIT/inside-the-spread logic, with a supervised escalation path (marketable limit if not filled quickly). Research on retail options costs and broker tooling both support the idea that limit orders can materially reduce trading costs, while market orders guarantee execution but can be expensive in wide spreads.

For “must exit now” safety scenarios (kill switch / data health collapse / position limit breach), a marketable limit with a worst-case cap is usually safer than a pure market order, but the exact rule should be tested with your own slippage metrics.

Maximum daily drawdown you can tolerate?
You asked this as a preference question, but a research-backed “safe default” for early-stage day trading and system shakedowns is low single-digit percent.

The SEC explicitly warns that day traders often suffer severe financial losses and should only risk money they can afford to lose.

For a $10k account, a conservative control-plane default for an initial test phase is:

Daily max loss: 1%–2% ($100–$200) for the first couple of weeks of real-time practice/paper simulation.

Once you have stable metrics (slippage, win rate, drawdown), you can revisit this, but the “top-tier” approach is to adjust only with evidence and keep the lockout enforceable.
Event-risk days: fully block or warn?
Default should be warn + auto-tighten (risk downshift + stricter gates). Hard-block should be available per event type/time window if your data shows that slippage/stale-quote rates spike materially during those windows.

Add a second strategy now?
Not yet, unless you have enough replay data and a validation plan. The biggest research risk in adding strategies is false discovery / overfitting; formal work on backtest overfitting and multiple testing shows why strategies can look good by luck when many variations are tried.

A “top-tier” progression is:

stabilize one strategy’s execution realism and risk controls,
build trustworthy replay (including more realistic chain evolution),
then add a second strategy with walk-forward validation and explicit change logs.
Run only at specific times of day?
Yes, and your existing open/close restrictions are well supported by liquidity/volatility intraday patterns:

Evidence across markets shows spreads are commonly widest near the open, and short-dated options research finds a familiar intraday volatility pattern with higher volatility at open/close than midday.
Options spreads in classic research are documented as widest near the open and then declining/leveling later.
A research-aligned default is:

avoid the first ~10 minutes unless your “scalp mode” has extra-tight quote freshness and spread limits,
focus on mid-session for holds,
degrade/limit new entries late-day unless your exit mechanics are proven.
