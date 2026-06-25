/**
 * Embeddable AI front-desk web chat widget.
 * Drop on any site:  <script src="https://app.example.com/widget.js" data-tenant="TENANT_ID"></script>
 * Minimal loader: renders a launcher + panel and talks to POST /api/chat.
 */
(function () {
  var script = document.currentScript;
  var tenantId = script && script.getAttribute("data-tenant");
  var apiBase = (script && script.getAttribute("data-api")) || script.src.replace(/\/widget\.js.*$/, "");
  if (!tenantId) return console.error("[ai-front-desk] missing data-tenant");

  var visitorRef = localStorage.getItem("afd_visitor") || "v_" + Math.random().toString(36).slice(2);
  localStorage.setItem("afd_visitor", visitorRef);
  var history = [];

  var panel = document.createElement("div");
  panel.style.cssText =
    "position:fixed;bottom:88px;right:20px;width:340px;max-height:60vh;display:none;flex-direction:column;" +
    "background:#fff;border:1px solid #e5e5e5;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.12);" +
    "font:14px/1.4 system-ui;z-index:99999;overflow:hidden";
  panel.innerHTML =
    '<div style="padding:12px 14px;background:#111;color:#fff;font-weight:600">Chat with us</div>' +
    '<div id="afd-log" style="flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:8px"></div>' +
    '<form id="afd-form" style="display:flex;border-top:1px solid #eee">' +
    '<input id="afd-input" placeholder="Type a message…" style="flex:1;border:0;padding:12px;outline:none"/>' +
    '<button style="border:0;background:#111;color:#fff;padding:0 16px;cursor:pointer">Send</button></form>';

  var btn = document.createElement("button");
  btn.textContent = "Chat";
  btn.style.cssText =
    "position:fixed;bottom:20px;right:20px;border:0;background:#111;color:#fff;border-radius:999px;" +
    "padding:14px 20px;font:600 14px system-ui;cursor:pointer;z-index:99999;box-shadow:0 6px 20px rgba(0,0,0,.2)";
  btn.onclick = function () {
    panel.style.display = panel.style.display === "none" ? "flex" : "none";
  };

  document.body.appendChild(panel);
  document.body.appendChild(btn);

  function add(role, text) {
    var log = panel.querySelector("#afd-log");
    var el = document.createElement("div");
    el.textContent = text;
    el.style.cssText =
      "max-width:80%;padding:8px 10px;border-radius:10px;" +
      (role === "user" ? "align-self:flex-end;background:#111;color:#fff" : "align-self:flex-start;background:#f1f1f1");
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  panel.querySelector("#afd-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var input = panel.querySelector("#afd-input");
    var msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    add("user", msg);
    fetch(apiBase + "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenantId: tenantId, visitorRef: visitorRef, message: msg, history: history }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        history = d.history || history;
        add("assistant", d.reply || "…");
      })
      .catch(function () { add("assistant", "Sorry, something went wrong."); });
  });
})();
