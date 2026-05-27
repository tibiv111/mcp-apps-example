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
    }
    if (m.method === 'ui/resource-teardown' && m.id != null) {
      send({ jsonrpc: '2.0', id: m.id, result: {} });
    }
  });

  // Fire-and-acknowledge ui/initialize. We don't wait for a response —
  // the host flips visibility on the notification below, not the reply.
  send({
    jsonrpc: '2.0',
    id: Math.floor(Math.random() * 1e9),
    method: 'ui/initialize',
    params: {
      protocolVersion: '2025-06-18',
      appCapabilities: { availableDisplayModes: ['inline'] },
      clientInfo: { name: 'shiny-mcp-iframe', version: '0.1.0' },
    },
  });
  send({ jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {} });

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
