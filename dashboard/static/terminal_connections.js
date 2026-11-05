/* Codex-owned optional account visibility and explicit public-chart drawer.
   No credentials, account identities, or token addresses are stored locally. */
(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const dialog = byId("connections-dialog");
  if (!dialog) return;

  let snapshot;
  try {
    snapshot = JSON.parse(byId("terminal-data")?.textContent || "{}");
  } catch (_) {
    snapshot = {};
  }
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot))
    snapshot = {};
  // An absent or malformed mode must never become an account request.
  const isDemo = snapshot.demo !== false;
  const tabs = ["account", "charts"];
  const chainNames = {
    sol: "Solana",
    eth: "Ethereum",
    bsc: "BNB Chain",
    base: "Base",
    robinhood: "Robinhood",
  };
  const embedChains = new Set(["sol", "eth", "bsc"]);
  const intervals = new Set(["1", "5", "15", "60", "240", "1D"]);
  const statuses = {
    connected: "Account read",
    stale: "Cached account read",
    unavailable: "Unavailable",
    rate_limited: "Rate limited",
    invalid_response: "Invalid response",
    not_checked: "Not checked",
  };
  const messages = {
    connected:
      "GMGN returned account visibility. This does not verify wallet binding or grant trading permission.",
    stale:
      "Showing an earlier account observation. Its original timestamp is preserved; balances and permissions are not inferred.",
    unavailable:
      "The account check is unavailable. Open GMGN or use the agent's existing connection setup.",
    rate_limited:
      "The provider limited this account check. No automatic retry is scheduled.",
    invalid_response:
      "The provider response could not be verified. No account identities or balances are inferred.",
    not_checked:
      "No account check has been made here. Check account to request a read-only account observation.",
  };
  let currentTab = "account";
  let opener = null;
  let accountBusy = false;
  let fetchedThisOpen = false;
  let accountGeneration = 0;
  let accountFreshnessTimer = null;

  function clearAccountTimer() {
    if (accountFreshnessTimer !== null) clearTimeout(accountFreshnessTimer);
    accountFreshnessTimer = null;
  }

  function put(id, value) {
    const element = byId(id);
    if (element) element.textContent = value;
  }
  function error(id, message) {
    const element = byId(id);
    if (!element) return;
    element.textContent = message;
    element.hidden = !message;
  }
  function timestamp(value, missing = "Unavailable") {
    if (!Number.isSafeInteger(value) || value <= 0) return missing;
    const date = new Date(value);
    return Number.isFinite(date.getTime())
      ? date
          .toISOString()
          .replace("T", " ")
          .replace(".000Z", " UTC")
          .replace("Z", " UTC")
      : missing;
  }
  function validAddress(chain, address) {
    if (typeof address !== "string") return false;
    if (chain !== "sol") return /^0x[0-9a-fA-F]{40}$/.test(address);
    // Decode the public key: character count alone does not prove 32 bytes.
    if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address)) return false;
    const alphabet =
      "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
    let number = 0n;
    for (const character of address) {
      number = number * 58n + BigInt(alphabet.indexOf(character));
    }
    let significantBytes = 0;
    for (; number > 0n; number >>= 8n) significantBytes += 1;
    const leadingZeroBytes = address.match(/^1*/)[0].length;
    return leadingZeroBytes + significantBytes === 32;
  }
  function validateWallet(value) {
    if (!value || typeof value !== "object" || Array.isArray(value))
      return null;
    if (
      !Object.hasOwn(chainNames, value.chain) ||
      !validAddress(value.chain, value.address)
    )
      return null;
    const nativeSymbols = {
      sol: "SOL",
      eth: "ETH",
      bsc: "BNB",
      base: "ETH",
      robinhood: "ETH",
    };
    const balance =
      typeof value.native_balance === "string" &&
      /^(?:0|[1-9][0-9]{0,39})(?:\.[0-9]{1,30})?$/.test(value.native_balance)
        ? value.native_balance
        : null;
    return {
      chain: value.chain,
      address: value.address,
      symbol: nativeSymbols[value.chain],
      balance: value.balance_status === "available" ? balance : null,
    };
  }
  function setAccountBusy(busy) {
    accountBusy = busy;
    const button = byId("account-check");
    if (button) button.disabled = busy;
    byId("connection-account-panel")?.setAttribute("aria-busy", String(busy));
  }
  function renderWallets(wallets) {
    const host = byId("account-wallets");
    if (!host) return;
    host.replaceChildren();
    for (const wallet of wallets) {
      const row = document.createElement("div");
      row.className = "account-wallet";
      const identity = document.createElement("div");
      const heading = document.createElement("strong");
      heading.textContent = chainNames[wallet.chain];
      const address = document.createElement("code");
      address.textContent = wallet.address;
      identity.append(heading, address);
      const balance = document.createElement("span");
      balance.className = "account-wallet-balance";
      balance.textContent =
        wallet.balance === null
          ? "Balance unavailable"
          : `${wallet.balance} ${wallet.symbol} · provider reported`;
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "subtle-button account-copy";
      copy.textContent = "Copy address";
      copy.setAttribute(
        "aria-label",
        `Copy ${chainNames[wallet.chain]} wallet address`,
      );
      copy.addEventListener("click", async () => {
        try {
          if (!navigator.clipboard?.writeText) throw new Error("unavailable");
          await navigator.clipboard.writeText(wallet.address);
          copy.textContent = "Copied";
          error("account-error", "");
        } catch (_) {
          error(
            "account-error",
            "Copy is unavailable in this browser. Select the displayed public address to copy it.",
          );
        }
      });
      row.append(identity, balance, copy);
      host.append(row);
    }
  }
  function renderAccount(payload) {
    if (
      !payload ||
      payload.provider !== "gmgn" ||
      !Object.hasOwn(statuses, payload.status) ||
      !Array.isArray(payload.wallets)
    ) {
      throw new Error("invalid account projection");
    }
    clearAccountTimer();
    // Only a verified projection may supply identities; failed checks never do.
    const wallets = ["connected", "stale"].includes(payload.status)
      ? payload.wallets.slice(0, 100).map(validateWallet).filter(Boolean)
      : [];
    put("account-status", statuses[payload.status]);
    byId("account-status")?.setAttribute("data-status", payload.status);
    put("account-description", messages[payload.status]);
    const omitted =
      Number.isSafeInteger(payload.omitted_count) && payload.omitted_count > 0
        ? payload.omitted_count
        : 0;
    put(
      "account-wallet-count",
      `${wallets.length} shown${omitted ? ` · ${omitted} omitted` : ""}`,
    );
    put(
      "account-checked",
      timestamp(
        payload.checked_at_ms,
        payload.status === "not_checked" ? "Never" : "Unavailable",
      ),
    );
    put("account-refresh", timestamp(payload.observed_at_ms));
    renderWallets(wallets);
    // Age the same receipt locally. This timer never refreshes an observation,
    // changes its original clocks, or makes a provider / cached GET request.
    if (payload.status === "connected") {
      const age = payload.age_ms;
      const budget = payload.freshness_budget_ms;
      if (
        typeof age === "number" &&
        Number.isFinite(age) &&
        age >= 0 &&
        typeof budget === "number" &&
        Number.isFinite(budget) &&
        budget >= 0 &&
        budget <= 86_400_000
      ) {
        const markStale = () => {
          accountFreshnessTimer = null;
          put("account-status", statuses.stale);
          byId("account-status")?.setAttribute("data-status", "stale");
          put("account-description", messages.stale);
        };
        if (payload.fresh === false || age >= budget) markStale();
        else if (dialog.open)
          accountFreshnessTimer = setTimeout(
            markStale,
            Math.ceil(budget - age),
          );
      }
    }
  }
  async function readAccount(check = false) {
    if (accountBusy) return;
    error("account-error", "");
    if (isDemo) {
      if (check) {
        put("account-status", "Preview only");
        put(
          "account-description",
          "Account checks are available after the authenticated agent dashboard installs its read-only GMGN connection. This preview makes no account request.",
        );
      }
      return;
    }
    const csrf =
      document.querySelector('meta[name="csrf-token"]')?.content ||
      snapshot.csrf_token;
    if (check && (typeof csrf !== "string" || !csrf.trim())) {
      error(
        "account-error",
        "Account check needs an authenticated dashboard session. Reload the operator dashboard and try again.",
      );
      return;
    }
    const generation = ++accountGeneration;
    setAccountBusy(true);
    try {
      const response = await fetch(
        check ? "/api/gmgn/account/check" : "/api/gmgn/account",
        {
          method: check ? "POST" : "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers: check ? { "X-Kaiba-CSRF": csrf } : {},
          signal: AbortSignal.timeout(20_000),
        },
      );
      const payload = await response.json();
      if (generation !== accountGeneration) return;
      if ([401, 403].includes(response.status)) {
        error(
          "account-error",
          "The authenticated dashboard session could not authorize this account read. Reload the operator dashboard.",
        );
        return;
      }
      if (!response.ok && ![409, 429, 503].includes(response.status))
        throw new Error("unavailable");
      renderAccount(payload);
      if ([409, 429].includes(response.status)) {
        error(
          "account-error",
          response.status === 409
            ? "An account check is already running. No duplicate check was sent to the provider."
            : "Account checks are cooling down. Wait before requesting another; no automatic retry is scheduled.",
        );
      }
    } catch (_) {
      if (generation === accountGeneration) {
        error(
          "account-error",
          check
            ? "The account check outcome is unavailable. The server may still be checking; no automatic retry is scheduled."
            : "The cached account view is unavailable. No provider check was requested.",
        );
      }
    } finally {
      if (generation === accountGeneration) setAccountBusy(false);
    }
  }
  function switchTab(tab, focus = false) {
    if (!tabs.includes(tab)) return;
    currentTab = tab;
    for (const name of tabs) {
      const button = byId(`connection-tab-${name}`);
      const selected = name === tab;
      if (button) {
        button.setAttribute("aria-selected", String(selected));
        button.tabIndex = selected ? 0 : -1;
      }
      const panel = byId(`connection-${name}-panel`);
      if (panel) panel.hidden = !selected;
    }
    if (focus) byId(`connection-tab-${tab}`)?.focus();
    if (tab !== "charts") unloadChart();
    if (tab === "account" && dialog.open && !fetchedThisOpen) {
      fetchedThisOpen = true;
      void readAccount();
    }
  }
  function open(tab = "account", trigger = document.activeElement) {
    if (!tabs.includes(tab)) tab = "account";
    if (!dialog.open) {
      opener = trigger instanceof HTMLElement ? trigger : null;
      fetchedThisOpen = false;
      dialog.showModal();
    }
    switchTab(tab);
    byId(`connection-tab-${tab}`)?.focus();
  }
  function unloadChart() {
    const frame = byId("gmgn-chart-frame");
    if (frame) {
      frame.src = "about:blank";
      frame.hidden = true;
    }
    const link = byId("gmgn-chart-link");
    if (link) {
      link.removeAttribute("href");
      link.hidden = true;
    }
    const clear = byId("gmgn-chart-clear");
    if (clear) clear.hidden = true;
    const empty = byId("gmgn-chart-empty");
    if (empty) empty.hidden = false;
    put(
      "gmgn-chart-caption",
      "A public chart loads only after you submit a valid token address. It does not connect your GMGN account.",
    );
  }
  function submitChart(event) {
    event.preventDefault();
    const chain = byId("gmgn-chain")?.value;
    const interval = byId("gmgn-interval")?.value;
    const address = byId("gmgn-address")?.value.trim() || "";
    if (
      !embedChains.has(chain) ||
      !intervals.has(interval) ||
      !validAddress(chain, address)
    ) {
      error(
        "gmgn-chart-error",
        chain === "sol"
          ? "Paste a valid Solana token address decoding to 32 bytes. The current chart has not changed."
          : "Paste an EVM token address: 0x followed by exactly 40 hexadecimal characters. The current chart has not changed.",
      );
      return;
    }
    const frame = byId("gmgn-chart-frame");
    if (!frame) return;
    const url = new URL(
      `https://www.gmgn.cc/kline/${chain}/${encodeURIComponent(address)}`,
    );
    url.searchParams.set("theme", "dark");
    url.searchParams.set("interval", interval);
    frame.referrerPolicy = "no-referrer";
    frame.src = url.href;
    frame.hidden = false;
    const link = byId("gmgn-chart-link");
    if (link) {
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.hidden = false;
    }
    const clear = byId("gmgn-chart-clear");
    if (clear) clear.hidden = false;
    const empty = byId("gmgn-chart-empty");
    if (empty) empty.hidden = true;
    put(
      "gmgn-chart-caption",
      "GMGN chart requested. If it stays blank, open in a new tab.",
    );
    error("gmgn-chart-error", "");
  }

  byId("connections-open")?.addEventListener("click", (event) =>
    open("account", event.currentTarget),
  );
  byId("gmgn-charts-open")?.addEventListener("click", (event) =>
    open("charts", event.currentTarget),
  );
  for (const tab of tabs) {
    const button = byId(`connection-tab-${tab}`);
    button?.addEventListener("click", () => switchTab(tab));
    button?.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
        return;
      event.preventDefault();
      const next =
        event.key === "Home"
          ? "account"
          : event.key === "End"
            ? "charts"
            : tabs[(tabs.indexOf(currentTab) + 1) % tabs.length];
      switchTab(next, true);
    });
  }
  byId("account-check")?.addEventListener(
    "click",
    () => void readAccount(true),
  );
  byId("gmgn-chart-form")?.addEventListener("submit", submitChart);
  byId("gmgn-chart-clear")?.addEventListener("click", () => {
    unloadChart();
    byId("gmgn-chart-form")?.reset();
    error("gmgn-chart-error", "");
    byId("gmgn-address")?.focus();
  });
  dialog.addEventListener("close", () => {
    clearAccountTimer();
    unloadChart();
    // Native dialog restores focus; this also supports programmatic opens.
    if (opener?.isConnected) opener.focus();
  });
  put("account-status", "Not checked");
  put(
    "account-description",
    isDemo
      ? "This preview does not access your GMGN account."
      : messages.not_checked,
  );
  put("account-wallet-count", "Unavailable");
  put("account-checked", "Never");
  put("account-refresh", "Unavailable");
  const check = byId("account-check");
  if (check && isDemo) check.textContent = "Preview account check";
  renderWallets([]);
  unloadChart();
  switchTab("account");
  window.KaibaConnections = Object.freeze({ open });
})();
