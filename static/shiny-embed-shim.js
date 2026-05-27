// Shiny iframe runtime shim.
//
// Adapts Shiny's client to the constraints of an MCP-App iframe whose
// base URL is the parent host's origin (not ours):
//
//   - WebSocket: Shiny computes the WS URL from `location.host`, which
//     is empty/host-origin in this context. We force every WebSocket
//     construction to point at our reverse-proxy WS endpoint.
//   - fetch + XHR: Shiny generates session-relative URLs at runtime
//     (`session/<id>/...`, `_w_xxx/...`) that aren't in the initial
//     HTML, so HTML rewriting can't catch them. We rewrite at request
//     time instead.
//   - No <base href>: the host enforces `base-uri 'self'` and rejects
//     any non-self base URL, so we can't use that shorthand.
//
// We ALSO inject a small floating ResultsBus widget into the iframe.
// It lets the Shiny iframe send payloads to the hub (NAV AI) iframe
// and receive payloads from it, all through the server's /bus/* relay
// — no postMessage between siblings is required.
//
// Two placeholders are substituted server-side before this script is
// inlined into the iframe body:
//   {{PROXY_BASE}}  →  e.g. "https://nav-ai-mock-mcp.onrender.com/shiny-proxy/"
//   {{PROXY_WS}}    →  e.g. "wss://nav-ai-mock-mcp.onrender.com/shiny-proxy/websocket/"

(() => {
  const PROXY_BASE = '{{PROXY_BASE}}';
  const PROXY_NO_SLASH = PROXY_BASE.replace(/\/+$/, '');
  const PROXY_WS = '{{PROXY_WS}}';

  // Derive the bare service origin (no /shiny-proxy/ suffix) from the
  // proxy base — the bus endpoints live at the service root, not under
  // the proxy.
  const SERVICE_ORIGIN = PROXY_NO_SLASH.replace(/\/shiny-proxy$/, '');

  const rewriteUrl = (u) => {
    if (!u || typeof u !== 'string') return u;
    if (/^[a-zA-Z][a-zA-Z0-9+.\-]*:/.test(u)) return u;  // already has a scheme
    if (u.startsWith('//')) return u;                    // protocol-relative
    if (u.startsWith('#')) return u;                     // fragment
    if (u.startsWith('/')) return PROXY_NO_SLASH + u;    // absolute path
    return PROXY_BASE + u;                               // relative
  };

  // ── WebSocket monkey-patch ──
  const OriginalWS = window.WebSocket;
  class PatchedWS extends OriginalWS {
    constructor(_url, protocols) {
      super(PROXY_WS, protocols);
    }
  }
  window.WebSocket = PatchedWS;

  // ── fetch wrapper ──
  if (window.fetch) {
    const origFetch = window.fetch;
    window.fetch = function (input, init) {
      if (typeof input === 'string') {
        input = rewriteUrl(input);
      } else if (input && typeof Request !== 'undefined' && input instanceof Request) {
        try { input = new Request(rewriteUrl(input.url), input); } catch (_) {}
      }
      return origFetch.call(window, input, init);
    };
  }

  // ── XMLHttpRequest wrapper ──
  if (window.XMLHttpRequest && XMLHttpRequest.prototype) {
    const origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      return origOpen.call(this, method, rewriteUrl(url), ...rest);
    };
  }

  // ── ResultsBus widget ──────────────────────────────────────────────
  // Floating top-right panel: shows messages coming in from the hub on
  // topic `hub→shiny`, and a Send button that publishes a reply on
  // `shiny→hub`. Pure DOM, no Shiny bindings — survives any rebuild of
  // the Shiny client.
  // Topic pair must match the hub's delegation view and the standalone
  // /ui/peer page — every iframe is just "a peer" from the bus's view.
  const TOPIC_IN = 'hub→peer';
  const TOPIC_OUT = 'peer→hub';
  // Bus URLs are absolute (start with https://…), and rewriteUrl leaves
  // already-schemed URLs alone — so the wrapped fetch is safe to use.
  const busPost = (topic, payload) => fetch(SERVICE_ORIGIN + '/bus/publish', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({topic, payload}),
  });

  const mountWidget = () => {
    if (document.getElementById('mcp-bus-widget')) return;

    const wrap = document.createElement('div');
    wrap.id = 'mcp-bus-widget';
    wrap.style.cssText = [
      'position:fixed', 'top:12px', 'right:12px', 'z-index:99999',
      'width:280px', 'background:#15161b', 'color:#e6e6ea',
      'border:1px solid #2a5a3a', 'border-radius:8px', 'padding:10px 12px',
      "font-family:'JetBrains Mono',ui-monospace,monospace", 'font-size:11px',
      'box-shadow:0 4px 16px rgba(0,0,0,0.5)',
    ].join(';');

    wrap.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;
                  margin-bottom:8px;letter-spacing:.12em;color:#5fb878">
        <span>SHINY · BUS</span>
        <button id="mcp-bus-toggle" style="background:none;border:0;color:#7a8087;
                cursor:pointer;font:inherit">_</button>
      </div>
      <div id="mcp-bus-body">
        <div style="color:#7a8087;margin-bottom:6px">incoming (hub→shiny):</div>
        <div id="mcp-bus-incoming"
             style="background:#0d0d10;border:1px solid #24252c;border-radius:4px;
                    padding:6px 8px;min-height:44px;max-height:140px;overflow:auto;
                    margin-bottom:10px;line-height:1.5">
          <span style="color:#7a8087;font-style:italic">waiting…</span>
        </div>
        <div style="display:flex;gap:6px">
          <input id="mcp-bus-reply" type="text" placeholder="reply text"
                 style="flex:1;background:#0d0d10;border:1px solid #24252c;
                        color:#e6e6ea;padding:6px 8px;border-radius:4px;
                        font:inherit"/>
          <button id="mcp-bus-send" style="background:#1a3322;color:#5fb878;
                  border:1px solid #2a5a3a;padding:6px 10px;border-radius:4px;
                  cursor:pointer;font:inherit">Send →</button>
        </div>
        <div id="mcp-bus-status" style="color:#7a8087;margin-top:6px;min-height:1.2em"></div>
      </div>
    `;
    document.body.appendChild(wrap);

    const incoming = document.getElementById('mcp-bus-incoming');
    const replyInput = document.getElementById('mcp-bus-reply');
    const sendBtn = document.getElementById('mcp-bus-send');
    const status = document.getElementById('mcp-bus-status');
    const toggle = document.getElementById('mcp-bus-toggle');
    const body = document.getElementById('mcp-bus-body');

    toggle.addEventListener('click', () => {
      const hidden = body.style.display === 'none';
      body.style.display = hidden ? '' : 'none';
      toggle.textContent = hidden ? '_' : '+';
    });

    let placeholderShown = true;
    const appendIncoming = (payload) => {
      if (placeholderShown) { incoming.innerHTML = ''; placeholderShown = false; }
      const row = document.createElement('div');
      const text = typeof payload === 'string' ? payload : JSON.stringify(payload);
      row.textContent = new Date().toLocaleTimeString() + '  ' + text;
      row.style.cssText = 'border-bottom:1px solid #1d1e24;padding:3px 0';
      incoming.appendChild(row);
      incoming.scrollTop = incoming.scrollHeight;
    };

    sendBtn.addEventListener('click', async () => {
      const text = (replyInput.value || '').trim();
      if (!text) { status.textContent = 'enter a reply first'; return; }
      status.textContent = 'publishing…';
      try {
        const r = await busPost(TOPIC_OUT, {
          from: 'shiny', sent_at: Date.now(), text,
        });
        const j = await r.json();
        status.textContent = '→ delivered to ' + j.delivered + ' listener(s)';
        replyInput.value = '';
      } catch (e) {
        status.textContent = 'publish failed: ' + ((e && e.message) || e);
      }
    });

    // Subscribe to hub messages. EventSource handles its own reconnect.
    const url = SERVICE_ORIGIN + '/bus/subscribe?topic=' + encodeURIComponent(TOPIC_IN);
    try {
      const es = new EventSource(url);
      es.addEventListener('message', (ev) => {
        let parsed;
        try { parsed = JSON.parse(ev.data); } catch { parsed = ev.data; }
        appendIncoming(parsed);
      });
      es.onerror = () => {
        // Silent — EventSource retries automatically. Visible only if
        // the page is opened outside a working network.
      };
    } catch (e) {
      status.textContent = 'subscribe failed: ' + ((e && e.message) || e);
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountWidget, { once: true });
  } else {
    mountWidget();
  }
})();
