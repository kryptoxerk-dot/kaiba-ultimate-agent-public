/* Agent chat and commands pages.
 *
 * Both pages work as plain forms without this file (the server answers the POST with JSON);
 * this adds the live part: the agent's reply and a command's output stream in over SSE.
 * Text from the agent or a command is always inserted with textContent. The one exception is
 * a finished agent reply, which the server renders from Markdown with raw HTML disabled. */

(function () {
  "use strict";

  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.getAttribute("content") : "";

  function post(url, form) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "x-kaiba-csrf": csrf },
      body: new URLSearchParams(new FormData(form))
    }).then(function (res) {
      return res.json().then(
        function (body) { return { ok: res.ok, status: res.status, body: body }; },
        function () { return { ok: false, status: res.status, body: { error: "HTTP " + res.status } }; }
      );
    }, function () {
      return { ok: false, status: 0, body: { error: "network error: the console did not answer" } };
    });
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function secs(ms) {
    if (!ms && ms !== 0) return "";
    var s = ms / 1000;
    return s < 90 ? Math.round(s) + "s" : Math.round(s / 60) + "m";
  }

  /* ------------------------------------------------------------------ chat */

  var chat = document.getElementById("chat");
  if (chat) {
    var conv = document.getElementById("conversation");
    var form = document.getElementById("composer");
    var box = document.getElementById("message");
    var sendBtn = document.getElementById("chat-send");
    var stopBtn = document.getElementById("chat-stop");
    var status = document.getElementById("chat-status");
    var empty = conv.querySelector(".empty-chat");

    var scroll = function () { conv.scrollTop = conv.scrollHeight; window.scrollTo(0, document.body.scrollHeight); };
    var busy = function (on, text) {
      sendBtn.disabled = on;
      stopBtn.hidden = !on;
      status.textContent = text || "";
    };

    var addUser = function (text) {
      if (empty) { empty.remove(); empty = null; }
      var a = el("article", "msg msg-user");
      a.appendChild(el("div", "who", "you · now"));
      a.appendChild(el("div", "body plain", text));
      conv.appendChild(a);
      scroll();
    };

    var follow = function (turnId) {
      var a = el("article", "msg msg-agent");
      var who = el("div", "who", "agent · starting…");
      var live = el("div", "body plain live", "");
      var steps = el("ul", "steps");
      a.appendChild(who);
      a.appendChild(live);
      a.appendChild(steps);
      conv.appendChild(a);
      scroll();
      busy(true, "the agent is working…");
      var started = Date.now();
      var tick = setInterval(function () { status.textContent = "the agent is working… " + secs(Date.now() - started); }, 1000);
      var es = new EventSource("/events/chat?turn_id=" + encodeURIComponent(turnId));
      var finish = function (text) {
        clearInterval(tick);
        es.close();
        busy(false, text || "");
      };
      es.addEventListener("chat", function (ev) {
        var d;
        try { d = JSON.parse(ev.data); } catch (e) { return; }
        (d.events || []).forEach(function (e) {
          if (e.kind === "text") { live.textContent += e.text; }
          else if (e.kind === "start") { who.textContent = "agent · " + e.text; }
          else if (e.kind === "tool" || e.kind === "error" || e.kind === "log") {
            steps.appendChild(el("li", e.kind === "error" ? "neg" : "", e.text));
          }
        });
        scroll();
        if (d.status && d.status !== "running") {
          var bits = ["agent"];
          if (d.model) bits.push(d.model);
          if (d.duration_ms) bits.push(secs(d.duration_ms));
          if (d.tokens && d.tokens.total) bits.push(d.tokens.total.toLocaleString() + " tokens");
          who.textContent = bits.join(" · ");
          if (d.reply_html) {
            var body = el("div", "body md");
            body.innerHTML = d.reply_html;  // server-rendered Markdown, raw HTML disabled
            a.replaceChild(body, live);
          }
          if (d.error) { a.classList.add("msg-failed"); a.appendChild(el("div", "body error", d.error)); }
          if (!steps.children.length) steps.remove();
          finish(d.status === "done" ? "" : "the agent did not finish this reply");
          scroll();
        }
      });
      es.onerror = function () {
        finish("lost the live stream; reload the page to see the reply when it is ready");
      };
    };

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var text = box.value.trim();
      if (!text) return;
      busy(true, "sending…");
      post("/control/chat", form).then(function (res) {
        if (!res.ok) { busy(false, res.body.error || "refused"); return; }
        addUser(text);
        box.value = "";
        follow(res.body.turn_id);
      });
    });
    box.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        if (!sendBtn.disabled) form.requestSubmit();
      }
    });
    stopBtn.addEventListener("click", function () {
      var f = el("form");
      var c = el("input");
      c.name = "_csrf";
      c.value = csrf;
      f.appendChild(c);
      post("/control/chat-stop", f).then(function () { status.textContent = "stopping…"; });
    });
    scroll();
    if (chat.getAttribute("data-running")) follow(chat.getAttribute("data-running"));
  }

  /* ------------------------------------------------------------------ commands */

  var out = document.getElementById("run-output");
  if (out) {
    var title = document.getElementById("output-title");
    var info = document.getElementById("output-meta");
    var current = null;

    var show = function (label, lines, state) {
      title.textContent = label;
      out.textContent = (lines || []).join("\n");
      info.textContent = state || "";
    };

    var watch = function (runId, label) {
      if (current) current.close();
      show(label, [], "running…");
      document.getElementById("output-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      var es = new EventSource("/events/command/" + encodeURIComponent(runId));
      current = es;
      es.addEventListener("run", function (ev) {
        var d;
        try { d = JSON.parse(ev.data); } catch (e) { return; }
        if (d.lines && d.lines.length) {
          out.textContent += (out.textContent ? "\n" : "") + d.lines.join("\n");
          out.scrollTop = out.scrollHeight;
        }
        if (d.status && d.status !== "running") {
          info.textContent = "finished: " + d.status + (d.exit_code !== null && d.exit_code !== undefined ? " (exit " + d.exit_code + ")" : "");
          es.close();
          current = null;
        } else {
          info.textContent = "running…";
        }
      });
      es.onerror = function () { info.textContent = "stream closed; open the run below for its output"; es.close(); };
    };

    Array.prototype.forEach.call(document.querySelectorAll("form.command"), function (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var label = form.querySelector(".command-head b").textContent;
        post("/control/command", form).then(function (res) {
          if (!res.ok) { show(label, [res.body.error || "refused"], "not started"); return; }
          var confirmBox = form.querySelector('input[name="confirm"]');
          if (confirmBox) confirmBox.value = "";
          watch(res.body.run_id, res.body.label || label);
        });
      });
    });

    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest("[data-run]") : null;
      if (!btn) return;
      var id = btn.getAttribute("data-run");
      fetch("/api/commands/runs/" + encodeURIComponent(id), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status === "running") { watch(id, d.label); return; }
          show(d.label + (d.arg ? " · " + d.arg : ""), d.lines, "finished: " + d.status);
          document.getElementById("output-panel").scrollIntoView({ behavior: "smooth", block: "start" });
        });
    });

    document.getElementById("output-copy").addEventListener("click", function () {
      if (navigator.clipboard) navigator.clipboard.writeText(out.textContent);
    });

    if (location.hash) {
      var target = document.getElementById(location.hash.slice(1));
      if (target && target.classList.contains("command")) target.classList.add("highlight");
    }
  }
})();
