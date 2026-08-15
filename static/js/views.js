/* M10 鎺у埗涓績瑙嗗浘锛氭瑙?/ 椤圭洰 / Agent / 宸ヤ綔娴併€?   鏁版嵁锛氭瑙堜笌椤圭洰鏉ヨ嚜 /api/state锛堢敱 app.js 浼犲叆锛夛紱Agent 浼氳瘽涓?   宸ヤ綔娴佹潵鑷?/api/v1锛堟湰妯″潡鑺傛祦鎷夊彇锛屼粎娓叉煋鏃惰Е鍙戯級銆?*/

import { $, el, setText, setChildren, icon, escapeHtml, fmtDuration,
  taskExitStatus } from './core.js';

let cachedV1 = { sessions: [], workflowRuns: [], workflows: [], worktrees: [] };
let lastV1Fetch = 0;
let busy = false;

const V1_TTL_MS = 2500;

async function fetchJson(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

/* ---------------- 鐘舵€佹枃妗?---------------- */
function statusClass(status) {
  const good = ['succeeded', 'running', 'ok'];
  const bad = ['failed', 'canceled', 'timed_out', 'lost', 'stopped'];
  if (good.includes(status)) return 'good';
  if (bad.includes(status)) return 'bad';
  return '';
}
function statusLabel(status) {
  return {
    queued: '鎺掗槦涓?, starting: '鍚姩涓?, running: '杩愯涓?,
    succeeded: '鎴愬姛', failed: '澶辫触', canceled: '宸插彇娑?,
    stopped: '宸插仠姝?, timed_out: '瓒呮椂', lost: '涓㈠け', pending: '寰呮墽琛?,
    skipped: '宸茶烦杩?,
  }[status] || status;
}

async function ensureV1() {
  const now = Date.now();
  if (now - lastV1Fetch < V1_TTL_MS && cachedV1.sessions) return;
  if (busy) return;
  busy = true;
  lastV1Fetch = now;
  try {
    const [sessions, workflowRuns, workflows, worktrees] = await Promise.all([
      fetchJson('/api/v1/agents/sessions?limit=100').catch(() => ({ sessions: [] })),
      fetchJson('/api/v1/workflow-runs?limit=50').catch(() => ({ runs: [] })),
      fetchJson('/api/v1/workflows').catch(() => []),
      fetchJson('/api/v1/git/worktrees').catch(() => []),
    ]);
    cachedV1 = {
      sessions: sessions.sessions || [],
      workflowRuns: workflowRuns.runs || [],
      workflows: Array.isArray(workflows) ? workflows : [],
      worktrees: Array.isArray(worktrees) ? worktrees : [],
    };
    renderViews(window.__state ? window.__state.data : null);
  } catch (e) {
    /* 闈欓粯锛歷1 鏁版嵁涓嶅彲鐢ㄤ笉褰卞搷涓昏鍥?*/
  } finally {
    busy = false;
  }
}

/* ================= 姒傝 ================= */
function kpi(iconName, tone, label, value, sub) {
  const card = el('div', 'ov');
  setChildren(card,
    el('span', 'ov-icon tone-' + tone, icon(iconName, 17)),
    (() => {
      const body = el('div', 'ov-body');
      setChildren(body,
        el('div', 'ov-label', label),
        el('div', 'ov-main', value),
        el('div', 'ov-sub', sub || ''));
      return body;
    })());
  return card;
}

function renderOverview(data) {
  const projects = data.projects || [];
  const apps = data.apps || [];
  const services = data.services || [];
  const sessions = cachedV1.sessions;
  const runs = cachedV1.workflowRuns;
  const runningApps = apps.filter(a => a.running).length;
  const failedRuns = runs.filter(r => r.status === 'failed');
  const portConflicts = apps.filter(a => a.portOccupied).length;
  const daemonOk = !data.degraded;
  const row = $('#ovOverviewRow');
  setChildren(row,
    kpi('folder', 'blue', '椤圭洰', String(projects.length),
      '娲昏穬 ' + projects.filter(p => p.runningCount > 0).length),
    kpi('bot', 'purple', 'Agent 浼氳瘽', String(sessions.length),
      '杩愯涓?' + sessions.filter(s => s.status === 'running').length),
    kpi('activity', 'green', '杩愯鏈嶅姟', String(runningApps),
      '鍏?' + apps.length + ' 涓簲鐢?),
    kpi('x', 'red', '澶辫触浠诲姟/宸ヤ綔娴?, String(failedRuns.length),
      portConflicts ? '绔彛鍐茬獊 ' + portConflicts : ''),
    kpi('gauge', daemonOk ? 'green' : 'red', 'daemon',
      daemonOk ? '姝ｅ父' : '闄嶇骇',
      '绔彛 :' + (data.consolePort || '--')));
  const attention = $('#ovAttention');
  setChildren(attention);
  const items = [];
  if (portConflicts) {
    items.push(['bad', '绔彛鍗犵敤', portConflicts + ' 涓簲鐢ㄧ鍙ｈ鍏朵粬杩涚▼鍗犵敤']);
  }
  for (const run of failedRuns) {
    items.push(['bad', '宸ヤ綔娴佸け璐?, run.name + '锛? + run.id + '锛?]);
  }
  for (const session of sessions) {
    if (session.status === 'failed') {
      items.push(['bad', 'Agent 澶辫触', session.id]);
    }
  }
  const blocking = apps.filter(a => !a.running && a.health && a.health.blocking);
  for (const app of blocking.slice(0, 5)) {
    items.push(['warn', '閰嶇疆闂', app.name + '锛? +
      ((app.health.issues || [])[0] || {}).title || '']);
  }
  if (!items.length) {
    setChildren(attention, el('div', 'empty-state', '涓€鍒囨甯革紝鏃犻渶鍏虫敞'));
    return;
  }
  for (const [tone, title, text] of items) {
    const item = el('div', 'attention-item tone-' + tone);
    setChildren(item,
      el('span', 'attention-title', title),
      el('span', 'attention-text', text));
    attention.appendChild(item);
  }
}

/* ================= 椤圭洰 ================= */
function projectCard(project, data) {
  const card = el('article', 'project-card');
  const head = el('div', 'project-head');
  const resources = project.resources || [];
  const running = resources.filter(r => r.kind !== 'mcp_server' &&
    data.apps.some(a => a.id === r.appId && a.running)).length;
  setChildren(head,
    el('span', 'project-icon', icon('folder', 16)),
    el('div', 'project-title', escapeHtml(project.name)),
    el('span', 'project-meta',
      resources.length + ' 璧勬簮 路 ' + running + ' 杩愯涓?));
  const body = el('div', 'project-body');
  const sessions = cachedV1.sessions.filter(s => s.projectId === project.id);
  const workflows = cachedV1.workflows.filter(w => w.projectId === project.id);
  const lines = [];
  if (project.repoPath) {
    lines.push(el('div', 'project-line',
      icon('folder-git-2', 13), ' ' + escapeHtml(project.repoPath)));
  } else if (project.rootPath) {
    lines.push(el('div', 'project-line mono',
      icon('folder', 13), ' ' + escapeHtml(project.rootPath)));
  }
  if (sessions.length) {
    lines.push(el('div', 'project-line',
      icon('bot', 13), ' ' + sessions.length + ' 涓?Agent 浼氳瘽'));
  }
  if (workflows.length) {
    lines.push(el('div', 'project-line',
      icon('link-2', 13), ' ' + workflows.length + ' 涓伐浣滄祦'));
  }
  if (!lines.length) lines.push(el('div', 'project-line', '锛堟棤棰濆淇℃伅锛?));
  setChildren(body, ...lines);
  const actions = el('div', 'project-actions');
  for (const resource of resources.slice(0, 6)) {
    const app = data.apps.find(a => a.id === resource.appId);
    const runningNow = !!(app && app.running);
    const btn = el('button', 'chip-btn' + (runningNow ? ' running' : ''),
      (runningNow ? icon('square', 12) : icon('play', 12)) + ' ' +
      escapeHtml(resource.name));
    btn.type = 'button';
    btn.addEventListener('click', async () => {
      try {
        await fetchJson('/api/v1/resources/' + resource.id +
          (runningNow ? '/stop' : '/start'), { method: 'POST' });
      } catch (e) { /* 闈欓粯 */ }
      window.__poll && window.__poll();
      ensureV1();
    });
    actions.appendChild(btn);
  }
  card.append(head, body, actions);
  return card;
}

function renderProjects(data) {
  const grid = $('#projectsGrid');
  const projects = data.projects || [];
  setChildren(grid);
  if (!projects.length) {
    setChildren(grid, el('div', 'empty-state',
      '鏆傛棤椤圭洰銆傚湪鍚姩鍙版坊鍔犳湇鍔℃椂浼氳嚜鍔ㄦ寜鐩綍褰掔粍銆?));
    return;
  }
  for (const project of projects) {
    grid.appendChild(projectCard(project, data));
  }
}

/* ================= Agent ================= */
function sessionCard(session, adapters) {
  const adapter = adapters.find(a => a.id === session.adapterId);
  const card = el('article', 'agent-card');
  const head = el('div', 'agent-head');
  setChildren(head,
    el('span', 'status-dot ' + statusClass(session.status), ''),
    el('div', 'agent-title', escapeHtml((adapter && adapter.name) || session.adapterId)),
    el('span', 'agent-status ' + statusClass(session.status),
      statusLabel(session.status)));
  const body = el('div', 'agent-body');
  const lines = [];
  lines.push(el('div', 'agent-line mono',
    '浼氳瘽 ' + session.id + (session.pid ? ' 路 PID ' + session.pid : '')));
  if (session.durationSec != null) {
    lines.push(el('div', 'agent-line', '鑰楁椂 ' + fmtDuration(session.durationSec)));
  }
  if (session.exitCode != null) {
    lines.push(el('div', 'agent-line', '閫€鍑虹爜 ' + session.exitCode));
  }
  setChildren(body, ...lines);
  const actions = el('div', 'agent-actions');
  if (session.status === 'running' || session.status === 'queued') {
    const stop = el('button', 'btn btn-sm', '鍋滄');
    stop.type = 'button';
    stop.addEventListener('click', async () => {
      try {
        await fetchJson('/api/v1/agents/sessions/' + session.id + '/stop',
          { method: 'POST' });
      } catch (e) { /* 闈欓粯 */ }
      ensureV1();
    });
    actions.appendChild(stop);
  }
  card.append(head, body, actions);
  return card;
}

async function openAgentModal() {
  const overlay = el('div', 'modal-mask');
  overlay.setAttribute('aria-hidden', 'false');
  const modal = el('div', 'modal');
  const nameInput = el('input');
  nameInput.type = 'text';
  nameInput.placeholder = '浼氳瘽鍚嶇О锛堝彲閫夛級';
  const promptInput = el('textarea');
  promptInput.rows = 4;
  promptInput.placeholder = '鎻愮ず璇嶏紙prompt锛?;
  const adapterSelect = el('select');
  for (const adapter of cachedV1.adapters || []) {
    const option = el('option', '', escapeHtml(adapter.name));
    option.value = adapter.id;
    adapterSelect.appendChild(option);
  }
  const projectSelect = el('select');
  const projects = (window.__state && window.__state.data &&
    window.__state.data.projects) || [];
  for (const project of projects) {
    const option = el('option', '', escapeHtml(project.name));
    option.value = project.id;
    projectSelect.appendChild(option);
  }
  const run = el('button', 'btn', '鍚姩');
  run.type = 'button';
  const close = el('button', 'btn ghost', '鍙栨秷');
  close.type = 'button';
  run.addEventListener('click', async () => {
    if (!adapterSelect.value || !projectSelect.value) return;
    try {
      await fetchJson('/api/v1/agents/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          adapterId: adapterSelect.value,
          projectId: projectSelect.value,
          prompt: promptInput.value || '',
        }),
      });
    } catch (e) { /* 闈欓粯 */ }
    overlay.remove();
    ensureV1();
  });
  close.addEventListener('click', () => overlay.remove());
  overlay.addEventListener('mousedown', e => {
    if (e.target === overlay) overlay.remove();
  });
  setChildren(modal,
    el('h3', 'modal-title', '鏂板缓 Agent 浼氳瘽'),
    el('label', 'field-label', '閫傞厤鍣?), adapterSelect,
    el('label', 'field-label', '椤圭洰'), projectSelect,
    el('label', 'field-label', '鎻愮ず璇?), promptInput,
    el('div', 'modal-actions', close, run));
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  window.__state && (window.__state._mask = overlay);
}

function renderAgents() {
  const list = $('#agentsList');
  const adapters = cachedV1.adapters || [];
  setChildren(list);
  if (!cachedV1.sessions.length) {
    setChildren(list, el('div', 'empty-state',
      '鏆傛棤 Agent 浼氳瘽銆傞厤缃€傞厤鍣ㄥ悗鍦ㄦ鍚姩澶栭儴缂栫爜 Agent銆?));
    return;
  }
  for (const session of cachedV1.sessions) {
    list.appendChild(sessionCard(session, adapters));
  }
  setText($('#agentsCount'), String(cachedV1.sessions.length));
}

/* ================= 宸ヤ綔娴?================= */
function workflowCard(wf) {
  const card = el('article', 'wf-card');
  const runs = cachedV1.workflowRuns.filter(r => r.workflowId === wf.id);
  const latest = runs[0];
  const head = el('div', 'wf-head');
  setChildren(head,
    el('span', 'project-icon', icon('link-2', 16)),
    el('div', 'wf-title', escapeHtml(wf.name)),
    el('span', 'wf-meta', wf.steps.length + ' 姝ラ 路 ' +
      (latest ? statusLabel(latest.status) : '鏈繍琛?)));
  const body = el('div', 'wf-body');
  const steps = (wf.steps || []).map(step =>
    escapeHtml(step.kind) + (step.needs && step.needs.length
      ? ' 鈫?' + step.needs.length : ''));
  setChildren(body, el('div', 'wf-steps mono', steps.join(' 路 ')));
  if (latest) {
    const stepLines = (latest.steps || []).map(sr =>
      el('div', 'wf-step-line',
        el('span', 'status-dot ' + statusClass(sr.status), ''),
        ' ' + escapeHtml(sr.stepId) + ' ' + statusLabel(sr.status) +
        (sr.retries ? '锛堥噸璇?' + sr.retries + '锛? : '')));
    const runsBox = el('div', 'wf-runs');
    setChildren(runsBox,
      el('div', 'wf-run-head', '鏈€杩戣繍琛?' + latest.id +
        ' 路 ' + statusLabel(latest.status)),
      ...stepLines);
    body.appendChild(runsBox);
  }
  const actions = el('div', 'wf-actions');
  const runBtn = el('button', 'btn btn-sm', icon('play', 12) + ' 杩愯');
  runBtn.type = 'button';
  runBtn.addEventListener('click', async () => {
    try {
      await fetchJson('/api/v1/workflows/' + wf.id + '/runs',
        { method: 'POST' });
    } catch (e) { /* 闈欓粯 */ }
    ensureV1();
  });
  actions.appendChild(runBtn);
  if (latest && latest.status === 'running') {
    const cancel = el('button', 'btn btn-sm ghost', '鍙栨秷');
    cancel.type = 'button';
    cancel.addEventListener('click', async () => {
      try {
        await fetchJson('/api/v1/workflow-runs/' + latest.id + '/cancel',
          { method: 'POST' });
      } catch (e) { /* 闈欓粯 */ }
      ensureV1();
    });
    actions.appendChild(cancel);
  }
  card.append(head, body, actions);
  return card;
}

function renderWorkflows() {
  const grid = $('#workflowsGrid');
  setChildren(grid);
  if (!cachedV1.workflows.length) {
    setChildren(grid, el('div', 'empty-state',
      '鏆傛棤宸ヤ綔娴併€傚彲閫氳繃 API 鎴?CLI 鍒涘缓澹版槑寮?DAG 宸ヤ綔娴併€?));
    return;
  }
  for (const wf of cachedV1.workflows) {
    grid.appendChild(workflowCard(wf));
  }
}

/* ================= 鍏ュ彛 ================= */
export function initViews() {
  setChildren($('#railIconOverview'), icon('gauge', 19));
  setChildren($('#railIconProjects'), icon('folder', 19));
  setChildren($('#railIconAgents'), icon('bot', 19));
  setChildren($('#railIconWorkflows'), icon('link-2', 19));
  const newBtn = $('#agentsNewBtn');
  if (newBtn) newBtn.addEventListener('click', openAgentModal);
  window.__state = window.__state || {};
}

export function renderViews(data) {
  if (!data) return;
  window.__state = window.__state || {};
  window.__state.data = data;
  renderOverview(data);
  renderProjects(data);
  const view = window.__state.view;
  if (view === 'agents' || view === 'workflows' || view === 'overview' ||
      view === 'projects') {
    ensureV1();
  }
  if (view === 'agents') renderAgents();
  if (view === 'workflows') renderWorkflows();
}

export async function refreshV1() {
  lastV1Fetch = 0;
  await ensureV1();
}

