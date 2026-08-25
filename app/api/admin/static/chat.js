/*
 * Session chat widget shared by the admin session page and the tenant chat
 * portal. Endpoints are derived from #chat[data-base] (e.g. /admin/sessions/ID
 * or /chat/s/ID): POST base/message (SSE), GET base/state, base/files/...
 */
(function () {
  "use strict";
  var chat = document.getElementById("chat");
  var BASE = chat ? (chat.dataset.base || location.pathname) : location.pathname;

  // md.js loads deferred (after this script), so keep a full local renderer.
  function mdLite(text) {
    if (window.mdLite) return window.mdLite(text);
    var e = document.createElement("div");
    e.textContent = text || "";
    var s = e.innerHTML;
    s = s.replace(/\n?```(?:[\w-]*)\n?([\s\S]*?)\n?```\n?/g, function (_, code) {
      return '<pre class="md-code"><code>' + code + "</code></pre>";
    });
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    return s;
  }
  document.querySelectorAll(".b-ai.md").forEach(function (el) { el.innerHTML = mdLite(el.textContent); });

  window.onAttach = function (input) {
    if (!input.value) return;
    var btn = document.getElementById("attach-btn");
    if (btn) { btn.disabled = true; btn.classList.add("busy"); }
    input.form.submit();
  };
  async function removeFile(path) {
    if (!confirm("Remove " + path + " from the workspace?")) return;
    var res = await fetch(BASE + "/files/" + path.split("/").map(encodeURIComponent).join("/"), { method: "DELETE" });
    if (res.ok) location.reload();
    else alert("Could not remove file (" + res.status + ")");
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest(".fc-remove");
    if (b) removeFile(b.dataset.path);
  });

  function _scroll() { if (chat) chat.scrollTop = chat.scrollHeight; }
  function _row(side) {
    var r = document.createElement("div");
    r.className = "crow crow-" + (side === "user" ? "user" : "ai");
    if (side !== "user") {
      var a = document.createElement("span");
      a.className = "c-avatar" + (side === "ai" ? "" : " ghost");
      r.appendChild(a);
    }
    return r;
  }
  function _append(turn, side, node) { var r = _row(side); r.appendChild(node); turn.appendChild(r); }
  function _bub(cls, text, isMd) {
    var d = document.createElement("div");
    d.className = "bubble " + cls;
    if (isMd) d.innerHTML = mdLite(text); else d.textContent = text;
    return d;
  }
  function _liveEvent(turn, type, ev) {
    if (type === "text" && ev.text) _append(turn, "ai", _bub("b-ai", ev.text, true));
    else if (type === "thinking" && (ev.text || "").trim()) _append(turn, "ghost", _bub("b-think", ev.text));
    else if (type === "tool_use") {
      var d = document.createElement("div");
      d.className = "tool-call";
      d.innerHTML = '<span class="tc-badge">tool</span> <span class="mono"></span> <span class="tc-args"></span>';
      d.querySelector(".mono").textContent = ev.name || "";
      d.querySelector(".tc-args").textContent = JSON.stringify(ev.input || {});
      _append(turn, "ghost", d);
    } else if (type === "tool_result") {
      var c = typeof ev.content === "string" ? ev.content : JSON.stringify(ev.content);
      var det = document.createElement("details");
      det.className = "tool-result" + (ev.is_error ? " err" : "");
      var sm = document.createElement("summary"); sm.textContent = ev.is_error ? "error" : "result"; det.appendChild(sm);
      var pre = document.createElement("div"); pre.className = "pre"; pre.textContent = (c || "").slice(0, 4000); det.appendChild(pre);
      _append(turn, "ghost", det);
    } else if (type === "error") _append(turn, "ghost", _bub("b-error", ev.message || "error"));
  }

  var promptEl = document.getElementById("prompt");
  if (promptEl) {
    promptEl.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 160) + "px";
    });
    promptEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        document.getElementById("composer").requestSubmit();
      }
    });
  }

  var STREAMING = false;
  window.sendMessage = async function (e) {
    e.preventDefault();
    var prompt = promptEl.value.trim();
    if (!prompt || STREAMING) return false;
    var btn = document.getElementById("send-btn");
    var status = document.getElementById("send-status");
    btn.disabled = true; status.textContent = "Sending…"; STREAMING = true;

    var empty = chat.querySelector(".chat-empty");
    if (empty) empty.remove();
    var turn = document.createElement("div");
    turn.className = "chat-turn"; turn.id = "live-turn";
    var div = document.createElement("div"); div.className = "chat-divider"; div.innerHTML = "<span>Now</span>";
    turn.appendChild(div);
    _append(turn, "user", _bub("b-user", prompt));
    chat.appendChild(turn);
    promptEl.value = ""; promptEl.style.height = "auto";
    _scroll();

    try {
      var res = await fetch(BASE + "/message", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: prompt }),
      });
      if (!res.ok) {
        var t = await res.text();
        status.textContent = "Error " + res.status;
        _append(turn, "ghost", _bub("b-error", t.slice(0, 400)));
        btn.disabled = false; STREAMING = false; _scroll(); return false;
      }
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
      while (true) {
        var r = await reader.read();
        if (r.done) break;
        buf += dec.decode(r.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          var frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
          var type = "", data = "";
          frame.split("\n").forEach(function (l) {
            if (l.indexOf("event:") === 0) type = l.slice(6).trim();
            else if (l.indexOf("data:") === 0) data += l.slice(5).trim();
          });
          if (data) { try { _liveEvent(turn, type, JSON.parse(data)); _scroll(); } catch (_) {} }
        }
      }
      status.textContent = ""; STREAMING = false;
      setTimeout(function () { location.reload(); }, 500);
    } catch (err) {
      status.textContent = "Error";
      _append(turn, "ghost", _bub("b-error", String(err)));
      btn.disabled = false; STREAMING = false;
    }
    return false;
  };

  (function () {
    if (!chat) return;
    setInterval(async function () {
      if (STREAMING || document.hidden) return;
      try {
        var res = await fetch(BASE + "/state");
        if (!res.ok) return;
        var s = await res.json();
        if (String(s.events) !== chat.dataset.events || s.status !== chat.dataset.status) location.reload();
      } catch (_) {}
    }, 3500);
  })();

  _scroll();
})();
