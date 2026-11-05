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

  // minimal Markdown -> HTML for explanations (bold, code, inline code, lists, paragraphs)
  function miniMd(md) {
    const esc = t => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    let out = '', inCode = false, inList = false, buf = [];
    const flushP = () => { if (buf.length) { out += '<p>' + buf.join(' ') + '</p>'; buf = []; } };
    for (const raw of md.split('\n')) {
      const line = raw.trimEnd();
      if (line.startsWith('```')) { flushP(); if (inList) { out += '</ul>'; inList = false; } inCode = !inCode; out += inCode ? '<pre><code>' : '</code></pre>'; continue; }
      if (inCode) { out += esc(raw) + '\n'; continue; }
      const inline = t => esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*(.+?)\*/g, '<i>$1</i>');
      if (/^\s*[-*] /.test(line)) { flushP(); if (!inList) { out += '<ul>'; inList = true; } out += '<li>' + inline(line.replace(/^\s*[-*] /, '')) + '</li>'; continue; }
      if (inList) { out += '</ul>'; inList = false; }
      if (!line.trim()) { flushP(); continue; }
      buf.push(inline(line));
    }
    flushP(); if (inList) out += '</ul>'; if (inCode) out += '</code></pre>';
    return out;
  }

  // highlight the quoted passage inside the anchored element
  function highlightQuote(el, quote) {
    if (!quote || quote.length < 3) return null;
    const needle = quote.replace(/\s+/g, ' ').trim().slice(0, 300);
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, { acceptNode: n => n.parentElement.closest('.explanation,.user-note,.regen,form') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
    let n; const nodes = []; while ((n = walker.nextNode())) nodes.push(n);
    for (const node of nodes) {
      const text = node.nodeValue; const idx = text.replace(/\s+/g, ' ').indexOf(needle);
      if (idx < 0) continue;
      // map back to the original string positions (approximate for collapsed whitespace)
      let start = -1, count = 0, i = 0; const norm = text.replace(/\s+/g, ' ');
      for (; i < text.length; i++) { if (count === idx) { start = i; break; } if (!(/\s/.test(text[i]) && /\s/.test(text[i - 1] || ''))) count++; }
      if (start < 0) continue;
      const before = text.slice(0, start); const rest = text.slice(start);
      const m = rest.match(new RegExp('^' + needle.split(' ').map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+')));
      if (!m) continue;
      const mark = document.createElement('mark'); mark.className = 'hl'; mark.textContent = m[0];
      const after = document.createTextNode(rest.slice(m[0].length));
      node.nodeValue = before; node.parentNode.insertBefore(mark, node.nextSibling); mark.parentNode.insertBefore(after, mark.nextSibling);
      return mark;
    }
    return null;
  }

  function renderExplanation(el, a) {
    const box = document.createElement('div'); box.className = 'explanation'; box.dataset.annId = a.id;
    const label = { explain: 'Explained', simpler: 'Simpler', example: 'Example', why: 'Why' }[a.mode] || 'Explained';
    box.innerHTML = '<button class="btn tiny del">✕</button><span class="tag">' + label + ' · generated</span>' +
      (a.quote ? '<div class="q">“' + a.quote.replace(/</g, '&lt;').slice(0, 220) + (a.quote.length > 220 ? '…' : '') + '”</div>' : '') + miniMd(a.body || '');
    box.querySelector('.del').onclick = async () => { await fetch('/api/annotations/' + a.id, { method: 'DELETE' }); box.remove(); };
    const host = el.querySelector('.concept-body') || el;
    // place right after the highlighted sentence's block when possible
    const mark = highlightQuote(el, a.quote);
    const block = mark && mark.closest('p,li,dd,h4,div.example');
    if (block && block.parentNode) block.insertAdjacentElement('afterend', box); else host.appendChild(box);
  }

  function applyAnnotations(anns) {
    for (const a of anns) {
      const el = document.getElementById(a.anchor);
      if (!el) continue;
      el.classList.add('has-' + a.kind);
      if (a.kind === 'explanation') { renderExplanation(el, a); continue; }
      if (a.kind === 'note' && a.body) {
        const n = document.createElement('div');
        n.className = 'user-note';
        if (a.quote) { const q = document.createElement('span'); q.className = 'q'; q.textContent = '“' + a.quote.slice(0, 160) + (a.quote.length > 160 ? '…' : '') + '”'; n.appendChild(q); }
        n.appendChild(document.createTextNode('📝 ' + a.body));
        const del = document.createElement('button'); del.className = 'btn tiny'; del.textContent = '✕'; del.style.marginLeft = '8px';
        del.onclick = async () => { await fetch('/api/annotations/' + a.id, { method: 'DELETE' }); n.remove(); };
        n.appendChild(del);
        const mark = a.quote ? highlightQuote(el, a.quote) : null;
        const block = mark && mark.closest('p,li,dd,h4');
        if (block) block.insertAdjacentElement('afterend', n); else (el.querySelector('.concept-body') || el).appendChild(n);
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

  // ---------- selection toolbar: explain / simpler / example / why / my note ----------
  function setupSelection(lectureId) {
    const bar = $('#selToolbar'); const reading = $('#reading'); if (!bar || !reading) return;
    let current = null;
    const hide = () => { bar.classList.add('hidden'); };
    document.addEventListener('selectionchange', () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) { hide(); return; }
      const range = sel.getRangeAt(0);
      const node = range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
      if (!reading.contains(node) || node.closest('form,textarea,input,.explanation,.user-note')) { hide(); return; }
      const text = sel.toString().trim();
      if (text.length < 3 || text.length > 2000) { hide(); return; }
      const anchorEl = node.closest('[id]');
      current = { quote: text, anchor: anchorEl ? anchorEl.id : '', el: anchorEl };
      const r = range.getBoundingClientRect();
      bar.style.left = Math.max(8, Math.min(window.innerWidth - 420, r.left + window.scrollX)) + 'px';
      bar.style.top = (r.top + window.scrollY - 40) + 'px';
      bar.classList.remove('hidden');
    });
    bar.addEventListener('mousedown', e => e.preventDefault());  // keep the selection
    bar.querySelectorAll('button').forEach(b => b.addEventListener('click', async () => {
      if (!current) return;
      const mode = b.dataset.mode; const { quote, anchor, el } = current;
      hide();
      if (mode === 'note') {
        const body = prompt('Your note about: “' + quote.slice(0, 80) + '…”'); if (body === null || !body.trim()) return;
        const r = await fetch(`/api/lectures/${lectureId}/annotations`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind: 'note', anchor, anchor_label: quote.slice(0, 80), body, quote }) });
        const d = await r.json();
        if (el) applyAnnotations([{ id: d.id, kind: 'note', anchor, body, quote }]);
        return;
      }
      const placeholder = document.createElement('div'); placeholder.className = 'explanation';
      placeholder.innerHTML = '<span class="tag">' + mode + '</span> Asking the model… <span class="muted small">(a few seconds)</span>';
      (el && (el.querySelector('.concept-body') || el) || reading).appendChild(placeholder);
      try {
        const r = await fetch(`/api/lectures/${lectureId}/explain`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ quote, anchor, mode }) });
        const d = await r.json();
        placeholder.remove();
        if (!r.ok) { alert('Could not explain: ' + (d.detail || r.status)); return; }
        if (el) renderExplanation(el, { id: d.id, mode, quote, body: d.explanation });
        window.getSelection()?.removeAllRanges();
      } catch (e) { placeholder.textContent = 'Request failed: ' + e; }
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

  // ---------- glossary tooltips: every glossary term in the notes gets a hover definition ----------
  function setupGlossary(glossary) {
    const root = $('#reading'); if (!root || !glossary || !glossary.length) return;
    const defs = new Map();
    for (const g of glossary) {
      const base = g.term.replace(/\s*\(.*?\)\s*/g, ' ').trim();
      for (const t of [g.term, base, ...(g.term.match(/\(([^)]+)\)/g) || []).map(x => x.slice(1, -1))]) {
        const k = t.trim().toLowerCase(); if (k.length >= 3 && !defs.has(k)) defs.set(k, g.explanation);
      }
    }
    const terms = [...defs.keys()].sort((a, b) => b.length - a.length).map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    const re = new RegExp('\\b(' + terms.join('|') + ')\\b', 'gi');
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, { acceptNode: n => (n.parentElement.closest('a,button,code,pre,summary,.ref,.term,h1,h2,h3,h4,dt,.user-note') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT) });
    const nodes = []; let n; while ((n = walker.nextNode())) nodes.push(n);
    let marked = 0;
    for (const node of nodes) {
      if (!re.test(node.nodeValue)) { re.lastIndex = 0; continue; }
      re.lastIndex = 0;
      const frag = document.createDocumentFragment(); let last = 0; let m; const seen = new Set();
      while ((m = re.exec(node.nodeValue))) {
        const key = m[1].toLowerCase();
        if (seen.has(key)) continue;  // first occurrence per text node only
        seen.add(key);
        frag.appendChild(document.createTextNode(node.nodeValue.slice(last, m.index)));
        const span = document.createElement('span'); span.className = 'term'; span.textContent = m[1]; span.dataset.tip = defs.get(key);
        frag.appendChild(span); last = m.index + m[0].length; marked++;
      }
      frag.appendChild(document.createTextNode(node.nodeValue.slice(last)));
      node.parentNode.replaceChild(frag, node);
    }
    let tip = null;
    root.addEventListener('mouseover', e => {
      const t = e.target.closest('.term'); if (!t) return;
      tip = document.createElement('div'); tip.className = 'term-tip'; tip.textContent = t.dataset.tip;
      document.body.appendChild(tip);
      const r = t.getBoundingClientRect(); tip.style.left = Math.max(8, Math.min(window.innerWidth - 360, r.left + window.scrollX)) + 'px'; tip.style.top = (r.bottom + window.scrollY + 6) + 'px';
    });
    root.addEventListener('mouseout', e => { if (e.target.closest('.term') && tip) { tip.remove(); tip = null; } });
  }

  window.initLecture = function (lectureId, annotations, savedPosition, glossary) {
    setupGlossary(glossary || []);
    applyAnnotations(annotations || []);
    setupSelection(lectureId);
    setupPopover(lectureId);
    setupPosition(lectureId, savedPosition);
    setupFocus();
    setupTimer();
    setupTranscriptFilter();
  };

  // ---------- upload progress ----------
  function setupUpload() {
    const form = $('#importForm'); if (!form) return;
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const status = $('#uploadStatus'), bar = $('#uploadBar'), msg = $('#uploadMsg'), btn = $('#importBtn');
      const files = [...form.querySelectorAll('input[type=file]')].flatMap(i => [...i.files]);
      status.classList.remove('hidden'); btn.disabled = true; btn.textContent = 'Uploading…';
      const xhr = new XMLHttpRequest();
      xhr.open('POST', form.action);
      xhr.upload.onprogress = (ev) => {
        if (!ev.lengthComputable) return;
        const pct = Math.round(ev.loaded / ev.total * 100);
        bar.style.width = pct + '%';
        msg.textContent = `Uploading ${files.length} file(s), ${(ev.loaded / 1048576).toFixed(1)} / ${(ev.total / 1048576).toFixed(1)} MB (${pct}%)`;
        if (pct >= 100) msg.textContent = 'Upload complete. Parsing transcripts and extracting images…';
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 400) { msg.textContent = 'Done. Opening review…'; window.location.href = xhr.responseURL || '/'; }
        else { msg.textContent = 'Upload failed (' + xhr.status + '). ' + (xhr.responseText || '').replace(/<[^>]+>/g, ' ').slice(0, 200); btn.disabled = false; btn.textContent = 'Upload and review'; }
      };
      xhr.onerror = () => { msg.textContent = 'Network error during upload.'; btn.disabled = false; btn.textContent = 'Upload and review'; };
      xhr.send(new FormData(form));
    });
  }

  // ---------- export status (toast while the server renders PDF/DOCX) ----------
  function toast(text, sticky = false) {
    let t = $('#toast'); if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
    t.textContent = text; t.classList.remove('hidden');
    clearTimeout(t._timer); if (!sticky) t._timer = setTimeout(() => t.classList.add('hidden'), 3500);
    return t;
  }
  function setupExports() {
    document.addEventListener('click', async (e) => {
      const a = e.target.closest('a.export-link'); if (!a) return;
      e.preventDefault();
      const label = a.textContent.trim();
      toast('Preparing ' + label + '… (PDF and DOCX take a few seconds)', true);
      a.classList.add('busy');
      try {
        const t0 = Date.now();
        const r = await fetch(a.href);
        if (!r.ok) { const d = await r.text(); toast('Export failed: ' + d.replace(/<[^>]+>/g, ' ').slice(0, 160)); return; }
        const blob = await r.blob();
        const cd = r.headers.get('Content-Disposition') || '';
        const name = (cd.match(/filename="?([^";]+)"?/) || [])[1] || ('export.' + a.href.split('/export/')[1].split('?')[0]);
        const url = URL.createObjectURL(blob);
        const dl = document.createElement('a'); dl.href = url; dl.download = name; document.body.appendChild(dl); dl.click(); dl.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        toast(`Downloaded ${name} (${(blob.size / 1024).toFixed(0)} KB, ${((Date.now() - t0) / 1000).toFixed(1)} s)`);
      } catch (err) { toast('Export failed: ' + err); }
      finally { a.classList.remove('busy'); }
    });
  }

  // ---------- draft autosave for the editor ----------
  function setupDrafts() {
    const forms = $$('form.draftable'); if (!forms.length) return;
    const m = location.pathname.match(/\/lectures\/([a-f0-9]{12})/); if (!m) return;
    const lectureId = m[1];
    for (const form of forms) {
      const status = form.querySelector('.draft-status'); let timer = null, dirty = false;
      const save = async () => {
        const data = {}; new FormData(form).forEach((v, k) => { if (k !== 'action') data[k] = v; });
        try {
          const r = await fetch(`/api/lectures/${lectureId}/drafts/${form.dataset.section}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
          const d = await r.json();
          if (status) status.textContent = 'Draft autosaved ' + new Date(d.saved_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ' · not published yet';
          dirty = false;
        } catch (e) { if (status) status.textContent = 'Autosave failed'; }
      };
      form.addEventListener('input', () => { dirty = true; if (status) status.textContent = 'Typing… (autosaves in 2 s)'; clearTimeout(timer); timer = setTimeout(save, 2000); });
      window.addEventListener('beforeunload', () => { if (dirty) { clearTimeout(timer); navigator.sendBeacon && navigator.sendBeacon(`/api/lectures/${lectureId}/drafts/${form.dataset.section}`, new Blob([JSON.stringify(Object.fromEntries([...new FormData(form)].filter(([k]) => k !== 'action')))], { type: 'application/json' })); } });
      form.addEventListener('submit', () => { clearTimeout(timer); dirty = false; });
    }
  }

  document.addEventListener('DOMContentLoaded', () => { pollJobs(); setupUpload(); setupExports(); setupDrafts(); });
})();
