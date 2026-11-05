/* Codex-owned terminal. Live mode is read-only. Demo orders never leave this page. */
(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [
    ...root.querySelectorAll(selector),
  ];
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const icon = (name) =>
    `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const num = (value, digits = 2) =>
    finite(value)
      ? value.toLocaleString("en-US", {
          maximumFractionDigits: digits,
          minimumFractionDigits: digits,
        })
      : "—";
  const price = (value) =>
    !finite(value)
      ? "—"
      : "$" +
        num(value, value < 0.001 ? 8 : value < 1 ? 4 : value < 10 ? 3 : 2);
  const dollars = (value) => (finite(value) ? "$" + num(value) : "—");
  const signed = (value) =>
    !finite(value)
      ? "—"
      : (value >= 0 ? "+" : "−") + "$" + num(Math.abs(value));
  const pct = (value) =>
    finite(value) ? (value >= 0 ? "+" : "") + num(value) + "%" : "—";
  const color = (value) =>
    !finite(value) ? "muted" : value >= 0 ? "positive" : "negative";
  const compact = (value) =>
    !finite(value)
      ? "—"
      : value >= 1e9
        ? "$" + num(value / 1e9) + "B"
        : value >= 1e6
          ? "$" + num(value / 1e6, 1) + "M"
          : dollars(value);
  const quantity = (value) =>
    finite(value)
      ? value.toLocaleString("en-US", { maximumFractionDigits: 6 })
      : "—";
  const time = (value) =>
    value ? new Date(value).toISOString().slice(11, 19) : "—";
  const chainName = (chain) =>
    ({
      sol: "Solana",
      eth: "Ethereum",
      bsc: "BNB Chain",
      base: "Base",
      robinhood: "Robinhood",
    })[chain] || chain;
  const coinClass = (token) =>
    ["sol", "eth", "bnb", "bonk", "wif", "popcat", "jup", "aero"].includes(
      token.symbol?.toLowerCase(),
    )
      ? token.symbol.toLowerCase()
      : "eth";
  const coin = (token) =>
    `<span class="coin ${coinClass(token)}" aria-hidden="true">${esc((token.symbol || "?").slice(0, 1))}</span>`;
  let snapshot = JSON.parse($("#terminal-data").textContent);
  const isDemo = snapshot.demo === true;
  let favorites = [];
  try {
    const raw = JSON.parse(
      localStorage.getItem("kaiba-terminal-favorites-v1") || "[]",
    );
    if (Array.isArray(raw))
      favorites = raw.filter((x) => typeof x === "string").slice(0, 100);
  } catch (_) {
    /* Storage is optional. */
  }
  const state = {
    selected:
      snapshot.tokens.find((t) => t.symbol === "BONK")?.id ||
      snapshot.tokens[0]?.id,
    chain: "all",
    search: "",
    favoritesOnly: false,
    favorites,
    timeframe: "15m",
    chart: "candles",
    view: "terminal",
    table: "positions",
    side: "buy",
    type: "market",
    orders: [],
    history: [],
    positions: structuredClone(snapshot.positions),
    balances: structuredClone(snapshot.balances),
    events: structuredClone(snapshot.events),
    consoleHistory: [],
    historyIndex: 0,
  };
  let pendingReview = null;
  let toastTimer;
  let pointer = null;
  let keyboardCandle = null;
  let paletteIndex = 0;
  let paletteMatches = [];
  const selected = () => snapshot.tokens.find((t) => t.id === state.selected);
  const balance = () =>
    state.balances.find((b) => b.symbol === "SOL")?.qty ?? 0;
  const nativePrice = () =>
    snapshot.tokens.find((t) => t.symbol === "SOL")?.price;
  const owned = () =>
    state.positions
      .filter((p) => p.token_id === state.selected)
      .reduce((sum, p) => sum + (p.qty || 0), 0);
  const orderSupported = () =>
    isDemo && selected()?.chain === "sol" && selected()?.symbol !== "SOL";
  const toast = (message) => {
    const host = $("#toast");
    host.textContent = message;
    host.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      host.hidden = true;
    }, 3600);
  };
  function detail(title, html) {
    $("#detail-title").textContent = title;
    $("#detail-body").innerHTML = html;
    $("#detail-dialog").showModal();
  }
  const detailRow = (label, value, cls = "") =>
    `<div class="detail-row"><span>${esc(label)}</span><b class="${cls}">${esc(value)}</b></div>`;

  function sparkline(token) {
    const values = token.candles?.slice(-35).map((c) => c.close) || [];
    if (!values.length) return "";
    const lo = Math.min(...values),
      hi = Math.max(...values),
      range = hi - lo || 1;
    return `<svg class="sparkline" viewBox="0 0 60 26" aria-hidden="true"><polyline points="${values.map((v, i) => `${(i * 60) / (values.length - 1)},${23 - ((v - lo) / range) * 20}`).join(" ")}" fill="none" stroke="${token.change >= 0 ? "#55d6ac" : "#ed8094"}" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
  }
  function renderMarket() {
    const tokens = snapshot.tokens.filter((t) =>
      ["SOL", "ETH", "BNB"].includes(t.symbol),
    );
    $("#market-strip").innerHTML = tokens.length
      ? tokens
          .map(
            (t) =>
              `<button class="ticker-item" data-token="${esc(t.id)}">${coin(t)}<strong>${esc(t.symbol)}</strong><span class="ticker-price">${price(t.price)}</span><span class="ticker-change ${color(t.change)}">${pct(t.change)}</span><span class="ticker-note">${isDemo ? "SAMPLE" : "STORED"}</span></button>`,
          )
          .join("")
      : `<div class="ticker-item"><span class="muted">Market quotes unavailable</span></div><div class="ticker-item"><span class="muted">Execution evidence stays explicit</span></div><div class="ticker-item"><span class="muted">${esc(snapshot.mode.toUpperCase())} · stored snapshot</span></div>`;
  }
  function renderWatch() {
    const tokens = snapshot.tokens.filter(
      (t) =>
        (state.chain === "all" ||
          (state.chain === "sol" ? t.chain === "sol" : t.chain !== "sol")) &&
        (!state.favoritesOnly || state.favorites.includes(t.id)) &&
        `${t.symbol} ${t.name} ${t.address || ""}`
          .toLowerCase()
          .includes(state.search.toLowerCase()),
    );
    $("#watch-count").textContent = tokens.length;
    $("#watch-rows").innerHTML = tokens.length
      ? tokens
          .map(
            (t) =>
              `<button class="watch-row ${t.id === state.selected ? "active" : ""}" data-token="${esc(t.id)}" aria-pressed="${t.id === state.selected}">${coin(t)}<span class="watch-name"><b>${esc(t.symbol)}${state.favorites.includes(t.id) ? " · ★" : ""}</b><small>${esc(t.name)}</small></span><span class="watch-values"><span>${price(t.price)}</span><span class="${color(t.change)}">${pct(t.change)}</span></span>${sparkline(t)}</button>`,
          )
          .join("")
      : `<p class="watch-empty">${snapshot.errors.tokens ? "Token data unavailable. Try Refresh after the database recovers." : "No matching tokens. Try another search or chain filter."}</p>`;
  }
  function portfolio() {
    const assets = state.balances.map((b) => ({
      symbol: b.symbol,
      value: b.qty * b.price,
      qty: b.qty,
      chain: b.chain,
    }));
    state.positions.forEach((p) => {
      if (finite(p.mark) && finite(p.qty))
        assets.push({
          symbol: p.symbol,
          value: p.qty * p.mark,
          qty: p.qty,
          chain: p.chain,
        });
    });
    const total = assets.reduce((s, a) => s + a.value, 0);
    const pnl = state.positions.reduce((s, p) => s + (p.pnl || 0), 0);
    return { assets, total, pnl };
  }
  function renderAllocation() {
    const host = $("#allocation-content");
    if (!isDemo) {
      host.innerHTML = `<p class="allocation-missing">Wallet balances and USD valuation are unavailable in this view. <a href="/trades" class="blue-text">View accounting ${icon("arrow")}</a></p>`;
      return;
    }
    const p = portfolio(),
      sorted = [...p.assets].sort((a, b) => b.value - a.value);
    const colors = [
      "#81acff",
      "#55d6ac",
      "#8a9bb4",
      "#c7b4dd",
      "#edbc73",
      "#607a9d",
    ];
    host.innerHTML = `<div class="allocation-bar" aria-label="Illustrative portfolio allocation">${sorted.map((a, i) => `<span style="width:${p.total ? (a.value / p.total) * 100 : 0}%;background:${colors[i % colors.length]}"></span>`).join("")}</div>${sorted
      .slice(0, 4)
      .map(
        (a, i) =>
          `<div class="allocation-row"><span><i class="allocation-dot" style="background:${colors[i]}"></i>${esc(a.symbol)}</span><span>${num((a.value / p.total) * 100, 1)}%</span><span class="mono">${dollars(a.value)}</span></div>`,
      )
      .join(
        "",
      )}<div class="allocation-total"><div><span class="muted">Total balance</span><strong>${dollars(p.total)}</strong></div><div><span class="muted">Unrealized P&L</span><strong class="${color(p.pnl)}">${signed(p.pnl)}</strong></div><div class="allocation-note">Sample portfolio · fees excluded</div></div>`;
  }
  function renderHeader() {
    const t = selected();
    $("#token-header").innerHTML = t
      ? `${coin(t)}<div class="token-title"><h1>${esc(t.symbol)}<span>/ USD</span><button class="icon-button" id="token-favorite" title="Favorite token" aria-label="Favorite ${esc(t.symbol)}" aria-pressed="${state.favorites.includes(t.id)}">${icon("star")}</button></h1><p class="token-meta">${esc(t.name)}<span class="chain-label">${esc(chainName(t.chain))}</span></p></div><div class="token-quote"><strong>${price(t.price)}</strong><div class="${color(t.change)}">${pct(t.change)} <span class="muted">(24h${isDemo ? " sample" : ""})</span></div></div><div class="token-stats">${[
          ["Market cap", t.market_cap],
          ["Liquidity", t.liquidity],
          ["24h volume", t.volume],
        ]
          .map(
            ([label, value]) =>
              `<div class="token-stat"><span>${label}</span><b>${compact(value)}</b></div>`,
          )
          .join("")}</div>`
      : `<div class="token-title"><h1>Trading terminal</h1><p>Select a token when data becomes available.</p></div>`;
    $("#chart-provenance").textContent = isDemo
      ? "ILLUSTRATIVE MARKET DATA"
      : "LIVE MARKET SOURCE UNAVAILABLE";
    $("#price-chart").setAttribute(
      "aria-label",
      `${t?.symbol || "Token"} ${state.timeframe} ${state.chart} chart. ${isDemo ? "Illustrative data; not current market prices." : "No live chart source available."}`,
    );
  }
  function eventHTML(e) {
    const symbol =
      e.kind === "risk" ? "shield" : e.kind === "paper" ? "bolt" : "file";
    return `<article class="activity-row ${esc(["risk", "paper", "quote"].includes(e.kind) ? e.kind : "quote")}"><time datetime="${new Date(e.time).toISOString()}">${time(e.time)}</time>${icon(symbol)}<div><h3>${esc(e.title)}</h3><p>${esc(e.detail)}</p></div></article>`;
  }
  function renderActivity() {
    $("#activity-feed").innerHTML = state.events.length
      ? state.events.slice(0, 3).map(eventHTML).join("")
      : `<p class="table-empty">${snapshot.errors.events ? "Event source unavailable" : "No recent events"}</p>`;
  }
  function showView(view) {
    state.view = view;
    $$(".nav-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === view);
      if (b.dataset.view === view) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    $("#chart-panel").hidden = view !== "terminal";
    $("#workspace-view").hidden = view === "terminal";
    if (view === "terminal") drawChart();
    else renderView();
  }
  function renderView() {
    const host = $("#workspace-view"),
      t = selected();
    if (state.view === "portfolio") {
      const p = portfolio();
      host.innerHTML = `<header class="view-heading"><div><h1>Portfolio</h1><p>${isDemo ? "Illustrative balances · browser session" : "Stored positions · balances unavailable"}</p></div>${icon("wallet")}</header><div class="portfolio-summary"><div><span>Total balance</span><strong>${isDemo ? dollars(p.total) : "—"}</strong><small>${isDemo ? "Demo valuation, fees excluded" : "No verified wallet balance feed"}</small></div><div><span>Unrealized P&L</span><strong class="${isDemo ? color(p.pnl) : "muted"}">${isDemo ? signed(p.pnl) : "—"}</strong><small>${state.positions.length} ${isDemo ? "simulated" : "stored"} positions</small></div></div>${isDemo ? p.assets.map((a) => `<div class="view-list-row">${coin(a)}<div><h3>${esc(a.symbol)}</h3><p>${quantity(a.qty)} ${esc(a.symbol)} · ${esc(chainName(a.chain))}</p></div><span>${dollars(a.value)}</span></div>`).join("") : `<div class="detail-text"><p>Prices, total balance, fees and current exposure need a verified source. Open positions remain listed below with their original mode and chain.</p><p><a href="/trades" class="blue-text">Open trade accounting →</a></p></div>`}`;
    } else if (state.view === "intelligence") {
      host.innerHTML = `<header class="view-heading"><div><h1>Token intelligence</h1><p>${esc(t?.symbol || "No token selected")} · ${isDemo ? "illustrative evidence workspace" : "source coverage"}</p></div>${icon("shield")}</header><div class="evidence-grid">${[
        [
          "Market observations",
          isDemo ? "Synthetic chart history" : "Unavailable",
        ],
        ["Contract safety", "Not verified"],
        ["Holder concentration", "Not verified"],
        ["Actual fee cashflows", "Unavailable"],
        ["Realized slippage", "Unavailable"],
        ["Win probability", "Not calibrated"],
      ]
        .map(
          ([label, value]) =>
            `<div><span>${label}</span><b class="${value.includes("Synthetic") ? "amber-text" : "muted"}">${value}</b></div>`,
        )
        .join(
          "",
        )}</div><p class="intelligence-note">Wallet counts, dossier grades and conviction scores are not calibrated win probabilities. They do not increase order size. Missing evidence stays unavailable.</p>${!isDemo ? '<a class="subtle-button" href="/wallets" style="margin-top:16px">Open wallet intelligence →</a>' : ""}`;
    } else if (state.view === "activity") {
      host.innerHTML = `<header class="view-heading"><div><h1>Activity</h1><p>${isDemo ? "Illustrative feed and actions from this session" : "Recent stored event headers · no inferred current health"}</p></div>${icon("clock")}</header><div class="activity-expanded">${state.events.map(eventHTML).join("") || '<p class="muted">No events available.</p>'}</div>`;
    }
  }
  function renderTable() {
    $("#positions-count").textContent = snapshot.positions_truncated
      ? "100+"
      : state.positions.length;
    $("#orders-count").textContent = state.orders.length;
    $$("[data-table]").forEach((b) => {
      b.classList.toggle("active", b.dataset.table === state.table);
      b.setAttribute("aria-pressed", String(b.dataset.table === state.table));
    });
    $("#console").hidden = state.table !== "console";
    $("#table-content").hidden = state.table === "console";
    $("#export-data").hidden = state.table === "console";
    $("#table-scope").textContent = isDemo
      ? "Browser session · fees excluded"
      : snapshot.positions_truncated
        ? "First 100 open positions · prices unavailable"
        : "Stored positions · mode & chain shown";
    $("#pnl-total").textContent = isDemo
      ? `Unrealized ${signed(portfolio().pnl)}`
      : "Unrealized P&L unavailable";
    $("#pnl-total").className = isDemo ? color(portfolio().pnl) : "muted";
    if (state.table === "console") return;
    if (state.table === "positions") {
      $("#table-content").innerHTML = state.positions.length
        ? `<table class="data-table"><thead><tr><th scope="col">Token</th><th scope="col">Entry</th><th scope="col">Mark</th><th scope="col">Size</th><th scope="col">Unrealized P&L</th><th scope="col">Protection</th></tr></thead><tbody>${state.positions.map((p) => `<tr><td><button class="position-token" data-position="${esc(p.id)}">${coin(p)}<span><b>${esc(p.symbol)}</b><small>${esc(p.mode)} · ${esc(chainName(p.chain))}</small></span></button></td><td>${price(p.entry)}</td><td>${price(p.mark)}</td><td>${quantity(p.qty)}<span class="cell-secondary">${finite(p.mark) ? dollars(p.qty * p.mark) : "Valuation unavailable"}</span></td><td class="${color(p.pnl)}">${signed(p.pnl)}<span class="cell-secondary">${finite(p.entry) && p.entry > 0 && finite(p.mark) ? pct((p.mark / p.entry - 1) * 100) : "No fresh quote"}</span></td><td class="protection-cell">${esc(p.protection)}<span class="cell-secondary ${isDemo ? "positive" : "muted"}">${isDemo ? "Illustrative" : "Freshness unverified"}</span></td></tr>`).join("")}</tbody></table>`
        : `<p class="table-empty">${snapshot.errors.positions ? "Position source unavailable. A failed read is not an empty portfolio." : "No open positions in this snapshot."}</p>`;
    } else if (state.table === "orders") {
      $("#table-content").innerHTML = state.orders.length
        ? `<table class="data-table"><thead><tr><th>Token</th><th>Side</th><th>Limit</th><th>Quantity</th><th>Status</th><th>Action</th></tr></thead><tbody>${state.orders.map((o) => `<tr><td>${esc(o.symbol)}</td><td class="${o.side === "buy" ? "positive" : "negative"}">${o.side.toUpperCase()}</td><td>${price(o.price)}</td><td>${quantity(o.qty)}</td><td class="amber-text">Unsubmitted draft</td><td><button class="cancel-order" data-cancel="${esc(o.id)}">Remove</button></td></tr>`).join("")}</tbody></table>`
        : `<p class="table-empty">${isDemo ? "No limit drafts. Market simulations appear in Trade history." : "Live order state is available in the existing operator console."}</p>`;
    } else {
      $("#table-content").innerHTML = state.history.length
        ? `<table class="data-table"><thead><tr><th>Time (UTC)</th><th>Token</th><th>Side</th><th>Quantity</th><th>Price</th><th>Status</th></tr></thead><tbody>${state.history.map((o) => `<tr><td>${time(o.time)}</td><td>${esc(o.symbol)}</td><td class="${o.side === "buy" ? "positive" : "negative"}">${o.side.toUpperCase()}</td><td>${quantity(o.qty)}</td><td>${price(o.price)}</td><td class="muted">${esc(o.status)}</td></tr>`).join("")}</tbody></table>`
        : `<p class="table-empty">${isDemo ? "Your paper market orders will appear here. Session resets on reload." : 'Open <a class="blue-text" href="/trades">trade accounting</a> for actual closed trades.'}</p>`;
    }
  }
  function updateTicket() {
    const t = selected(),
      sell = state.side === "sell",
      max = sell ? owned() : balance();
    $("#order-fields").disabled = !orderSupported();
    $("#amount-unit").textContent = sell ? t?.symbol || "TOKEN" : "SOL";
    $("#available-balance").textContent = isDemo
      ? `${sell ? "Held" : "Balance"}: ${quantity(max)} ${sell ? t?.symbol || "" : "SOL"}`
      : "Balance unavailable";
    $(".side-switch").classList.toggle("sell", sell);
    $$("[data-side]").forEach((b) => {
      b.classList.toggle("active", b.dataset.side === state.side);
      b.setAttribute("aria-pressed", String(b.dataset.side === state.side));
    });
    $$("[data-order-type]").forEach((b) => {
      b.classList.toggle("active", b.dataset.orderType === state.type);
      b.setAttribute(
        "aria-pressed",
        String(b.dataset.orderType === state.type),
      );
    });
    $("#limit-field").hidden = state.type !== "limit";
    $("#limit-price").required = state.type === "limit";
    $("#review-order").innerHTML =
      `${state.type === "limit" ? "Review limit draft" : "Review paper order"}${icon("arrow")}`;
    $(".order-disclaimer span").textContent = !isDemo
      ? "Execution is available through existing agent controls."
      : !orderSupported()
        ? "Select a Solana token to simulate an order."
        : "Simulated execution · no funds move";
    const amount = Number($("#order-amount").value),
      unitPrice =
        state.type === "limit" ? Number($("#limit-price").value) : t?.price;
    const receive = sell
      ? (amount * unitPrice) / nativePrice()
      : (amount * nativePrice()) / unitPrice;
    $("#receive-estimate").textContent =
      orderSupported() && finite(receive) && receive > 0
        ? `${quantity(receive)} ${sell ? "SOL" : t.symbol}`
        : "—";
  }
  function selectToken(id) {
    if (!snapshot.tokens.some((t) => t.id === id)) return;
    state.selected = id;
    pointer = null;
    keyboardCandle = null;
    const t = selected();
    $("#limit-price").value = finite(t.price) ? t.price : "";
    $("#order-error").hidden = true;
    closeWatch();
    renderWatch();
    renderHeader();
    updateTicket();
    drawChart();
    if (state.view !== "terminal") renderView();
  }
  function aggregate(candles) {
    const bucket = { "15m": 1, "1h": 4, "4h": 16, "1D": 96 }[state.timeframe];
    const groups = new Map();
    for (const c of candles) {
      const key = Math.floor(c.time / (bucket * 900000));
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(c);
    }
    return [...groups.values()]
      .map((g) => ({
        time: g[0].time,
        open: g[0].open,
        close: g.at(-1).close,
        high: Math.max(...g.map((c) => c.high)),
        low: Math.min(...g.map((c) => c.low)),
        volume: g.reduce((s, c) => s + c.volume, 0),
      }))
      .slice(-92);
  }
  function drawChart() {
    const canvas = $("#price-chart"),
      host = $(".chart-stage"),
      t = selected();
    canvas.setAttribute(
      "aria-label",
      `${t?.symbol || "Token"} ${state.timeframe} ${state.chart} chart. ${isDemo ? "Illustrative data; not current market prices." : "No live chart source available."}`,
    );
    if ($("#chart-panel").hidden) return;
    const w = host.clientWidth,
      h = host.clientHeight;
    if (!w || !h) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    const candles = t?.candles || [];
    const empty = $("#chart-empty");
    empty.hidden = candles.length > 0;
    if (!candles.length) {
      empty.innerHTML = `${icon("chart")}<h3>Price history unavailable</h3><p>${esc(snapshot.market_reason || "No market source connected. Prices and returns are not inferred.")}</p>`;
      $("#chart-legend").textContent = "";
      return;
    }
    const data = aggregate(candles);
    const r = w < 500 ? 66 : 80,
      l = 9,
      top = 53,
      bottom = h - 31,
      volHeight = Math.min(65, h * 0.18),
      priceBottom = bottom - volHeight - 13;
    const chartW = w - r - l,
      chartH = priceBottom - top;
    let low = Math.min(...data.map((c) => c.low)),
      high = Math.max(...data.map((c) => c.high));
    const pad = (high - low) * 0.13 || high * 0.01;
    high += pad;
    low -= pad;
    const y = (v) => top + ((high - v) / (high - low)) * chartH,
      step = chartW / data.length,
      x = (i) => l + step * (i + 0.5);
    ctx.font = "10px Consolas, monospace";
    ctx.textBaseline = "middle";
    ctx.strokeStyle = "#232d3d77";
    ctx.lineWidth = 1;
    for (let i = 0; i < 6; i++) {
      const yy = top + (i * chartH) / 5;
      ctx.beginPath();
      ctx.moveTo(l, Math.round(yy) + 0.5);
      ctx.lineTo(w - r, Math.round(yy) + 0.5);
      ctx.stroke();
      ctx.fillStyle = "#71849e";
      ctx.fillText(
        (high - (i * (high - low)) / 5).toFixed(
          t.price < 0.001 ? 8 : t.price < 1 ? 4 : 2,
        ),
        w - r + 9,
        yy,
      );
    }
    ctx.beginPath();
    ctx.moveTo(w - r + 0.5, 0);
    ctx.lineTo(w - r + 0.5, bottom);
    ctx.moveTo(l, priceBottom + 12.5);
    ctx.lineTo(w, priceBottom + 12.5);
    ctx.moveTo(l, bottom + 0.5);
    ctx.lineTo(w, bottom + 0.5);
    ctx.stroke();
    const labelCount = Math.min(data.length, w < 500 ? 4 : 6);
    for (let i = 0; i < labelCount; i++) {
      const index = Math.min(
          data.length - 1,
          Math.round((i * (data.length - 1)) / (labelCount - 1)),
        ),
        xx = x(index);
      ctx.beginPath();
      ctx.moveTo(Math.round(xx) + 0.5, 0);
      ctx.lineTo(Math.round(xx) + 0.5, bottom);
      ctx.stroke();
      ctx.fillStyle = "#71849e";
      ctx.textAlign =
        i === 0 ? "left" : i === labelCount - 1 ? "right" : "center";
      const date = new Date(data[index].time);
      const label =
        state.timeframe === "15m"
          ? date.toISOString().slice(11, 16)
          : date.toISOString().slice(5, 10) +
            " " +
            date.toISOString().slice(11, 13) +
            "h";
      ctx.fillText(label, xx, bottom + 17);
    }
    ctx.textAlign = "left";
    const maxVol = Math.max(...data.map((c) => c.volume)),
      candleW = Math.max(2, Math.min(10, step * 0.64));
    data.forEach((c, i) => {
      const up = c.close >= c.open;
      ctx.fillStyle = up ? "#55d6ac" : "#ed8094";
      ctx.strokeStyle = ctx.fillStyle;
      if (state.chart === "candles") {
        ctx.beginPath();
        ctx.moveTo(x(i), y(c.high));
        ctx.lineTo(x(i), y(c.low));
        ctx.stroke();
        ctx.fillRect(
          x(i) - candleW / 2,
          Math.min(y(c.open), y(c.close)),
          candleW,
          Math.max(1, Math.abs(y(c.open) - y(c.close))),
        );
      }
      ctx.globalAlpha = 0.35;
      const vh = (c.volume / maxVol) * (volHeight - 12);
      ctx.fillRect(x(i) - candleW / 2, bottom - vh, candleW, vh);
      ctx.globalAlpha = 1;
    });
    ctx.strokeStyle = state.chart === "line" ? "#55d6ac" : "#81acff";
    ctx.lineWidth = 1.25;
    ctx.beginPath();
    let started = false;
    data.forEach((c, i) => {
      if (state.chart === "candles" && i < 19) return;
      const value =
        state.chart === "line"
          ? c.close
          : data.slice(i - 19, i + 1).reduce((s, d) => s + d.close, 0) / 20;
      if (!started) {
        ctx.moveTo(x(i), y(value));
        started = true;
      } else ctx.lineTo(x(i), y(value));
    });
    ctx.stroke();
    const current = data.at(-1).close,
      cy = y(current);
    ctx.strokeStyle = "#55d6ac99";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(l, cy);
    ctx.lineTo(w - r, cy);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#55d6ac";
    ctx.fillRect(w - r, cy - 10, r, 20);
    ctx.fillStyle = "#0b1921";
    ctx.font = "9px Consolas, monospace";
    ctx.fillText(
      current.toFixed(t.price < 0.001 ? 8 : t.price < 1 ? 4 : 2),
      w - r + 7,
      cy,
    );
    ctx.fillStyle = "#71849e";
    ctx.font = "9px Segoe UI";
    ctx.fillText("Volume (sample)", l + 4, priceBottom + 25);
    const keyboardIndex = Math.max(
      0,
      Math.min(data.length - 1, keyboardCandle ?? data.length - 1),
    );
    const inspectPoint =
      keyboardCandle === null
        ? pointer
        : { x: x(keyboardIndex), y: y(data[keyboardIndex].close) };
    let active = data.at(-1);
    if (
      inspectPoint &&
      inspectPoint.x >= l &&
      inspectPoint.x < w - r &&
      inspectPoint.y > top &&
      inspectPoint.y < bottom
    ) {
      const index = Math.max(
        0,
        Math.min(data.length - 1, Math.floor((inspectPoint.x - l) / step)),
      );
      active = data[index];
      ctx.strokeStyle = "#8591a688";
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(x(index), top);
      ctx.lineTo(x(index), bottom);
      ctx.moveTo(l, inspectPoint.y);
      ctx.lineTo(w - r, inspectPoint.y);
      ctx.stroke();
      ctx.setLineDash([]);
      const label =
        new Date(active.time).toISOString().slice(5, 16).replace("T", " ") +
        " UTC";
      ctx.fillStyle = "#243349";
      ctx.fillRect(
        Math.min(Math.max(x(index) - 61, l), w - r - 125),
        bottom + 4,
        125,
        21,
      );
      ctx.fillStyle = "#d4e0f2";
      ctx.font = "9px Consolas";
      ctx.fillText(
        label,
        Math.min(Math.max(x(index) - 55, l + 6), w - r - 119),
        bottom + 15,
      );
    }
    const decimals = t.price < 0.001 ? 8 : t.price < 1 ? 4 : 2;
    $("#chart-legend").innerHTML =
      ["open", "high", "low", "close"]
        .map(
          (k) =>
            `<span>${k[0].toUpperCase()}<strong>${active[k].toFixed(decimals)}</strong></span>`,
        )
        .join("") +
      `<span class="ma-legend">${state.chart === "candles" ? "MA (20) · " : "Line · "}${isDemo ? "synthetic history" : "stored history"}</span>`;
    const reading = `${t.symbol} ${state.timeframe}, ${new Date(active.time).toISOString().replace("T", " ").replace(".000Z", " UTC")}. Open ${price(active.open)}, high ${price(active.high)}, low ${price(active.low)}, close ${price(active.close)}. Volume ${quantity(active.volume)}. ${isDemo ? "Illustrative history." : "Stored history."}`;
    if ($("#chart-reading").textContent !== reading)
      $("#chart-reading").textContent = reading;
  }

  function reviewOrder(event) {
    event.preventDefault();
    if (!orderSupported()) return;
    const t = selected(),
      amount = Number($("#order-amount").value),
      unitPrice =
        state.type === "limit" ? Number($("#limit-price").value) : t.price;
    const tp = $("#take-profit").value ? Number($("#take-profit").value) : null,
      sl = $("#stop-loss").value ? Number($("#stop-loss").value) : null;
    let error = "";
    if (!finite(amount) || amount <= 0) error = "Enter an amount above zero.";
    else if (!finite(unitPrice) || unitPrice <= 0)
      error = "Enter a valid positive limit price.";
    else if (state.side === "buy" && amount > balance() + 1e-10)
      error = "Amount exceeds your simulated SOL balance.";
    else if (state.side === "sell" && amount > owned() + 1e-10)
      error = "Amount exceeds your simulated token holdings.";
    else if (state.history.length >= 200 || state.orders.length >= 100)
      error =
        "Session limit reached. Export your activity and reload to reset.";
    if (error) {
      $("#order-error").textContent = error;
      $("#order-error").hidden = false;
      return;
    }
    $("#order-error").hidden = true;
    const qty =
        state.side === "buy" ? (amount * nativePrice()) / unitPrice : amount,
      native =
        state.side === "buy" ? amount : (amount * unitPrice) / nativePrice();
    pendingReview = {
      id: crypto.randomUUID(),
      symbol: t.symbol,
      token_id: t.id,
      side: state.side,
      type: state.type,
      amount,
      qty,
      native,
      price: unitPrice,
      tp,
      sl,
      time: Date.now(),
    };
    $("#review-title").textContent =
      state.type === "limit" ? "Review limit draft" : "Review paper order";
    $("#review-body").innerHTML =
      detailRow("Action", `${state.side.toUpperCase()} ${t.symbol}`) +
      detailRow(
        "Type",
        state.type === "limit"
          ? "Unsubmitted limit draft"
          : "Market simulation",
      ) +
      detailRow("Quantity", `${quantity(qty)} ${t.symbol}`) +
      detailRow("Sample price", price(unitPrice)) +
      detailRow("SOL value", `${quantity(native)} SOL`) +
      detailRow("Take profit / stop loss", `${tp ?? "—"}% / ${sl ?? "—"}%`) +
      detailRow("Actual costs", "Unavailable", "amber-text");
    $("#confirm-order").textContent =
      state.type === "limit" ? "Save unsubmitted draft" : "Confirm paper order";
    $("#review-dialog").showModal();
  }
  function confirmOrder() {
    const o = pendingReview;
    if (!isDemo || !o) return;
    // Recheck holdings at confirmation. Never call an execution, provider or POST route.
    const held = state.positions
      .filter((p) => p.token_id === o.token_id)
      .reduce((s, p) => s + p.qty, 0);
    if (
      (o.side === "buy" && o.native > balance() + 1e-10) ||
      (o.side === "sell" && o.qty > held + 1e-10)
    ) {
      toast("Balance changed. Review the ticket again.");
      $("#review-dialog").close();
      pendingReview = null;
      return;
    }
    pendingReview = null;
    if (o.type === "limit") {
      state.orders.unshift(o);
      state.table = "orders";
      toast(
        "Limit draft saved locally. No funds reserved; no matching engine.",
      );
    } else {
      const cash = state.balances.find((b) => b.symbol === "SOL");
      cash.qty += o.side === "buy" ? -o.native : o.native;
      if (o.side === "buy")
        state.positions.unshift({
          id: o.id,
          token_id: o.token_id,
          symbol: o.symbol,
          chain: "sol",
          mode: "demo",
          lane: "Manual simulation",
          qty: o.qty,
          entry: o.price,
          mark: o.price,
          pnl: 0,
          stop: o.sl ? o.price * (1 - o.sl / 100) : null,
          protection: `${o.tp || o.sl ? "TP / SL" : "No exits"} · simulated`,
        });
      else {
        let remaining = o.qty;
        for (const p of state.positions.filter(
          (p) => p.token_id === o.token_id,
        )) {
          const sold = Math.min(remaining, p.qty);
          p.qty -= sold;
          remaining -= sold;
          p.pnl = (p.mark - p.entry) * p.qty;
          if (remaining <= 1e-10) break;
        }
        state.positions = state.positions.filter((p) => p.qty > 1e-10);
      }
      state.history.unshift({ ...o, status: "Simulated · no fees" });
      state.table = "history";
      toast(`Paper ${o.side} recorded. No funds moved.`);
    }
    state.events.unshift({
      time: Date.now(),
      title:
        o.type === "limit" ? "Limit draft saved" : `Paper ${o.side} recorded`,
      detail: `${o.symbol} · browser simulation only · costs unavailable`,
      kind: "paper",
    });
    state.events = state.events.slice(0, 50);
    $("#review-dialog").close();
    renderTable();
    renderAllocation();
    renderActivity();
    updateTicket();
    if (state.view !== "terminal") renderView();
  }
  function positionDetail(id) {
    const p = state.positions.find((p) => p.id === id);
    if (!p) return;
    detail(
      `${p.symbol} position`,
      detailRow("Chain / mode", `${chainName(p.chain)} / ${p.mode}`) +
        detailRow("Lane", p.lane || "—") +
        detailRow("Quantity", quantity(p.qty)) +
        detailRow("Entry price", price(p.entry)) +
        detailRow("Current mark", price(p.mark)) +
        detailRow("Unrealized P&L", signed(p.pnl), color(p.pnl)) +
        detailRow("Stored stop", price(p.stop)) +
        detailRow("Protection", p.protection) +
        detailRow("Fees / slippage", "Unavailable", "amber-text") +
        '<p class="detail-text" style="margin-top:16px">' +
        (isDemo
          ? "Simulated position. TP / SL does not trigger automatic exits in this preview."
          : "Stored protection is not proof of current watchdog health. Check the operator console for the latest protection state.") +
        "</p>",
    );
  }
  function toggleFavorite() {
    const t = selected();
    if (!t) return;
    const i = state.favorites.indexOf(t.id);
    if (i >= 0) state.favorites.splice(i, 1);
    else state.favorites.push(t.id);
    state.favorites = state.favorites.slice(-100);
    try {
      localStorage.setItem(
        "kaiba-terminal-favorites-v1",
        JSON.stringify(state.favorites),
      );
    } catch (_) {
      toast("Favorites updated for this session. Storage unavailable.");
    }
    renderHeader();
    renderWatch();
  }
  function exportData() {
    const type = state.table,
      rows =
        type === "orders"
          ? state.orders
          : type === "history"
            ? state.history
            : state.positions;
    const columns =
      type === "positions"
        ? [
            "symbol",
            "chain",
            "mode",
            "qty",
            "entry",
            "mark",
            "pnl",
            "protection",
          ]
        : ["symbol", "side", "type", "qty", "price", "status", "time"];
    // Quote and neutralize spreadsheet formula prefixes even in stored token names.
    const csvCell = (value) => {
      let text = String(value ?? "");
      if (typeof value !== "number" && /^\s*[=+\-@]/u.test(text))
        text = "'" + text;
      return '"' + text.replace(/"/g, '""') + '"';
    };
    const csv = [
      columns.join(","),
      ...rows.map((row) => columns.map((c) => csvCell(row[c])).join(",")),
    ].join("\r\n");
    const url = URL.createObjectURL(
      new Blob([csv], { type: "text/csv;charset=utf-8" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `kaiba-${isDemo ? "demo" : "stored"}-${type}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Exported ${rows.length} ${type} rows.`);
  }
  function consoleLine(text, entry = false) {
    const row = document.createElement("div");
    row.textContent = text;
    if (entry) row.className = "console-entry";
    $("#console-output").append(row);
    while ($("#console-output").childElementCount > 150)
      $("#console-output").firstElementChild.remove();
    $("#console-output").scrollTop = $("#console-output").scrollHeight;
  }
  function runCommand(raw) {
    const parts = raw.trim().split(/\s+/),
      command = (parts.shift() || "").toLowerCase();
    if (!command) return;
    state.consoleHistory.push(raw);
    state.historyIndex = state.consoleHistory.length;
    consoleLine(`kaiba › ${raw}`, true);
    if (command === "help")
      consoleLine(
        "help · status · token SYMBOL · positions · orders · history · risk · clear\nRead-only local commands. Use the existing Commands page for service actions.",
      );
    else if (command === "status")
      consoleLine(
        `${isDemo ? "DEMO WORKSPACE — all market data synthetic" : "STORED SNAPSHOT — read only"}\n${state.positions.length} ${isDemo ? "demo" : "stored"} positions · ${state.orders.length} local limit drafts\nCollected ${new Date(snapshot.generated_ms).toISOString()}\nFees and actual slippage: unavailable. Protection freshness: ${isDemo ? "simulated" : "unverified"}.`,
      );
    else if (command === "risk")
      consoleLine(
        "No risk configuration changed. Confidence, wallet count and dossier grades do not increase position size.\n" +
          (isDemo
            ? "Paper operations are browser-local. TP / SL does not run an exit engine."
            : "Open Command centre → Risk for authenticated bounded controls."),
      );
    else if (["positions", "orders", "history"].includes(command))
      consoleLine(
        command === "positions"
          ? state.positions
              .map(
                (p) =>
                  `${p.symbol.padEnd(9)} ${quantity(p.qty)} · ${p.mode} · ${chainName(p.chain)} · mark ${price(p.mark)}`,
              )
              .join("\n") || "No stored positions."
          : command === "orders"
            ? state.orders
                .map(
                  (o) =>
                    `${o.symbol} ${o.side.toUpperCase()} ${quantity(o.qty)} @ ${price(o.price)} — unsubmitted draft`,
                )
                .join("\n") || "No local limit drafts."
            : state.history
                .map(
                  (o) =>
                    `${time(o.time)} ${o.symbol} ${o.side.toUpperCase()} ${quantity(o.qty)} — ${o.status}`,
                )
                .join("\n") || "No session history.",
      );
    else if (command === "token") {
      const t = snapshot.tokens.find(
        (t) => t.symbol.toLowerCase() === parts.join(" ").toLowerCase(),
      );
      if (t) {
        selectToken(t.id);
        consoleLine(
          `Selected ${t.symbol} · ${chainName(t.chain)} · ${isDemo ? "illustrative price" : "price unavailable"} ${price(t.price)}`,
        );
      } else consoleLine("Token not found. Use a symbol from the watchlist.");
    } else if (command === "clear") $("#console-output").replaceChildren();
    else
      consoleLine(
        `Unknown command: ${command}. Type help. No shell command was executed.`,
      );
  }
  function showHelp() {
    detail(
      "Your trading workspace",
      '<div class="detail-text"><p><b>Find a token</b> in the watchlist. Filter by chain, search its name, or star it for later.</p><p><b>Inspect the market</b> with candle/line views, timeframes and a chart crosshair. All demo history is synthetic.</p><p><b>Paper orders</b> use the sample price and browser memory. Fees are unknown; limit drafts reserve no balance and do not auto-fill. Reload resets the paper session.</p><p><b>Keyboard</b> <code>Ctrl / ⌘ K</code> command palette · <code>/</code> watchlist search · <code>Esc</code> close. Open Terminal for <code>help</code>, <code>status</code> and <code>risk</code>.</p></div>',
    );
  }
  const commands = () => [
    {
      label: "Trading terminal",
      meta: "Workspace",
      icon: "chart",
      run: () => showView("terminal"),
    },
    {
      label: "Portfolio",
      meta: "Workspace",
      icon: "wallet",
      run: () => showView("portfolio"),
    },
    {
      label: "Token intelligence",
      meta: "Workspace",
      icon: "shield",
      run: () => showView("intelligence"),
    },
    {
      label: "Agent activity",
      meta: "Workspace",
      icon: "bot",
      run: () => showView("activity"),
    },
    {
      label: "Open command terminal",
      meta: "Local commands",
      icon: "terminal",
      run: () => {
        state.table = "console";
        renderTable();
        $("#console-input").focus();
      },
    },
    {
      label: "Export positions",
      meta: "CSV",
      icon: "download",
      run: () => {
        state.table = "positions";
        renderTable();
        exportData();
      },
    },
    {
      label: "Keyboard shortcuts & help",
      meta: "Guide",
      icon: "info",
      run: showHelp,
    },
    ...snapshot.tokens.map((t) => ({
      label: `${t.symbol} · ${t.name}`,
      meta: chainName(t.chain),
      icon: "search",
      run: () => {
        selectToken(t.id);
        showView("terminal");
      },
    })),
  ];
  function renderPalette() {
    const q = $("#command-input").value.toLowerCase();
    paletteMatches = commands().filter((c) =>
      `${c.label} ${c.meta}`.toLowerCase().includes(q),
    );
    paletteIndex = Math.min(
      Math.max(0, paletteIndex),
      Math.max(0, paletteMatches.length - 1),
    );
    $("#command-results").innerHTML = paletteMatches.length
      ? paletteMatches
          .map(
            (c, i) =>
              `<button class="command-result ${i === paletteIndex ? "active" : ""}" data-command="${i}" role="listitem">${icon(c.icon)}<span>${esc(c.label)}</span><small>${esc(c.meta)}</small></button>`,
          )
          .join("")
      : '<p class="table-empty">No matching commands or tokens.</p>';
  }
  function openPalette() {
    paletteIndex = 0;
    $("#command-input").value = "";
    renderPalette();
    $("#command-dialog").showModal();
    $("#command-input").focus();
  }
  function activateCommand(i) {
    const cmd = paletteMatches[i];
    if (!cmd) return;
    $("#command-dialog").close();
    cmd.run();
  }
  function renderClock() {
    const age = Math.max(
      0,
      Math.floor((Date.now() - snapshot.generated_ms) / 1000),
    );
    $("#snapshot-clock").textContent =
      new Date(snapshot.generated_ms)
        .toISOString()
        .slice(0, 19)
        .replace("T", " ") + " UTC";
    $("#freshness-label").textContent =
      `${isDemo ? "Sample" : "Snapshot"} · ${age < 60 ? age + "s" : Math.floor(age / 60) + "m"} ago`;
    if (!isDemo && age > 60) {
      $("#connection-status").innerHTML =
        '<i class="dot amber"></i>Snapshot stale';
    }
  }
  async function refreshSnapshot() {
    if (isDemo) {
      toast(
        "Demo session retained. Reload the page to reset all paper activity.",
      );
      return;
    }
    const b = $("#refresh-snapshot");
    b.disabled = true;
    try {
      const response = await fetch("/api/terminal", {
        cache: "no-store",
        credentials: "same-origin",
        signal: AbortSignal.timeout(12000),
      });
      if (!response.ok) throw new Error("Unavailable");
      const next = await response.json();
      if (
        next.demo !== false ||
        !Array.isArray(next.tokens) ||
        !Array.isArray(next.positions)
      )
        throw new Error("Invalid snapshot");
      snapshot = next;
      state.positions = structuredClone(next.positions);
      state.events = structuredClone(next.events);
      if (!selected()) state.selected = next.tokens[0]?.id;
      renderMarket();
      renderWatch();
      renderHeader();
      renderTable();
      renderActivity();
      renderAllocation();
      drawChart();
      renderClock();
      if (state.view !== "terminal") renderView();
      $("#connection-status").innerHTML =
        '<i class="dot mint"></i>Stored snapshot';
      toast(
        Object.keys(next.errors).length
          ? "Some sources are unavailable. Missing data remains explicit."
          : "Stored snapshot refreshed.",
      );
    } catch (_) {
      $("#connection-status").innerHTML =
        '<i class="dot amber"></i>Refresh failed · stale';
      toast(
        "Refresh failed. Showing the previous snapshot and its original timestamp.",
      );
    } finally {
      b.disabled = false;
    }
  }
  document.addEventListener("click", (event) => {
    const target = event.target.closest("button,a");
    if (!target) return;
    if (target.dataset.token) {
      selectToken(target.dataset.token);
      return;
    }
    if (target.dataset.chain) {
      state.chain = target.dataset.chain;
      $$("[data-chain]").forEach((b) => {
        b.classList.toggle("active", b === target);
        b.setAttribute("aria-pressed", String(b === target));
      });
      renderWatch();
    }
    if (target.dataset.view) showView(target.dataset.view);
    if (target.dataset.table) {
      state.table = target.dataset.table;
      renderTable();
      if (state.table === "console") $("#console-input").focus();
    }
    if (target.dataset.time) {
      state.timeframe = target.dataset.time;
      pointer = null;
      keyboardCandle = null;
      $$("[data-time]").forEach((b) => {
        b.classList.toggle("active", b === target);
        b.setAttribute("aria-pressed", String(b === target));
      });
      drawChart();
    }
    if (target.dataset.side) {
      state.side = target.dataset.side;
      $("#order-amount").value =
        state.side === "buy" ? "0.10" : String(Math.min(owned(), 100));
      updateTicket();
    }
    if (target.dataset.orderType) {
      state.type = target.dataset.orderType;
      updateTicket();
    }
    if (target.dataset.amount) {
      const max = state.side === "buy" ? balance() : owned();
      $("#order-amount").value = String(
        Number((max * Number(target.dataset.amount)).toFixed(8)),
      );
      updateTicket();
    }
    if (target.dataset.position) positionDetail(target.dataset.position);
    if (target.dataset.cancel) {
      const index = state.orders.findIndex(
        (o) => o.id === target.dataset.cancel,
      );
      if (index >= 0 && isDemo) {
        const [order] = state.orders.splice(index, 1);
        state.history.unshift({
          ...order,
          time: Date.now(),
          status: "Draft removed",
        });
        renderTable();
        toast("Local limit draft removed.");
      }
    }
    if (target.dataset.command !== undefined)
      activateCommand(Number(target.dataset.command));
    if (target.hasAttribute("data-close-dialog"))
      target.closest("dialog").close();
    if (target.id === "token-favorite") toggleFavorite();
    if (
      isDemo &&
      target.tagName === "A" &&
      target.getAttribute("href").startsWith("/") &&
      target.getAttribute("href") !== "/terminal"
    ) {
      event.preventDefault();
      detail(
        "Connected operator tools",
        '<div class="detail-text"><p>The connected dashboard includes wallet intelligence, agent chat, bounded risk controls and named service commands.</p><p>This isolated preview has no connection to the trading agent. Open <code>/terminal</code> on the authenticated operator dashboard to use its existing tools.</p></div>',
      );
    }
  });
  $("#watch-search").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderWatch();
  });
  $("#favorites-toggle").addEventListener("click", (e) => {
    state.favoritesOnly = !state.favoritesOnly;
    e.currentTarget.setAttribute("aria-pressed", String(state.favoritesOnly));
    renderWatch();
  });
  $("#chart-type").addEventListener("change", (e) => {
    state.chart = e.target.value;
    drawChart();
  });
  $("#chart-expand").addEventListener("click", () => {
    const on = document.body.classList.toggle("expanded-chart");
    $("#chart-expand").setAttribute("aria-pressed", String(on));
    drawChart();
  });
  $("#price-chart").addEventListener("pointermove", (e) => {
    keyboardCandle = null;
    const rect = e.currentTarget.getBoundingClientRect();
    pointer = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    drawChart();
  });
  $("#price-chart").addEventListener("pointerleave", () => {
    pointer = null;
    drawChart();
  });
  $("#price-chart").addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const candles = selected()?.candles || [];
    if (!candles.length) return;
    e.preventDefault();
    const last = aggregate(candles).length - 1;
    keyboardCandle =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? last
          : Math.max(
              0,
              Math.min(
                last,
                (keyboardCandle ?? last) + (e.key === "ArrowLeft" ? -1 : 1),
              ),
            );
    pointer = null;
    drawChart();
  });
  $("#price-chart").addEventListener("blur", () => {
    keyboardCandle = null;
    pointer = null;
    drawChart();
  });
  new ResizeObserver(() => drawChart()).observe($(".chart-stage"));
  $("#order-form").addEventListener("submit", reviewOrder);
  $("#order-form").addEventListener("input", updateTicket);
  $("#confirm-order").addEventListener("click", confirmOrder);
  $("#review-dialog").addEventListener("close", () => {
    pendingReview = null;
  });
  $("#order-info").addEventListener("click", () =>
    detail(
      "Paper execution",
      '<div class="detail-text"><p>Market simulations use the selected illustrative price. No orders are sent and no real balances change.</p><p>Limit drafts are unsubmitted, reserve no balance and have no automatic fill mechanism. TP / SL does not run a protection engine. Fees, execution slippage and profitability are unproved.</p></div>',
    ),
  );
  $("#console-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runCommand($("#console-input").value);
    $("#console-input").value = "";
  });
  $("#console-input").addEventListener("keydown", (e) => {
    if (["ArrowUp", "ArrowDown"].includes(e.key)) {
      e.preventDefault();
      state.historyIndex = Math.max(
        0,
        Math.min(
          state.consoleHistory.length,
          state.historyIndex + (e.key === "ArrowUp" ? -1 : 1),
        ),
      );
      e.target.value = state.consoleHistory[state.historyIndex] || "";
    }
  });
  $("#terminal-toggle").addEventListener("click", () => {
    state.table = "console";
    renderTable();
    $("#console-input").focus();
  });
  $("#command-input").addEventListener("input", () => {
    paletteIndex = 0;
    renderPalette();
  });
  $("#command-input").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      paletteIndex =
        (paletteIndex +
          (e.key === "ArrowDown" ? 1 : -1) +
          Math.max(1, paletteMatches.length)) %
        Math.max(1, paletteMatches.length);
      renderPalette();
      $(".command-result.active")?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      activateCommand(paletteIndex);
    }
  });
  ["#command-open", "#footer-command"].forEach((id) =>
    $(id).addEventListener("click", openPalette),
  );
  $("#help-open").addEventListener("click", showHelp);
  $("#export-data").addEventListener("click", exportData);
  $("#refresh-snapshot").addEventListener("click", refreshSnapshot);
  $("#watch-open").addEventListener("click", () => {
    $("#watchlist").classList.add("watch-open");
    $("#watchlist").setAttribute("role", "dialog");
    $("#watchlist").setAttribute("aria-modal", "true");
    $("#watch-search").focus();
  });
  function closeWatch() {
    const open = $("#watchlist").classList.contains("watch-open");
    $("#watchlist").classList.remove("watch-open");
    $("#watchlist").removeAttribute("role");
    $("#watchlist").removeAttribute("aria-modal");
    if (open) $("#watch-open").focus();
  }
  $("#watch-close").addEventListener("click", closeWatch);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (!$("dialog[open]")) openPalette();
      return;
    }
    if (e.key === "Escape") {
      document.body.classList.remove("expanded-chart");
      $("#chart-expand").setAttribute("aria-pressed", "false");
      closeWatch();
      drawChart();
    }
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName);
    if (e.key === "/" && !typing && !$("dialog[open]")) {
      e.preventDefault();
      if (window.innerWidth <= 700) $("#watch-open").click();
      $("#watch-search").focus();
    }
    if (e.key === "Tab" && $("#watchlist").classList.contains("watch-open")) {
      const nodes = $$("button,input", $("#watchlist")).filter(
        (n) => n.offsetParent !== null,
      );
      if (e.shiftKey && document.activeElement === nodes[0]) {
        e.preventDefault();
        nodes.at(-1).focus();
      } else if (!e.shiftKey && document.activeElement === nodes.at(-1)) {
        e.preventDefault();
        nodes[0].focus();
      }
    }
  });
  $$("dialog").forEach((d) =>
    d.addEventListener("click", (e) => {
      if (e.target === d) {
        const r = d.getBoundingClientRect();
        if (
          e.clientX < r.left ||
          e.clientX > r.right ||
          e.clientY < r.top ||
          e.clientY > r.bottom
        )
          d.close();
      }
    }),
  );
  renderMarket();
  renderWatch();
  renderAllocation();
  renderHeader();
  renderActivity();
  renderTable();
  updateTicket();
  renderClock();
  $("#limit-price").value = selected()?.price || "";
  consoleLine(
    `KAIBA TERMINAL\n${isDemo ? "Demo session · synthetic market data · no real funds" : "Stored snapshot · read-only local commands"}\nType help to get started.`,
  );
  setInterval(renderClock, 10000);
})();
