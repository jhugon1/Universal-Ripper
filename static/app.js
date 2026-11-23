/* Ripped Ripper: the single page. Plain JavaScript, no framework. Layout and sizes live in app.css;
   this file only fills the fixed places the template provides. */
(function () {
  'use strict';

  // ---------- helpers ----------
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const MINUS = '−', DASH = '—', DOT = '·';
  const FACE = '/static/ripped_face.jpg';
  const C = { accent: '#F5A524', warn: '#FF8A4C', text: '#ECEDEE', muted: '#9BA0A8', faint: '#6E747D' };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const fmt = (sec) => { sec = Math.max(0, Math.floor(sec)); return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0'); };
  const stem = (name) => (name || '').replace(/\.[^.\\/]+$/, '');
  function setText(node, value) { if (node._v !== value) { node._v = value; node.textContent = value; } }
  function setStyle(node, prop, value) { const key = '_s' + prop; if (node[key] !== value) { node[key] = value; node.style[prop] = value; } }
  function form(fields) {
    const f = new FormData();
    Object.entries(fields).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') f.append(k, v); });
    return f;
  }
  async function api(path, options) {
    try {
      const resp = await fetch(path, options);
      let data = null;
      try { data = await resp.json(); } catch (e) { /* not JSON */ }
      return { ok: resp.ok, status: resp.status, data: data || {} };
    } catch (e) {
      return { ok: false, status: 0, data: { message: 'The app is not answering.' } };
    }
  }
  const short = (text, n) => { text = String(text || ''); return text.length > n ? text.slice(0, n - 1) + '…' : text; };

  // ---------- toast (an overlay: it never moves anything) ----------
  let toastTimer = null;
  function showToast(opts) {
    const toast = $('#toast'), art = $('#toast-art');
    const head = opts.head || 'word';   // 'word' | 'already' | 'none'
    $('#toast-head').hidden = head === 'none';
    $('#toast-already').hidden = head !== 'already';
    $('#toast-title').textContent = opts.title || '';
    $('#toast-sub').textContent = opts.sub || '';
    art.onerror = () => { art.onerror = null; art.src = FACE; };   // no cover: the Ripped face
    art.src = opts.cover || FACE;
    toast.classList.toggle('fail', !!opts.fail);
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 3000);
  }
  const cover = (videoId) => (videoId ? '/api/library/cover/' + encodeURIComponent(videoId) : null);

  // ---------- decks ----------
  let snap = null, rxAt = 0;
  const decks = {};
  $$('.deck').forEach((card) => {
    decks[card.dataset.deck] = {
      card, state: $('.deck-state', card), title: $('.deck-title', card), artist: $('.deck-artist', card),
      time: $('.deck-time', card), bpm: $('.deck-bpm', card), key: $('.deck-key', card), fill: $('.bar-fill', card),
      total: $('.deck-total', card), tempo: $('.deck-tempo', card), master: $('.dbtn-master', card), head: $('.deck-playhead', card), mainCv: $('.wave-main', card), overCv: $('.wave-over', card), over: $('.over-head', card), waveKey: '', cols: null, overDirty: true, trackLen: 0, frac: null,
      times: $('.deck-times', card), reset: $('.deck-reset', card),
    };
  });

  const serverNow = () => snap.server_time + (performance.now() - rxAt) / 1000;

  // Mirrors traktor.state.deck_status; the server sends the thresholds in the snapshot.
  function deckStatus(d, now) {
    if (!d.file_name) return 'empty';
    if (!d.is_playing) return 'paused';
    const age = d.updated_at == null ? Infinity : now - d.updated_at;
    if (age > snap.lost_after) return 'paused';
    return age > snap.stale_after ? 'stale' : 'playing';
  }

  function shownElapsed(d, st, now) {
    const tempo = d.tempo || 1;
    let e = d.elapsed || 0;
    if (d.elapsed_at != null) {
      if (st === 'playing') e += Math.max(0, now - d.elapsed_at) * tempo;
      else if (st === 'stale') e += Math.max(0, d.updated_at + snap.stale_after - d.elapsed_at) * tempo;   // frozen where it went quiet
    }
    return d.track_length ? Math.min(e, d.track_length) : e;
  }

  const clock = (sec) => { const t = Math.max(0, Math.round(sec)); return String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0'); };

  function renderDeck(letter, now) {
    const d = snap.decks[letter], ui = decks[letter];
    if (!d || !ui) return;
    const st = deckStatus(d, now);
    refreshWaveform(letter, d);
    let stateText = '', stateColor = C.muted, opacity = 1, timeColor = '';
    if (st === 'empty') { stateText = 'EMPTY'; stateColor = C.faint; }
    else if (st === 'playing') { stateText = 'PLAYING'; stateColor = C.accent; }
    else if (st === 'paused') { stateText = 'PAUSED'; opacity = 0.85; }
    else { stateText = 'TRAKTOR SIGNAL LOST'; stateColor = C.warn; opacity = 0.5; }
    if (d.restored && st !== 'stale' && st !== 'empty') stateText += ' ' + DOT + ' RESTORED';

    if (st === 'empty') {
      setText(ui.title, 'Empty'); setText(ui.artist, '');
      setText(ui.time, '-' + '--:--'); setText(ui.total, '--:--');
      setText(ui.bpm, '---.--'); setText(ui.tempo, '');
      setText(ui.key, DASH);
      setStyle(ui.fill, 'width', '0%'); setStyle(ui.over, 'left', '0%'); ui.frac = null;
      setText(ui.times, '');
    } else {
      const tempo = d.tempo || 1, len = d.track_length || 0, e = shownElapsed(d, st, now);
      const remaining = len ? (len - e) / tempo : null;
      if (st === 'playing' && remaining != null && remaining < 30) timeColor = C.warn;
      const pct = (len ? Math.min(100, Math.max(0, e / len * 100)) : 0).toFixed(2) + '%';
      setText(ui.title, d.title || stem(d.file_name)); setText(ui.artist, d.artist || '');
      setText(ui.time, remaining == null ? '-' + '--:--' : '-' + clock(Math.ceil(remaining)));
      setText(ui.total, len ? clock(len) : '--:--');
      setText(ui.bpm, d.bpm ? (d.bpm * tempo).toFixed(2) : '---.--');
      const pc = (tempo - 1) * 100;
      setText(ui.tempo, (pc >= 0 ? '+' : MINUS) + Math.abs(pc).toFixed(1) + '%');
      setText(ui.key, d.key || DASH);
      setStyle(ui.fill, 'width', pct); setStyle(ui.over, 'left', pct);
      ui.trackLen = len; ui.frac = len ? Math.min(1, Math.max(0, e / len)) : null;
      setText(ui.times, '');
    }
    const isMaster = snap.master && snap.master.deck === letter && st !== 'empty';
    ui.master.classList.toggle('on', !!isMaster);
    paintOverview(ui); paintMain(ui, ui.frac);
    setText(ui.state, stateText); setStyle(ui.state, 'color', stateColor);
    setStyle(ui.time, 'color', timeColor); setStyle(ui.fill, 'background', timeColor || C.accent);
    setStyle(ui.title, 'color', st === 'empty' ? C.faint : '');
    setStyle(ui.card, 'opacity', String(opacity));
    setStyle(ui.reset, 'visibility', (d.restored || st === 'stale') ? 'visible' : 'hidden');   // only when there is something to forget
  }

  // Traktor's own waveform (read from its Stripes folder by the app): 16384 columns x 4 stacked band levels
  const WAVE_COLS = 16384, VIEW_SECONDS = 14;
  const BANDS = ['#2F52FF', '#38D6A0', '#5EC8FF', '#F2A0D6'];   // channel 0..3 (low to top), Traktor-like colours
  function refreshWaveform(letter, d) {
    const ui = decks[letter], key = d && d.file_name ? d.file_name : '';
    if (ui.waveKey === key) return;
    ui.waveKey = key; ui.cols = null; ui.overDirty = true;
    if (!key) return;
    [1500, 4000, 9000].forEach((ms) => setTimeout(async () => {
      if (ui.waveKey !== key || ui.cols) return;
      try {
        const r = await fetch('/api/waveform/' + letter + '?t=' + Date.now());
        if (r.ok) { ui.cols = new Uint8Array(await r.arrayBuffer()); ui.overDirty = true; }
      } catch (e) { /* no waveform yet */ }
    }, ms));
  }

  function fitCanvas(cv) {
    const w = Math.max(1, Math.round(cv.clientWidth * devicePixelRatio)), h = Math.max(1, Math.round(cv.clientHeight * devicePixelRatio));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; return true; }
    return false;
  }
  function paintColumn(g, cols, col, x, mid, half) {
    const o = Math.min(WAVE_COLS - 1, Math.max(0, col)) * 4;
    const order = [0, 1, 2, 3].sort((a, b) => cols[o + b] - cols[o + a]);   // tallest first so every band stays visible
    for (const j of order) {
      const h = Math.max(1, cols[o + j] / 255 * half);
      g.fillStyle = BANDS[j]; g.fillRect(x, mid - h, 1, h * 2);
    }
  }
  function paintOverview(ui) {
    const cv = ui.overCv, resized = fitCanvas(cv);
    if (!resized && !ui.overDirty) return;
    ui.overDirty = false;
    const g = cv.getContext('2d'); g.clearRect(0, 0, cv.width, cv.height);
    if (!ui.cols) return;
    for (let x = 0; x < cv.width; x++) paintColumn(g, ui.cols, Math.floor(x / cv.width * WAVE_COLS), x, cv.height / 2, cv.height / 2 - 1);
  }
  function paintMain(ui, frac) {
    const cv = ui.mainCv; fitCanvas(cv);
    const g = cv.getContext('2d'); g.clearRect(0, 0, cv.width, cv.height);
    if (!ui.cols || frac == null) return;
    const perCol = (ui.trackLen || 1) / WAVE_COLS, span = VIEW_SECONDS / perCol;   // columns visible
    const centre = frac * WAVE_COLS, step = span / cv.width;
    for (let x = 0; x < cv.width; x++) {
      const col = Math.floor(centre - span / 2 + x * step);
      if (col < 0 || col >= WAVE_COLS) continue;
      paintColumn(g, ui.cols, col, x, cv.height / 2, cv.height / 2 - 2);
    }
  }

  function frame() {
    if (snap) { const now = serverNow(); Object.keys(decks).forEach((letter) => renderDeck(letter, now)); }
    requestAnimationFrame(frame);
  }
  $$('.deck-reset').forEach((btn) => btn.addEventListener('click', async () => {
    await api('/api/traktor/reset', { method: 'POST' });
    const r = await api('/api/traktor/state');
    if (r.ok) takeSnapshot(r.data);
  }));
  function takeSnapshot(state) { snap = state; rxAt = performance.now(); }

  // ---------- tonight's queue ----------
  const ICON = {
    up: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 15l6-6 6 6"></path></svg>',
    down: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"></path></svg>',
    remove: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>',
    retry: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4v6h6M20 20v-6h-6M20 9a8 8 0 0 0-14-3M4 15a8 8 0 0 0 14 3"></path></svg>',
  };
  function iconButton(label, svg, onClick, disabled, hidden) {
    const b = el('button', 'icon-btn');
    b.type = 'button'; b.setAttribute('aria-label', label); b.title = label; b.innerHTML = svg;   // svg: constants above
    b.disabled = !!disabled;
    if (hidden) b.style.visibility = 'hidden';
    b.addEventListener('click', onClick);
    return b;
  }

  function queueRow(item) {
    const row = el('div', 'q-row');
    row.appendChild(el('span', 'q-pos', String(item.position).padStart(3, '0')));
    const text = el('div', 'q-text');
    text.append(el('span', 'q-title', item.title), el('span', 'q-artist', item.artist));
    row.appendChild(text);
    row.appendChild(el('span', 'q-len', item.length_s ? fmt(item.length_s) : DASH));
    row.appendChild(el('span', 'q-bpm', item.bpm ? (Math.round(item.bpm * 10) / 10).toFixed(1) : DASH));
    const failed = item.status === 'failed';
    const kind = failed ? 'failed' : item.status === 'ready' ? 'ready' : 'downloading';
    const chip = el('span', 'chip ' + kind, kind.toUpperCase());
    if (failed && item.error) chip.title = item.error;
    row.appendChild(chip);
    const ctl = el('div', 'q-ctl');
    if (failed) {
      ctl.append(iconButton('Try the download again', ICON.retry, () => queueCall('retry', { id: item.id })),
                 iconButton('', ICON.down, () => {}, true, true));
    } else {
      ctl.append(iconButton('Move up', ICON.up, () => queueCall('move', { id: item.id, direction: 'up' })),
                 iconButton('Move down', ICON.down, () => queueCall('move', { id: item.id, direction: 'down' })));
    }
    ctl.appendChild(iconButton('Remove from the queue', ICON.remove, () => queueCall('remove', { id: item.id })));
    row.appendChild(ctl);
    return row;
  }

  function renderQueue(state) {
    setText($('#queue-sub'), (state.session ? state.session.name : '') + ' ' + DOT + ' ' + state.to_play + ' to play');
    const on = !!(state.folder && state.folder.reselect_needed);
    $('#hint').classList.toggle('on', on);
    setText($('#hint-text'), on ? 'New tracks: re-select tonight’s folder in Traktor, sorted by #' : 'Traktor folder is up to date');
    setStyle($('#hint-done'), 'visibility', on ? 'visible' : 'hidden');
    const list = $('#queue-list'), scroll = list.scrollTop;
    list.textContent = '';
    state.items.forEach((item) => list.appendChild(queueRow(item)));
    if (!state.items.length) list.appendChild(el('div', 'empty-note', 'Nothing in the queue yet. Rip a track and it appears here.'));
    list.scrollTop = scroll;
    setText($('#played-count'), 'Played tonight: ' + state.played.length);
    const playedList = $('#played-list');
    playedList.textContent = '';
    state.played.forEach((p) => playedList.appendChild(el('span', '', p.title + ' ' + DOT + ' ' + p.artist)));
  }

  let queueTimer = null;
  function loadQueueSoon() { clearTimeout(queueTimer); queueTimer = setTimeout(loadQueue, 80); }
  async function loadQueue() {
    const r = await api('/api/queue_items');
    if (r.ok) renderQueue(r.data);
  }
  async function queueCall(action, fields) {
    const r = await api('/api/queue_items/' + action, { method: 'POST', body: form(fields || {}) });
    if (r.ok) renderQueue(r.data);
    else showToast({ head: 'none', fail: true, title: 'Queue', sub: 'Couldn’t ' + action + ': ' + short(r.data.message || r.status, 60) });
  }

  $('#hint-done').addEventListener('click', () => queueCall('refreshed'));
  $('#open-folder').addEventListener('click', () => api('/api/open_folder', { method: 'POST', body: form({ folder_type: 'queue' }) }));
  let sureTimer = null;
  $('#new-night').addEventListener('click', (e) => {
    const btn = e.currentTarget;
    const back = () => { btn.classList.remove('sure'); btn.textContent = 'New night'; };
    if (!btn.classList.contains('sure')) { btn.classList.add('sure'); btn.textContent = 'Sure?'; sureTimer = setTimeout(back, 3000); return; }
    clearTimeout(sureTimer); back();
    queueCall('new_night');
  });
  $('#played-toggle').addEventListener('click', (e) => {
    const open = $('#played-list').hidden;
    $('#played-list').hidden = !open;
    e.currentTarget.setAttribute('aria-expanded', String(open));
    setText($('#played-hint'), open ? 'hide' : 'show');
  });

  // ---------- ripper: tabs ----------
  function showTab(name) {
    const search = name === 'search';
    $('#view-search').hidden = !search; $('#view-playlist').hidden = search;
    $('#tab-search').classList.toggle('on', search); $('#tab-playlist').classList.toggle('on', !search);
    $('#tab-search').setAttribute('aria-selected', String(search)); $('#tab-playlist').setAttribute('aria-selected', String(!search));
  }
  $('#tab-search').addEventListener('click', () => showTab('search'));
  $('#tab-playlist').addEventListener('click', () => showTab('playlist'));

  // ---------- ripper: search ----------
  const pendingRips = {};   // video id -> {button, note}: waiting for a download to finish
  const libraryNotes = {};  // video id -> note element, so a finished rip can say "In library"

  function resultRow(r) {
    const row = el('div', 'result');
    const thumb = el('div', 'thumb');
    if (r.thumbnail) { const img = el('img'); img.src = r.thumbnail; img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; thumb.appendChild(img); }
    const text = el('div', 'result-text');
    text.append(el('span', 'result-title', r.title), el('span', 'result-sub', r.artist + ' ' + DOT + ' ' + (r.length_s ? fmt(r.length_s) : DASH)));
    const note = el('span', 'result-note', r.in_library ? 'In library' : '');
    const button = el('button', 'rip', 'Rip');
    button.type = 'button';
    libraryNotes[r.video_id] = note;
    button.addEventListener('click', () => rip(r, button));
    row.append(thumb, text, note, button);
    return row;
  }

  async function search(query) {
    const list = $('#results');
    list.textContent = '';
    list.appendChild(el('div', 'empty-note', 'Searching…'));
    const r = await api('/api/search?q=' + encodeURIComponent(query));
    list.textContent = '';
    if (!r.ok) { list.appendChild(el('div', 'empty-note', 'Search did not work: ' + short(r.data.message || r.data.detail || r.status, 80))); return; }
    if (!r.data.length) { list.appendChild(el('div', 'empty-note', 'No results.')); return; }
    r.data.forEach((item) => list.appendChild(resultRow(item)));
  }
  $('#results').appendChild(el('div', 'empty-note', 'Search for a song, or paste a YouTube link.'));
  $('#search-form').addEventListener('submit', (e) => { e.preventDefault(); const q = $('#q').value.trim(); if (q) search(q); });

  async function rip(r, button) {
    button.disabled = true; button.textContent = '…';
    const r2 = await api('/api/rip', { method: 'POST', body: form({ video_id: r.video_id, artist: r.artist, title: r.title, album: r.album, thumbnail: r.thumbnail }) });
    if (!r2.ok) {
      button.disabled = false; button.textContent = 'Rip';
      showToast({ head: 'none', fail: true, title: r.title, sub: 'Couldn’t rip: ' + short(r2.data.message || r2.data.detail || r2.status, 70) });
      return;
    }
    const kind = r2.data.kind;
    loadQueueSoon();
    if (kind === 'new') { pendingRips[r.video_id] = { button }; return; }   // the toast comes when the download finishes
    button.disabled = false; button.textContent = 'Rip';
    if (kind === 'duplicate') showToast({ head: 'already', title: r.title, sub: 'Added to tonight’s folder', cover: r2.data.cover || cover(r.video_id) });
    else if (kind === 'in_queue') showToast({ head: 'already', title: r.title, sub: 'Already in tonight’s queue', cover: cover(r.video_id) });
    else showToast({ head: 'none', fail: true, title: r.title, sub: 'Couldn’t rip: ' + short(r2.data.message || 'the file could not be linked', 70) });
  }

  function ripFinished(m) {
    const pending = pendingRips[m.video_id];
    if (pending) { pending.button.disabled = false; pending.button.textContent = 'Rip'; delete pendingRips[m.video_id]; }
    if (m.ok) {
      if (libraryNotes[m.video_id]) setText(libraryNotes[m.video_id], 'In library');
      showToast({ head: 'word', title: m.title, sub: 'Added to tonight’s queue', cover: cover(m.video_id) });
    } else {
      showToast({ head: 'none', fail: true, title: m.title, sub: 'Couldn’t rip: ' + short(m.message || 'download failed', 70) });
    }
  }

  // ---------- ripper: playlists ----------
  let fetched = null, lastPlaylistUrl = '';

  function lengthText(totalS) {
    if (!totalS) return '?';
    const minutes = Math.round(totalS / 60), h = Math.floor(minutes / 60), m = minutes % 60;
    return h ? h + 'h ' + m + 'm' : m + 'm';
  }
  function setStatus(text, error, title) {
    const node = $('#pl-status-text');
    node.textContent = text || ''; node.classList.toggle('err', !!error); node.title = title || '';
    $('#spotify-id-form').hidden = true; $('#spotify-connect').hidden = true;
  }
  function showPlaylist(data) {
    setText($('#pl-name'), data.name);
    const shown = data.truncated && data.claimed_count ? data.count + ' of ' + data.claimed_count : String(data.count);
    setText($('#pl-count'), shown + ' tracks ' + DOT + ' ' + lengthText(data.total_s));
    const parts = String(data.dest || '').split(/[\\/]/).filter(Boolean);
    const where = parts.slice(-2).join('\\') + '\\';
    const save = $('#pl-save');
    save.textContent = '';
    save.append('Saves to ', el('b', '', where), '. Not added to tonight’s queue.' + (data.existing ? ' ' + data.existing + ' already there.' : ''));
    $('#pl-bar-fill').style.width = '0%'; setText($('#pl-bar-text'), '');
    $('#pl-rip').disabled = false;
    if (data.truncated) setStatus('Only the first ' + data.count + ' tracks were readable from this page.', true);
  }

  async function fetchPlaylist() {
    const url = $('#pl').value.trim();
    if (!url) return;
    lastPlaylistUrl = url;
    setStatus('Fetching…');
    $('#pl-rip').disabled = true;
    const r = await api('/api/playlist/fetch', { method: 'POST', body: form({ url }) });
    if (!r.ok) { await playlistProblem(r.data); return; }
    fetched = r.data;
    setStatus('');
    showPlaylist(fetched);
  }
  $('#playlist-form').addEventListener('submit', (e) => { e.preventDefault(); fetchPlaylist(); });

  async function playlistProblem(data) {
    const message = data.message || 'Could not read that playlist.';
    if (data.action === 'setup') {
      const s = await api('/api/spotify/status');
      const uri = s.data.redirect_uri || 'http://127.0.0.1:8000/api/spotify/callback';
      setStatus(message, true, message + ' To read private playlists: create a free app at developer.spotify.com/dashboard, add the redirect URI '
        + uri + ', then paste its Client ID here.');
      $('#spotify-id-form').hidden = false; $('#spotify-id').focus();
    } else if (data.action === 'login') {
      setStatus(message, true, message);
      $('#spotify-connect').hidden = false;
    } else {
      setStatus(message, true, message);
    }
  }
  $('#spotify-id-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const r = await api('/api/spotify/client_id', { method: 'POST', body: form({ client_id: $('#spotify-id').value }) });
    if (!r.ok) { setStatus(r.data.message || 'That Client ID was not accepted.', true); $('#spotify-id-form').hidden = false; return; }
    playlistProblem({ action: 'login' });
  });
  $('#spotify-connect').addEventListener('click', () => window.open('/api/spotify/login', 'spotify-login', 'popup=yes,width=520,height=760'));

  $('#pl-rip').addEventListener('click', async () => {
    if (!fetched) return;
    $('#pl-rip').disabled = true;
    const r = await api('/api/playlist/rip', { method: 'POST', body: form({ fetch_id: fetched.fetch_id }) });
    if (!r.ok) {
      setStatus(r.data.message || 'Could not start.', true);
      $('#pl-rip').disabled = r.status === 409;
      return;
    }
    setStatus('');
    playlistProgress(r.data);
  });

  function playlistProgress(m) {
    const finished = m.done + m.failed;
    $('#pl-bar-fill').style.width = (m.total ? Math.min(100, finished / m.total * 100) : 0).toFixed(1) + '%';
    setText($('#pl-bar-text'), m.done + ' / ' + m.total + ' ripped' + (m.failed ? ', ' + m.failed + ' failed' : ''));
  }
  function playlistDone(m) {
    playlistProgress(m);
    $('#pl-rip').disabled = false;
    if (m.problems && m.problems.length) setStatus(short(m.problems[0], 90), true, m.problems.join('\n'));
    showToast({ head: 'word', fail: false, title: m.name,
      sub: m.failed ? 'Playlist ripped · ' + m.done + ' of ' + m.total + ', ' + m.failed + ' failed' : 'Playlist ripped · ' + m.done + ' of ' + m.total });
  }

  // ---------- live updates ----------
  function connect() {
    const es = new EventSource('/api/events');
    es.onopen = () => { $('#conn').hidden = true; };
    es.onerror = () => { $('#conn').hidden = false; };
    es.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      switch (m.event) {
        case 'traktor_state': takeSnapshot(m.state); break;
        case 'queue_update': loadQueueSoon(); break;
        case 'rip_result': ripFinished(m); break;
        case 'playlist_progress': playlistProgress(m); break;
        case 'playlist_done': playlistDone(m); break;
        case 'spotify_connected':
          setStatus('Spotify is connected.');
          if (lastPlaylistUrl) fetchPlaylist();
          break;
        default: break;
      }
    };
  }

  // ---------- start ----------
  api('/api/traktor/state').then((r) => { if (r.ok && !snap) takeSnapshot(r.data); });
  connect();
  loadQueue();
  setInterval(loadQueue, 10000);   // a safety net; the live events normally arrive first
  requestAnimationFrame(frame);
  const splash = $('#splash');
  if (splash) setTimeout(() => { splash.classList.add('gone'); setTimeout(() => splash.remove(), 450); }, 1000);
})();
