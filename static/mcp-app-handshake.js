// Minimal MCP Apps host handshake.
//
// Claude's MCP App host mounts the iframe per tool call, but keeps it
// invisible until the iframe posts `ui/notifications/initialized` back
// to the parent via postMessage. The full NAV AI shell handles this in
// shell.js; iframes that don't ship our shell (the hello-world resource,
// the rewritten Shiny embed) must run this minimal handshake instead.
//
// Loaded once by the server and inlined into the iframe body so it
// executes immediately, before any other scripts the iframe might need
// to fetch from over the network.

(() => {
  // Captured from the first inbound host message. Until then we have to
  // broadcast — the host's origin isn't knowable from inside a sandboxed
  // iframe up-front (no document.referrer guarantee, no ancestorOrigins
  // on Safari). Switching to a pinned origin on first receipt shrinks
  // the postMessage attack surface for the rest of the session.
  let targetOrigin = '*';

  const send = (m) => {
    try { window.parent.postMessage(m, targetOrigin); } catch (_) {}
  };

  // Track display mode + which modes the host advertises, so the floating
  // maximize button only appears when the host can actually honour the
  // request. Updated from the ui/initialize response and from
  // ui/notifications/host-context-changed (per SEP-1865).
  let hostDisplayMode = 'inline';
  let hostAvailableModes = ['inline'];
  let initId = Math.floor(Math.random() * 1e9);
  let nextId = initId + 1;
  const pending = new Map();

  const updateMaxButton = () => {
    const btn = document.getElementById('mcp-mode-toggle');
    if (!btn) return;
    if (hostAvailableModes.indexOf('fullscreen') === -1) {
      btn.style.display = 'none';
      return;
    }
    btn.style.display = '';
    if (hostDisplayMode === 'fullscreen') {
      btn.textContent = '↙ restore';
      btn.dataset.target = 'inline';
    } else {
      btn.textContent = '↗ maximize';
      btn.dataset.target = 'fullscreen';
    }
  };

  const applyHostContext = (ctx) => {
    if (!ctx || typeof ctx !== 'object') return;
    if (typeof ctx.displayMode === 'string') hostDisplayMode = ctx.displayMode;
    if (Array.isArray(ctx.availableDisplayModes)) hostAvailableModes = ctx.availableDisplayModes;
    updateMaxButton();
  };

  // Register the inbound listener BEFORE any outbound notifications.
  // Some hosts reply immediately to `ui/notifications/initialized`, and
  // we don't want to race the listener wiring against that reply.
  window.addEventListener('message', (ev) => {
    if (targetOrigin === '*' && ev.origin && ev.origin !== 'null') {
      targetOrigin = ev.origin;
    }
    const m = ev.data;
    if (!m || m.jsonrpc !== '2.0') return;
    if (m.method === 'ping' && m.id != null) {
      send({ jsonrpc: '2.0', id: m.id, result: {} });
      return;
    }
    if (m.method === 'ui/resource-teardown' && m.id != null) {
      send({ jsonrpc: '2.0', id: m.id, result: {} });
      return;
    }
    if (m.method === 'ui/notifications/host-context-changed') {
      applyHostContext(m.params || {});
      return;
    }
    // Response to our own outbound requests (ui/initialize, ui/request-display-mode)
    if (m.id != null && pending.has(m.id)) {
      const cb = pending.get(m.id);
      pending.delete(m.id);
      cb(m.result, m.error);
      return;
    }
    // ui/initialize response carries hostContext with the supported modes.
    if (m.id === initId && m.result) {
      applyHostContext(m.result.hostContext || {});
    }
  });

  // Fire-and-acknowledge ui/initialize. We don't wait for a response —
  // the host flips visibility on the notification below, not the reply.
  pending.set(initId, (result) => applyHostContext((result && result.hostContext) || {}));
  send({
    jsonrpc: '2.0',
    id: initId,
    method: 'ui/initialize',
    params: {
      protocolVersion: '2025-06-18',
      appCapabilities: { availableDisplayModes: ['inline', 'fullscreen'] },
      clientInfo: { name: 'shiny-mcp-iframe', version: '0.1.0' },
    },
  });
  send({ jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {} });

  // Floating maximize button. Positioned bottom-left to stay clear of the
  // Shiny bus widget (top-right). Only inserted into the DOM once and only
  // becomes visible after the host confirms fullscreen support.
  const mountMaximizeBtn = () => {
    if (document.getElementById('mcp-mode-toggle')) return;
    const btn = document.createElement('button');
    btn.id = 'mcp-mode-toggle';
    btn.type = 'button';
    btn.textContent = '↗ maximize';
    btn.style.cssText = [
      'position:fixed', 'bottom:12px', 'left:12px', 'z-index:99999',
      'background:#15161b', 'color:#e6e6ea',
      'border:1px solid #2a5a3a', 'border-radius:6px',
      'padding:6px 12px', 'cursor:pointer',
      "font-family:'JetBrains Mono',ui-monospace,monospace",
      'font-size:11px', 'letter-spacing:.08em',
      'box-shadow:0 4px 12px rgba(0,0,0,0.4)',
      'display:none',
    ].join(';');
    btn.addEventListener('click', async () => {
      const target = btn.dataset.target || (hostDisplayMode === 'fullscreen' ? 'inline' : 'fullscreen');
      btn.disabled = true;
      const id = nextId++;
      const result = await new Promise((resolve) => {
        pending.set(id, (res) => resolve(res));
        send({ jsonrpc: '2.0', id, method: 'ui/request-display-mode', params: { mode: target } });
        // Don't block forever if the host silently drops the request.
        setTimeout(() => { if (pending.has(id)) { pending.delete(id); resolve(null); } }, 4000);
      });
      if (result && typeof result.mode === 'string') {
        hostDisplayMode = result.mode;
        updateMaxButton();
      }
      btn.disabled = false;
    });
    document.body.appendChild(btn);
    updateMaxButton();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountMaximizeBtn, { once: true });
  } else {
    mountMaximizeBtn();
  }

  // Report content size whenever the document grows or shrinks. The
  // host uses this to size our iframe slot. ResizeObserver fires on
  // every layout-affecting change, so we don't need polling.
  const reportSize = () => {
    const h = Math.max(
      document.documentElement.scrollHeight,
      document.body ? document.body.scrollHeight : 0,
      200,
    );
    send({
      jsonrpc: '2.0',
      method: 'ui/notifications/size-changed',
      params: {
        height: h,
        width: document.documentElement.clientWidth || window.innerWidth,
      },
    });
  };

  const wireResizeObserver = () => {
    reportSize();
    if (typeof ResizeObserver === 'function') {
      const ro = new ResizeObserver(reportSize);
      ro.observe(document.documentElement);
      if (document.body) ro.observe(document.body);
    } else {
      window.addEventListener('resize', reportSize);
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireResizeObserver, { once: true });
  } else {
    wireResizeObserver();
  }
})();
