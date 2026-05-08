const cardsEl = document.getElementById("cards");
const emptyEl = document.getElementById("empty-state");
const statusPill = document.getElementById("status-pill");
const lastUpdatedEl = document.getElementById("last-updated");
const alertEl = document.getElementById("alert");
const clockEl = document.getElementById("clock");
const summaryEl = document.getElementById("summary");
const rejectsEl = document.getElementById("rejects");
const performanceEl = document.getElementById("performance");

const REFRESH_MS = 5000;
const persistedAccepts = new Map();

function formatTime(isoString) {
  if (!isoString) {
    return "--";
  }
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) {
    return "--";
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatAction(plan) {
  if (plan.persisted && plan.status === "ALLOWED") {
    return plan.direction === "CALL" ? "Accepted CALL (earlier)" : "Accepted PUT (earlier)";
  }
  if (plan.status === "ALLOWED") {
    return plan.direction === "CALL" ? "Buy CALL" : "Buy PUT";
  }
  return "No trade";
}

function planKey(plan) {
  const option = plan.option_contract || {};
  return `${plan.symbol}|${option.expiration || ""}|${option.strike || ""}|${option.option_type || ""}`;
}

function updateStatus(data) {
  const updatedAt = data.updated_at;
  if (updatedAt) {
    statusPill.textContent = "Connected";
    statusPill.classList.remove("alert");
    lastUpdatedEl.textContent = `Last update: ${formatTime(updatedAt)}`;
  } else {
    statusPill.textContent = "Waiting";
    statusPill.classList.add("alert");
    lastUpdatedEl.textContent = "Last update: --";
  }

  if (data.error) {
    alertEl.hidden = false;
    alertEl.textContent = data.error;
  } else {
    alertEl.hidden = true;
    alertEl.textContent = "";
  }
}

function renderPlans(plans) {
  cardsEl.innerHTML = "";
  if (!plans || plans.length === 0) {
    emptyEl.hidden = false;
    return;
  }

  emptyEl.hidden = true;
  plans.forEach((plan, index) => {
    const option = plan.option_contract || {};
    const strike = option.strike ? option.strike.toFixed(2) : "N/A";
    const expiration = option.expiration || "N/A";
    const action = formatAction(plan);
    const health = typeof plan.data_health_score === "number" ? plan.data_health_score.toFixed(2) : "--";
    const timeLabel = formatTime(plan.timestamp);
    const underlying =
      typeof plan.underlying_price === "number"
        ? plan.underlying_price.toFixed(2)
        : typeof option.underlying_price === "number"
          ? option.underlying_price.toFixed(2)
          : "N/A";

    const orb = plan.orb || null;
    let orbHtml = "";
    if (orb) {
      const rh = typeof orb.range_high === "number" ? orb.range_high.toFixed(2) : "--";
      const rl = typeof orb.range_low === "number" ? orb.range_low.toFixed(2) : "--";
      const entry = typeof orb.entry === "number" ? orb.entry.toFixed(2) : "--";
      const stop = typeof orb.stop_loss === "number" ? orb.stop_loss.toFixed(2) : "--";
      const target = typeof orb.target === "number" ? orb.target.toFixed(2) : "--";
      const status = orb.status || "n/a";
      const direction = orb.direction || "--";
      const confirm = orb.confirm_reason || "--";
      const rangeMinutes = orb.range_minutes || 15;
      orbHtml = `
        <div class="orb-block">
          <div class="orb-title">ORB (${rangeMinutes}m)</div>
          <div class="orb-row"><span>Range</span><span>${rl} / ${rh}</span></div>
          <div class="orb-row"><span>Status</span><span>${status}</span></div>
          <div class="orb-row"><span>Dir</span><span>${direction}</span></div>
          <div class="orb-row"><span>Confirm</span><span>${confirm}</span></div>
          <div class="orb-row"><span>Entry</span><span>${entry}</span></div>
          <div class="orb-row"><span>Stop</span><span>${stop}</span></div>
          <div class="orb-row"><span>Target</span><span>${target}</span></div>
        </div>
      `;
    }

    const card = document.createElement("article");
    card.className = `plan-card ${plan.status === "REJECTED" ? "reject" : ""} ${
      plan.persisted ? "persisted" : ""
    }`;
    card.style.animationDelay = `${index * 0.08}s`;

    card.innerHTML = `
      <div class="card-top">
        <div class="symbol">${plan.symbol}</div>
        <div class="decision">${plan.status}</div>
      </div>
      <div class="action">${action}</div>
      <div class="price-line">Stock: ${underlying}</div>
      <div class="meta">
        <div class="meta-row">
          <span class="label">Strike</span>
          <span>${strike}</span>
        </div>
        <div class="meta-row">
          <span class="label">Expires</span>
          <span>${expiration}</span>
        </div>
        <div class="meta-row">
          <span class="label">Data</span>
          <span>${health}</span>
        </div>
        <div class="meta-row">
          <span class="label">Stock</span>
          <span>${underlying}</span>
        </div>
      </div>
      <div class="time">Signal time: ${timeLabel}</div>
      ${orbHtml}
    `;

    cardsEl.appendChild(card);
  });
}

function renderSummary(plans, sessionTotals) {
  const hasPlans = plans && plans.length > 0;
  const hasTotals = sessionTotals && typeof sessionTotals.total === "number";
  if (!hasPlans && !hasTotals) {
    summaryEl.innerHTML = "";
    return;
  }
  const allowed = plans.filter((plan) => plan.status === "ALLOWED").length;
  const rejected = plans.length - allowed;
  const scores = plans
    .map((plan) => plan.data_health_score)
    .filter((score) => typeof score === "number");
  const avgScore =
    scores.length > 0
      ? (scores.reduce((total, value) => total + value, 0) / scores.length).toFixed(2)
      : "--";
  const totalsLabel = sessionTotals?.session_date_exchange
    ? `Session totals (${sessionTotals.session_date_exchange})`
    : "Session totals";
  const avgHealth =
    typeof sessionTotals?.avg_data_health === "number"
      ? sessionTotals.avg_data_health.toFixed(2)
      : "--";
  const staleRate =
    typeof sessionTotals?.stale_rate === "number"
      ? `${sessionTotals.stale_rate.toFixed(1)}%`
      : "--";
  const totalsHtml = hasTotals
    ? `
    <div class="summary-subhead">${totalsLabel}</div>
    <div class="summary-grid secondary">
      <div class="summary-card"><span>${sessionTotals.total}</span><small>Total</small></div>
      <div class="summary-card"><span>${sessionTotals.allowed}</span><small>Allowed</small></div>
      <div class="summary-card"><span>${sessionTotals.rejected}</span><small>Rejected</small></div>
    </div>
  `
    : "";
  summaryEl.innerHTML = `
    <h2>Session Snapshot</h2>
    <div class="summary-grid">
      <div class="summary-card"><span>${plans.length}</span><small>Total</small></div>
      <div class="summary-card"><span>${allowed}</span><small>Allowed</small></div>
      <div class="summary-card"><span>${rejected}</span><small>Rejected</small></div>
      <div class="summary-card"><span>${avgScore}</span><small>Data</small></div>
      <div class="summary-card"><span>${avgHealth}</span><small>Health Avg</small></div>
      <div class="summary-card"><span>${staleRate}</span><small>Stale %</small></div>
    </div>
    ${totalsHtml}
  `;
}

function renderRejectReasons(plans) {
  if (!plans || plans.length === 0) {
    rejectsEl.innerHTML = "";
    return;
  }
  const counts = new Map();
  plans.forEach((plan) => {
    (plan.reject_reasons || []).forEach((reason) => {
      counts.set(reason, (counts.get(reason) || 0) + 1);
    });
  });
  if (counts.size === 0) {
    rejectsEl.innerHTML = "";
    return;
  }
  const sorted = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  const pills = sorted
    .slice(0, 6)
    .map(([reason, count]) => `<span class="reject-pill">${reason} (${count})</span>`)
    .join("");
  rejectsEl.innerHTML = `<h2>Top Reject Reasons</h2><div class="reject-list">${pills}</div>`;
}

function renderPerformance(perf) {
  if (!perf) {
    performanceEl.innerHTML = "";
    return;
  }
  const totals = perf.totals || {};
  const openPositions = perf.open_positions || [];
  const lastFills = perf.last_fills || [];
  const modeLabel = perf.mode ? perf.mode.toUpperCase() : "PAPER";

  const invested = typeof totals.invested === "number" ? totals.invested.toFixed(2) : "--";
  const marketValue =
    typeof totals.market_value === "number" ? totals.market_value.toFixed(2) : "--";
  const unrealized =
    typeof totals.unrealized_pnl === "number" ? totals.unrealized_pnl.toFixed(2) : "--";
  const realized =
    typeof totals.realized_pnl === "number" ? totals.realized_pnl.toFixed(2) : "--";
  const trades = typeof totals.trades === "number" ? totals.trades : 0;
  const winRate = typeof totals.win_rate === "number" ? totals.win_rate.toFixed(1) : "--";
  const profitFactor =
    typeof totals.profit_factor === "number" ? totals.profit_factor.toFixed(2) : "--";
  const maxDrawdown =
    typeof totals.max_drawdown === "number" ? totals.max_drawdown.toFixed(2) : "--";
  const avgSlippage =
    typeof totals.avg_slippage === "number" ? totals.avg_slippage.toFixed(4) : "--";
  const pnlClass =
    typeof totals.unrealized_pnl === "number"
      ? totals.unrealized_pnl >= 0
        ? "pnl-positive"
        : "pnl-negative"
      : "";
  const realizedClass =
    typeof totals.realized_pnl === "number"
      ? totals.realized_pnl >= 0
        ? "pnl-positive"
        : "pnl-negative"
      : "";

  const positionsHtml =
    openPositions.length > 0
      ? `
    <div class="positions-list">
      ${openPositions
        .map((pos) => {
          const mid = pos.current_mid !== null ? pos.current_mid.toFixed(2) : "N/A";
          const pnl = pos.unrealized_pnl;
          const pnlText = pnl !== null ? pnl.toFixed(2) : "N/A";
          const rowClass = pnl !== null && pnl >= 0 ? "pnl-positive" : pnl !== null ? "pnl-negative" : "";
          return `
        <div class="position-row">
          <div><strong>${pos.symbol}</strong> ${pos.option_type} ${pos.strike}</div>
          <div>Exp: ${pos.expiration}</div>
          <div>Qty: ${pos.qty}</div>
          <div>Entry: ${pos.avg_price.toFixed(2)}</div>
          <div>Mid: ${mid}</div>
          <div class="${rowClass}">PnL: ${pnlText}</div>
        </div>
      `;
        })
        .join("")}
    </div>
  `
      : `<p class="subhead">No open positions yet.</p>`;

  const fillsHtml =
    lastFills.length > 0
      ? lastFills
          .map((fill) => {
            const order = fill.order_payload || {};
            const symbol = order.symbol || "N/A";
            const strike = order.strike || "--";
            const optionType = order.option_type || order.direction || "--";
            const price = fill.fill_price !== null ? Number(fill.fill_price).toFixed(2) : "--";
            const time = formatTime(fill.fill_time_utc);
            return `<div class="meta-row"><span>${symbol} ${optionType} ${strike}</span><span>${price} @ ${time}</span></div>`;
          })
          .join("")
      : `<div class="meta-row"><span>No fills yet</span><span>--</span></div>`;

  const lastClosed = perf.last_closed || [];
  const closedHtml =
    lastClosed.length > 0
      ? lastClosed
          .map((trade) => {
            const pnl = typeof trade.realized_pnl === "number" ? trade.realized_pnl.toFixed(2) : "--";
            const pnlClass = typeof trade.realized_pnl === "number" && trade.realized_pnl >= 0 ? "pnl-positive" : "pnl-negative";
            return `<div class="meta-row"><span>${trade.symbol} ${trade.option_type} ${trade.strike}</span><span class="${pnlClass}">$${pnl}</span></div>`;
          })
          .join("")
      : `<div class="meta-row"><span>No closed trades yet</span><span>--</span></div>`;

  performanceEl.innerHTML = `
    <h2>Performance (${modeLabel})</h2>
    <div class="performance-grid">
      <div class="performance-card">
        <span class="label">Open Positions</span>
        <span class="value">${totals.open_positions ?? 0}</span>
      </div>
      <div class="performance-card">
        <span class="label">Invested</span>
        <span class="value">$${invested}</span>
      </div>
      <div class="performance-card">
        <span class="label">Market Value</span>
        <span class="value">$${marketValue}</span>
      </div>
      <div class="performance-card">
        <span class="label">Unrealized PnL</span>
        <span class="value ${pnlClass}">$${unrealized}</span>
      </div>
      <div class="performance-card">
        <span class="label">Realized PnL</span>
        <span class="value ${realizedClass}">$${realized}</span>
      </div>
      <div class="performance-card">
        <span class="label">Win Rate</span>
        <span class="value">${winRate}%</span>
      </div>
      <div class="performance-card">
        <span class="label">Trades</span>
        <span class="value">${trades}</span>
      </div>
      <div class="performance-card">
        <span class="label">Profit Factor</span>
        <span class="value">${profitFactor}</span>
      </div>
      <div class="performance-card">
        <span class="label">Max Drawdown</span>
        <span class="value">$${maxDrawdown}</span>
      </div>
      <div class="performance-card">
        <span class="label">Avg Slippage</span>
        <span class="value">${avgSlippage}</span>
      </div>
    </div>
    <div class="summary-subhead">Open Positions</div>
    ${positionsHtml}
    <div class="summary-subhead">Closed Trades</div>
    <div class="meta">${closedHtml}</div>
    <div class="summary-subhead">Recent Fills</div>
    <div class="meta">${fillsHtml}</div>
  `;
}

async function loadPlans() {
  try {
    const response = await fetch("/api/plans", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    updateStatus(data);
    const currentPlans = data.plans || [];
    const currentKeys = new Set(currentPlans.map((plan) => planKey(plan)));
    (data.session_accepts || []).forEach((plan) => {
      const key = planKey(plan);
      if (!persistedAccepts.has(key)) {
        persistedAccepts.set(key, plan);
      }
    });
    const persisted = Array.from(persistedAccepts.values())
      .filter((plan) => !currentKeys.has(planKey(plan)))
      .map((plan) => ({ ...plan, persisted: true }));
    const combinedPlans = [...currentPlans, ...persisted];
    renderPlans(combinedPlans);
    renderSummary(currentPlans, data.session_totals);
    renderRejectReasons(currentPlans);
    renderPerformance(data.performance);
  } catch (error) {
    updateStatus({ error: "Unable to reach the planner. Check the server." });
  }
}

function updateClock() {
  const now = new Date();
  clockEl.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

loadPlans();
updateClock();
setInterval(loadPlans, REFRESH_MS);
setInterval(updateClock, 1000);
