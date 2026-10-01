/* Progressive enhancement only.
 *
 * The page is fully rendered by the server; everything below is optional polish:
 * copy-to-clipboard on truncated addresses, an SSE connection light, trimming the feed so
 * a long session does not grow without bound, and the Cytoscape entity graph. If HTMX or
 * Cytoscape fail to load (offline VPS, blocked CDN) the console still shows every number,
 * and the graph falls back to the table that is already in the DOM. */

(function () {
  "use strict";

  var ARCHETYPE_COLOUR = {
    insider: "#ff8b77",
    sniper: "#ffd37a",
    bundler: "#ff8b77",
    dev: "#ff8b77",
    kol: "#ab93ff",
    smart_money: "#77e5a0",
    top_trader: "#77e5a0",
    diamond: "#70e4f5",
    early_buyer: "#70e4f5",
    side_wallet: "#ab93ff",
    copybot: "#7f8da0",
    bot: "#7f8da0",
    fomo: "#7f8da0",
    unknown: "#4d6076"
  };

  /* ---------------------------------------------------------- copy addresses */

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest(".addr") : null;
    if (!btn) return;
    var value = btn.getAttribute("data-copy");
    if (!value) return;
    var done = function () {
      btn.classList.add("copied");
      setTimeout(function () { btn.classList.remove("copied"); }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done, function () {});
    } else {
      var box = document.createElement("textarea");
      box.value = value;
      document.body.appendChild(box);
      box.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* nothing we can do */ }
      document.body.removeChild(box);
    }
  });

  /* ---------------------------------------------------------- nav highlight */

  var navItems = Array.prototype.slice.call(document.querySelectorAll(".nav-item"));
  if (navItems.length && "IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        navItems.forEach(function (item) {
          item.classList.toggle("active", item.getAttribute("href") === "#" + entry.target.id);
        });
      });
    }, { rootMargin: "-20% 0px -70% 0px" });
    navItems.forEach(function (item) {
      var target = document.querySelector(item.getAttribute("href"));
      if (target) observer.observe(target);
    });
  }

  /* ---------------------------------------------------------- stream light */

  var light = document.getElementById("stream-status");
  function setStream(state, label) {
    if (!light) return;
    light.setAttribute("data-state", state);
    light.innerHTML = "<i></i> " + label;
  }
  document.body.addEventListener("htmx:sseOpen", function () { setStream("open", "stream live"); });
  document.body.addEventListener("htmx:sseError", function () { setStream("closed", "stream lost — retrying"); });
  document.body.addEventListener("htmx:sseClose", function () { setStream("closed", "stream closed"); });

  /* ---------------------------------------------------------- feed trimming */

  var feed = document.getElementById("feed");
  if (feed) {
    var max = parseInt(feed.getAttribute("data-max-rows") || "200", 10);
    var trim = function () {
      var rows = feed.querySelectorAll(".feed-row");
      for (var i = max; i < rows.length; i++) rows[i].remove();
    };
    document.body.addEventListener("htmx:sseMessage", function () { setTimeout(trim, 0); });
    document.body.addEventListener("htmx:afterSwap", function (e) {
      if (e.target === feed) trim();
    });
  }

  /* ---------------------------------------------------------- entity graph */

  function drawGraph() {
    var host = document.getElementById("entity-graph");
    if (!host || host.getAttribute("data-drawn") === "1") return;
    var fallback = document.getElementById("entity-graph-fallback");
    if (typeof cytoscape === "undefined") {
      // Library missing: keep the honest table and say why.
      host.style.display = "none";
      if (fallback) {
        fallback.open = true;
        fallback.querySelector("summary").textContent =
          "Graph library unavailable — showing the same data as a table";
      }
      return;
    }
    var raw = host.getAttribute("data-graph");
    var graph;
    try { graph = JSON.parse(raw); } catch (e) { return; }
    if (!graph || !graph.nodes || !graph.nodes.length) return;

    var elements = graph.nodes.map(function (n) {
      return { data: { id: n.id, label: n.label, archetype: n.archetype || "unknown", grade: n.grade } };
    }).concat(graph.edges.map(function (e) {
      return { data: { id: e.id, source: e.source, target: e.target, kind: e.edge_type } };
    }));

    host.setAttribute("data-drawn", "1");
    cytoscape({
      container: host,
      elements: elements,
      layout: { name: "cose", animate: false, nodeRepulsion: 9000, idealEdgeLength: 70 },
      style: [
        {
          selector: "node",
          style: {
            "background-color": function (n) {
              return ARCHETYPE_COLOUR[n.data("archetype")] || ARCHETYPE_COLOUR.unknown;
            },
            label: "data(label)",
            color: "#c7d4e2",
            "font-size": 8,
            "text-valign": "bottom",
            "text-margin-y": 4,
            width: 14,
            height: 14,
            "border-width": 1,
            "border-color": "#0d141f"
          }
        },
        {
          selector: "edge",
          style: {
            width: 1,
            "line-color": "#39566b",
            "curve-style": "haystack",
            opacity: 0.7
          }
        }
      ]
    });
  }

  if (document.readyState === "complete") {
    drawGraph();
  } else {
    window.addEventListener("load", drawGraph);
  }
  document.body.addEventListener("htmx:afterSwap", function () { setTimeout(drawGraph, 0); });
})();
