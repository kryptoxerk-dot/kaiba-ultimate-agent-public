/* Kaiba terminal (2026-10-20 redesign). Vanilla JS, no dependencies, no build.
   - [data-refresh] regions re-fetch their HTML every [data-every] s while the tab is visible;
   - [data-load] opens a token in #token-panel (the terminal) or navigates to [data-href];
   - [data-tabs] switch [data-pane]s in the same panel, and survive a refresh ([data-keep-tab]);
   - canvas[data-series] draws a line with crosshair, last-value tag, entry marks and ranges;
   - [data-copy] copies text, [data-copy-url] copies a fetched file (GMGN import lists);
   - [data-buy], [data-exit], [data-lane-mode], [data-post] send authenticated POSTs;
   - the agent chat (dock and /agent) streams turns from /events/chat. */
(function () {
  "use strict";

  var meta = document.querySelector('meta[name="csrf-token"]');
  var CSRF = meta ? meta.getAttribute("content") : "";
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // ------------------------------------------------------------------ toast + POST

  var toastTimer = null;
  function toast(title, body, ok) {
    var el = $("[data-toast]");
    if (!el) { return; }
    el.className = "toast " + (ok ? "ok" : "bad");
    el.textContent = "";
    var b = document.createElement("b"); b.textContent = title; el.appendChild(b);
    if (body) { var s = document.createElement("span"); s.textContent = body; el.appendChild(s); }
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, ok ? 4500 : 9000);
  }

  function post(url, fields) {
    var form = new FormData();
    Object.keys(fields || {}).forEach(function (k) { form.append(k, fields[k]); });
    return fetch(url, {
      method: "POST", body: form, credentials: "same-origin",
      headers: { "x-kaiba-csrf": CSRF, "accept": "application/json" }
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { throw new Error((j && (j.detail || j.error)) || ("HTTP " + r.status)); }
        return j;
      });
    });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) { return navigator.clipboard.writeText(text); }
    return new Promise(function (ok, bad) {
      var box = document.createElement("textarea"); box.value = text; document.body.appendChild(box); box.select();
      try { document.execCommand("copy"); ok(); } catch (e) { bad(e); }
      document.body.removeChild(box);
    });
  }

  // ------------------------------------------------------------------ numbers

  var SUB = "₀₁₂₃₄₅₆₇₈₉";
  function fmtPx(v) {
    if (!isFinite(v)) { return "—"; }
    var a = Math.abs(v), sign = v < 0 ? "-" : "";
    if (a >= 1000) { return sign + a.toLocaleString("en-US", { maximumFractionDigits: 0 }); }
    if (a >= 1) { return sign + a.toFixed(a >= 100 ? 2 : 4); }
    if (a === 0) { return "0"; }
    var s = a.toFixed(20).split(".")[1], z = s.length - s.replace(/^0+/, "").length;
    var d = s.slice(z, z + 4).replace(/0+$/, "") || "0";
    if (z >= 4) { return sign + "0.0" + String(z).replace(/\d/g, function (c) { return SUB[+c]; }) + d; }
    return sign + "0." + "0".repeat(z) + d;
  }
  function fmtT(t, span) {
    var d = new Date(t), hh = String(d.getUTCHours()).padStart(2, "0"), mm = String(d.getUTCMinutes()).padStart(2, "0");
    return (span > 86400000 ? (d.getUTCMonth() + 1) + "/" + d.getUTCDate() + " " : "") + hh + ":" + mm;
  }

  // ------------------------------------------------------------------ charts

  var CSS = getComputedStyle(document.documentElement);
  function cssv(name, fallback) { return (CSS.getPropertyValue(name) || "").trim() || fallback; }
  var C = {
    line: cssv("--k-line", "#232d3d"), muted: cssv("--k-muted", "#8591a6"), faint: cssv("--k-faint", "#5d6a80"),
    blue: cssv("--k-blue", "#81acff"), mint: cssv("--k-mint", "#55d6ac"), salmon: cssv("--k-salmon", "#ed8094"),
    panel: cssv("--k-panel", "#111824"), mono: cssv("--k-mono", "monospace")
  };
  function alpha(hex, a) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex); if (!m) { return hex; }
    var n = parseInt(m[1], 16); return "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  function drawChart(canvas) {
    var series, marks = [];
    try { series = JSON.parse(canvas.getAttribute("data-series") || "{}"); } catch (e) { return; }
    try { marks = JSON.parse(canvas.getAttribute("data-marks") || "[]"); } catch (e) { marks = []; }
    var all = (series && series.points) || [];
    var range = parseInt(canvas.getAttribute("data-range") || "0", 10) || 0;
    var pts = all;
    if (range && all.length) {
      var cut = all[all.length - 1][0] - range;
      pts = all.filter(function (p) { return p[0] >= cut; });
      if (pts.length < 2) { pts = all.slice(-2); }
    }
    var zero = canvas.hasAttribute("data-zero");
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 600, h = canvas.clientHeight || 230;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (pts.length < 2) { return; }
    var padL = 4, padR = 78, padT = 12, padB = 24;
    var t0 = pts[0][0], t1 = pts[pts.length - 1][0];
    var lo = Infinity, hi = -Infinity;
    pts.forEach(function (p) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); });
    if (zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    if (hi === lo) { hi = hi * 1.01 + 1e-12; lo = lo * 0.99 - 1e-12; }
    var pad = (hi - lo) * 0.08; hi += pad; lo -= pad;
    var X = function (t) { return padL + (w - padL - padR) * ((t - t0) / Math.max(1, t1 - t0)); };
    var Y = function (v) { return padT + (h - padT - padB) * (1 - (v - lo) / (hi - lo)); };
    var last = pts[pts.length - 1][1];
    var col = zero ? (last >= 0 ? C.mint : C.salmon) : C.blue;
    var fmtV = function (v) { return zero ? (v >= 0 ? "+" : "") + v.toFixed(Math.abs(hi - lo) < 20 ? 1 : 0) + "%" : fmtPx(v); };

    ctx.font = "11px " + C.mono; ctx.textBaseline = "middle"; ctx.textAlign = "left";
    for (var i = 0; i <= 4; i++) {
      var v = lo + (hi - lo) * (i / 4), y = Math.round(Y(v)) + 0.5;
      ctx.strokeStyle = C.line; ctx.lineWidth = 1; ctx.setLineDash([2, 4]);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR + 6, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = C.faint; ctx.fillText(fmtV(v), w - padR + 10, y);
    }
    if (zero && lo < 0 && hi > 0) {
      var zy = Math.round(Y(0)) + 0.5;
      ctx.strokeStyle = alpha(C.muted, 0.6); ctx.beginPath(); ctx.moveTo(padL, zy); ctx.lineTo(w - padR + 6, zy); ctx.stroke();
    }
    ctx.textBaseline = "alphabetic"; ctx.fillStyle = C.faint;
    var n = Math.max(2, Math.min(6, Math.floor((w - padR) / 110)));
    for (var j = 0; j <= n; j++) {
      var tt = t0 + (t1 - t0) * (j / n);
      ctx.textAlign = j === 0 ? "left" : j === n ? "right" : "center";
      ctx.fillText(fmtT(tt, t1 - t0), X(tt), h - 6);
    }
    var floor = Y(zero ? Math.max(lo, 0) : lo);
    ctx.beginPath(); ctx.moveTo(X(pts[0][0]), Y(pts[0][1]));
    pts.forEach(function (p) { ctx.lineTo(X(p[0]), Y(p[1])); });
    ctx.lineTo(X(t1), floor); ctx.lineTo(X(t0), floor); ctx.closePath();
    ctx.fillStyle = alpha(col, 0.08); ctx.fill();
    ctx.beginPath(); ctx.moveTo(X(pts[0][0]), Y(pts[0][1]));
    pts.forEach(function (p) { ctx.lineTo(X(p[0]), Y(p[1])); });
    ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.lineJoin = "round"; ctx.stroke();
    marks.forEach(function (m) {
      if (m.t < t0 || m.t > t1 || !(m.p > lo && m.p < hi)) { return; }
      var mx = X(m.t), my = Y(m.p);
      ctx.fillStyle = m.live ? C.salmon : C.blue; ctx.strokeStyle = C.panel; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(mx, my - 7); ctx.lineTo(mx - 6, my + 4); ctx.lineTo(mx + 6, my + 4); ctx.closePath(); ctx.stroke(); ctx.fill();
    });
    var ly = Y(last);
    ctx.setLineDash([3, 3]); ctx.strokeStyle = alpha(col, 0.55); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(X(t1), ly); ctx.lineTo(w - padR + 4, ly); ctx.stroke(); ctx.setLineDash([]);
    var label = zero ? (last >= 0 ? "+" : "") + last.toFixed(1) + "%" : fmtPx(last);
    ctx.font = "600 11px " + C.mono; var tw = ctx.measureText(label).width;
    ctx.fillStyle = col; ctx.fillRect(w - padR + 4, ly - 9, tw + 12, 18);
    ctx.fillStyle = "#0b1019"; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillText(label, w - padR + 10, ly);

    var wrap = canvas.parentNode, tip = wrap.querySelector(".chart-tip");
    if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip"; tip.hidden = true; wrap.appendChild(tip); }
    var base = ctx.getImageData(0, 0, canvas.width, canvas.height);
    canvas.onmousemove = function (ev) {
      var r = canvas.getBoundingClientRect(), mx = ev.clientX - r.left;
      var t = t0 + (t1 - t0) * Math.min(1, Math.max(0, (mx - padL) / (w - padL - padR)));
      var best = pts[0];
      for (var k = 1; k < pts.length; k++) { if (Math.abs(pts[k][0] - t) < Math.abs(best[0] - t)) { best = pts[k]; } }
      var bx = X(best[0]), by = Y(best[1]);
      ctx.putImageData(base, 0, 0);
      ctx.strokeStyle = alpha(C.muted, 0.5); ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(Math.round(bx) + 0.5, padT); ctx.lineTo(Math.round(bx) + 0.5, h - padB); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(padL, Math.round(by) + 0.5); ctx.lineTo(w - padR, Math.round(by) + 0.5); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = col; ctx.beginPath(); ctx.arc(bx, by, 3.5, 0, 6.3); ctx.fill();
      var d = new Date(best[0]);
      tip.textContent = (zero ? (best[1] >= 0 ? "+" : "") + best[1].toFixed(2) + "%" : fmtPx(best[1]) + (series.unit && series.unit !== "USD" ? " " + series.unit : "")) +
        "  ·  " + d.toISOString().slice(5, 16).replace("T", " ") + " UTC";
      tip.hidden = false;
      var left = bx + canvas.offsetLeft + 12;
      if (left + tip.offsetWidth > wrap.clientWidth - 4) { left = bx + canvas.offsetLeft - tip.offsetWidth - 12; }
      tip.style.left = left + "px"; tip.style.top = Math.max(0, by + canvas.offsetTop - 34) + "px";
    };
    canvas.onmouseleave = function () { tip.hidden = true; ctx.putImageData(base, 0, 0); };
  }
  function drawCharts(root) { $$("canvas[data-series]", root).forEach(drawChart); }

  // ------------------------------------------------------------------ chart source: GMGN candles or our data
  // GMGN's public kline embed loads only while its pane is showing; switching away unloads it.
  // The choice is a per-browser convenience (localStorage), never needed for the page to work.

  function chartPref() { try { return localStorage.getItem("kx.chartSrc") || "gmgn"; } catch (e) { return "gmgn"; } }
  function setChartSrc(box, src, remember) {
    if (!box.hasAttribute("data-has-gmgn")) { src = "kaiba"; }
    box.setAttribute("data-mode", src);
    $$("[data-csrc]", box).forEach(function (b) { b.classList.toggle("on", b.getAttribute("data-csrc") === src); });
    $$("[data-cpane]", box).forEach(function (p) { p.hidden = p.getAttribute("data-cpane") !== src; });
    var frame = box.querySelector("iframe[data-src]");
    if (frame) {
      var want = src === "gmgn" ? frame.getAttribute("data-src") : "about:blank";
      if (frame.getAttribute("src") !== want && !(src !== "gmgn" && !frame.getAttribute("src"))) { frame.setAttribute("src", want); }
    }
    if (src === "kaiba") { var cv = box.querySelector("canvas[data-series]"); if (cv) { drawChart(cv); } }
    if (remember) { try { localStorage.setItem("kx.chartSrc", src); } catch (e) { /* private window */ } }
  }
  function initCharts(root) { $$("[data-chart]", root).forEach(function (b) { setChartSrc(b, chartPref(), false); }); }

  // ------------------------------------------------------------------ tabs

  var tabState = {};
  function showTab(nav, name) {
    var key = nav.getAttribute("data-tabs");
    tabState[key] = name;
    $$("[data-tab]", nav).forEach(function (b) { b.classList.toggle("on", b.getAttribute("data-tab") === name); });
    var scope = nav.closest(".kx-panel") || document;
    $$("[data-pane]", scope).forEach(function (p) { p.hidden = p.getAttribute("data-pane") !== name; });
  }
  function reapplyTabs(root) {
    var keep = root.getAttribute && root.getAttribute("data-keep-tab");
    if (!keep || !tabState[keep]) { return; }
    var nav = (root.closest(".kx-panel") || document).querySelector('[data-tabs="' + keep + '"]');
    if (nav) { showTab(nav, tabState[keep]); }
  }

  // ------------------------------------------------------------------ the token panel

  var panel = $("#token-panel");
  function currentToken() { var th = $("#token-panel [data-token]"); return th ? th.getAttribute("data-token") : null; }
  function markActive() {
    var cur = currentToken();
    $$(".wr.is-active").forEach(function (n) { n.classList.remove("is-active"); });
    if (cur) { $$('.wr[data-push="' + cur + '"]').forEach(function (n) { n.classList.add("is-active"); }); }
  }
  function mountTicket() {
    var src = $("#token-panel template[data-ticket-src]"), slot = $("[data-ticket-slot]");
    if (src && slot) { slot.innerHTML = src.innerHTML; }
  }
  function openToken(url, push) {
    if (!panel || !$("[data-term]")) { return false; }
    panel.setAttribute("aria-busy", "true");
    fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (r.status === 401) { throw new Error("signed out: reload the page to sign in"); }
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.text();
    }).then(function (html) {
      panel.innerHTML = html;
      panel.removeAttribute("aria-busy");
      panel.scrollTop = 0;
      mountTicket(); markActive(); drawCharts(panel); initCharts(panel);
      if (push) {
        var u = new URL(location.href); u.searchParams.set("t", push);
        history.replaceState(null, "", "/terminal" + u.search);
        var sym = $("#token-panel .th h2"); document.title = (sym ? sym.textContent + " · " : "") + "Kaiba Terminal";
      }
    }).catch(function (e) { panel.removeAttribute("aria-busy"); toast("Could not open the token", e.message, false); });
    return true;
  }

  // ------------------------------------------------------------------ clicks

  document.addEventListener("click", function (ev) {
    var t = ev.target;
    var tab = t.closest("[data-tabs] [data-tab]");
    if (tab) { showTab(tab.closest("[data-tabs]"), tab.getAttribute("data-tab")); return; }

    var csrc = t.closest("[data-chart] [data-csrc]");
    if (csrc) { setChartSrc(csrc.closest("[data-chart]"), csrc.getAttribute("data-csrc"), true); return; }

    var rng = t.closest(".kx-chart [data-range]");
    if (rng) {
      var box = rng.closest(".kx-chart"), cv = box.querySelector("canvas[data-series]");
      $$("[data-range]", box).forEach(function (b) { b.classList.toggle("on", b === rng); });
      if (cv) { cv.setAttribute("data-range", rng.getAttribute("data-range")); drawChart(cv); }
      return;
    }

    var copy = t.closest("[data-copy]");
    if (copy) {
      ev.preventDefault();
      var text = copy.getAttribute("data-copy");
      copyText(text).then(function () { toast("Copied", text, true); }, function () { toast("Copy failed", text, false); });
      return;
    }

    var curl = t.closest("[data-copy-url]");
    if (curl) {
      ev.preventDefault();
      var src = curl.getAttribute("data-copy-url"), label = curl.getAttribute("data-copy-label") || "list";
      var got = fetch(src, { credentials: "same-origin" }).then(function (r) { if (!r.ok) { throw new Error("HTTP " + r.status); } return r.text(); });
      var done = function () { toast("Copied " + label, "Paste it into GMGN's wallet import.", true); };
      var fail = function (e) { toast("Copy failed", (e && e.message) || "use Download instead", false); };
      if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
        var blob = got.then(function (txt) { return new Blob([txt], { type: "text/plain" }); });
        navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]).then(done, function () { got.then(copyText).then(done, fail); });
      } else { got.then(copyText).then(done, fail); }
      return;
    }

    var load = t.closest("[data-load]");
    if (load && !t.closest("button")) {
      if (openToken(load.getAttribute("data-load"), load.getAttribute("data-push"))) { ev.preventDefault(); return; }
      var href = load.getAttribute("data-href");
      if (href && load.tagName !== "A") { location.href = href; }
      return;
    }
    var ext = t.closest("[data-href]");
    if (ext && !t.closest("a, button")) {
      var to = ext.getAttribute("data-href");
      if (ext.hasAttribute("data-external")) { window.open(to, "_blank", "noopener,noreferrer"); } else { location.href = to; }
      return;
    }

    var exit = t.closest("[data-exit]");
    if (exit) {
      var pct = exit.getAttribute("data-pct"), what = exit.getAttribute("data-label") || "this position";
      if (!window.confirm("Sell " + pct + "% of " + what + "? Protection executes it on its next tick.")) { return; }
      exit.disabled = true;
      post("/control/exit", { position_id: exit.getAttribute("data-exit"), pct: pct }).then(function (j) {
        toast("Sell " + pct + "% requested", j.note || "", true);
      }).catch(function (e) { toast("Sell refused", e.message, false); exit.disabled = false; });
      return;
    }

    var p = t.closest("[data-post]");
    if (p) {
      if (!window.confirm(p.getAttribute("data-msg") || "Are you sure?")) { return; }
      p.disabled = true;
      post(p.getAttribute("data-post"), {}).then(function () { toast("Done", p.textContent.trim(), true); setTimeout(function () { location.reload(); }, 600); })
        .catch(function (e) { toast("Refused", e.message, false); p.disabled = false; });
      return;
    }

    $$(".kx-more[open]").forEach(function (d) { if (!d.contains(t)) { d.removeAttribute("open"); } });
  });

  // ------------------------------------------------------------------ forms

  document.addEventListener("submit", function (ev) {
    var f = ev.target;
    if (f.matches("[data-buy]")) {
      ev.preventDefault();
      var btn = f.querySelector("button[type=submit]"), data = {};
      new FormData(f).forEach(function (v, k) { data[k] = v; });
      btn.disabled = true;
      post("/control/paper-buy", data).then(function (j) {
        var blockers = (j.blockers || []).join(", ");
        if (j.action === "enter") { toast(j.mode === "shadow" ? "Paper buy placed" : "Buy sent", (j.lane || "manual") + " · " + (j.mode || ""), true); }
        else { toast("Not bought", blockers || j.action || "the gates said no", false); }
        btn.disabled = false;
      }).catch(function (e) { toast("Buy refused", e.message, false); btn.disabled = false; });
      return;
    }
    if (f.matches("[data-lane-mode]")) {
      ev.preventDefault();
      var d = {};
      new FormData(f).forEach(function (v, k) { d[k] = v; });
      post("/control/lane-mode", d).then(function (j) {
        toast(j.lane + ": " + j.mode_before + " → " + j.mode, "", true); setTimeout(function () { location.reload(); }, 700);
      }).catch(function (e) { toast("Not changed", e.message, false); });
      return;
    }
    if (f.matches("[data-chat-form]")) {
      ev.preventDefault();
      var c = f.closest("[data-chat]")._chat, v = c.box.value; c.box.value = ""; c.send(v);
      return;
    }
    if (f.matches("[data-tg-send]")) {
      ev.preventDefault();
      var ta = f.querySelector("textarea"), txt = ta.value.trim();
      if (!txt) { return; }
      if (!window.confirm("Post this to the Telegram chat as the bot?")) { return; }
      post("/control/agent-telegram", { text: txt }).then(function () { ta.value = ""; toast("Posted to Telegram", "", true); })
        .catch(function (e) { toast("Not posted", e.message, false); });
    }
  });
  document.addEventListener("change", function (ev) {
    var sel = ev.target;
    if (sel.matches("[data-lane-mode] select")) {
      var c = sel.form.querySelector(".ap-confirm"), live = sel.value === "canary" || sel.value === "live";
      c.hidden = !live; c.required = live;
    }
  });

  // ------------------------------------------------------------------ live regions

  var refreshers = [];
  function refreshOne(el) {
    if (document.hidden || el.dataset.busy) { return; }
    el.dataset.busy = "1";
    fetch(el.getAttribute("data-refresh"), { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) { throw new Error(String(r.status)); }
      return r.text();
    }).then(function (html) {
      document.body.removeAttribute("data-conn");
      var watch = el.hasAttribute("data-watch");
      var key = function (n) { return (n.getAttribute("data-push") || "") + "|" + n.getAttribute("data-seen"); };
      var seen = watch ? new Set($$("[data-seen]", el).map(key)) : null;
      var scroll = el.scrollTop;
      el.innerHTML = html;
      el.scrollTop = scroll;
      if (seen && seen.size) {
        $$("[data-seen]", el).forEach(function (n) { if (!seen.has(key(n))) { n.classList.add("is-new"); } });
      }
      if (watch) { var cnt = $("[data-watch-count]"); if (cnt) { cnt.textContent = $$(".wr", el).length; } }
      reapplyTabs(el); markActive(); drawCharts(el);
    }).catch(function (e) {
      document.body.setAttribute("data-conn", "lost");
      if (String(e && e.message) === "401" && !window.__kaibaSignedOut) {
        window.__kaibaSignedOut = true;
        toast("Signed out", "Your session ended. Reload the page to sign in again.", false);
      }
    }).finally(function () { delete el.dataset.busy; });
  }
  function refreshAll() { refreshers.forEach(function (r) { refreshOne(r.el); }); }
  $$("[data-refresh]").forEach(function (el) {
    var every = Math.max(2, parseInt(el.getAttribute("data-every"), 10) || 10) * 1000;
    refreshers.push({ el: el, id: setInterval(function () { refreshOne(el); }, every) });
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { refreshAll(); } });

  setInterval(function () {
    var c = $("[data-clock]"); if (c) { c.textContent = new Date().toISOString().slice(11, 19); }
  }, 1000);

  // ------------------------------------------------------------------ the agent (dock + /agent)

  function esc(v) { var d = document.createElement("div"); d.textContent = v == null ? "" : String(v); return d.innerHTML; }
  function clock(ms) { return ms ? new Date(ms).toISOString().slice(11, 16) + " UTC" : ""; }
  function bubble(kind, head, html) {
    var b = document.createElement("div"); b.className = "bub " + kind;
    var h = document.createElement("div"); h.className = "bub-h"; h.textContent = head; b.appendChild(h);
    var body = document.createElement("div"); body.className = "md"; body.innerHTML = html || ""; b.appendChild(body);
    return b;
  }

  function Chat(root) {
    this.root = root;
    this.thread = root.getAttribute("data-thread") || "terminal";
    this.msgs = root.querySelector("[data-chat-msgs]");
    this.form = root.querySelector("[data-chat-form]");
    this.box = this.form.querySelector("textarea");
    this.stopBtn = root.querySelector("[data-chat-stop]");
    this.loaded = false; this.busy = false;
    root._chat = this;
  }
  Chat.prototype.scroll = function () { this.msgs.scrollTop = this.msgs.scrollHeight; };
  Chat.prototype.load = function () {
    var self = this;
    if (self.loaded) { return Promise.resolve(); }
    return fetch("/api/chat/history?thread=" + encodeURIComponent(self.thread), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        self.msgs.innerHTML = "";
        var turns = j.turns || [];
        if (!turns.length) { self.msgs.innerHTML = '<p class="nodata pad">Ask the agent anything: a token, a wallet, the book, or what it has been doing.</p>'; }
        turns.forEach(function (t) {
          self.msgs.appendChild(bubble("me", "you · " + clock(t.started_ms), esc(t.user).replace(/\n/g, "<br>")));
          if (t.status === "running") { self.follow(t.turn_id); }
          else { self.msgs.appendChild(bubble("ai", "agent" + (t.duration_ms ? " · " + Math.round(t.duration_ms / 1000) + " s" : ""), t.reply_html || esc(t.error || "(no reply)"))); }
        });
        self.loaded = true; self.scroll();
      })
      .catch(function () { self.msgs.innerHTML = '<p class="nodata pad">Could not load the conversation.</p>'; });
  };
  Chat.prototype.follow = function (turnId, pending) {
    var self = this, text = "", tools = 0;
    pending = pending || bubble("ai pending", "agent · thinking…", "");
    if (!pending.parentNode) { self.msgs.appendChild(pending); }
    var body = pending.querySelector(".md"), head = pending.querySelector(".bub-h");
    self.busy = true; if (self.stopBtn) { self.stopBtn.hidden = false; }
    var es = new EventSource("/events/chat?turn_id=" + encodeURIComponent(turnId));
    es.addEventListener("chat", function (msg) {
      var d; try { d = JSON.parse(msg.data); } catch (e) { return; }
      (d.events || []).forEach(function (ev) {
        if (ev.kind === "text") { text += ev.text || ""; }
        else if (ev.kind === "tool") { tools += 1; head.textContent = "agent · working · " + tools + " tool call" + (tools === 1 ? "" : "s"); }
        else if (ev.kind === "start") { head.textContent = "agent · " + (ev.text || "thinking"); }
      });
      if (text) { body.innerHTML = esc(text).replace(/\n/g, "<br>"); }
      if (d.status && d.status !== "running") {
        es.close(); self.busy = false; if (self.stopBtn) { self.stopBtn.hidden = true; }
        pending.classList.remove("pending");
        head.textContent = "agent" + (d.duration_ms ? " · " + Math.round(d.duration_ms / 1000) + " s" : "") + (tools ? " · " + tools + " tools" : "");
        body.innerHTML = d.reply_html || esc(d.error || text || "(no reply)");
      }
      self.scroll();
    });
    es.onerror = function () { es.close(); self.busy = false; if (self.stopBtn) { self.stopBtn.hidden = true; } };
  };
  Chat.prototype.send = function (message) {
    var self = this;
    message = (message || "").trim();
    if (!message) { return; }
    if (self.busy) { toast("The agent is still answering", "Wait for the reply, or press Stop.", false); return; }
    self.load().then(function () {
      var empty = self.msgs.querySelector(".nodata"); if (empty) { empty.remove(); }
      self.msgs.appendChild(bubble("me", "you · " + clock(Date.now()), esc(message).replace(/\n/g, "<br>")));
      var pending = bubble("ai pending", "agent · starting…", "");
      self.msgs.appendChild(pending); self.scroll();
      post("/control/chat", { message: message, thread: self.thread }).then(function (j) {
        self.follow(j.turn_id, pending);
      }).catch(function (e) { pending.remove(); toast("Not sent", e.message, false); });
    });
  };

  var chats = $$("[data-chat]").map(function (root) { return new Chat(root); });
  chats.forEach(function (c) { if (!c.root.closest("[data-dock]")) { c.load(); } });
  var dock = $("[data-dock]");
  var dockChat = dock ? dock.querySelector("[data-chat]")._chat : null;
  function openDock(focus) {
    if (!dock) { return null; }
    dock.hidden = false;
    $$("[data-dock-toggle]").forEach(function (b) { b.setAttribute("aria-expanded", "true"); });
    dockChat.load();
    if (focus) { setTimeout(function () { dockChat.box.focus(); }, 50); }
    return dockChat;
  }
  function closeDock() {
    if (!dock) { return; }
    dock.hidden = true;
    $$("[data-dock-toggle]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
  }

  document.addEventListener("click", function (ev) {
    var tog = ev.target.closest("[data-dock-toggle]");
    if (tog && dock) { if (dock.hidden) { openDock(true); } else { closeDock(); } return; }
    var q = ev.target.closest(".ag-q");
    if (q) { q.closest("[data-chat]")._chat.send(q.getAttribute("data-prompt")); return; }
    var ask = ev.target.closest("[data-ask]");
    if (ask) {
      var c = openDock(false) || chats[0];
      if (c) { c.send(ask.getAttribute("data-ask")); } else { toast("No chat here", "Open the Agent page to ask.", false); }
      return;
    }
    var stop = ev.target.closest("[data-chat-stop]");
    if (stop) { post("/control/chat-stop", {}).then(function () { toast("Stopped", "", true); }).catch(function (e) { toast("Not stopped", e.message, false); }); return; }
    var pause = ev.target.closest("[data-agent-pause]");
    if (pause) {
      var want = pause.getAttribute("data-agent-pause") === "1", fields = { paused: want ? "1" : "0" };
      if (want) {
        var typed = window.prompt((pause.getAttribute("data-msg") || "") + "\n\nType PAUSE AGENT to confirm.");
        if (typed === null) { return; }
        fields.confirm = typed;
      } else if (!window.confirm(pause.getAttribute("data-msg") || "Resume?")) { return; }
      post("/control/agent-pause", fields).then(function () { toast(want ? "Agent paused" : "Agent resumed", "", true); setTimeout(function () { location.reload(); }, 800); })
        .catch(function (e) { toast("Refused", e.message, false); });
    }
  });

  // ------------------------------------------------------------------ keyboard

  document.addEventListener("keydown", function (ev) {
    var t = ev.target, typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (t && t.matches && t.matches("[data-chat-form] textarea") && ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault(); t.form.requestSubmit(); return;
    }
    if (ev.key === "Escape" && dock && !dock.hidden) { closeDock(); return; }
    if (typing || ev.ctrlKey || ev.metaKey || ev.altKey) { return; }
    if (ev.key === "/") { var s = $("[data-search]"); if (s) { ev.preventDefault(); s.focus(); s.select(); } return; }
    if (ev.key === "Enter" && t && t.classList && t.classList.contains("wr")) { t.click(); return; }
    if (ev.key === "a" && dock) { ev.preventDefault(); if (dock.hidden) { openDock(true); } else { closeDock(); } return; }
    if (ev.key === "j" || ev.key === "k") {
      var rows = $$("[data-watch] .wr[data-load]");
      if (!rows.length) { return; }
      var i = rows.findIndex(function (r) { return r.classList.contains("is-active"); });
      var next = rows[Math.min(rows.length - 1, Math.max(0, i + (ev.key === "j" ? 1 : -1)))] || rows[0];
      next.focus(); next.click(); next.scrollIntoView({ block: "nearest" });
    }
  });

  // ------------------------------------------------------------------ GMGN account (/intel/wallets)
  // Codex's gmgn_account_routes: GET is the last explicit check (no provider call); POST runs one
  // read-only portfolio info behind the CSRF header. The POST body must be empty or only _csrf.

  var GA_STATUS = {
    connected: ["ok", "connected"], stale: ["warn", "stale"], unavailable: ["bad", "unavailable"],
    rate_limited: ["warn", "rate limited"], invalid_response: ["bad", "unreadable answer"], not_checked: ["unk", "not checked"]
  };
  var GA_REASON = {
    explicit_check_required: "Not checked since the console started. Press Check now.",
    provider_timeout: "GMGN did not answer within 8 seconds.",
    provider_exception: "The GMGN read failed on the server.",
    provider_unavailable: "GMGN returned no account data.",
    provider_rate_limited: "GMGN's rate limit refused the read; try again shortly.",
    account_check_failed: "The check failed on the server.",
    partial_wallet_data: "Some wallet rows were unreadable and are left out.",
    cached_observation_expired: "This answer is older than a minute.",
    receipt_identity_invalid: "The answer did not come from GMGN's portfolio info.",
    wallets_shape_invalid: "GMGN's answer had no wallet list.",
    wallet_identities_invalid: "No wallet in the answer had a valid address."
  };
  var GA_EXPLORER = { sol: "sol", bsc: "bsc", eth: "eth", base: "base" };
  function renderAccount(box, j) {
    var st = GA_STATUS[j.status] || ["unk", j.status || "unknown"];
    var pill = box.querySelector("[data-ga-status]"); pill.className = "pill " + st[0]; pill.textContent = st[1];
    var when = box.querySelector("[data-ga-when]");
    when.textContent = j.observed_at_ms ? "observed " + Math.round((j.age_ms || 0) / 1000) + " s ago" : "";
    var note = box.querySelector("[data-ga-note]");
    if (j.reason && GA_REASON[j.reason]) { note.textContent = GA_REASON[j.reason]; }
    else if (j.status === "connected" || j.status === "stale") {
      note.textContent = j.wallet_count + " wallet" + (j.wallet_count === 1 ? "" : "s") + " bound to the API key. GMGN does not report balances here, so none are shown; use the Autopilot book for what the agent holds.";
    }
    var list = box.querySelector("[data-ga-wallets]"); list.textContent = "";
    (j.wallets || []).forEach(function (w) {
      var row = document.createElement("div"); row.className = "ga-w";
      var chip = document.createElement("span"); chip.className = "chip c-" + w.chain; chip.textContent = w.chain; row.appendChild(chip);
      var addr = document.createElement("span"); addr.className = "mono"; addr.textContent = w.address.slice(0, 6) + "…" + w.address.slice(-6); addr.title = w.address; row.appendChild(addr);
      var copy = document.createElement("button"); copy.type = "button"; copy.className = "kb xs ghost"; copy.textContent = "Copy"; copy.setAttribute("data-copy", w.address); row.appendChild(copy);
      if (GA_EXPLORER[w.chain]) {
        var a = document.createElement("a"); a.className = "kb xs ghost"; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.href = "https://gmgn.ai/" + GA_EXPLORER[w.chain] + "/address/" + encodeURIComponent(w.address); a.textContent = "GMGN ↗"; row.appendChild(a);
      }
      var bal = document.createElement("span"); bal.className = "bal"; bal.textContent = w.native_symbol + " balance not reported"; row.appendChild(bal);
      list.appendChild(row);
    });
  }
  var gaBox = $("[data-gmgn-account]");
  if (gaBox) {
    fetch("/api/gmgn/account", { credentials: "same-origin", headers: { accept: "application/json" } })
      .then(function (r) { return r.json(); }).then(function (j) { renderAccount(gaBox, j); }).catch(function () { /* stays "not checked" */ });
    gaBox.querySelector("[data-ga-check]").addEventListener("click", function (ev) {
      var btn = ev.currentTarget; btn.disabled = true;
      fetch("/api/gmgn/account/check", { method: "POST", credentials: "same-origin", headers: { "x-kaiba-csrf": CSRF, accept: "application/json" } })
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            if (j && j.status) { renderAccount(gaBox, j); }
            if (r.status === 429 || r.status === 409) { toast("Checked recently", "Try again in " + ((j.check && j.check.retry_after_s) || 30) + " s.", false); }
            else if (!r.ok && !(j && j.status)) { toast("Check refused", (j && j.detail) || ("HTTP " + r.status), false); }
          });
        })
        .catch(function (e) { toast("Check failed", e.message, false); })
        .finally(function () { setTimeout(function () { btn.disabled = false; }, 1500); });
    });
  }

  mountTicket(); markActive(); drawCharts(document); initCharts(document);
  var resizeTimer = null;
  window.addEventListener("resize", function () { clearTimeout(resizeTimer); resizeTimer = setTimeout(function () { drawCharts(document); }, 150); });
})();
