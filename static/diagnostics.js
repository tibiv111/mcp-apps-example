(function(){
  const tl = document.getElementById('timeline');
  const empty = document.getElementById('empty');
  const statTotal = document.getElementById('stat-total');
  const statRate = document.getElementById('stat-rate');
  const pauseBtn = document.getElementById('pause');
  const clearBtn = document.getElementById('clear');

  let paused = false;
  let total = 0;
  let recentTimes = [];

  function fmtTs(ts){
    const d = new Date(ts * 1000);
    const ms = String(d.getMilliseconds()).padStart(3,'0');
    return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}.${ms}`;
  }
  function durClass(ms){
    if (ms == null) return '';
    if (ms < 30) return 'fast';
    if (ms < 200) return '';
    if (ms < 1000) return 'slow';
    return 'veryslow';
  }
  function render(ev){
    const li = document.createElement('li');
    li.className = 'row';
    li.dataset.corr = ev.correlation_id || '';
    li.dataset.layer = ev.layer;

    const ts = document.createElement('div');
    ts.className = 'col-ts';
    ts.textContent = fmtTs(ev.ts);

    const chip = document.createElement('div');
    chip.innerHTML = `<span class="chip ${ev.layer}">${ev.layer}</span>`;

    const summary = document.createElement('div');
    summary.className = 'col-summary';
    const kindSpan = document.createElement('span');
    kindSpan.className = 'kind';
    kindSpan.textContent = ev.kind;
    summary.appendChild(kindSpan);
    summary.appendChild(document.createTextNode(ev.summary || ''));
    if (ev.correlation_id) {
      const c = document.createElement('span');
      c.className = 'corr';
      c.textContent = ev.correlation_id;
      c.title = 'Click to highlight related events';
      c.onclick = (e) => { e.stopPropagation(); highlightCorr(ev.correlation_id); };
      summary.appendChild(c);
    }

    const dur = document.createElement('div');
    dur.className = 'col-dur ' + durClass(ev.duration_ms);
    dur.textContent = ev.duration_ms != null ? `${ev.duration_ms.toFixed(1)} ms` : '';

    li.appendChild(ts);
    li.appendChild(chip);
    li.appendChild(summary);
    li.appendChild(dur);

    if (ev.detail && Object.keys(ev.detail).length){
      const det = document.createElement('div');
      det.className = 'detail';
      det.textContent = JSON.stringify(ev.detail, null, 2);
      li.appendChild(det);
      li.style.cursor = 'pointer';
      li.onclick = () => li.classList.toggle('expanded');
    }

    tl.prepend(li);
    // Cap rendered rows so the page doesn't grow unbounded.
    while (tl.children.length > 400) tl.removeChild(tl.lastChild);

    total++;
    statTotal.textContent = `${total} events`;
    const now = Date.now() / 1000;
    recentTimes.push(now);
    recentTimes = recentTimes.filter(t => now - t < 5);
    statRate.textContent = `${(recentTimes.length / 5).toFixed(1)}/s`;
    if (empty) empty.style.display = total ? 'none' : '';

    // brief flash
    li.classList.add('flash');
    setTimeout(() => li.classList.remove('flash'), 700);
  }
  function highlightCorr(corr){
    if (!corr) return;
    document.querySelectorAll('li.row').forEach(r => {
      r.classList.toggle('dim', r.dataset.corr !== corr);
    });
    setTimeout(() => {
      document.querySelectorAll('li.row.dim').forEach(r => r.classList.remove('dim'));
    }, 3000);
  }

  pauseBtn.onclick = () => {
    paused = !paused;
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
  };
  clearBtn.onclick = () => {
    tl.innerHTML = '';
    total = 0;
    statTotal.textContent = '0 events';
    if (empty) empty.style.display = '';
  };

  const src = new EventSource('/diagnostics/events');
  const buffer = [];
  let flushScheduled = false;
  function flush(){
    flushScheduled = false;
    if (paused) return;
    while (buffer.length) render(buffer.shift());
  }
  src.addEventListener('snapshot', (e) => {
    try {
      const arr = JSON.parse(e.data);
      arr.forEach(ev => buffer.push(ev));
    } catch(_){}
    if (!flushScheduled){ flushScheduled = true; requestAnimationFrame(flush); }
  });
  src.addEventListener('trace', (e) => {
    try { buffer.push(JSON.parse(e.data)); } catch(_){}
    if (!flushScheduled){ flushScheduled = true; requestAnimationFrame(flush); }
  });
  src.addEventListener('error', () => {
    // EventSource auto-reconnects; just log.
    console.debug('[diagnostics] sse error, will retry');
  });
})();
