/* M10 控制中心视图：概览 / 项目 / Agent / 工作流。
   数据：概览与项目来自 /api/state（由 app.js 传入）；Agent 会话、
   工作流、模板来自 /api/v1（本模块节流拉取，仅渲染时触发）。
   P1：项目新建（模板）、适配器注册、工作流创建、会话日志展开。 */

import { $, el, setText, setChildren, icon, escapeHtml, fmtDuration } from './core.js';

let cachedV1 = { sessions: [], workflowRuns: [], workflows: [], worktrees: [],
                 templates: [], adapters: [] };
let lastV1Fetch = 0;
let busy = false;

const V1_TTL_MS = 2500;

async function fetchJson(url, options) {
  const r = await fetch(url, Object.assign({ cache: 'no-store' }, options));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

async function postJson(url, body) {
  return fetchJson(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

/* ---------------- 状态文案 ---------------- */
function statusClass(status) {
  const good = ['succeeded', 'running', 'ok'];
  const bad = ['failed', 'canceled', 'timed_out', 'lost', 'stopped'];
  if (good.includes(status)) return 'good';
  if (bad.includes(status)) return 'bad';
  return '';
}
function statusLabel(status) {
  return {
    queued: '排队中', starting: '启动中', running: '运行中',
    succeeded: '成功', failed: '失败', canceled: '已取消',
    stopped: '已停止', timed_out: '超时', lost: '丢失', pending: '待执行',
    skipped: '已跳过',
  }[status] || status;
}

async function ensureV1() {
  const now = Date.now();
  if (now - lastV1Fetch < V1_TTL_MS && cachedV1.sessions) return;
  if (busy) return;
  busy = true;
  lastV1Fetch = now;
  try {
    const [sessions, workflowRuns, workflows, worktrees, templates, adapters] =
      await Promise.all([
        fetchJson('/api/v1/agents/sessions?limit=100').catch(() => ({ sessions: [] })),
        fetchJson('/api/v1/workflow-runs?limit=50').catch(() => ({ runs: [] })),
        fetchJson('/api/v1/workflows').catch(() => []),
        fetchJson('/api/v1/git/worktrees').catch(() => []),
        fetchJson('/api/v1/project-templates').catch(() => []),
        fetchJson('/api/v1/agents/adapters').catch(() => []),
      ]);
    cachedV1 = {
      sessions: sessions.sessions || [],
      workflowRuns: workflowRuns.runs || [],
      workflows: Array.isArray(workflows) ? workflows : [],
      worktrees: Array.isArray(worktrees) ? worktrees : [],
      templates: Array.isArray(templates) ? templates : [],
      adapters: Array.isArray(adapters) ? adapters : [],
    };
    renderViews(window.__state ? window.__state.data : null);
  } catch (e) {
    /* 静默：v1 数据不可用不影响主视图 */
  } finally {
    busy = false;
  }
}

/* ================= 模态基座 ================= */
function openModal(title, fields, onSave) {
  const overlay = el('div', 'modal-mask');
  const modal = el('div', 'modal');
  const save = el('button', 'btn', '确定');
  save.type = 'button';
  const close = el('button', 'btn ghost', '取消');
  close.type = 'button';
  save.addEventListener('click', async () => {
    try {
      await onSave();
      overlay.remove();
      ensureV1();
    } catch (e) { /* 静默 */ }
  });
  close.addEventListener('click', () => overlay.remove());
  overlay.addEventListener('mousedown', e => {
    if (e.target === overlay) overlay.remove();
  });
  const children = [el('h3', 'modal-title', title)];
  for (const field of fields) {
    children.push(el('label', 'field-label', field.label));
    children.push(field.node);
  }
  children.push(el('div', 'modal-actions', close, save));
  setChildren(modal, ...children);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  return { overlay, close };
}

function textInput(placeholder) {
  const node = el('input');
  node.type = 'text';
  node.placeholder = placeholder || '';
  return node;
}

function selectNode(options, placeholder) {
  const node = el('select');
  const blank = el('option', '', placeholder || '请选择');
  blank.value = '';
  node.appendChild(blank);
  for (const [value, label] of options) {
    const option = el('option', '', escapeHtml(label));
    option.value = value;
    node.appendChild(option);
  }
  return node;
}

/* ================= 概览 ================= */
function kpi(iconName, tone, label, value, sub) {
  const card = el('div', 'ov');
  const body = el('div', 'ov-body');
  setChildren(body,
    el('div', 'ov-label', label),
    el('div', 'ov-main', value),
    el('div', 'ov-sub', sub || ''));
  setChildren(card, el('span', 'ov-icon tone-' + tone, icon(iconName, 17)), body);
  return card;
}

function renderOverview(data) {
  const projects = data.projects || [];
  const apps = data.apps || [];
  const sessions = cachedV1.sessions;
  const runs = cachedV1.workflowRuns;
  const runningApps = apps.filter(a => a.running).length;
  const failedRuns = runs.filter(r => r.status === 'failed');
  const portConflicts = apps.filter(a => a.portOccupied).length;
  const daemonOk = !data.degraded;
  const row = $('#ovOverviewRow');
  setChildren(row,
    kpi('folder', 'blue', '项目', String(projects.length),
      '活跃 ' + projects.filter(p => p.runningCount > 0).length),
    kpi('bot', 'purple', 'Agent 会话', String(sessions.length),
      '运行中 ' + sessions.filter(s => s.status === 'running').length),
    kpi('activity', 'green', '运行服务', String(runningApps),
      '共 ' + apps.length + ' 个应用'),
    kpi('x', 'red', '失败任务/工作流', String(failedRuns.length),
      portConflicts ? '端口冲突 ' + portConflicts : ''),
    kpi('gauge', daemonOk ? 'green' : 'red', 'daemon',
      daemonOk ? '正常' : '降级',
      '端口 :' + (data.consolePort || '--')));
  const attention = $('#ovAttention');
  setChildren(attention);
  const items = [];
  if (portConflicts) {
    items.push(['bad', '端口占用', portConflicts + ' 个应用端口被其他进程占用']);
  }
  for (const run of failedRuns) {
    items.push(['bad', '工作流失败', run.name + '（' + run.id + '）']);
  }
  for (const session of sessions) {
    if (session.status === 'failed') {
      items.push(['bad', 'Agent 失败', session.id]);
    }
  }
  const blocking = apps.filter(a => !a.running && a.health && a.health.blocking);
  for (const app of blocking.slice(0, 5)) {
    items.push(['warn', '配置问题', app.name + '：' +
      (((app.health.issues || [])[0] || {}).title || '')]);
  }
  if (!items.length) {
    setChildren(attention, el('div', 'empty-state', '一切正常，无需关注'));
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

/* ================= 项目 ================= */
function openProjectModal() {
  const nameInput = textInput('项目名称');
  const pathInput = textInput('项目根路径（绝对路径）');
  const templateSelect = selectNode(
    cachedV1.templates.map(t => [t.id, t.name + ' — ' + t.description]),
    '不使用模板');
  openModal('新建项目', [
    { label: '名称', node: nameInput },
    { label: '根路径', node: pathInput },
    { label: '模板（可选）', node: templateSelect },
  ], async () => {
    if (!nameInput.value.trim() || !pathInput.value.trim()) {
      throw new Error('名称与路径必填');
    }
    await postJson('/api/v1/projects', {
      name: nameInput.value.trim(),
      rootPath: pathInput.value.trim(),
      template: templateSelect.value || undefined,
    });
    window.__poll && window.__poll();
  });
}

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
      resources.length + ' 资源 · ' + running + ' 运行中'));
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
      icon('bot', 13), ' ' + sessions.length + ' 个 Agent 会话'));
  }
  if (workflows.length) {
    lines.push(el('div', 'project-line',
      icon('link-2', 13), ' ' + workflows.length + ' 个工作流'));
  }
  if (!lines.length) lines.push(el('div', 'project-line', '（无额外信息）'));
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
        await postJson('/api/v1/resources/' + resource.id +
          (runningNow ? '/stop' : '/start'));
      } catch (e) { /* 静默 */ }
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
  setText($('#projectsCount'), projects.length ? String(projects.length) : '');
  if (!projects.length) {
    setChildren(grid, el('div', 'empty-state',
      '暂无项目。点「新建项目」或添加服务时按目录自动归组。'));
    return;
  }
  for (const project of projects) {
    grid.appendChild(projectCard(project, data));
  }
}

/* ================= Agent ================= */
function openAdapterModal() {
  const nameInput = textInput('适配器名称（如 OpenCode）');
  const executableInput = textInput('可执行文件（如 opencode）');
  const argsInput = textInput('参数模板，逗号分隔（如 run,--prompt-file,{prompt_file}）');
  const envInput = textInput('环境模板，KEY=VALUE 逗号分隔（可选）');
  openModal('注册 Agent 适配器', [
    { label: '名称', node: nameInput },
    { label: '可执行文件', node: executableInput },
    { label: '参数模板', node: argsInput },
    { label: '环境模板', node: envInput },
  ], async () => {
    if (!nameInput.value.trim() || !executableInput.value.trim()) {
      throw new Error('名称与可执行文件必填');
    }
    const env = {};
    for (const part of (envInput.value || '').split(',')) {
      const [key, ...rest] = part.trim().split('=');
      if (key && rest.length) env[key.trim()] = rest.join('=').trim();
    }
    await postJson('/api/v1/agents/adapters', {
      name: nameInput.value.trim(),
      executable: executableInput.value.trim(),
      argsTemplate: (argsInput.value || '').split(',').map(s => s.trim())
        .filter(Boolean),
      envTemplate: env,
      stdinMode: 'file',
    });
  });
}

function openAgentModal() {
  if (!cachedV1.adapters.length) {
    openAdapterModal();
    return;
  }
  const adapterSelect = selectNode(
    cachedV1.adapters.map(a => [a.id, a.name]),
    '选择适配器');
  const projects = (window.__state && window.__state.data &&
    window.__state.data.projects) || [];
  const projectSelect = selectNode(
    projects.map(p => [p.id, p.name]),
    '选择项目');
  const promptInput = el('textarea');
  promptInput.rows = 4;
  promptInput.placeholder = '提示词（prompt）';
  openModal('新建 Agent 会话', [
    { label: '适配器', node: adapterSelect },
    { label: '项目', node: projectSelect },
    { label: '提示词', node: promptInput },
  ], async () => {
    if (!adapterSelect.value || !projectSelect.value) {
      throw new Error('请选择适配器与项目');
    }
    await postJson('/api/v1/agents/sessions', {
      adapterId: adapterSelect.value,
      projectId: projectSelect.value,
      prompt: promptInput.value || '',
    });
  });
}

function sessionCard(session, adapters) {
  const adapter = adapters.find(a => a.id === session.adapterId);
  const card = el('article', 'agent-card');
  card.tabIndex = 0;
  const head = el('div', 'agent-head');
  setChildren(head,
    el('span', 'status-dot ' + statusClass(session.status), ''),
    el('div', 'agent-title', escapeHtml((adapter && adapter.name) || session.adapterId)),
    el('span', 'agent-status ' + statusClass(session.status),
      statusLabel(session.status)));
  const body = el('div', 'agent-body');
  const lines = [];
  lines.push(el('div', 'agent-line mono',
    '会话 ' + session.id + (session.pid ? ' · PID ' + session.pid : '')));
  if (adapter && adapter.cost && adapter.cost.model) {
    lines.push(el('div', 'agent-line',
      '模型 ' + escapeHtml(adapter.cost.model) +
      (adapter.tokenBudget ? ' · 预算 ' + adapter.tokenBudget : '')));
  }
  if (session.durationSec != null) {
    lines.push(el('div', 'agent-line', '耗时 ' + fmtDuration(session.durationSec)));
  }
  if (session.exitCode != null) {
    lines.push(el('div', 'agent-line', '退出码 ' + session.exitCode));
  }
  setChildren(body, ...lines);
  const actions = el('div', 'agent-actions');
  if (session.status === 'running' || session.status === 'queued') {
    const stop = el('button', 'btn btn-sm', '停止');
    stop.type = 'button';
    stop.addEventListener('click', async (event) => {
      event.stopPropagation();
      try {
        await postJson('/api/v1/agents/sessions/' + session.id + '/stop');
      } catch (e) { /* 静默 */ }
      ensureV1();
    });
    actions.appendChild(stop);
  }
  const logs = el('button', 'btn btn-sm ghost', '日志');
  logs.type = 'button';
  logs.addEventListener('click', async (event) => {
    event.stopPropagation();
    await toggleSessionLogs(card, session.id);
  });
  actions.appendChild(logs);
  card.append(head, body, actions);
  return card;
}

async function toggleSessionLogs(card, sessionId) {
  let drawer = card.querySelector('.agent-log-drawer');
  if (drawer) {
    drawer.remove();
    return;
  }
  drawer = el('div', 'agent-log-drawer');
  const pre = el('pre', 'agent-log mono');
  pre.textContent = '加载中…';
  drawer.appendChild(pre);
  card.appendChild(drawer);
  try {
    const data = await fetchJson('/api/v1/agents/sessions/' + sessionId + '/logs?tail=300');
    pre.textContent = data.text || '（无日志）';
  } catch (e) {
    pre.textContent = '日志加载失败';
  }
}

function renderAgents() {
  const list = $('#agentsList');
  const adapters = cachedV1.adapters || [];
  setChildren(list);
  setText($('#agentsCount'), String(cachedV1.sessions.length));
  if (!cachedV1.sessions.length) {
    setChildren(list, el('div', 'empty-state',
      '暂无 Agent 会话。「注册适配器」后即可启动外部编码 Agent。'));
    return;
  }
  for (const session of cachedV1.sessions) {
    list.appendChild(sessionCard(session, adapters));
  }
}

/* ================= 工作流 ================= */
function openWorkflowModal() {
  const nameInput = textInput('工作流名称');
  const projects = (window.__state && window.__state.data &&
    window.__state.data.projects) || [];
  const projectSelect = selectNode(
    projects.map(p => [p.id, p.name]),
    '选择项目');
  const stepsInput = el('textarea');
  stepsInput.rows = 10;
  stepsInput.placeholder =
    '步骤 JSON 数组，例如：\n' +
    '[{"id":"impl","kind":"agent","config":{"adapterId":"<adapter>","prompt":"实现功能"}},\n' +
    ' {"id":"tests","kind":"task","needs":["impl"],"config":{"resourceId":"<resource>"}},\n' +
    ' {"id":"gate","kind":"gate","needs":["tests"],"config":{"command":"python -m pytest"}}]';
  openModal('新建工作流（DAG）', [
    { label: '名称', node: nameInput },
    { label: '项目', node: projectSelect },
    { label: '步骤（JSON）', node: stepsInput },
  ], async () => {
    let steps;
    try {
      steps = JSON.parse(stepsInput.value || '[]');
    } catch (e) {
      throw new Error('步骤不是合法 JSON');
    }
    if (!nameInput.value.trim() || !projectSelect.value) {
      throw new Error('名称与项目必填');
    }
    await postJson('/api/v1/workflows', {
      name: nameInput.value.trim(),
      projectId: projectSelect.value,
      steps,
    });
  });
}

function workflowCard(wf) {
  const card = el('article', 'wf-card');
  const runs = cachedV1.workflowRuns.filter(r => r.workflowId === wf.id);
  const latest = runs[0];
  const head = el('div', 'wf-head');
  setChildren(head,
    el('span', 'project-icon', icon('link-2', 16)),
    el('div', 'wf-title', escapeHtml(wf.name)),
    el('span', 'wf-meta', wf.steps.length + ' 步骤 · ' +
      (latest ? statusLabel(latest.status) : '未运行')));
  const body = el('div', 'wf-body');
  const steps = (wf.steps || []).map(step =>
    escapeHtml(step.kind) + (step.needs && step.needs.length
      ? ' ← ' + step.needs.length : ''));
  setChildren(body, el('div', 'wf-steps mono', steps.join(' · ')));
  if (latest) {
    const stepLines = (latest.steps || []).map(sr =>
      el('div', 'wf-step-line',
        el('span', 'status-dot ' + statusClass(sr.status), ''),
        ' ' + escapeHtml(sr.stepId) + ' ' + statusLabel(sr.status) +
        (sr.retries ? '（重试 ' + sr.retries + '）' : '') +
        (sr.error ? ' — ' + escapeHtml(sr.error) : '')));
    const runsBox = el('div', 'wf-runs');
    setChildren(runsBox,
      el('div', 'wf-run-head', '最近运行 ' + latest.id +
        ' · ' + statusLabel(latest.status)),
      ...stepLines);
    body.appendChild(runsBox);
  }
  const actions = el('div', 'wf-actions');
  const runBtn = el('button', 'btn btn-sm', icon('play', 12) + ' 运行');
  runBtn.type = 'button';
  runBtn.addEventListener('click', async () => {
    try {
      await postJson('/api/v1/workflows/' + wf.id + '/runs');
    } catch (e) { /* 静默 */ }
    ensureV1();
  });
  actions.appendChild(runBtn);
  if (latest && latest.status === 'running') {
    const cancel = el('button', 'btn btn-sm ghost', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', async () => {
      try {
        await postJson('/api/v1/workflow-runs/' + latest.id + '/cancel');
      } catch (e) { /* 静默 */ }
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
  setText($('#workflowsCount'), String(cachedV1.workflows.length));
  if (!cachedV1.workflows.length) {
    setChildren(grid, el('div', 'empty-state',
      '暂无工作流。点「新建工作流」创建 DAG（步骤 JSON）。'));
    return;
  }
  for (const wf of cachedV1.workflows) {
    grid.appendChild(workflowCard(wf));
  }
}

/* ================= 入口 ================= */
export function initViews() {
  setChildren($('#railIconOverview'), icon('gauge', 19));
  setChildren($('#railIconProjects'), icon('folder', 19));
  setChildren($('#railIconAgents'), icon('bot', 19));
  setChildren($('#railIconWorkflows'), icon('link-2', 19));
  const newProject = $('#projectsNewBtn');
  if (newProject) newProject.addEventListener('click', openProjectModal);
  const newAgent = $('#agentsNewBtn');
  if (newAgent) newAgent.addEventListener('click', openAgentModal);
  const adapterBtn = $('#agentsAdapterBtn');
  if (adapterBtn) adapterBtn.addEventListener('click', openAdapterModal);
  const newWorkflow = $('#workflowsNewBtn');
  if (newWorkflow) newWorkflow.addEventListener('click', openWorkflowModal);
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
