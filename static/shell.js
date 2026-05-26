(function(){
  // BASE_URL is set by the page before this script loads.
  const BASE_URL = window.NAV_AI_BASE_URL;
  const VIEWS = ['launcher','dashboard','form','forecast','catalog'];

  // ── /diagnostics tap ──
  // Cheap fire-and-forget that drops a marker on the trace bus so the
  // diagnostics page sees iframe-side events alongside server traffic.
  function diagNote(kind, summary, detail, correlationId){
    try {
      fetch(BASE_URL + '/diagnostics/note', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        keepalive: true,
        body: JSON.stringify({
          kind: kind,
          summary: summary,
          detail: detail || {},
          correlation_id: correlationId || null
        })
      });
    } catch(_){}
  }

  // Hold onto the most recent submissions so the Discuss buttons can pass
  // them back through the tool boundary.
  const lastSelection = { forecast: null, pricing: null, catalog: null };

  // ── view router ──
  window.show = function(name){
    VIEWS.forEach(v => {
      const el = document.getElementById('view-'+v);
      if (!el) return;
      if (v === name) { el.classList.remove('hidden'); el.style.animation='none'; void el.offsetWidth; el.style.animation=''; }
      else el.classList.add('hidden');
    });
  };

  // ── JSON-RPC postMessage client (with direct-HTTP fallback for /ui/shell preview) ──
  let nextId = 1;
  const pending = new Map();
  // If no parent responds within HOST_PROBE_MS, we assume we're in the preview
  // (a regular browser tab, no MCP host listening) and fall back to direct HTTP.
  const HOST_PROBE_MS = 800;
  let hostMode = 'unknown'; // 'host' | 'direct' | 'unknown'

  function postRpc(payload){
    try { window.parent.postMessage(payload, '*'); } catch(e) {}
  }

  async function directCall(method, params){
    const r = await fetch(BASE_URL + '/mcp', {
      method: 'POST',
      headers: {'Content-Type':'application/json', 'Accept':'application/json'},
      body: JSON.stringify({jsonrpc:'2.0', id: nextId++, method, params: params||{}})
    });
    const json = await r.json();
    if (json.error) throw new Error(json.error.message || 'rpc error');
    return json.result;
  }

  function sendRequest(method, params){
    if (hostMode === 'direct') return directCall(method, params);
    return new Promise((resolve, reject) => {
      const id = nextId++;
      let settled = false;
      pending.set(id, {
        resolve: (v) => { settled = true; resolve(v); },
        reject:  (e) => { settled = true; reject(e); }
      });
      postRpc({jsonrpc:'2.0', id, method, params: params||{}});
      // Fallback to direct HTTP only for tools/* and resources/* — never for
      // ui/* methods, which are host-only and meaningless to our /mcp endpoint.
      // Also: only fall back if no message at all has arrived from the parent.
      if (hostMode === 'unknown' && !method.startsWith('ui/')) {
        setTimeout(() => {
          if (!settled && pending.has(id) && hostMode === 'unknown') {
            pending.delete(id);
            hostMode = 'direct';
            console.debug('[NAV AI] no MCP host detected, falling back to direct HTTP /mcp');
            directCall(method, params).then(resolve, reject);
          }
        }, HOST_PROBE_MS);
      }
    });
  }
  function sendNotification(method, params){
    if (hostMode === 'direct') return; // host-only notifications
    postRpc({jsonrpc:'2.0', method, params: params||{}});
  }
  // Per SEP-1865, the host sends notifications (no id, has 'method') such as
  // ui/notifications/tool-input, ui/notifications/tool-result, and
  // ui/notifications/host-context-changed. Previous versions dropped these.
  function applyHostContext(ctx) {
    if (!ctx || typeof ctx !== 'object') return;
    var vars = ctx.styles && ctx.styles.variables;
    if (vars && typeof vars === 'object') {
      Object.entries(vars).forEach(function(kv){
        // The shell is intentionally always-dark; skip any host-provided
        // color tokens so Claude's light theme can't bleed in. Fonts,
        // spacing, and other non-color vars still flow through.
        if (typeof kv[0] === 'string' && kv[0].indexOf('--color-') === 0) return;
        if (kv[1] != null) document.documentElement.style.setProperty(kv[0], kv[1]);
      });
    }
    var fonts = ctx.styles && ctx.styles.css && ctx.styles.css.fonts;
    if (typeof fonts === 'string' && fonts.length) {
      var s = document.createElement('style');
      s.textContent = fonts;
      document.head.appendChild(s);
    }
  }

  window.addEventListener('message', (ev) => {
    const msg = ev.data;
    // ANY message from the parent — including Claude's non-JSON-RPC auth/session
    // probes — proves there's a host. Flip out of unknown mode immediately so
    // we don't fall back to direct HTTP and break the handshake. See
    // https://github.com/anthropics/claude-ai-mcp/issues/47 for context.
    if (msg && typeof msg === 'object') {
      if (hostMode !== 'direct') hostMode = 'host';
    }
    if (!msg || msg.jsonrpc !== '2.0') {
      // Log non-protocol traffic for debugging; do not respond.
      try { console.debug('[NAV AI] non-JSON-RPC msg from parent:', msg); } catch(e){}
      return;
    }

    // Notifications & requests from host (have 'method', no 'result'/'error').
    if (typeof msg.method === 'string') {
      try { console.debug('[NAV AI] host →', msg.method, msg.params); } catch(e){}
      switch (msg.method) {
        case 'ui/notifications/tool-input':
        case 'ui/notifications/tool-input-partial':
          // The host is delivering the original tool args. We don't need them
          // for launch_nav_ai (no args) but acknowledging the message keeps
          // the handshake healthy for hosts that wait for activity.
          break;
        case 'ui/notifications/tool-result':
          // Final tool result. For launch_nav_ai this is informational only —
          // the launcher view is already rendered. Ignore safely.
          break;
        case 'ui/notifications/host-context-changed':
          applyHostContext(msg.params || {});
          break;
        case 'ui/resource-teardown':
          // Host is tearing us down; ack with a result so it can proceed.
          if (msg.id != null) {
            window.parent.postMessage({jsonrpc:'2.0', id: msg.id, result: {}}, '*');
          }
          break;
        case 'ping':
          // Respond to ping requests if they carry an id.
          if (msg.id != null) {
            window.parent.postMessage({jsonrpc:'2.0', id: msg.id, result: {}}, '*');
          }
          break;
        default:
          // Unknown notification — ignore silently.
          break;
      }
      return;
    }

    // Responses to our outgoing requests (have 'result' or 'error').
    try { console.debug('[NAV AI] host ← response id=' + msg.id, msg.result || msg.error); } catch(e){}
    if (msg.id != null && pending.has(msg.id)) {
      const {resolve, reject} = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(msg.error); else resolve(msg.result);
    }
  });

  // ── ui/initialize handshake + size reporting ──
  function reportSize() {
    const h = Math.max(
      document.documentElement.scrollHeight,
      document.body ? document.body.scrollHeight : 0,
      300
    );
    sendNotification('ui/notifications/size-changed', {
      height: h,
      width: document.documentElement.clientWidth || window.innerWidth,
    });
  }

  (async () => {
    let initFired = false;
    function fireInitialized() {
      if (initFired) return;
      initFired = true;
      try { console.debug('[NAV AI] → ui/notifications/initialized'); } catch(e){}
      sendNotification('ui/notifications/initialized', {});
    }
    try {
      // Race the host's ui/initialize response against a 3s timeout. Some hosts
      // route the response through a sandbox proxy and the round-trip is slow
      // on cold starts; rather than block forever (and never let the host flip
      // visibility), we fire 'initialized' regardless and apply hostContext
      // when/if the response arrives later.
      const initPromise = sendRequest('ui/initialize', {
        protocolVersion: '2025-06-18',
        appCapabilities: {
          availableDisplayModes: ['inline'],
        },
        clientInfo: { name: 'nav-ai-shell', version: '0.1.0' },
      });
      const timeoutPromise = new Promise(function(_, rej){
        setTimeout(function(){ rej(new Error('ui/initialize response timeout')); }, 3000);
      });
      try {
        const result = await Promise.race([initPromise, timeoutPromise]);
        applyHostContext(result && result.hostContext);
      } catch (raceErr) {
        try { console.debug('[NAV AI] ui/initialize did not resolve in time:', raceErr.message); } catch(e){}
        // Still apply context if the real promise resolves later.
        initPromise.then(function(r){ applyHostContext(r && r.hostContext); }).catch(function(){});
      }
      fireInitialized();
    } catch (e) {
      try { console.debug('[NAV AI] ui/initialize threw:', e); } catch(_){}
      // Even on error, if we're in host mode (parent posted something), fire
      // initialized so the host can flip visibility on its end.
      if (hostMode === 'host') fireInitialized();
    }
    // Report initial size, then keep reporting on any DOM growth/shrink.
    reportSize();
    try {
      const ro = new ResizeObserver(() => reportSize());
      if (document.body) ro.observe(document.body);
    } catch(e) { /* older browser fallback below */ }
    window.addEventListener('resize', reportSize);
    // Re-measure after layout settles, animations finish, etc.
    setTimeout(reportSize, 100);
    setTimeout(reportSize, 500);
    setTimeout(reportSize, 1500);
  })();

  // Re-measure after every view switch.
  const _show = window.show;
  window.show = function(name){
    _show(name);
    setTimeout(reportSize, 50);
    setTimeout(reportSize, 350);
  };

  // ── Pricing form ──
  window.submitPricing = async function(){
    const btn = document.getElementById('submit-btn');
    const prodLabel = document.getElementById('prod').value;
    const product = prodLabel.split(' · ')[0];
    const new_price = parseFloat(document.getElementById('price').value);
    btn.disabled = true; btn.textContent = 'Submitting…';
    try {
      const res = await sendRequest('tools/call', {
        name: 'submit_pricing_change',
        arguments: { product, new_price }
      });
      const data = (res && res.structuredContent) || {};
      lastSelection.pricing = data;
      const box = document.getElementById('receipt');
      document.getElementById('r-ticket').textContent  = data.ticket || '—';
      document.getElementById('r-product').textContent = data.product || product;
      document.getElementById('r-price').textContent   = '$' + Number(data.new_price || new_price).toFixed(2);
      document.getElementById('r-status').textContent  = (data.status || 'submitted').replace(/_/g,' ');
      document.getElementById('r-time').textContent    = new Date((data.submitted_at||Date.now()/1000)*1000).toISOString().slice(11,19) + ' UTC';
      box.classList.remove('hidden');
    } catch (e) {
      alert('Submit failed: ' + (e && e.message || e));
    } finally {
      btn.disabled = false; btn.textContent = 'Submit for review';
    }
  };

  // ── Forecast + SSE ──
  let currentSource = null;
  function setProgress(pct, label){
    document.getElementById('bar').style.width = (pct||0) + '%';
    document.getElementById('pct').textContent = (pct||0) + '%';
    if (label) document.getElementById('step-label').textContent = label;
  }
  function markStep(active){
    const rows = document.querySelectorAll('.step-row');
    let passed = true;
    rows.forEach(r => {
      r.classList.remove('active','done');
      if (r.dataset.step === active) { r.classList.add('active'); passed = false; }
      else if (passed) { r.classList.add('done'); }
    });
  }
  window.startForecast = async function(){
    if (currentSource) { try { currentSource.close(); } catch(e){} currentSource = null; }
    const btn = document.getElementById('start-btn');
    const region = document.getElementById('region').value;
    btn.disabled = true; btn.textContent = 'Starting…';
    document.getElementById('forecast-result').classList.add('hidden');
    const shell = document.getElementById('progress-shell');
    shell.classList.remove('hidden');
    shell.classList.add('running');
    setProgress(0, 'queued');
    document.querySelectorAll('.step-row').forEach(r => r.classList.remove('active','done'));

    try {
      const res = await sendRequest('tools/call', {
        name: 'start_forecast',
        arguments: { region }
      });
      const job_id = res && res.structuredContent && res.structuredContent.job_id;
      if (!job_id) throw new Error('no job_id returned');
      document.getElementById('fr-job').textContent = job_id;

      const url = BASE_URL + '/jobs/' + encodeURIComponent(job_id) + '/events';
      const src = new EventSource(url);
      currentSource = src;
      const handle = (ev, type) => {
        let payload = {};
        try { payload = JSON.parse(ev.data); } catch(e){}
        if (type === 'progress' || type === 'snapshot') {
          setProgress(payload.progress, payload.step_label || payload.step);
          if (payload.step) markStep(payload.step);
        } else if (type === 'done') {
          setProgress(100, 'complete');
          markStep('finalizing');
          document.querySelectorAll('.step-row').forEach(r => { r.classList.remove('active'); r.classList.add('done'); });
          shell.classList.remove('running');
          const r = payload.result || {};
          lastSelection.forecast = Object.assign({ job_id: payload.job_id }, r);
          document.getElementById('fr-region').textContent     = r.region || region;
          document.getElementById('fr-horizon').textContent    = (r.horizon_weeks || 12) + ' wk';
          document.getElementById('fr-baseline').textContent   = (r.baseline_units || 0).toLocaleString() + ' u';
          document.getElementById('fr-uplift').textContent     = (r.uplift_pct != null ? '+'+r.uplift_pct+'%' : '—');
          document.getElementById('fr-confidence').textContent = r.confidence != null ? (r.confidence*100).toFixed(1)+'%' : '—';
          document.getElementById('forecast-result').classList.remove('hidden');
          src.close(); currentSource = null;
          btn.disabled = false; btn.textContent = 'Start forecast';
        } else if (type === 'error') {
          shell.classList.remove('running');
          alert('Forecast failed: ' + (payload.error || 'unknown'));
          src.close(); currentSource = null;
          btn.disabled = false; btn.textContent = 'Start forecast';
        }
      };
      src.addEventListener('snapshot', e => handle(e, 'snapshot'));
      src.addEventListener('progress', e => handle(e, 'progress'));
      src.addEventListener('done',     e => handle(e, 'done'));
      src.addEventListener('error',    e => { /* network blip; EventSource auto-retries */ });
    } catch (e) {
      shell.classList.remove('running');
      alert('Start failed: ' + (e && e.message || e));
      btn.disabled = false; btn.textContent = 'Start forecast';
    }
  };

  // ── Catalog (calls backend MCP via the frontend's lookup_product tool) ──
  window.lookupProduct = async function(){
    const btn = document.getElementById('lookup-btn');
    const sku = document.getElementById('sku').value.trim().toUpperCase();
    const ok  = document.getElementById('catalog-receipt');
    const err = document.getElementById('catalog-error');
    ok.classList.add('hidden');
    err.classList.add('hidden');
    btn.disabled = true; btn.textContent = 'Looking up…';
    try {
      const res = await sendRequest('tools/call', {
        name: 'lookup_product',
        arguments: { sku }
      });
      const data = (res && res.structuredContent) || {};
      if (res && res.isError || data.found === false) {
        const text = (res && res.content && res.content[0] && res.content[0].text) || 'lookup failed';
        document.getElementById('c-error').textContent = text;
        err.classList.remove('hidden');
        return;
      }
      lastSelection.catalog = data;
      document.getElementById('c-sku').textContent      = data.sku || sku;
      document.getElementById('c-name').textContent     = data.name || '—';
      document.getElementById('c-price').textContent    = data.price != null ? Number(data.price).toFixed(2) : '—';
      document.getElementById('c-currency').textContent = data.currency || '—';
      document.getElementById('c-stock').textContent    = data.in_stock === true ? 'yes' : data.in_stock === false ? 'no' : '—';
      document.getElementById('c-updated').textContent  = data.last_updated || '—';
      document.getElementById('c-source').textContent   = data.source || 'unknown';
      ok.classList.remove('hidden');
    } catch (e) {
      document.getElementById('c-error').textContent = (e && e.message) || String(e);
      err.classList.remove('hidden');
    } finally {
      btn.disabled = false; btn.textContent = 'Look up via backend';
    }
  };

  // ── Bidirectional: ask the host model to comment on a selection ──
  // The discuss_selection tool returns text addressed *to* the model. Claude
  // sees the tool result and answers in the chat thread without the user
  // having typed anything. We trace both sides so /diagnostics shows the
  // iframe-initiated path clearly.
  async function callDiscuss(kind, context, buttonId){
    const btn = document.getElementById(buttonId);
    const original = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = 'Sending to assistant…'; }
    const corr = 'discuss-' + Date.now().toString(36);
    diagNote('ui.discuss', 'iframe → host: discuss ' + kind, { kind, context }, corr);
    try {
      await sendRequest('tools/call', {
        name: 'discuss_selection',
        arguments: { kind, context }
      });
      diagNote('ui.discuss.sent', 'tool result delivered — host should respond in chat', { kind }, corr);
      if (btn) {
        btn.textContent = '✓ sent to chat';
        setTimeout(() => { btn.disabled = false; if (original) btn.textContent = original; }, 1800);
      }
    } catch (e) {
      diagNote('ui.discuss.error', String(e && e.message || e), { kind }, corr);
      if (btn) { btn.disabled = false; btn.textContent = original || 'Discuss with Claude'; }
      alert('Could not reach the assistant: ' + (e && e.message || e));
    }
  }
  window.discussForecast = function(){
    if (!lastSelection.forecast) { alert('Run a forecast first.'); return; }
    callDiscuss('forecast', lastSelection.forecast, 'discuss-forecast');
  };
  window.discussPricing = function(){
    if (!lastSelection.pricing) { alert('Submit a pricing change first.'); return; }
    callDiscuss('pricing', lastSelection.pricing, 'discuss-pricing');
  };
  window.discussCatalog = function(){
    if (!lastSelection.catalog) { alert('Look up a product first.'); return; }
    callDiscuss('catalog', lastSelection.catalog, 'discuss-catalog');
  };

  // ── Server-pushed shell updates (banner + revision) ──
  // The MCP-level path is notifications/resources/updated, which Claude's
  // host re-reads. This direct iframe channel is the always-works copy:
  // the server pushes the same fact down /shell/events and the banner
  // appears immediately for demo viewers.
  function applyShellUpdate(payload){
    const banner = payload && payload.banner;
    const slot = document.getElementById('ops-banner');
    const txt = document.getElementById('ops-text');
    if (slot && txt) {
      slot.classList.remove('visible','tone-info','tone-warn','tone-alert');
      if (banner && banner.text) {
        txt.textContent = banner.text;
        slot.classList.add('visible', 'tone-' + (banner.tone || 'info'));
      } else {
        txt.textContent = '';
      }
    }
    const rev = document.getElementById('shell-rev');
    if (rev && payload && payload.revision != null) {
      rev.textContent = 'rev ' + payload.revision;
      rev.classList.add('bump');
      setTimeout(() => rev.classList.remove('bump'), 1200);
    }
    // Tap so /diagnostics shows where the iframe handled it.
    diagNote('ui.shell-update', 'iframe applied shell update rev ' + (payload && payload.revision), payload || {});
    // Resize after the banner toggles.
    setTimeout(() => { try { reportSize(); } catch(_){} }, 50);
  }

  try {
    const shellSrc = new EventSource(BASE_URL + '/shell/events');
    shellSrc.addEventListener('snapshot', (e) => {
      try { applyShellUpdate(JSON.parse(e.data)); } catch(_){}
    });
    shellSrc.addEventListener('shell-update', (e) => {
      try { applyShellUpdate(JSON.parse(e.data)); } catch(_){}
    });
    shellSrc.addEventListener('error', () => { /* auto-retry */ });
  } catch (e) {
    console.debug('[NAV AI] /shell/events not available:', e);
  }
})();
