(function(){
  // BASE_URL is set by the page before this script loads.
  const BASE_URL = window.NAV_AI_BASE_URL;
  const VIEWS = ['launcher','dashboard','form','forecast','catalog','shiny','shiny-history','delegation'];

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
  // Track the host's current display mode and which modes it advertises
  // support for. Updated from ui/initialize result and from
  // ui/notifications/host-context-changed. Drives the maximize button label
  // and visibility — we only show the button if the host advertises
  // 'fullscreen' (otherwise the request would just be rejected).
  var hostDisplayMode = 'inline';
  var hostAvailableModes = ['inline'];

  function updateMaxButton() {
    var btn = document.getElementById('display-mode-toggle');
    if (!btn) return;
    var canFullscreen = hostAvailableModes.indexOf('fullscreen') !== -1;
    btn.style.display = canFullscreen ? '' : 'none';
    if (hostDisplayMode === 'fullscreen') {
      btn.textContent = '↙ restore';
      btn.dataset.target = 'inline';
      btn.title = 'Return to inline view';
    } else {
      btn.textContent = '↗ maximize';
      btn.dataset.target = 'fullscreen';
      btn.title = 'Expand to fullscreen';
    }
  }

  function applyHostContext(ctx) {
    if (!ctx || typeof ctx !== 'object') return;
    if (typeof ctx.displayMode === 'string') {
      hostDisplayMode = ctx.displayMode;
    }
    if (Array.isArray(ctx.availableDisplayModes)) {
      hostAvailableModes = ctx.availableDisplayModes;
    }
    updateMaxButton();
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

  // User-clickable toggle. Sends ui/request-display-mode per SEP-1865; the
  // host MAY decline, so we only commit to the new mode when the host's
  // host-context-changed notification confirms it.
  window.toggleDisplayMode = async function() {
    var btn = document.getElementById('display-mode-toggle');
    var target = (btn && btn.dataset.target) || (hostDisplayMode === 'fullscreen' ? 'inline' : 'fullscreen');
    if (btn) { btn.disabled = true; }
    try {
      var res = await sendRequest('ui/request-display-mode', { mode: target });
      if (res && typeof res.mode === 'string') {
        hostDisplayMode = res.mode;
        updateMaxButton();
        setTimeout(reportSize, 50);
      }
    } catch (e) {
      try { console.debug('[NAV AI] display mode request rejected:', e); } catch(_){}
    } finally {
      if (btn) { btn.disabled = false; }
    }
  };

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
          availableDisplayModes: ['inline', 'fullscreen'],
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
          // Layer 1: baseline shift from approved drifts (price moves in effect).
          const baseWrap = document.getElementById('fr-baseline-wrap');
          const driftsList = document.getElementById('fr-drifts');
          const drifts = Array.isArray(r.considered_price_drifts) ? r.considered_price_drifts : [];
          driftsList.innerHTML = '';
          if (drifts.length) {
            const shift = r.baseline_shift_pct != null ? r.baseline_shift_pct : 0;
            const shiftEl = document.getElementById('fr-baseline-shift');
            shiftEl.textContent = (shift > 0 ? '+' : '') + shift.toFixed(2) + '% baseline';
            shiftEl.className = 'forecast-pricing-drag ' + (shift < 0 ? 'down' : 'up');
            drifts.forEach(d => {
              const row = document.createElement('div');
              row.className = 'considered-row';
              const dpct = d.drift_pct != null ? (d.drift_pct > 0 ? '+' : '') + d.drift_pct + '%' : '—';
              row.innerHTML =
                '<span class="considered-ticket">approved</span>' +
                '<span class="considered-sku">' + (d.sku || '—') + '</span>' +
                '<span class="considered-delta ' + (d.drift_pct > 0 ? 'up' : 'down') + '">' + dpct + '</span>' +
                '<span class="considered-drag">' + Number(d.seed_price || 0).toFixed(2) + ' → ' + Number(d.current_price || 0).toFixed(2) + '</span>';
              driftsList.appendChild(row);
            });
            baseWrap.classList.remove('hidden');
          } else {
            baseWrap.classList.add('hidden');
          }

          // Layer 2: uplift drag from pending changes (future, uncertain).
          const wrap = document.getElementById('fr-pricing-wrap');
          const considered = Array.isArray(r.considered_pricing_changes) ? r.considered_pricing_changes : [];
          const list = document.getElementById('fr-considered');
          list.innerHTML = '';
          if (considered.length) {
            const drag = r.pricing_drag_pct != null ? r.pricing_drag_pct : 0;
            const dragEl = document.getElementById('fr-drag');
            dragEl.textContent = (drag > 0 ? '−' : '+') + Math.abs(drag).toFixed(2) + ' pp uplift';
            dragEl.className = 'forecast-pricing-drag ' + (drag > 0 ? 'down' : 'up');
            considered.forEach(c => {
              const row = document.createElement('div');
              row.className = 'considered-row';
              const dpct = c.delta_pct != null ? (c.delta_pct > 0 ? '+' : '') + c.delta_pct + '%' : '—';
              row.innerHTML =
                '<span class="considered-ticket">' + (c.ticket || '—') + '</span>' +
                '<span class="considered-sku">' + (c.product || '—') + '</span>' +
                '<span class="considered-delta ' + (c.delta_pct > 0 ? 'up' : 'down') + '">' + dpct + '</span>' +
                '<span class="considered-drag">→ ' + (c.uplift_drag_pct != null ? (c.uplift_drag_pct > 0 ? '−' : '+') + Math.abs(c.uplift_drag_pct).toFixed(2) + ' pp' : '—') + '</span>';
              list.appendChild(row);
            });
            wrap.classList.remove('hidden');
          } else {
            wrap.classList.add('hidden');
          }
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

  // ── Dashboard (live snapshot from /dashboard/snapshot) ──
  let _dashboardActive = false;
  function _fmtTime(epoch){
    if (!epoch) return '—';
    const d = new Date(epoch * 1000);
    const hh = String(d.getHours()).padStart(2,'0');
    const mm = String(d.getMinutes()).padStart(2,'0');
    const ss = String(d.getSeconds()).padStart(2,'0');
    return hh + ':' + mm + ':' + ss;
  }
  async function fetchDashboardSnapshot(){
    try {
      const r = await fetch(BASE_URL + '/dashboard/snapshot', {cache:'no-store'});
      if (!r.ok) return null;
      return await r.json();
    } catch(_) { return null; }
  }
  function renderDashboard(snap){
    if (!snap) return;
    document.getElementById('dash-products').textContent = snap.products;
    document.getElementById('dash-stock').textContent =
      snap.in_stock + ' in stock · ' + snap.out_of_stock + ' out';
    document.getElementById('dash-stock').className = 'delta ' + (snap.out_of_stock > 0 ? 'down' : 'up');
    document.getElementById('dash-pending').textContent = snap.pending_pricing_changes;
    document.getElementById('dash-pending-delta').textContent =
      snap.pending_pricing_changes > 0 ? 'awaiting review' : 'queue clear';
    document.getElementById('dash-pending-delta').className =
      'delta ' + (snap.pending_pricing_changes > 0 ? 'up' : 'down');
    document.getElementById('dash-jobs').textContent = snap.jobs_total;
    document.getElementById('dash-jobs-running').textContent =
      snap.jobs_running + ' running · ' + snap.jobs_done + ' done';
    document.getElementById('dash-jobs-running').className =
      'delta ' + (snap.jobs_running > 0 ? 'up' : 'down');
    document.getElementById('dash-now').textContent = _fmtTime(snap.now);

    const pending = snap.recent_pending || [];
    const pf = document.getElementById('dash-pending-feed');
    pf.innerHTML = '';
    if (pending.length){
      pending.forEach(p => {
        const tr = document.createElement('tr');
        const dpct = p.delta_pct != null ? (p.delta_pct > 0 ? '+' : '') + p.delta_pct + '%' : '';
        tr.innerHTML =
          '<td>' + _fmtTime(p.submitted_at) + '</td>' +
          '<td>' + (p.product || '—') + ' · ' +
            Number(p.previous_price || 0).toFixed(2) + ' → ' +
            Number(p.new_price || 0).toFixed(2) +
            ' <span style="color:var(--text-3)">' + dpct + '</span></td>' +
          '<td>' + (p.ticket || '—') + ' · ' + (p.status || 'pending').replace(/_/g,' ') + '</td>';
        pf.appendChild(tr);
      });
    } else {
      pf.innerHTML = '<tr><td colspan="3" style="color:var(--text-3); font-style: italic;">No pending pricing changes yet. Submit one from the pricing view.</td></tr>';
    }
    document.getElementById('dash-pending-meta').textContent = pending.length + ' shown';

    const jobs = snap.recent_jobs || [];
    const jf = document.getElementById('dash-jobs-feed');
    jf.innerHTML = '';
    if (jobs.length){
      jobs.forEach(j => {
        const tr = document.createElement('tr');
        const note = j.pending_pricing_count > 0
          ? ' · factoring ' + j.pending_pricing_count + ' price chg'
          : '';
        tr.innerHTML =
          '<td>' + _fmtTime(j.started_at) + '</td>' +
          '<td>Forecast · ' + (j.region || '—') + note + '</td>' +
          '<td>' + j.id + ' · ' + (j.status || '—') +
            (j.status === 'running' ? ' (' + (j.progress || 0) + '%)' : '') + '</td>';
        jf.appendChild(tr);
      });
    } else {
      jf.innerHTML = '<tr><td colspan="3" style="color:var(--text-3); font-style: italic;">No forecast jobs yet. Start one from the forecast view.</td></tr>';
    }
    document.getElementById('dash-jobs-meta').textContent = jobs.length + ' shown';
  }
  // Public — called from the SSE pricing-event handler. Only does work
  // when the dashboard view is on screen.
  window.refreshDashboardIfShowing = async function(){
    if (!_dashboardActive) return;
    const snap = await fetchDashboardSnapshot();
    if (snap) renderDashboard(snap);
  };
  // Pull a fresh snapshot every time the dashboard view becomes active.
  const _showBase = window.show;
  window.show = function(name){
    _showBase(name);
    _dashboardActive = (name === 'dashboard');
    if (_dashboardActive) {
      fetchDashboardSnapshot().then(s => { if (s) renderDashboard(s); });
    }
  };

  // ── Card E: live launch_shiny_embedded call (inline-HTML MCP resource) ──
  // The server-side embed path. Tool returns a text/html;profile=mcp-app
  // body with Shiny's HTML rewritten + WebSocket shim injected. Same
  // MIME as the NAV AI shell, so today's Claude actually renders it.
  window.launchShinyEmbedded = async function(){
    const btn   = document.getElementById('launch-shiny-embedded-btn');
    const box   = document.getElementById('launch-shiny-embedded-result');
    const frame = document.getElementById('shiny-embed-iframe');
    if (!btn || !box) return;
    btn.disabled = true; btn.textContent = 'Calling launch_shiny_embedded…';
    box.classList.add('hidden');
    try {
      // Spec-correct MCP wire path: tool call + resources/read. Today's
      // Claude fetches the result but doesn't mount a second iframe for
      // it (see Card E commentary in shiny/README.md), so the visible
      // demo comes from srcdoc'ing the same HTML below.
      await sendRequest('tools/call', { name: 'launch_shiny_embedded', arguments: {} });
      const resRead = await sendRequest('resources/read', { uri: 'ui://nav-ai/shiny-embedded' });
      const first = ((resRead && resRead.contents) || [])[0] || {};
      const html  = first.text || '';

      // The pragmatic in-shell render. srcdoc isn't a navigation, so the
      // host's `frame-src 'self'` doesn't gate it (the same CSP that
      // blocks `<iframe src=…>` pointing at our origin). Inside the
      // srcdoc document, Shiny's rewritten asset URLs + the WS shim
      // point at our /shiny-proxy/, which the iframe's connect-src will
      // need to allow.
      if (frame && html) frame.srcdoc = html;

      document.getElementById('lse-uri').textContent  = first.uri || '—';
      document.getElementById('lse-mime').textContent = first.mimeType || '—';
      document.getElementById('lse-len').textContent  = html.length.toLocaleString() + ' chars';
      document.getElementById('lse-action').textContent =
        'MCP wire call sent (see /diagnostics). In-shell render mounted via srcdoc below.';
      box.classList.remove('hidden');
      btn.textContent = '✓ embedded';
      setTimeout(() => { btn.disabled = false; btn.textContent = 'Call launch_shiny_embedded ↗'; }, 2400);
    } catch (e) {
      document.getElementById('lse-uri').textContent  = '—';
      document.getElementById('lse-mime').textContent = '—';
      document.getElementById('lse-len').textContent  = '—';
      document.getElementById('lse-action').textContent = (e && e.message) || String(e);
      box.classList.remove('hidden');
      btn.disabled = false; btn.textContent = 'Call launch_shiny_embedded ↗';
    }
  };

  // ── Card D: live launch_shiny call (URL-form MCP resource) ──
  // Calls tools/call launch_shiny, then fetches the referenced resource
  // via resources/read so we can show the URL-form payload + see whether
  // the host opened its own iframe for it. Inspect the trace at
  // /diagnostics to compare the host's behavior across builds.
  window.launchShiny = async function(){
    const btn = document.getElementById('launch-shiny-btn');
    const box = document.getElementById('launch-shiny-result');
    if (!btn || !box) return;
    btn.disabled = true; btn.textContent = 'Calling launch_shiny…';
    box.classList.add('hidden');
    try {
      // The tools/call itself is fire-and-acknowledge — the meaningful
      // payload (URL + _meta) lives on the referenced resource.
      await sendRequest('tools/call', { name: 'launch_shiny', arguments: {} });
      const resRead = await sendRequest('resources/read', { uri: 'ui://nav-ai/shiny' });
      const contents = (resRead && resRead.contents) || [];
      const first = contents[0] || {};
      const meta = (first._meta && first._meta.ui) || {};
      document.getElementById('ls-uri').textContent  = first.uri || '—';
      document.getElementById('ls-mime').textContent = first.mimeType || '—';
      document.getElementById('ls-url').textContent  = meta.externalUrl || '—';
      document.getElementById('ls-body').textContent = (first.text || '').trim() || '—';
      // We can't directly detect whether the host opened a new iframe for
      // the resource — but if it did, you'll see another mcp.request hit
      // /diagnostics. Surface a hint so the demo has something visible
      // even on hosts that ignore URL resources.
      document.getElementById('ls-action').textContent =
        'check /diagnostics for a follow-up resources/read or iframe load — see if the host honored the URL form';
      box.classList.remove('hidden');
      btn.textContent = '✓ called launch_shiny';
      setTimeout(() => { btn.disabled = false; btn.textContent = 'Call launch_shiny ↗'; }, 2400);
    } catch (e) {
      document.getElementById('ls-uri').textContent  = '—';
      document.getElementById('ls-mime').textContent = '—';
      document.getElementById('ls-url').textContent  = '—';
      document.getElementById('ls-body').textContent = (e && e.message) || String(e);
      document.getElementById('ls-action').textContent = 'tool call failed — check the message in body';
      box.classList.remove('hidden');
      btn.disabled = false; btn.textContent = 'Call launch_shiny ↗';
    }
  };

  // ── Catalog (calls backend MCP via the frontend's lookup_product tool) ──
  function renderCatalogReceipt(data){
    document.getElementById('c-sku').textContent      = data.sku || '—';
    document.getElementById('c-name').textContent     = data.name || '—';
    const price = (data.current_price != null ? data.current_price : data.price);
    document.getElementById('c-price').textContent    = price != null ? Number(price).toFixed(2) : '—';
    document.getElementById('c-currency').textContent = data.currency || '—';
    document.getElementById('c-stock').textContent    = data.in_stock === true ? 'yes' : data.in_stock === false ? 'no' : '—';
    document.getElementById('c-updated').textContent  = data.last_updated || '—';
    document.getElementById('c-source').textContent   = data.source || 'unknown';

    const wrap = document.getElementById('c-pending-wrap');
    const list = document.getElementById('c-pending-list');
    const pending = Array.isArray(data.pending_changes) ? data.pending_changes : [];
    list.innerHTML = '';
    if (!pending.length) {
      wrap.classList.add('hidden');
      return;
    }
    pending.slice().reverse().forEach(p => {
      const row = document.createElement('div');
      row.className = 'pending-row';
      const dpct = p.delta_pct != null ? (p.delta_pct > 0 ? '+' : '') + p.delta_pct + '%' : '—';
      const dir  = p.delta_pct != null && p.delta_pct > 0 ? 'up' : 'down';
      row.innerHTML =
        '<span class="pending-ticket">' + (p.ticket || '—') + '</span>' +
        '<span class="pending-prices">' +
          '<span class="prev">' + Number(p.previous_price || 0).toFixed(2) + '</span>' +
          '<span class="arrow">→</span>' +
          '<span class="next">' + Number(p.new_price || 0).toFixed(2) + '</span>' +
        '</span>' +
        '<span class="pending-delta ' + dir + '">' + dpct + '</span>' +
        '<span class="pending-status">' + (p.status || 'pending').replace(/_/g, ' ') + '</span>';
      list.appendChild(row);
    });
    wrap.classList.remove('hidden');
  }

  // Re-fetch the catalog row when the pricing book changes for the SKU
  // currently displayed. Called from the /shell/events handler below.
  async function refreshCatalogIfShowing(sku){
    if (!lastSelection.catalog) return;
    if ((lastSelection.catalog.sku || '').toUpperCase() !== (sku || '').toUpperCase()) return;
    try {
      const res = await sendRequest('tools/call', {
        name: 'lookup_product',
        arguments: { sku: lastSelection.catalog.sku }
      });
      const data = (res && res.structuredContent) || {};
      if (data && data.found !== false) {
        lastSelection.catalog = data;
        renderCatalogReceipt(data);
      }
    } catch (_) { /* best effort */ }
  }

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
      renderCatalogReceipt(data);
      ok.classList.remove('hidden');
    } catch (e) {
      document.getElementById('c-error').textContent = (e && e.message) || String(e);
      err.classList.remove('hidden');
    } finally {
      btn.disabled = false; btn.textContent = 'Look up via backend';
    }
  };

  // ── Send selection to chat via host ──
  // Spec-correct path (SEP-1865, 2026-01-26):
  //   1. ui/update-model-context  — push structured selection silently so
  //                                 it doesn't crowd the chat.
  //   2. ui/message               — visible user-role trigger so Claude
  //                                 replies inline.
  //
  // Caveat: Claude's MCP Apps host has a confirmed bug where step (1) is
  // acknowledged (returns success) but silently dropped before the model
  // sees it — specifically for iframes rendered live in response to a
  // launch_* tool call. Reported workarounds: page refresh, restored
  // history, or DevTools open during initial render. Tracked here:
  // https://community.openai.com/t/mcp-app-updatemodelcontext-silently-dropped-for-live-rendered-iframes-works-after-page-refresh/1379700
  //
  // Until that's fixed we ALSO include the formatted selection inline in
  // the ui/message body so today's demo works without depending on the
  // silent push. When the host bug is fixed, drop the `+ _formatSelection`
  // concatenation in callDiscuss for clean chat output.
  const HOST_METHOD_CANDIDATES = {
    sendMessage:        ['ui/message', 'ui/send-message', 'sendMessage', 'ui/sendMessage'],
    updateModelContext: ['ui/update-model-context', 'updateModelContext', 'ui/updateModelContext'],
  };
  // Cache the first method name that worked so subsequent calls go direct.
  const _hostMethodResolved = { sendMessage: null, updateModelContext: null };

  function _isMethodNotFound(err){
    if (!err) return false;
    if (err.code === -32601) return true;
    const msg = (err.message || String(err)).toLowerCase();
    return msg.indexOf('method not found') !== -1 || msg.indexOf('unknown method') !== -1;
  }

  async function callHost(logicalName, params){
    const cached = _hostMethodResolved[logicalName];
    if (cached) return sendRequest(cached, params);
    const candidates = HOST_METHOD_CANDIDATES[logicalName] || [logicalName];
    let lastErr = null;
    for (const m of candidates){
      try {
        const result = await sendRequest(m, params);
        _hostMethodResolved[logicalName] = m;
        return result;
      } catch (e) {
        lastErr = e;
        if (!_isMethodNotFound(e)) throw e;
      }
    }
    const err = new Error('host does not implement ' + logicalName);
    err.cause = lastErr;
    err.notImplemented = true;
    throw err;
  }

  // Human-readable rendering of a selection for inclusion in the chat
  // message. Keeps the data visible to the user *and* gives Claude full
  // context in a single ui/message turn — no separate updateModelContext
  // step (which Claude's host has been observed to silently drop).
  function _formatSelection(kind, context){
    function row(k, v){
      if (v === null || v === undefined || v === '') return null;
      return '- ' + k + ': ' + (typeof v === 'object' ? JSON.stringify(v) : String(v));
    }
    function table(pairs){
      return pairs.map(p => row(p[0], p[1])).filter(Boolean).join('\n');
    }
    if (kind === 'forecast') {
      return 'Forecast result (' + (context.region || '—') + '):\n' + table([
        ['Region', context.region],
        ['Horizon (weeks)', context.horizon_weeks],
        ['Baseline units', context.baseline_units != null ? context.baseline_units.toLocaleString() : null],
        ['Uplift', context.uplift_pct != null ? '+' + context.uplift_pct + '%' : null],
        ['Confidence', context.confidence != null ? (context.confidence * 100).toFixed(1) + '%' : null],
        ['Job ID', context.job_id],
      ]);
    }
    if (kind === 'pricing') {
      return 'Pricing change submitted:\n' + table([
        ['Ticket', context.ticket],
        ['Product', context.product],
        ['New price (USD)', context.new_price != null ? Number(context.new_price).toFixed(2) : null],
        ['Status', (context.status || '').replace(/_/g, ' ')],
        ['Submitted (UTC)', context.submitted_at ? new Date(context.submitted_at * 1000).toISOString().slice(0, 19).replace('T', ' ') : null],
      ]);
    }
    if (kind === 'catalog') {
      return 'Product catalog entry (' + (context.sku || '—') + '):\n' + table([
        ['SKU', context.sku],
        ['Name', context.name],
        ['Price', context.price != null ? Number(context.price).toFixed(2) + ' ' + (context.currency || '') : null],
        ['In stock', context.in_stock === true ? 'yes' : context.in_stock === false ? 'no' : null],
        ['Last updated', context.last_updated],
        ['Source', context.source],
      ]);
    }
    // Generic fallback for any unknown kind.
    return 'Selection (' + kind + '):\n```json\n' + JSON.stringify(context, null, 2) + '\n```';
  }

  // Two paths to send a selection into chat:
  //   mode = "inline"  → ui/message contains trigger + formatted data.
  //                      User sees the data in the chat thread. Always
  //                      works because the model receives the data in the
  //                      same user turn.
  //   mode = "silent"  → ui/update-model-context pushes structured data
  //                      with no rendered text, then ui/message sends the
  //                      trigger alone. Spec-correct, leaves the chat
  //                      clean. Claude's MCP Apps host currently has a
  //                      bug where the context push is acked but dropped
  //                      for live-rendered iframes (see README).
  async function callDiscuss(kind, context, mode, buttonId, hintId, triggerText){
    const btn = document.getElementById(buttonId);
    const hint = hintId ? document.getElementById(hintId) : null;
    const original = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = 'Asking Claude…'; }
    const corr = 'discuss-' + Date.now().toString(36);

    diagNote('ui.discuss',
      'iframe → host: ' + mode + ' send for ' + kind,
      { kind, mode }, corr);

    function fallbackToHint(reason){
      diagNote('ui.discuss.fallback', reason, { kind, mode }, corr);
      if (hint) {
        hint.classList.remove('hidden');
        setTimeout(() => { try { reportSize(); } catch(_){} }, 50);
      }
      if (btn) {
        btn.textContent = '✗ host rejected · paste manually';
        setTimeout(() => { btn.disabled = false; if (original) btn.textContent = original; }, 3200);
      }
    }
    function success(label){
      if (btn) {
        btn.textContent = label;
        setTimeout(() => { btn.disabled = false; if (original) btn.textContent = original; }, 2800);
      }
    }

    // For "silent" mode, push structured context first (best effort).
    let silentPushOk = false;
    if (mode === 'silent') {
      try {
        await callHost('updateModelContext', {
          structuredContent: { kind, selection: context },
        });
        silentPushOk = true;
        diagNote('ui.updateModelContext.ok',
          'host acknowledged silent context push (may still be dropped — see README)',
          { kind }, corr);
      } catch (e) {
        diagNote('ui.updateModelContext.fail', String(e && e.message || e),
          { kind, notImplemented: !!e.notImplemented }, corr);
      }
    }

    // The visible user message. Inline mode includes the formatted data;
    // silent mode sends just the trigger.
    const messageText = mode === 'silent'
      ? triggerText
      : (triggerText + '\n\n' + _formatSelection(kind, context));

    try {
      const result = await callHost('sendMessage', {
        role: 'user',
        content: [{ type: 'text', text: messageText }],
      });
      if (result && result.isError) {
        diagNote('ui.sendMessage.rejected', 'host rejected ui/message (isError)',
          { kind, mode }, corr);
        fallbackToHint('host returned isError on ui/message');
        return;
      }
      diagNote('ui.sendMessage.ok',
        'host accepted ui/message — Claude is responding',
        { kind, mode, silentPushOk }, corr);
      success(mode === 'silent'
        ? '✓ trigger sent · context staged silently'
        : '✓ sent · Claude is replying');
    } catch (e) {
      diagNote('ui.sendMessage.fail', String(e && e.message || e),
        { kind, mode, notImplemented: !!e.notImplemented }, corr);
      fallbackToHint('host does not implement ui/message');
    }
  }

  // Send an arbitrary user-role chat message to the host. Used by Card F
  // in the Shiny launcher tab to nudge Claude into invoking a tool that
  // lives on a different MCP server (the standalone /shiny-mcp endpoint).
  window.sendChatPrompt = async function(text){
    return callHost('sendMessage', {
      role: 'user',
      content: [{ type: 'text', text: String(text || '') }],
    });
  };

  // Public bindings. Each kind exposes both modes.
  window.discussForecast       = function(){ _discuss('forecast',  'inline'); };
  window.discussForecastSilent = function(){ _discuss('forecast',  'silent'); };
  window.discussPricing        = function(){ _discuss('pricing',   'inline'); };
  window.discussPricingSilent  = function(){ _discuss('pricing',   'silent'); };
  window.discussCatalog        = function(){ _discuss('catalog',   'inline'); };
  window.discussCatalogSilent  = function(){ _discuss('catalog',   'silent'); };

  function _discuss(kind, mode){
    const selection = lastSelection[kind];
    if (!selection) {
      const label = kind === 'forecast' ? 'Run a forecast first.'
                  : kind === 'pricing'  ? 'Submit a pricing change first.'
                  :                       'Look up a product first.';
      alert(label);
      return;
    }
    const trigger = kind === 'forecast' ? 'analyze this forecast'
                  : kind === 'pricing'  ? 'review this pricing change'
                  :                       'summarize this product';
    const btnId   = (mode === 'silent' ? 'discuss-' + kind + '-silent' : 'discuss-' + kind);
    const hintId  = 'hint-' + kind;
    callDiscuss(kind, selection, mode, btnId, hintId, trigger);
  }

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
    // Live pricing-book mutations from the server. The catalog view auto-
    // re-fetches if the change touches the SKU it's currently showing, and
    // the dashboard re-pulls its snapshot if it's the active view.
    shellSrc.addEventListener('pricing-event', (e) => {
      try {
        const ev = JSON.parse(e.data);
        const sku = ev && ev.payload && ev.payload.product;
        diagNote('ui.pricing-event', 'iframe received pricing-event ' + (ev.type || ''), ev, ev && ev.payload && ev.payload.ticket);
        if (sku) refreshCatalogIfShowing(sku);
        if (typeof refreshDashboardIfShowing === 'function') refreshDashboardIfShowing();
      } catch(_){}
    });
    shellSrc.addEventListener('error', () => { /* auto-retry */ });
  } catch (e) {
    console.debug('[NAV AI] /shell/events not available:', e);
  }
})();
