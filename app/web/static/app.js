/* Lecture Companion front-end: job polling, reading position, annotations, focus mode, study timer. */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  // ---------- job polling (top bar + lecture page) ----------
  async function pollJobs() {
    const chips = $$('.jobchip');
    let anyActive = false;
    for (const chip of chips) {
      try {
        const r = await fetch('/api/jobs/' + chip.dataset.job);
        if (!r.ok) continue;
        const j = await r.json();
        chip.querySelector('.jobmsg').textContent = `${j.status} · ${Math.round(j.progress * 100)}% · ${(j.message || '').slice(0, 70)}`;
        chip.querySelector('.dot').className = 'dot ' + j.status;
        if (j.status === 'queued' || j.status === 'running') anyActive = true;
      } catch (e) { /* offline */ }
    }
    const js = $('#jobstatus');
    if (js && js.classList.contains('active')) {
      try {
        const r = await fetch('/api/lectures/' + js.dataset.lecture + '/jobs');
        const d = await r.json();
        const j = d.jobs[0];
        if (j) {
          const p = $('#jobProgress'); if (p) p.style.width = Math.round(j.progress * 100) + '%';
          const m = $('#jobMessage'); if (m) m.textContent = `${j.status} · ${Math.round(j.progress * 100)}% · ${j.message || ''}` + (j.cost_usd ? ` · $${j.cost_usd.toFixed(2)}` : '');
          if (j.status === 'queued' || j.status === 'running') anyActive = true;
          else if (d.lecture_status !== 'processing' && d.lecture_status !== 'queued') { setTimeout(() => location.reload(), 800); return; }
        }
      } catch (e) { }
    }
    if (anyActive || chips.length) setTimeout(pollJobs, 2500);
  }
  window.jobAction = async function (id, action) {
    await fetch(`/api/jobs/${id}/${action}`, { method: 'POST' });
    setTimeout(() => location.reload(), 500);
  };

  // ---------- annotations ----------
  window.toggleAnn = async function (lectureId, kind, anchor, btn, label) {
    const r = await fetch(`/api/lectures/${lectureId}/annotations`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, anchor, anchor_label: label || anchor }) });
    const d = await r.json();
    if (btn) btn.classList.toggle('on', !d.removed);
    const el = document.getElementById(anchor);
    if (el) el.classList.toggle('has-' + kind, !d.removed);
  };

  function applyAnnotations(anns) {
    for (const a of anns) {
      const el = document.getElementById(a.anchor);
      if (!el) continue;
      el.classList.add('has-' + a.kind);
      if (a.kind === 'note' && a.body) {
        const n = document.createElement('div');
        n.className = 'user-note'; n.textContent = '📝 ' + a.body;
        const del = document.createElement('button'); del.className = 'btn tiny'; del.textContent = '✕'; del.style.marginLeft = '8px';
        del.onclick = async () => { await fetch('/api/annotations/' + a.id, { method: 'DELETE' }); n.remove(); };
        n.appendChild(del);
        (el.querySelector('.concept-body') || el).appendChild(n);
      }
      $$(`button[onclick*="'${a.anchor}'"]`).forEach(b => { if (b.textContent.includes(a.kind === 'practiced' ? '✓' : '↺')) b.classList.add('on'); });
    }
  }

  function setupPopover(lectureId) {
    const pop = $('#annPopover'); if (!pop) return;
    let target = null;
    document.addEventListener('click', (e) => {
      const el = e.target.closest('.annotatable');
      if (!el || e.target.closest('a,button,summary,details.sub,input,textarea,.popover')) { if (!e.target.closest('.popover')) pop.classList.add('hidden'); return; }
      target = el;
      const rect = el.getBoundingClientRect();
      pop.style.left = (rect.left + window.scrollX) + 'px';
      pop.style.top = (rect.top + window.scrollY - 36) + 'px';
      pop.classList.remove('hidden');
    });
    pop.querySelectorAll('button').forEach(b => b.addEventListener('click', async () => {
      if (!target) return;
      const kind = b.dataset.kind;
      const label = target.dataset.label || target.querySelector('h2,h3,.ctitle')?.textContent?.trim() || target.textContent.trim().slice(0, 60);
      let body = '';
      if (kind === 'note') { body = prompt('Your note:'); if (body === null) return; }
      const r = await fetch(`/api/lectures/${lectureId}/annotations`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, anchor: target.id, anchor_label: label, body }) });
      const d = await r.json();
      target.classList.toggle('has-' + kind, !d.removed);
      if (kind === 'note' && !d.removed) applyAnnotations([{ id: d.id, kind, anchor: target.id, body }]);
      pop.classList.add('hidden');
    }));
  }

  // ---------- reading position ----------
  function setupPosition(lectureId, saved) {
    const reading = $('#reading'); if (!reading) return;
    if (saved && !location.hash) {
      const el = document.getElementById(saved);
      if (el) { const d = el.closest('details.concept'); if (d) d.open = true; setTimeout(() => el.scrollIntoView({ block: 'start' }), 50); }
    }
    if (location.hash) { const el = document.getElementById(location.hash.slice(1)); const d = el && el.closest('details.concept'); if (d) d.open = true; }
    const targets = $$('#reading [id]').filter(e => e.id);
    let current = saved || '';
    let timer = null;
    const io = new IntersectionObserver((entries) => {
      for (const en of entries) if (en.isIntersecting) { current = en.target.id; }
      $$('.toc a').forEach(a => a.classList.toggle('current', a.getAttribute('href') === '#' + current || ('#' + (document.getElementById(current)?.closest('details.concept')?.id)) === a.getAttribute('href')));
      clearTimeout(timer);
      timer = setTimeout(() => {
        const progress = Math.min(1, (window.scrollY + window.innerHeight) / document.body.scrollHeight);
        fetch(`/api/lectures/${lectureId}/position`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ anchor: current, progress }) }).catch(() => { });
      }, 1200);
    }, { rootMargin: '-10% 0px -70% 0px' });
    targets.forEach(t => io.observe(t));
  }

  // ---------- focus mode + timer ----------
  function setupFocus() {
    const b = $('#focusBtn'); if (!b) return;
    if (localStorage.getItem('lc.focus') === '1') document.body.classList.add('focus');
    b.addEventListener('click', () => { document.body.classList.toggle('focus'); localStorage.setItem('lc.focus', document.body.classList.contains('focus') ? '1' : '0'); });
    document.addEventListener('keydown', (e) => { if (e.key === 'f' && !e.target.matches('input,textarea')) b.click(); });
  }
  function setupTimer() {
    const btn = $('#timerBtn'), panel = $('#timerPanel'); if (!btn || !panel) return;
    const clock = $('#timerClock'), mins = $('#timerMinutes');
    let remaining = (parseInt(localStorage.getItem('lc.timer.remaining')) || mins.value * 60), running = false, iv = null;
    const fmt = s => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
    const draw = () => { clock.textContent = fmt(remaining); $('#timerLabel').textContent = running ? fmt(remaining) : mins.value + ' min'; };
    const chime = () => { try { const ctx = new (window.AudioContext || window.webkitAudioContext)(); const o = ctx.createOscillator(); const g = ctx.createGain(); o.connect(g); g.connect(ctx.destination); o.frequency.value = 660; g.gain.value = 0.08; o.start(); setTimeout(() => { o.stop(); }, 600); } catch (e) { } };
    btn.addEventListener('click', () => panel.classList.toggle('hidden'));
    $('#timerStart').addEventListener('click', () => { if (running) return; running = true; iv = setInterval(() => { remaining--; localStorage.setItem('lc.timer.remaining', remaining); draw(); if (remaining <= 0) { clearInterval(iv); running = false; chime(); alert('Study session complete. Take a short break — your position is saved.'); remaining = mins.value * 60; draw(); } }, 1000); });
    $('#timerPause').addEventListener('click', () => { running = false; clearInterval(iv); draw(); });
    $('#timerReset').addEventListener('click', () => { running = false; clearInterval(iv); remaining = mins.value * 60; localStorage.removeItem('lc.timer.remaining'); draw(); });
    mins.addEventListener('change', () => { if (!running) { remaining = mins.value * 60; draw(); } });
    draw();
  }

  // ---------- transcript filter ----------
  function setupTranscriptFilter() {
    const f = $('#tfilter'); if (!f) return;
    f.addEventListener('input', () => { const q = f.value.toLowerCase(); $$('#transcript .seg').forEach(p => p.style.display = !q || p.textContent.toLowerCase().includes(q) ? '' : 'none'); });
  }

  window.initLecture = function (lectureId, annotations, savedPosition) {
    applyAnnotations(annotations || []);
    setupPopover(lectureId);
    setupPosition(lectureId, savedPosition);
    setupFocus();
    setupTimer();
    setupTranscriptFilter();
  };

  document.addEventListener('DOMContentLoaded', pollJobs);
})();
