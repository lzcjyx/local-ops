/* M10 控制中心视图：概览 / 项目 / Agent / 工作流。
   数据：概览与项目来自 /api/state（由 app.js 传入）；Agent 会话、
   工作流、模板来自 /api/v1（本模块节流拉取，仅渲染时触发）。
   P1：项目新建（模板）、适配器注册、工作流创建、会话日志展开。 */

import { $, el, setText, setChildren, icon, escapeHtml, fmtDuration } from './core.js';
import { openConfirm } from './overlays.js';

let cachedV1 = { sessions: [], workflowRuns: [], workflows: [], worktrees: [],
                 templates: [], adapters: [], projectsDetail: [] };
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

async function deleteJson(url) {
  const r = await fetch(url, { method: 'DELETE', cache: 'no-store' });
  if (!r.ok) {
    let message = 'HTTP ' + r.status;
    try {
      const payload = await r.json();
      if (payload && payload.error) message = payload.error;
    } catch (e) { /* 静默 */ }
    throw new Error(message);
  }
  return r.json();
}

function confirmDelete(title, detail, onOk) {
  openConfirm({
    title,
    bodyHtml: '<div class="confirm-detail">' + escapeHtml(detail) + '</div>',
    okText: '删除',
    tone: 'danger',
    onOk: async () => {
      try {
        await onOk();
        window.__poll && window.__poll();
        ensureV1();
      } catch (e) {
        window.__toast ? window.__toast('删除失败：' + e.message) : null;
      }
    },
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
    const [sessions, workflowRuns, workflows, worktrees, templates, adapters,
           projectsDetail] =
      await Promise.all([
        fetchJson('/api/v1/agents/sessions?limit=100').catch(() => ({ sessions: [] })),
        fetchJson('/api/v1/workflow-runs?limit=50').catch(() => ({ runs: [] })),
        fetchJson('/api/v1/workflows').catch(() => []),
        fetchJson('/api/v1/git/worktrees').catch(() => []),
        fetchJson('/api/v1/project-templates').catch(() => []),
        fetchJson('/api/v1/agents/adapters').catch(() => []),
        fetchJson('/api/v1/projects').catch(() => []),
      ]);
    cachedV1 = {
      sessions: sessions.sessions || [],
      workflowRuns: workflowRuns.runs || [],
      workflows: Array.isArray(workflows) ? workflows : [],
      worktrees: Array.isArray(worktrees) ? worktrees : [],
      templates: Array.isArray(templates) ? templates : [],
      adapters: Array.isArray(adapters) ? adapters : [],
      projectsDetail: Array.isArray(projectsDetail) ? projectsDetail : [],
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
  const overlay = el('div', 'modal-mask open');
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
    } catch (e) {
      /* 保存失败：给出明确提示，弹窗保持打开 */
      const hint = el('div', 'hint form-error',
        icon('alert-triangle', 12) + ' ' + escapeHtml(e.message || '操作失败'));
      modal.querySelectorAll('.form-error').forEach(n => n.remove());
      modal.appendChild(hint);
    }
  });
  close.addEventListener('click', () => overlay.remove());
  overlay.addEventListener('mousedown', e => {
    if (e.target === overlay) overlay.remove();
  });
  const children = [el('h3', 'modal-title', title)];
  for (const field of fields) {
    children.push(field.wrap || field);
  }
  children.push(el('div', 'modal-actions', close, save));
  setChildren(modal, ...children);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  return { overlay, close, modal, save };
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

/* 与现有表单一致的表单字段（.field 结构，ops 主题样式） */
function fieldWrap(label, node, hint) {
  const wrapper = el('div', 'field');
  const head = el('div', 'field-head');
  setChildren(head, el('label', '', escapeHtml(label)));
  wrapper.appendChild(head);
  wrapper.appendChild(node);
  if (hint) {
    wrapper.appendChild(el('div', 'hint', hint));
  }
  return wrapper;
}

function fieldInput(label, placeholder, hint) {
  return fieldWrap(label, textInput(placeholder), hint);
}

function fieldSelect(label, options, placeholder, hint) {
  return fieldWrap(label, selectNode(options, placeholder), hint);
}

/* 带「选择文件夹」的路径字段（原生目录选择器） */
function fieldDir(label, placeholder, hint) {
  const input = textInput(placeholder);
  const row = el('div', 'input-row');
  row.appendChild(input);
  const pick = el('button', 'btn', '选择…');
  pick.type = 'button';
  pick.addEventListener('click', async () => {
    pick.disabled = true;
    try {
      const r = await postJson('/api/pick', { what: 'dir' });
      if (r && !r.canceled && r.path) {
        input.value = r.path;
        const change = new Event('input', { bubbles: true });
        input.dispatchEvent(change);
      }
    } catch (e) { /* 静默 */ }
    pick.disabled = false;
  });
  row.appendChild(pick);
  return { wrap: fieldWrap(label, row, hint), input };
}

/* 异步刷新下拉选项（模板/资源在 ensureV1 完成后回填） */
function refreshSelectOptions(select, options, placeholder) {
  setChildren(select);
  const blank = el('option', '', placeholder || '请选择');
  blank.value = '';
  select.appendChild(blank);
  for (const [value, label] of options) {
    const option = el('option', '', escapeHtml(label));
    option.value = value;
    select.appendChild(option);
  }
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
function exportManifest() {
  fetchJson('/api/v1/projects/export').then(manifest => {
    const blob = new Blob([JSON.stringify(manifest, null, 2)],
      { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = el('a');
    link.href = url;
    link.download = 'adcc-projects.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }).catch(() => {});
}

function importManifest() {
  const fileInput = el('input');
  fileInput.type = 'file';
  fileInput.accept = '.json,application/json';
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    try {
      const manifest = JSON.parse(await file.text());
      const result = await postJson('/api/v1/projects/import', manifest);
      window.__poll && window.__poll();
      ensureV1();
    } catch (e) { /* 静默 */ }
  });
  fileInput.click();
}

function openProjectModal() {
  const dir = fieldDir('根路径', '项目根目录（可点「选择…」）',
    '服务会自动按此目录归组到本项目');
  const nameInput = textInput('项目名称（默认取目录名）');
  const nameField = fieldWrap('名称', nameInput, '留空时使用根目录名');
  const templateSelect = selectNode(
    cachedV1.templates.map(t => [t.id, t.name + ' — ' + t.description]),
    '不使用模板');
  if (!cachedV1.templates.length) {
    fetchJson('/api/v1/project-templates').then(list => {
      cachedV1.templates = Array.isArray(list) ? list : [];
      refreshSelectOptions(
        templateSelect,
        cachedV1.templates.map(t => [t.id, t.name + ' — ' + t.description]),
        '不使用模板');
    }).catch(() => {});
  }
  const overlay = openModal('新建项目', [
    dir.wrap,
    nameField,
    fieldWrap('模板（可选）', templateSelect,
      '模板会预填一组资源（如 Web 前端开发服务器）'),
  ], async () => {
    const root = dir.input.value.trim();
    if (!root) throw new Error('请选择项目根路径');
    const fallback = root.split(/[\\/]/).pop() || '新项目';
    await postJson('/api/v1/projects', {
      name: nameInput.value.trim() || fallback,
      rootPath: root,
      template: templateSelect.value || undefined,
    });
    window.__poll && window.__poll();
  });
  overlay.close.focus();
}

function projectCard(project, data) {
  const card = el('article', 'project-card');
  const head = el('div', 'project-head');
  // 资源详情来自 /api/v1/projects（/api/state 的 projects 是摘要）
  const detail = cachedV1.projectsDetail.find(p => p.id === project.id);
  const resources = (detail && detail.resources) || project.resources || [];
  const running = resources.filter(r => r.kind !== 'mcp_server' &&
    data.apps.some(a => a.id === r.appId && a.running)).length;
  setChildren(head,
    el('span', 'project-icon', icon('folder', 16)),
    el('div', 'project-title', escapeHtml(project.name)),
    el('span', 'project-meta',
      resources.length + ' 资源 · ' + running + ' 运行中'));
  const deleteBtn = el('button', 'icon-btn sm danger', icon('trash-2', 13));
  deleteBtn.type = 'button';
  deleteBtn.title = '删除项目';
  deleteBtn.setAttribute('aria-label', '删除项目');
  deleteBtn.addEventListener('click', () => {
    confirmDelete('删除项目', '项目「' + project.name + '」及其未分配资源将被移除（受管应用保留）。',
      async () => deleteJson('/api/v1/projects/' + project.id));
  });
  head.appendChild(deleteBtn);
  const body = el('div', 'project-body');
  const sessions = cachedV1.sessions.filter(s => s.projectId === project.id);
  const workflows = cachedV1.workflows.filter(w => w.projectId === project.id);
  const worktrees = cachedV1.worktrees
    .filter(w => w.projectId === project.id)
    .flatMap(w => w.worktrees || [])
    .filter(w => w.branch && w.branch.startsWith('adcc/'));
  const lines = [];
  if (project.repoPath) {
    lines.push(el('div', 'project-line',
      icon('folder-git-2', 13), ' ' + escapeHtml(project.repoPath)));
  } else if (project.rootPath) {
    lines.push(el('div', 'project-line mono',
      icon('folder', 13), ' ' + escapeHtml(project.rootPath)));
  }
  if (worktrees.length) {
    lines.push(el('div', 'project-line',
      icon('link-2', 13), ' ' + worktrees.length + ' 个隔离 worktree（' +
      worktrees.map(w => escapeHtml(w.branch)).join('、') + '）'));
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
  for (const resource of resources.slice(0, 8)) {
    const app = data.apps.find(a => a.id === resource.appId);
    const runningNow = !!(app && app.running);
    const group = el('span', 'chip-group');
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
    const remove = el('button', 'chip-btn danger', icon('x', 11));
    remove.type = 'button';
    remove.title = '删除资源';
    remove.setAttribute('aria-label', '删除资源 ' + resource.name);
    remove.addEventListener('click', () => {
      confirmDelete('删除资源', '资源「' + resource.name +
        '」及其关联应用将被删除。',
        async () => deleteJson('/api/v1/resources/' + resource.id));
    });
    group.append(btn, remove);
    actions.appendChild(group);
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
  const nameInput = textInput('如 OpenCode / Codex / 自定义');
  const executableInput = textInput('如 opencode、codex、python');
  const argsInput = textInput('如 run,--prompt-file,{prompt_file}');
  const envInput = textInput('如 ADCC_PROJECT_ID={project_id}（可选）');
  const overlay = openModal('注册 Agent 适配器', [
    fieldWrap('名称', nameInput),
    fieldWrap('可执行文件', executableInput,
      '命令模板变量：{project_id} {session_id} {project_root} {prompt_file} {worktree_path}'),
    fieldWrap('参数模板', argsInput, '逗号分隔；{prompt_file} 会写入提示词文件路径'),
    fieldWrap('环境模板', envInput, '逗号分隔的 KEY=VALUE'),
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
  /* 已安装 Agent 一键填充（探测 PATH） */
  fetchJson('/api/v1/agents/discovery').then(found => {
    if (!found || !found.length) return;
    const bar = el('div', 'agent-adapters');
    bar.appendChild(el('span', 'adapter-empty', '已安装：'));
    for (const agent of found) {
      const chip = el('button', 'chip-btn', icon('bot', 12) + ' ' +
        escapeHtml(agent.label));
      chip.type = 'button';
      chip.addEventListener('click', () => {
        nameInput.value = agent.label;
        executableInput.value = agent.executable;
        argsInput.value = (agent.argsTemplate || []).join(',');
      });
      bar.appendChild(chip);
    }
    const title = overlay.modal.querySelector('.modal-title');
    if (title) title.parentElement.insertBefore(bar, title.nextSibling);
  }).catch(() => {});
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
  promptInput.rows = 5;
  promptInput.placeholder = '例如：实现 xxx 功能并补充测试，完成后汇报改动摘要';
  openModal('新建 Agent 会话', [
    fieldWrap('适配器', adapterSelect,
      '尚未注册适配器？先点「注册适配器」。'),
    fieldWrap('项目', projectSelect),
    fieldWrap('提示词', promptInput,
      '提示词会写入会话文件并注入 {prompt_file}'),
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
  /* 适配器管理区 */
  const adapterBar = el('div', 'agent-adapters');
  if (adapters.length) {
    for (const adapter of adapters) {
      const chip = el('span', 'adapter-chip',
        icon('bot', 12) + ' ' + escapeHtml(adapter.name) +
        (adapter.cost && adapter.cost.model
          ? ' · ' + escapeHtml(adapter.cost.model) : ''));
      const remove = el('button', 'chip-btn danger', icon('x', 11));
      remove.type = 'button';
      remove.title = '删除适配器';
      remove.setAttribute('aria-label', '删除适配器 ' + adapter.name);
      remove.addEventListener('click', () => {
        confirmDelete('删除适配器', '适配器「' + adapter.name +
          '」将被移除（历史会话保留）。',
          async () => deleteJson('/api/v1/agents/adapters/' + adapter.id));
      });
      chip.appendChild(remove);
      adapterBar.appendChild(chip);
    }
  } else {
    adapterBar.appendChild(el('span', 'adapter-empty',
      '尚未注册适配器。点击「注册适配器」添加。'));
  }
  list.appendChild(adapterBar);
  if (!cachedV1.sessions.length) {
    list.appendChild(el('div', 'empty-state',
      '暂无 Agent 会话。注册适配器后即可启动外部编码 Agent。'));
    return;
  }
  for (const session of cachedV1.sessions) {
    list.appendChild(sessionCard(session, adapters));
  }
}

/* ================= 工作流 ================= */
function openWorkflowModal() {
  const nameInput = textInput('如：实现→测试→评审');
  const projects = (window.__state && window.__state.data &&
    window.__state.data.projects) || [];
  const projectSelect = selectNode(
    projects.map(p => [p.id, p.name]),
    '选择项目');
  const steps = [];            // {id, kind, config, needs}
  const stepRows = [];         // DOM 行
  const editor = el('div', 'wf-step-editor');

  function projectResources() {
    // 实时读取 /api/v1 的项目详情（含 resources）
    const projectId = projectSelect.value;
    const project = cachedV1.projectsDetail.find(p => p.id === projectId);
    return (project && project.resources) || [];
  }

  function rebuildNeeds() {
    for (const row of stepRows) {
      const box = row.querySelector('.needs-box');
      setChildren(box);
      if (steps.length <= 1) {
        box.appendChild(el('span', 'adapter-empty', '无依赖步骤'));
        continue;
      }
      for (const other of steps) {
        if (other.id === row._step.id) continue;
        const label = el('label');
        const check = el('input');
        check.type = 'checkbox';
        check.checked = row._step.needs.includes(other.id);
        check.addEventListener('change', () => {
          const index = row._step.needs.indexOf(other.id);
          if (check.checked && index < 0) row._step.needs.push(other.id);
          if (!check.checked && index >= 0) row._step.needs.splice(index, 1);
        });
        label.appendChild(check);
        label.appendChild(document.createTextNode(' ' + other.id));
        box.appendChild(label);
      }
    }
  }

  function renderStepConfig(row) {
    const step = row._step;
    const configBox = row.querySelector('.wf-step-config');
    setChildren(configBox);
    const kind = step.kind;
    if (kind === 'service' || kind === 'task') {
      const resources = projectResources();
      const resourceSelect = selectNode(
        resources.map(r => [r.id, r.name + '（' + r.kind + '）']),
        '选择资源');
      resourceSelect.value = step.config.resourceId || '';
      resourceSelect.addEventListener('change', () => {
        step.config.resourceId = resourceSelect.value;
      });
      configBox.appendChild(fieldWrap(
        kind === 'task' ? '任务资源' : '服务资源', resourceSelect,
        '运行前会先校验配置健康与端口占用'));
    } else if (kind === 'agent') {
      const adapterSelect = selectNode(
        cachedV1.adapters.map(a => [a.id, a.name]),
        '选择适配器');
      adapterSelect.value = step.config.adapterId || '';
      adapterSelect.addEventListener('change', () => {
        step.config.adapterId = adapterSelect.value;
      });
      const promptInput = textInput('给 Agent 的提示词（可选）');
      promptInput.value = step.config.prompt || '';
      promptInput.addEventListener('input', () => {
        step.config.prompt = promptInput.value;
      });
      configBox.appendChild(fieldWrap('适配器', adapterSelect));
      configBox.appendChild(fieldWrap('提示词', promptInput));
    } else if (kind === 'gate') {
      const commandInput = textInput('如 python -m pytest -q');
      commandInput.value = step.config.command || '';
      commandInput.addEventListener('input', () => {
        step.config.command = commandInput.value;
      });
      configBox.appendChild(fieldWrap('验证命令', commandInput,
        '退出码 0 视为通过；失败会阻断下游必需步骤'));
    }
    configBox.appendChild(fieldWrap('依赖步骤', (() => {
      const box = el('div', 'needs-row needs-box');
      return box;
    })()));
  }

  function addStep() {
    const step = { id: 'step' + (steps.length + 1), kind: 'task',
      config: {}, needs: [] };
    steps.push(step);
    const row = el('div', 'wf-step-card');
    row._step = step;
    const head = el('div', 'wf-step-head');
    const kindSelect = selectNode([
      ['task', '任务 task'], ['service', '服务 service'],
      ['agent', 'Agent agent'], ['gate', '门禁 gate']], '步骤类型');
    kindSelect.value = step.kind;
    kindSelect.addEventListener('change', () => {
      step.kind = kindSelect.value;
      step.config = {};
      renderStepConfig(row);
    });
    const idInput = textInput('步骤 id（如 tests）');
    idInput.value = step.id;
    idInput.addEventListener('input', () => {
      step.id = idInput.value.trim() || step.id;
    });
    const remove = el('button', 'btn btn-sm ghost step-remove', '删除');
    remove.type = 'button';
    remove.addEventListener('click', () => {
      steps.splice(steps.indexOf(step), 1);
      stepRows.splice(stepRows.indexOf(row), 1);
      row.remove();
      rebuildNeeds();
    });
    setChildren(head, kindSelect, idInput, remove);
    const configBox = el('div', 'wf-step-config');
    row.append(head, configBox);
    stepRows.push(row);
    editor.appendChild(row);
    renderStepConfig(row);
    rebuildNeeds();
  }

  projectSelect.addEventListener('change', () => {
    for (const row of stepRows) renderStepConfig(row);
    // 拉取所选项目的最新资源（弹窗打开时 projectsDetail 可能尚未加载）
    fetchJson('/api/v1/projects').then(list => {
      cachedV1.projectsDetail = Array.isArray(list) ? list : [];
      for (const row of stepRows) renderStepConfig(row);
    }).catch(() => {});
  });

  const overlay = openModal('新建工作流', [
    fieldWrap('名称', nameInput),
    fieldWrap('项目', projectSelect),
    (() => {
      const wrap = el('div', 'field');
      const head = el('div', 'field-head');
      setChildren(head, el('label', '', '步骤（DAG）'));
      wrap.appendChild(head);
      const add = el('button', 'btn btn-sm wf-add-step',
        icon('plus', 12) + ' 添加步骤');
      add.type = 'button';
      add.addEventListener('click', addStep);
      wrap.append(editor, add);
      return wrap;
    })(),
  ], async () => {
    if (!nameInput.value.trim() || !projectSelect.value) {
      throw new Error('名称与项目必填');
    }
    if (!steps.length) throw new Error('请至少添加一个步骤');
    const payload = [];
    for (const step of steps) {
      const config = {};
      if (step.kind === 'service' || step.kind === 'task') {
        if (!step.config.resourceId) throw new Error('步骤 ' + step.id +
          '：请选择资源');
        config.resourceId = step.config.resourceId;
      } else if (step.kind === 'agent') {
        if (!step.config.adapterId) throw new Error('步骤 ' + step.id +
          '：请选择适配器');
        config.adapterId = step.config.adapterId;
        if (step.config.prompt) config.prompt = step.config.prompt;
      } else if (step.kind === 'gate') {
        if (!step.config.command || !step.config.command.trim()) {
          throw new Error('步骤 ' + step.id + '：请填写验证命令');
        }
        config.command = step.config.command.trim();
      }
      payload.push({
        id: step.id,
        kind: step.kind,
        config,
        needs: step.needs.slice(),
      });
    }
    await postJson('/api/v1/workflows', {
      name: nameInput.value.trim(),
      projectId: projectSelect.value,
      steps: payload,
    });
  });
  addStep();  // 预置一个步骤引导
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
  const deleteBtn = el('button', 'icon-btn sm danger', icon('trash-2', 13));
  deleteBtn.type = 'button';
  deleteBtn.title = '删除工作流';
  deleteBtn.setAttribute('aria-label', '删除工作流');
  deleteBtn.addEventListener('click', () => {
    confirmDelete('删除工作流', '工作流「' + wf.name +
      '」将被移除（运行历史保留）。运行中时会被拒绝。',
      async () => deleteJson('/api/v1/workflows/' + wf.id));
  });
  head.appendChild(deleteBtn);
  const body = el('div', 'wf-body');
  /* DAG 步骤图：按拓扑顺序横排步骤卡，依赖用箭头标注（P1 轻量可视化） */
  const steps = wf.steps || [];
  const dag = el('div', 'wf-dag');
  const stepById = {};
  for (const step of steps) stepById[step.id] = step;
  const ordered = steps.slice();
  for (const step of steps) {
    const needs = step.needs || [];
    if (!needs.length) continue;
    for (const need of needs) {
      const from = stepById[need];
      if (from) {
        ordered.splice(ordered.indexOf(step), 0, ordered.splice(
          ordered.indexOf(from), 1)[0]);
      }
    }
  }
  for (const step of ordered) {
    const node = el('span', 'dag-node ' + step.kind, escapeHtml(step.id));
    node.title = step.kind + (step.needs && step.needs.length
      ? ' ← ' + step.needs.join(',') : '');
    dag.appendChild(node);
    if (ordered.indexOf(step) < ordered.length - 1) {
      dag.appendChild(el('span', 'dag-arrow', icon('chevron-right', 12)));
    }
  }
  body.appendChild(dag);
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
  const importBtn = $('#projectsImportBtn');
  if (importBtn) importBtn.addEventListener('click', importManifest);
  const exportBtn = $('#projectsExportBtn');
  if (exportBtn) exportBtn.addEventListener('click', exportManifest);
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
