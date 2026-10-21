'use strict';
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const $ = id => document.getElementById(id);
// a keyboard button gives an empty initData (but supports sendData); the menu button gives initData (but no sendData)
const MODE_KB = !tg.initData;
document.body.classList.add(MODE_KB ? 'mode-kb' : 'mode-menu');

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Safe DOM builder: text goes in through textContent only
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const clear = el => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

function haptic(kind) {
  try {
    const hf = tg.HapticFeedback;
    if (!hf) return;
    if (kind === 'ok' || kind === 'error' || kind === 'warning') hf.notificationOccurred(kind === 'ok' ? 'success' : kind);
    else hf.impactOccurred('light');
  } catch (e) { /* non-critical */ }
}

function toast(msg, isErr) {
  const t = h('div', { class: 'toast' + (isErr ? ' err' : ''), text: msg });
  $('toasts').appendChild(t);
  haptic(isErr ? 'error' : 'ok');
  setTimeout(() => t.remove(), isErr ? 4200 : 2200);
}
const fail = e => toast(e && e.message ? e.message : String(e), true);

function confirmBox(text) {
  return new Promise(res => {
    if (typeof tg.showConfirm === 'function') tg.showConfirm(text, ok => res(!!ok));
    else res(window.confirm(text));
  });
}

// Bottom sheet picker: options = [{label, value}]
function sheet(title, options) {
  return new Promise(res => {
    const done = v => { bg.remove(); res(v); };
    const bg = h('div', { class: 'sheet-bg', onclick: e => { if (e.target === bg) done(null); } },
      h('div', { class: 'sheet' },
        h('h3', { text: title }),
        options.map(o => h('button', { class: 'btn', text: o.label, onclick: () => done(o.value) })),
        h('button', { class: 'btn danger', text: 'Cancel', onclick: () => done(null) })));
    $('sheetHost').appendChild(bg);
  });
}

async function api(path, method, body) {
  const opts = { method: method || 'GET', headers: { 'X-Init-Data': tg.initData } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || ('HTTP ' + res.status));
    err.status = res.status;
    throw err;
  }
  return data;
}

function copyText(text, msg) {
  const ok = () => toast(msg || 'Copied');
  const bad = () => tg.showAlert ? tg.showAlert(text) : toast(text);
  try { navigator.clipboard.writeText(text).then(ok, bad); } catch (e) { bad(); }
}

const fmtDate = iso => {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit' }) + ' ' +
         d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
};
const fmtDay = iso => iso ? new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric' }) : '';
const fmtSize = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
const plural = (n, one, many) => n === 1 ? one : many;

const ICONS = { delivered: '✅', sent: '📤', spam: '🚫', muted: '🔕', unassigned: '📥', failed: '⚠️', blocked: '⛔' };
const STATUS_NAMES = { delivered: 'Delivered', sent: 'Sent', spam: 'Spam', muted: 'Muted', unassigned: 'Unassigned', failed: 'Errors', blocked: 'Blocked' };

function skeleton(n) {
  return Array.from({ length: n || 4 }, () => h('div', { class: 'skel' }, h('i'), h('i'), h('i')));
}
function emptyBox(icon, text) {
  return h('div', { class: 'empty' }, h('div', { class: 'big', text: icon }), h('div', { text }));
}

// --- State ---
let ME = null;
let TEMPLATES = null;
let curMail = null;
let compose = { reply_to: null, forward_of: null, attachments: [] };

// --- Tabs ---
const TABS = ['inbox', 'compose', 'addr', 'more'];
const PAGE_ID = { inbox: 'pageInbox', compose: 'pageCompose', addr: 'pageAddr', more: 'pageMore' };
let curTab = null;

function showTab(name) {
  if (stack.length) closeAll();
  curTab = name;
  for (const t of TABS) $(PAGE_ID[t]).hidden = t !== name;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  if (MODE_KB) { if (name === 'compose') tg.MainButton.show(); else tg.MainButton.hide(); return; }
  if (name === 'addr') loadAliases();
  if (name === 'compose') ensureTemplates();
  if (name === 'inbox' && !$('list').firstChild) reload();
  window.scrollTo(0, 0);
}
document.querySelectorAll('.tab').forEach(b => b.onclick = () => { haptic(); showTab(b.dataset.tab); });

// --- Navigation stack (BackButton) ---
const stack = [];
function pushScreen(title, build) {
  const inner = h('div', { class: 'inner' });
  const screen = h('div', { class: 'screen' }, inner);
  inner.appendChild(h('div', { class: 'screen-head' },
    h('button', { class: 'back', text: '← Back', onclick: popScreen }),
    h('h2', { text: title })));
  const body = h('div');
  inner.appendChild(body);
  $('stack').appendChild(screen);
  stack.push(screen);
  tg.BackButton.show();
  window.scrollTo(0, 0);
  build(body, screen);
  return body;
}
function popScreen() {
  const s = stack.pop();
  if (s) s.remove();
  if (!stack.length) tg.BackButton.hide();
}
function closeAll() { while (stack.length) popScreen(); }
tg.BackButton.onClick(popScreen);

// ===================== Inbox =====================
const FILTERS = [
  { kind: 'status', val: '', label: 'All' },
  { kind: 'unread', val: '', label: '● Unread' },
  { kind: 'starred', val: '', label: '⭐ Starred' },
  { kind: 'bulk', val: '', label: '📰 Newsletters' },
  { kind: 'expiring', val: '', label: '🗑 Expiring soon', id: 'chipExpiring', hidden: true },
  { kind: 'status', val: 'delivered', label: '✅ Delivered' },
  { kind: 'status', val: 'sent', label: '📤 Sent' },
  { kind: 'status', val: 'spam', label: '🚫 Spam' },
  { kind: 'status', val: 'muted', label: '🔕 Muted' },
  { kind: 'status', val: 'unassigned', label: '📥 Unassigned', admin: true },
];
let curKind = 'status', curStatus = '', curQuery = '', curPage = 0, loading = false, hasMore = false, reqSeq = 0;

function buildChips() {
  clear($('chips'));
  FILTERS.forEach(f => {
    if (f.admin && !(ME && ME.is_admin)) return;
    const c = h('button', { class: 'chip' + (f.kind === curKind && f.val === curStatus ? ' active' : ''), text: f.label });
    if (f.id) c.id = f.id;
    if (f.hidden) c.hidden = true;
    c.onclick = () => {
      haptic();
      document.querySelectorAll('#chips .chip').forEach(x => x.classList.remove('active'));
      c.classList.add('active');
      curKind = f.kind; curStatus = f.val;
      reload();
    };
    $('chips').appendChild(c);
  });
}

const OPS = [
  ['from:', 'sender'], ['to:', 'recipient or alias'], ['alias:', 'mail to an address'],
  ['has:code', 'has a code'], ['has:attachment', 'has an attachment'], ['is:unread', 'unread'],
  ['is:starred', 'starred'], ['is:bulk', 'newsletters'], ['after:', 'after date YYYY-MM-DD'], ['before:', 'before date YYYY-MM-DD'],
];
$('searchHelp').onclick = async () => {
  const t = await sheet('Search operators', OPS.map(o => ({ label: o[0] + '  ' + o[1], value: o[0] })));
  if (!t) return;
  const s = $('search');
  s.value = (s.value ? s.value.replace(/\s*$/, '') + ' ' : '') + t + (t.endsWith(':') ? '' : ' ');
  s.focus();
  if (!t.endsWith(':')) { curQuery = s.value.trim(); reload(); }
};

let searchTimer;
$('search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { curQuery = $('search').value.trim(); reload(); }, 400);
});

function searchBy(q) {
  closeAll();
  $('search').value = q;
  curQuery = q;
  curKind = 'status'; curStatus = '';
  buildChips();
  showTab('inbox');
  reload();
}
const aliasQuery = m => { const a = m.orig_to || m.to || ''; return 'alias:' + (/\s/.test(a) ? '"' + a + '"' : a); };

let selMode = false;
const sel = new Set();
function updateSelBar() {
  $('selBar').hidden = !selMode;
  $('selCount').textContent = sel.size + ' selected';
}
function toggleSel(id, row) {
  if (sel.has(id)) { sel.delete(id); row.classList.remove('sel'); }
  else {
    if (sel.size >= 100) return toast('No more than 100 emails at a time', true);
    sel.add(id); row.classList.add('sel');
  }
  haptic();
  if (!sel.size) exitSel(); else updateSelBar();
}
function enterSel(id, row) { selMode = true; $('list').classList.add('selmode'); toggleSel(id, row); }
function exitSel() {
  selMode = false; sel.clear();
  $('list').classList.remove('selmode');
  document.querySelectorAll('#list .row.sel').forEach(r => r.classList.remove('sel'));
  updateSelBar();
}
$('selX').onclick = exitSel;
document.querySelectorAll('#selBar [data-act]').forEach(b => b.onclick = () => batch(b.dataset.act));

const stOf = v => (v && typeof v === 'object') ? v.status : v;
async function batch(action) {
  const ids = [...sel];
  if (!ids.length) return;
  if (action === 'delete' && !await confirmBox('Delete ' + ids.length + ' ' + plural(ids.length, 'email', 'emails') + '? This cannot be undone.')) return;
  try {
    const r = await api('/api/mails/batch', 'POST', { ids, action });
    if (action === 'unsubscribe') {
      const c = { ok: 0, manual: 0, fail: 0 };
      Object.values(r.results || {}).forEach(v => {
        const s = stOf(v);
        if (['posted', 'already', 'mailed'].includes(s)) c.ok++; else if (s === 'link' || s === 'manual') c.manual++; else c.fail++;
      });
      toast('Unsubscribe: done ' + c.ok + (c.manual ? ', manual ' + c.manual : '') + (c.fail ? ', failed ' + c.fail : ''), c.fail > 0 && !c.ok);
    } else {
      const names = { star: 'Starred', unstar: 'Removed from starred', read: 'Marked as read', unread: 'Marked as unread', delete: 'Deleted' };
      toast(names[action] + ': ' + (r.done != null ? r.done : ids.length));
    }
    exitSel();
    reload();
  } catch (e) { fail(e); }
}

function mailRow(m, onOpen, selectable) {
  const tags = [];
  if (m.otp) tags.push(h('span', { class: 'badge code', text: '🔐 ' + m.otp }));
  if (m.bulk) tags.push(h('span', { class: 'badge', text: 'newsletter' }));
  if (m.has_attachments) tags.push(h('span', { class: 'badge', text: '📎' }));
  if (m.alias_note) tags.push(h('button', { class: 'badge link', text: '🏷 ' + m.alias_note, title: 'All emails to this address',
    onclick: ev => { ev.stopPropagation(); searchBy(aliasQuery(m)); } }));
  const main = h('div', { class: 'main' },
    h('div', { class: 'top' },
      h('div', { class: 'from' }, m.starred ? h('span', { class: 'star', text: '⭐ ' }) : null, (ICONS[m.status] || '') + ' ' + (m.from || '')),
      h('div', { class: 'date', text: fmtDate(m.date) })),
    h('div', { class: 'subj' }, m.subject || h('i', { text: 'no subject' })),
    m.preview ? h('div', { class: 'prev', text: m.preview }) : null,
    tags.length ? h('div', { class: 'tags' }, tags) : null);
  const row = h('div', { class: 'row mail' + (m.read === false ? ' unread' : ''), 'data-id': m.id }, h('span', { class: 'cb' }), main);
  if (!selectable) { row.onclick = () => (onOpen || openDetail)(m.id); return row; }
  let timer = null, long = false;
  const stop = () => { clearTimeout(timer); timer = null; };
  row.addEventListener('pointerdown', () => { long = false; timer = setTimeout(() => { long = true; if (!selMode) enterSel(m.id, row); }, 500); });
  ['pointerup', 'pointerleave', 'pointercancel'].forEach(ev => row.addEventListener(ev, stop));
  row.addEventListener('contextmenu', ev => ev.preventDefault());
  row.onclick = () => {
    if (long) { long = false; return; }
    if (selMode) toggleSel(m.id, row); else openDetail(m.id);
  };
  return row;
}

async function load() {
  if (loading) return;
  loading = true;
  const seq = reqSeq;
  const first = curPage === 0;
  if (first) { clear($('list')).append(...skeleton(5)); $('empty').hidden = true; }
  try {
    const qs = curKind === 'unread' ? (curQuery + ' is:unread').trim() : curQuery;
    const q = new URLSearchParams({ query: qs, status: curStatus, page: curPage });
    if (curKind === 'bulk') q.set('bulk', '1');
    if (curKind === 'starred') q.set('starred', '1');
    if (curKind === 'expiring') q.set('expiring', '1');
    const data = await api('/api/mails?' + q);
    if (seq !== reqSeq) return;
    if (first) clear($('list'));
    data.items.forEach(m => $('list').appendChild(mailRow(m, null, true)));
    hasMore = !!data.has_more;
    if (typeof data.unread === 'number') setUnread(data.unread);
    $('empty').hidden = !(first && data.items.length === 0);
    if (first && data.items.length === 0) {
      clear($('empty')).append(h('div', { class: 'big', text: curQuery ? '🔍' : '📭' }),
        h('div', { text: curQuery ? 'Nothing found. Try a different query.' : 'No emails here yet.' }));
    }
    if (data.ttl_days > 0 && $('chipExpiring')) $('chipExpiring').hidden = false;
    if (curKind === 'expiring') setExpiringLabel(data.total);
  } catch (e) {
    if (seq !== reqSeq) return;
    if (first) clear($('list'));
    clear($('empty')).append(h('div', { class: 'big', text: '⚠️' }), h('div', { text: 'Failed to load: ' + e.message }),
      h('div', { class: 'actions', style: 'justify-content:center' }, h('button', { class: 'btn', text: 'Retry', onclick: reload })));
    $('empty').hidden = false;
    hasMore = false;
  } finally {
    loading = false;
  }
}
function reload() { exitSel(); reqSeq++; loading = false; curPage = 0; hasMore = false; load(); }

if ('IntersectionObserver' in window) {
  new IntersectionObserver(es => {
    if (es[0].isIntersecting && hasMore && !loading && curTab === 'inbox' && !stack.length) { curPage++; load(); }
  }, { rootMargin: '400px' }).observe($('sentinel'));
}

function setExpiringLabel(total) {
  const c = $('chipExpiring');
  if (c) c.textContent = '🗑 Expiring soon' + (total ? ' (' + total + ')' : '');
}
async function initExpiring() {
  try {
    const data = await api('/api/mails?expiring=1&page=0');
    if (data.ttl_days > 0 && $('chipExpiring')) { $('chipExpiring').hidden = false; setExpiringLabel(data.total); }
  } catch (e) { /* the chip stays hidden */ }
}

let UNREAD = 0;
function setUnread(n) {
  UNREAD = Math.max(0, n | 0);
  const d = $('unreadDot');
  d.hidden = !UNREAD;
  d.textContent = UNREAD > 99 ? '99+' : String(UNREAD);
}
const minsAgo = iso => {
  const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (m < 1) return 'just now';
  if (m < 60) return m + ' min ago';
  if (m < 1440) return Math.round(m / 60) + ' h ago';
  return fmtDate(iso);
};
async function initCodes() {
  try {
    const data = await api('/api/codes');
    const recent = (data.items || []).filter(c => Date.now() - new Date(c.at).getTime() < 3600000);
    const bar = $('codesBar');
    if (!recent.length) { bar.hidden = true; return; }
    const c = recent[0];
    clear(bar).append(h('span', { text: '🔑' }), h('span', { class: 'code', text: c.otp }),
      h('span', { class: 'grow', text: minsAgo(c.at) + ' · ' + (c.from || '') }),
      h('span', { text: recent.length > 1 ? 'All (' + recent.length + ')' : 'All' }));
    bar.onclick = openCodes;
    bar.hidden = false;
  } catch (e) { $('codesBar').hidden = true; }
}

// ===================== Email card =====================
async function openDetail(id) {
  haptic();
  try {
    const m = await api('/api/mails/' + id);
    pushScreen('Email', body => renderDetail(body, m));
    if (m.read === false) {
      api('/api/mails/read', 'POST', { ids: [id], read: true }).then(() => {
        setUnread(UNREAD - 1);
        document.querySelectorAll('#list .row[data-id="' + id + '"]').forEach(r => r.classList.remove('unread'));
      }).catch(() => {});
    }
  } catch (e) { fail(e); }
}

function metaLine(label, val) { return h('div', { class: 'meta' }, label + ': ', h('b', { text: val })); }

const mimeIcon = mt => {
  mt = mt || '';
  if (mt.startsWith('image/')) return '🖼';
  if (mt === 'application/pdf') return '📕';
  if (/zip|rar|7z|tar|gzip/.test(mt)) return '🗜';
  if (mt.startsWith('audio/')) return '🎵';
  if (mt.startsWith('video/')) return '🎞';
  if (mt.startsWith('text/') || /word|document|sheet|excel|presentation/.test(mt)) return '📄';
  return '📎';
};

async function downloadBlob(a) {
  try {
    const res = await fetch('/api/blobs/' + encodeURIComponent(a.id), { headers: { 'X-Init-Data': tg.initData } });
    if (!res.ok) {
      let d = null; try { d = await res.json(); } catch (e) { /* not JSON */ }
      throw new Error((d && d.error) || ('HTTP ' + res.status));
    }
    const url = URL.createObjectURL(await res.blob());
    const link = h('a', { href: url, download: a.name || 'file' });
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    toast('Downloading: ' + (a.name || 'file'));
  } catch (e) { fail(e); }
}

function buildSrcdoc(html, images) {
  const csp = "default-src 'none'; style-src 'unsafe-inline'; img-src data:" + (images ? ' https:' : '') + "; font-src data:";
  return '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + csp + '">' +
    '<meta name="referrer" content="no-referrer"><style>html{background:#fff;color:#000}body{margin:8px;overflow-wrap:anywhere}img{max-width:100%;height:auto}</style></head><body>' + html + '</body></html>';
}

function origView(m) {
  const box = h('div');
  let html = null, images = false;
  const frame = h('iframe', { class: 'orig', sandbox: '', referrerpolicy: 'no-referrer', title: 'Original email' });
  const imgBtn = h('button', { class: 'btn sm', text: '🖼 Show images' });
  imgBtn.onclick = () => { images = true; frame.srcdoc = buildSrcdoc(html, true); imgBtn.textContent = 'Images shown'; imgBtn.setAttribute('disabled', ''); };
  frame.addEventListener('load', () => {
    try { const d = frame.contentDocument; if (d && d.documentElement) frame.style.height = Math.min(d.documentElement.scrollHeight + 16, 4000) + 'px'; } catch (e) { /* sandbox: keeps a fixed height with scrolling */ }
  });
  box.load = async () => {
    if (html !== null) return;
    clear(box).append(...skeleton(1));
    try {
      const r = await api('/api/mails/' + m.id + '/html');
      html = r.html || '';
      clear(box).append(h('div', { class: 'actions', style: 'margin-top:0' }, imgBtn), frame);
      frame.srcdoc = buildSrcdoc(html, images);
    } catch (e) { clear(box).append(emptyBox('⚠️', e.message)); }
  };
  return box;
}

function renderDetail(root, m) {
  const same = (a, b) => (a || '').toLowerCase() === (b || '').toLowerCase();
  const canUnsub = m.unsubscribe && !m.unsubscribed_at;
  const starBtn = h('button', { class: 'btn' });
  const setStar = () => { starBtn.textContent = m.starred ? '⭐ Starred' : '☆ Star'; };
  setStar();
  starBtn.onclick = async () => {
    haptic();
    const want = !m.starred;
    m.starred = want; setStar();
    try { await api('/api/mails/' + m.id + '/star', 'POST', { starred: want }); reload(); }
    catch (e) { m.starred = !want; setStar(); fail(e); }
  };
  const unsubOut = h('div');
  const unsubBtn = canUnsub ? h('button', { class: 'btn', text: '🚫 Unsubscribe' }) : null;
  if (unsubBtn) unsubBtn.onclick = () => doUnsubscribe(m, unsubBtn, unsubOut);

  const acts = [starBtn,
    h('button', { class: 'btn', text: '↩️ Reply', onclick: () => startCompose('reply', m) }),
    h('button', { class: 'btn', text: '➡️ Forward', onclick: () => startCompose('forward', m) }),
    unsubBtn];
  if (m.can_rule) {
    acts.push(h('button', { class: 'btn', text: '🔕 Mute', onclick: () => makeRule(m, 'mute') }));
    acts.push(h('button', { class: 'btn', text: '✅ Allow', onclick: () => makeRule(m, 'allow') }));
  }
  acts.push(h('button', { class: 'btn danger', text: '🗑 Delete', onclick: async () => {
    if (!await confirmBox('Delete the email “' + (m.subject || 'no subject') + '”? This cannot be undone.')) return;
    try { await api('/api/mails/' + m.id, 'DELETE'); haptic('ok'); popScreen(); toast('Email deleted'); reload(); } catch (e) { fail(e); }
  } }));

  const textBox = h('div', { class: 'body', text: m.body || '' });
  let tabs = null;
  let origBox = null;
  if (m.has_html) {
    origBox = origView(m);
    origBox.hidden = true;
    const bT = h('button', { class: 'active', text: 'Text' });
    const bO = h('button', { text: 'Original' });
    const pick = orig => {
      bT.classList.toggle('active', !orig); bO.classList.toggle('active', orig);
      textBox.hidden = orig; origBox.hidden = !orig;
      if (orig) origBox.load();
    };
    bT.onclick = () => pick(false); bO.onclick = () => pick(true);
    tabs = h('div', { class: 'seg' }, bT, bO);
  }
  const atts = (m.attachments || []).map(a => h('div', { class: 'att', onclick: () => downloadBlob(a) },
    h('span', { class: 'ic', text: mimeIcon(a.mime) }),
    h('span', { class: 'nm' }, a.name || 'file', ' ', h('small', { text: fmtSize(a.size || 0) })),
    h('span', { text: '⬇️' })));

  const hdrRows = (m.headers || []).map(x => h('tr', null, h('td', { text: x[0] }), h('td', { text: x[1] })));
  root.append(
    h('div', { class: 'card form' },
      h('h3', { text: m.subject || 'No subject' }),
      h('div', { style: 'height:8px' }),
      metaLine('From', m.from),
      metaLine('To', m.orig_to || m.to),
      m.orig_to && !same(m.orig_to, m.to) ? metaLine('Received at', m.to) : null,
      m.forwarded_by && ![m.orig_to, m.to].some(x => same(x, m.forwarded_by)) ? metaLine('Forwarded by', m.forwarded_by) : null,
      m.alias_note ? h('div', { class: 'meta' }, h('button', { class: 'badge link', text: '🏷 ' + m.alias_note, title: 'All emails to this address', onclick: () => searchBy(aliasQuery(m)) })) : null,
      h('div', { class: 'meta', text: fmtDate(m.date) + ' · ' + (ICONS[m.status] || '') + ' ' + (STATUS_NAMES[m.status] || m.status) }),
      m.expires_at ? h('div', { class: 'meta', text: '🗑 Deleted on ' + fmtDay(m.expires_at) }) : null,
      m.unsubscribed_at ? h('div', { class: 'meta', text: '🚫 You unsubscribed on ' + fmtDay(m.unsubscribed_at) }) : null,
      m.otp ? h('div', { class: 'otp', onclick: () => copyText(m.otp, 'Code copied') },
        h('span', { class: 'code', text: m.otp }), h('span', { class: 'cp', text: 'Copy' })) : null,
      h('div', { class: 'actbar' }, acts),
      unsubOut,
      tabs, textBox, origBox,
      atts.length ? h('div', { style: 'margin-top:16px' }, h('div', { class: 'hint', text: 'Attachments (' + atts.length + ')' }), atts) : null,
      hdrRows.length ? h('details', { class: 'hdrs' }, h('summary', { text: 'Headers' }), h('table', { class: 'hdr-table' }, hdrRows)) : null));
}

async function makeRule(m, action) {
  const target = await sheet(action === 'mute' ? 'Mute sender' : 'Add to allowlist', [
    { label: 'This address only', value: 'address' }, { label: 'Whole domain', value: 'domain' }]);
  if (!target) return;
  try {
    const r = await api('/api/mails/' + m.id + '/rule', 'POST', { action, target });
    toast((action === 'mute' ? '🔕 Muted: ' : '✅ Allowed: ') + (r.pattern || ''));
  } catch (e) { fail(e); }
}

async function doUnsubscribe(m, btn, out) {
  btn.setAttribute('disabled', '');
  const note = html => { clear(out).append(h('div', { class: 'hint', text: html })); };
  try {
    const r = await api('/api/mails/' + m.id + '/unsubscribe', 'POST');
    if (['posted', 'mailed', 'already'].includes(r.status)) {
      note('✅ Unsubscribe request sent'); btn.hidden = true; m.unsubscribed_at = new Date().toISOString(); haptic('ok');
    } else if (r.status === 'link') {
      note('Opening the unsubscribe page…'); btn.removeAttribute('disabled'); tg.openLink(r.info);
    } else if (r.status === 'manual') {
      clear(out).append(h('div', { class: 'hint', text: 'The sender only accepts unsubscribe requests by email. Write from your address to:' }),
        h('div', { class: 'created', text: r.info, onclick: () => copyText(r.info, 'Address copied') }));
      btn.removeAttribute('disabled');
    } else {
      note('⚠️ ' + (r.info || (r.status === 'none' ? 'No way to unsubscribe' : 'Could not unsubscribe')));
      btn.removeAttribute('disabled');
    }
  } catch (e) { note('⚠️ ' + e.message); btn.removeAttribute('disabled'); }
}

// ===================== Compose =====================
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const OTHER = '__other';
const MAX_ATT = 15 * 1024 * 1024;

function extractEmail(s) {
  const m = /<([^>]+)>/.exec(s || '');
  return (m ? m[1] : (s || '')).trim();
}

let toTimer, toSeq = 0;
function setupContacts() {
  const to = $('to'), box = $('toSug');
  const hide = () => { box.hidden = true; clear(box); };
  to.addEventListener('input', () => {
    clearTimeout(toTimer);
    const q = to.value.split(',').pop().trim();
    if (q.length < 2) return hide();
    toTimer = setTimeout(async () => {
      const seq = ++toSeq;
      try {
        const r = await api('/api/contacts?q=' + encodeURIComponent(q));
        if (seq !== toSeq) return;
        clear(box);
        (r.items || []).forEach(a => box.appendChild(h('div', { text: a, onmousedown: ev => ev.preventDefault(), onclick: () => {
          const parts = to.value.split(','); parts.pop();
          to.value = parts.concat(a).map(x => x.trim()).filter(Boolean).join(', ') + ', ';
          hide(); to.focus();
        } })));
        box.hidden = !box.firstChild;
      } catch (e) { hide(); }
    }, 250);
  });
  to.addEventListener('blur', () => setTimeout(hide, 150));
}

function setupCompose() {
  if (MODE_KB) {
    const params = new URLSearchParams(location.search);
    const domain = params.get('domain') || '';
    const aliases = (params.get('aliases') || '').split(',').filter(Boolean);
    $('fromKb').hidden = false;
    aliases.forEach(a => $('aliases').appendChild(h('option', { value: a })));
    if (aliases.length) $('from').value = aliases[0];
    $('domainLabel').textContent = domain ? '@' + domain : '';
    tg.MainButton.setText('Continue → preview');
    tg.MainButton.onClick(() => {
      const data = { from: $('from').value.trim(), to: $('to').value.trim(), subject: $('subject').value.trim(), body: $('body').value };
      if (!data.from) { tg.showAlert('Choose a sender'); return; }
      if (!EMAIL_RE.test(data.to)) { tg.showAlert('Invalid recipient address'); return; }
      const json = JSON.stringify(data);
      if (json.length > 4000) { tg.showAlert('The text is too long for this form (~4000 characters).'); return; }
      tg.sendData(json);
    });
    return;
  }
  const sel = $('fromSel');
  sel.hidden = false;
  clear(sel);
  (ME.from_addresses || []).forEach(a => sel.appendChild(h('option', { value: a, text: a })));
  sel.appendChild(h('option', { value: OTHER, text: 'Other address…' }));
  const dsel = clear($('fromDomain'));
  (ME.domains || []).forEach(d => dsel.appendChild(h('option', { value: d, text: d })));
  sel.onchange = () => { $('fromOther').hidden = sel.value !== OTHER; if (sel.value === OTHER) $('fromLocal').focus(); };
  setupContacts();
  $('attachBox').hidden = false;
  $('sendRow').hidden = false;
  $('fileInput').onchange = onFiles;
  $('sendBtn').onclick = sendMail;
  $('tplSel').onchange = () => {
    const t = (TEMPLATES || []).find(x => x.id === $('tplSel').value);
    if (t) { const b = $('body'); b.value = b.value ? b.value.replace(/\s*$/, '') + '\n\n' + t.text : t.text; haptic(); }
    $('tplSel').value = '';
  };
}

async function ensureTemplates(force) {
  if (TEMPLATES && !force) { fillTplSelect(); return; }
  try { TEMPLATES = (await api('/api/templates')).items || []; } catch (e) { TEMPLATES = TEMPLATES || []; }
  fillTplSelect();
}
function fillTplSelect() {
  const s = $('tplSel');
  clear(s);
  s.appendChild(h('option', { value: '', text: '📝 Template…' }));
  (TEMPLATES || []).forEach(t => s.appendChild(h('option', { value: t.id, text: t.name })));
  s.hidden = !(TEMPLATES && TEMPLATES.length);
}

function renderFiles() {
  const box = clear($('files'));
  compose.attachments.forEach((a, i) => box.appendChild(h('div', { class: 'file' },
    h('span', null, a.name, h('small', { text: fmtSize(a.size) })),
    h('button', { 'aria-label': 'Remove', text: '✕', onclick: () => { compose.attachments.splice(i, 1); renderFiles(); } }))));
  const total = compose.attachments.reduce((s, a) => s + a.size, 0);
  $('attachHint').textContent = total ? 'Total ' + fmtSize(total) + ' of 15 MB' : 'Up to 15 MB total';
}

function readB64(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(',')[1] || '');
    r.onerror = () => rej(new Error('Could not read ' + file.name));
    r.readAsDataURL(file);
  });
}
async function onFiles(ev) {
  const files = Array.from(ev.target.files || []);
  ev.target.value = '';
  let total = compose.attachments.reduce((s, a) => s + a.size, 0);
  for (const f of files) {
    if (total + f.size > MAX_ATT) { toast('Attachment limit is 15 MB. “' + f.name + '” was not added.', true); continue; }
    try {
      const data = await readB64(f);
      compose.attachments.push({ name: f.name, mime: f.type || 'application/octet-stream', data, size: f.size });
      total += f.size;
    } catch (e) { fail(e); }
  }
  renderFiles();
}

function resetCompose() {
  compose = { reply_to: null, forward_of: null, attachments: [] };
  ['to', 'subject', 'body', 'fromLocal'].forEach(i => { $(i).value = ''; });
  renderFiles();
  $('composeBanner').hidden = true;
}

function showBanner(text) {
  const b = clear($('composeBanner'));
  b.append(h('span', { text }), h('button', { class: 'btn sm', text: 'Reset', onclick: resetCompose }));
  b.hidden = false;
}

function startCompose(mode, m) {
  closeAll();
  resetCompose();
  const addrs = ME.from_addresses || [];
  const target = (m.orig_to || m.to || '').toLowerCase();
  const pick = addrs.find(a => a.toLowerCase() === target);
  if (pick) { $('fromSel').value = pick; $('fromOther').hidden = true; }
  else if (target && (ME.domains || []).includes(target.split('@')[1])) {
    $('fromSel').value = OTHER; $('fromOther').hidden = false;
    $('fromLocal').value = target.split('@')[0]; $('fromDomain').value = target.split('@')[1];
  }
  const quote = (m.body || '').split('\n').slice(0, 40).map(l => '> ' + l).join('\n');
  if (mode === 'reply') {
    compose.reply_to = m.id;
    $('to').value = extractEmail(m.from);
    $('subject').value = /^re:/i.test(m.subject || '') ? m.subject : 'Re: ' + (m.subject || '');
    $('body').value = '\n\n' + fmtDate(m.date) + ', ' + m.from + ' wrote:\n' + quote;
    showBanner('↩️ Reply to “' + (m.subject || 'no subject') + '”');
  } else {
    compose.forward_of = m.id;
    $('subject').value = /^(fwd|fw):/i.test(m.subject || '') ? m.subject : 'Fwd: ' + (m.subject || '');
    $('body').value = '\n\n---------- Forwarded message ----------\nFrom: ' + m.from + '\nDate: ' + fmtDate(m.date) + '\nSubject: ' + (m.subject || '') + '\n\n' + (m.body || '');
    showBanner('➡️ Forwarding “' + (m.subject || 'no subject') + '”');
  }
  showTab('compose');
  setTimeout(() => $(mode === 'reply' ? 'body' : 'to').focus(), 50);
}

function currentFrom() {
  const v = $('fromSel').value;
  if (v !== OTHER) return v;
  const l = $('fromLocal').value.trim();
  if (!l) return '';
  return l.includes('@') ? l : l + '@' + $('fromDomain').value;
}

async function sendMail() {
  const data = {
    from: currentFrom(), to: $('to').value.trim(), subject: $('subject').value.trim(), body: $('body').value,
    reply_to: compose.reply_to, forward_of: compose.forward_of,
    attachments: compose.attachments.map(a => ({ name: a.name, mime: a.mime, data: a.data })),
  };
  if (!data.from) return toast('Choose or enter a sender address', true);
  const addrs = data.to.split(',').map(x => x.trim()).filter(Boolean);
  if (!addrs.length || !addrs.every(x => EMAIL_RE.test(x))) return toast('Invalid recipient address', true);
  data.to = addrs.join(', ');
  if (!data.body.trim() && !data.reply_to && !data.forward_of && !data.attachments.length) return toast('Add text or an attachment', true);
  const btn = $('sendBtn');
  btn.setAttribute('disabled', '');
  btn.textContent = 'Sending…';
  try {
    await api('/api/send', 'POST', data);
    toast('✅ Email sent');
    resetCompose();
    showTab('inbox');
    reload();
  } catch (e) { fail(e); }
  finally { btn.removeAttribute('disabled'); btn.textContent = 'Send email'; }
}

// ===================== Addresses =====================
function aliasRow(a) {
  const many = (a.sender_domains || []).length > 1;
  return h('div', { class: 'row static alias' + (a.disabled ? ' off' : '') },
    h('div', { class: 'addr', text: a.address, style: 'cursor:pointer', onclick: () => copyText(a.address, 'Address copied') }),
    a.note ? h('div', { class: 'note', text: a.note }) : null,
    h('div', { class: 'stat', text: a.mail_count + ' ' + plural(a.mail_count, 'email', 'emails') + (a.disabled ? ' · disabled' : '') }),
    many ? h('div', { class: 'warnline', text: '⚠️ ' + a.sender_domains.length + ' different sender domains: ' + a.sender_domains.join(', ') }) : null,
    h('div', { class: 'btns' },
      h('button', { class: 'btn sm', text: a.disabled ? 'Enable' : 'Disable', onclick: async () => {
        haptic();
        try { await api('/api/aliases/' + a.id + '/toggle', 'POST'); loadAliases(); } catch (e) { fail(e); } } }),
      h('button', { class: 'btn sm danger', text: 'Delete', onclick: async () => {
        if (!await confirmBox('Delete address ' + a.address + '?')) return;
        try { await api('/api/aliases/' + a.id, 'DELETE'); haptic('ok'); loadAliases(); } catch (e) { fail(e); } } })));
}

async function loadAliases() {
  const box = $('aliasList');
  if (!box.firstChild) box.append(...skeleton(2));
  try {
    const data = await api('/api/aliases');
    clear(box);
    data.items.forEach(a => box.appendChild(aliasRow(a)));
    $('aliasEmpty').hidden = data.items.length > 0;
    box.hidden = data.items.length === 0;
  } catch (e) { clear(box); fail(e); }
}

$('aliasCreate').onclick = async () => {
  const name = $('aliasName').value.trim();
  if (!name) return toast('Enter an address name', true);
  const btn = $('aliasCreate');
  btn.setAttribute('disabled', '');
  try {
    const a = await api('/api/aliases', 'POST', { name, note: $('aliasNote').value.trim() });
    $('aliasName').value = ''; $('aliasNote').value = '';
    clear($('aliasCreated')).append(h('div', { class: 'hint', text: 'Address created. Tap to copy:' }),
      h('div', { class: 'created', text: a.address, onclick: () => copyText(a.address, 'Address copied') }));
    haptic('ok');
    loadAliases();
  } catch (e) { toast(e.status === 409 ? 'This address already exists' : e.message, true); }
  finally { btn.removeAttribute('disabled'); }
};

let whereTimer, whereSeq = 0;
$('whereQ').addEventListener('input', () => {
  clearTimeout(whereTimer);
  const q = $('whereQ').value.trim();
  if (!q) { clear($('whereRes')); return; }
  whereTimer = setTimeout(async () => {
    const seq = ++whereSeq;
    const box = clear($('whereRes'));
    box.append(h('div', { style: 'margin-top:12px' }, skeleton(2)));
    try {
      const data = await api('/api/whereis?q=' + encodeURIComponent(q));
      if (seq !== whereSeq) return;
      clear(box);
      if (!data.items.length) { box.append(h('div', { class: 'hint', style: 'margin-top:12px', text: 'No emails from “' + q + '” found. Check the spelling or try part of the address.' })); return; }
      box.append(h('div', { style: 'height:12px' }), ...data.items.map(it => h('div', { class: 'where row static', style: 'padding-left:0;padding-right:0' },
        h('div', { class: 'to', text: 'To ' + it.to }),
        it.via && it.via !== it.to ? h('div', { class: 'note', text: 'via ' + it.via }) : null,
        h('div', { class: 'stat', text: it.count + ' ' + plural(it.count, 'email', 'emails') + ' · ' + fmtDay(it.first) + (it.last && it.last !== it.first ? ' – ' + fmtDay(it.last) : '') }),
        it.sample_from ? h('div', { class: 'note', text: 'For example, from ' + it.sample_from }) : null)));
    } catch (e) { if (seq === whereSeq) { clear(box); fail(e); } }
  }, 400);
});

// ===================== More =====================
function buildMore() {
  const items = [
    { ic: '📰', t: 'Newsletters', s: 'Newsletter senders: unsubscribe or mute', f: openNewsletters },
    { ic: '🔑', t: 'Codes', s: 'Recent verification codes', f: openCodes },
    { ic: '📊', t: 'Statistics', s: 'How many emails and from where', f: openStats },
    { ic: '📝', t: 'Templates', s: 'Ready-made email texts', f: openTemplates },
    { ic: '🔕', t: 'Rules', s: 'Mute or allow a sender', f: openRules },
    { ic: '⚙️', t: 'Settings', s: 'Quiet hours, digest, code masking', f: openSettings },
  ];
  if (ME.is_admin) items.push(
    { ic: '📥', t: 'Unassigned', s: 'Emails without an owner', f: openUnassigned },
    { ic: '🧭', t: 'Routes', s: 'Which addresses belong to whom', f: openRoutes },
    { ic: '🖥', t: 'Server', s: 'Disk, memory, containers, bans', f: openServer });
  const menu = clear($('moreMenu'));
  items.forEach(i => menu.appendChild(h('div', { class: 'row', onclick: () => { haptic(); i.f(); } },
    h('span', { class: 'ic', text: i.ic }), h('span', { class: 'tx' }, i.t, h('small', { text: i.s })))));
  const help = [
    ['📬 Inbox', 'Email list, search, filters. The bot\'s "Full" button opens the whole email here.'],
    ['⭐ ↩️ ➡️', 'In the email card: star, reply and forward. The same buttons are under the message in the bot.'],
    ['🚫 Unsubscribe', 'Sends an unsubscribe request or opens the sender\'s page. Shown when the email is a newsletter.'],
    ['🔕 Mute / ✅ Allow', 'A rule for an address or domain. Available in the email card; the rule list is under "Rules". Shared rules are visible to everyone but only the admin can delete them.'],
    ['📰 Newsletters', 'All newsletter senders in one list, with bulk unsubscribe and mute.'],
    ['🔑 Codes', 'Recent verification codes; tap to copy a code.'],
    ['✍️ Compose', 'Send an email with attachments. The bot\'s "✅ Send" button does the same, without attachments.'],
    ['🔎 Where did I sign up', 'The Addresses tab, search field at the top.'],
    ['📊 Statistics', 'Section "More → Statistics".'],
  ];
  const hb = clear($('moreHelp'));
  hb.append(h('h2', { text: 'What is where' }), help.map(x => h('p', null, h('b', { text: x[0] + ': ' }), x[1])));
}

async function openStats() {
  pushScreen('Statistics', async body => {
    body.append(...skeleton(2));
    try {
      const s = await api('/api/stats');
      clear(body);
      const tile = (n, l, sub) => h('div', { class: 'tile' }, h('div', { class: 'n', text: String(n) }), h('div', { class: 'l', text: l }), sub ? h('div', { class: 's', text: sub }) : null);
      const tiles = [tile(s.total, 'Total emails'), tile(s.starred, '⭐ Starred'), tile(s.aliases, '🏷 Addresses')];
      if (s.routes != null) tiles.push(tile(s.routes, '🧭 Routes'));
      const last = Object.values(s.last_24h || {}).reduce((a, b) => a + b, 0);
      tiles.splice(1, 0, tile(last, 'Last 24 hours'));
      body.append(h('div', { class: 'tiles' }, tiles));
      const keys = Object.keys(s.by_status || {});
      if (keys.length) body.append(h('div', { class: 'card list' }, keys.map(k => h('div', { class: 'row static' },
        h('div', { class: 'line' }, h('div', { class: 'grow', text: (ICONS[k] || '•') + ' ' + (STATUS_NAMES[k] || k) }),
          h('b', { text: String(s.by_status[k]) }),
          s.last_24h && s.last_24h[k] ? h('span', { class: 'badge', text: '+' + s.last_24h[k] + ' in 24 h' }) : null)))));
    } catch (e) { clear(body); body.append(emptyBox('⚠️', e.message)); }
  });
}

function openTemplates() {
  pushScreen('Templates', async body => {
    const nameI = h('input', { placeholder: 'Name', maxlength: '60' });
    const textI = h('textarea', { placeholder: 'Template text', style: 'min-height:110px' });
    const listBox = h('div', { class: 'card list' });
    const emptyB = emptyBox('📝', 'No templates yet. Add the first one below.');
    const render = () => {
      clear(listBox);
      emptyB.hidden = TEMPLATES.length > 0;
      listBox.hidden = TEMPLATES.length === 0;
      TEMPLATES.forEach(t => listBox.appendChild(h('div', { class: 'row static' },
        h('div', { class: 'line' },
          h('div', { class: 'grow' }, h('div', { class: 'addr', text: t.name }), h('div', { class: 'note', text: t.text, style: 'white-space:pre-wrap;max-height:4.2em;overflow:hidden' })),
          h('button', { class: 'btn sm danger', text: 'Delete', onclick: async () => {
            if (!await confirmBox('Delete template “' + t.name + '”?')) return;
            try { await api('/api/templates/' + t.id, 'DELETE'); TEMPLATES = TEMPLATES.filter(x => x.id !== t.id); fillTplSelect(); render(); haptic('ok'); } catch (e) { fail(e); } } })))));
    };
    body.append(listBox, emptyB, h('div', { class: 'card form' }, h('h2', { text: 'New template' }), nameI, h('div', { style: 'height:8px' }), textI,
      h('div', { class: 'actions' }, h('button', { class: 'btn primary', text: 'Save template', onclick: async () => {
        const name = nameI.value.trim(), text = textI.value;
        if (!name || !text.trim()) return toast('Fill in the name and text', true);
        try {
          const r = await api('/api/templates', 'POST', { name, text });
          TEMPLATES.push(r.item); nameI.value = ''; textI.value = ''; fillTplSelect(); render(); toast('Template saved');
        } catch (e) { fail(e); } } }))));
    listBox.append(...skeleton(2));
    await ensureTemplates(true);
    render();
  });
}

function openUnassigned() {
  pushScreen('Unassigned', async body => {
    body.append(...skeleton(3));
    try {
      const data = await api('/api/unassigned');
      clear(body);
      if (!data.items.length) { body.append(emptyBox('🎉', 'No unassigned emails.')); return; }
      const card = h('div', { class: 'card list' });
      data.items.forEach(m => {
        const row = mailRow(m);
        row.append(h('div', { class: 'btns' }, h('button', { class: 'btn sm primary', text: '👤 Me', onclick: async ev => {
          ev.stopPropagation();
          try { await api('/api/mails/' + m.id + '/assign', 'POST'); row.remove(); haptic('ok'); toast('Email assigned to you');
            if (!card.firstChild) { card.remove(); body.append(emptyBox('🎉', 'No more unassigned emails.')); } } catch (e) { fail(e); } } })));
        card.appendChild(row);
      });
      body.append(card);
    } catch (e) { clear(body); body.append(emptyBox('⚠️', e.message)); }
  });
}

function openRoutes() {
  pushScreen('Routes', async body => {
    const listBox = h('div', { class: 'card list' });
    const kind = h('select', null, h('option', { value: 'prefix', text: 'Address prefix' }), h('option', { value: 'domain', text: 'Domain' }));
    const val = h('input', { placeholder: 'shop or example.com', autocapitalize: 'none', autocomplete: 'off' });
    const user = h('select', null, (ME.users || []).map(u => h('option', { value: u.user_id, text: u.user_id + (u.domain ? ' · ' + u.domain : '') })));
    const load = async () => {
      clear(listBox).append(...skeleton(2));
      try {
        const data = await api('/api/routes');
        clear(listBox);
        if (!data.items.length) { listBox.append(emptyBox('🧭', 'No routes. Add the first one below.')); return; }
        data.items.forEach(r => {
          const sw = h('button', { class: 'switch' + (r.disabled ? '' : ' on'), 'aria-label': 'Enable or disable' });
          sw.onclick = async () => {
            haptic();
            try { const x = await api('/api/routes/toggle', 'POST', { kind: r.kind, value: r.value }); r.disabled = !!x.disabled; sw.classList.toggle('on', !r.disabled); row.classList.toggle('off', r.disabled); } catch (e) { fail(e); }
          };
          const row = h('div', { class: 'row static alias' + (r.disabled ? ' off' : '') },
            h('div', { class: 'line' },
              h('div', { class: 'grow' }, h('div', { class: 'addr', text: r.value }),
                h('div', { class: 'note', text: (r.kind === 'domain' ? 'domain' : 'prefix') + ' → ' + r.user_id })),
              sw,
              h('button', { class: 'btn sm danger', text: '✕', 'aria-label': 'Delete', onclick: async () => {
                if (!await confirmBox('Delete route ' + r.value + '?')) return;
                try { await api('/api/routes/delete', 'POST', { kind: r.kind, value: r.value }); haptic('ok'); load(); } catch (e) { fail(e); } } })));
          listBox.appendChild(row);
        });
      } catch (e) { clear(listBox).append(emptyBox('⚠️', e.message)); }
    };
    body.append(listBox, h('div', { class: 'card form' }, h('h2', { text: 'New route' }),
      h('label', { text: 'Type' }), kind, h('label', { text: 'Value' }), val, h('label', { text: 'User' }), user,
      h('div', { class: 'actions' }, h('button', { class: 'btn primary', text: 'Add route', onclick: async () => {
        const value = val.value.trim();
        if (!value) return toast('Enter a value', true);
        try { await api('/api/routes', 'POST', { kind: kind.value, value, user_id: Number(user.value) || user.value }); val.value = ''; toast('Route added'); load(); } catch (e) { fail(e); } } }))));
    load();
  });
}

function openRules() {
  pushScreen('Rules', async body => {
    const listBox = h('div', { class: 'card list' });
    const pat = h('input', { placeholder: 'news@example.com or @example.com', autocapitalize: 'none', autocomplete: 'off' });
    const act = h('select', null, h('option', { value: 'mute', text: '🔕 Mute' }), h('option', { value: 'allow', text: '✅ Allow' }));
    const load = async () => {
      clear(listBox).append(...skeleton(2));
      try {
        const data = await api('/api/rules');
        clear(listBox);
        if (!data.items.length) { listBox.append(emptyBox('🔕', 'No rules. Add one below or from the email card.')); return; }
        data.items.forEach(r => listBox.appendChild(h('div', { class: 'row static' }, h('div', { class: 'line' },
          h('span', { text: r.action === 'mute' ? '🔕' : '✅' }),
          h('div', { class: 'grow' }, h('div', { class: 'addr', text: r.pattern }), h('div', { class: 'note', text: (r.action === 'mute' ? 'muted' : 'allowlist') + (r.user_id == null && !ME.is_admin ? ' · shared' : r.user_id == null ? ' · for everyone' : '') })),
          (ME.is_admin || r.user_id != null) ? h('button', { class: 'btn sm danger', text: 'Delete', onclick: async () => {
            if (!await confirmBox('Delete rule ' + r.pattern + '?')) return;
            try { await api('/api/rules/' + r.id, 'DELETE'); haptic('ok'); load(); } catch (e) { fail(e); } } }) : null))));
      } catch (e) { clear(listBox).append(emptyBox('⚠️', e.message)); }
    };
    body.append(listBox, h('div', { class: 'card form' }, h('h2', { text: 'New rule' }),
      h('label', { text: 'Sender address or domain' }), pat, h('label', { text: 'Action' }), act,
      h('div', { class: 'actions' }, h('button', { class: 'btn primary', text: 'Add rule', onclick: async () => {
        const pattern = pat.value.trim();
        if (!pattern) return toast('Enter a pattern', true);
        try { await api('/api/rules', 'POST', { pattern, action: act.value }); pat.value = ''; toast('Rule added'); load(); } catch (e) { fail(e); } } }))));
    load();
  });
}

// ===================== Newsletters =====================
function unsubOutcome(r, noteEl, auto) {
  const st = stOf(r), info = r && typeof r === 'object' ? r.info : null;
  clear(noteEl);
  if (['posted', 'already', 'mailed'].includes(st)) { noteEl.append(h('span', { class: 'badge', text: '✅ Unsubscribe request sent' })); return true; }
  if (st === 'link') {
    if (info) { noteEl.append(h('button', { class: 'btn sm', text: 'Open unsubscribe page', onclick: () => tg.openLink(info) })); if (auto) tg.openLink(info); }
    else noteEl.append(h('span', { class: 'badge warn', text: 'Unsubscribe page needed: tap "Unsubscribe" on the row' }));
    return false;
  }
  if (st === 'manual') {
    if (info) noteEl.append(h('div', { class: 'hint', text: 'The sender only accepts unsubscribe requests by email. Write to:' }), h('div', { class: 'created', text: info, onclick: () => copyText(info, 'Address copied') }));
    else noteEl.append(h('span', { class: 'badge warn', text: 'Email-only unsubscribe: tap "Unsubscribe" on the row' }));
    return false;
  }
  noteEl.append(h('span', { class: 'badge warn', text: '⚠️ ' + (info || (st === 'none' ? 'No way to unsubscribe' : 'Could not unsubscribe')) }));
  return false;
}

function openNewsletters() {
  pushScreen('Newsletters', async body => {
    body.append(...skeleton(3));
    let items;
    try { items = (await api('/api/newsletters')).items || []; }
    catch (e) { clear(body).append(emptyBox('⚠️', e.message)); return; }
    clear(body);
    if (!items.length) { body.append(emptyBox('📰', 'No newsletters yet.')); return; }
    const picked = new Set();
    const rows = new Map();
    const bulk = h('div', { class: 'bulkbar', hidden: true });
    const upd = () => {
      bulk.hidden = !picked.size;
      clear(bulk).append(
        h('button', { class: 'btn', text: '🚫 Unsubscribe (' + picked.size + ')', onclick: bulkUnsub }),
        h('button', { class: 'btn', text: '🔕 Mute (' + picked.size + ')', onclick: () => bulkMute([...picked]) }));
    };
    const markMuted = n => { n.muted = true; const r = rows.get(n.sender); if (r) r.badges.append(h('span', { class: 'badge', text: 'muted' })); };
    async function bulkMute(senders) {
      const target = await sheet('Mute ' + senders.length + ' ' + plural(senders.length, 'sender', 'senders'), [
        { label: 'Addresses only', value: 'address' }, { label: 'Whole domains', value: 'domain' }]);
      if (!target) return;
      try {
        await api('/api/newsletters/mute', 'POST', { senders, target });
        items.filter(n => senders.includes(n.sender) && !n.muted).forEach(markMuted);
        toast('🔕 Muted: ' + senders.length);
      } catch (e) { fail(e); }
    }
    async function bulkUnsub() {
      const senders = [...picked].filter(s => { const n = items.find(x => x.sender === s); return n && !n.unsubscribed; });
      if (!senders.length) return toast('The selected senders are already unsubscribed', true);
      if (!await confirmBox('Unsubscribe from the selected newsletters: ' + senders.length + '?')) return;
      bulk.setAttribute('hidden', '');
      try {
        const r = await api('/api/newsletters/unsubscribe', 'POST', { senders: senders.slice(0, 50) });
        let ok = 0, rest = 0;
        for (const s of senders) {
          const v = (r.results || {})[s];
          const row = rows.get(s), n = items.find(x => x.sender === s);
          if (v === undefined || !row) continue;
          if (unsubOutcome(v, row.note) ) { ok++; n.unsubscribed = true; row.unsubBtn.hidden = true; row.badges.append(h('span', { class: 'badge', text: 'unsubscribed' })); }
          else rest++;
        }
        toast('Unsubscribe: done ' + ok + (rest ? ', needs attention ' + rest : ''), !ok && rest > 0);
        picked.clear(); rows.forEach(x => { x.cb.checked = false; }); upd();
      } catch (e) { fail(e); upd(); }
    }
    const card = h('div', { class: 'card list' });
    items.forEach(n => {
      const cb = h('input', { type: 'checkbox', class: 'cbx', 'aria-label': 'Select' });
      cb.onchange = () => { if (cb.checked) picked.add(n.sender); else picked.delete(n.sender); haptic(); upd(); };
      const badges = h('div', { class: 'tags', style: 'margin-top:4px' },
        n.unsubscribed ? h('span', { class: 'badge', text: 'unsubscribed' }) : null,
        n.muted ? h('span', { class: 'badge', text: 'muted' }) : null);
      const note = h('div');
      const unsubBtn = h('button', { class: 'btn sm', text: '🚫 Unsubscribe', hidden: !!n.unsubscribed || !n.unsubscribe });
      unsubBtn.onclick = async () => {
        unsubBtn.setAttribute('disabled', '');
        try {
          const r = await api('/api/mails/' + n.latest_mail_id + '/unsubscribe', 'POST');
          if (unsubOutcome(r, note, true)) { n.unsubscribed = true; unsubBtn.hidden = true; badges.append(h('span', { class: 'badge', text: 'unsubscribed' })); haptic('ok'); }
        } catch (e) { fail(e); }
        unsubBtn.removeAttribute('disabled');
      };
      const row = h('div', { class: 'row static nl' },
        h('div', { class: 'line' }, cb,
          h('div', { class: 'grow' },
            h('div', { class: 'nm', text: n.name || n.sender }),
            h('div', { class: 'note', text: n.sender }),
            h('div', { class: 'stat', text: n.count + ' ' + plural(n.count, 'email', 'emails') + ' · latest ' + fmtDate(n.last) }),
            badges)),
        note,
        h('div', { class: 'btns' }, unsubBtn,
          h('button', { class: 'btn sm', text: '🔕 Mute', onclick: () => bulkMute([n.sender]) }),
          h('button', { class: 'btn sm', text: 'Email', onclick: () => openDetail(n.latest_mail_id) })));
      rows.set(n.sender, { note, badges, unsubBtn, cb });
      card.appendChild(row);
    });
    body.append(h('div', { class: 'hint', text: 'Select senders to unsubscribe or mute several at once.' }), card, bulk);
  });
}

// ===================== Codes =====================
function openCodes() {
  pushScreen('Codes', async body => {
    body.append(...skeleton(3));
    try {
      const data = await api('/api/codes');
      clear(body);
      if (!(data.items || []).length) { body.append(emptyBox('🔑', 'No codes recently.')); return; }
      body.append(h('div', { class: 'hint', text: 'Tap a code to copy it; tap the text to open the email.' }),
        h('div', { class: 'card list' }, data.items.map(c => h('div', { class: 'row static' }, h('div', { class: 'codeitem' },
          h('div', { class: 'big', text: c.otp, onclick: () => copyText(c.otp, 'Code copied') }),
          h('div', { class: 'txt', onclick: () => openDetail(c.mail_id) },
            h('div', { class: 'addr', text: c.from || '' }),
            h('div', { class: 'note', text: c.subject || 'no subject' }),
            h('div', { class: 'stat', text: minsAgo(c.at) })))))));
    } catch (e) { clear(body).append(emptyBox('⚠️', e.message)); }
  });
}

// ===================== Settings =====================
const POP_TZ = ['Europe/Kyiv', 'Europe/Moscow', 'Europe/Minsk', 'Europe/Warsaw', 'Europe/Berlin', 'Europe/London', 'Asia/Almaty', 'Asia/Tbilisi', 'Asia/Dubai', 'America/New_York', 'UTC'];
function openSettings() {
  pushScreen('Settings', async body => {
    body.append(...skeleton(3));
    let s;
    try { s = await api('/api/settings'); } catch (e) { clear(body).append(emptyBox('⚠️', e.message)); return; }
    clear(body);
    const sw = on => h('button', { class: 'switch' + (on ? ' on' : ''), type: 'button' });
    const toggle = (b, cb) => { b.onclick = () => { haptic(); b.classList.toggle('on'); cb && cb(b.classList.contains('on')); }; };
    const quietOn = sw(!!(s.quiet_from && s.quiet_to));
    const qf = h('input', { type: 'time', value: s.quiet_from || '23:00' });
    const qt = h('input', { type: 'time', value: s.quiet_to || '08:00' });
    const qBox = h('div', { class: 'two-t', hidden: !quietOn.classList.contains('on') }, h('div', null, h('label', { text: 'From' }), qf), h('div', null, h('label', { text: 'To' }), qt));
    toggle(quietOn, on => { qBox.hidden = !on; });
    const tzs = POP_TZ.includes(s.tz) || !s.tz ? POP_TZ : [s.tz].concat(POP_TZ);
    const tz = h('select', null, tzs.map(z => h('option', { value: z, text: z })));
    tz.value = s.tz || 'UTC';
    const digOn = sw(!!s.digest_enabled);
    const digT = h('input', { type: 'time', value: s.digest_time || '09:00' });
    const digBox = h('div', { hidden: !s.digest_enabled }, h('label', { text: 'Digest time' }), digT);
    toggle(digOn, on => { digBox.hidden = !on; });
    const otp = h('input', { type: 'number', min: '0', max: '1440', value: String(s.otp_hide_minutes == null ? 0 : s.otp_hide_minutes), inputmode: 'numeric' });
    body.append(
      h('div', { class: 'card form' }, h('div', { class: 'setrow' }, h('div', null, h('h2', { text: 'Quiet hours', style: 'margin:0' }), h('div', { class: 'hint', style: 'margin:2px 0 0', text: 'Silent notifications. Emails with a code still arrive with sound.' })), quietOn), qBox),
      h('div', { class: 'card form' }, h('h2', { text: 'Time zone' }), tz),
      h('div', { class: 'card form' }, h('div', { class: 'setrow' }, h('h2', { text: 'Digest', style: 'margin:0' }), digOn), digBox),
      h('div', { class: 'card form' }, h('h2', { text: 'Mask codes' }), h('div', { class: 'hint', text: 'Minutes until a code in the bot message is replaced with a mask. 0 means never mask.' }), otp),
      h('div', { class: 'actions' }, h('button', { class: 'btn primary wide', text: 'Save settings', onclick: async ev => {
        const mins = parseInt(otp.value, 10);
        if (isNaN(mins) || mins < 0 || mins > 1440) return toast('Masking minutes: 0 to 1440', true);
        const on = quietOn.classList.contains('on');
        const patch = { quiet_from: on ? qf.value : null, quiet_to: on ? qt.value : null, tz: tz.value,
          digest_enabled: digOn.classList.contains('on'), digest_time: digT.value || '09:00', otp_hide_minutes: mins };
        const btn = ev.currentTarget; btn.setAttribute('disabled', '');
        try { await api('/api/settings', 'POST', patch); haptic('ok'); toast('Settings saved'); } catch (e) { fail(e); }
        btn.removeAttribute('disabled');
      } })));
  });
}

// ===================== Server =====================
function openServer() {
  pushScreen('Server', (body) => {
    const load = async () => {
      clear(body).append(...skeleton(3));
      let s;
      try { s = await api('/api/server'); } catch (e) { clear(body).append(emptyBox('⚠️', e.message)); return; }
      clear(body);
      const tile = (n, l, sub, warn) => h('div', { class: 'tile' + (warn ? ' warn' : '') }, h('div', { class: 'n', text: String(n) }), h('div', { class: 'l', text: l }), sub ? h('div', { class: 's', text: sub }) : null);
      const stale = !s.status_updated_at || Date.now() - new Date(s.status_updated_at).getTime() > 300000;
      if (stale) body.append(h('div', { class: 'stale', text: '⚠️ Host data is stale' + (s.status_updated_at ? ' (updated ' + minsAgo(s.status_updated_at) + ')' : '') + '. Check newmail-status.timer.' }));
      const t = [];
      if (s.disk) t.push(tile(s.disk.pct + '%', 'Disk', 'free ' + s.disk.free_mb + ' MB', s.disk.pct > 85));
      if (s.mem) { t.push(tile(s.mem.avail_mb + ' MB', 'Memory available', null, s.mem.avail_mb < 150)); t.push(tile(s.mem.swap_pct + '%', 'Swap', null, s.mem.swap_pct > 50)); }
      t.push(tile(s.cert_days == null ? '—' : s.cert_days + ' d', 'Certificate', null, s.cert_days != null && s.cert_days < 14));
      t.push(tile(s.last_backup ? s.last_backup.size_mb + ' MB' : '—', 'Last backup', s.last_backup ? fmtDay(s.last_backup.date) : 'no data'));
      t.push(tile(s.failed_24h, 'Failed in 24 h', null, s.failed_24h > 0));
      t.push(tile(s.postfix_queue == null ? '—' : s.postfix_queue, 'Postfix queue', null, s.postfix_queue > 20));
      body.append(h('div', { class: 'tiles' }, t));
      if (s.containers && s.containers.length) body.append(h('div', { class: 'card list' }, s.containers.map(c => h('div', { class: 'row static' }, h('div', { class: 'line' },
        h('div', { class: 'grow' }, h('div', { class: 'addr', text: c.name }), h('div', { class: 'note', text: c.status || '' })),
        h('span', { class: 'badge ' + (c.state === 'running' ? 'state-ok' : 'state-bad'), text: c.state }))))));
      if (s.fail2ban) {
        const banned = s.fail2ban.banned || [];
        body.append(h('div', { class: 'card form' }, h('h2', { text: 'fail2ban' }),
          h('div', { class: 'hint', text: 'Total failed logins: ' + (s.fail2ban.total_failed || 0) + '. Banned: ' + banned.length + '.' }),
          banned.map(ip => h('div', { class: 'setrow' }, h('span', { class: 'addr', text: ip }),
            h('button', { class: 'btn sm', text: 'Unban', onclick: async () => {
              if (!await confirmBox('Unban ' + ip + '?')) return;
              try { await api('/api/server/unban', 'POST', { ip }); haptic('ok'); toast('Unban request sent: ' + ip); } catch (e) { fail(e); }
            } })))));
      }
      body.append(h('div', { class: 'actions' }, h('button', { class: 'btn wide', text: '🔄 Refresh', onclick: load })));
    };
    load();
  });
}

// ===================== Start =====================
async function init() {
  if (MODE_KB) { setupCompose(); showTab('compose'); return; }
  $('tabbar').hidden = false;
  try {
    ME = await api('/api/me');
  } catch (e) {
    ME = { is_admin: false, from_addresses: [], users: [], domains: [] };
    fail(e);
  }
  setupCompose();
  buildChips();
  buildMore();
  if (typeof ME.unread === 'number') setUnread(ME.unread);
  showTab('inbox');
  initExpiring();
  initCodes();
}
init();
