/* Local evidence only. No upstream requests, order controls or telemetry. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {snapshot: null, activity: 'all', etf: 'BTC', kind: 'all', search: '', pulseChain: 'all', chainSort: 'volume', tokenSort: 'rank', walletSort: 'pnl', seen: new Set(), initialized: false, loading: false};
  const num = value => typeof value === 'number' && Number.isFinite(value);
  const text = (tag, value, cls) => { const el = document.createElement(tag); el.textContent = value ?? '—'; if (cls) el.className = cls; return el; };
  const icon = name => { const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); const use = document.createElementNS(el.namespaceURI, 'use'); use.setAttribute('href', `#i-${name}`); el.append(use); el.setAttribute('aria-hidden', 'true'); return el; };
  function link(label, url) {
    if (typeof url !== 'string' || !url.trim()) return text('span', label);
    const el = text('a', label);
    try { const parsed = new URL(url, location.origin); if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) throw Error(); el.href = parsed.href; if (parsed.origin !== location.origin) { el.target = '_blank'; el.rel = 'noopener noreferrer'; } } catch { return text('span', label); }
    return el;
  }
  const money = (value, signed = false) => num(value) ? `${signed && value > 0 ? '+' : value < 0 ? '−' : ''}$${Math.abs(value).toLocaleString('en-US', {maximumFractionDigits: Math.abs(value) < 1 ? 4 : 0})}` : '—';
  const price = value => num(value) ? `$${value.toLocaleString('en-US', {maximumFractionDigits: value < 1 ? 5 : 2})}` : '—';
  const tone = value => num(value) ? value > 0 ? 'positive' : value < 0 ? 'negative' : '' : '';
  const stamp = value => { const date = new Date(value); return value && Number.isFinite(date.valueOf()) ? date : null; };
  const when = value => { const date = stamp(value); return date ? `${new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Jakarta', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(date)} WIB` : 'Time unknown'; };
  const day = value => { const date = stamp(value); return date ? new Intl.DateTimeFormat('en-GB', {timeZone: 'UTC', day: '2-digit', month: 'short'}).format(date) : 'Date unknown'; };
  const status = value => value === 'fresh' || value === 'available' ? 'Current' : value === 'missing' ? 'Unavailable' : value ? value[0].toUpperCase() + value.slice(1) : 'Unknown';
  const matches = row => !state.search || [row.title, row.symbol, row.asset, row.name, row.source, row.chain, row.chain_name, row.kind, row.ticker, row.address, row.short_address].filter(Boolean).join(' ').toLowerCase().includes(state.search);
  const clear = id => { const el = $(id); el.replaceChildren(); return el; };
  function tableRow(cells, className) { const tr = document.createElement('tr'); if (className) tr.className = className; cells.forEach(cell => { const td = document.createElement('td'); if (cell instanceof Node) td.append(cell); else td.textContent = cell ?? '—'; tr.append(td); }); return tr; }
  function badge(value) { return text('span', status(value), `status ${value || ''}`); }

  function renderSummary(s) {
    const m = s.kaiba?.metrics || {};
    $('metric-open').textContent = num(m.open_positions) ? String(m.open_positions) : '—';
    $('metric-open').title = `${m.live_positions ?? 'Unknown'} live · ${m.paper_positions ?? 'Unknown'} paper`;
    $('metric-open').previousElementSibling.querySelector('p').textContent = num(m.live_positions) && num(m.paper_positions) ? `Kaiba · ${m.live_positions} live / ${m.paper_positions} paper` : 'Kaiba ledger';
    const pnl = (m.realized_today_by_chain || []).filter(row => row.mode === 'live');
    $('metric-pnl').className = '';
    if (!m.day_complete) $('metric-pnl').textContent = '—';
    else if (!pnl.length) { $('metric-pnl').textContent = 'No live closes'; $('metric-pnl').className = 'small-value'; }
    else { $('metric-pnl').textContent = pnl.map(row => `${num(row.pnl_native) ? `${row.pnl_native > 0 ? '+' : ''}${row.pnl_native.toLocaleString('en-US', {maximumFractionDigits: 5})}` : '—'} ${row.symbol || row.chain}`).join(' · '); $('metric-pnl').className = 'small-value'; }
    $('pnl-label').textContent = 'Kaiba · live · native units · UTC';
    const b = s.intelligence?.bridge || {};
    const fresh = b.fresh_count || 0, total = (s.intelligence?.collectors || []).length;
    const current = total > 0 && fresh === total && s.kaiba?.status === 'available';
    $('metric-fresh').textContent = total && fresh === total ? 'Current' : fresh ? `${fresh}/${total} current` : 'Needs refresh';
    $('metric-fresh').className = current ? 'positive' : 'amber';
    $('health-dot').style.background = current ? 'var(--teal)' : 'var(--amber)';
    $('notice').classList.toggle('ok', current);
    $('notice').querySelector('span').textContent = current ? 'Your sources are current.' : 'Some sources need a refresh.';
    $('sidebar-status').textContent = current ? 'Saved observations are current.' : 'Some sources need a refresh.';
    $('updated-at').textContent = `Workspace read ${when(s.as_of_ms)}`;
    $('snapshot-age').textContent = `Refreshes every ${s.refresh_interval_s || 30}s`;
  }

  function renderMarkets(s) {
    const el = clear('market-list'), rows = s.intelligence?.markets || [];
    ['BTC', 'ETH', 'SOL'].forEach(symbol => {
      const row = rows.find(r => r.symbol === symbol || r.symbol === `${symbol}USDT` || r.name === symbol);
      const item = text('div', '', 'market-row'); item.append(text('span', symbol === 'BTC' ? '₿' : symbol === 'ETH' ? 'Ξ' : 'S', `coin ${symbol}`), text('b', symbol));
      const detail = text('span', row && num(row.price) ? price(row.price) : 'Awaiting fresh data', 'market-price');
      if (row) { detail.append(text('small', `${num(row.change_pct) ? `${row.change_pct > 0 ? '+' : ''}${row.change_pct.toFixed(2)}% · ` : ''}${status(row.status)}`)); detail.title = `Observed ${when(row.observed_at)}`; }
      item.append(detail); el.append(item);
    });
    const current = rows.some(row => row.status === 'fresh');
    $('market-context').querySelector('p').textContent = current ? 'Latest market observations. A single price is not a historical trend.' : 'Connect fresh market observations to see the trend.';
  }

  function renderMacro(s) {
    const el = clear('macro-list');
    const rows = (s.intelligence?.macro_events || []).filter(row => !['expiry', 'funding'].includes(row.kind) && matches(row) && (!num(row.event_ms) || row.event_ms >= Date.now())).slice(0, 3);
    if (!rows.length) ['CPI', 'FOMC', 'PCE'].forEach(title => rows.push({title, status: 'unavailable'}));
    rows.forEach(row => {
      const item = text('div', '', 'macro-row'), detail = document.createElement('div');
      detail.append(link(row.title, row.source_url), text('small', row.event_at ? `${row.source || 'Calendar'} · ${status(row.status)}` : 'Awaiting schedule refresh'));
      const t = text('time', row.event_at ? when(row.event_at) : '—'); if (row.event_at) t.dateTime = row.event_at;
      item.append(icon('calendar'), detail, t); el.append(item);
    });
  }

  function activities(s) {
    const smart = (s.kaiba?.wallet_alerts || []).map(row => ({...row, type: 'smart', asset: row.symbol || row.chain, direction: 'buy confluence', observed_at: row.observed_ms, status: row.observation_status === 'unknown' ? 'unknown' : row.stale ? 'stale' : 'fresh', source_url: row.url, note: row.detail}));
    const whales = (s.intelligence?.whales || []).map(row => ({...row, type: 'whale'}));
    return [...smart, ...whales].sort((a, b) => (stamp(b.observed_at)?.valueOf() || 0) - (stamp(a.observed_at)?.valueOf() || 0));
  }

  function renderActivity(s) {
    const el = clear('activity-body');
    const rows = activities(s).filter(row => (state.activity === 'all' || state.activity === row.type) && matches(row)).slice(0, 12);
    $('alert-count').textContent = rows.length ? `${rows.length} observations` : '';
    $('activity-empty').hidden = rows.length > 0;
    rows.forEach(row => {
      const info = text('div', ''); info.append(link(row.title || 'Wallet observation', row.source_url), text('small', row.source || row.note || row.kind || '')); info.title = row.note || row.detail || '';
      const ts = text('div', when(row.observed_at)); ts.append(badge(row.status));
      const value = text('span', num(row.amount_usd) ? money(row.amount_usd) : num(row.amount) ? `${row.amount.toLocaleString('en-US', {maximumFractionDigits: 3})} ${row.unit || ''}` : '—', 'number');
      if (row.kind === 'cohort_position') value.title = 'Signed net position exposure, not wallet inflow or outflow';
      const direction = row.direction === 'unknown' ? 'Unverified' : row.direction || 'Unverified';
      el.append(tableRow([info, row.asset || '—', direction, value, ts]));
    });
  }

  function renderInstitutions(s) {
    const el = clear('institution-body');
    const rows = (s.intelligence?.institutional_flows || []).filter(row => row.asset === state.etf);
    const fundRows = rows.filter(row => row.ticker !== 'TOTAL').slice(0, 3);
    const blackrock = state.etf === 'BTC' ? 'IBIT' : 'ETHA';
    if (!fundRows.some(row => row.ticker === blackrock)) fundRows.unshift({ticker: blackrock, name: `BlackRock ${blackrock}`, status: 'unavailable'});
    const total = rows.find(row => row.ticker === 'TOTAL');
    [...fundRows, total || {ticker: 'TOTAL', name: 'Total', status: 'unavailable'}].forEach(row => {
      const name = text('div', row.ticker === 'TOTAL' ? 'Total' : row.name || row.ticker); if (row.status !== 'fresh' || row.partial) name.append(badge(row.partial && row.status === 'fresh' ? 'partial' : row.status));
      const value = text('span', money(row.flow_usd, true), `number ${row.status === 'fresh' ? tone(row.flow_usd) : 'historical-value'}`);
      const date = row.report_date || '—';
      const tr = tableRow([name, value, link(date, row.source_url)], row.ticker === 'TOTAL' ? 'institution-total' : '');
      tr.title = row.note || 'Reported ETF net subscriptions/redemptions'; el.append(tr);
    });
  }

  function normalizedKind(kind) { return kind === 'nft_mint' ? 'nft' : ['memecoin', 'new_launch', 'listing'].includes(kind) ? 'token' : kind; }
  function renderDiscover(s) {
    const el = clear('discover-list');
    const rows = (s.kaiba?.opportunities || []).filter(row => (state.kind === 'all' || normalizedKind(row.kind) === state.kind) && matches(row)).slice(0, 10);
    $('discover-empty').hidden = rows.length > 0;
    rows.forEach(row => { const item = text('div', '', 'opportunity'), copy = document.createElement('div'); copy.append(link(row.title, row.url), text('small', `${row.chain || 'Chain unverified'} · ${row.source || 'Research lead'} · ${when(row.observed_ms)}${row.stale ? ' · Stale' : ''}`)); item.append(copy, text('span', row.kind?.replaceAll('_', ' ') || 'Research', 'tag')); el.append(item); });
    if (!rows.length && (state.kind !== 'all' || state.search)) { $('discover-empty').querySelector('strong').textContent = 'No matching discoveries'; $('discover-empty').querySelector('p').textContent = 'Try another category or clear your search. New leads appear on the Alpha page.'; }
    else { $('discover-empty').querySelector('strong').textContent = 'No discoveries yet'; const p = $('discover-empty').querySelector('p'); p.replaceChildren(text('span', 'Explore new tokens, narratives and early opportunities on the '), link('Alpha page', '/alpha'), text('span', '.')); }
  }

  function renderBrief(s) {
    const el = clear('brief-content'), b = s.daily_brief || {};
    const today = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Jakarta', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
    const old = b.brief_date !== today;
    if (b.schema_version === 1 && b.compiled_at_ms) {
      el.append(text('strong', `${b.brief_date} · ${old ? 'Previous brief' : status(b.status)}`), text('p', b.summary || 'No source summary available.'), text('p', `Compiled ${when(b.compiled_at_ms)}. Source freshness is assessed separately.`));
      const cats = Object.entries(b.categories || {}).map(([key, val]) => `${key}: ${status(val?.status || val?.coverage)}`).join(' · '); if (cats) el.append(text('p', cats));
    } else el.append(text('strong', 'Awaiting the first daily brief'), text('p', 'The daily compiler summarizes saved macro evidence at 07:00 WIB. Check source health for collection gaps.'));
    const readings = clear('macro-readings');
    ['DFF', 'CPIAUCSL', 'PCEPILFE'].forEach(id => {
      const row = (s.intelligence?.macro_series || []).find(series => series.id === id);
      const item = text('div', '', 'macro-reading');
      item.append(text('span', row?.name || ({DFF:'Fed funds rate',CPIAUCSL:'CPI inflation',PCEPILFE:'Core PCE inflation'})[id]), text('b', row && num(row.value) ? `${row.value.toLocaleString('en-US', {maximumFractionDigits: 2})} ${row.units || ''}` : '—'));
      item.append(text('small', row ? `Observation ${row.report_date || 'date unknown'} · ${status(row.status)}` : 'Awaiting source observation')); readings.append(item);
    });
    const news = clear('news-list');
    (Array.isArray(b.news) ? b.news : []).filter(matches).slice(0, 4).forEach(row => { const item = text('div', '', 'news-row'); item.append(link(row.title, row.url), text('small', `${row.source || 'News'} · ${when(row.published_at)} · ${status(row.status)}`)); news.append(item); });
    if (!news.children.length) news.append(text('p', 'No current cached headlines. This does not establish that no news occurred.', 'panel-note'));
  }

  function renderPerps(s) {
    const el = clear('perps-content'), p = s.intelligence?.perps || {};
    el.append(text('strong', p.status === 'fresh' ? `Engine ${p.mode || 'mode unknown'}` : 'Engine status unavailable'), text('p', p.reason || 'Connect an existing Aegis engine snapshot to see its current status.'));
    if (num(p.positions_count)) el.append(text('p', `${p.positions_count} open positions · ${when(p.observed_at)}`));
    if (s.aegis_terminal_url) { const a = link('Open Aegis terminal', s.aegis_terminal_url); a.append(icon('arrow')); el.append(a); }
    else el.append(text('p', 'Aegis terminal link is not configured. The intelligence panels above already combine both systems.'));
  }

  function renderSources(s) {
    const el = clear('source-list');
    const ledger = (s.kaiba?.health || []).map(row => ({...row, title: `Kaiba · ${row.key.replaceAll('_', ' ').replaceAll(':', ' / ')}`, fetched_at: s.kaiba?.captured_at_ms}));
    const pulse = (s.pulse?.source_health || []).map(row => ({...row, title: `Market pulse · ${row.key}`}));
    [...(s.intelligence?.collectors || []), ...pulse, ...ledger].forEach(row => {
      const item = text('div', '', 'source'); item.append(text('span', status(row.status), `source-state ${row.status === 'available' ? 'fresh' : row.status}`), text('b', row.title || row.key), text('small', row.fetched_at ? `Collected ${when(row.fetched_at)}` : 'No collection timestamp'));
      if (row.error || row.reason) item.append(text('small', row.reason || 'Last collection reported an error')); el.append(item);
    });
  }

  const compactUsd = value => num(value) ? `${value < 0 ? '−' : ''}$${new Intl.NumberFormat('en-US', {notation: 'compact', maximumFractionDigits: 1}).format(Math.abs(value))}` : '—';
  const descending = (a, b) => num(a) && num(b) ? b - a : num(a) ? -1 : num(b) ? 1 : 0;
  const pulseChainMatch = row => state.pulseChain === 'all' || row.chain === state.pulseChain;

  function selectPulseChain(chain) {
    state.pulseChain = chain;
    $('pulse-chain').value = chain;
    $('token-chain').value = chain;
    render();
  }

  function renderPulse(s) {
    const p = s.pulse || {}, chains = p.chains || [];
    const chainNames = {sol: 'Solana', eth: 'Ethereum', base: 'Base', bsc: 'BNB Chain', arbitrum: 'Arbitrum', robinhood: 'Robinhood'};
    const expectedChains = ['sol', 'eth', 'base', 'bsc', 'arbitrum'];
    const chainRows = expectedChains.map(chain => chains.find(row => row.chain === chain) || {chain, name: chainNames[chain], status: 'unavailable'});
    chainRows.push(...chains.filter(row => !expectedChains.includes(row.chain)));
    chainRows.sort((a, b) => descending(state.chainSort === 'growth' ? a.change_24h_pct : a.volume_24h_usd, state.chainSort === 'growth' ? b.change_24h_pct : b.volume_24h_usd));
    const largest = Math.max(0, ...chainRows.map(row => num(row.volume_24h_usd) ? row.volume_24h_usd : 0));
    const heatmap = clear('chain-heatmap');
    chainRows.forEach(row => {
      const tile = document.createElement('button'); tile.type = 'button';
      tile.className = `chain-tile ${state.pulseChain === row.chain ? 'selected' : ''}`;
      tile.setAttribute('aria-pressed', String(state.pulseChain === row.chain));
      tile.setAttribute('aria-label', `Filter ${row.name}`);
      tile.title = row.source || 'Awaiting source observation';
      const title = text('span', '', 'chain-name');
      title.append(text('span', row.chain === 'eth' ? 'Ξ' : row.name[0], `chain-mark ${row.chain}`), text('strong', row.name), icon('arrow'));
      const volume = text('span', '', 'chain-stat'); volume.append(text('span', 'Volume (24h)'), text('b', compactUsd(row.volume_24h_usd)));
      const growth = text('span', '', 'chain-stat'); growth.append(text('span', 'Change (24h)'), text('b', num(row.change_24h_pct) ? `${row.change_24h_pct > 0 ? '+' : ''}${row.change_24h_pct.toFixed(1)}%` : '—', row.status === 'fresh' ? tone(row.change_24h_pct) : ''));
      const source = text('span', '', 'chain-source'); source.append(text('span', 'Source'), text('span', row.observed_ms ? `${status(row.status)} · ${when(row.observed_ms)}` : 'Awaiting snapshot'));
      const track = text('span', '', 'heat-volume-track'), bar = document.createElement('span');
      bar.style.width = `${largest && num(row.volume_24h_usd) ? row.volume_24h_usd / largest * 100 : 0}%`;
      track.append(bar); tile.append(title, volume, growth, source, track);
      tile.addEventListener('click', () => selectPulseChain(state.pulseChain === row.chain ? 'all' : row.chain));
      heatmap.append(tile);
    });
    const known = chains.filter(row => num(row.volume_24h_usd)).length;
    $('chain-coverage').textContent = `DefiLlama reported DEX spot volume · ${known} of ${chainRows.length} covered chains measured. Bars compare volume within this set.`;
    $('pulse-status').textContent = p.reason || `${(p.tokens || []).length} token observations · ${(p.wallets || []).length} reviewed wallets · Snapshot ${status(p.status).toLowerCase()}`;
    $('pulse-clock').textContent = p.observed_ms ? `Snapshot ${when(p.observed_ms)}` : 'Source times and coverage stay visible.';

    let tokens = (p.tokens || []).filter(row => pulseChainMatch(row) && matches(row));
    if (state.tokenSort === 'volume1h') tokens = tokens.filter(row => num(row.volume_1h_usd)).sort((a, b) => descending(a.volume_1h_usd, b.volume_1h_usd));
    else if (state.tokenSort === 'volume24h') tokens = tokens.filter(row => num(row.volume_24h_usd)).sort((a, b) => descending(a.volume_24h_usd, b.volume_24h_usd));
    else if (state.tokenSort === 'reported') tokens = tokens.filter(row => row.volume_window === 'unknown' && num(row.reported_volume_usd)).sort((a, b) => descending(a.reported_volume_usd, b.reported_volume_usd));
    else tokens.sort((a, b) => num(a.source_rank) && num(b.source_rank) ? a.source_rank - b.source_rank || a.chain.localeCompare(b.chain) : num(a.source_rank) ? -1 : num(b.source_rank) ? 1 : 0);
    const tokenBody = clear('token-rank-body');
    tokens.slice(0, 50).forEach(row => {
      const name = text('div', ''); name.append(link(row.symbol, row.url), text('small', row.name || row.address.slice(0, 8)));
      let amount = row.reported_volume_usd, period = row.volume_window === 'unknown' ? 'Window unspecified' : row.volume_window;
      if (state.tokenSort === 'volume1h' || (state.tokenSort === 'rank' && num(row.volume_1h_usd))) { amount = row.volume_1h_usd; period = '1h'; }
      else if (state.tokenSort === 'volume24h' || (state.tokenSort === 'rank' && num(row.volume_24h_usd))) { amount = row.volume_24h_usd; period = '24h'; }
      const volume = text('div', compactUsd(amount), 'number'); volume.append(text('small', period));
      const observed = text('div', when(row.observed_ms)); observed.append(badge(row.status));
      const rank = text('div', row.source_rank); rank.append(text('small', `List ${row.trend_window}`));
      tokenBody.append(tableRow([rank, name, row.chain_name, text('span', compactUsd(row.market_cap_usd), 'number'), text('span', compactUsd(row.liquidity_usd), 'number'), volume, observed]));
    });
    $('token-rank-count').textContent = `${Math.min(tokens.length, 50)} of ${tokens.length} visible`;
    $('token-rank-basis').textContent = state.tokenSort === 'reported' ? 'Source-reported volume · measurement window unspecified. This is separate from the 1h and 24h rankings.' : 'Ranks belong to each source list. Volume is compared only within the selected period.';
    $('token-rank-empty').hidden = tokens.length > 0;
    $('token-rank-empty').querySelector('strong').textContent = state.tokenSort === 'rank' ? 'Awaiting token rankings' : 'No comparable volume records';
    $('token-rank-empty').querySelector('p').textContent = state.tokenSort === 'rank' ? 'Uses the existing market feed. Try another chain or clear your search.' : 'This view requires an explicit matching volume period. Try Source rank.';

    let wallets = (p.wallets || []).filter(row => pulseChainMatch(row) && matches(row));
    if (state.walletSort === 'today') wallets = wallets.filter(row => row.found_today);
    wallets.sort((a, b) => state.walletSort === 'recent' ? descending(a.scored_at_ms, b.scored_at_ms) : descending(a.realized_pnl_usd, b.realized_pnl_usd) || descending(a.scored_at_ms, b.scored_at_ms));
    const walletBody = clear('wallet-rank-body');
    wallets.slice(0, 30).forEach(row => {
      const name = text('div', ''); name.append(link(row.name, row.url), text('small', `${row.chain_name} · ${row.short_address}`), text('small', `Found ${when(row.first_seen_ms)}`));
      const pnl = text('div', compactUsd(row.realized_pnl_usd), `number ${row.status === 'fresh' ? tone(row.realized_pnl_usd) : ''}`); pnl.append(text('small', row.pnl_window === 'unknown' ? 'Window unknown' : row.pnl_window));
      const evidence = text('div', row.grade || 'Unscored'); evidence.append(badge(row.status));
      if (row.flags?.length) evidence.append(text('small', 'Flagged', 'rank-flag'));
      evidence.title = [row.basis, `Assessed ${when(row.scored_at_ms)}`, ...(row.flags || [])].join(' · ');
      walletBody.append(tableRow([name, pnl, num(row.win_rate_pct) ? `${row.win_rate_pct.toFixed(1)}%` : '—', row.closed_trades, evidence]));
    });
    $('wallet-rank-count').textContent = `${Math.min(wallets.length, 30)} of ${wallets.length} visible`;
    $('wallet-rank-empty').hidden = wallets.length > 0;
    $('wallet-rank-empty').querySelector('strong').textContent = state.walletSort === 'today' ? 'No new wallets in this snapshot today' : 'Awaiting wallet snapshot';
    const scope = p.coverage?.wallet_scope || 'Among reviewed wallets';
    $('wallet-rank-scope').textContent = state.walletSort === 'today' ? `First seen today in Asia/Jakarta · exported candidates from ${scope}. PnL retains its own period.` : `${scope} · source history; measurement windows may vary.`;
  }

  function notify(s) {
    const current = activities(s).filter(row => { const date = stamp(row.observed_at); const age = date ? Date.now() - date.valueOf() : NaN; return row.status === 'fresh' && age >= 0 && age <= 3600000; });
    const unseen = current.filter(row => !state.seen.has(row.id));
    current.forEach(row => state.seen.add(row.id));
    if (state.seen.size > 1000) state.seen = new Set(current.map(row => row.id));
    if (state.initialized && $('sound-alerts').checked && unseen.length) { $('toast').textContent = `${unseen.length} new wallet or whale observations. Review source time and attribution in Wallet flows.`; $('toast').hidden = false; setTimeout(() => { $('toast').hidden = true; }, 9000); }
    state.initialized = true;
  }
  function render() { if (!state.snapshot) return; const s = state.snapshot; renderSummary(s); renderPulse(s); renderMarkets(s); renderMacro(s); renderActivity(s); renderInstitutions(s); renderDiscover(s); renderBrief(s); renderPerps(s); renderSources(s); }
  async function refresh() {
    if (state.loading || document.hidden) return;
    state.loading = true; $('refresh').disabled = true; $('refresh').classList.add('loading');
    try {
      const response = await fetch('/api/ultimate', {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(35000)});
      if (response.status === 401) { location.assign('/login'); return; }
      if (!response.ok) throw Error(`Snapshot unavailable (${response.status})`);
      const value = await response.json();
      if (!value || typeof value !== 'object' || !value.kaiba || !value.intelligence) throw Error('Invalid snapshot');
      state.snapshot = value; notify(value); render();
    } catch (error) {
      $('notice').classList.remove('ok'); $('notice').querySelector('span').textContent = 'Workspace refresh failed. Saved observations may be out of date.';
      $('metric-fresh').textContent = 'Refresh failed'; $('metric-fresh').className = 'amber'; $('sidebar-status').textContent = 'Workspace connection unavailable.';
      document.querySelectorAll('.source-state.fresh, .status.fresh').forEach(el => { el.classList.remove('fresh'); el.textContent = 'Refresh failed'; });
      $('health-dot').style.background = 'var(--amber)';
      if (!state.snapshot) { $('market-list').replaceChildren(text('p', 'Market observations unavailable.', 'panel-note')); $('brief-content').textContent = 'Daily brief unavailable while the workspace cannot be reached.'; }
    } finally { state.loading = false; $('refresh').disabled = false; $('refresh').classList.remove('loading'); }
  }
  document.querySelectorAll('[data-activity], [data-etf], [data-kind]').forEach(button => button.addEventListener('click', () => {
    const key = button.hasAttribute('data-activity') ? 'activity' : button.hasAttribute('data-etf') ? 'etf' : 'kind';
    state[key] = button.dataset[key];
    button.parentElement.querySelectorAll('button').forEach(el => { const selected = el === button; el.classList.toggle('selected', selected); el.setAttribute('aria-pressed', String(selected)); }); render();
  }));
  document.querySelectorAll('[data-chain-sort], [data-token-sort], [data-wallet-sort]').forEach(button => button.addEventListener('click', () => {
    const key = button.hasAttribute('data-chain-sort') ? 'chainSort' : button.hasAttribute('data-token-sort') ? 'tokenSort' : 'walletSort';
    state[key] = button.dataset[key];
    button.parentElement.querySelectorAll('button').forEach(el => { const selected = el === button; el.classList.toggle('selected', selected); el.setAttribute('aria-pressed', String(selected)); });
    render();
  }));
  ['pulse-chain', 'token-chain'].forEach(id => $(id).addEventListener('change', event => selectPulseChain(event.target.value)));
  $('search').addEventListener('input', event => { state.search = event.target.value.trim().toLowerCase(); render(); });
  $('refresh').addEventListener('click', refresh);
  $('sound-alerts').addEventListener('change', event => { if (event.target.checked) { $('toast').textContent = 'In-app alerts are on for new observations while this workspace is open.'; $('toast').hidden = false; setTimeout(() => { $('toast').hidden = true; }, 7000); } });
  function nav() { const current = location.hash.slice(1) || 'pulse'; document.querySelectorAll('[data-nav]').forEach(el => { el.classList.toggle('active', el.dataset.nav === current); if (el.dataset.nav === current) el.setAttribute('aria-current', 'location'); else el.removeAttribute('aria-current'); }); }
  window.addEventListener('hashchange', nav); document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  nav(); refresh(); setInterval(refresh, 30000);
})();
