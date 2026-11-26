// SEBE 3D — интерфейс (без сборки, чистый JS-модуль)
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const STATE_RU = {
  offline: 'нет связи', idle: 'готов', preparing: 'подготовка', printing: 'печать', paused: 'пауза',
  finished: 'готово', cancelled: 'отменено', error: 'ошибка', busy: 'занят', unknown: 'неизвестно',
};
const KIND_RU = { gcode: 'G-code', '3mf': '3MF (нарезан)', mesh: '3D-модель', resin: 'Фотополимер', other: 'Другое' };
const TEMP_RU = { nozzle: 'Сопло', nozzle1: 'Сопло 2', bed: 'Стол', chamber: 'Камера', uvled: 'УФ-LED', vat: 'Ванна', box: 'Корпус' };

const S = {
  tab: localStorageGet('tab') || 'printers',
  sub: 'list',            // вкладка «Модели»: list | prepare | calc
  addOpen: false,         // панель «Найти / добавить» на вкладке «Принтеры»
  found: null,            // результат последнего поиска в сети
  discSubnet: '',
  printers: {},          // id -> snapshot
  order: [],
  drivers: [],
  materials: [],
  settings: {},
  info: {},
  transfers: {},
  cameraOn: new Set(),
  prepare: { id: localStorageGet('prepare_id') || '', viewer: null, printer: localStorageGet('prepare_printer') || '' },
};

function localStorageGet(k) { try { return localStorage.getItem('ph_' + k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem('ph_' + k, v); } catch { /* приватный режим */ } }

// ------------------------------------------------------------------ утилиты
async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.body = JSON.stringify(o.json);
    o.headers = { ...(o.headers || {}), 'Content-Type': 'application/json' };
    delete o.json;
  }
  const r = await fetch('/api' + path, o);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || data || r.statusText);
  return data;
}

function toast(text, level = 'info', ms = 5000) {
  const el = document.createElement('div');
  el.className = 'toast ' + level;
  el.textContent = text;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function guard(fn, okText) {
  try {
    const r = await fn();
    if (okText) toast(okText, 'ok');
    return r;
  } catch (e) {
    toast(e.message || String(e), 'error', 8000);
    return undefined;
  }
}

function fmtDur(s) {
  if (s === null || s === undefined || isNaN(s)) return '—';
  s = Math.round(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  const parts = [];
  if (d) parts.push(d + ' д');
  if (h) parts.push(h + ' ч');
  if (m || (!d && !h)) parts.push(m + ' мин');
  return parts.join(' ');
}
function fmtEta(remaining) {
  if (!remaining) return '';
  const t = new Date(Date.now() + remaining * 1000);
  const today = new Date();
  const sameDay = t.toDateString() === today.toDateString();
  return (sameDay ? '' : t.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' }) + ' ') +
    t.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
const fmtNum = (v, d = 1) => (v === null || v === undefined || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', { maximumFractionDigits: d }));
const fmtSize = (b) => (b == null ? '—' : b > 1048576 ? fmtNum(b / 1048576, 1) + ' МБ' : fmtNum(b / 1024, 0) + ' КБ');
const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—');
const money = (v) => fmtNum(v, 2) + ' ' + (S.settings.currency || '₸');

function modal(title, bodyHtml, { wide = false } = {}) {
  const bg = document.createElement('div');
  bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal" style="${wide ? 'width:min(1000px,100%)' : ''}"><h3>${esc(title)}<button class="x" aria-label="Закрыть">×</button></h3><div class="mbody">${bodyHtml}</div></div>`;
  const close = () => bg.remove();
  bg.addEventListener('click', (e) => { if (e.target === bg) close(); });
  $('.x', bg).onclick = close;
  document.body.appendChild(bg);
  return { el: $('.mbody', bg), close };
}

function confirmBox(text) { return window.confirm(text); }

// ------------------------------------------------------------------ WebSocket
let ws;
function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onopen = () => { $('#conn').className = 'conn ok'; $('#conn').textContent = 'онлайн'; };
  ws.onclose = () => {
    $('#conn').className = 'conn bad'; $('#conn').textContent = 'нет связи';
    setTimeout(connectWs, 3000);
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === 'hello') {
      setPrinters(m.printers);
      if (S.tab === 'printers') renderPrinters();
    } else if (m.type === 'status') {
      const p = S.printers[m.printer_id];
      if (p) {
        p.status = m.status;
        p.capabilities = m.capabilities;
        if (S.tab === 'printers') updatePrinterCard(m.printer_id);
      }
    } else if (m.type === 'printers_changed') {
      refreshPrinters();
    } else if (m.type === 'transfer') {
      S.transfers[m.transfer.id] = m.transfer;
      onTransfer(m.transfer);
    } else if (m.type === 'update') {
      S.update = m.update;
      renderUpdateBar();
      if (S.tab === 'settings') renderUpdatePanel();
    } else if (m.type === 'toast') {
      toast(m.text, m.level === 'error' ? 'error' : 'info', 10000);
    } else if (m.type === 'library_changed' && S.tab === 'models' && S.sub === 'list') {
      renderLibrary();
    } else if (m.type === 'queue_changed' && S.tab === 'jobs') {
      renderQueue();
    } else if (m.type === 'jobs_changed' && S.tab === 'jobs') {
      renderHistory();
    }
  };
}

function setPrinters(list) {
  S.printers = {};
  S.order = [];
  for (const p of list) { S.printers[p.config.id] = p; S.order.push(p.config.id); }
}
async function refreshPrinters() {
  setPrinters(await api('/printers'));
  if (S.tab === 'printers') renderPrinters();
}

function onTransfer(t) {
  const pct = t.total ? Math.round((t.sent / t.total) * 100) : 0;
  const el = $(`#transfer-${t.printer_id}`);
  const text = t.status === 'error' ? `❌ ${t.filename}: ${t.error}` :
    t.status === 'done' ? `✅ ${t.filename}: ${t.start ? 'печать запущена' : 'загружено'}` :
      t.status === 'starting' ? `▶ ${t.filename}: запуск печати…` : `⬆ ${t.filename}: ${pct}%`;
  if (el) { el.textContent = text; el.classList.toggle('hidden', false); }
  if (t.status === 'error') toast(`${t.printer_name}: ${t.error}`, 'error', 10000);
  if (t.status === 'done') toast(`${t.printer_name}: ${t.start ? 'печать запущена' : 'файл загружен'} — ${t.filename}`, 'ok');
}

// ------------------------------------------------------------------ навигация
// Четыре вкладки: Принтеры, Модели, Печать, Настройки. Старые разделы — части этих страниц
// (их имена оставлены псевдонимами: работают внутренние переходы и сохранённая вкладка).
const TAB_ALIAS = { discover: ['printers'], library: ['models', 'list'], prepare: ['models', 'prepare'], calc: ['models', 'calc'],
  queue: ['jobs'], history: ['jobs'], materials: ['settings'] };
function setTab(tab, sub) {
  if (tab === 'discover') S.addOpen = true;
  if (TAB_ALIAS[tab]) [tab, sub] = [TAB_ALIAS[tab][0], sub || TAB_ALIAS[tab][1]];
  if (!['printers', 'models', 'jobs', 'settings'].includes(tab)) tab = 'printers';
  if (tab === 'models') S.sub = sub || 'list';
  if (S.prepare.viewer && !(tab === 'models' && S.sub === 'prepare')) { S.prepare.viewer.dispose(); S.prepare.viewer = null; }
  S.tab = tab;
  localStorageSet('tab', tab);
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  ({ printers: renderPrinters, models: renderModels, jobs: renderJobs, settings: renderSettingsPage })[tab]();
}
/** Блок страницы-сборки; если пользователь уже ушёл с этой страницы — отрисовка уходит «в никуда», а не поверх другой вкладки. */
function view(id) { return $('#' + id) || document.createElement('div'); }

function renderModels() {
  $('#main').innerHTML = '<div id="models-body"></div>';
  ({ list: renderLibrary, prepare: renderPrepare, calc: renderCalc })[S.sub]();
}
/** Заголовок страниц вкладки «Модели»: «← Все модели» слева от заголовка, «Калькулятор» — справа в той же строке. */
function modelsTitle(title, right = '') {
  const back = S.sub === 'list' ? '' : '<button class="btn small" data-mnav="list">← Все модели</button>';
  const calc = S.sub === 'calc' ? '' : '<button class="btn small" data-mnav="calc">₸ Калькулятор</button>';
  return `<div class="row" style="margin-bottom:12px;gap:12px;align-items:center">${back}<h2 style="margin:0">${title}</h2>
    <span class="spacer"></span>${right}${calc}</div>`;
}
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-mnav]');
  if (b) setTab('models', b.dataset.mnav);
});
function renderJobs() {
  $('#main').innerHTML = '<div id="jobs-q"></div><div id="jobs-h" style="margin-top:28px"></div>';
  renderQueue(); renderHistory();
}
function renderSettingsPage() {
  $('#main').innerHTML = `<div class="panel row" id="ui-zoom-panel" style="margin-bottom:16px;gap:12px"><b>Масштаб интерфейса</b>
      <select id="ui-zoom">${[['auto', 'авто (по размеру экрана)'], ['1', '100%'], ['1.25', '125%'], ['1.5', '150%'], ['1.75', '175%'], ['2', '200%']]
        .map(([v, t]) => `<option value="${v}" ${v === zoomSetting() ? 'selected' : ''}>${t}</option>`).join('')}</select>
      <span class="muted small">сейчас ${Math.round(currentZoom() * 100)}% · также Ctrl + колесо мыши</span></div>
    <div id="set-main"></div><div id="set-mat" style="margin-top:28px"></div>`;
  $('#ui-zoom').onchange = (e) => { localStorageSet('zoom', e.target.value); applyZoom(); renderSettingsPage(); };
  const panel = $('#ui-zoom-panel');
  // блок масштаба — сразу под заголовком «Настройки»
  Promise.resolve(renderSettings()).then(() => { const h = $('#set-main h2'); if (h && panel) h.insertAdjacentElement('afterend', panel); });
  renderMaterials();
}

// ------------------------------------------------------------------ масштаб интерфейса
// На больших мониторах (ультраширокие, 4K при масштабе Windows 100%) всё выглядит мелко — увеличиваем сами.
function zoomSetting() { return localStorageGet('zoom') || 'auto'; }
function currentZoom() {
  const v = zoomSetting();
  if (v !== 'auto') return +v || 1;
  const w = window.innerWidth;
  return w >= 3200 ? 1.5 : w >= 2400 ? 1.25 : 1;
}
function applyZoom() { document.documentElement.style.zoom = String(currentZoom()); }
applyZoom();
let zoomTimer;
window.addEventListener('resize', () => {
  if (zoomSetting() !== 'auto') return;
  clearTimeout(zoomTimer);
  zoomTimer = setTimeout(() => { const z = String(currentZoom()); if (document.documentElement.style.zoom !== z) applyZoom(); }, 200);
});
$('#nav').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setTab(b.dataset.tab); });

// ------------------------------------------------------------------ принтеры
function renderPrinters() {
  const main = $('#main');
  const open = S.addOpen || !S.order.length;
  main.innerHTML = `<div class="row" style="margin-bottom:12px"><h2 style="margin:0">Принтеры</h2>
    <span class="muted small" id="summary"></span><span class="spacer"></span>
    ${S.order.length ? `<button class="btn ${open ? '' : 'primary'}" id="add-toggle">${open ? '✕ Скрыть поиск' : '🔍 Найти / ＋ добавить принтер'}</button>` : ''}</div>
    <div id="add-panel" class="${open ? '' : 'hidden'}" style="margin-bottom:16px"></div>
    <div class="grid" id="cards"></div>`;
  if ($('#add-toggle')) $('#add-toggle').onclick = () => { S.addOpen = !open; renderPrinters(); };
  if (open) renderDiscover();
  const cards = $('#cards');
  for (const id of S.order) {
    const card = document.createElement('div');
    card.className = 'panel';
    card.id = 'card-' + id;
    card.innerHTML = `<div class="body"></div><div class="transfer muted hidden" id="transfer-${id}"></div><div class="cam-holder"></div>`;
    cards.appendChild(card);
    updatePrinterCard(id);
    if (S.cameraOn.has(id)) showCamera(id, true);
  }
}

function updatePrinterCard(id) {
  const p = S.printers[id];
  const card = $('#card-' + id);
  if (!p || !card) return;
  const st = p.status || {};
  const caps = p.capabilities || {};
  const cfg = p.config;
  const state = st.state || 'offline';
  const active = ['printing', 'paused', 'preparing'].includes(state);
  const temps = Object.entries(st.temps || {}).map(([k, t]) =>
    `<span>${esc(TEMP_RU[k] || k)}: <b>${fmtNum(t.current, 0)}°</b>${t.target ? ` / ${fmtNum(t.target, 0)}°` : ''}</span>`).join('');
  const slots = (st.materials || []).map((m) => `<span class="slot ${m.active ? 'active' : ''}" title="${esc(m.name)}">
      <span class="swatch" style="background:${esc(m.color || '#888')}"></span>${esc(m.type || m.name || m.id)}${m.remain_pct != null && m.remain_pct >= 0 ? ` ${fmtNum(m.remain_pct, 0)}%` : ''}</span>`).join('');
  const progress = st.progress != null ? st.progress : null;
  const queueCount = (p.queue || []).filter((q) => q.status === 'queued').length;
  card.querySelector('.body').innerHTML = `
    <div class="card-head">
      <div style="flex:1;min-width:0">
        <div class="title">${esc(cfg.name)}</div>
        <div class="sub">${esc(cfg.model || p.driver_title || cfg.kind)} · ${esc(cfg.host)}${cfg.technology === 'resin' ? ' · фотополимер' : ''}</div>
      </div>
      <span class="badge ${state}">${STATE_RU[state] || state}</span>
    </div>
    ${active || state === 'finished' ? `
      <div class="progress"><div style="width:${progress ?? 0}%"></div></div>
      <div class="kv">
        <span>Файл</span><span style="overflow-wrap:anywhere">${esc(st.filename || '—')}</span>
        <span>Прогресс</span><span>${progress != null ? fmtNum(progress, 1) + '%' : '—'}${st.layer != null ? ` · слой ${st.layer}${st.total_layers ? ' / ' + st.total_layers : ''}` : ''}</span>
        ${active ? `<span>Осталось</span><span>${fmtDur(st.remaining_s)}${st.remaining_s ? ` · будет готово в ${fmtEta(st.remaining_s)}` : ''}</span>` : ''}
        ${st.elapsed_s ? `<span>Прошло</span><span>${fmtDur(st.elapsed_s)}</span>` : ''}
        ${st.speed_level || st.speed_pct ? `<span>Скорость</span><span>${esc(st.speed_level || '')}${st.speed_pct ? ' ' + st.speed_pct + '%' : ''}</span>` : ''}
      </div>` : ''}
    ${temps ? `<div class="temps">${temps}</div>` : ''}
    ${slots ? `<div class="slots">${slots}</div>` : ''}
    ${st.error ? `<div class="msg err">⚠ ${esc(st.error)}</div>` : ''}
    ${st.message ? `<div class="msg">${esc(st.message)}</div>` : ''}
    ${state === 'offline' && p.last_error ? `<div class="msg err small">${esc(p.last_error)}</div>` : ''}
    <div class="actions">
      ${state === 'printing' || state === 'preparing' ? `<button class="btn small" data-a="pause" ${caps.pause ? '' : 'disabled'}>⏸ Пауза</button>` : ''}
      ${state === 'paused' ? `<button class="btn small primary" data-a="resume" ${caps.resume ? '' : 'disabled'}>▶ Продолжить</button>` : ''}
      ${active ? `<button class="btn small danger" data-a="stop" ${caps.stop ? '' : 'disabled'}>⏹ Стоп</button>` : ''}
      ${caps.light ? `<button class="btn small" data-a="light">💡 ${st.light ? 'Выкл. свет' : 'Вкл. свет'}</button>` : ''}
      ${caps.camera ? `<button class="btn small" data-a="camera">📷 ${S.cameraOn.has(id) ? 'Скрыть камеру' : 'Камера'}</button>` : ''}
      ${caps.files ? `<button class="btn small" data-a="files">📁 Файлы</button>` : ''}
      ${caps.speed && active ? `<button class="btn small" data-a="speed">⚡ Скорость</button>` : ''}
      ${caps.gcode ? `<button class="btn small" data-a="gcode">⌨ G-code</button>` : ''}
      ${['finished', 'cancelled', 'idle'].includes(state) && queueCount ? `<button class="btn small ${p.ready_for_next ? '' : 'primary'}" data-a="ready">${p.ready_for_next ? '✓ Стол свободен' : 'Стол очищен → следующее'}</button>` : ''}
      <span class="spacer"></span>
      ${queueCount ? `<span class="muted small">в очереди: ${queueCount}</span>` : ''}
      <button class="btn small" data-a="edit" title="Настройки принтера">⚙</button>
    </div>`;
  card.querySelectorAll('[data-a]').forEach((b) => { b.onclick = () => printerAction(id, b.dataset.a); });
}

async function printerAction(id, a) {
  const p = S.printers[id];
  if (a === 'pause') return guard(() => api(`/printers/${id}/action`, { method: 'POST', json: { action: 'pause' } }), 'Пауза отправлена');
  if (a === 'resume') return guard(() => api(`/printers/${id}/action`, { method: 'POST', json: { action: 'resume' } }), 'Продолжаем');
  if (a === 'stop') {
    if (!confirmBox(`Остановить печать на «${p.config.name}»? Это нельзя отменить.`)) return;
    return guard(() => api(`/printers/${id}/action`, { method: 'POST', json: { action: 'stop' } }), 'Печать остановлена');
  }
  if (a === 'light') return guard(() => api(`/printers/${id}/action`, { method: 'POST', json: { action: 'light', value: !p.status.light } }));
  if (a === 'camera') { showCamera(id, !S.cameraOn.has(id)); updatePrinterCard(id); return; }
  if (a === 'files') return showPrinterFiles(id);
  if (a === 'edit') return editPrinter(p.config);
  if (a === 'ready') {
    await guard(() => api(`/printers/${id}/action`, { method: 'POST', json: { action: 'ready', value: true } }), 'Отмечено: стол свободен');
    return refreshPrinters();
  }
  if (a === 'speed') {
    const isBambu = p.config.kind === 'bambu';
    const v = window.prompt(isBambu ? 'Режим скорости: 1 — тихий, 2 — стандарт, 3 — спорт, 4 — турбо' : 'Скорость печати, % (100 — норма)', isBambu ? '2' : '100');
    if (v) return guard(() => api(`/printers/${id}/action`, { method: 'POST', json: { action: 'speed', value: v } }), 'Скорость изменена');
  }
  if (a === 'gcode') {
    const v = window.prompt('G-code команда (например G28 или M104 S200):');
    if (v) return guard(() => api(`/printers/${id}/action`, { method: 'POST', json: { action: 'gcode', value: v } }), 'Отправлено');
  }
}

function showCamera(id, on) {
  const holder = $(`#card-${id} .cam-holder`);
  if (!holder) return;
  if (on) {
    S.cameraOn.add(id);
    holder.innerHTML = `<div class="camera"><img alt="Камера принтера" src="/api/printers/${id}/camera.mjpg?t=${Date.now()}"></div>`;
    $('img', holder).onerror = () => { holder.innerHTML = '<div class="msg err">Камера недоступна</div>'; };
  } else {
    S.cameraOn.delete(id);
    holder.innerHTML = '';
  }
}

async function showPrinterFiles(id) {
  const p = S.printers[id];
  const m = modal(`Файлы на принтере — ${p.config.name}`, '<p class="muted">Загрузка…</p>');
  let files;
  try {
    files = await api(`/printers/${id}/files`);
  } catch (e) {
    m.el.innerHTML = `<p class="msg err">Не удалось получить список файлов.<br>${esc(e.message || e)}</p>`;
    return;
  }
  if (!files.length) { m.el.innerHTML = '<p class="muted">Файлов нет</p>'; return; }
  m.el.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Файл</th><th class="num">Размер</th><th>Дата</th><th></th></tr></thead><tbody>
    ${files.map((f, i) => `<tr><td style="overflow-wrap:anywhere">${esc(f.path)}</td><td class="num">${fmtSize(f.size)}</td><td>${fmtDate(f.modified)}</td>
      <td style="white-space:nowrap"><button class="btn small primary" data-print="${i}">▶ Печать</button>
      ${p.capabilities.delete ? `<button class="btn small danger" data-del="${i}">🗑</button>` : ''}</td></tr>`).join('')}
    </tbody></table></div>`;
  m.el.querySelectorAll('[data-print]').forEach((b) => {
    b.onclick = async () => {
      const f = files[+b.dataset.print];
      if (!confirmBox(`Запустить печать «${f.name}»? Убедитесь, что стол свободен.`)) return;
      await guard(() => api(`/printers/${id}/print`, { method: 'POST', json: { path: f.path, options: {} } }), 'Печать запущена');
      m.close();
    };
  });
  m.el.querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = async () => {
      const f = files[+b.dataset.del];
      if (!confirmBox(`Удалить «${f.name}» с принтера?`)) return;
      await guard(() => api(`/printers/${id}/files?path=${encodeURIComponent(f.path)}`, { method: 'DELETE' }), 'Удалено');
      m.close(); showPrinterFiles(id);
    };
  });
}

// ------------------------------------------------------------------ поиск и добавление
const PRESETS = {
  'Bambu Lab X1 Carbon': { kind: 'bambu', build_volume: [256, 256, 256], power_w: 200 },
  'Bambu Lab X1E': { kind: 'bambu', build_volume: [256, 256, 256], power_w: 220 },
  'Bambu Lab P1S': { kind: 'bambu', build_volume: [256, 256, 256], power_w: 150 },
  'Bambu Lab P1P': { kind: 'bambu', build_volume: [256, 256, 256], power_w: 140 },
  'Bambu Lab P2S': { kind: 'bambu', build_volume: [256, 256, 256], power_w: 160 },
  'Bambu Lab A1': { kind: 'bambu', build_volume: [256, 256, 256], power_w: 110 },
  'Bambu Lab A1 mini': { kind: 'bambu', build_volume: [180, 180, 180], power_w: 80 },
  'Bambu Lab H2D': { kind: 'bambu', build_volume: [325, 320, 325], power_w: 250 },
  'Bambu Lab H2S': { kind: 'bambu', build_volume: [340, 320, 340], power_w: 230 },
  'Elegoo Centauri Carbon': { kind: 'elegoo_sdcp', build_volume: [256, 256, 256], power_w: 150 },
  'Elegoo Centauri Carbon 2': { kind: 'elegoo_cc2', build_volume: [256, 256, 256], power_w: 160 },
  'Elegoo Centauri 2': { kind: 'elegoo_cc2', build_volume: [256, 256, 256], power_w: 150 },
  'Elegoo Saturn 4 Ultra': { kind: 'elegoo_sdcp', technology: 'resin', build_volume: [218, 123, 220], power_w: 70 },
  'Elegoo Saturn 4 Ultra 16K': { kind: 'elegoo_sdcp', technology: 'resin', build_volume: [211, 118, 220], power_w: 70 },
  'Elegoo Mars 5 Ultra': { kind: 'elegoo_sdcp', technology: 'resin', build_volume: [153, 77, 165], power_w: 50 },
  'Elegoo Neptune 4 / 4 Pro': { kind: 'moonraker', build_volume: [225, 225, 265], power_w: 140 },
  'Elegoo Neptune 4 Plus': { kind: 'moonraker', build_volume: [320, 320, 385], power_w: 180 },
  'Elegoo Neptune 4 Max': { kind: 'moonraker', build_volume: [420, 420, 480], power_w: 250 },
  'Anycubic Kobra 3 / 3 V2': { kind: 'anycubic_lan', build_volume: [250, 250, 260], power_w: 140 },
  'Anycubic Kobra 3 Max': { kind: 'anycubic_lan', build_volume: [420, 420, 500], power_w: 250 },
  'Anycubic Kobra S1': { kind: 'anycubic_lan', build_volume: [250, 250, 250], power_w: 160 },
  'Anycubic Kobra S1 Max': { kind: 'anycubic_lan', build_volume: [340, 340, 340], power_w: 200 },
  'Anycubic Kobra 4': { kind: 'anycubic_lan', build_volume: [250, 250, 260], power_w: 150 },
  'Anycubic Kobra X': { kind: 'anycubic_lan', build_volume: [260, 260, 260], power_w: 150 },
  'Anycubic Kobra с Rinkhals (Klipper)': { kind: 'moonraker', build_volume: [250, 250, 260], power_w: 150 },
  'Anycubic Photon P1': { kind: 'anycubic_lan', technology: 'resin', build_volume: [223, 126, 230], power_w: 60 },
  'Anycubic Photon Mono X / X 6K / SE': { kind: 'anycubic_photon', technology: 'resin', build_volume: [192, 120, 245], power_w: 60 },
  'Anycubic Kobra 2 / Kobra (через OctoPrint)': { kind: 'octoprint', build_volume: [220, 220, 250], power_w: 130 },
};

async function renderDiscover() {
  if (!S.drivers.length) S.drivers = await api('/drivers');
  const el = view('add-panel');
  el.innerHTML = `
  <div class="cols">
    <div>
      <div class="panel">
        <div class="row"><h3 style="margin:0">Поиск в локальной сети</h3><span class="spacer"></span>
          <label class="row small muted"><input type="checkbox" id="scan" checked> сканировать подсеть</label>
          <input id="subnet" value="${esc(S.discSubnet)}" placeholder="${esc(S.info.subnet || '192.168.1.0/24')}" style="width:160px" title="Подсеть для сканирования">
          <button class="btn primary" id="discover-btn">🔍 Найти принтеры</button></div>
        <p class="muted small">Bambu Lab находятся по SSDP, Elegoo — по SDCP и UDP-запросу Centauri 2, Anycubic, Klipper и OctoPrint — сканированием портов.
          Программа должна работать на компьютере в той же сети, что и принтеры. Поиск занимает до 15 секунд.</p>
        <div id="found"></div>
      </div>
    </div>
    <div>
      <div class="panel"><h3>Добавить вручную</h3>
        <label class="field">Модель (заполнит тип и размеры)
          <select id="preset"><option value="">— выберите —</option>${Object.keys(PRESETS).map((k) => `<option>${esc(k)}</option>`).join('')}</select></label>
        <div style="margin-top:10px"><button class="btn primary" id="manual-btn">Заполнить данные…</button></div>
        <details class="muted small" style="margin-top:12px"><summary>Какие принтеры поддерживаются и что для них нужно</summary>
          ${S.drivers.map((d) => `<p><b>${esc(d.title)}</b> — ${esc(d.help)}</p>`).join('')}</details>
      </div>
    </div>
  </div>`;
  $('#discover-btn').onclick = runDiscovery;
  $('#subnet').oninput = () => { S.discSubnet = $('#subnet').value; };
  if (S.found) renderFound(S.found);
  $('#manual-btn').onclick = () => {
    const name = $('#preset').value;
    const pr = PRESETS[name] || { kind: 'bambu' };
    editPrinter({ name: name || '', model: name || '', technology: 'fdm', ...pr });
  };
}

async function runDiscovery() {
  const btn = $('#discover-btn');
  btn.disabled = true; btn.textContent = 'Ищу… (до 15 с)';
  $('#found').innerHTML = '';
  const q = new URLSearchParams({ scan: $('#scan').checked, subnet: $('#subnet').value.trim() });
  const res = await guard(() => api('/discover?' + q));
  if (!$('#discover-btn')) return;           // пользователь ушёл со страницы
  btn.disabled = false; btn.textContent = '🔍 Найти принтеры';
  if (!res) return;
  S.found = res;
  renderFound(res);
}

function renderFound(res) {
  if (!$('#found')) return;
  if (!res.printers.length) {
    $('#found').innerHTML = `<div class="msg">Ничего не найдено в ${esc(res.subnet || 'сети')} за ${res.took_s} с.
      Проверьте, что компьютер и принтеры в одной сети и принтеры включены. Bambu Lab: включите «LAN Only» и «Режим разработчика».</div>`;
    return;
  }
  const kindTitle = Object.fromEntries(S.drivers.map((d) => [d.kind, d.title]));
  $('#found').innerHTML = `<p class="muted small">Подсеть ${esc(res.subnet)}, ${res.took_s} с</p>
    <div class="table-wrap"><table><thead><tr><th>Принтер</th><th>Адрес</th><th>Протокол</th><th></th></tr></thead><tbody>
    ${res.printers.map((d, i) => `<tr><td><b>${esc(d.name || d.model || d.host)}</b><div class="small muted">${esc(d.model)} ${d.serial ? '· ' + esc(d.serial) : ''} ${d.firmware ? '· ' + esc(d.firmware) : ''}</div>
      ${d.notes ? `<div class="small muted">${esc(d.notes)}</div>` : ''}</td>
      <td>${esc(d.host)}</td><td class="small">${esc(kindTitle[d.kind] || d.kind)}</td>
      <td>${d.already_added || Object.values(S.printers).some((p) => p.config.host === d.host) ? '<span class="muted small">добавлен</span>' : `<button class="btn small primary" data-add="${i}">Добавить</button>`}</td></tr>`).join('')}
    </tbody></table></div>`;
  $$('[data-add]').forEach((b) => {
    b.onclick = () => {
      const d = res.printers[+b.dataset.add];
      const preset = Object.entries(PRESETS).find(([k]) => d.model && k.toLowerCase().includes(String(d.model).toLowerCase()));
      editPrinter({ kind: d.kind, host: d.host, name: d.name || d.model || d.host, model: d.model, serial: d.serial,
        technology: d.technology, port: d.port || 0, ...(preset ? { build_volume: preset[1].build_volume, power_w: preset[1].power_w } : {}) }, d.needs);
    };
  });
}

function editPrinter(cfg, needs = []) {
  const isNew = !cfg.id;
  const kinds = S.drivers.length ? S.drivers : [];
  const bv = cfg.build_volume || [0, 0, 0];
  const m = modal(isNew ? 'Добавить принтер' : `Настройки — ${cfg.name}`, `
    <div class="form">
      <label class="field">Тип подключения<select name="kind">${kinds.map((d) => `<option value="${d.kind}" ${d.kind === cfg.kind ? 'selected' : ''}>${esc(d.title)}</option>`).join('')}</select></label>
      <label class="field">Название<input name="name" value="${esc(cfg.name || '')}" placeholder="Например: Bambu #1"></label>
      <label class="field">IP-адрес<input name="host" value="${esc(cfg.host || '')}" placeholder="192.168.1.50"></label>
      <label class="field">Модель<input name="model" value="${esc(cfg.model || '')}"></label>
      <label class="field" data-for="bambu elegoo_sdcp elegoo_cc2">Серийный номер (пусто — определить автоматически)<input name="serial" value="${esc(cfg.serial || '')}"></label>
      <label class="field" data-for="bambu elegoo_cc2">Код доступа${cfg.kind === 'elegoo_cc2' ? ' (если задан на принтере)' : ' (LAN Access Code)'}<input name="access_code" value="${esc(cfg.access_code || '')}" ${needs.includes('access_code') ? 'autofocus' : ''}></label>
      <label class="field" data-for="octoprint moonraker">API-ключ${cfg.kind === 'moonraker' ? ' (если включена авторизация)' : ''}<input name="api_key" value="${esc(cfg.api_key || '')}"></label>
      <label class="field">Порт (0 — по умолчанию)<input name="port" type="number" value="${cfg.port || 0}"></label>
      <label class="field">Технология<select name="technology"><option value="fdm" ${cfg.technology !== 'resin' ? 'selected' : ''}>FDM (пластик)</option><option value="resin" ${cfg.technology === 'resin' ? 'selected' : ''}>Фотополимер (смола)</option></select></label>
      <label class="field">Область печати X × Y × Z, мм<span class="row"><input name="bx" type="number" value="${bv[0] || ''}" style="width:70px"><input name="by" type="number" value="${bv[1] || ''}" style="width:70px"><input name="bz" type="number" value="${bv[2] || ''}" style="width:70px"></span></label>
      <label class="field">Мощность, Вт (для расчёта)<input name="power_w" type="number" value="${cfg.power_w || ''}"></label>
      ${!isNew ? `<label class="field">Включён<select name="enabled"><option value="1" ${cfg.enabled !== false ? 'selected' : ''}>да</option><option value="0" ${cfg.enabled === false ? 'selected' : ''}>нет</option></select></label>` : ''}
    </div>
    <div class="msg small" id="kind-help"></div>
    <div class="row" style="margin-top:14px">
      <button class="btn" id="test">Проверить соединение</button>
      <button class="btn" id="diag" title="Пошаговая проверка: сеть, порты, вход, FTP">🩺 Диагностика</button>
      <span id="test-res" class="small"></span><span class="spacer"></span>
      ${!isNew ? '<button class="btn danger" id="del">Удалить</button>' : ''}
      <button class="btn primary" id="save">${isNew ? 'Добавить' : 'Сохранить'}</button>
    </div>`);
  const f = m.el;
  const val = () => {
    const g = (n) => f.querySelector(`[name="${n}"]`)?.value ?? '';
    return {
      ...(cfg.id ? { id: cfg.id } : {}), kind: g('kind'), name: g('name') || g('host'), host: g('host').trim(), model: g('model'),
      serial: g('serial').trim(), access_code: g('access_code').trim(), api_key: g('api_key').trim(), port: +g('port') || 0,
      technology: g('technology'), build_volume: [+g('bx') || 0, +g('by') || 0, +g('bz') || 0], power_w: +g('power_w') || 0,
      enabled: g('enabled') === '' ? true : g('enabled') === '1',
    };
  };
  const updKind = () => {
    const k = f.querySelector('[name=kind]').value;
    f.querySelectorAll('[data-for]').forEach((el) => el.classList.toggle('hidden', !el.dataset.for.split(' ').includes(k)));
    const d = kinds.find((x) => x.kind === k);
    $('#kind-help', f).textContent = d ? d.help : '';
  };
  f.querySelector('[name=kind]').onchange = updKind;
  updKind();
  $('#test', f).onclick = async () => {
    $('#test-res', f).textContent = 'проверяю…';
    const r = await guard(() => api('/printers/test', { method: 'POST', json: val() }));
    if (r) {
      $('#test-res', f).innerHTML = r.ok ? `<span style="color:var(--ok)">✓ Связь есть</span> ${esc(r.model || r.version || r.state || '')}${r.warning ? `<br><span style="color:var(--warn)">⚠ ${esc(r.warning)}</span>` : ''}` : `<span style="color:var(--err)">✗ ${esc(r.error)}</span>`;
      if (r.ok && r.serial && !f.querySelector('[name=serial]').value) f.querySelector('[name=serial]').value = r.serial;
      if (r.ok && r.model && !f.querySelector('[name=model]').value) f.querySelector('[name=model]').value = r.model;
    }
    else $('#test-res', f).textContent = '';
  };
  $('#diag', f).onclick = async () => {
    const d = modal('Диагностика подключения', '<p class="muted">Проверяю сеть, порты и вход… (до минуты)</p>');
    const r = await guard(() => api('/printers/diagnose', { method: 'POST', json: val() }));
    if (!r) { d.close(); return; }
    d.el.innerHTML = `<ul class="issues">${r.steps.map((s) => `<li class="${s.ok ? 'ok' : 'error'}">${s.ok ? '✓' : '✗'} <b>${esc(s.step)}</b>: ${esc(s.detail)}</li>`).join('')}</ul>
      <p class="small muted" style="margin-top:10px">${r.ok ? 'Всё в порядке.' : 'Пришлите скриншот этого окна, если не получается исправить самостоятельно.'}</p>`;
  };
  $('#save', f).onclick = async () => {
    const body = val();
    const r = await guard(() => (isNew ? api('/printers', { method: 'POST', json: body }) : api(`/printers/${cfg.id}`, { method: 'PUT', json: body })), 'Сохранено');
    if (r) { m.close(); await refreshPrinters(); }
  };
  if (!isNew) {
    $('#del', f).onclick = async () => {
      if (!confirmBox(`Удалить принтер «${cfg.name}» из списка?`)) return;
      await guard(() => api(`/printers/${cfg.id}`, { method: 'DELETE' }), 'Удалён');
      m.close(); refreshPrinters();
    };
  }
}

// ------------------------------------------------------------------ библиотека
async function renderLibrary() {
  const items = await api('/library');
  view('models-body').innerHTML = `
    ${modelsTitle('Модели')}
    <label class="drop" id="drop">Перетащите сюда файлы или нажмите для выбора<br>
      <span class="small">G-code, 3MF (Bambu/Orca/Elegoo/Anycubic), STL/OBJ/3MF-модели, файлы смолы (.goo, .ctb, .pwmx, .pm4n, .pm5…)</span>
      <input type="file" id="file" multiple hidden></label>
    <div id="uploads" class="small muted" style="margin:8px 0"></div>
    <div class="panel" style="margin-top:12px">
    ${items.length ? `<div class="table-wrap"><table><thead><tr><th></th><th>Файл</th><th>Тип</th><th class="num">Время</th><th class="num">Материал</th><th class="num">Размер</th><th>Добавлен</th><th></th></tr></thead><tbody>
      ${items.map((it) => {
        const s = it.summary || {};
        const mat = s.material_ml ? fmtNum(s.material_ml, 1) + ' мл' : s.material_g ? fmtNum(s.material_g, 1) + ' г' : '—';
        return `<tr><td>${it.has_thumb ? `<img class="thumb" loading="lazy" src="/api/library/${it.id}/thumbnail" alt="">` : '<div class="thumb"></div>'}</td>
          <td style="overflow-wrap:anywhere"><b>${esc(it.filename)}</b>${it.analysis.error ? `<div class="small" style="color:var(--err)">${esc(it.analysis.error)}</div>` : ''}${it.plates > 1 ? `<div class="small muted">пластин: ${it.plates}</div>` : ''}</td>
          <td class="small">${esc(KIND_RU[it.kind] || it.kind)}</td>
          <td class="num">${s.time_s ? fmtDur(s.time_s) : '—'}${it.kind === 'mesh' && s.time_s ? '*' : ''}</td><td class="num">${mat}</td>
          <td class="num">${fmtSize(it.size)}</td><td class="small">${fmtDate(it.uploaded_at)}</td>
          <td style="white-space:nowrap">
            ${it.kind !== 'mesh' && it.kind !== 'other' ? `<button class="btn small primary" data-send="${it.id}">▶ На печать</button>` : ''}
            ${it.kind === 'mesh' ? `<button class="btn small primary" data-prep="${it.id}">🛠 Подготовка</button>
              ${/_(opalubka|forma)\.3mf$/i.test(it.filename) ? '' : `<button class="btn small" data-mold="${it.id}" title="Опалубка под силикон или разъёмная форма для литья">🧱 Опалубка</button>`}` : ''}
            <button class="btn small" data-info="${it.id}" title="Подробности">ℹ</button>
            <button class="btn small" data-cost="${it.id}" title="Стоимость">₽</button>
            <a class="btn small" href="/api/library/${it.id}/download" title="Скачать">⬇</a>
            <button class="btn small danger" data-del="${it.id}" title="Удалить">🗑</button></td></tr>`;
      }).join('')}</tbody></table></div>
      <p class="small muted">* для 3D-моделей время и расход — приблизительная оценка до нарезки.</p>` : '<div class="empty">Библиотека пуста</div>'}
    </div>`;
  const drop = $('#drop'), input = $('#file');
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); uploadFiles(e.dataTransfer.files); });
  input.onchange = () => uploadFiles(input.files);
  $$('[data-send]').forEach((b) => { b.onclick = () => sendDialog(b.dataset.send); });
  $$('[data-prep]').forEach((b) => { b.onclick = () => { S.prepare.id = b.dataset.prep; localStorageSet('prepare_id', S.prepare.id); setTab('prepare'); }; });
  $$('[data-info]').forEach((b) => { b.onclick = () => itemInfo(b.dataset.info); });
  $$('[data-mold]').forEach((b) => { b.onclick = () => moldDialog(b.dataset.mold); });
  $$('[data-cost]').forEach((b) => { b.onclick = () => { S.calcItem = b.dataset.cost; setTab('calc'); }; });
  $$('[data-del]').forEach((b) => {
    b.onclick = async () => { if (confirmBox('Удалить файл из библиотеки?')) { await guard(() => api(`/library/${b.dataset.del}`, { method: 'DELETE' })); renderLibrary(); } };
  });
}

function uploadFiles(files) {
  const box = $('#uploads');
  return Promise.all([...files].map((file) => new Promise((resolve) => {
    const row = document.createElement('div');
    row.textContent = `⬆ ${file.name}: 0%`;
    box.appendChild(row);
    const fd = new FormData();
    fd.append('file', file);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/library');
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) row.textContent = `⬆ ${file.name}: ${Math.round((e.loaded / e.total) * 100)}%${e.loaded === e.total ? ' — анализ…' : ''}`; };
    xhr.onload = () => {
      if (xhr.status < 300) { row.textContent = `✓ ${file.name}`; } else { row.textContent = `✗ ${file.name}: ${xhr.responseText}`; }
      resolve();
    };
    xhr.onerror = () => { row.textContent = `✗ ${file.name}: ошибка сети`; resolve(); };
    xhr.send(fd);
  }))).then(() => { if (S.tab === 'library') renderLibrary(); });
}

// ------------------------------------------------------------------ опалубка / форма для литья
async function moldDialog(id) {
  const it = await api(`/library/${id}`);
  const mats = await api('/mold/materials');
  const m = modal(`Опалубка — ${it.filename}`, `
    <div class="row" style="gap:8px;margin-bottom:12px">
      <label class="row"><input type="radio" name="mk" value="silicone_box" checked> <b>Опалубка под силикон</b></label>
      <label class="row"><input type="radio" name="mk" value="two_part"> <b>Разъёмная форма для литья</b></label>
    </div>
    <p class="small muted" id="mk-help"></p>
    <div class="form" data-k="silicone_box">
      <label class="field">Зазор до модели (толщина силикона), мм<input name="gap" type="number" value="8" step="0.5" min="3"></label>
      <label class="field">Силикон над моделью, мм<input name="over" type="number" value="8" step="0.5" min="3"></label>
      <label class="field">Толщина стенок опалубки, мм<input name="wall" type="number" value="2.4" step="0.2" min="1.2"></label>
      <label class="row small"><input type="checkbox" name="bolts" checked> проушины под болты M3</label>
      <label class="row small"><input type="checkbox" name="with_base"> печатать дно (обычно не нужно — стенки ставятся на стол)</label>
    </div>
    <div class="form hidden" data-k="two_part">
      <label class="field">Что заливать<select name="material">${mats.filter((x) => x.id !== 'silicone').map((x) => `<option value="${x.id}">${esc(x.name)} — ${x.density} г/см³</option>`).join('')}</select></label>
      <label class="field">Толщина формы, мм<input name="wall2" type="number" value="10" step="1" min="6"></label>
      <label class="field">Высота разъёма от низа модели, мм (пусто — авто по самому широкому месту)<input name="split" type="number" step="0.5" placeholder="авто"></label>
      <label class="field">Диаметр литника, мм<input name="sprue_d" type="number" value="6" step="0.5" min="3"></label>
      <label class="row small"><input type="checkbox" name="vents" checked> воздушники</label>
      <label class="row small"><input type="checkbox" name="keys" checked> замки-полусферы</label>
      <label class="row small"><input type="checkbox" name="bolts2" checked> отверстия под болты</label>
    </div>
    <div id="mold-res" style="margin-top:12px"></div>
    <div class="row" style="margin-top:14px"><span class="spacer"></span><button class="btn primary" id="mold-go">Создать</button></div>`);
  const f = m.el;
  const HELP = {
    silicone_box: 'Стенки-короб вокруг мастер-модели из двух половин на болтах: ставите на ровный стол вокруг модели, заливаете силикон — получаете силиконовую форму.',
    two_part: 'Готовая форма из двух половин: литник с воронкой, воздушники, замки, пазы для разъёма. Сразу лейте гипс, бетон, смолу или воск.',
  };
  const kind = () => f.querySelector('[name=mk]:checked').value;
  const upd = () => {
    f.querySelectorAll('[data-k]').forEach((el) => el.classList.toggle('hidden', el.dataset.k !== kind()));
    $('#mk-help', f).textContent = HELP[kind()];
  };
  f.querySelectorAll('[name=mk]').forEach((r) => { r.onchange = upd; });
  upd();
  const g = (n) => f.querySelector(`[name="${n}"]`);
  $('#mold-go', f).onclick = async () => {
    const k = kind();
    const options = k === 'silicone_box'
      ? { gap: +g('gap').value, over: +g('over').value, wall: +g('wall').value, bolts: g('bolts').checked, with_base: g('with_base').checked }
      : { material: g('material').value, wall: +g('wall2').value, split: g('split').value === '' ? 'auto' : +g('split').value,
          sprue_d: +g('sprue_d').value, vents: g('vents').checked, keys: g('keys').checked, bolts: g('bolts2').checked };
    const btn = $('#mold-go', f); btn.disabled = true; btn.textContent = 'Строю…';
    const r = await guard(() => api(`/library/${id}/mold`, { method: 'POST', json: { kind: k, options } }));
    btn.disabled = false; btn.textContent = 'Создать заново';
    if (!r) return;
    const i = r.info;
    const amount = k === 'silicone_box'
      ? `<li>Силикона: <b>${fmtNum(i.silicone_g, 0)} г</b> (${fmtNum(i.silicone_cm3, 0)} см³), с запасом 10% — <b>${fmtNum(i.silicone_g_with_reserve, 0)} г</b></li>
         <li>Уровень заливки: ${fmtNum(i.fill_height_mm, 0)} мм от дна, внутренний размер ${i.inner_mm.join(' × ')} мм</li>`
      : `<li>Разъём на высоте <b>${fmtNum(i.split_mm, 1)} мм</b>${i.split_auto ? ' (по самому широкому месту)' : ''}</li>
         <li>Материала на отливку: <b>${fmtNum(i.cast_g, 0)} г</b> (${fmtNum(i.cast_cm3, 1)} см³), с запасом — ${fmtNum(i.cast_g_with_reserve, 0)} г</li>
         <li>Размер формы: ${i.mold_mm.join(' × ')} мм, воздушников: ${i.vents}</li>`;
    $('#mold-res', f).innerHTML = `<div class="panel">
      <p>✅ Готово: <b>${esc(r.filename)}</b> — добавлен в «Модели», детали уже разложены на столе.</p>
      ${r.warnings.map((w) => `<div class="msg" style="color:var(--warn)">⚠ ${esc(w)}</div>`).join('')}
      <ul class="small">${amount}${i.bolts ? `<li>Крепёж: ${esc(i.bolts)}</li>` : ''}
        <li>Детали: ${r.parts.map((p) => `${esc(p.name)} (${p.size_mm.join('×')} мм)`).join(', ')}</li></ul>
      <details class="small"><summary>Как пользоваться</summary><ol>${i.steps.map((s) => `<li>${esc(s)}</li>`).join('')}</ol></details>
      <div class="row" style="margin-top:10px"><a class="btn small" href="/api/library/${r.item_id}/download">⬇ Скачать 3MF</a>
        <button class="btn small primary" id="mold-prep">✨ Подготовить к печати</button></div></div>`;
    $('#mold-prep', f).onclick = () => { m.close(); S.prepare.id = r.item_id; localStorageSet('prepare_id', r.item_id); setTab('models', 'prepare'); };
  };
}

async function itemInfo(id) {
  const it = await api(`/library/${id}`);
  const a = it.analysis || {};
  let html = '';
  if (a.kind === 'gcode') {
    const g = a.gcode;
    html = `<div class="kv">
      <span>Слайсер</span><span>${esc(g.slicer.slicer || '?')} ${esc(g.slicer.version || '')}</span>
      <span>Принтер в профиле</span><span>${esc(g.slicer.printer_model || '—')}</span>
      <span>Время</span><span>${fmtDur(g.time_s)} <span class="muted small">(${g.time_source === 'slicer' ? 'по данным слайсера' : 'по симуляции'})</span></span>
      <span>Симуляция</span><span>${fmtDur(g.sim_time_s)}</span>
      <span>Филамент</span><span>${fmtNum(g.filament_mm / 1000, 2)} м · ${fmtNum(g.filament_g, 1)} г · ${fmtNum(g.filament_cm3, 1)} см³</span>
      <span>Материал</span><span>${esc((g.slicer.filament_type || []).join(', ') || '—')}</span>
      <span>Слоёв</span><span>${g.layers} · высота ${fmtNum(g.max_z, 2)} мм${g.slicer.layer_height ? ` · слой ${g.slicer.layer_height} мм` : ''}</span>
      <span>Габариты печати</span><span>${g.bbox_min.map((v, i) => fmtNum(g.bbox_max[i] - v, 1)).join(' × ')} мм</span>
      <span>Смен инструмента</span><span>${g.tool_changes}</span>
      <span>Строк</span><span>${fmtNum(g.lines, 0)}</span></div>
      ${(g.warnings || []).map((w) => `<div class="msg">${esc(w)}</div>`).join('')}`;
  } else if (a.kind === '3mf') {
    const t = a.threemf;
    html = `<p>Принтер в проекте: <b>${esc(t.printer_model || '—')}</b></p><div class="table-wrap"><table><thead><tr><th>Пластина</th><th class="num">Время</th><th class="num">Вес</th><th>Филаменты</th></tr></thead><tbody>
      ${t.plates.map((p) => `<tr><td>№${p.index}${p.objects.length ? `<div class="small muted">${esc(p.objects.slice(0, 5).join(', '))}</div>` : ''}</td><td class="num">${fmtDur(p.prediction_s)}</td><td class="num">${fmtNum(p.weight_g, 1)} г</td>
      <td>${p.filaments.map((f) => `<span class="slot"><span class="swatch" style="background:${esc(f.color)}"></span>${esc(f.type)} ${fmtNum(f.used_g, 1)} г</span>`).join(' ')}</td></tr>`).join('')}
      </tbody></table></div>`;
  } else if (a.kind === 'resin') {
    const r = a.resin;
    html = `<div class="kv">${Object.entries(r).filter(([, v]) => typeof v !== 'object').map(([k, v]) => `<span>${esc(k)}</span><span>${esc(typeof v === 'number' ? fmtNum(v, 3) : v)}</span>`).join('')}</div>`;
  } else if (a.kind === 'mesh') {
    html = meshReportHtml(a.mesh);
  }
  if (a.error) html += `<div class="msg err">${esc(a.error)}</div>`;
  modal(it.filename, html || '<p class="muted">Нет данных</p>');
}

async function sendDialog(id) {
  const it = await api(`/library/${id}`);
  const list = S.order.map((pid) => S.printers[pid]).filter(Boolean);
  if (!list.length) { toast('Сначала добавьте принтер', 'error'); return; }
  if (!S.materials.length) S.materials = await api('/materials');
  const plates = it.analysis?.threemf?.plates || [];
  const m = modal(`На печать: ${it.filename}`, `
    <div class="form">
      <label class="field">Принтер<select name="printer">${list.map((p) => `<option value="${p.config.id}">${esc(p.config.name)} — ${STATE_RU[p.status?.state] || ''}</option>`).join('')}</select></label>
      ${plates.length > 1 ? `<label class="field">Пластина<select name="plate">${plates.map((p) => `<option value="${p.index}">№${p.index} · ${fmtDur(p.prediction_s)} · ${fmtNum(p.weight_g, 1)} г</option>`).join('')}</select></label>` : ''}
      <label class="field">Материал (для учёта и стоимости)<select name="material"><option value="">—</option>${S.materials.map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select></label>
    </div>
    <div id="bambu-opts" class="row" style="margin-top:10px">
      <label class="row small"><input type="checkbox" name="use_ams" checked> AMS</label>
      <label class="row small"><input type="checkbox" name="bed_leveling" checked> Калибровка стола</label>
      <label class="row small"><input type="checkbox" name="flow_cali"> Калибровка потока</label>
      <label class="row small"><input type="checkbox" name="timelapse"> Таймлапс</label>
    </div>
    <div id="elegoo-opts" class="row hidden" style="margin-top:10px">
      <label class="row small"><input type="checkbox" name="calibration" checked> Автокалибровка</label>
      <label class="row small"><input type="checkbox" name="tlp"> Таймлапс</label>
    </div>
    <div id="warn" class="msg hidden"></div>
    <div class="row" style="margin-top:14px"><span class="spacer"></span>
      <button class="btn" id="upload-only">Только загрузить</button>
      <button class="btn" id="to-queue">В очередь</button>
      <button class="btn primary" id="go">▶ Загрузить и печатать</button></div>`);
  const f = m.el;
  const g = (n) => f.querySelector(`[name="${n}"]`);
  const upd = () => {
    const p = S.printers[g('printer').value];
    $('#bambu-opts', f).classList.toggle('hidden', p.config.kind !== 'bambu');
    $('#elegoo-opts', f).classList.toggle('hidden', !['elegoo_sdcp', 'elegoo_cc2', 'anycubic_lan'].includes(p.config.kind) || p.config.technology === 'resin');
    const w = $('#warn', f);
    const accepts = p.accepts || [];
    const name = it.filename.toLowerCase();
    const ok = accepts.some((e) => name.endsWith(e)) || (name.endsWith('.3mf') && accepts.includes('.gcode'));
    const busy = ['printing', 'paused', 'preparing'].includes(p.status?.state);
    w.classList.toggle('hidden', ok && !busy && p.status?.state !== 'offline');
    w.textContent = !ok ? `Этот принтер принимает: ${accepts.join(', ')}` : busy ? 'Принтер сейчас печатает — можно только загрузить файл или поставить в очередь.' : p.status?.state === 'offline' ? 'Принтер не в сети.' : '';
    const model = (it.analysis?.gcode?.slicer?.printer_model || it.analysis?.threemf?.printer_model || '').toLowerCase();
    if (ok && model && p.config.model && !model.includes(p.config.model.toLowerCase().replace('bambu lab ', '')) && !p.config.model.toLowerCase().includes(model.replace('bambu lab ', ''))) {
      w.classList.remove('hidden');
      w.textContent = `⚠ Файл нарезан для «${model}», а выбран «${p.config.model}». Проверьте совместимость!`;
    }
  };
  g('printer').onchange = upd;
  upd();
  const opts = () => {
    const p = S.printers[g('printer').value];
    if (p.config.kind === 'bambu') return { use_ams: g('use_ams').checked, bed_leveling: g('bed_leveling').checked, flow_cali: g('flow_cali').checked, timelapse: g('timelapse').checked };
    if (['elegoo_sdcp', 'elegoo_cc2'].includes(p.config.kind)) return { calibration: g('calibration').checked, timelapse: g('tlp').checked };
    if (p.config.kind === 'anycubic_lan') return { bed_leveling: g('calibration').checked, timelapse: g('tlp').checked };
    return {};
  };
  const send = async (start) => {
    const body = { printer_id: g('printer').value, start, plate: g('plate') ? +g('plate').value : 1, options: opts(), material_id: g('material').value ? +g('material').value : null };
    if (start && !confirmBox('Убедитесь, что стол принтера свободен и материал заправлен. Запустить печать?')) return;
    const r = await guard(() => api(`/library/${id}/send`, { method: 'POST', json: body }));
    if (r) { m.close(); toast('Отправка начата — прогресс на карточке принтера'); setTab('printers'); }
  };
  $('#go', f).onclick = () => send(true);
  $('#upload-only', f).onclick = () => send(false);
  $('#to-queue', f).onclick = async () => {
    const o = opts(); o.material_id = g('material').value ? +g('material').value : null;
    const r = await guard(() => api('/queue', { method: 'POST', json: { library_id: id, printer_id: g('printer').value, plate: g('plate') ? +g('plate').value : 1, options: o, auto_start: false } }), 'Добавлено в очередь');
    if (r) m.close();
  };
}

// ------------------------------------------------------------------ подготовка модели
function meshReportHtml(r) {
  if (!r) return '';
  const issues = r.issues && r.issues.length ? r.issues : [{ severity: 'ok', text: 'Проблем не найдено — модель готова к нарезке' }];
  return `<div class="kv">
      <span>Размер</span><span><b>${r.size_mm.map((v) => fmtNum(v, 2)).join(' × ')}</b> мм</span>
      <span>Объём</span><span>${fmtNum(r.volume_cm3, 2)} см³</span>
      <span>Площадь</span><span>${fmtNum(r.area_cm2, 1)} см²</span>
      <span>Треугольников</span><span>${fmtNum(r.faces, 0)}</span>
      <span>Тел</span><span>${r.bodies}</span>
      <span>Замкнута</span><span>${r.watertight ? '✓ да' : '✗ нет'}</span>
      <span>Нависания</span><span>${fmtNum(r.overhang_pct, 1)}% (${fmtNum(r.overhang_area_cm2, 1)} см²)</span>
      <span>Контакт со столом</span><span>${fmtNum(r.bed_contact_cm2, 2)} см²</span>
      ${r.fits_build_volume !== null ? `<span>Помещается</span><span>${r.fits_build_volume ? '✓ да' : '✗ нет'}</span>` : ''}
    </div>
    <ul class="issues" style="margin-top:10px">${issues.map((i) => `<li class="${i.severity}">${esc(i.text)}${i.count ? `: <b>${i.count}</b>` : ''}</li>`).join('')}</ul>`;
}

async function renderPrepare() {
  const items = (await api('/library')).filter((i) => i.kind === 'mesh');
  const printers = S.order.map((id) => S.printers[id]).filter(Boolean);
  if (!items.length) {
    view('models-body').innerHTML = `<div class="panel empty"><h3>Нет 3D-моделей</h3><p>Загрузите STL, OBJ или 3MF в списке моделей.</p><button class="btn primary" id="go-lib">К списку моделей</button></div>`;
    $('#go-lib').onclick = () => setTab('library');
    return;
  }
  if (!items.find((i) => i.id === S.prepare.id)) S.prepare.id = items[0].id;
  const info = S.info.slicers || [];
  const pr = S.printers[S.prepare.printer];
  view('models-body').innerHTML = `
  <div class="row" style="margin-bottom:12px;gap:12px;align-items:flex-end">
    <button class="btn small" data-mnav="list" style="align-self:center">← Все модели</button>
    <h2 style="margin:0;align-self:center">Подготовка модели</h2><span class="spacer"></span>
    <label class="field">Модель<select id="prep-item">${items.map((i) => `<option value="${i.id}" ${i.id === S.prepare.id ? 'selected' : ''}>${esc(i.filename)}</option>`).join('')}</select></label>
    <label class="field">Принтер (область печати)<select id="prep-printer"><option value="">—</option>${printers.map((p) => `<option value="${p.config.id}" ${p.config.id === S.prepare.printer ? 'selected' : ''}>${esc(p.config.name)}</option>`).join('')}</select></label>
    <button class="btn small" data-mnav="calc" style="align-self:center">₸ Калькулятор</button>
  </div>
  <div class="cols">
    <div class="panel">
      <div id="viewer"></div>
      <div class="row small" style="margin-top:8px"><label class="row"><input type="checkbox" id="show-over" checked> подсветить нависания (красным)</label>
        <span class="spacer"></span><span class="muted">мышь: вращение, колесо: масштаб, правая кнопка: сдвиг</span></div>
    </div>
    <div>
      <div class="panel">
        <button class="btn primary" id="auto-prep" style="width:100%;justify-content:center;font-size:16px;min-height:46px">✨ Подготовить к печати</button>
        <p class="small muted" style="margin:8px 0 0">${pr ? `Для принтера «${esc(pr.config.name)}»: ` : 'Выберите принтер выше. '}исправит модель, положит её устойчиво, проверит, что помещается на стол, и нарежет${pr && pr.config.technology === 'resin' ? ' для фотополимера' : ''}.</p>
        <div id="prep-result"></div>
      </div>
      <div class="panel"><h3>Проверка</h3><div id="report" class="muted">Анализ…</div></div>
      <div class="panel"><h3>Оценка до нарезки</h3><div id="estimate" class="small muted">—</div></div>
      <details id="prep-adv" ${localStorageGet('prep_adv_open') === '1' ? 'open' : ''}>
      <summary class="panel" style="cursor:pointer;list-style:none;margin-top:14px"><b>⚙ Дополнительные настройки</b> <span class="small muted">— если нужно что-то поправить вручную</span></summary>
      <div class="panel"><h3>Автоподготовка</h3>
        <div class="form" style="grid-template-columns:1fr 1fr">
          <label class="row small"><input type="checkbox" data-po="repair"> исправлять ошибки модели</label>
          <label class="row small"><input type="checkbox" data-po="aggressive"> глубокий ремонт (MeshFix)</label>
          <label class="row small"><input type="checkbox" data-po="merge_bodies"> объединять пересекающиеся тела</label>
          <label class="row small"><input type="checkbox" data-po="fit_rotate"> разворачивать, чтобы поместилось</label>
          <label class="row small"><input type="checkbox" data-po="scale_to_fit"> уменьшать, если не помещается</label>
          <label class="row small"><input type="checkbox" data-po="slice"> нарезать после подготовки</label>
          <label class="field">Ориентация<select data-po="orient">
            <option value="auto">авто (менять, только если плохая)</option><option value="none">не менять</option>
            <option value="balanced">баланс</option><option value="supports">минимум поддержек</option>
            <option value="contact">максимум контакта со столом</option><option value="height">минимальная высота</option></select></label>
          <label class="field">Материал (для профиля слайсера)<select data-po="material">
            ${['PLA', 'PETG', 'ABS', 'ASA', 'TPU', 'PA', 'PC'].map((x) => `<option>${x}</option>`).join('')}</select></label>
        </div>
        <h3 style="margin-top:14px">Поддержки</h3>
        <div class="form" style="grid-template-columns:1fr 1fr">
          <label class="field">Когда ставить<select data-po="supports">
            <option value="auto">авто — если есть нависания</option><option value="on">всегда</option><option value="off">никогда</option></select></label>
          <label class="field">Тип (пластик)<select data-po="support_type">
            <option value="tree">древовидные — легко снимаются</option><option value="normal">обычные — для плоских нависаний</option></select></label>
          <label class="row small"><input type="checkbox" data-po="support_plate_only"> только от стола (не опираться на модель)</label>
          <label class="field">Угол нависания, ° (пусто — из профиля)<input type="number" min="10" max="80" data-po="support_angle" placeholder="30"></label>
          <label class="field">Плотность (смола)<select data-po="resin_supports">
            <option value="light">лёгкие</option><option value="medium">средние</option><option value="heavy">тяжёлые</option></select></label>
        </div>
        <p class="small muted">Красным в 3D-просмотре подсвечены места, которым нужны поддержки.</p>
      </div>
      <div class="panel"><h3>Исправление</h3>
        <div class="row"><button class="btn primary" data-repair="basic">🩹 Исправить</button>
          <button class="btn" data-repair="aggressive" title="MeshFix: тяжёлые случаи, самопересечения">Глубокий ремонт</button>
          <button class="btn" data-repair="merge" title="Объединить пересекающиеся тела в одно">Объединить тела</button></div>
        <div id="repair-res" class="small" style="margin-top:8px"></div>
      </div>
      <div class="panel"><h3>Размещение</h3>
        <div class="row">
          <button class="btn" data-op='{"op":"lay_flat","goal":"balanced"}'>⬇ Положить на грань</button>
          <button class="btn" id="orients">Варианты ориентации…</button>
          <button class="btn" data-op='{"op":"rotate","x":90}'>X +90°</button>
          <button class="btn" data-op='{"op":"rotate","y":90}'>Y +90°</button>
          <button class="btn" data-op='{"op":"rotate","z":90}'>Z +90°</button>
          <button class="btn" data-op='{"op":"mirror","axis":0}'>Зеркало X</button>
        </div>
        <div class="row" style="margin-top:10px">
          <label class="field">Масштаб, %<input id="scale-pct" type="number" value="100" style="width:90px"></label>
          <button class="btn" id="scale-go">Применить</button>
          <button class="btn small" data-op='{"op":"scale","value":10}' title="Модель была в сантиметрах">см→мм ×10</button>
          <button class="btn small" data-op='{"op":"scale","value":25.4}' title="Модель была в дюймах">дюймы→мм ×25.4</button>
        </div>
        <div class="row" style="margin-top:10px">
          <label class="field">Подогнать по оси<select id="fit-axis"><option value="0">X</option><option value="1">Y</option><option value="2" selected>Z (высота)</option></select></label>
          <label class="field">до, мм<input id="fit-size" type="number" style="width:90px"></label>
          <button class="btn" id="fit-go">Применить</button>
        </div>
        <p class="small muted">Каждая операция сохраняет новую версию файла в библиотеке (исходник не меняется).</p>
      </div>
      <div class="panel"><h3>Нарезка вручную (выбор профилей)</h3>
        ${info.length ? `<p class="small">FDM: ${info.map((s) => esc(s.name)).join(', ')}</p>
          <button class="btn primary" id="slice-btn">✂ Нарезать для FDM…</button>` :
    `<p class="small muted">FDM-слайсер не найден. Установите OrcaSlicer (подходит для Bambu, Elegoo и Anycubic) и укажите путь в «Настройках», либо нарежьте модель в своём слайсере и загрузите G-code / 3MF во «Файлы».</p>`}
        ${S.info.prusaslicer && S.info.uvtools ? `<p class="small" style="margin-top:10px">Смола: PrusaSlicer + UVtools</p>
          <button class="btn" id="slice-resin-btn">✂ Нарезать для фотополимера…</button>` :
    `<p class="small muted" style="margin-top:10px">Для нарезки под фотополимер установите PrusaSlicer и UVtools (бесплатные) — или режьте в Chitubox / Photon Workshop и загружайте готовый файл.</p>`}
      </div>
      </details>
    </div>
  </div>`;
  // настройки автоподготовки — запоминаются в браузере
  const PO_DEFAULT = { repair: true, aggressive: false, merge_bodies: true, fit_rotate: true, scale_to_fit: false, slice: true, orient: 'auto', material: 'PLA',
    supports: 'auto', support_type: 'tree', support_plate_only: false, support_angle: '', resin_supports: 'medium' };
  let po = { ...PO_DEFAULT };
  try { po = { ...PO_DEFAULT, ...JSON.parse(localStorageGet('prep_opts') || '{}') }; } catch { /* по умолчанию */ }
  $$('[data-po]').forEach((el) => {
    const k = el.dataset.po;
    if (el.type === 'checkbox') el.checked = !!po[k]; else el.value = po[k];
    el.onchange = () => {
      po[k] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? (el.value === '' ? '' : +el.value) : el.value;
      localStorageSet('prep_opts', JSON.stringify(po));
    };
  });
  $('#prep-adv').ontoggle = (e) => localStorageSet('prep_adv_open', e.target.open ? '1' : '0');
  // опалубки и формы уже разложены на столе по деталям — не поворачивать и не сливать их
  const cur = items.find((i) => i.id === S.prepare.id);
  const isMold = cur && /_(opalubka|forma)\.3mf$/i.test(cur.filename);
  $('#auto-prep').onclick = () => autoPrepare(S.prepare.id, isMold
    ? { ...po, orient: 'none', merge_bodies: false, fit_rotate: false, scale_to_fit: false } : po);
  $('#prep-item').onchange = (e) => { S.prepare.id = e.target.value; localStorageSet('prepare_id', S.prepare.id); renderPrepare(); };
  $('#prep-printer').onchange = (e) => { S.prepare.printer = e.target.value; localStorageSet('prepare_printer', S.prepare.printer); renderPrepare(); };
  const bed = pr && pr.config.build_volume && pr.config.build_volume[0] ? pr.config.build_volume : [256, 256, 256];
  const { ModelViewer } = await import('./viewer.js');
  if (S.prepare.viewer) S.prepare.viewer.dispose();
  const viewer = new ModelViewer($('#viewer'));
  S.prepare.viewer = viewer;
  viewer.setBed(bed);
  $('#show-over').onchange = (e) => viewer.setOverhangsVisible(e.target.checked);
  const id = S.prepare.id;
  const item = items.find((i) => i.id === id);
  viewer.loadUrl(`/api/library/${id}/mesh.stl`).then(() => {
    if (!item.has_thumb) setTimeout(() => api(`/library/${id}/thumbnail`, { method: 'POST', json: { data_url: viewer.snapshot() } }).catch(() => {}), 300);
  }).catch((e) => toast('Не удалось загрузить модель: ' + e.message, 'error'));
  api(`/library/${id}/mesh/analyze`, { method: 'POST', json: { printer_id: S.prepare.printer } })
    .then((r) => { $('#report').classList.remove('muted'); $('#report').innerHTML = meshReportHtml(r); })
    .catch((e) => { $('#report').innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`; });
  const tech = pr ? pr.config.technology : 'fdm';
  api('/estimate', { method: 'POST', json: { library_id: id, printer_id: S.prepare.printer, technology: tech } }).then((r) => {
    const e = r.estimate;
    $('#estimate').innerHTML = tech === 'resin'
      ? `Смола: <b>${fmtNum(e.resin_ml, 1)} мл</b> (${fmtNum(e.resin_g, 1)} г), слоёв ${e.layers}, время ≈ <b>${fmtDur(e.time_s)}</b>, стоимость ≈ <b>${money(r.cost.total)}</b><br>${esc(e.note)}`
      : `Пластик ≈ <b>${fmtNum(e.filament_g, 1)} г</b> (${fmtNum(e.filament_m, 1)} м), время ≈ <b>${fmtDur(e.time_s)}</b>, стоимость ≈ <b>${money(r.cost.total)}</b><br>${esc(e.note)}`;
  }).catch(() => {});

  const after = async (newId, msg) => {
    toast(msg, 'ok');
    S.prepare.id = newId; localStorageSet('prepare_id', newId);
    renderPrepare();
  };
  $$('[data-repair]').forEach((b) => {
    b.onclick = async () => {
      const mode = b.dataset.repair;
      b.disabled = true; $('#repair-res').textContent = 'Исправляю…';
      const r = await guard(() => api(`/library/${id}/mesh/repair`, { method: 'POST', json: { printer_id: S.prepare.printer, aggressive: mode === 'aggressive', merge_bodies: mode === 'merge' } }));
      b.disabled = false;
      if (!r) { $('#repair-res').textContent = ''; return; }
      toast(r.steps.join('; '), 'ok', 9000);
      after(r.item_id, `Сохранена исправленная версия${r.after.printable ? ' — модель готова к печати' : ' — остались проблемы'}`);
    };
  });
  const transform = async (ops) => {
    const r = await guard(() => api(`/library/${id}/mesh/transform`, { method: 'POST', json: { ops, printer_id: S.prepare.printer } }));
    if (r) after(r.item_id, 'Сохранена новая версия модели');
  };
  $$('[data-op]').forEach((b) => { b.onclick = () => transform([JSON.parse(b.dataset.op)]); });
  $('#scale-go').onclick = () => { const v = +$('#scale-pct').value; if (v > 0 && v !== 100) transform([{ op: 'scale', value: v / 100 }]); };
  $('#fit-go').onclick = () => { const v = +$('#fit-size').value; if (v > 0) transform([{ op: 'scale_to', axis: +$('#fit-axis').value, size: v }]); };
  $('#orients').onclick = async () => {
    const m = modal('Варианты ориентации', '<p class="muted">Считаю…</p>');
    const goals = { balanced: 'Баланс', supports: 'Минимум поддержек', contact: 'Максимум контакта', height: 'Минимальная высота' };
    const load = async (goal) => {
      const opts = await guard(() => api(`/library/${id}/mesh/orientations?goal=${goal}`));
      if (!opts) return;
      m.el.innerHTML = `<div class="row" style="margin-bottom:10px">${Object.entries(goals).map(([k, v]) => `<button class="btn small ${k === goal ? 'primary' : ''}" data-goal="${k}">${v}</button>`).join('')}</div>
        <table><thead><tr><th>#</th><th class="num">Контакт, см²</th><th class="num">Нависания, см²</th><th class="num">Высота, мм</th><th></th></tr></thead><tbody>
        ${opts.map((o, i) => `<tr><td>${i + 1}</td><td class="num">${fmtNum(o.contact_cm2, 2)}</td><td class="num">${fmtNum(o.overhang_cm2, 1)}</td><td class="num">${fmtNum(o.height_mm, 1)}</td><td><button class="btn small primary" data-apply="${i}">Применить</button></td></tr>`).join('')}</tbody></table>`;
      m.el.querySelectorAll('[data-goal]').forEach((b) => { b.onclick = () => load(b.dataset.goal); });
      m.el.querySelectorAll('[data-apply]').forEach((b) => { b.onclick = () => { m.close(); transform([{ op: 'orientation', matrix: opts[+b.dataset.apply].matrix }]); }; });
    };
    load('balanced');
  };
  const sb = $('#slice-btn');
  if (sb) sb.onclick = () => sliceDialog(id);
  const rb = $('#slice-resin-btn');
  if (rb) rb.onclick = () => sliceResinDialog(id);
}

async function autoPrepare(id, po) {
  const btn = $('#auto-prep');
  const box = $('#prep-result');
  const pid = S.prepare.printer;
  if (!pid && po.slice) toast('Принтер не выбран: модель будет подготовлена, но не нарезана', 'info', 7000);
  btn.disabled = true;
  btn.textContent = po.slice && pid ? '⏳ Готовлю и нарезаю… (до нескольких минут)' : '⏳ Готовлю…';
  box.innerHTML = '';
  const opts = { ...po, slice: po.slice && !!pid };
  if (opts.support_angle === '') delete opts.support_angle;
  const r = await guard(() => api(`/library/${id}/prepare`, { method: 'POST', json: { printer_id: pid, options: opts } }));
  btn.disabled = false;
  btn.textContent = '✨ Подготовить к печати';
  if (!r) return;
  let cost = '';
  if (r.sliced_item_id) {
    const e = await api('/estimate', { method: 'POST', json: { library_id: r.sliced_item_id, printer_id: pid } }).catch(() => null);
    if (e) {
      const c = e.cost;
      cost = `<div class="stats" style="margin:12px 0">
        <div class="stat"><span class="v">${fmtDur(e.time_s)}</span><span class="l">время</span></div>
        <div class="stat"><span class="v">${c.material_ml ? fmtNum(c.material_ml, 1) + ' мл' : fmtNum(c.material_g, 1) + ' г'}</span><span class="l">материал</span></div>
        <div class="stat"><span class="v">${money(c.total)}</span><span class="l">себестоимость</span></div></div>`;
    }
  }
  box.innerHTML = `<ul class="issues" style="margin-top:12px">${r.steps.map((s) => `<li class="${s.ok ? 'ok' : 'error'}">${s.ok ? '✓' : '✗'} <b>${esc(s.step)}</b>: ${esc(s.detail)}</li>`).join('')}</ul>
    ${cost}
    <div class="row" style="margin-top:10px">
      ${r.sliced_item_id ? `<button class="btn primary" id="prep-send">▶ Отправить на печать</button>` : ''}
      <button class="btn" id="prep-open">Показать подготовленную модель</button></div>
    ${!r.sliced_item_id && r.printable ? '<p class="small muted">Модель готова. Чтобы напечатать, нарежьте её (раздел «Дополнительные настройки») или в своём слайсере.</p>' : ''}`;
  const send = $('#prep-send');
  if (send) send.onclick = () => sendDialog(r.sliced_item_id);
  $('#prep-open').onclick = () => { S.prepare.id = r.mesh_item_id; localStorageSet('prepare_id', r.mesh_item_id); renderPrepare(); };
  toast(r.sliced_item_id ? 'Готово к печати' : r.printable ? 'Модель подготовлена' : 'Есть проблемы — смотрите список', r.printable ? 'ok' : 'error');
}

async function sliceResinDialog(id) {
  const m = modal('Нарезка для фотополимера (PrusaSlicer + UVtools)', '<p class="muted">Загружаю профили…</p>');
  const prof = await guard(() => api('/slicer/resin-profiles'));
  if (!prof) { m.close(); return; }
  if (!prof.printers.length) { m.el.innerHTML = `<div class="msg err">${esc(prof.error || 'Профили не найдены')}</div>`; return; }
  const opt = (arr) => arr.map((p) => `<option value="${esc(p.path)}">${esc(p.name)}</option>`).join('');
  m.el.innerHTML = `<div class="form" style="grid-template-columns:1fr">
    <label class="field">Принтер<select id="r-printer">${opt(prof.printers)}</select></label>
    <label class="field">Профиль (слой и поддержки)<select id="r-print">${opt(prof.prints)}</select></label></div>
    <div class="form" style="margin-top:8px">
      <label class="field">Экспозиция, с<input id="r-exp" type="number" step="0.1" value="2.5"></label>
      <label class="field">Экспозиция нижних слоёв, с<input id="r-bexp" type="number" step="1" value="25"></label>
      <label class="field">Нижних слоёв<input id="r-bl" type="number" value="5"></label></div>
    <p class="small muted">Формат файла (.goo, .ctb, .pwmx, .pm4n…) выбирается автоматически по профилю принтера. Поддержки генерирует PrusaSlicer.</p>
    <div class="row"><span class="spacer"></span><button class="btn primary" id="go">✂ Нарезать</button></div>`;
  $('#go', m.el).onclick = async () => {
    const btn = $('#go', m.el); btn.disabled = true; btn.textContent = 'Нарезаю… (может занять несколько минут)';
    const r = await guard(() => api(`/library/${id}/slice-resin`, { method: 'POST', json: {
      printer: $('#r-printer', m.el).value, print: $('#r-print', m.el).value, exposure_s: +$('#r-exp', m.el).value,
      bottom_exposure_s: +$('#r-bexp', m.el).value, bottom_layers: +$('#r-bl', m.el).value } }));
    btn.disabled = false; btn.textContent = '✂ Нарезать';
    if (r) { toast(`Готово: ${fmtDur(r.summary.time_s)}, ${fmtNum(r.summary.material_ml, 1)} мл`, 'ok', 9000); m.close(); setTab('library'); }
  };
}

async function sliceDialog(id) {
  const m = modal('Нарезка в OrcaSlicer', '<p class="muted">Загружаю профили…</p>');
  const prof = await guard(() => api('/slicer/profiles'));
  if (!prof) { m.close(); return; }
  if (!prof.machines || !prof.machines.length) {
    m.el.innerHTML = `<div class="msg err">Профили не найдены (${esc(prof.error || prof.dir || '')}). Укажите папку профилей в «Настройках».</div>`;
    return;
  }
  const opt = (arr) => arr.map((p) => `<option value="${esc(p.path)}">${esc(p.name)}</option>`).join('');
  m.el.innerHTML = `<div class="form" style="grid-template-columns:1fr">
    <label class="field">Принтер<input list="dl-mach" id="mach" placeholder="начните вводить: Bambu Lab A1 0.4 nozzle"><datalist id="dl-mach">${opt(prof.machines)}</datalist></label>
    <label class="field">Профиль печати<input list="dl-proc" id="proc" placeholder="0.20mm Standard"><datalist id="dl-proc">${opt(prof.processes)}</datalist></label>
    <label class="field">Филамент<input list="dl-fil" id="fil" placeholder="Bambu PLA Basic"><datalist id="dl-fil">${opt(prof.filaments)}</datalist></label>
    <label class="field">Тип стола<select id="bed">${(prof.bed_types || []).map((b) => `<option>${esc(b)}</option>`).join('')}</select></label>
    <label class="field">Поддержки<select id="sup"><option value="">как в профиле печати</option><option value="tree">включить: древовидные</option>
      <option value="normal">включить: обычные</option><option value="off">выключить</option></select></label>
    <label class="row small"><input type="checkbox" id="arrange" checked> авторасстановка</label>
    <label class="row small"><input type="checkbox" id="orient"> автоориентация</label></div>
    <p class="small muted">Можно выбрать из списка или вставить путь к своему .json-профилю, экспортированному из слайсера.</p>
    <div class="row"><span class="spacer"></span><button class="btn primary" id="go">✂ Нарезать</button></div><pre id="slog" class="small" style="white-space:pre-wrap;max-height:200px;overflow:auto"></pre>`;
  const resolve = (val, arr) => (arr.find((p) => p.name === val) || {}).path || val;
  $('#go', m.el).onclick = async () => {
    const btn = $('#go', m.el); btn.disabled = true; btn.textContent = 'Нарезаю… (до нескольких минут)';
    const r = await guard(() => api(`/library/${id}/slice`, { method: 'POST', json: {
      machine: resolve($('#mach', m.el).value, prof.machines), process: resolve($('#proc', m.el).value, prof.processes),
      filaments: [resolve($('#fil', m.el).value, prof.filaments)].filter(Boolean), arrange: $('#arrange', m.el).checked, orient: $('#orient', m.el).checked,
      bed_type: $('#bed', m.el).value,
      ...($('#sup', m.el).value ? { supports: $('#sup', m.el).value === 'off' ? 'off' : 'on', support_type: $('#sup', m.el).value } : {}) } }));
    btn.disabled = false; btn.textContent = '✂ Нарезать';
    if (r) {
      $('#slog', m.el).textContent = r.log || '';
      toast(`Готово: ${fmtDur(r.summary.time_s)}, ${fmtNum(r.summary.material_g, 1)} г`, 'ok', 9000);
      m.close(); setTab('library');
    }
  };
}

// ------------------------------------------------------------------ калькулятор
async function renderCalc() {
  const [items, materials] = await Promise.all([api('/library'), api('/materials')]);
  S.materials = materials;
  const printers = S.order.map((id) => S.printers[id]).filter(Boolean);
  view('models-body').innerHTML = `${modelsTitle('Калькулятор времени и стоимости')}
  <div class="cols"><div class="panel">
    <div class="form">
      <label class="field">Файл из библиотеки<select id="c-item"><option value="">— ввести вручную —</option>${items.map((i) => `<option value="${i.id}" ${i.id === S.calcItem ? 'selected' : ''}>${esc(i.filename)}</option>`).join('')}</select></label>
      <label class="field">Принтер<select id="c-printer"><option value="">—</option>${printers.map((p) => `<option value="${p.config.id}">${esc(p.config.name)}</option>`).join('')}</select></label>
      <label class="field">Технология<select id="c-tech"><option value="fdm">FDM (пластик)</option><option value="resin">Фотополимер</option></select></label>
      <label class="field">Материал<select id="c-mat"><option value="">—</option>${materials.map((m) => `<option value="${m.id}">${esc(m.name)} — ${money(m.price)}/кг</option>`).join('')}</select></label>
      <label class="field">Количество, шт<input id="c-qty" type="number" value="1" min="1"></label>
    </div>
    <div class="form" id="manual" style="margin-top:10px">
      <label class="field">Время, ч<input id="c-h" type="number" value="2" step="0.1"></label>
      <label class="field">Материал, г<input id="c-g" type="number" value="50"></label>
      <label class="field">Смола, мл (для фотополимера)<input id="c-ml" type="number" value=""></label>
    </div>
    <details style="margin-top:10px"><summary class="small">Параметры оценки 3D-модели (до нарезки)</summary>
      <div class="form" style="margin-top:8px">
        <label class="field">Заполнение, %<input id="f-infill" type="number" value="15"></label>
        <label class="field">Толщина стенок, мм<input id="f-wall" type="number" value="1.2" step="0.1"></label>
        <label class="field">Объёмная скорость, мм³/с<input id="f-flow" type="number" value="12"></label>
        <label class="field">Слой смолы, мм<input id="r-layer" type="number" value="0.05" step="0.01"></label>
        <label class="field">Экспозиция, с<input id="r-exp" type="number" value="2.5" step="0.1"></label>
        <label class="field">Цикл подъёма, с<input id="r-lift" type="number" value="5" step="0.5"></label>
        <label class="field">Поддержки, % объёма<input id="r-sup" type="number" value="15"></label>
        <label class="row small"><input type="checkbox" id="r-hollow"> полая модель</label>
      </div></details>
    <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="btn primary" id="c-go">Рассчитать</button></div>
  </div>
  <div class="panel"><h3>Результат</h3><div id="c-res" class="muted">Нажмите «Рассчитать»</div>
    <p class="small muted">Тарифы и наценка — во вкладке «Настройки», цены материалов — в «Материалах».</p></div></div>`;
  const upd = () => { $('#manual').classList.toggle('hidden', !!$('#c-item').value); };
  $('#c-item').onchange = upd; upd();
  $('#c-printer').onchange = () => { const p = S.printers[$('#c-printer').value]; if (p) $('#c-tech').value = p.config.technology; };
  $('#c-go').onclick = async () => {
    const tech = $('#c-tech').value;
    const body = { technology: tech, printer_id: $('#c-printer').value, material_id: $('#c-mat').value ? +$('#c-mat').value : null, quantity: +$('#c-qty').value || 1,
      fdm: { infill_pct: +$('#f-infill').value, wall_mm: +$('#f-wall').value, volumetric_flow: +$('#f-flow').value },
      resin: { layer_height_mm: +$('#r-layer').value, exposure_s: +$('#r-exp').value, lift_cycle_s: +$('#r-lift').value, supports_pct: +$('#r-sup').value, hollow: $('#r-hollow').checked } };
    if ($('#c-item').value) body.library_id = $('#c-item').value;
    else { body.time_s = (+$('#c-h').value || 0) * 3600; body.material_g = +$('#c-g').value || 0; if ($('#c-ml').value) body.material_ml = +$('#c-ml').value; }
    const r = await guard(() => api('/estimate', { method: 'POST', json: body }));
    if (!r) return;
    const c = r.cost;
    $('#c-res').classList.remove('muted');
    $('#c-res').innerHTML = `<div class="stats" style="margin-bottom:12px">
        <div class="stat"><span class="v">${fmtDur(r.time_s)}</span><span class="l">время печати</span></div>
        <div class="stat"><span class="v">${c.material_ml ? fmtNum(c.material_ml, 1) + ' мл' : fmtNum(c.material_g, 1) + ' г'}</span><span class="l">материал</span></div>
        <div class="stat"><span class="v">${money(c.total)}</span><span class="l">за 1 шт</span></div>
        ${c.quantity > 1 ? `<div class="stat"><span class="v">${money(c.total_for_quantity)}</span><span class="l">за ${c.quantity} шт</span></div>` : ''}</div>
      <div class="kv"><span>Материал</span><span>${money(c.material_cost)}</span>
        <span>Электричество</span><span>${money(c.electricity_cost)} (${fmtNum(c.electricity_kwh, 2)} кВт·ч)</span>
        <span>Амортизация</span><span>${money(c.depreciation_cost)}</span>
        <span>Запас на брак</span><span>${money(c.failure_reserve)}</span>
        <span>Работа</span><span>${money(c.labor_cost)}</span>
        <span>Наценка</span><span>${money(c.markup)}</span></div>
      ${r.estimate && r.estimate.note ? `<p class="small muted">${esc(r.estimate.note)}</p>` : ''}`;
  };
  if (S.calcItem) { $('#c-go').click(); S.calcItem = null; }
}

// ------------------------------------------------------------------ очередь
async function renderQueue() {
  const [q, items] = await Promise.all([api('/queue'), api('/library')]);
  const printers = S.order.map((id) => S.printers[id]).filter(Boolean);
  const printable = items.filter((i) => ['gcode', '3mf', 'resin'].includes(i.kind));
  view('jobs-q').innerHTML = `<h2>Очередь печати</h2>
    <div class="panel"><div class="row">
      <label class="field">Файл<select id="q-item">${printable.map((i) => `<option value="${i.id}">${esc(i.filename)}</option>`).join('')}</select></label>
      <label class="field">Принтер<select id="q-printer"><option value="">— любой подходящий —</option>${printers.map((p) => `<option value="${p.config.id}">${esc(p.config.name)}</option>`).join('')}</select></label>
      <label class="row small"><input type="checkbox" id="q-auto"> автозапуск, когда стол свободен</label>
      <button class="btn primary" id="q-add" ${printable.length ? '' : 'disabled'}>Добавить</button></div>
      <p class="small muted">Автозапуск срабатывает, только когда вы нажмёте «Стол очищен» на карточке принтера: программа не знает, сняли ли деталь со стола.</p></div>
    <div class="panel">${q.length ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>Файл</th><th>Принтер</th><th class="num">Время</th><th>Статус</th><th>Авто</th><th></th></tr></thead><tbody>
      ${q.map((x, i) => `<tr><td>${i + 1}</td><td>${esc(x.filename)}${x.plate > 1 ? ` <span class="small muted">пластина ${x.plate}</span>` : ''}</td>
        <td><select data-printer="${x.id}"><option value="">— любой —</option>${printers.map((p) => `<option value="${p.config.id}" ${p.config.id === x.printer_id ? 'selected' : ''}>${esc(p.config.name)}</option>`).join('')}</select></td>
        <td class="num">${fmtDur(x.summary.time_s)}</td><td class="small">${esc({ queued: 'ожидает', uploading: 'загрузка', uploaded: 'загружено', started: 'запущено', failed: 'ошибка' }[x.status] || x.status)}${x.message ? `<div class="muted">${esc(x.message)}</div>` : ''}</td>
        <td><input type="checkbox" data-auto="${x.id}" ${x.auto_start ? 'checked' : ''}></td>
        <td style="white-space:nowrap"><button class="btn small" data-up="${i}" ${i ? '' : 'disabled'}>↑</button>
          <button class="btn small primary" data-start="${x.id}">▶ Запустить</button>
          <button class="btn small danger" data-del="${x.id}">🗑</button></td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">Очередь пуста</div>'}</div>`;
  $('#q-add').onclick = async () => {
    await guard(() => api('/queue', { method: 'POST', json: { library_id: $('#q-item').value, printer_id: $('#q-printer').value || null, auto_start: $('#q-auto').checked } }), 'Добавлено');
    renderQueue();
  };
  $$('[data-printer]').forEach((s) => { s.onchange = () => api(`/queue/${s.dataset.printer}`, { method: 'PUT', json: { printer_id: s.value || null } }); });
  $$('[data-auto]').forEach((c) => { c.onchange = () => api(`/queue/${c.dataset.auto}`, { method: 'PUT', json: { auto_start: c.checked } }); });
  $$('[data-del]').forEach((b) => { b.onclick = async () => { await guard(() => api(`/queue/${b.dataset.del}`, { method: 'DELETE' })); renderQueue(); }; });
  $$('[data-up]').forEach((b) => {
    b.onclick = async () => {
      const i = +b.dataset.up; const a = q[i], c = q[i - 1];
      await api(`/queue/${a.id}`, { method: 'PUT', json: { position: c.position } });
      await api(`/queue/${c.id}`, { method: 'PUT', json: { position: a.position } });
      renderQueue();
    };
  });
  $$('[data-start]').forEach((b) => {
    b.onclick = async () => {
      const x = q.find((y) => y.id === +b.dataset.start);
      let pid = x.printer_id;
      if (!pid) { toast('Выберите принтер для задания', 'error'); return; }
      if (!confirmBox('Стол принтера свободен? Запустить печать?')) return;
      const r = await guard(() => api(`/queue/${x.id}/start`, { method: 'POST', json: { printer_id: pid } }), 'Отправка начата');
      if (r) renderQueue();
    };
  });
}

// ------------------------------------------------------------------ история
async function renderHistory() {
  const [jobs, stats] = await Promise.all([api('/jobs'), api('/stats')]);
  const t = stats.total || {};
  const STATUS = { printing: 'печатается', finished: '✓ готово', cancelled: 'отменено', error: '✗ ошибка' };
  view('jobs-h').innerHTML = `<h2>История и статистика</h2>
    <div class="panel"><div class="stats">
      <div class="stat"><span class="v">${t.jobs || 0}</span><span class="l">заданий</span></div>
      <div class="stat"><span class="v">${t.jobs ? Math.round((100 * (t.finished || 0)) / t.jobs) : 0}%</span><span class="l">успешных</span></div>
      <div class="stat"><span class="v">${fmtNum((t.hours_s || 0) / 3600, 1)} ч</span><span class="l">время печати</span></div>
      <div class="stat"><span class="v">${fmtNum((t.material_g || 0) / 1000, 2)} кг</span><span class="l">пластика</span></div>
      <div class="stat"><span class="v">${fmtNum(t.material_ml || 0, 0)} мл</span><span class="l">смолы</span></div>
      <div class="stat"><span class="v">${money(t.cost || 0)}</span><span class="l">себестоимость</span></div></div></div>
    ${stats.per_printer.length ? `<div class="panel"><h3>По принтерам</h3><div class="table-wrap"><table><thead><tr><th>Принтер</th><th class="num">Заданий</th><th class="num">Успешно</th><th class="num">Сбоев</th><th class="num">Часы</th><th class="num">Материал</th><th class="num">Стоимость</th></tr></thead><tbody>
      ${stats.per_printer.map((p) => `<tr><td>${esc(p.printer_name)}</td><td class="num">${p.jobs}</td><td class="num">${p.finished}</td><td class="num">${p.failed}</td><td class="num">${fmtNum(p.hours_s / 3600, 1)}</td><td class="num">${p.material_ml ? fmtNum(p.material_ml, 0) + ' мл' : fmtNum(p.material_g, 0) + ' г'}</td><td class="num">${money(p.cost)}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
    <div class="panel"><h3>Задания</h3>${jobs.length ? `<div class="table-wrap"><table><thead><tr><th>Начало</th><th>Принтер</th><th>Файл</th><th>Статус</th><th class="num">Длительность</th><th class="num">Материал</th><th class="num">Стоимость</th></tr></thead><tbody>
      ${jobs.map((j) => `<tr><td class="small">${fmtDate(j.started_at)}</td><td>${esc(j.printer_name)}</td><td style="overflow-wrap:anywhere">${esc(j.filename)}</td><td class="small">${STATUS[j.status] || esc(j.status)}</td>
        <td class="num">${fmtDur(j.duration_s ?? (Date.now() / 1000 - j.started_at))}</td><td class="num">${j.material_ml ? fmtNum(j.material_ml, 1) + ' мл' : j.material_g ? fmtNum(j.material_g, 1) + ' г' : '—'}</td><td class="num">${j.cost != null ? money(j.cost) : '—'}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">Пока ничего не печаталось</div>'}</div>`;
}

// ------------------------------------------------------------------ материалы
async function renderMaterials() {
  S.materials = await api('/materials');
  view('set-mat').innerHTML = `<div class="row" style="margin-bottom:12px"><h2 style="margin:0">Материалы</h2><span class="spacer"></span><button class="btn primary" id="m-add">+ Добавить</button></div>
    <div class="panel"><div class="table-wrap"><table><thead><tr><th></th><th>Название</th><th>Тип</th><th>Технология</th><th class="num">Цена за кг</th><th class="num">Плотность</th><th class="num">Остаток</th><th></th></tr></thead><tbody>
    ${S.materials.map((m) => `<tr><td><span class="swatch" style="background:${esc(m.color || '#888')}"></span></td><td>${esc(m.name)}</td><td>${esc(m.type)}</td><td>${m.technology === 'resin' ? 'смола' : 'пластик'}</td>
      <td class="num">${money(m.price)}</td><td class="num">${fmtNum(m.density, 2)} г/см³</td><td class="num">${fmtNum(m.remaining_g, 0)} г${m.remaining_g < 150 ? ' ⚠' : ''}</td>
      <td style="white-space:nowrap"><button class="btn small" data-edit="${m.id}">✎</button><button class="btn small danger" data-del="${m.id}">🗑</button></td></tr>`).join('')}</tbody></table></div>
    <p class="small muted">Остаток уменьшается автоматически после печати, если при отправке выбран материал.</p></div>`;
  $('#m-add').onclick = () => editMaterial({ technology: 'fdm', type: 'PLA', density: 1.24, diameter: 1.75, remaining_g: 1000, price: 1500, color: '#ffffff' });
  $$('[data-edit]').forEach((b) => { b.onclick = () => editMaterial(S.materials.find((m) => m.id === +b.dataset.edit)); });
  $$('[data-del]').forEach((b) => { b.onclick = async () => { if (confirmBox('Удалить материал?')) { await api(`/materials/${b.dataset.del}`, { method: 'DELETE' }); renderMaterials(); } }; });
}

function editMaterial(mt) {
  const m = modal(mt.id ? 'Материал' : 'Новый материал', `<div class="form">
    <label class="field">Название<input name="name" value="${esc(mt.name || '')}"></label>
    <label class="field">Технология<select name="technology"><option value="fdm" ${mt.technology === 'fdm' ? 'selected' : ''}>пластик</option><option value="resin" ${mt.technology === 'resin' ? 'selected' : ''}>смола</option></select></label>
    <label class="field">Тип<input name="type" value="${esc(mt.type || '')}" placeholder="PLA, PETG, ABS-like…"></label>
    <label class="field">Цвет<input name="color" type="color" value="${esc(mt.color || '#ffffff')}"></label>
    <label class="field">Цена за кг<input name="price" type="number" value="${mt.price ?? ''}"></label>
    <label class="field">Плотность, г/см³<input name="density" type="number" step="0.01" value="${mt.density ?? ''}"></label>
    <label class="field">Диаметр нити, мм<input name="diameter" type="number" step="0.01" value="${mt.diameter ?? 1.75}"></label>
    <label class="field">Остаток, г<input name="remaining_g" type="number" value="${mt.remaining_g ?? 1000}"></label></div>
    <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="btn primary" id="save">Сохранить</button></div>`);
  $('#save', m.el).onclick = async () => {
    const g = (n) => m.el.querySelector(`[name=${n}]`).value;
    const body = { id: mt.id, name: g('name'), technology: g('technology'), type: g('type'), color: g('color'), price: +g('price') || 0, density: +g('density') || 1.24, diameter: +g('diameter') || 0, remaining_g: +g('remaining_g') || 0, notes: mt.notes || '' };
    if (await guard(() => api('/materials', { method: 'POST', json: body }), 'Сохранено')) { m.close(); renderMaterials(); }
  };
}

// ------------------------------------------------------------------ обновления
const UPD_STATUS = {
  idle: 'ожидание', checking: 'проверка…', available: 'доступна новая версия', downloading: 'загрузка…',
  ready: 'загружено, устанавливается', waiting: 'ждём окончания передачи файлов', installing: 'установка… программа перезапустится',
  up_to_date: 'установлена последняя версия', error: 'ошибка', disabled: 'источник обновлений не настроен',
};

function renderUpdateBar() {
  const u = S.update || {};
  let bar = $('#update-bar');
  const show = ['downloading', 'ready', 'waiting', 'installing'].includes(u.status) || (u.status === 'available' && u.installed);
  if (!show) {
    if (bar) bar.style.display = 'none';
    return;
  }
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'update-bar';
    bar.className = 'msg';
    bar.style.cssText = 'margin:0;border-radius:0;text-align:center';
    document.body.insertBefore(bar, $('main'));
  }
  bar.style.display = '';
  bar.textContent = u.status === 'downloading' ? `⬇ Загружается обновление ${u.latest}: ${fmtNum(u.progress, 0)}%`
    : u.status === 'installing' ? `🔄 Устанавливается версия ${u.latest}. Страница обновится сама через минуту.`
      : u.status === 'waiting' ? `⏳ Обновление ${u.latest} установится после окончания передачи файлов`
        : `✨ Доступна версия ${u.latest} — установится автоматически`;
  if (u.status === 'installing' && !S.reloadPlanned) {
    S.reloadPlanned = true;
    setTimeout(() => location.reload(), 60000);
  }
}

function renderUpdatePanel() {
  const el = $('#update-panel');
  if (!el) return;
  const u = S.update || {};
  el.innerHTML = `<div class="kv">
      <span>Установлена</span><span><b>${esc(u.current || S.info.version || '')}</b>${u.installed ? '' : ' (запуск из исходников — автоустановка отключена)'}</span>
      <span>Последняя</span><span>${esc(u.latest || '—')}</span>
      <span>Статус</span><span>${esc(UPD_STATUS[u.status] || u.status || '—')}${u.status === 'downloading' ? ' ' + fmtNum(u.progress, 0) + '%' : ''}</span>
      <span>Проверено</span><span>${u.last_check ? fmtDate(u.last_check) : '—'}</span>
      <span>Источник</span><span class="small">${esc(u.source || '—')}</span></div>
    ${u.error ? `<div class="msg err">${esc(u.error)}</div>` : ''}
    ${u.notes && u.status === 'available' ? `<details style="margin-top:8px"><summary class="small">Что нового</summary><pre class="small" style="white-space:pre-wrap">${esc(u.notes)}</pre></details>` : ''}
    <div class="row" style="margin-top:10px"><button class="btn" id="upd-check">Проверить обновления</button>
      ${u.installed && ['available', 'ready', 'error'].includes(u.status) ? '<button class="btn primary" id="upd-install">Обновить сейчас</button>' : ''}</div>`;
  $('#upd-check').onclick = async () => {
    const r = await guard(() => api('/update/check', { method: 'POST' }));
    if (r) { S.update = r; renderUpdatePanel(); renderUpdateBar(); }
  };
  const ib = $('#upd-install');
  if (ib) {
    ib.onclick = async () => {
      const r = await guard(() => api('/update/install', { method: 'POST' }));
      if (r) { S.update = r; renderUpdatePanel(); renderUpdateBar(); }
    };
  }
}

// ------------------------------------------------------------------ настройки
const CURRENCIES = [
  ['₸', 'тенге'], ['₽', 'рубль'], ['$', 'доллар США'], ['€', 'евро'], ['сўм', 'узбекский сум'], ['сом', 'киргизский сом'],
  ['Br', 'белорусский рубль'], ['₴', 'гривна'], ['₾', 'лари'], ['֏', 'драм'], ['₼', 'манат'], ['ЅМ', 'сомони'],
  ['¥', 'юань'], ['₺', 'турецкая лира'], ['£', 'фунт'], ['AED', 'дирхам ОАЭ'],
];

async function renderSettings() {
  S.settings = await api('/settings');
  const s = S.settings;
  const ev = new Set(s.notify_events || []);
  view('set-main').innerHTML = `<h2>Настройки</h2>
  <div class="cols"><div>
    <div class="panel"><h3>Стоимость</h3><div class="form">
      <label class="field">Валюта<select name="currency">${CURRENCIES.concat(CURRENCIES.some(([c]) => c === s.currency) || !s.currency ? [] : [[s.currency, s.currency]])
        .map(([c, n]) => `<option value="${esc(c)}" ${c === (s.currency || '₸') ? 'selected' : ''}>${esc(c)} — ${esc(n)}</option>`).join('')}</select></label>
      <label class="field">Электричество, за кВт·ч<input name="electricity_price" type="number" step="0.01" value="${s.electricity_price}"></label>
      <label class="field">Амортизация, за час<input name="depreciation_per_hour" type="number" step="0.1" value="${s.depreciation_per_hour}"></label>
      <label class="field">Работа, за задание<input name="labor_per_job" type="number" value="${s.labor_per_job}"></label>
      <label class="field">Запас на брак, %<input name="failure_pct" type="number" value="${s.failure_pct}"></label>
      <label class="field">Наценка, %<input name="markup_pct" type="number" value="${s.markup_pct}"></label></div>
      <div class="row" style="margin-top:10px"><button class="btn small" id="recalc">💱 Пересчитать все цены по курсу…</button>
        <span class="small muted">тариф, амортизация, работа и цены материалов</span></div></div>
    <div class="panel"><h3>Уведомления в Telegram</h3><div class="form">
      <label class="field">Токен бота (от @BotFather)<input name="telegram_token" value="${esc(s.telegram_token)}"></label>
      <label class="field">Chat ID<input name="telegram_chat_id" value="${esc(s.telegram_chat_id)}"></label></div>
      <div class="row" style="margin-top:8px">${[['finished', 'завершение'], ['error', 'ошибка'], ['paused', 'пауза'], ['cancelled', 'отмена'], ['offline', 'потеря связи при печати']].map(([k, v]) => `<label class="row small"><input type="checkbox" data-ev="${k}" ${ev.has(k) ? 'checked' : ''}> ${v}</label>`).join('')}</div>
      <div class="row" style="margin-top:8px"><button class="btn" id="tg-test">Отправить тест</button></div></div>
  </div><div>
    <div class="panel"><h3>Слайсер</h3><div class="form" style="grid-template-columns:1fr">
      <label class="field">Путь к OrcaSlicer / Bambu Studio (пусто — искать автоматически)<input name="slicer_path" value="${esc(s.slicer_path)}" placeholder="C:\\Program Files\\OrcaSlicer\\orca-slicer.exe"></label>
      <label class="field">Папка профилей (пусто — профили из установки слайсера)<input name="slicer_profiles_dir" value="${esc(s.slicer_profiles_dir)}"></label>
      <label class="field">PrusaSlicer (для смолы)<input name="prusaslicer_path" value="${esc(s.prusaslicer_path)}" placeholder="C:\\Program Files\\Prusa3D\\PrusaSlicer\\prusa-slicer-console.exe"></label>
      <label class="field">UVtoolsCmd (для смолы)<input name="uvtools_path" value="${esc(s.uvtools_path)}"></label></div>
      <p class="small muted">Найдено: ${(S.info.slicers || []).map((x) => esc(x.name + ' — ' + x.path)).join('<br>') || 'FDM-слайсер не найден'}
        ${S.info.prusaslicer ? '<br>PrusaSlicer — ' + esc(S.info.prusaslicer) : ''}${S.info.uvtools ? '<br>UVtools — ' + esc(S.info.uvtools) : ''}</p></div>
    <div class="panel"><h3>Сеть и доступ</h3><div class="form">
      <label class="field">Подсеть для поиска<input name="discovery_subnet" value="${esc(s.discovery_subnet)}" placeholder="${esc(S.info.subnet || '192.168.1.0/24')}"></label>
      <label class="field">Кадров камеры в секунду<input name="camera_fps" type="number" step="0.5" value="${s.camera_fps}"></label>
      <label class="field">Пароль на вход (пусто — без пароля)<input name="auth_password" type="password" value="${esc(s.auth_password)}"></label></div>
      <p class="small muted">Пароль рекомендуется, если интерфейс открыт для всех устройств в сети. Логин — любой.</p></div>
    <div class="panel"><h3>Обновления</h3><div id="update-panel"></div>
      <label class="row small" style="margin-top:10px"><input type="checkbox" name="auto_update" ${s.auto_update !== false ? 'checked' : ''}> устанавливать обновления автоматически</label>
      <details style="margin-top:8px"><summary class="small muted">Источник обновлений</summary><div class="form" style="grid-template-columns:1fr;margin-top:8px">
        <label class="field">Репозиторий GitHub (владелец/имя)<input name="update_repo" value="${esc(s.update_repo || '')}" placeholder="из сборки"></label>
        <label class="field">или адрес JSON-манифеста<input name="update_url" value="${esc(s.update_url || '')}"></label></div></details></div>
    <div class="panel small muted">SEBE 3D ${esc(S.info.version || '')} · данные: <code>${esc(S.info.data_dir || '')}</code></div>
  </div></div>
  <div class="row" style="margin-top:14px"><span class="spacer"></span><button class="btn primary" id="save">Сохранить</button></div>`;
  $('#save').onclick = async () => {
    const body = {};
    $$('#main [name]').forEach((el) => { body[el.name] = el.type === 'number' ? +el.value : el.type === 'checkbox' ? el.checked : el.value; });
    body.notify_events = $$('[data-ev]').filter((c) => c.checked).map((c) => c.dataset.ev);
    if (await guard(() => api('/settings', { method: 'PUT', json: body }), 'Настройки сохранены')) {
      S.info = await api('/info');
      renderSettings();
    }
  };
  $('#recalc').onclick = async () => {
    const from = S.settings.currency || '₽';
    const rate = parseFloat(String(window.prompt(`Сколько новых единиц валюты в 1 ${from}?\nНапример, из рублей в тенге: 1 ₽ ≈ 6 ₸ — введите 6.\nВсе цены будут умножены на это число.`, '') || '').replace(',', '.'));
    if (!rate || rate <= 0) return;
    const to = $('[name=currency]').value;
    const keys = ['electricity_price', 'depreciation_per_hour', 'labor_per_job'];
    const body = { currency: to };
    keys.forEach((k) => { body[k] = Math.round((+S.settings[k] || 0) * rate * 100) / 100; });
    const mats = await api('/materials');
    await guard(async () => {
      await api('/settings', { method: 'PUT', json: body });
      for (const m of mats) await api('/materials', { method: 'POST', json: { ...m, price: Math.round((+m.price || 0) * rate) } });
    }, `Цены пересчитаны: ×${rate}, валюта ${to}`);
    renderSettings();
  };
  $('#tg-test').onclick = async () => {
    const r = await guard(() => api('/settings/test-telegram', { method: 'POST' }));
    if (r) toast(r.ok ? 'Сообщение отправлено' : 'Не удалось — проверьте токен и Chat ID (и что вы написали боту /start)', r.ok ? 'ok' : 'error');
  };
  S.update = await api('/update').catch(() => S.update);
  renderUpdatePanel();
}

// ------------------------------------------------------------------ старт
(async () => {
  try {
    [S.info, S.drivers, S.settings] = await Promise.all([api('/info'), api('/drivers'), api('/settings')]);
    setPrinters(await api('/printers'));
    S.update = S.info.update;
    renderUpdateBar();
  } catch (e) { toast('Сервер недоступен: ' + e.message, 'error'); }
  connectWs();
  setTab(S.tab);
  // обновлять «осталось/будет готово» раз в минуту
  setInterval(() => { if (S.tab === 'printers') S.order.forEach(updatePrinterCard); }, 60000);
})();
