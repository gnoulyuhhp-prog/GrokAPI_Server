import * as api from './api.js?v=3.4';
import { toast } from './toast.js?v=3.3';

const getTools = api.getTools;
const getToolStats = api.getToolStats;
const getToolResults = api.getToolResults;
const getCurrentJob = api.getCurrentJob;
const startJob = api.startJob;
const stopJob = api.stopJob;
const clearJobLogs = api.clearJobLogs;
const clearToolLogs = api.clearToolLogs;
const getConfigSummary = api.getConfigSummary;
const updateConfig = api.updateConfig;
const getHealth = api.getHealth;
const getHotmails =
  typeof api.getHotmails === 'function'
    ? api.getHotmails
    : async () => ({ count: 0, accounts: [], slots: 0 });
const importHotmails =
  typeof api.importHotmails === 'function'
    ? api.importHotmails
    : async () => {
        throw new Error('API Hotmail chưa sẵn sàng — restart web server');
      };

const generateSub2apiKeys = api.generateSub2apiKeys;
const listSub2apiKeys = api.listSub2apiKeys;

const PAGE_META = {
  '#/register': { title: 'Bảng điều khiển', eyebrow: 'Thiết lập và theo dõi các phiên đăng ký.' },
  '#/results': { title: 'Kho tài khoản', eyebrow: 'Quản lý tài khoản và trạng thái xử lý.' },
  '#/logs': { title: 'Dòng sự kiện', eyebrow: 'Theo dõi tiến trình và lỗi theo thời gian thực.' },
  '#/keys': { title: 'Tạo Key API Bán Lẻ', eyebrow: 'Tạo nhanh API Key theo gói Token không giới hạn ngày.' },
  '#/settings': { title: 'Thiết lập', eyebrow: 'Quản lý kết nối và hành vi mặc định của hệ thống.' },
  '#/tools': { title: 'Hệ sinh thái', eyebrow: 'Danh sách nền tảng đang kết nối với Nexus Ops.' },
};

const state = {
  tools: [],
  selectedTool: 'grok',
  form: {},
  job: null,
  logSeq: 0,
  pollTimer: null,
  autoScroll: true,
  hotmailDraft: '',
  hotmailPool: null,
};

// Keep each route mounted after its first render. Besides making navigation
// instant, this preserves form values, scroll position and live log state.
const routeViews = new Map();
const routeLoads = new Map();

function isHotmailMail(val) {
  const v = String(val ?? '').trim().toLowerCase();
  return v === '1' || v === 'hotmail' || v === 'outlook' || v === 'ms';
}

function isSheetOnly(id) {
  return id === 'heygen' || id === 'capcut' || id === 'zai' || id === 'canva';
}

function brandIconSrc(t) {
  if (t.brand_icon) return t.brand_icon;
  return `/static/img/brands/${t.id}.svg`;
}

function brandIconHtml(t) {
  const src = brandIconSrc(t);
  const name = t.name || t.id || '';
  return `<div class="ico brand-official" data-brand="${esc(t.id)}">
    <img src="${esc(src)}" alt="${esc(name)}" width="40" height="40"
      onerror="this.onerror=null;this.remove();this.parentElement.classList.add('is-fallback');" />
    <span class="ico-fallback">${esc(t.icon || '')}</span>
  </div>`;
}

/* ── Theme / chrome ── */
function initChrome() {
  const btn = document.getElementById('theme-toggle');
  btn?.addEventListener('click', () => {
    const root = document.documentElement;
    const sequence = Number(root.dataset.themeSequence || 0) + 1;
    root.dataset.themeSequence = String(sequence);
    root.classList.add('theme-switching');
    const dark = root.classList.toggle('dark-theme');
    localStorage.setItem('nexus-theme-v4', dark ? 'dark' : 'light');
    root.dataset.theme = dark ? 'dark' : 'light';
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (root.dataset.themeSequence === String(sequence)) {
          root.classList.remove('theme-switching');
        }
      });
    });
  });

  const menu = document.getElementById('mobile-menu');
  const sidebar = document.getElementById('sidebar');
  const scrim = document.getElementById('sidebar-scrim');
  const close = () => {
    sidebar?.classList.remove('is-open');
    scrim?.classList.remove('is-open');
  };
  menu?.addEventListener('click', () => {
    sidebar?.classList.toggle('is-open');
    scrim?.classList.toggle('is-open');
  });
  scrim?.addEventListener('click', close);
  document.querySelectorAll('.nav-item').forEach((a) => {
    a.addEventListener('click', close);
  });
}

function setActiveNav(hash) {
  document.querySelectorAll('.nav-item').forEach((a) => {
    a.classList.toggle('is-active', a.dataset.route === hash);
  });
  const meta = PAGE_META[hash] || PAGE_META['#/register'];
  const t = document.getElementById('page-title');
  const e = document.getElementById('page-eyebrow');
  if (t) t.textContent = meta.title;
  if (e) e.textContent = meta.eyebrow;
  document.title = `${meta.title} · Nexus Ops`;
}

function revealLiveLog() {
  const panel = document.querySelector('.console-card') || document.getElementById('log-box');
  if (!panel) return;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  panel.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
  const box = document.getElementById('log-box');
  if (box) box.scrollTop = box.scrollHeight;
}

function updateRunPill(job) {
  const pill = document.getElementById('run-pill');
  const text = document.getElementById('run-pill-text');
  if (!pill || !text) return;
  const running = job && ['running', 'pending', 'stopping'].includes(job.status);
  pill.classList.toggle('is-running', !!running);
  if (!job || job.status === 'idle') text.textContent = 'Sẵn sàng';
  else text.textContent = `${job.tool_id || 'job'} · ${job.status}`;
}

function normalizedProgress(job) {
  if (!job) return null;
  const source = job.progress || {};
  const total = Math.max(0, Number(source.total ?? job.params?.count ?? 0) || 0);
  let completed = Math.max(0, Number(source.completed) || 0);
  if (job.status === 'done' && total > 0 && completed === 0) completed = total;
  completed = total > 0 ? Math.min(total, completed) : completed;
  const ok = Math.min(completed, Math.max(0, Number(source.ok) || 0));
  const failed = Math.min(completed, Math.max(0, Number(source.failed) || 0));
  const percent = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : null;
  return { completed, total, ok, failed, percent, continuous: total === 0 };
}

function formatElapsed(job) {
  const started = Number(job?.created_at || job?.started_at || 0);
  if (!started) return '00:00:00';
  const finished = Number(job?.ended_at || 0);
  const end = finished > 0 ? finished : Date.now() / 1000;
  const seconds = Math.max(0, Math.floor(end - started));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = seconds % 60;
  return [hours, minutes, remaining].map((value) => String(value).padStart(2, '0')).join(':');
}

function progressContent(job) {
  const p = normalizedProgress(job);
  if (!p) return '';
  const status = String(job?.status || 'idle').toLowerCase();
  const active = ['running', 'pending', 'stopping'].includes(status);
  const countText = p.continuous
    ? `${p.completed} tài khoản đã xử lý <small>· liên tục</small>`
    : `${p.completed} / ${p.total} tài khoản`;
  let stateText = `${p.percent ?? 0}%`;
  if (p.continuous) stateText = active ? '<span class="progress-live-pill">ĐANG CHẠY</span>' : '<span class="progress-rest-pill">ĐÃ DỪNG</span>';
  const width = p.continuous ? Math.min(100, p.completed * 5) : p.percent;
  return `
    <div class="progress-meta">
      <strong>${countText}</strong>
      <span>${stateText}</span>
    </div>
    <div class="progress-track${active ? ' is-active' : ''}" role="progressbar"
      aria-label="Tiến trình đăng ký" aria-valuemin="0"
      ${p.continuous ? '' : `aria-valuemax="${p.total}" aria-valuenow="${p.completed}"`}>
      <i style="width:${width}%"></i>
    </div>
    <div class="progress-detail">
      <span class="progress-ok">${p.ok} thành công</span>
      <span class="progress-fail">${p.failed} thất bại</span>
      <span class="progress-elapsed">${active ? 'Đang chạy' : 'Tổng thời gian'} <strong>${formatElapsed(job)}</strong></span>
    </div>`;
}

function shouldShowProgress(job) {
  if (!job) return false;
  const p = normalizedProgress(job);
  const status = String(job.status || 'idle').toLowerCase();
  return status !== 'idle' || Number(p?.completed || 0) > 0;
}

function progressPanelState(job) {
  const status = String(job?.status || 'idle').toLowerCase();
  return ['running', 'pending', 'stopping'].includes(status) ? 'is-active' : 'is-inactive';
}

function formatStatusLine(job) {
  if (!job) return '<span class="status-pill status-idle"><i class="status-pulse-dot"></i> Đang chờ phiên chạy mới</span>';
  const status = (job.status || 'idle').toLowerCase();
  const tool = esc(job.tool_id || 'grok');
  let label = status.toUpperCase();
  let cls = 'idle';
  if (status === 'running' || status === 'pending') {
    label = 'ĐANG CHẠY';
    cls = 'running';
  } else if (status === 'done' || status === 'finished' || status === 'completed') {
    label = 'HOÀN THÀNH';
    cls = 'done';
  } else if (status === 'error' || status === 'failed') {
    label = 'LỖI';
    cls = 'error';
  } else if (status === 'stopping') {
    label = 'ĐANG DỪNG';
    cls = 'warn';
  }
  return `<span class="status-pill status-${cls}"><i class="status-pulse-dot"></i> <strong>${tool}</strong> · ${label}</span>`;
}

function updateJobProgress(job) {
  const panel = document.getElementById('job-progress');
  if (!panel) return;
  panel.hidden = !shouldShowProgress(job);
  panel.classList.toggle('is-active', progressPanelState(job) === 'is-active');
  panel.classList.toggle('is-inactive', progressPanelState(job) === 'is-inactive');
  panel.innerHTML = progressContent(job);
}

/* ── Helpers ── */
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function lineClass(line) {
  const l = line.toLowerCase();
  if (/error|fail|fatal|❌/.test(l)) return 'err';
  if (/success|✅|ok |done/.test(l)) return 'ok';
  if (/warn|⚠/.test(l)) return 'warn';
  if (/info|▶|===|sub2api|sso/.test(l)) return 'info';
  return '';
}

function statusTag(status, ok) {
  if (ok) return `<span class="tag tag-ok">${esc(status || 'ok')}</span>`;
  if (/pending|manual|stopped/i.test(status || '')) {
    return `<span class="tag tag-mid">${esc(status)}</span>`;
  }
  return `<span class="tag tag-fail">${esc(status || 'fail')}</span>`;
}

function toolLabel() {
  const t = (state.tools || []).find((x) => x.id === state.selectedTool);
  return t?.name || 'tool';
}

function hotmailPlanText(pool) {
  const acc = Number(pool?.count ?? 0);
  const slots = Number(pool?.slots ?? acc);
  const maxA = Number(pool?.max_aliases ?? 5);
  const name = toolLabel();
  if (!acc) return 'Pool trống — import Hotmail rồi Start.';
  if (maxA <= 1) {
    return `Sẽ reg <strong>${slots}</strong> acc ${esc(name)} từ ${acc} Hotmail (1 mail = 1 acc).`;
  }
  return `Sẽ reg <strong>${slots}</strong> acc ${esc(name)} từ ${acc} Hotmail (mỗi acc tối đa ${maxA} alias).`;
}

function syncHotmailUi(root) {
  const hotmail = isHotmailMail(state.form.mail);
  const panel = root.querySelector('#hotmail-pool');
  const countWrap = root.querySelector('[data-field-wrap="count"]');
  const plan = root.querySelector('#hotmail-plan');
  if (panel) panel.hidden = !hotmail;
  if (countWrap) countWrap.hidden = hotmail;
  if (plan) {
    plan.hidden = !hotmail;
    plan.innerHTML = hotmailPlanText(state.hotmailPool);
  }
}

function syncCanvaJobUi(root) {
  if (state.selectedTool !== 'canva') return;
  const redeem = String(state.form.job || 'reg') === 'redeem';
  const codesEl = root.querySelector('[data-field-wrap="codes"]');
  if (codesEl) codesEl.hidden = false;
  const thr = root.querySelector('[data-field-wrap="threads"]');
  if (thr) thr.hidden = !redeem;
  const panel = root.querySelector('#hotmail-pool');
  const plan = root.querySelector('#hotmail-plan');
  if (redeem) {
    if (panel) panel.hidden = true;
    if (plan) plan.hidden = true;
    const countWrap = root.querySelector('[data-field-wrap="count"]');
    if (countWrap) countWrap.hidden = true;
  }
}

function hotmailPanelHtml(pool, show) {
  const acc = pool?.accounts || [];
  const count = pool?.count ?? acc.length;
  const maxA = pool?.max_aliases ?? 5;
  const rows = acc
    .slice(0, 40)
    .map(
      (a) => `<li>
        <span class="mono">${esc(a.email)}</span>
        <span class="hm-flags">
          ${a.has_refresh ? '<span class="tag tag-ok">refresh</span>' : '<span class="tag tag-mid">no token</span>'}
          ${a.has_client_id ? '<span class="tag tag-ok">cid</span>' : ''}
        </span>
      </li>`
    )
    .join('');
  return `
    <div class="hotmail-panel" id="hotmail-pool" ${show ? '' : 'hidden'}>
      <div class="card-head" style="margin-bottom:10px">
        <div>
          <div class="card-title">Pool Hotmail</div>
          <div class="card-sub">
            ${count} Hotmail · Start sẽ reg ${pool?.slots ?? count} ${esc(toolLabel())}${maxA > 1 ? ` (×${maxA} alias)` : ''} ·
            <span class="mono">${esc(pool?.path || 'data/hotmails.txt')}</span>
          </div>
        </div>
      </div>
      <div class="field">
        <label>Dán Hotmail vào đây</label>
        <textarea id="hotmail-draft" rows="7" placeholder="email|password|refresh_token|client_id
email:password
email----password----client_id----refresh_token">${esc(state.hotmailDraft || '')}</textarea>
        <div class="hint">1 dòng = 1 acc. Nhận <span class="mono">|</span> <span class="mono">:</span> <span class="mono">----</span> <span class="mono">;</span> tab.${maxA > 1 ? ` Alias +1…+${maxA - 1} dùng ở lần Start sau (còn slot).` : ''}</div>
      </div>
      <div class="btn-row" style="margin:8px 0 12px">
        <button type="button" class="btn btn-ghost" id="btn-hotmail-browse">Browse file</button>
        <button type="button" class="btn btn-primary" id="btn-hotmail-add">Thêm vào pool</button>
        <button type="button" class="btn btn-ghost" id="btn-hotmail-replace">Ghi đè pool</button>
        <input type="file" id="hotmail-file" accept=".txt,.csv,.tsv,.log,.text,text/plain" hidden />
      </div>
      <div class="hotmail-list-wrap">
        <div class="card-sub" style="margin-bottom:6px">Đang trong pool</div>
        ${
          acc.length
            ? `<ul class="hotmail-list">${rows}${
                acc.length > 40 ? `<li class="card-sub">… +${acc.length - 40} acc nữa</li>` : ''
              }</ul>`
            : `<div class="empty" style="padding:16px">Pool trống — dán list hoặc Browse từ Explorer.</div>`
        }
      </div>
    </div>
  `;
}

function bindHotmailPanel(root, toolId) {
  const panel = root.querySelector('#hotmail-pool');
  const draft = root.querySelector('#hotmail-draft');
  const fileEl = root.querySelector('#hotmail-file');
  if (!panel) return;

  const syncDraft = () => {
    if (draft) state.hotmailDraft = draft.value;
  };
  draft?.addEventListener('input', syncDraft);

  const applyPool = (pool) => {
    state.hotmailPool = pool;
    const next = hotmailPanelHtml(pool, true);
    panel.insertAdjacentHTML('afterend', next);
    panel.remove();
    bindHotmailPanel(root, toolId);
    const mailSel = root.querySelector('[data-key="mail"]');
    if (mailSel) state.form.mail = mailSel.value;
    syncHotmailUi(root);
  };

  const send = async (mode) => {
    syncDraft();
    const text = (state.hotmailDraft || '').trim();
    if (!text) {
      toast('Dán Hotmail hoặc Browse file trước', 'err');
      return;
    }
    if (mode === 'replace' && !confirm('Ghi đè toàn bộ data/hotmails.txt?')) return;
    try {
      const res = await importHotmails(toolId, text, mode);
      state.hotmailDraft = '';
      const added = res.added ?? 0;
      const skipped = res.skipped ?? 0;
      const invalid = res.invalid ?? 0;
      toast(
        mode === 'replace'
          ? `Đã ghi đè ${res.count ?? 0} acc`
          : `Thêm ${added} · bỏ trùng ${skipped}${invalid ? ` · lỗi ${invalid}` : ''}`,
        invalid && !added ? 'err' : 'ok'
      );
      applyPool(res);
    } catch (err) {
      toast(err.message || String(err), 'err');
    }
  };

  root.querySelector('#btn-hotmail-browse')?.addEventListener('click', () => fileEl?.click());
  fileEl?.addEventListener('change', async () => {
    const file = fileEl.files && fileEl.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      state.hotmailDraft = text;
      if (draft) draft.value = text;
      toast(`Đã đọc ${file.name} (${text.split(/\r?\n/).filter((l) => l.trim()).length} dòng)`, 'ok');
    } catch (err) {
      toast(err.message || 'Không đọc được file', 'err');
    }
    fileEl.value = '';
  });
  root.querySelector('#btn-hotmail-add')?.addEventListener('click', () => send('append'));
  root.querySelector('#btn-hotmail-replace')?.addEventListener('click', () => send('replace'));
}

/* ── Pages ── */
async function renderRegister(root) {
  if (!state.tools.length) {
    const data = await getTools();
    state.tools = data.tools || [];
  }
  let tool = state.tools.find((t) => t.id === state.selectedTool) || state.tools[0];
  if (!tool) {
    root.innerHTML = `<div class="empty">Không có tool</div>`;
    return;
  }
  state.selectedTool = tool.id;

  // defaults into form
  for (const f of tool.fields || []) {
    if (state.form[f.key] === undefined) state.form[f.key] = f.default;
  }

  let stats = {};
  try {
    if (tool.status === 'ready') stats = await getToolStats(tool.id);
  } catch (_) {}
  if (tool.id === 'grok') {
    // Sub2API lives on the VPS and can take seconds to answer. Never block the
    // local dashboard render on it; the live poll updates these values later.
    fetch('/api/sub2api/pool/stats')
      .then((r) => r.json())
      .then((live) => {
        if (!live?.connected || !root.isConnected) return;
        const total = root.querySelector('#register-sub2-total');
        const detail = root.querySelector('#register-sub2-detail');
        if (total) total.textContent = live.total_accounts ?? '—';
        if (detail) detail.textContent = `${stats.sub2api ?? 0} email unique · ${live.active_accounts ?? '—'} active`;
      })
      .catch(() => {});
  }
  if (['grok', 'heygen', 'capcut', 'zai', 'canva'].includes(tool.id)) {
    try {
      state.hotmailPool = await getHotmails(tool.id);
    } catch (_) {
      state.hotmailPool = state.hotmailPool || { count: 0, accounts: [] };
    }
  }

  try {
    const snap = await getCurrentJob(tool.id, 0);
    if (snap && snap.status) {
      state.job = snap;
    }
  } catch (_) {}

  const job = state.job && state.job.tool_id === tool.id ? state.job : null;
  const running = job && ['running', 'pending', 'stopping'].includes(job.status);

  root.innerHTML = `
    <div class="page">
      <div class="grid-4">
        <div class="stat-card info" title="Số email khác nhau (lấy status lần cuối)">
          <div class="stat-label">Email (unique)</div>
          <div class="stat-value">${stats.unique_emails ?? stats.total ?? '—'}</div>
          <div class="card-sub" style="margin-top:4px">${stats.attempts ?? '—'} lượt thử</div>
        </div>
        <div class="stat-card ok" title="${isSheetOnly(tool.id) ? 'Reg thành công — chỉ lên Google Sheet, không Sub2' : 'Reg xong: Sub2API + reg-only + reg OK nhưng sub2 fail'}">
          <div class="stat-label">Reg OK</div>
          <div class="stat-value">${stats.success ?? '—'}</div>
          <div class="card-sub" style="margin-top:4px">${isSheetOnly(tool.id) ? `lên sheet ${esc(tool.id)}, không Sub2` : `chỉ reg: ${stats.reg_only ?? 0} · sub2 fail: ${stats.sub2_fail ?? 0}`}</div>
        </div>
        ${isSheetOnly(tool.id) ? `
        <div class="stat-card" title="${esc(tool.name)} không import Sub2API">
          <div class="stat-label">Google Sheet</div>
          <div class="stat-value">${stats.success ?? '—'}</div>
          <div class="card-sub" style="margin-top:4px">tab ${esc(tool.id)}</div>
        </div>` : `
        <div class="stat-card" title="Database Sub2API live; email unique đã đối chiếu: ${stats.sub2api ?? 0}">
          <div class="stat-label">Sub2API OK</div>
          <div class="stat-value" id="register-sub2-total">${stats.sub2api_live_total ?? stats.sub2api ?? '—'}</div>
          <div class="card-sub" id="register-sub2-detail" style="margin-top:4px">${stats.sub2api ?? 0} email unique · ${stats.sub2api_live_active ?? '—'} active</div>
        </div>`}
        <div class="stat-card bad" title="error* lần status cuối mỗi email">
          <div class="stat-label">Fail</div>
          <div class="stat-value">${stats.fail ?? '—'}</div>
          <div class="card-sub" style="margin-top:4px">pending: ${stats.pending ?? 0}</div>
        </div>
      </div>
      ${stats.blurb ? `<div class="card-sub" style="margin-top:-6px">${esc(stats.blurb)}</div>` : ''}

      <div class="workspace">
        <div class="card control-card">
          <div class="card-head">
            <div>
              <div class="card-title">Thiết lập phiên chạy</div>
              <div class="card-sub">Chọn nền tảng, nguồn email và thông số vận hành.</div>
            </div>
            <span class="badge badge-ready">Ready</span>
          </div>

          <div class="section-label">01 · Chọn nền tảng</div>
          <div class="platform-nav" role="tablist" style="margin-bottom:14px">
            ${state.tools
              .map((t) => {
                const soon = t.status === 'coming_soon';
                const sel = t.id === tool.id;
                const shortName = (t.name || '').split('/')[0].trim();
                return `<button type="button" class="platform-tab ${sel ? 'is-active' : ''} ${soon ? 'is-soon' : ''}" data-tool="${esc(t.id)}" ${soon ? 'disabled' : ''} role="tab" aria-selected="${sel}">
                  <span class="platform-tab-icon">${brandIconHtml(t)}</span>
                  <span class="platform-tab-name">${esc(shortName)}</span>
                  <span class="platform-tab-status" title="${soon ? 'Soon' : 'Ready'}"></span>
                </button>`;
              })
              .join('')}
          </div>

          <div class="platform-hero">
            <div class="platform-hero-left">
              <div class="platform-hero-icon">${brandIconHtml(tool)}</div>
              <div>
                <div class="platform-hero-title">
                  <span>${esc(tool.name)}</span>
                  <span class="badge badge-ready">Ready</span>
                </div>
                <div class="platform-hero-desc">${esc(tool.description)}</div>
              </div>
            </div>
            <div class="platform-hero-right">
              <span class="tag tag-ok">● Sẵn sàng</span>
            </div>
          </div>

          <div class="section-label">02 · Thông số chạy</div>
          <div class="form-stack form-grid" id="tool-form">
            ${(tool.fields || [])
              .map((f) => {
                if (f.type === 'select') {
                  return `<div class="field" data-field-wrap="${esc(f.key)}">
                    <label>${esc(f.label)}</label>
                    <select data-key="${esc(f.key)}">
                      ${(f.options || [])
                        .map(
                          (o) =>
                            `<option value="${esc(o.value)}" ${String(state.form[f.key]) === String(o.value) ? 'selected' : ''}>${esc(o.label)}${o.hint ? ' — ' + esc(o.hint) : ''}</option>`
                        )
                        .join('')}
                    </select>
                    ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}
                  </div>`;
                }
                if (f.type === 'checkbox') {
                  const checked = state.form[f.key] === true || state.form[f.key] === 'true' || state.form[f.key] === 1;
                  return `<div class="check-row" data-field-wrap="${esc(f.key)}">
                    <input type="checkbox" data-key="${esc(f.key)}" id="f-${esc(f.key)}" ${checked ? 'checked' : ''} />
                    <label for="f-${esc(f.key)}" style="margin:0;color:var(--text-primary)">${esc(f.label)}</label>
                  </div>
                  ${f.hint ? `<div class="hint" style="margin-top:-6px">${esc(f.hint)}</div>` : ''}`;
                }
                if (f.type === 'textarea') {
                  return `<div class="field span-2 redeem-box" data-field-wrap="${esc(f.key)}">
                    <label>${esc(f.label)}</label>
                    <textarea data-key="${esc(f.key)}" rows="5" placeholder="CANVASPIDERMAN
MOI_MA_KHAC">${esc(state.form[f.key] ?? f.default ?? '')}</textarea>
                    ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}
                  </div>`;
                }
                return `<div class="field" data-field-wrap="${esc(f.key)}" ${f.key === 'count' && isHotmailMail(state.form.mail) ? 'hidden' : ''}>
                  <label>${esc(f.label)}</label>
                  <input type="${f.type === 'number' ? 'number' : 'text'}" data-key="${esc(f.key)}"
                    value="${esc(state.form[f.key] ?? f.default)}"
                    ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''} />
                  ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}
                </div>`;
              })
              .join('')}
          </div>

          ${['grok', 'heygen', 'capcut', 'zai', 'canva'].includes(tool.id) ? hotmailPanelHtml(state.hotmailPool, isHotmailMail(state.form.mail)) : ''}
          ${
            ['grok', 'heygen', 'capcut', 'zai', 'canva'].includes(tool.id)
              ? `<div class="hotmail-plan" id="hotmail-plan" ${isHotmailMail(state.form.mail) ? '' : 'hidden'}>${hotmailPlanText(state.hotmailPool)}</div>`
              : ''
          }

          ${!isSheetOnly(tool.id) && stats.next_name ? `<div class="card-sub" style="margin-top:12px">Next Sub2API name: <span class="mono">${esc(stats.next_name)}</span> · Hotmail pool: ${stats.hotmails ?? 0}</div>` : ''}

          <div class="btn-row action-row">
            <button class="btn btn-primary" id="btn-start" ${running || tool.status !== 'ready' ? 'disabled' : ''}>Start</button>
            ${tool.id === 'canva' ? `<button class="btn btn-ghost" id="btn-start-redeem" ${running || tool.status !== 'ready' ? 'disabled' : ''}>Start redeem</button>` : ''}
            <button class="btn btn-danger" id="btn-stop" ${!running ? 'disabled' : ''}>Stop</button>
            <button class="btn btn-ghost" id="btn-refresh-stats">Stats</button>
          </div>
        </div>

        <div class="card console-card">
          <div class="log-head">
            <div style="display:flex;align-items:center;gap:10px;min-width:0">
              <span class="term-dots" aria-hidden="true"><i></i><i></i><i></i></span>
              <div>
              <div class="card-title">Live activity</div>
              <div class="card-sub" id="job-status-line">${formatStatusLine(job)}</div>
              </div>
            </div>
            <div class="log-actions">
              <label class="check-row" style="padding:6px 10px;margin:0">
                <input type="checkbox" id="auto-scroll" ${state.autoScroll ? 'checked' : ''} />
                <span style="font-size:12px">Auto-scroll</span>
              </label>
              <button class="btn btn-ghost" id="btn-copy-log" type="button">Copy log</button>
              <button class="btn btn-danger" id="btn-clear-log" type="button" title="Xoá log của phiên hiện tại">Xoá log</button>
            </div>
          </div>
          <div class="job-progress ${progressPanelState(job)}" id="job-progress" ${shouldShowProgress(job) ? '' : 'hidden'}>${progressContent(job)}</div>
          <div class="log-console" id="log-box"></div>
        </div>
      </div>
    </div>
  `;

  const liveBox = document.getElementById('log-box');
  bindLogBox(liveBox);
  paintLogs(liveBox, job?.logs || []);

  root.querySelectorAll('[data-tool]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      state.selectedTool = btn.dataset.tool;
      state.form = {};
      await renderRegister(root);
    });
  });

  root.querySelectorAll('[data-key]').forEach((el) => {
    const key = el.dataset.key;
    const sync = () => {
      if (el.type === 'checkbox') state.form[key] = el.checked;
      else if (el.type === 'number') state.form[key] = el.value === '' ? 0 : Number(el.value);
      else state.form[key] = el.value;
      if (key === 'mail') syncHotmailUi(root);
      if (key === 'job') syncCanvaJobUi(root);
    };
    el.addEventListener('change', sync);
    el.addEventListener('input', sync);
  });
  bindHotmailPanel(root, tool.id);
  syncHotmailUi(root);
  syncCanvaJobUi(root);

  document.getElementById('auto-scroll')?.addEventListener('change', (e) => {
    setAutoScroll(e.target.checked, { scrollBox: document.getElementById('log-box') });
  });
  document.getElementById('btn-copy-log')?.addEventListener('click', async () => {
    try {
      await copyLogBox(document.getElementById('log-box'));
    } catch {
      toast('Copy thất bại', 'err');
    }
  });
  bindClearLogButton();

  document.getElementById('btn-start-redeem')?.addEventListener('click', () => {
    const sel = root.querySelector('[data-key="job"]');
    if (sel) sel.value = 'redeem';
    state.form.job = 'redeem';
    document.getElementById('btn-start')?.click();
  });

  document.getElementById('btn-start')?.addEventListener('click', async () => {
    try {
      // collect form
      root.querySelectorAll('[data-key]').forEach((el) => {
        const key = el.dataset.key;
        if (el.type === 'checkbox') state.form[key] = el.checked;
        else if (el.type === 'number') state.form[key] = Number(el.value);
        else state.form[key] = el.value;
      });
      if (state.form.job === 'redeem') {
        const raw = String(state.form.codes || '').trim();
        if (!raw) {
          toast('Dán mã redeem vào ô Mã redeem (mỗi dòng 1 mã)', 'err');
          return;
        }
      } else if (isHotmailMail(state.form.mail)) {
        try {
          state.hotmailPool = await getHotmails(state.selectedTool);
        } catch (_) {}
        const n = Number(state.hotmailPool?.slots || state.hotmailPool?.count || 0);
        if (!n) {
          toast('Pool Hotmail trống / hết slot — import acc rồi Start', 'err');
          return;
        }
        state.form.count = n;
        syncHotmailUi(root);
      }
      const res = await startJob(state.selectedTool, { ...state.form });
      state.job = res.job;
      state.logSeq = 0;
      toast('Đã Start job', 'ok');
      updateRunPill(state.job);
      await renderRegister(root);
      revealLiveLog();
    } catch (err) {
      toast(err.message || String(err), 'err');
    }
  });

  document.getElementById('btn-stop')?.addEventListener('click', async () => {
    try {
      const res = await stopJob(state.job?.id || null);
      toast(res.message || 'Đang dừng…', 'ok');
      revealLiveLog();
    } catch (err) {
      toast(err.message || String(err), 'err');
    }
  });

  document.getElementById('btn-refresh-stats')?.addEventListener('click', async () => {
    await renderRegister(root);
    toast('Đã refresh stats', 'ok');
    revealLiveLog();
  });
}

function selectionIn(el) {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
  const node = sel.anchorNode;
  return !!(node && el.contains(node));
}

function parseLogLine(text) {
  const raw = String(text ?? '');
  let body = raw.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').trimEnd();
  let time = '';
  let level = '';
  let task = '';
  let source = '';

  const outerTime = body.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*/);
  if (outerTime) {
    time = outerTime[1];
    body = body.slice(outerTime[0].length);
  }

  // Technical logger already includes its own clock. Keep only the web clock
  // so lines do not render as "[14:14:44] 14:14:44 | INFO | ...".
  const technical = body.match(/^(\d{2}:\d{2}:\d{2})\s*\|\s*([A-Z]+)\s*\|\s*/);
  if (technical) {
    if (!time) time = technical[1];
    level = technical[2];
    body = body.slice(technical[0].length);
  }

  const taskPrefix = body.match(/^(\d+)\]\s*/);
  if (taskPrefix) {
    task = taskPrefix[1];
    body = body.slice(taskPrefix[0].length);
  }

  const tagged = body.match(/^\[([^\]]+)\]\s*/);
  if (tagged) {
    source = tagged[1];
    body = body.slice(tagged[0].length);
  } else if (/^===/.test(body)) {
    source = 'JOB';
  } else if (/^CMD:/.test(body)) {
    source = 'CMD';
  }

  const sourceKey = source.toLowerCase();
  if (sourceKey === 'grok-api') source = task ? `ACC ${task}` : 'GROK';
  else if (/^sub2api/.test(sourceKey)) source = 'SUB2API';
  else if (sourceKey === 'turnstile') source = 'CAPTCHA';
  else if (sourceKey === 'delivery') source = 'SYNC';
  else if (sourceKey === 'solver') source = 'SOLVER';
  else if (source) source = source.toUpperCase();
  else if (task) source = `ACC ${task}`;
  else if (level) source = level;
  else source = 'SYSTEM';

  return { raw, time: time || '--:--:--', source, message: body.trim(), level };
}

function makeLogLine(text) {
  const parsed = parseLogLine(text);
  const div = document.createElement('div');
  div.className = `line ${lineClass(parsed.raw)}${parsed.message ? '' : ' blank'}`;
  div.dataset.raw = parsed.raw;
  if (!parsed.message) return div;

  const time = document.createElement('span');
  time.className = 'log-time';
  time.textContent = parsed.time;
  const source = document.createElement('span');
  source.className = 'log-source';
  source.textContent = parsed.source;
  const message = document.createElement('span');
  message.className = 'log-message';
  message.textContent = parsed.message;
  div.append(time, source, message);
  return div;
}

function logBoxText(box) {
  if (!box) return '';
  return [...box.querySelectorAll('.line')].map((el) => el.dataset.raw ?? el.textContent).join('\n');
}

async function copyToClipboard(text) {
  if (text === undefined || text === null) return;
  const str = String(text);
  if (navigator.clipboard && (window.isSecureContext || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
    try {
      await navigator.clipboard.writeText(str);
      return;
    } catch (_) {
      // fallback below
    }
  }
  const ta = document.createElement('textarea');
  ta.value = str;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  ta.style.top = '0';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  ta.setSelectionRange(0, str.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (_) {
    ok = false;
  }
  document.body.removeChild(ta);
  if (!ok) {
    throw new Error('Copy command failed');
  }
}

async function copyLogBox(box) {
  const text = logBoxText(box);
  if (!text) {
    toast('Không có log để copy', 'warn');
    return;
  }
  await copyToClipboard(text);
  toast('Đã copy toàn bộ log vào clipboard!', 'ok');
}

function isLogAtBottom(box, slop = 48) {
  if (!box) return true;
  return box.scrollHeight - box.scrollTop - box.clientHeight < slop;
}

function setAutoScroll(on, { scrollBox } = {}) {
  state.autoScroll = !!on;
  const cb = document.getElementById('auto-scroll');
  if (cb && cb.checked !== state.autoScroll) cb.checked = state.autoScroll;
  if (state.autoScroll && scrollBox) scrollBox.scrollTop = scrollBox.scrollHeight;
}

function paintLogs(box, lines) {
  if (!box) return;
  const next = Array.isArray(lines) ? lines : [];
  const selecting = selectionIn(box);
  // Keep streaming while the user is selecting/copying. Only skip a full
  // rewrite — that would wipe the highlight and look like the log "stopped".
  const pin =
    !selecting &&
    !box.dataset.holdScroll &&
    (state.autoScroll || isLogAtBottom(box));
  const kids = box.children;
  const prevN = kids.length;
  const canAppend =
    prevN > 0 &&
    next.length >= prevN &&
    kids[0].dataset.raw === String(next[0] ?? '') &&
    kids[prevN - 1].dataset.raw === String(next[prevN - 1] ?? '');

  if (canAppend) {
    if (next.length === prevN) return;
    const frag = document.createDocumentFragment();
    for (let i = prevN; i < next.length; i++) frag.appendChild(makeLogLine(next[i]));
    box.appendChild(frag);
  } else {
    if (selecting) return;
    const frag = document.createDocumentFragment();
    next.forEach((l) => frag.appendChild(makeLogLine(l)));
    box.replaceChildren(frag);
  }
  if (pin) box.scrollTop = box.scrollHeight;
}

function bindLogBox(box) {
  if (!box || box.dataset.copyBound) return;
  box.dataset.copyBound = '1';
  box.setAttribute('tabindex', '0');
  box.setAttribute('role', 'log');
  box.setAttribute('aria-label', 'Live log — bôi chọn để copy');
  box.addEventListener('keydown', (e) => {
    const key = String(e.key).toLowerCase();
    if (!(e.ctrlKey || e.metaKey)) return;
    if (key === 'a') {
      e.preventDefault();
      e.stopPropagation();
      const range = document.createRange();
      range.selectNodeContents(box);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      return;
    }
    if (key === 'c') {
      // Copy only — never treat Ctrl+C in the log as Stop.
      e.stopPropagation();
      const sel = window.getSelection();
      const picked =
        sel && !sel.isCollapsed && box.contains(sel.anchorNode)
          ? sel.toString()
          : '';
      if (picked) return;
      e.preventDefault();
      copyLogBox(box).catch(() => toast('Copy thất bại', 'err'));
    }
  });
  box.addEventListener('copy', (e) => {
    e.stopPropagation();
  });
  box.addEventListener('pointerdown', (e) => {
    box.dataset.holdScroll = '1';
    try {
      box.setPointerCapture(e.pointerId);
    } catch (_) {
      /* ignore */
    }
  });
  const release = () => {
    delete box.dataset.holdScroll;
    setAutoScroll(isLogAtBottom(box));
  };
  box.addEventListener('pointerup', release);
  box.addEventListener('pointercancel', release);
  box.addEventListener(
    'wheel',
    () => {
      requestAnimationFrame(() => setAutoScroll(isLogAtBottom(box)));
    },
    { passive: true },
  );
  box.addEventListener(
    'scroll',
    () => {
      setAutoScroll(isLogAtBottom(box));
    },
    { passive: true },
  );
}
function bindClearLogButton() {
  document.getElementById('btn-clear-log')?.addEventListener('click', async () => {
    const toolId = state.selectedTool || 'grok';
    try {
      const result = await clearToolLogs(toolId);
      state.job = result.job || { status: 'idle', tool_id: toolId, logs: [], running: false };
      paintLogs(document.getElementById('log-box'), []);
      const statusLine = document.getElementById('job-status-line');
      if (statusLine) {
        statusLine.innerHTML = formatStatusLine({ tool_id: toolId, status: 'idle' });
      }
      toast(`Đã xoá ${result.removed || 0} dòng log của ${toolId}`, 'ok');
    } catch (e) {
      toast(e.message || 'Xoá log thất bại', 'err');
    }
  });
}

async function renderResults(root) {
  const toolId = state.selectedTool || 'grok';
  let rows = [];
  let stats = {};
  try {
    const r = await getToolResults(toolId, 150);
    rows = r.results || [];
    stats = await getToolStats(toolId);
    // Remote Sub2API totals are supplemental; local results must render first.
  } catch (e) {
    /* ignore fetch error */
  }

  root.innerHTML = `
    <div class="page">
      <div class="page-banner">
        <div><div class="hero-kicker"><i></i> Data vault</div><h2>Kho dữ liệu tài khoản</h2><p>Dữ liệu tài khoản và trạng thái xử lý mới nhất của ${esc(toolId)}.</p></div>
        <span class="page-banner-mark">${rows.length} RECORDS</span>
      </div>
      <div class="grid-4">
        <div class="stat-card info"><div class="stat-label">Email unique</div><div class="stat-value">${stats.unique_emails ?? stats.total ?? 0}</div>
          <div class="card-sub" style="margin-top:4px">${stats.attempts ?? 0} lượt thử</div></div>
        <div class="stat-card ok"><div class="stat-label">Reg OK</div><div class="stat-value">${stats.success ?? 0}</div>
          <div class="card-sub" style="margin-top:4px">${isSheetOnly(toolId) ? 'không Sub2 — chỉ sheet' : `reg-only ${stats.reg_only ?? 0} · sub2 fail ${stats.sub2_fail ?? 0}`}</div></div>
        <div class="stat-card"><div class="stat-label">${isSheetOnly(toolId) ? `Sheet ${esc(toolId)}` : 'Sub2API OK'}</div><div class="stat-value">${isSheetOnly(toolId) ? (stats.success ?? 0) : (stats.sub2api_live_total ?? stats.sub2api ?? 0)}</div>${!isSheetOnly(toolId) ? `<div class="card-sub" style="margin-top:4px">${stats.sub2api ?? 0} email unique · ${stats.sub2api_live_active ?? '—'} active</div>` : ''}</div>
        <div class="stat-card bad"><div class="stat-label">Fail</div><div class="stat-value">${stats.fail ?? 0}</div>
          <div class="card-sub" style="margin-top:4px">pending ${stats.pending ?? 0}</div></div>
      </div>
      ${stats.blurb ? `<div class="card-sub">${esc(stats.blurb)}</div>` : ''}
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">Accounts · ${esc(toolId)}</div>
            <div class="card-sub">data/accounts.txt — mỗi dòng = 1 lượt thử (email có thể lặp)</div>
          </div>
          <button class="btn btn-ghost" id="btn-copy-ok">Copy Reg OK</button>
        </div>
        <div class="table-wrap">
          <table class="results">
            <thead>
              <tr><th>Email</th><th>Password</th><th>Status</th></tr>
            </thead>
            <tbody>
              ${
                rows.length
                  ? rows
                      .map(
                        (r) => `<tr>
                  <td class="mono">${esc(r.email)}</td>
                  <td class="mono">${esc(r.password)}</td>
                  <td>${statusTag(r.status, r.ok)}</td>
                </tr>`
                      )
                      .join('')
                  : `<tr><td colspan="3" class="empty">Chưa có kết quả</td></tr>`
              }
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  document.getElementById('btn-copy-ok')?.addEventListener('click', async () => {
    const text = rows
      .filter((r) => r.ok)
      .map((r) => `${r.email}|${r.password}|${r.status}`)
      .join('\n');
    try {
      await navigator.clipboard.writeText(text || '');
      toast(`Đã copy ${rows.filter((r) => r.ok).length} dòng`, 'ok');
    } catch {
      toast('Copy thất bại', 'err');
    }
  });
}

async function renderLogs(root) {
  const job = state.job;
  root.innerHTML = `
    <div class="page">
      <div class="page-banner">
        <div><div class="hero-kicker"><i></i> Activity stream</div><h2>Dòng sự kiện trực tiếp</h2><p>Theo dõi chi tiết tiến trình, cảnh báo và lỗi trong thời gian thực.</p></div>
        <span class="page-banner-mark">LIVE STREAM</span>
      </div>
      <div class="card">
        <div class="log-head">
          <div>
            <div class="card-title">Full log stream</div>
            <div class="card-sub" id="job-status-line">${job ? `${esc(job.tool_id)} · ${esc(job.status)} · id ${esc(job.id || '')}` : 'Idle'}</div>
          </div>
          <div class="btn-row" style="margin:0">
            <label class="check-row" style="padding:6px 10px;margin:0">
              <input type="checkbox" id="auto-scroll" ${state.autoScroll ? 'checked' : ''} />
              <span style="font-size:12px">Auto-scroll</span>
            </label>
            <button class="btn btn-danger" id="btn-stop-log">⏹ Stop</button>
            <button class="btn btn-ghost" id="btn-copy-log" type="button">Copy log</button>
            <button class="btn btn-danger" id="btn-clear-log" type="button" title="Xoá log của phiên hiện tại">Xoá log</button>
          </div>
        </div>
        <div class="job-progress ${progressPanelState(job)}" id="job-progress" ${shouldShowProgress(job) ? '' : 'hidden'}>${progressContent(job)}</div>
        <div class="log-console" id="log-box" style="height:calc(100vh - 220px)"></div>
      </div>
    </div>
  `;
  const fullBox = document.getElementById('log-box');
  bindLogBox(fullBox);
  paintLogs(fullBox, job?.logs || []);
  document.getElementById('auto-scroll')?.addEventListener('change', (e) => {
    setAutoScroll(e.target.checked, { scrollBox: document.getElementById('log-box') });
  });
  document.getElementById('btn-copy-log')?.addEventListener('click', async () => {
    try {
      await copyLogBox(document.getElementById('log-box'));
    } catch {
      toast('Copy thất bại', 'err');
    }
  });
  bindClearLogButton();
  document.getElementById('btn-stop-log')?.addEventListener('click', async () => {
    try {
      await stopJob();
      toast('Stop sent', 'ok');
    } catch (e) {
      toast(e.message, 'err');
    }
  });
}

async function renderSettings(root) {
  let sum = {};
  try {
    sum = await getConfigSummary();
  } catch (e) {
    root.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  const sub = sum.sub2api || {};
  const gs = sum.google_sheets || {};
  root.innerHTML = `
    <div class="page">
      <div class="page-banner">
        <div><div class="hero-kicker"><i></i> System settings</div><h2>Thiết lập hệ thống</h2><p>Tổng quan các kết nối và chính sách đang được áp dụng.</p></div>
        <span class="page-banner-mark">EDITABLE</span>
      </div>
      <form id="settings-form" class="settings-form">
        <div class="grid-2">
          <section class="card settings-card">
            <div class="card-head">
              <div><div class="card-title">Sub2API</div><div class="card-sub">Kết nối và thông tin xác thực.</div></div>
              <label class="switch"><input id="cfg-sub-enabled" type="checkbox" ${sub.enabled ? 'checked' : ''}><span></span></label>
            </div>
            <div class="form-grid">
              <div class="field"><label>Chế độ</label><select id="cfg-sub-mode">
                ${['auto', 'api', 'sso', 'browser'].map((v) => `<option value="${v}" ${sub.mode === v ? 'selected' : ''}>${v}</option>`).join('')}
              </select></div>
              <div class="field"><label>URL</label><input id="cfg-sub-url" type="text" value="${esc(sub.url)}" placeholder="http://localhost:8080"></div>
              <div class="field"><label>Group</label><input id="cfg-sub-group" type="text" value="${esc(sub.group)}"></div>
              <div class="field"><label>Name prefix</label><input id="cfg-sub-prefix" type="text" value="${esc(sub.name_prefix)}"></div>
              <div class="field span-2"><label>User</label><input id="cfg-sub-user" type="text" value="${esc(sub.user)}" autocomplete="username"></div>
              <div class="field"><label>Password</label><input id="cfg-sub-pass" type="password" value="" autocomplete="new-password" placeholder="${sub.password_set || sub.has_password ? '••••••••  (Đã cấu hình · nhập mới để đổi)' : 'Chưa cấu hình'}"></div>
              <div class="field"><label>API token</label><input id="cfg-sub-token" type="password" value="" autocomplete="off" placeholder="${sub.api_token_set || sub.has_token ? '••••••••  (Đã cấu hình · nhập mới để đổi)' : 'Chưa cấu hình'}"></div>
            </div>
          </section>

          <section class="card settings-card">
            <div class="card-head">
              <div><div class="card-title">Google Sheets</div><div class="card-sub">Đích xuất dữ liệu sau khi chạy.</div></div>
              <label class="switch"><input id="cfg-sheet-enabled" type="checkbox" ${gs.enabled ? 'checked' : ''}><span></span></label>
            </div>
            <div class="form-stack">
              <div class="field"><label>Spreadsheet ID</label><input id="cfg-sheet-id" type="text" value="${esc(gs.spreadsheet_id)}" placeholder="1Abc..."></div>
              <div class="field"><label>Webapp URL</label><input id="cfg-sheet-webapp" type="text" value="${esc(gs.webapp_url || '')}" autocomplete="off" placeholder="https://script.google.com/macros/s/.../exec"></div>
              <div class="settings-note">Thông tin kết nối Google Sheets tự động đồng bộ thời gian thực mỗi tài khoản reg xong.</div>
            </div>
          </section>
        </div>

        <section class="card settings-card">
          <div class="card-head"><div><div class="card-title">Phiên & bảo mật</div><div class="card-sub">Các hành vi mặc định cho phiên đăng ký mới.</div></div></div>
          <div class="settings-options">
            <label class="check-row"><input id="cfg-force-guest" type="checkbox" ${sum.force_guest_on_start ? 'checked' : ''}><span>Force guest khi bắt đầu</span></label>
            <label class="check-row"><input id="cfg-open-grok" type="checkbox" ${sum.open_grok_after_success ? 'checked' : ''}><span>Mở Grok sau khi thành công</span></label>
            <div class="field"><label>Fixed password</label><input id="cfg-fixed-pass" type="password" value="" autocomplete="new-password" placeholder="${sum.fixed_password_set ? '••••••••  (Đã cấu hình · nhập mới để đổi)' : 'Chưa cấu hình'}"></div>
          </div>
        </section>

        <div class="settings-actions">
          <div><strong>config.json</strong><span>Thay đổi áp dụng cho các job bắt đầu sau khi lưu.</span></div>
          <div class="btn-row"><button type="button" class="btn btn-ghost" id="btn-settings-reset">Hoàn tác</button><button type="submit" class="btn btn-primary" id="btn-settings-save">Lưu thiết lập</button></div>
        </div>
      </form>
    </div>
  `;

  const form = root.querySelector('#settings-form');
  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const save = root.querySelector('#btn-settings-save');
    if (save) { save.disabled = true; save.textContent = 'Đang lưu…'; }
    const val = (id) => root.querySelector(id)?.value || '';
    const checked = (id) => !!root.querySelector(id)?.checked;
    try {
      const result = await updateConfig({
        sub2api: {
          enabled: checked('#cfg-sub-enabled'), mode: val('#cfg-sub-mode'), url: val('#cfg-sub-url'),
          group: val('#cfg-sub-group'), name_prefix: val('#cfg-sub-prefix'), user: val('#cfg-sub-user'),
          password: val('#cfg-sub-pass') || null, api_token: val('#cfg-sub-token') || null,
        },
        google_sheets: {
          enabled: checked('#cfg-sheet-enabled'), spreadsheet_id: val('#cfg-sheet-id'),
          webapp_url: val('#cfg-sheet-webapp') || null,
        },
        force_guest_on_start: checked('#cfg-force-guest'),
        open_grok_after_success: checked('#cfg-open-grok'),
        fixed_password: val('#cfg-fixed-pass') || null,
      });
      toast(result.message || 'Đã lưu thiết lập', 'ok');
      await renderSettings(root);
    } catch (error) {
      toast(error.message || 'Không lưu được thiết lập', 'err');
      if (save) { save.disabled = false; save.textContent = 'Lưu thiết lập'; }
    }
  });
  root.querySelector('#btn-settings-reset')?.addEventListener('click', () => renderSettings(root));
}

async function renderTools(root) {
  if (!state.tools.length) {
    const data = await getTools();
    state.tools = data.tools || [];
  }
  root.innerHTML = `
    <div class="page">
      <div class="page-banner">
        <div><div class="hero-kicker"><i></i> Connected apps</div><h2>Hệ sinh thái Nexus</h2><p>Quản lý các nền tảng đã kết nối với Nexus Ops.</p></div>
        <span class="page-banner-mark">${state.tools.length} PLUGINS</span>
      </div>
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">Danh mục nền tảng</div>
            <div class="card-sub">Các công cụ kết nối với Nexus Core. Placeholder = sắp triển khai.</div>
          </div>
        </div>
        <div class="tool-grid">
          ${state.tools
            .map((t) => {
              const soon = t.status === 'coming_soon';
              return `<div class="tool-tile ${soon ? 'is-soon' : ''}" style="cursor:default">
                ${brandIconHtml(t)}
                <strong>${esc(t.name)}</strong>
                <p>${esc(t.description)}</p>
                <div style="margin-top:10px;display:flex;gap:8px;align-items:center">
                  <span class="badge ${soon ? 'badge-soon' : 'badge-ready'}">${soon ? 'Coming soon' : 'Ready'}</span>
                  <span class="mono" style="font-size:11px;color:var(--text-muted)">${esc(t.id)}</span>
                </div>
              </div>`;
            })
            .join('')}
        </div>
      </div>
    </div>
  `;
}

async function renderKeys(root) {
  let recent = [];
  let pool = {
    connected: false,
    total_accounts: 0,
    active_accounts: 0,
    total_max_tokens: 0,
    remaining_tokens: 0,
    remaining_percent: 0,
    token_per_acc: 0,
    safe_keys: { '10k': 0, '50k': 0, '65k': 0, '100k': 0, '500k': 0, '1m': 0 },
  };

  // Render controls immediately. Slow VPS data is filled by
  // updateKeysRealtime() after the page is interactive.

  const packages = [
    { label: '500,000 Token (500K)', tokens: 500000 },
    { label: '1,000,000 Token (1M)', tokens: 1000000 },
    { label: '3,000,000 Token (3M)', tokens: 3000000 },
    { label: '5,000,000 Token (5M)', tokens: 5000000 },
    { label: '10,000,000 Token (10M)', tokens: 10000000 },
    { label: '20,000,000 Token (20M)', tokens: 20000000 },
    { label: '30,000,000 Token (30M)', tokens: 30000000 },
    { label: '40,000,000 Token (40M)', tokens: 40000000 },
    { label: '50,000,000 Token (50M)', tokens: 50000000 },
    { label: '100,000,000 Token (100M)', tokens: 100000000 },
    { label: '150,000,000 Token (150M)', tokens: 150000000 },
    { label: '✏️ Tùy ý (Nhập tay)', tokens: 0 },
  ];

  let selectedTokens = 500000;
  let pendingGenerateRequest = null;

  const chooseCapacityAmount = (detail, keyCount) => new Promise((resolve) => {
    const available = Math.max(0, Number(detail?.available_tokens || 0));
    const minimum = Math.max(1000, Number(detail?.minimum_tokens || 1000));
    const suggestedTotal = Math.min(available, Math.max(0, Number(detail?.suggested_tokens || 0)));
    const suggestedPerKey = Math.floor(suggestedTotal / keyCount / 1000) * 1000;
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(2,6,23,.78);display:grid;place-items:center;padding:20px';
    overlay.innerHTML = `
      <div class="card" style="width:min(520px,100%);box-shadow:0 24px 80px rgba(0,0,0,.5)">
        <div class="card-title" style="color:#f59e0b">⚠️ Kho không đủ cho toàn bộ yêu cầu</div>
        <div class="card-sub" style="margin-top:10px;line-height:1.7">
          Đang yêu cầu <b>${Number(detail?.requested_tokens || 0).toLocaleString()}</b> token.<br>
          Kho chính xác còn <b style="color:#38bdf8">${available.toLocaleString()}</b> token.<br>
          Hệ thống sẽ không tạo hoặc giao thiếu một phần.
        </div>
        <div class="field" style="margin-top:16px">
          <label>Token cho mỗi key (${keyCount} key)</label>
          <input id="capacity-custom-tokens" type="number" min="${minimum}" max="${Math.floor(available / keyCount)}" step="1000" value="${suggestedPerKey || ''}" />
          <div class="hint">Tổng phải ≤ ${available.toLocaleString()} token; tối thiểu ${minimum.toLocaleString()} token/key.</div>
        </div>
        <div class="btn-row" style="justify-content:flex-end;margin-top:18px">
          <button class="btn btn-ghost" data-action="cancel">Hủy</button>
          ${suggestedPerKey >= minimum ? `<button class="btn btn-secondary" data-action="suggested">Dùng mức làm tròn ${suggestedPerKey.toLocaleString()}/key</button>` : ''}
          <button class="btn btn-primary" data-action="custom">Dùng số đã nhập</button>
        </div>
      </div>`;
    const close = (value) => { overlay.remove(); resolve(value); };
    overlay.addEventListener('click', (event) => {
      const action = event.target?.dataset?.action;
      if (action === 'cancel') close(null);
      if (action === 'suggested') close(suggestedPerKey);
      if (action === 'custom') {
        const value = Math.floor(Number(overlay.querySelector('#capacity-custom-tokens')?.value || 0));
        if (value < minimum || value * keyCount > available) {
          toast(`Số token phải từ ${minimum.toLocaleString()} và tổng không vượt ${available.toLocaleString()}.`, 'err');
          return;
        }
        close(value);
      }
    });
    document.body.appendChild(overlay);
    overlay.querySelector('#capacity-custom-tokens')?.focus();
  });

  const pct = pool.remaining_percent ?? 100;
  const pctColor = pct > 60 ? '#10b981' : (pct > 25 ? '#f59e0b' : '#ef4444');

  root.innerHTML = `
    <div class="page">
      <!-- 4 Top Stat Cards for Real-Time Token Pool -->
      <div class="grid-4" style="margin-bottom: 20px;">
        <div class="stat-card info">
          <div class="stat-label">🔋 Tổng Token Khả Dụng</div>
          <div class="stat-value" id="stat-pool-remaining" style="font-size: 26px; color: #10b981;">${(pool.remaining_tokens || 0).toLocaleString()}</div>
          <div class="card-sub" id="stat-pool-sub" style="margin-top: 4px; color: #38bdf8; font-weight: 600;">
            ≈ ${((pool.remaining_tokens || 0) / 1000000).toFixed(1)} Triệu Tokens (${pool.token_per_acc ? (pool.token_per_acc / 1000).toFixed(0) + 'k' : '50k'}/acc thương mại)
          </div>
        </div>

        <div class="stat-card ok">
          <div class="stat-label">🌐 Kho Acc Kết Nối</div>
          <div class="stat-value" id="stat-pool-accs" style="font-size: 26px;">${pool.active_accounts || pool.total_accounts || 0}</div>
          <div class="card-sub" id="stat-pool-accs-sub" style="margin-top: 4px;">
            ${pool.connected ? '<span style="color:#10b981;">● Sub2API Live</span>' : '<span style="color:#f59e0b;">● Kho Local</span>'} · ${pool.total_accounts || 0} Acc Active
          </div>
        </div>

        <div class="stat-card">
          <div class="stat-label">📦 Sức Chứa Key (1M)</div>
          <div class="stat-value" id="stat-pool-capacity" style="font-size: 26px; color: #a855f7;">${Math.floor((pool.remaining_tokens || 0) / 1000000).toLocaleString()}</div>
          <div class="card-sub" id="stat-pool-capacity-sub" style="margin-top: 4px;">
            ${Math.floor((pool.remaining_tokens || 0) / 2000000).toLocaleString()} Key 2M · ${Math.floor((pool.remaining_tokens || 0) / 5000000).toLocaleString()} Key 5M · ${Math.floor((pool.remaining_tokens || 0) / 10000000).toLocaleString()} Key 10M
          </div>
        </div>

        <div class="stat-card">
          <div class="stat-label">⚡ Tỉ Lệ Khả Dụng</div>
          <div class="stat-value" id="stat-pool-pct" style="font-size: 26px; color: ${pctColor};">${pct}%</div>
          <div class="card-sub" id="stat-pool-used-sub" style="margin-top: 4px; font-weight: 600; color: ${(pool.used_tokens || 0) > 0 ? '#f59e0b' : 'var(--muted)'};">
            ${(pool.used_tokens || 0) > 0 ? `⚡ Đã dùng ${(pool.used_tokens || 0).toLocaleString()} tokens` : '✨ Chưa tiêu thụ'}
          </div>
          <div style="width: 100%; height: 6px; background: rgba(255,255,255,0.08); border-radius: 4px; overflow: hidden; margin-top: 6px;">
            <div id="stat-pool-gauge" style="height: 100%; width: ${pct}%; background: linear-gradient(90deg, #10b981 0%, #3b82f6 100%); border-radius: 4px; transition: width 0.3s ease;"></div>
          </div>
        </div>
      </div>

      <div class="workspace">
        <div class="card control-card">
          <div class="card-head">
            <div>
              <div class="card-title">Tạo Key Bán Lẻ Mới</div>
              <div class="card-sub">Chọn gói token và số lượng key cần tạo.</div>
            </div>
            <span class="badge badge-ready" id="badge-total-accs">Grok (${pool.total_accounts}+ Acc)</span>
          </div>

          <div class="section-label">01 · Chọn gói Token</div>
          <div class="token-presets" id="token-preset-group">
            ${packages.map((p, idx) => `<button type="button" class="token-chip ${idx === 0 ? 'is-selected' : ''}" data-tokens="${p.tokens}">${p.label}</button>`).join('')}
          </div>

          <div class="form-stack form-grid">
            <div class="field">
              <label>Số Token của mỗi Key</label>
              <input type="number" id="key-tokens" value="${selectedTokens}" min="1000" step="10000" />
              <div class="hint" id="token-calc-hint">Quy đổi: 1,000,000 token = $2.00 USD (50,000đ)</div>
            </div>

            <div class="field">
              <label>Số lượng Key muốn tạo</label>
              <input type="number" id="key-count" value="1" min="1" max="100" />
              <div class="hint">Tạo hàng loạt từ 1 đến 100 key trong 1 giây</div>
            </div>

            <div class="field">
              <label>Tên / Tiền tố Key (Tùy chọn)</label>
              <input type="text" id="key-prefix" value="Grok" placeholder="VD: KhachA, Grok, Shop..." />
              <div class="hint">Hệ thống sẽ tự đặt tên: Grok_10k_01, Grok_10k_02...</div>
            </div>

            <div class="field">
              <label>Hạn sử dụng</label>
              <input type="text" value="♾️ Không bao giờ hết hạn (Dùng hết Token thì thôi)" disabled style="opacity:0.8;background:var(--subtle)" />
              <div class="hint">Khách dùng khi nào hết sạch token thì tự khóa</div>
            </div>
          </div>

          <div class="btn-row action-row">
            <button class="btn btn-primary" id="btn-generate-keys" type="button" style="padding:10px 20px;font-size:13px">⚡ Tạo Key Ngay</button>
          </div>

          <div id="key-result-container" hidden></div>
        </div>

        <div class="card">
          <div class="card-head">
            <div>
              <div class="card-title">Mẫu gửi cho khách hàng</div>
              <div class="card-sub">Tự động gắn Key mới nhất: <strong style="color:var(--primary);" id="lbl-template-name">${esc(recent[0]?.name || 'Grok Key')}</strong></div>
            </div>
            <div class="btn-row" style="margin:0">
              <button class="btn btn-primary" id="btn-copy-full-customer-template" type="button" style="font-size:11px; padding: 6px 12px;">📋 Copy Mẫu Này</button>
              <button class="btn btn-secondary" id="btn-copy-top-oneclick" type="button" style="font-size:11px; padding: 6px 12px;">⚡ Copy Lệnh 1-Click</button>
            </div>
          </div>
          <div class="customer-template-box">
            <div class="template-title">📌 THÔNG TIN KẾT NỐI GROK API</div>
            <div class="info-row">
              <span class="lbl">🌐 Base URL:</span>
              <span class="val">https://grokapi.vorte.me/v1</span>
            </div>
            <div class="info-row">
              <span class="lbl">🔑 API Key:</span>
              <span class="val" id="lbl-template-key" style="color:var(--primary); font-weight:700;">${esc(recent[0]?.key || 'sk-...')}</span>
            </div>
            <div class="info-row">
              <span class="lbl">📊 Tra cứu số dư:</span>
              <span class="val"><a id="link-template-check" href="https://grokapi.vorte.me/check?key=${esc(recent[0]?.key || '')}" target="_blank">https://grokapi.vorte.me/check?key=${esc(recent[0]?.key || '')}</a></span>
            </div>
            <div class="info-row">
              <span class="lbl">🤖 Model hỗ trợ:</span>
              <span class="val">grok-4.6</span>
            </div>
            <div class="info-row">
              <span class="lbl">⚡ Chuẩn kết nối:</span>
              <span class="val" style="font-family:var(--sans);">OpenAI Compatible (Chatbox, NextChat, Cursor, VS Code, Dify...)</span>
            </div>

            <div style="margin-top:14px;padding-top:12px;border-top:1px dashed var(--border)">
              <div style="font-weight:700;font-size:12px;margin-bottom:6px;color:var(--fg)">⚡ Lệnh 1-Click cài đặt cho Windows (PowerShell / Codex App):</div>
              <div class="customer-oneclick-box" id="box-template-oneclick">
                irm "https://grokapi.vorte.me/setup-codex-windows?key=${esc(recent[0]?.key || '')}" | iex
              </div>
            </div>

            <div style="margin-top:10px;">
              <div style="font-weight:700;font-size:12px;margin-bottom:6px;color:var(--success)">🐧 Lệnh 1-Click cài đặt cho Linux / macOS (Terminal):</div>
              <div class="customer-oneclick-box" id="box-template-oneclick-linux" style="border-color:rgba(16,185,129,0.3); color:#34d399;">
                curl -fsSL "https://grokapi.vorte.me/setup-linux?key=${esc(recent[0]?.key || '')}" | bash
              </div>
              <div style="font-size:11px;color:var(--muted);margin-top:6px">Khách dùng Linux/Mac chỉ cần mở Terminal dán dòng lệnh trên là tự động lưu biến môi trường + cấu hình Codex App!</div>
            </div>
          </div>

          <div class="card-head" style="margin-top:24px">
            <div>
              <div class="card-title" id="keys-history-title">Lịch sử Key gần đây (${recent.length})</div>
              <div class="card-sub" style="display:flex; align-items:center; gap:6px;">
                <span>Dung lượng Token còn lại của từng Key</span>
                <span style="display:inline-block; width:6px; height:6px; border-radius:50%; background:#10b981; animation: pulse 1.5s infinite;" title="Auto-refresh 2s"></span>
              </div>
            </div>
            <button class="btn btn-ghost" id="btn-refresh-keys" type="button">Làm mới</button>
          </div>
          <div class="table-wrap" style="max-height:350px">
            <table class="results">
              <thead>
                <tr><th>Tên Key</th><th>API Key</th><th>Dung lượng còn lại</th><th>Trạng thái</th><th>Thao tác</th></tr>
              </thead>
              <tbody id="keys-table-body">
                ${recent.length ? recent.map((k) => {
                  const used = Number(k.quota_used || 0);
                  const max = Number(k.quota || 0);
                  const maxTokens = Math.round(max * 500000);
                  const usedTokens = k.actual_used_tokens !== undefined ? k.actual_used_tokens : Math.round(used * 500000);
                  const remainTokens = k.actual_remain_tokens !== undefined ? k.actual_remain_tokens : Math.max(0, maxTokens - usedTokens);
                  const remainPct = k.actual_remain_pct !== undefined ? k.actual_remain_pct : (maxTokens > 0 ? Math.round((remainTokens / maxTokens) * 100) : 100);
                  return `<tr>
                    <td><strong>${esc(k.name || 'Key')}</strong></td>
                    <td><code>${esc(k.key ? k.key.slice(0, 14) + '...' + k.key.slice(-6) : '—')}</code></td>
                    <td style="font-size:12px; line-height: 1.4;">
                      ${maxTokens > 0 ? `
                        <div><b>${remainTokens.toLocaleString()}</b> <span style="color:var(--muted); font-size:11px">/ ${maxTokens.toLocaleString()} tokens</span></div>
                        <div style="font-size:11px; font-weight:600; color:${remainPct > 50 ? '#10b981' : (remainPct > 15 ? '#f59e0b' : '#ef4444')}">
                          ${usedTokens > 0 ? `⚡ Còn ${remainPct}% (Đã dùng ${usedTokens.toLocaleString()} tokens)` : `✨ Chưa dùng (100%)`}
                        </div>
                      ` : 'Không giới hạn (100%)'}
                    </td>
                    <td><span class="tag tag-${k.status === 'active' && (maxTokens === 0 || remainTokens > 0) ? 'ok' : 'fail'}">${esc(k.status === 'active' && (maxTokens === 0 || remainTokens > 0) ? 'active' : 'exhausted')}</span></td>
                    <td>
                      <div class="btn-row" style="margin:0; gap:4px;">
                        <button class="btn btn-ghost btn-row-copy-template" data-key="${esc(k.key)}" data-name="${esc(k.name)}" title="Copy Mẫu Gửi Khách" style="font-size:10px; padding:3px 6px;">📋 Copy</button>
                        <a href="https://grokapi.vorte.me/check?key=${esc(k.key)}" target="_blank" class="btn btn-ghost" title="Tra cứu số dư trên grokapi.vorte.me" style="font-size:10px; padding:3px 6px; text-decoration:none;">🔍 Check</a>
                      </div>
                    </td>
                  </tr>`;
                }).join('') : '<tr><td colspan="5" class="empty">Chưa có key nào</td></tr>'}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  `;

  // Dynamic calculation hint helper
  const updateCalcHint = (toks) => {
    const hint = root.querySelector('#token-calc-hint');
    if (!hint) return;
    const usd = (toks / 500000);
    const vnd = Math.round(usd * 25600);
    if (toks >= 1000000) {
      hint.textContent = `Quy đổi: ${(toks / 1000000).toFixed(1)}M token = $${usd.toFixed(2)} USD (~${vnd.toLocaleString()}đ)`;
    } else {
      hint.textContent = `Quy đổi: ${toks.toLocaleString()} token = $${usd.toFixed(4)} USD (~${vnd.toLocaleString()}đ)`;
    }
  };

  // Handle Preset Click
  root.querySelectorAll('.token-chip').forEach((btn) => {
    btn.addEventListener('click', () => {
      root.querySelectorAll('.token-chip').forEach((c) => c.classList.remove('is-selected'));
      btn.classList.add('is-selected');
      const tokens = Number(btn.dataset.tokens);
      const input = root.querySelector('#key-tokens');
      if (input) {
        if (tokens > 0) {
          input.value = tokens;
          updateCalcHint(tokens);
        } else {
          input.focus();
          input.select();
        }
      }
    });
  });

  // Handle manual input in #key-tokens
  root.querySelector('#key-tokens')?.addEventListener('input', (e) => {
    const val = Number(e.target.value || 0);
    updateCalcHint(val);
    const chips = root.querySelectorAll('.token-chip');
    let matched = false;
    chips.forEach((c) => {
      if (Number(c.dataset.tokens) === val && val > 0) {
        c.classList.add('is-selected');
        matched = true;
      } else {
        c.classList.remove('is-selected');
      }
    });
    if (!matched) {
      chips.forEach((c) => {
        if (Number(c.dataset.tokens) === 0) c.classList.add('is-selected');
      });
    }
  });

  // Customer Template generator helper
  const getCustomerTemplateText = (key, name) => {
    const k = key || 'sk-...';
    return `⚡ THÔNG TIN KẾT NỐI GROK API & CODEX APP:
* 🌐 Base URL: https://grokapi.vorte.me/v1
* 🔑 API Key: ${k}
* 📊 Link Tra Cứu Số Dư: https://grokapi.vorte.me/check?key=${k}
* 🤖 Model: grok-4.6
* ⚡ Tương thích: 100% Codex Desktop App, Chatbox, NextChat, Cursor, VS Code...

🚀 LỆNH 1-CLICK CHO WINDOWS (PowerShell):
irm "https://grokapi.vorte.me/setup-codex-windows?key=${k}" | iex

🐧 LỆNH 1-CLICK CHO LINUX / MACOS (Terminal):
curl -fsSL "https://grokapi.vorte.me/setup-linux?key=${k}" | bash`;
  };

  root.querySelector('#btn-copy-full-customer-template')?.addEventListener('click', async () => {
    const key = recent[0]?.key || '';
    const name = recent[0]?.name || 'Grok';
    try {
      await navigator.clipboard.writeText(getCustomerTemplateText(key, name));
      toast('Đã copy trọn bộ mẫu gửi khách vào clipboard!', 'ok');
    } catch {
      toast('Copy thất bại', 'err');
    }
  });

  root.querySelector('#btn-copy-top-oneclick')?.addEventListener('click', async () => {
    const key = recent[0]?.key || '';
    const cmd = `irm "https://grokapi.vorte.me/setup-codex-windows?key=${key}" | iex`;
    try {
      await navigator.clipboard.writeText(cmd);
      toast('Đã copy Lệnh 1-Click gửi khách vào clipboard!', 'ok');
    } catch {
      toast('Copy thất bại', 'err');
    }
  });

  root.querySelectorAll('.btn-row-copy-template').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const key = btn.dataset.key || '';
      const name = btn.dataset.name || 'Grok';
      try {
        await navigator.clipboard.writeText(getCustomerTemplateText(key, name));
        toast(`Đã copy mẫu của ${name} vào clipboard!`, 'ok');
      } catch {
        toast('Copy thất bại', 'err');
      }
    });
  });

  root.querySelector('#btn-refresh-keys')?.addEventListener('click', () => renderKeys(root));

  // Handle Generate Click
  root.querySelector('#btn-generate-keys')?.addEventListener('click', async () => {
    const btn = root.querySelector('#btn-generate-keys');
    const tokens = Math.max(1000, Number(root.querySelector('#key-tokens')?.value || 1000000));
    const count = Math.max(1, Math.min(100, Number(root.querySelector('#key-count')?.value || 1)));
    const prefix = root.querySelector('#key-prefix')?.value || 'Grok';

    const signature = `${tokens}:${count}:${prefix}`;
    if (!pendingGenerateRequest || pendingGenerateRequest.signature !== signature) {
      pendingGenerateRequest = {
        signature,
        requestId: `console-${crypto.randomUUID?.() || (Date.now() + '-' + Math.random().toString(16).slice(2))}`,
      };
    }
    btn.disabled = true;
    btn.textContent = '⏳ Đang tạo key trên Sub2API...';
    try {
      const res = await generateSub2apiKeys({
        token_amount: tokens,
        count: count,
        name_prefix: prefix,
        group_name: 'Grok',
        request_id: pendingGenerateRequest.requestId,
      });

      if (!res.ok || !res.keys?.length) {
        throw new Error((res.errors || []).join('; ') || 'Không tạo được key nào!');
      }

      toast(`Đã tạo thành công ${res.keys.length} Key (${(tokens >= 1000000 ? (tokens/1000000) + 'M' : tokens.toLocaleString())} tokens)!`, 'ok');
      pendingGenerateRequest = null;

      const container = root.querySelector('#key-result-container');
      const allKeyTexts = res.keys.map((k) => k.key).join('\n');
      const allFormatTexts = res.keys.map((k, i) => `[Key #${i + 1} - ${k.name}]\nBase URL: ${res.base_url}\nAPI Key: ${k.key}\nSố dư: ${tokens.toLocaleString()} Tokens\nTra cứu số dư: https://grokapi.vorte.me/check?key=${k.key}\nModel: grok-4.6`).join('\n\n---\n\n');

      const firstKey = res.keys[0]?.key || '';
      const oneClickCmd = `irm "https://grokapi.vorte.me/setup-windows?key=${firstKey}" | iex`;

      container.hidden = false;
      container.innerHTML = `
        <div class="key-gen-result">
          <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
            <strong style="color:var(--success);font-size:13px">✅ Đã tạo thành công ${res.keys.length} Key:</strong>
            <div class="btn-row" style="margin:0">
              <button class="btn btn-primary" id="btn-copy-oneclick-cmd" type="button" style="background:#0284c7;border-color:#0284c7">⚡ Copy Lệnh 1-Click</button>
              <button class="btn btn-primary" id="btn-copy-all-keys" type="button">📋 Copy Key</button>
              <button class="btn btn-ghost" id="btn-export-key-file" type="button">📥 Tải TXT</button>
            </div>
          </div>
          <textarea readonly id="key-text-output">${esc(allKeyTexts)}</textarea>
          <div style="margin-top:8px;font-size:11px;color:var(--muted)">
            ⚡ <b>Lệnh 1-Click gửi khách:</b> <code>${esc(oneClickCmd)}</code>
          </div>
        </div>
      `;

      container.querySelector('#btn-copy-oneclick-cmd')?.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(oneClickCmd);
          toast(`Đã copy Lệnh 1-Click gửi khách vào clipboard!`, 'ok');
        } catch {
          toast('Copy thất bại', 'err');
        }
      });

      container.querySelector('#btn-copy-all-keys')?.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(allKeyTexts);
          toast(`Đã copy ${res.keys.length} Key vào clipboard!`, 'ok');
        } catch {
          toast('Copy thất bại', 'err');
        }
      });

      container.querySelector('#btn-export-key-file')?.addEventListener('click', () => {
        downloadFile(`grok_keys_${tokens}_${Date.now()}.txt`, allFormatTexts);
      });
    } catch (e) {
      if (e.status === 409 && e.detail?.code === 'INSUFFICIENT_GROK_CAPACITY') {
        const adjusted = await chooseCapacityAmount(e.detail, count);
        if (adjusted !== null) {
          const input = root.querySelector('#key-tokens');
          if (input) {
            input.value = adjusted;
            input.dispatchEvent(new Event('input', { bubbles: true }));
          }
          pendingGenerateRequest = null;
          toast(`Đã điều chỉnh về ${adjusted.toLocaleString()} token/key. Bấm Tạo Key để xác nhận.`, 'ok');
        }
      } else {
        toast(`Lỗi tạo key: ${e.message}`, 'err');
      }
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '⚡ Tạo Key Ngay';
      }
    }
  });
}

/* ── Router ── */
async function route() {
  const hash = location.hash || '#/register';
  const known = PAGE_META[hash] ? hash : '#/register';
  if (known !== location.hash) location.hash = known;
  setActiveNav(known);
  const main = document.getElementById('main-content');
  if (!main) return;

  for (const [routeHash, view] of routeViews) {
    view.hidden = routeHash !== known;
  }

  let view = routeViews.get(known);
  if (!view) {
    view = document.createElement('div');
    view.className = 'route-view';
    view.dataset.routeView = known;
    view.innerHTML = `<div class="empty">Loading…</div>`;
    main.appendChild(view);
    routeViews.set(known, view);
  }
  view.hidden = false;

  if (routeLoads.has(known)) return routeLoads.get(known);
  if (view.dataset.loaded === '1') return;

  const load = (async () => {
    try {
      if (known === '#/register') await renderRegister(view);
      else if (known === '#/results') await renderResults(view);
      else if (known === '#/logs') await renderLogs(view);
      else if (known === '#/keys') await renderKeys(view);
      else if (known === '#/settings') await renderSettings(view);
      else if (known === '#/tools') await renderTools(view);
      view.dataset.loaded = '1';
      if (known === '#/keys') void updateKeysRealtime();
    } catch (e) {
      view.innerHTML = `<div class="empty">${esc(e.message || e)}</div>`;
    } finally {
      routeLoads.delete(known);
    }
  })();
  routeLoads.set(known, load);
  return load;
}

/* ── Poll job ── */
async function pollJob() {
  try {
    const curTool = state.selectedTool || '';
    const snap = await getCurrentJob(curTool, 0);
    if (snap && snap.status) {
      state.job = snap;
    }
    updateRunPill(state.job);
    updateJobProgress(state.job);

    const box = document.getElementById('log-box');
    const statusLine = document.getElementById('job-status-line');
    if (state.job) {
      if (statusLine) {
        statusLine.innerHTML = formatStatusLine(state.job);
      }
      if (box) paintLogs(box, state.job.logs || []);

      // refresh Start/Stop disabled state on register page without full re-render
      const running = ['running', 'pending', 'stopping'].includes(state.job.status);
      const bs = document.getElementById('btn-start');
      const bt = document.getElementById('btn-stop');
      if (bs) bs.disabled = running;
      if (bt) bt.disabled = !running;
    }

    // Live real-time update for retail keys page
    if (location.hash === '#/keys') {
      await updateKeysRealtime();
    }
  } catch (_) {
    /* ignore poll errors */
  }
}

let _lastKeysPoll = 0;
let _keysPollInFlight = false;
async function updateKeysRealtime() {
  const now = Date.now();
  if (_keysPollInFlight || now - _lastKeysPoll < 2000) return; // throttle + no overlapping VPS calls
  _lastKeysPoll = now;
  _keysPollInFlight = true;
  try {
    const [resKeys, resPool] = await Promise.all([
      listSub2apiKeys(1, 30).catch(() => ({ items: [] })),
      fetch('/api/sub2api/pool/stats').then((r) => r.json()).catch(() => null),
    ]);
    if (location.hash !== '#/keys') return;

    if (resPool && resPool.total_accounts > 0) {
      const pool = resPool;
      const pct = pool.remaining_percent ?? 100;
      const pctColor = pct > 60 ? '#10b981' : (pct > 25 ? '#f59e0b' : '#ef4444');

      const elRemain = document.getElementById('stat-pool-remaining');
      if (elRemain) elRemain.textContent = (pool.remaining_tokens || 0).toLocaleString();

      const elSub = document.getElementById('stat-pool-sub');
      if (elSub) elSub.innerHTML = `≈ ${((pool.remaining_tokens || 0) / 1000000).toFixed(1)} Triệu Tokens (${pool.token_per_acc ? (pool.token_per_acc / 1000).toFixed(0) + 'k' : '50k'}/acc thương mại)`;

      const elAcc = document.getElementById('stat-pool-accs');
      if (elAcc) elAcc.textContent = pool.active_accounts || pool.total_accounts || 0;

      const elCap = document.getElementById('stat-pool-capacity');
      if (elCap) elCap.textContent = (pool.safe_keys?.['10k'] || Math.floor((pool.remaining_tokens || 0) / 10000)).toLocaleString();

      const elCapSub = document.getElementById('stat-pool-capacity-sub');
      if (elCapSub) elCapSub.textContent = `${(pool.safe_keys?.['50k'] || Math.floor((pool.remaining_tokens || 0) / 50000)).toLocaleString()} account-equivalent 50k · ${(pool.safe_keys?.['1m'] || Math.floor((pool.remaining_tokens || 0) / 1000000)).toLocaleString()} Key 1M`;

      const elPct = document.getElementById('stat-pool-pct');
      if (elPct) {
        elPct.textContent = `${pct}%`;
        elPct.style.color = pctColor;
      }
      const elUsedSub = document.getElementById('stat-pool-used-sub');
      if (elUsedSub) {
        if ((pool.used_tokens || 0) > 0) {
          elUsedSub.innerHTML = `⚡ Đã dùng ${(pool.used_tokens || 0).toLocaleString()} tokens`;
          elUsedSub.style.color = '#f59e0b';
        } else {
          elUsedSub.innerHTML = `✨ Chưa tiêu thụ`;
          elUsedSub.style.color = 'var(--muted)';
        }
      }
      const elBar = document.getElementById('stat-pool-gauge');
      if (elBar) elBar.style.width = `${pct}%`;
    }

    const tbody = document.getElementById('keys-table-body');
    const recent = resKeys.items || [];
    if (tbody && recent.length) {
      tbody.innerHTML = recent.map((k) => {
        const used = Number(k.quota_used || 0);
        const max = Number(k.quota || 0);
        const maxTokens = Math.round(max * 500000);
        const usedTokens = k.actual_used_tokens !== undefined ? k.actual_used_tokens : Math.round(used * 500000);
        const remainTokens = k.actual_remain_tokens !== undefined ? k.actual_remain_tokens : Math.max(0, maxTokens - usedTokens);
        const remainPct = k.actual_remain_pct !== undefined ? k.actual_remain_pct : (maxTokens > 0 ? Math.round((remainTokens / maxTokens) * 100) : 100);
        return `<tr>
          <td><strong>${esc(k.name || 'Key')}</strong></td>
          <td><code>${esc(k.key ? k.key.slice(0, 14) + '...' + k.key.slice(-6) : '—')}</code></td>
          <td style="font-size:12px; line-height: 1.4;">
            ${maxTokens > 0 ? `
              <div><b>${remainTokens.toLocaleString()}</b> <span style="color:var(--muted); font-size:11px">/ ${maxTokens.toLocaleString()} tokens</span></div>
              <div style="font-size:11px; font-weight:600; color:${remainPct > 50 ? '#10b981' : (remainPct > 15 ? '#f59e0b' : '#ef4444')}">
                ${usedTokens > 0 ? `⚡ Còn ${remainPct}% (Đã dùng ${usedTokens.toLocaleString()} tokens)` : `✨ Chưa dùng (100%)`}
              </div>
            ` : 'Không giới hạn (100%)'}
          </td>
          <td><span class="tag tag-${k.status === 'active' && (maxTokens === 0 || remainTokens > 0) ? 'ok' : 'fail'}">${esc(k.status === 'active' && (maxTokens === 0 || remainTokens > 0) ? 'active' : 'exhausted')}</span></td>
        </tr>`;
      }).join('');
    }
  } catch (_) {
  } finally {
    _keysPollInFlight = false;
  }
}

async function boot() {
  initChrome();
  window.addEventListener('hashchange', () => route());
  try {
    await getHealth();
    const data = await getTools();
    state.tools = data.tools || [];
  } catch (e) {
    toast('API offline: ' + e.message, 'err');
  }
  await route();
  await pollJob();
  state.pollTimer = setInterval(pollJob, 900);
}

boot();
