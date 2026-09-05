const $ = (selector) => document.querySelector(selector);
const authView = $('#auth-view');
const appView = $('#app-view');
const toast = $('#toast');
let authMode = 'login';
let currentUser = null;
let conversation = [];
let currentConversationId = null;
let userCaptchaId = '';
let resetCaptchaId = '';
let registrationCodeSent = false;
let modelsLoading = false;
let sessionNeedsOrganization = false;
let organizingSession = false;
let videoGalleryTimer = null;
let activeChatController = null;
let activeDirectorProject = null;
let directorProjectTimer = null;
let directorOneClickMode = false;
let editingDirectorDraft = null;
let creativePreferences = {};
let selectedDirectorStage = null;
let activeStudioView = window.localStorage.getItem('assistant-studio-view') === 'library' ? 'library' : 'production';
let videoLibraryFilter = 'all';
let selectedAttachments = [];
let attachmentUploads = 0;
const customModelValue = '__custom__';
const resetToken = new URLSearchParams(window.location.hash.slice(1)).get('reset_token');
const initialWelcomeMarkup = $('#messages').innerHTML;

async function api(path, options = {}) {
  const headers = options.body instanceof FormData
    ? { ...(options.headers || {}) }
    : { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const response = await fetch(`/api/v1${path}`, {
    headers,
    ...options,
  });
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail?.message || body.detail || `请求失败（${response.status}）`);
    error.status = response.status;
    error.detail = body.detail;
    throw error;
  }
  return body;
}

function notify(message) {
  toast.textContent = message;
  toast.classList.remove('hidden');
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => toast.classList.add('hidden'), 2800);
}

function setAuthMode(mode) {
  authMode = mode;
  registrationCodeSent = false;
  $('#standard-auth').classList.remove('hidden');
  $('#reset-request-view').classList.add('hidden');
  $('#reset-confirm-view').classList.add('hidden');
  $('#login-tab').classList.toggle('active', mode === 'login');
  $('#register-tab').classList.toggle('active', mode === 'register');
  $('#auth-title').textContent = mode === 'login' ? '进入视频工作室' : '创建你的创作账号';
  $('#auth-subtitle').textContent = mode === 'login' ? '继续你的故事和作品' : '每天仅开放 3 个新用户名额';
  $('#auth-submit').textContent = mode === 'login' ? '进入知伴' : '注册并进入';
  $('#password').autocomplete = mode === 'login' ? 'current-password' : 'new-password';
  $('#register-tip').classList.toggle('hidden', mode !== 'register');
  $('#email-code-group').classList.toggle('hidden', mode !== 'register');
  $('#email-code').required = mode === 'register';
  $('#forgot-password').classList.toggle('hidden', mode !== 'login');
  $('#auth-captcha-group').classList.remove('hidden');
  $('#captcha-answer').required = mode === 'login';
  loadCaptcha('user');
}

async function loadCaptcha(kind) {
  const question = kind === 'user' ? $('#captcha-question') : $('#reset-captcha-question');
  const answer = kind === 'user' ? $('#captcha-answer') : $('#reset-captcha-answer');
  question.textContent = '刷新中…';
  answer.value = '';
  try {
    const result = await api('/auth/captcha');
    if (kind === 'user') userCaptchaId = result.captcha_id;
    else resetCaptchaId = result.captcha_id;
    question.textContent = result.question;
  } catch (error) {
    question.textContent = '点击重试';
    notify(error.message);
  }
}

function showResetRequest() {
  $('#standard-auth').classList.add('hidden');
  $('#reset-confirm-view').classList.add('hidden');
  $('#reset-request-view').classList.remove('hidden');
  $('#reset-email').value = $('#email').value;
  loadCaptcha('reset');
}

function showStandardLogin() {
  window.history.replaceState({}, '', window.location.pathname);
  setAuthMode('login');
}

function showApp(user) {
  currentUser = user;
  authView.classList.add('hidden');
  appView.classList.remove('hidden');
  updateUser(user);
  loadModels();
  loadAgentRouting();
  loadCapabilities();
  loadConversations();
  loadVideoGallery();
  loadDirectorProjects();
  loadCreativePreferences();
  try {
    const saved = JSON.parse(sessionStorage.getItem(`assistant-chat-pending:${user.id}`));
    if (saved?.body && saved?.key) {
      const message = addMessage('assistant', '上次请求的结果尚未确认，可以重新获取。');
      addChatRetry(message, saved);
    }
  } catch (_) { /* invalid local retry record */ }
}

async function loadCapabilities() {
  const badge = $('#skill-status');
  badge.className = 'skill-status checking';
  badge.textContent = '文档 Skill 检查中…';
  try {
    const result = await api('/chat/capabilities');
    const skill = (result.skills || []).find((item) => item.id === 'document-understanding');
    badge.className = `skill-status${skill?.ready ? '' : ' unavailable'}`;
    badge.textContent = skill?.ready ? '文档理解 Skill · MCP 就绪' : '文档 MCP 暂不可用';
  } catch (error) {
    badge.className = 'skill-status unavailable';
    badge.textContent = '能力状态获取失败';
  }
}

async function loadConversations() {
  try {
    const items = await api('/chat/conversations');
    const list = $('#history-list');
    list.innerHTML = '';
    if (!items.length) {
      list.innerHTML = '<button class="history-item active" type="button"><span>✦</span><span>新的对话</span></button>';
      return;
    }
    for (const item of items) {
      const row = document.createElement('div');
      row.className = 'history-row';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `history-item ${item.id === currentConversationId ? 'active' : ''}`;
      button.title = item.title;
      button.innerHTML = `<span aria-hidden="true">✦</span><span class="history-title">${escapeHtml(item.title)}</span>`;
      button.addEventListener('click', async () => {
        if (sessionNeedsOrganization) await organizeCurrentConversation();
        await openConversation(item.id);
      });

      const deleteButton = document.createElement('button');
      deleteButton.type = 'button';
      deleteButton.className = 'history-delete';
      deleteButton.setAttribute('aria-label', `删除对话：${item.title}`);
      deleteButton.title = '删除对话';
      deleteButton.innerHTML = '<span aria-hidden="true">×</span>';
      deleteButton.addEventListener('click', async (event) => {
        event.stopPropagation();
        await deleteConversation(item, deleteButton);
      });

      row.append(button, deleteButton);
      list.appendChild(row);
    }
  } catch (error) {
    notify(error.message);
  }
}

async function deleteConversation(item, button) {
  const confirmed = window.confirm(
    `确定删除“${item.title}”吗？\n\n删除后无法恢复；已整理进个人知识库的长期记忆不会受影响，尚未整理的内容将不再保留。`,
  );
  if (!confirmed) return;

  button.disabled = true;
  try {
    await api(`/chat/conversations/${item.id}`, { method: 'DELETE' });
    if (item.id === currentConversationId) resetConversationView(false);
    await loadConversations();
    notify('对话已删除，个人知识库中的长期记忆已保留');
  } catch (error) {
    button.disabled = false;
    notify(error.message);
  }
}

async function openConversation(conversationId) {
  try {
    const result = await api(`/chat/conversations/${conversationId}`);
    currentConversationId = conversationId;
    conversation = result.messages.map((item) => ({ role: item.role, content: item.content }));
    $('#messages').innerHTML = '';
    for (const item of result.messages) {
      const meta = item.role === 'assistant' && item.model
        ? `${item.channel || '模型渠道'} · ${item.model}`
        : '';
      const message = addMessage(item.role, item.content, meta);
      renderArtifacts(message, item.artifacts || {});
    }
    for (const run of result.runs || []) {
      const message = addMessage('assistant', run.error || '该请求正在处理，请稍后刷新会话');
      renderArtifacts(message, run.artifacts || {});
    }
    sessionNeedsOrganization = false;
    $('#organize-session').disabled = false;
    $('#session-memory-status').textContent = '创意会话已载入，可继续讨论或整理创作偏好';
    setSidebarOpen(false);
    await loadConversations();
  } catch (error) {
    notify(error.message);
  }
}

async function loadModels() {
  if (modelsLoading) return;
  modelsLoading = true;
  const select = $('#model-select');
  const custom = $('#model-custom');
  const refresh = $('#refresh-models');
  const selected = select.value === customModelValue ? custom.value.trim() : select.value;
  const previous = selected || window.localStorage.getItem('assistant-model') || '';
  select.disabled = true;
  custom.disabled = true;
  refresh.disabled = true;
  try {
    const models = await api('/chat/models');
    select.replaceChildren(new Option('请选择大模型', ''));
    for (const model of models) {
      select.add(new Option(model.model, model.model));
    }
    select.add(new Option('自定义模型 ID…', customModelValue));
    if (models.some((model) => model.model === previous)) {
      select.value = previous;
      custom.value = '';
    } else if (previous) {
      select.value = customModelValue;
      custom.value = previous;
    } else {
      select.value = '';
      custom.value = '';
    }
  } catch (error) {
    select.replaceChildren(
      new Option('模型列表加载失败', ''),
      new Option('自定义模型 ID…', customModelValue),
    );
    if (previous) {
      select.value = customModelValue;
      custom.value = previous;
    }
    notify(`${error.message}，仍可选择“自定义模型 ID”`);
  } finally {
    modelsLoading = false;
    select.disabled = false;
    custom.disabled = false;
    custom.classList.toggle('hidden', select.value !== customModelValue);
    $('#send-btn').disabled = false;
    refresh.disabled = false;
  }
}

async function loadAgentRouting() {
  try {
    const routing = await api('/chat/agent-model-routing');
    for (const assignment of routing.assignments || []) {
      const target = document.querySelector(`[data-agent-model="${assignment.agent}"]`);
      if (!target) continue;
      target.textContent = assignment.model || '暂无可用模型';
      const statusLabel = assignment.status === 'matched'
        ? '能力匹配'
        : assignment.status === 'tool' ? '工具执行' : '降级匹配';
      target.title = `${assignment.reason} · ${statusLabel}`;
      target.dataset.modelStatus = assignment.status;
    }
  } catch (error) {
    document.querySelectorAll('[data-agent-model]').forEach((target) => {
      target.textContent = '等待可用模型';
      target.title = error.message;
      target.dataset.modelStatus = 'unavailable';
    });
  }
}

function updateUser(user) {
  currentUser = { ...currentUser, ...user };
  $('#profile-email').textContent = currentUser.email;
  $('#profile-btn').textContent = currentUser.email.charAt(0).toUpperCase();
}

async function loadSession() {
  try {
    const user = await api('/auth/session');
    showApp(user);
  } catch {
    authView.classList.remove('hidden');
    appView.classList.add('hidden');
  }
}

$('#login-tab').addEventListener('click', () => setAuthMode('login'));
$('#register-tab').addEventListener('click', () => setAuthMode('register'));
$('#captcha-question').addEventListener('click', () => loadCaptcha('user'));
$('#reset-captcha-question').addEventListener('click', () => loadCaptcha('reset'));
$('#forgot-password').addEventListener('click', showResetRequest);
$('#refresh-models').addEventListener('click', loadModels);
$('#model-select').addEventListener('change', (event) => {
  const custom = $('#model-custom');
  const isCustom = event.target.value === customModelValue;
  custom.classList.toggle('hidden', !isCustom);
  if (isCustom) {
    custom.focus();
  } else if (event.target.value) {
    window.localStorage.setItem('assistant-model', event.target.value);
  }
});
$('#model-custom').addEventListener('change', (event) => {
  const value = event.target.value.trim();
  if (value) window.localStorage.setItem('assistant-model', value);
});
document.querySelectorAll('[data-back-login]').forEach((button) => button.addEventListener('click', showStandardLogin));

$('#send-email-code').addEventListener('click', async () => {
  if (!$('#email').reportValidity()) return;
  if (!$('#captcha-answer').value.trim()) {
    notify('请先输入图形验证码');
    return;
  }
  const button = $('#send-email-code');
  button.disabled = true;
  try {
    const result = await api('/auth/register/email-code', {
      method: 'POST',
      body: JSON.stringify({
        email: $('#email').value,
        captcha_id: userCaptchaId,
        captcha_answer: $('#captcha-answer').value,
      }),
    });
    registrationCodeSent = true;
    $('#auth-captcha-group').classList.add('hidden');
    $('#captcha-answer').required = false;
    notify(result.message);
    let seconds = 60;
    button.textContent = `${seconds} 秒后重发`;
    const timer = window.setInterval(() => {
      seconds -= 1;
      button.textContent = seconds > 0 ? `${seconds} 秒后重发` : '重新发送';
      if (seconds <= 0) {
        window.clearInterval(timer);
        button.disabled = false;
        registrationCodeSent = false;
        $('#auth-captcha-group').classList.remove('hidden');
        $('#captcha-answer').required = false;
        loadCaptcha('user');
      }
    }, 1000);
  } catch (error) {
    button.disabled = false;
    notify(error.message);
    loadCaptcha('user');
  }
});

$('#auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#auth-submit');
  button.disabled = true;
  try {
    const payload = { email: $('#email').value, password: $('#password').value };
    if (authMode === 'register') payload.email_code = $('#email-code').value;
    else {
      payload.captcha_id = userCaptchaId;
      payload.captcha_answer = $('#captcha-answer').value;
    }
    const user = await api(`/auth/${authMode}`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    showApp(user);
    notify(authMode === 'register' ? '注册成功，欢迎加入' : '登录成功');
  } catch (error) {
    notify(error.message);
    if (authMode === 'login') loadCaptcha('user');
  } finally {
    button.disabled = false;
  }
});

$('#reset-request-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await api('/auth/password-reset/request', {
      method: 'POST',
      body: JSON.stringify({
        email: $('#reset-email').value,
        captcha_id: resetCaptchaId,
        captcha_answer: $('#reset-captcha-answer').value,
      }),
    });
    notify(result.message);
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
    loadCaptcha('reset');
  }
});

$('#reset-confirm-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const password = $('#reset-password').value;
  if (password !== $('#reset-password-confirm').value) {
    notify('两次输入的密码不一致');
    return;
  }
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await api('/auth/password-reset/confirm', {
      method: 'POST',
      body: JSON.stringify({ token: resetToken, password }),
    });
    notify(result.message);
    showStandardLogin();
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
  }
});

$('#profile-btn').addEventListener('click', () => $('#profile-menu').classList.toggle('hidden'));
$('#logout-btn').addEventListener('click', async () => {
  await api('/auth/logout', { method: 'POST' }).catch(() => null);
  currentUser = null;
  appView.classList.add('hidden');
  authView.classList.remove('hidden');
  $('#profile-menu').classList.add('hidden');
});

function renderMemoryGraph(graph) {
  const host = $('#memory-graph');
  host.innerHTML = '';
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  if (!nodes.length) {
    host.innerHTML = '<p class="muted">还没有形成知识图谱，多聊几次后这里会出现与你有关的实体和关系。</p>';
    return;
  }
  const width = 760;
  const height = 500;
  const centerX = width / 2;
  const centerY = height / 2;
  const radius = Math.min(width, height) * 0.36;
  const positions = new Map(nodes.map((node, index) => [node.id, {
    x: centerX + radius * Math.cos((Math.PI * 2 * index) / nodes.length - Math.PI / 2),
    y: centerY + radius * Math.sin((Math.PI * 2 * index) / nodes.length - Math.PI / 2),
  }]));
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', '用户知识图谱');
  for (const edge of edges) {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) continue;
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', source.x); line.setAttribute('y1', source.y);
    line.setAttribute('x2', target.x); line.setAttribute('y2', target.y);
    line.setAttribute('class', 'graph-edge');
    svg.appendChild(line);
    const label = document.createElementNS(ns, 'text');
    label.setAttribute('x', (source.x + target.x) / 2);
    label.setAttribute('y', (source.y + target.y) / 2 - 5);
    label.setAttribute('class', 'graph-edge-label');
    label.textContent = edge.label;
    svg.appendChild(label);
  }
  for (const node of nodes) {
    const point = positions.get(node.id);
    const circle = document.createElementNS(ns, 'circle');
    circle.setAttribute('cx', point.x); circle.setAttribute('cy', point.y);
    circle.setAttribute('r', 28); circle.setAttribute('class', 'graph-node');
    svg.appendChild(circle);
    const label = document.createElementNS(ns, 'text');
    label.setAttribute('x', point.x); label.setAttribute('y', point.y + 44);
    label.setAttribute('class', 'graph-node-label');
    label.textContent = node.label.length > 12 ? `${node.label.slice(0, 12)}…` : node.label;
    svg.appendChild(label);
    const type = document.createElementNS(ns, 'text');
    type.setAttribute('x', point.x); type.setAttribute('y', point.y + 4);
    type.setAttribute('class', 'graph-node-type');
    type.textContent = node.type.slice(0, 8);
    svg.appendChild(type);
  }
  host.appendChild(svg);
}

async function showMemory() {
  const dialog = $('#memory-dialog');
  dialog.showModal();
  $('#memory-graph').innerHTML = '<p class="muted">正在加载图谱…</p>';
  $('#memory-items').innerHTML = '<p class="muted">正在加载记忆…</p>';
  try {
    const [graph, memories, stats] = await Promise.all([
      api('/memory/graph'),
      api('/memory/items'),
      api('/memory/stats'),
    ]);
    $('#vector-memory-count').textContent = stats.embeddings.toLocaleString('zh-CN');
    $('#graph-node-count').textContent = stats.graph_nodes.toLocaleString('zh-CN');
    $('#graph-edge-count').textContent = stats.graph_edges.toLocaleString('zh-CN');
    renderMemoryGraph(graph);
    $('#memory-items').innerHTML = memories.map((item) => `
      <article class="memory-item" data-memory-id="${item.id}">
        <div><span>${escapeHtml(item.type)}</span><p>${escapeHtml(item.content)}</p></div>
        <button type="button" title="删除这条记忆">删除</button>
      </article>`).join('') || '<p class="muted">暂时没有长期记忆。</p>';
    document.querySelectorAll('[data-memory-id] button').forEach((button) => button.addEventListener('click', async () => {
      const item = button.closest('[data-memory-id]');
      button.disabled = true;
      try {
        await api(`/memory/items/${item.dataset.memoryId}`, { method: 'DELETE' });
        item.remove();
        notify('记忆已删除，后续回答不会再使用');
      } catch (error) {
        button.disabled = false;
        notify(error.message);
      }
    }));
  } catch (error) {
    $('#memory-graph').innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    $('#memory-items').innerHTML = '';
  }
}

$('#memory-btn').addEventListener('click', showMemory);
$('#mobile-memory-btn').addEventListener('click', showMemory);
$('#memory-close').addEventListener('click', () => $('#memory-dialog').close());

function escapeHtml(value) {
  const node = document.createElement('div');
  node.textContent = value;
  return node.innerHTML;
}

function addMessage(role, content, meta = '') {
  $('.welcome-block')?.remove();
  const wrapper = document.createElement('div');
  wrapper.className = `message ${role}`;
  wrapper.innerHTML = `${role === 'assistant' ? '<div class="message-avatar">知</div>' : ''}
    <div class="message-bubble">${escapeHtml(content)}${meta ? `<div class="message-meta">${escapeHtml(meta)}</div>` : ''}</div>
    ${role === 'user' ? '<div class="message-avatar">我</div>' : ''}`;
  $('#messages').appendChild(wrapper);
  $('#messages').scrollTop = $('#messages').scrollHeight;
  return wrapper;
}

function startChatProgress(message, model) {
  const bubble = message.querySelector('.message-bubble');
  const startedAt = Date.now();
  const phases = [
    [0, '正在理解需求并选择执行方式'],
    [4, '模型正在生成回复或准备工具调用'],
    [12, '复杂任务仍在执行，请稍候'],
  ];
  let timer = null;
  const render = () => {
    const elapsed = Math.floor((Date.now() - startedAt) / 1000);
    const phase = [...phases].reverse().find(([after]) => elapsed >= after)?.[1];
    bubble.innerHTML = `<div class="chat-progress" role="status" aria-live="polite"><span class="chat-progress-mark">✦</span><div><strong>${escapeHtml(phase)}</strong><small>请求已提交给 ${escapeHtml(model)} · ${elapsed} 秒</small><p>完成后会展示实际工具调用、Agent 阶段、生成进度与失败原因；不展示模型的私有思维链。</p></div></div>`;
  };
  render();
  timer = window.setInterval(render, 1000);
  return () => window.clearInterval(timer);
}

function renderArtifacts(message, result) {
  const files = result.files || [];
  const documents = result.documents || [];
  const jobs = result.video_jobs || [];
  const musicJobs = result.music_jobs || [];
  const speechJobs = result.speech_jobs || [];
  const directorProjects = result.director_projects || [];
  const webSources = result.web_sources || [];
  if (!files.length && !documents.length && !jobs.length && !musicJobs.length && !speechJobs.length && !directorProjects.length && !webSources.length) return;
  const list = document.createElement('div');
  list.className = 'artifact-list';
  for (const [index, source] of webSources.entries()) {
    const card = document.createElement('div');
    card.className = 'artifact-card web-source-card';
    const sourceLabel = source.source || (() => { try { return new URL(source.url).hostname; } catch { return '公开网页'; } })();
    const date = source.date ? ` · ${source.date}` : '';
    card.innerHTML = `<div><strong>[${index + 1}] ${escapeHtml(source.title || source.url)}</strong><small>${escapeHtml(sourceLabel)}${escapeHtml(date)}</small></div><a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>`;
    list.appendChild(card);
  }
  for (const file of files) {
    const card = document.createElement('div');
    card.className = 'artifact-card';
    card.innerHTML = `<div><strong>▤ ${escapeHtml(file.filename)}</strong><small>${Math.max(1, Math.ceil(file.size_bytes / 1024))} KB</small></div><a href="${file.download_url}">下载文件</a>`;
    list.appendChild(card);
  }
  for (const attachment of documents) {
    const card = document.createElement('div');
    card.className = 'artifact-card';
    card.innerHTML = `<div><strong>📎 ${escapeHtml(attachment.filename)}</strong><small>文档理解 Skill · MarkItDown MCP 已读取</small></div><a href="${attachment.download_url}">查看附件</a>`;
    list.appendChild(card);
  }
  for (const job of jobs) {
    const card = document.createElement('div');
    card.className = 'artifact-card';
    card.innerHTML = `<div><strong>▶ 视频生成任务</strong><small>排队中 · ${escapeHtml(job.seconds)} 秒 · ${escapeHtml(job.size)} · ${escapeHtml(job.resolution || '768P')}</small></div><a href="#">刷新状态</a>`;
    card.querySelector('a').addEventListener('click', (event) => { event.preventDefault(); pollVideoJob(job.id, card); });
    list.appendChild(card);
    window.setTimeout(() => pollVideoJob(job.id, card), 5000);
  }
  for (const job of musicJobs) {
    const card = document.createElement('div');
    card.className = 'artifact-card';
    card.innerHTML = `<div><strong>♫ 音乐生成任务</strong><small>排队中 · ${job.is_instrumental ? '纯配乐' : '带人声歌曲'}</small></div><a href="#">刷新状态</a>`;
    card.querySelector('a').addEventListener('click', (event) => { event.preventDefault(); pollMusicJob(job.id, card); });
    list.appendChild(card);
    window.setTimeout(() => pollMusicJob(job.id, card), 5000);
  }
  for (const job of speechJobs) {
    const card = document.createElement('div');
    card.className = 'artifact-card';
    card.innerHTML = `<div><strong>◉ 语音配音任务</strong><small>排队中 · ${escapeHtml(job.voice_id)}</small></div><a href="#">刷新状态</a>`;
    card.querySelector('a').addEventListener('click', (event) => { event.preventDefault(); pollSpeechJob(job.id, card); });
    list.appendChild(card);
    window.setTimeout(() => pollSpeechJob(job.id, card), 3000);
  }
  for (const project of directorProjects) {
    const card = document.createElement('div');
    card.className = 'artifact-card director-artifact';
    card.innerHTML = '<div><strong data-director-card-title>🎬 导演项目已启动</strong><small data-director-card-status>正在读取真实执行状态…</small><small data-director-card-detail></small></div><a href="#">打开工作室</a>';
    card.querySelector('a').addEventListener('click', (event) => {
      event.preventDefault();
      switchWorkspace('studio');
      switchStudioView('production');
      loadDirectorProject(project.id);
    });
    updateDirectorArtifactCard(project, card);
    list.appendChild(card);
    if (['queued', 'processing'].includes(project.status)) {
      window.setTimeout(() => pollDirectorArtifact(project.id, card), 2000);
    }
  }
  message.querySelector('.message-bubble').appendChild(list);
}

function directorStageText(project) {
  if (project.status === 'awaiting_storyboard') return '等待分镜确认 · 尚未提交视频';
  if (project.status === 'awaiting_confirmation') return '等待你确认故事 · 视频调用 0 次';
  const active = project.agents?.find((run) => run.agent === project.current_stage);
  if (active) return `${active.agent_name} · ${directorAgentStatusLabel(active.status)}`;
  if (project.status === 'completed') return project.one_click ? '完整成片已交付' : '首镜预览已交付';
  if (project.status === 'failed') {
    const failed = project.agents?.find((run) => run.status === 'failed');
    return failed ? `${failed.agent_name} · 需要处理` : '任务需要处理';
  }
  return '总导演正在派单';
}

function updateDirectorArtifactCard(project, card) {
  const title = card.querySelector('[data-director-card-title]');
  const status = card.querySelector('[data-director-card-status]');
  const detail = card.querySelector('[data-director-card-detail]');
  const shotProgress = project.planned_shots
    ? ` · ${project.completed_shots}/${project.planned_shots} 镜`
    : '';
  title.textContent = project.status === 'awaiting_confirmation'
    ? '✋ 故事等待确认'
    : project.status === 'failed'
    ? '⚠ 导演项目需要处理'
    : project.status === 'completed'
      ? (project.one_click ? '🎬 完整成片已交付' : '🎬 首镜预览已完成')
      : '🎬 导演团队正在制作';
  status.textContent = `${project.progress}% · ${directorStageText(project)}${shotProgress}`;
  detail.textContent = project.status === 'awaiting_confirmation'
    ? '打开导演工作室核对故事；确认前不会启动 Agent 或调用视频模型'
    : project.error_message
    || `${project.aspect_ratio} · ${project.resolution || '768P'} · ${project.one_click ? `完整 ${project.target_seconds} 秒成片` : '4 秒首镜预览'}`;
  card.classList.toggle('failed', project.status === 'failed');
}

async function pollDirectorArtifact(projectId, card) {
  if (!card.isConnected) return;
  try {
    const project = await api(`/director/projects/${projectId}`);
    updateDirectorArtifactCard(project, card);
    if (['queued', 'processing'].includes(project.status)) {
      window.setTimeout(() => pollDirectorArtifact(projectId, card), 3500);
    }
  } catch (error) {
    card.classList.add('failed');
    card.querySelector('[data-director-card-detail]').textContent = error.message;
  }
}

async function pollSpeechJob(jobId, card) {
  if (!card.isConnected) return;
  try {
    const job = await api(`/speech/${jobId}`);
    const statusText = { queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[job.status] || job.status;
    const duration = job.duration_ms ? ` · ${(job.duration_ms / 1000).toFixed(1)} 秒` : '';
    card.querySelector('small').textContent = `${statusText} · ${job.voice_id}${duration}`;
    if (job.status === 'completed') {
      const download = document.createElement('a');
      download.textContent = '下载语音';
      download.href = job.download_url;
      card.querySelector('a')?.replaceWith(download);
      return;
    }
    if (job.status === 'failed') {
      card.classList.add('failed');
      card.querySelector('small').textContent = job.error_message || '语音渠道返回失败';
      card.querySelector('a').remove();
      return;
    }
    window.setTimeout(() => pollSpeechJob(jobId, card), 3000);
  } catch (error) {
    card.querySelector('small').textContent = error.message;
  }
}

async function pollMusicJob(jobId, card) {
  if (!card.isConnected) return;
  try {
    const job = await api(`/music/${jobId}`);
    const statusText = { queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[job.status] || job.status;
    const duration = job.duration_ms ? ` · ${Math.round(job.duration_ms / 1000)} 秒` : '';
    card.querySelector('small').textContent = `${statusText} · ${job.is_instrumental ? '纯配乐' : '带人声歌曲'}${duration}`;
    if (job.status === 'completed') {
      const download = document.createElement('a');
      download.textContent = '下载音乐';
      download.href = job.download_url;
      card.querySelector('a')?.replaceWith(download);
      return;
    }
    if (job.status === 'failed') {
      card.classList.add('failed');
      card.querySelector('small').textContent = job.error_message || '音乐渠道返回失败';
      card.querySelector('a').remove();
      return;
    }
    window.setTimeout(() => pollMusicJob(jobId, card), 5000);
  } catch (error) {
    card.querySelector('small').textContent = error.message;
  }
}

async function pollVideoJob(jobId, card) {
  if (!card.isConnected) return;
  try {
    const job = await api(`/videos/${jobId}`);
    if (job.status === 'awaiting_confirmation') {
      card.replaceChildren();
      const detail = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = '视频草稿 · 等待确认';
      const parameters = document.createElement('small');
      parameters.textContent = `${job.seconds} 秒 · ${job.size} · ${job.resolution}`;
      const prompt = document.createElement('p');
      prompt.textContent = job.prompt;
      detail.append(title, parameters, prompt);
      card.append(detail, videoConfirmButton(job, () => {
        card.innerHTML = '<div><strong>视频生成任务</strong><small>排队中</small></div><a href="#">刷新状态</a>';
        pollVideoJob(jobId, card);
      }));
      return;
    }
    const statusText = { queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[job.status] || job.status;
    card.querySelector('small').textContent = `${statusText} · ${job.seconds} 秒 · ${job.size} · ${job.resolution || '768P'}`;
    if (job.status === 'completed') {
      const download = document.createElement('a');
      download.textContent = '下载视频';
      download.href = job.download_url;
      card.querySelector('a')?.replaceWith(download);
      return;
    }
    if (job.status === 'failed') {
      card.classList.add('failed');
      card.querySelector('small').textContent = job.error_message || '视频渠道返回失败';
      card.querySelector('a').remove();
      return;
    }
    window.setTimeout(() => pollVideoJob(jobId, card), 5000);
  } catch (error) {
    card.querySelector('small').textContent = error.message;
  }
}

function videoConfirmButton(job, onConfirmed) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = '确认以上提示词和参数，开始生成';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await api(`/videos/${job.id}/confirm`, {
        method: 'POST', body: JSON.stringify({ draft_hash: job.draft_hash }),
      });
      onConfirmed();
    } catch (error) {
      notify(error.message);
      button.disabled = false;
    }
  });
  return button;
}

async function sendMessage(text) {
  if (activeChatController) return;
  const content = text.trim();
  if (!content) return;
  const selection = $('#model-select').value;
  const selectedModel = selection === customModelValue
    ? $('#model-custom').value.trim()
    : selection;
  if (!selectedModel) {
    notify('请选择或输入模型 ID');
    return;
  }
  window.localStorage.setItem('assistant-model', selectedModel);
  const attachmentsForMessage = [...selectedAttachments];
  const attachmentMeta = attachmentsForMessage.length
    ? `附件：${attachmentsForMessage.map((item) => item.filename).join('、')}`
    : '';
  addMessage('user', content, attachmentMeta);
  conversation.push({ role: 'user', content });
  $('#chat-input').value = '';
  const requestBody = JSON.stringify({
    model: selectedModel, message: content, conversation_id: currentConversationId,
    file_ids: attachmentsForMessage.map((item) => item.id),
  });
  const storageKey = `assistant-chat-pending:${currentUser.id}`;
  let previousRequest = null;
  try { previousRequest = JSON.parse(sessionStorage.getItem(storageKey)); } catch (_) { /* invalid cache */ }
  const requestKey = previousRequest?.body === requestBody ? previousRequest.key : crypto.randomUUID();
  sessionStorage.setItem(storageKey, JSON.stringify({ body: requestBody, key: requestKey }));
  const controller = new AbortController();
  activeChatController = controller;
  setChatGenerating(true);
  const pending = addMessage('assistant', '正在处理请求…');
  const stopChatProgress = startChatProgress(pending, selectedModel);
  try {
    const result = await api('/chat', {
      method: 'POST',
      body: requestBody,
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestKey },
      signal: controller.signal,
    });
    sessionStorage.removeItem(storageKey);
    pending.remove();
    const webMeta = result.web_sources?.length ? ` · 联网检索 ${result.web_sources.length} 个来源` : '';
    const documentMeta = result.documents?.length ? ` · 文档 Skill/MCP ${result.documents.length} 个附件` : '';
    const responseMessage = addMessage('assistant', result.content, `${result.channel} · ${result.model}${webMeta}${documentMeta}`);
    renderArtifacts(responseMessage, result);
    conversation.push({ role: 'assistant', content: result.content });
    currentConversationId = result.conversation_id;
    if (attachmentsForMessage.length) {
      selectedAttachments = selectedAttachments.filter(
        (item) => !attachmentsForMessage.some((used) => used.id === item.id),
      );
      renderAttachmentTray();
    }
    sessionNeedsOrganization = true;
    $('#organize-session').disabled = false;
    const usedMemories = Number(result.memory?.items_used || 0);
    const usedRelations = Number(result.memory?.graph_edges_used || 0);
    $('#session-memory-status').textContent = usedMemories || usedRelations
      ? `本轮调用了 ${usedMemories} 条记忆、${usedRelations} 条关系；结束后将归纳新素材`
      : '本轮已记录；结束会话时会整理长期创作偏好';
    loadConversations();
  } catch (error) {
    if (error.detail?.status === 'failed') {
      sessionStorage.removeItem(storageKey);
      if (error.detail.conversation_id) currentConversationId = error.detail.conversation_id;
    }
    if (error.name === 'AbortError') {
      pending.querySelector('.message-bubble').textContent = '已停止生成。你的提问仍保存在最近对话中，可以稍后重新发送。';
      sessionNeedsOrganization = true;
      $('#session-memory-status').textContent = '本轮回复已停止；已发送的内容仍会保存在会话中';
      loadConversations();
      notify('已停止当前回复');
    } else {
      pending.remove();
      const failure = addMessage('assistant', `暂时无法回答：${error.message}`);
      renderArtifacts(failure, error.detail?.artifacts || {});
      if (error.detail?.status !== 'failed') {
        addChatRetry(failure, { body: requestBody, key: requestKey });
      }
    }
  } finally {
    stopChatProgress();
    if (activeChatController === controller) activeChatController = null;
    setChatGenerating(false);
    $('#chat-input').focus();
  }
}

function addChatRetry(message, saved) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = '重新获取此请求的结果';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const result = await api('/chat', {
        method: 'POST', body: saved.body,
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': saved.key },
      });
      sessionStorage.removeItem(`assistant-chat-pending:${currentUser.id}`);
      await openConversation(result.conversation_id);
    } catch (error) {
      notify(error.message);
      if (error.detail?.status === 'failed') {
        sessionStorage.removeItem(`assistant-chat-pending:${currentUser.id}`);
        if (error.detail.conversation_id) await openConversation(error.detail.conversation_id);
        button.remove();
      } else {
        button.disabled = false;
      }
    }
  });
  message.querySelector('.message-bubble').appendChild(button);
}

function formatFileSize(bytes) {
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1000000) return `${Math.ceil(bytes / 1000)} KB`;
  return `${(bytes / 1000000).toFixed(1)} MB`;
}

function renderAttachmentTray() {
  const tray = $('#attachment-tray');
  tray.classList.toggle('hidden', !selectedAttachments.length);
  tray.innerHTML = selectedAttachments.map((item) => `
    <div class="attachment-chip" data-file-id="${item.id}">
      <span>📎 ${escapeHtml(item.filename)}</span>
      <small>${formatFileSize(item.size_bytes)}</small>
      <button type="button" aria-label="移除附件">×</button>
    </div>`).join('');
  tray.querySelectorAll('.attachment-chip button').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.closest('.attachment-chip').dataset.fileId;
      selectedAttachments = selectedAttachments.filter((item) => item.id !== id);
      renderAttachmentTray();
    });
  });
}

async function uploadAttachment(file) {
  if (selectedAttachments.length + attachmentUploads >= 4) {
    notify('每次最多使用 4 个附件');
    return;
  }
  const form = new FormData();
  form.append('upload', file);
  attachmentUploads += 1;
  $('#attach-btn').classList.add('uploading');
  try {
    const uploaded = await api('/files/upload', { method: 'POST', body: form });
    selectedAttachments.push(uploaded);
    renderAttachmentTray();
    notify(`附件已上传：${uploaded.filename}`);
  } catch (error) {
    notify(error.message);
  } finally {
    attachmentUploads -= 1;
    $('#attach-btn').classList.toggle('uploading', attachmentUploads > 0);
  }
}

$('#attach-btn').addEventListener('click', () => $('#attachment-input').click());
$('#attachment-input').addEventListener('change', async (event) => {
  const files = [...event.target.files];
  event.target.value = '';
  for (const file of files) await uploadAttachment(file);
});

function setChatGenerating(generating) {
  const button = $('#send-btn');
  button.classList.toggle('stopping', generating);
  button.textContent = generating ? '■' : '↑';
  button.setAttribute('aria-label', generating ? '停止生成' : '发送');
  button.title = generating ? '停止生成' : '发送';
}

function stopActiveChat() {
  if (!activeChatController) return;
  activeChatController.abort();
}

$('#chat-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (activeChatController) {
    stopActiveChat();
    return;
  }
  sendMessage($('#chat-input').value);
});
$('#chat-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    if (activeChatController) return;
    $('#chat-form').requestSubmit();
  }
});
$('#chat-input').addEventListener('input', (event) => {
  event.target.style.height = 'auto';
  event.target.style.height = `${Math.min(event.target.scrollHeight, 150)}px`;
});
function bindPromptButtons() {
  document.querySelectorAll('[data-prompt]').forEach((button) => {
    button.addEventListener('click', () => sendMessage(button.dataset.prompt));
  });
}

async function organizeCurrentConversation(force = false) {
  if (!currentConversationId || organizingSession || (!sessionNeedsOrganization && !force)) return null;
  organizingSession = true;
  const button = $('#organize-session');
  button.disabled = true;
  button.textContent = '正在整理…';
  $('#session-memory-status').textContent = '正在整理长期创作偏好，虚构人物与剧情不会作为个人经历保存…';
  try {
    const result = await api(`/chat/conversations/${currentConversationId}/organize`, {
      method: 'POST',
    });
    sessionNeedsOrganization = result.status !== 'completed' && result.status !== 'disabled';
    if (result.status === 'completed') {
      $('#session-memory-status').textContent = `整理完成：识别 ${result.memories} 条记忆、${result.entities} 个实体、${result.relations} 条关系`;
    } else {
      $('#session-memory-status').textContent = result.message;
    }
    notify(result.message);
    return result;
  } catch (error) {
    sessionNeedsOrganization = true;
    $('#session-memory-status').textContent = '整理暂未完成，原始对话已保存，可稍后重试';
    notify(error.message);
    return null;
  } finally {
    organizingSession = false;
    button.disabled = !currentConversationId;
    button.textContent = '结束并整理';
  }
}

function resetConversationView(reloadHistory = true) {
  conversation = [];
  currentConversationId = null;
  sessionNeedsOrganization = false;
  selectedAttachments = [];
  renderAttachmentTray();
  $('#messages').innerHTML = initialWelcomeMarkup;
  $('#organize-session').disabled = true;
  $('#session-memory-status').textContent = '聊喜欢的风格、节奏和声音，为下一部作品做准备';
  bindPromptButtons();
  if (reloadHistory) loadConversations();
}

bindPromptButtons();
$('#organize-session').addEventListener('click', () => organizeCurrentConversation(true));
$('#new-chat').addEventListener('click', async () => {
  if (sessionNeedsOrganization) await organizeCurrentConversation();
  resetConversationView();
});
function setSidebarOpen(open) {
  $('.sidebar').classList.toggle('open', open);
  $('#mobile-menu').setAttribute('aria-expanded', String(open));
}

$('#mobile-menu').addEventListener('click', () => setSidebarOpen(!$('.sidebar').classList.contains('open')));
$('#sidebar-scrim').addEventListener('click', () => setSidebarOpen(false));

const productionStages = {
  story: {
    avatar: '文', role: '故事 AGENT · 结构化规划', status: '等待执行',
    title: '故事、人物、节拍与对白', summary: '输出必须通过故事 JSON 校验，随后直接交给视觉 Agent。',
    evidence: ['✓ 人物与动机', '✓ 时间节拍', '✓ 可表演对白'], deliverable: '故事 JSON', meta: '受众 / 主题 / 角色 / 剧本',
  },
  visual: {
    avatar: '镜', role: '视觉 AGENT + 总导演 · 文本预演', status: '等待上游',
    title: '连续性、分镜、台词与视频提示词', summary: '逐镜方案至少经过两轮总导演文本预演，评分达到 90 分才允许调用视频模型。',
    evidence: ['✓ 连续性圣经', '✓ 清理无关提示词', '✓ 90 分视频门禁'], deliverable: '总导演审定视觉 JSON', meta: '资产 / 分镜 / speech_text / preflight',
  },
  media: {
    avatar: '制', role: '媒体制作 AGENT · 工具执行', status: '等待上游',
    title: '原生声画、兜底配音和字幕', summary: '优先保留 H3 同步生成的对白、情绪、音效和配乐；没有原生音轨时才调用 Speech 兜底。',
    evidence: ['✓ H3 原生声画', '✓ 单一说话者', '✓ 定时烧录字幕'], deliverable: '可播放 MP4', meta: '原生音轨 / TTS 兜底 / 烧录字幕',
  },
  quality: {
    avatar: '审', role: '质检 AGENT · 否决门禁', status: '等待媒体',
    title: '真实文件技术质检', summary: '通过 ffprobe 检查视频轨、语音轨、字幕标记和时长；失败时项目不会完成。',
    evidence: ['✓ 视频轨', '✓ 语音轨', '✓ 字幕与时长'], deliverable: '结构化质检报告', meta: 'pass / reject / issues',
  },
};

function switchStudioView(view) {
  activeStudioView = view === 'library' ? 'library' : 'production';
  $('#studio-production-view').classList.toggle('hidden', activeStudioView !== 'production');
  $('#studio-library-view').classList.toggle('hidden', activeStudioView !== 'library');
  document.querySelectorAll('[data-studio-view]').forEach((button) => {
    const active = button.dataset.studioView === activeStudioView;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  if (!$('#studio-workspace').classList.contains('hidden')) {
    $('#workspace-title').textContent = activeStudioView === 'library' ? '视频作品库' : 'AI 导演工作室';
    $('#workspace-subtitle').textContent = activeStudioView === 'library'
      ? '历史成片、镜头片段与失败记录'
      : '一人导演，一组 Agent 把关';
  }
  window.localStorage.setItem('assistant-studio-view', activeStudioView);
  if (activeStudioView === 'library' && currentUser) loadVideoGallery();
}

function switchWorkspace(mode) {
  const isStudio = mode === 'studio';
  $('#chat-workspace').classList.toggle('hidden', isStudio);
  $('#studio-workspace').classList.toggle('hidden', !isStudio);
  $('#chat-sidebar-context').classList.toggle('hidden', isStudio);
  $('#studio-sidebar-context').classList.toggle('hidden', !isStudio);
  $('#workspace-title').textContent = isStudio ? '视频工作室' : '创意对话';
  $('#workspace-subtitle').textContent = isStudio ? '创意 → 故事 → 分镜 → 声画 → 字幕 → 合片 → 验收' : '聊创意与偏好，为下一部作品做准备';
  document.querySelectorAll('[data-workspace]').forEach((button) => {
    button.classList.toggle('active', button.dataset.workspace === mode);
  });
  setSidebarOpen(false);
  window.localStorage.setItem('assistant-workspace', mode);
  if (isStudio && currentUser) {
    switchStudioView(activeStudioView);
    loadDirectorProjects();
  }
}

function directorStatusLabel(status, oneClick = false) {
  if (status === 'awaiting_storyboard') return '等待分镜确认';
  return { awaiting_confirmation: '等待故事确认', queued: '准备派单', processing: oneClick ? '完整成片制作中' : '首镜预览制作中', completed: oneClick ? '完整成片完成' : '首镜预览完成', failed: '制作失败' }[status] || status;
}

function directorAgentStatusLabel(status) {
  return { pending: '等待上游', processing: '正在工作', completed: '已通过', failed: '需要处理' }[status] || status;
}

async function loadDirectorProjects() {
  if (!currentUser) return;
  try {
    const projects = await api('/director/projects');
    renderDirectorProjectList(projects);
    if (!projects.length) return;
    if (!activeDirectorProject || !projects.some((item) => item.id === activeDirectorProject.id)) {
      await loadDirectorProject(projects[0].id);
      loadVideoGallery();
    }
  } catch (error) {
    notify(error.message);
  }
}

function renderDirectorProjectList(projects) {
  const list = $('#director-project-list');
  if (!projects.length) {
    list.innerHTML = '<button class="project-item" type="button" disabled><span class="project-cover">＋</span><span><strong>还没有导演项目</strong><small>从对话或工作室开始制作</small></span></button>';
    return;
  }
  list.innerHTML = projects.map((project, index) => `
    <button class="project-item ${project.id === activeDirectorProject?.id || (!activeDirectorProject && index === 0) ? 'active' : ''}" type="button" data-director-project="${escapeHtml(project.id)}">
      <span class="project-cover">${escapeHtml(String(project.title || '片').slice(0, 1))}</span>
      <span><strong>${escapeHtml(project.title)}</strong><small>${escapeHtml(directorStatusLabel(project.status, project.one_click))} · ${project.progress}%</small></span>
    </button>`).join('');
  list.querySelectorAll('[data-director-project]').forEach((button) => {
    button.addEventListener('click', () => {
      selectedDirectorStage = null;
      switchStudioView('production');
      loadDirectorProject(button.dataset.directorProject);
    });
  });
}

async function loadDirectorProject(projectId) {
  window.clearTimeout(directorProjectTimer);
  try {
    const project = await api(`/director/projects/${projectId}`);
    if (activeDirectorProject?.id !== project.id) selectedDirectorStage = null;
    renderDirectorProject(project);
    if (['queued', 'processing'].includes(project.status)) {
      directorProjectTimer = window.setTimeout(() => loadDirectorProject(project.id), 3500);
    } else if (project.preview_video) {
      loadVideoGallery();
    }
  } catch (error) {
    notify(error.message);
  }
}

function renderDirectorProject(project) {
  activeDirectorProject = project;
  renderCreativeContext(project);
  $('#production-kicker').innerHTML = `<span class="live-dot"></span> DIRECTOR PROJECT · ${escapeHtml(project.id.slice(0, 8).toUpperCase())}`;
  $('#production-title').textContent = project.title;
  const modeLabel = project.one_click ? '完整成片' : '首镜预览';
  const shotProgress = project.planned_shots ? ` · ${project.completed_shots}/${project.planned_shots} 镜` : '';
  const scope = project.one_click
    ? `目标 ${project.target_seconds} 秒`
    : `成片目标 ${project.target_seconds} 秒 · 本次只交付 4 秒首镜`;
  $('#production-meta').textContent = `${project.visual_style} · ${project.aspect_ratio} · ${project.resolution || '768P'} · ${modeLabel} · ${scope}${shotProgress}`;
  $('#director-working-badge').textContent = directorStatusLabel(project.status, project.one_click);
  $('.director-progress strong').textContent = `${project.progress}%`;
  $('.director-progress i').style.width = `${project.progress}%`;
  $('#director-progress-label').textContent = project.one_click ? '整片制作进度' : '首镜预览任务进度';
  $('#one-click-movie').textContent = project.one_click
    ? '▶ 再做一版'
    : `▶ 生成完整 ${project.target_seconds} 秒`;

  const directorRun = project.agents.find((run) => run.agent === 'story');
  const fallbackStage = project.agents.find((run) => run.status === 'failed')?.agent
    || [...project.agents].reverse().find((run) => run.status === 'completed')?.agent
    || 'story';
  const stageKey = selectedDirectorStage
    || (project.agents.some((run) => run.agent === project.current_stage) ? project.current_stage : fallbackStage);
  const activeRun = project.agents.find((run) => run.agent === stageKey);
  $('#director-heading').textContent = project.status === 'awaiting_confirmation'
    ? '请确认故事后再启动 Agent'
    : project.status === 'completed'
    ? (project.one_click ? '完整成片已生成，技术完整性检查通过；请复核对白与画面' : '首镜预览已完成，完整成片尚未生成')
    : project.status === 'failed'
      ? '导演项目暂停，需要处理失败任务'
      : `${activeRun?.agent_name || '总导演 Agent'}正在处理当前交付物`;
  $('#director-summary').textContent = project.status === 'awaiting_confirmation'
    ? `待确认故事：${project.premise}。确认前不会调用文本 Agent 或视频模型。`
    : project.error_message
    || (project.status === 'completed' && !project.one_click
      ? `已交付 4 秒首镜预览；要获得完整 ${project.target_seconds} 秒视频，请使用“一键成片”。`
      : project.final_summary)
    || directorRun?.decision_summary
    || '总导演编排器正在调度 4 位执行 Agent。页面展示结构化交付和真实工具执行结果。';
  if (project.status === 'awaiting_storyboard') {
    $('#director-heading').textContent = '分镜已就绪，请核对后生成视频';
    $('#director-summary').textContent = '尚未提交视频任务。先核对本片采用的偏好、故事和逐镜对白。';
  }
  $('#project-settings').textContent = project.status === 'awaiting_confirmation' ? '编辑草案' : '以此新建一版';

  const continueButton = $('#continue-production');
  continueButton.classList.toggle('hidden', !['awaiting_confirmation', 'failed'].includes(project.status));
  continueButton.innerHTML = project.status === 'awaiting_confirmation'
    ? '确认故事并开始预演 <span>→</span>'
    : '继续制作 <span>→</span>';

  document.querySelectorAll('[data-stage]').forEach((button) => {
    const run = project.agents.find((item) => item.agent === button.dataset.stage);
    button.classList.toggle('done', run?.status === 'completed');
    button.classList.toggle('active', run?.status === 'processing' || run?.status === 'failed');
    button.setAttribute('aria-pressed', String(run?.agent === stageKey));
    if (run) button.querySelector('em').textContent = directorAgentStatusLabel(run.status);
  });
  document.querySelectorAll('[data-org-stage]').forEach((gate) => {
    const run = project.agents.find((item) => item.agent === gate.dataset.orgStage);
    gate.classList.toggle('working', ['processing', 'failed'].includes(run?.status));
  });

  document.querySelectorAll('[data-director-project]').forEach((button) => {
    button.classList.toggle('active', button.dataset.directorProject === project.id);
  });
  const sidebarProject = document.querySelector(`[data-director-project="${project.id}"]`);
  if (sidebarProject) {
    sidebarProject.querySelector('strong').textContent = project.title;
    sidebarProject.querySelector('small').textContent = `${directorStatusLabel(project.status, project.one_click)} · ${project.progress}%`;
  }

  renderContinuityBible(project);
  renderDirectorShots(project);
  if (activeRun) renderProductionStage(activeRun.agent, false);
  window.clearTimeout(directorProjectTimer);
  if (['queued', 'processing'].includes(project.status)) {
    directorProjectTimer = window.setTimeout(() => loadDirectorProject(project.id), 3500);
  }
}

async function loadCreativePreferences() {
  try { creativePreferences = await api('/director/preferences'); }
  catch (error) { notify(`创作偏好暂不可用：${error.message}`); }
}

const preferenceLabels = {visual_style: '视觉', audience: '受众', narrative_tone: '叙事', pacing: '节奏', sound: '声音与字幕', avoid: '避免'};

function renderCreativeContext(project) {
  const snapshot = project.personalization || {};
  const explicit = Object.entries(snapshot.preferences || {})
    .filter(([key, value]) => preferenceLabels[key] && value)
    .map(([key, value]) => `${preferenceLabels[key]}：${value}`);
  const memories = (snapshot.memories || []).map(item => item.content);
  const context = $('#project-personalization');
  context.replaceChildren();
  const note = document.createElement('p');
  note.textContent = '本次创意与制作设定优先；以下是规划时的参考快照。';
  context.append(note);
  if (!explicit.length && !memories.length) {
    const empty = document.createElement('p');
    empty.textContent = '本片未采用历史偏好，将按本次要求创作。可在“创作偏好”中设置今后的默认风格。';
    context.append(empty);
  } else {
    const list = document.createElement('ul');
    [...explicit, ...memories.map(value => `创作记忆：${value}`)].forEach(value => {
      const row = document.createElement('li'); row.textContent = value; list.append(row);
    });
    context.append(list);
  }
  $('#project-feedback-btn').classList.toggle('hidden', project.status !== 'completed');
  const output = $('#project-output');
  const media = project.status === 'completed'
    ? (project.final_video || project.shots?.find(shot => shot.rendered_video)?.rendered_video)
    : null;
  output.replaceChildren();
  if (media) {
    const video = document.createElement('video');
    video.controls = true; video.playsInline = true; video.preload = 'metadata';
    video.src = media.preview_url;
    video.setAttribute('aria-label', '本片成品（已制作声音与字幕）');
    const link = document.createElement('a');
    link.href = media.download_url; link.textContent = '下载当前成品 MP4';
    output.append(video, link);
  }
  $('#project-feedback-status').textContent = project.feedback?.verdict
    ? `${project.feedback.verdict === 'accepted' ? '已验收 · 满意' : '待修改'} · 贴合度 ${project.feedback.rating}/5 · ${project.feedback.notes || ''}`
    : (project.status === 'completed' ? '技术检查通过，等待你观看并验收。' : '');
  const panel = $('#storyboard-review-panel');
  panel.classList.toggle('hidden', project.status !== 'awaiting_storyboard');
  const story = project.agents?.find(item => item.agent === 'story')?.result_data || {};
  $('#storyboard-review-content').innerHTML = `<p>${escapeHtml(story.script || project.premise)}</p>`
    + (project.storyboard || []).map((shot, index) => `<article class="storyboard-review-shot">
      <strong>第 ${index + 1} 镜 · ${escapeHtml(shot.title || '')}</strong>
      <p>${escapeHtml(shot.positive_prompt || shot.action || '')}</p>
      <p>对白：${escapeHtml(shot.speech_text || '')}</p>
      <p>字幕：${escapeHtml(shot.subtitle_text || shot.speech_text || '')}</p>
    </article>`).join('');
}

$('#creative-preferences-btn').addEventListener('click', async () => {
  await loadCreativePreferences();
  const form = $('#creative-preferences-form');
  for (const key of Object.keys(preferenceLabels)) form.elements[key].value = creativePreferences[key] || '';
  form.elements.use_memory.checked = creativePreferences.use_memory !== false;
  $('#creative-preferences-dialog').showModal();
});
document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => {
  document.getElementById(button.dataset.closeDialog).close();
}));
$('#creative-preferences-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type="submit"]'); button.disabled = true;
  try {
    const values = Object.fromEntries(Object.keys(preferenceLabels).map(key => [key, form.elements[key].value.trim()]));
    values.use_memory = form.elements.use_memory.checked;
    creativePreferences = await api('/director/preferences', {method: 'PUT', body: JSON.stringify(values)});
    $('#creative-preferences-dialog').close(); notify('已保存，之后创建的作品会参考这些偏好。');
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; }
});
$('#project-feedback-btn').addEventListener('click', () => {
  if (!activeDirectorProject) return;
  const form = $('#creative-feedback-form'); form.reset();
  form.dataset.projectId = activeDirectorProject.id;
  const saved = activeDirectorProject.feedback || {};
  for (const key of ['verdict', 'rating', 'notes', 'reusable_preference']) {
    if (saved[key] !== undefined) form.elements[key].value = saved[key];
  }
  form.elements.remember.checked = Boolean(saved.remember);
  $('#creative-feedback-dialog').showModal();
});
$('#creative-feedback-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type="submit"]'); button.disabled = true;
  const values = Object.fromEntries(['verdict', 'notes', 'reusable_preference'].map(key => [key, form.elements[key].value.trim()]));
  values.rating = Number(form.elements.rating.value);
  values.remember = form.elements.remember.checked;
  try {
    const project = await api(`/director/projects/${form.dataset.projectId}/feedback`, {method: 'PUT', body: JSON.stringify(values)});
    if (activeDirectorProject?.id === project.id) renderDirectorProject(project);
    $('#creative-feedback-dialog').close();
    notify(values.remember ? '反馈已保存，明确的创作偏好将用于后续作品。' : '已保存本片反馈。');
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; }
});
$('#approve-storyboard-btn').addEventListener('click', async () => {
  const project = activeDirectorProject;
  if (project?.status !== 'awaiting_storyboard') return;
  const button = $('#approve-storyboard-btn'); button.disabled = true;
  try {
    const result = await api(`/director/projects/${project.id}/approve-storyboard`, {
      method: 'POST', body: JSON.stringify({storyboard_hash: project.storyboard_hash}),
    });
    if (activeDirectorProject?.id === result.id) renderDirectorProject(result);
    notify('已确认这版分镜，视频任务即将开始。');
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; }
});
$('#revise-storyboard-btn').addEventListener('click', () => showDirectorStart(
  activeDirectorProject?.one_click || false, activeDirectorProject,
));

function renderContinuityBible(project) {
  const bible = project.continuity_bible || {};
  const characters = Array.isArray(bible.characters) ? bible.characters : [];
  const relationships = Array.isArray(bible.relationships) ? bible.relationships : [];
  const references = characters.filter((character) => character.reference_image_url).length;
  const score = characters.length ? Math.round((references / characters.length) * 100) : 0;
  $('#continuity-score-value').textContent = characters.length ? String(score) : '—';
  $('#continuity-score-label').textContent = characters.length
    ? `角色参考图登记率 ${references}/${characters.length} · 不是画面一致性评分`
    : '等待连续性建档';
  $('#continuity-score-bar').style.width = `${score}%`;
  const assets = $('#continuity-assets');
  if (characters.length) {
    assets.innerHTML = characters.map((character, index) => {
      const lock = character.reference_image_url ? 'REF READY' : 'TEXT';
      const voice = character.voice_id || character.voice_profile || '声线待锁';
      return `<button type="button"><span class="asset-thumb person-${index % 2 ? 'two' : 'one'}">${escapeHtml(String(character.name || '角').slice(0, 1))}</span><span><strong>${escapeHtml(character.name || '未命名角色')} · ${escapeHtml(character.role || '角色')}</strong><small>${escapeHtml(character.appearance || '外貌待完善')} · ${escapeHtml(character.wardrobe || '服装待完善')} · ${escapeHtml(voice)}</small></span><em>${lock}</em></button>`;
    }).join('');
  } else {
    assets.innerHTML = '<button type="button"><span class="asset-thumb person-one">角</span><span><strong>等待视觉 Agent 建档</strong><small>外貌 / 服装 / 声线 / 定妆照</small></span><em>WAIT</em></button>';
  }
  const relationPanel = $('#continuity-relationships');
  relationPanel.innerHTML = `<h3>人物关系 · ${characters.length ? '文字连续性已锁定' : '等待建档'}</h3>${relationships.length
    ? relationships.map((item, index) => `<p><span>${String(index + 1).padStart(2, '0')}</span>${escapeHtml(item.source || '')} → ${escapeHtml(item.target || '')}：${escapeHtml(item.relation || '待定义')}</p>`).join('')
    : '<p><span>—</span>项目启动后自动同步主角、配角和人物关系</p>'}`;
}

function renderDirectorShots(project) {
  const strip = $('#director-shot-strip');
  const showAll = $('#show-all-shots');
  showAll.textContent = `查看全部 ${project.planned_shots || project.shots?.length || 0} 镜 →`;
  if (!project.shots?.length) {
    strip.innerHTML = '<div class="video-gallery-empty"><span>镜</span><strong>等待视觉 Agent 生成分镜</strong><small>分镜会按真实后端任务自动同步</small></div>';
    $('#shot-heading').nextElementSibling.textContent = '尚未生成镜头';
    return;
  }
  strip.innerHTML = project.shots.map((shot) => `
    <button class="shot-card ${shot.status === 'completed' ? 'approved' : shot.status === 'processing' ? 'selected' : shot.status === 'failed' ? 'failed' : ''}" type="button" data-video-job="${escapeHtml(shot.video?.id || '')}">
      <div class="shot-frame frame-wide"><span>${shot.sequence}</span><i></i></div>
      <strong>${escapeHtml(shot.title)}</strong><small>${escapeHtml(shot.seconds)}s · ${escapeHtml(videoStatusLabel(shot.status))}${shot.has_burned_subtitles ? ` · ${shot.native_audio ? '原生声画+字幕' : '兜底配音+字幕'}` : ''}</small>
      ${shot.speech_text ? `<small>${escapeHtml(shot.speaker || '旁白')}：${escapeHtml(shot.speech_text)}</small>` : ''}
      ${shot.error_message ? `<small class="shot-error">${escapeHtml(shot.error_message)}</small>` : ''}
    </button>`).join('');
  $('#shot-heading').nextElementSibling.textContent = `${project.completed_shots} / ${project.planned_shots} 镜已生成`;
  strip.querySelectorAll('.shot-card').forEach((button) => {
    button.addEventListener('click', () => {
      strip.querySelectorAll('.shot-card').forEach((item) => item.classList.remove('selected'));
      button.classList.add('selected');
      const shot = project.shots.find(item => item.video?.id === button.dataset.videoJob);
      if (shot?.rendered_video) {
        const video = document.createElement('video');
        video.controls = true; video.playsInline = true;
        video.src = shot.rendered_video.preview_url;
        const link = document.createElement('a');
        link.href = shot.rendered_video.download_url; link.textContent = `下载第 ${shot.sequence} 镜成品`;
        $('#project-output').replaceChildren(video, link);
        $('#project-output').scrollIntoView({behavior:'smooth', block:'center'});
      }
    });
  });
}

function showDirectorStart(oneClick = false, sourceProject = null, edit = false) {
  editingDirectorDraft = edit ? sourceProject?.id : null;
  $('#director-start-form').reset();
  directorOneClickMode = oneClick;
  const title = $('#director-start-dialog h2');
  title.textContent = oneClick ? '一键成片' : '开始制作电影';
  $('#director-start-intro').textContent = oneClick
    ? '总导演编排器将调度 4 位执行 Agent，逐镜生成同步表演、原生声音和定时字幕，并最终合片。'
    : '总导演编排器会生成一个带同步对白、情绪声音和定时字幕的预览镜头。';
  $('#director-style').value = creativePreferences.visual_style || '';
  $('#director-use-memory').checked = creativePreferences.use_memory !== false;
  $('#director-use-memory').disabled = edit;
  $('#director-confirm-story').disabled = edit;
  if (sourceProject) {
    $('#director-premise').value = sourceProject.premise || '';
    $('#director-continuity-notes').value = sourceProject.continuity_notes || '';
    $('#director-duration').value = String(sourceProject.target_seconds || 60);
    $('#director-ratio').value = sourceProject.aspect_ratio || '9:16';
    $('#director-resolution').value = sourceProject.resolution || '768P';
    $('#director-style').value = sourceProject.visual_style || '';
    if (!edit && sourceProject.feedback?.notes) {
      $('#director-premise').value = `${sourceProject.premise}\n\n本版修改要求：${sourceProject.feedback.notes}`.slice(0, 8000);
    }
    if (edit) $('#director-use-memory').checked = Boolean(sourceProject.personalization?.memory_enabled);
  }
  if (edit) title.textContent = '编辑当前草案';
  updateDirectorModeSummary();
  $('#director-start-dialog').showModal();
  window.setTimeout(() => $('#director-premise').focus(), 80);
}

function updateDirectorModeSummary() {
  const seconds = Number($('#director-duration').value);
  const resolution = $('#director-resolution').value;
  const estimatedShots = Math.ceil(seconds / 12);
  const requireStoryConfirmation = $('#director-confirm-story').checked;
  const panel = $('#director-start-boundary');
  panel.classList.toggle('one-click', directorOneClickMode);
  panel.querySelector('strong').textContent = directorOneClickMode ? '一键成片 · 额度确认' : '常规制作 · 先看预览';
  const generationSummary = directorOneClickMode
    ? `通过门禁后，预计以 ${resolution} 生成约 ${estimatedShots} 个片段，再自动合成为约 ${seconds} 秒影片。`
    : `通过门禁后，以 ${resolution} 生成第一个带配音和字幕的 4 秒预览镜头。`;
  panel.querySelector('span').textContent = `${requireStoryConfirmation ? '先保存草案并等待你确认故事；确认后进行至少两轮文本预演，90 分才放行。' : '将立即启动文本预演，90 分才放行。'}${generationSummary}`;
  $('#director-start-submit').textContent = requireStoryConfirmation
    ? '保存故事并进入确认'
    : (directorOneClickMode ? `立即预演并准备约 ${estimatedShots} 个镜头` : '立即启动总导演文本预演');
  panel.querySelector('span').textContent += '文本规划完成后，还会展示故事与分镜供你确认，再生成视频。';
  if (editingDirectorDraft) $('#director-start-submit').textContent = '保存草案修改';
}

function videoStatusLabel(status) {
  return { awaiting_confirmation: '等待确认', queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[status] || status;
}

function videoCreatedAt(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false });
}

function renderStudioVideo(job, project = null) {
  const card = document.createElement('article');
  card.className = `video-result-card ${job.status}`;

  const media = document.createElement('div');
  media.className = 'video-result-media';
  const [width, height] = String(job.size || '').split('x').map(Number);
  if (width > height) media.classList.add('landscape');

  if (job.status === 'completed' && job.download_url) {
    const video = document.createElement('video');
    video.controls = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.src = job.preview_url || job.download_url;
    video.setAttribute('aria-label', `生成视频：${job.prompt}`);
    media.appendChild(video);
  } else {
    const state = document.createElement('div');
    state.className = 'video-result-state';
    state.innerHTML = `<span>${job.status === 'failed' ? '!' : '◌'}</span><strong>${escapeHtml(videoStatusLabel(job.status))}</strong>`;
    media.appendChild(state);
  }

  const detail = document.createElement('div');
  detail.className = 'video-result-detail';
  const heading = document.createElement('div');
  heading.className = 'video-result-heading';
  const title = project ? `镜头片段 · ${project.title}` : '独立视频';
  heading.innerHTML = `<strong>${escapeHtml(title)}</strong><em>${escapeHtml(videoStatusLabel(job.status))}</em>`;
  const meta = document.createElement('small');
  meta.textContent = `${job.seconds} 秒 · ${job.size} · ${job.resolution || '768P'}${videoCreatedAt(job.created_at) ? ` · ${videoCreatedAt(job.created_at)}` : ''}`;
  const prompt = document.createElement('p');
  prompt.textContent = job.prompt;
  detail.append(heading, meta, prompt);

  if (job.status === 'awaiting_confirmation') {
    detail.appendChild(videoConfirmButton(job, loadVideoGallery));
  }

  if (job.status === 'completed' && job.download_url) {
    const download = document.createElement('a');
    download.href = job.download_url;
    download.textContent = '下载 MP4 ↓';
    detail.appendChild(download);
  } else if (job.status === 'failed') {
    const error = document.createElement('b');
    error.textContent = job.error_message || '视频渠道返回失败';
    detail.appendChild(error);
  }

  card.append(media, detail);
  return card;
}

function renderDirectorMovie(project) {
  const card = document.createElement('article');
  card.className = 'video-result-card completed director-final-movie';
  card.innerHTML = `<div class="video-result-media"><video controls playsinline preload="metadata" src="${escapeHtml(project.final_video.preview_url)}" aria-label="一键成片：${escapeHtml(project.title)}"></video></div><div class="video-result-detail"><div class="video-result-heading"><strong>一键成片 · ${escapeHtml(project.title)}</strong><em>含配音与字幕</em></div><small>${project.target_seconds} 秒 · ${escapeHtml(project.aspect_ratio)} · ${escapeHtml(project.resolution || '768P')} · ${project.completed_shots} 个镜头</small><p>${escapeHtml(project.final_summary || '4 位执行 Agent 已完成真实制作和质检。')}</p><a href="${escapeHtml(project.final_video.download_url)}">下载完整 MP4 ↓</a></div>`;
  return card;
}

async function loadVideoGallery() {
  if (!currentUser) return;
  window.clearTimeout(videoGalleryTimer);
  const list = $('#video-gallery-list');
  const summary = $('#video-gallery-summary');
  const refresh = $('#refresh-video-gallery');
  refresh.disabled = true;
  try {
    const jobs = await api('/videos');
    let projects = [];
    try {
      projects = await api('/director/projects');
    } catch (_error) {
      // Individual generated clips stay playable if the director project list is unavailable.
    }
    const projectByJobId = new Map();
    projects.forEach((project) => {
      (project.shots || []).forEach((shot) => {
        if (shot.video?.id) projectByJobId.set(shot.video.id, project);
      });
    });
    const finalMovies = projects.filter((project) => project.final_video);
    const allEntries = [
      ...finalMovies.map((project) => ({
        type: 'final',
        createdAt: project.created_at || '',
        project,
      })),
      ...jobs.map((job) => ({
        type: job.status === 'failed' ? 'failed' : 'clip',
        createdAt: job.created_at || '',
        job,
        project: projectByJobId.get(job.id) || null,
      })),
    ].sort((left, right) => String(right.createdAt).localeCompare(String(left.createdAt)));
    const visibleEntries = videoLibraryFilter === 'all'
      ? allEntries
      : allEntries.filter((entry) => entry.type === videoLibraryFilter);
    $('#video-library-count').textContent = String(allEntries.length);
    list.replaceChildren();
    if (!visibleEntries.length) {
      const emptyLabels = {
        all: ['还没有历史视频', '生成后的完整成片与镜头会保存在这里'],
        final: ['还没有完整成片', '一键成片完成后会出现在这里'],
        clip: ['还没有镜头片段', '媒体 Agent 生成的镜头会出现在这里'],
        failed: ['没有失败记录', '当前历史任务没有失败项'],
      };
      const [emptyTitle, emptyDetail] = emptyLabels[videoLibraryFilter] || emptyLabels.all;
      list.innerHTML = `<div class="video-gallery-empty"><span>▶</span><strong>${emptyTitle}</strong><small>${emptyDetail}</small></div>`;
      summary.textContent = '作品库只展示属于你的生成记录';
      return;
    }

    list.append(...visibleEntries.map((entry) => (
      entry.type === 'final'
        ? renderDirectorMovie(entry.project)
        : renderStudioVideo(entry.job, entry.project)
    )));
    const completed = jobs.filter((job) => job.status === 'completed').length;
    const failed = jobs.filter((job) => job.status === 'failed').length;
    const active = jobs.filter((job) => ['queued', 'processing'].includes(job.status)).length;
    summary.textContent = `${finalMovies.length} 部完整成片 · ${completed} 个已完成片段 · ${failed} 条失败记录${active ? ` · ${active} 条正在生成` : ''} · 仅你本人可查看`;
    if (active) videoGalleryTimer = window.setTimeout(loadVideoGallery, 5000);
  } catch (error) {
    list.innerHTML = `<div class="video-gallery-empty failed"><span>!</span><strong>成片记录加载失败</strong><small>${escapeHtml(error.message)}</small></div>`;
    summary.textContent = '暂时无法读取真实生成记录';
  } finally {
    refresh.disabled = false;
  }
}

function renderProductionStage(key, remember = true) {
  if (remember) selectedDirectorStage = key;
  const liveRun = activeDirectorProject?.agents.find((run) => run.agent === key);
  if (liveRun) {
    const fallback = productionStages[key];
    const preflight = key === 'visual' ? liveRun.result_data?.director_preflight : null;
    const preflightEvidence = preflight ? [
      `${preflight.passed ? '✓' : '!'} 总导演门禁：${preflight.score || 0} 分 / 90 分`,
      `✓ 文本预演：${preflight.text_model_calls || 0} 轮，视频调用：${preflight.video_model_calls || 0}`,
      ...(preflight.removed_irrelevant?.length
        ? [`✓ 已清理无关项：${preflight.removed_irrelevant.slice(0, 2).join('；')}`]
        : []),
    ] : [];
    $('#stage-avatar').textContent = fallback?.avatar || '审';
    $('#stage-role').textContent = `${liveRun.agent_name} · ${directorAgentStatusLabel(liveRun.status)}`;
    $('#stage-status').textContent = directorAgentStatusLabel(liveRun.status);
    $('#stage-title').textContent = liveRun.status === 'pending'
      ? '等待上游 Agent 完成交付'
      : `${liveRun.agent_name}的可审计判断`;
    $('#stage-summary').textContent = liveRun.decision_summary
      || liveRun.error_message
      || 'Agent 尚未产出判断摘要。';
    $('#stage-evidence').replaceChildren(
      ...[
        `✓ 动态匹配模型：${liveRun.model}`,
        ...preflightEvidence,
        '✓ 仅展示专业判断摘要，不展示隐藏思维链',
        `◌ 当前状态：${directorAgentStatusLabel(liveRun.status)}`,
      ].map((item) => {
        const span = document.createElement('span');
        const icon = document.createElement('i');
        icon.textContent = item.slice(0, 1);
        span.append(icon, document.createTextNode(` ${item.slice(2)}`));
        return span;
      }),
    );
    $('#stage-deliverable').querySelector('strong').textContent = `${liveRun.agent_name} · 当前交付物`;
    $('#stage-deliverable').querySelector('small').textContent = liveRun.deliverable
      ? liveRun.deliverable.slice(0, 260)
      : '交付物生成后会自动同步到这里';
    document.querySelectorAll('[data-stage]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.stage === key));
    });
    return;
  }
  const stage = productionStages[key];
  if (!stage) return;
  $('#stage-avatar').textContent = stage.avatar;
  $('#stage-role').textContent = stage.role;
  $('#stage-status').textContent = stage.status;
  $('#stage-title').textContent = stage.title;
  $('#stage-summary').textContent = stage.summary;
  $('#stage-evidence').replaceChildren(...stage.evidence.map((item) => {
    const span = document.createElement('span');
    const icon = document.createElement('i');
    icon.textContent = item.slice(0, 1);
    span.append(icon, document.createTextNode(` ${item.slice(2)}`));
    return span;
  }));
  $('#stage-deliverable').querySelector('strong').textContent = stage.deliverable;
  $('#stage-deliverable').querySelector('small').textContent = stage.meta;
  document.querySelectorAll('[data-stage]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.stage === key));
  });
}

document.querySelectorAll('[data-workspace]').forEach((button) => {
  button.addEventListener('click', () => switchWorkspace(button.dataset.workspace));
});
document.querySelectorAll('[data-studio-view]').forEach((button) => {
  button.addEventListener('click', () => switchStudioView(button.dataset.studioView));
});
document.querySelectorAll('[data-video-library-filter]').forEach((button) => {
  button.addEventListener('click', () => {
    videoLibraryFilter = button.dataset.videoLibraryFilter;
    document.querySelectorAll('[data-video-library-filter]').forEach((item) => {
      item.classList.toggle('active', item === button);
    });
    loadVideoGallery();
  });
});
document.querySelectorAll('[data-stage]').forEach((button) => {
  button.addEventListener('click', () => renderProductionStage(button.dataset.stage));
});

let directorApprovalResolver = null;
function settleDirectorApproval(approved) {
  const dialog = $('#director-approval-dialog');
  if (dialog.open) dialog.close();
  if (!directorApprovalResolver) return;
  const resolve = directorApprovalResolver;
  directorApprovalResolver = null;
  resolve(approved);
}
function showDirectorApproval({ title, premise, resolution, estimatedShots }) {
  $('#director-approval-title').textContent = title;
  $('#director-approval-story').textContent = premise;
  $('#director-approval-video-label').textContent = `通过后以 ${resolution} 生成视频`;
  $('#director-approval-video-detail').textContent = `预计调用视频模型生成约 ${estimatedShots} 个镜头`;
  const dialog = $('#director-approval-dialog');
  if (dialog.open) dialog.close();
  dialog.showModal();
  $('#director-approval-confirm').focus();
  return new Promise((resolve) => {
    directorApprovalResolver = resolve;
  });
}
$('#director-approval-close').addEventListener('click', () => settleDirectorApproval(false));
$('#director-approval-cancel').addEventListener('click', () => settleDirectorApproval(false));
$('#director-approval-confirm').addEventListener('click', () => settleDirectorApproval(true));
$('#director-approval-dialog').addEventListener('cancel', (event) => {
  event.preventDefault();
  settleDirectorApproval(false);
});
$('#continue-production').addEventListener('click', async () => {
  if (!activeDirectorProject) return;
  const button = $('#continue-production');
  const awaitingConfirmation = activeDirectorProject.status === 'awaiting_confirmation';
  if (!awaitingConfirmation && activeDirectorProject.status !== 'failed') {
    notify('当前项目不需要确认或继续制作。');
    return;
  }
  if (awaitingConfirmation) {
    button.disabled = true;
    const estimatedShots = activeDirectorProject.one_click
      ? Math.ceil(activeDirectorProject.target_seconds / 12)
      : 1;
    const confirmed = await showDirectorApproval({
      title: '确认故事并开始预演',
      premise: activeDirectorProject.premise,
      resolution: activeDirectorProject.resolution,
      estimatedShots,
    });
    if (!confirmed) {
      button.disabled = false;
      return;
    }
  }
  button.disabled = true;
  try {
    const action = awaitingConfirmation ? 'approve' : 'resume';
    const project = await api(`/director/projects/${activeDirectorProject.id}/${action}`, {
      method: 'POST',
    });
    renderDirectorProject(project);
    notify(awaitingConfirmation
      ? '故事已确认。总导演正在进行文本预演；通过 90 分门禁前不会调用视频模型。'
      : '失败任务已重新派发，正在从导演流程继续制作。');
    loadDirectorProject(project.id);
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
  }
});
function updateStudioProgress() {
  const stages = [...document.querySelectorAll('[data-stage]')];
  const completed = stages.filter((item) => item.classList.contains('done')).length;
  const progress = Math.round((completed / stages.length) * 100);
  $('.director-progress strong').textContent = `${progress}%`;
  $('.director-progress i').style.width = `${progress}%`;
}

function latestVideoJobText(job) {
  if (!job) return '还没有提交过真实视频任务。';
  const labels = { queued: '排队中', processing: '生成中', completed: '已完成', failed: '失败' };
  const status = labels[job.status] || job.status;
  return job.status === 'failed' && job.error_message
    ? `最近一次任务：${status}，${job.error_message}`
    : `最近一次任务：${status}。`;
}

async function loadVideoReadiness() {
  const panel = $('#video-readiness');
  const mark = panel.querySelector('.readiness-mark');
  const title = $('#video-readiness-title');
  const detail = $('#video-readiness-detail');
  panel.classList.remove('ready', 'blocked');
  panel.classList.add('checking');
  mark.textContent = '…';
  title.textContent = '正在检查真实生成条件';
  detail.textContent = '确认视频渠道以及原生音频或兜底语音能力。';
  $('#guide-refresh-status').disabled = true;
  try {
    const result = await api('/videos/status');
    panel.classList.remove('checking');
    panel.classList.add(result.ready ? 'ready' : 'blocked');
    mark.textContent = result.ready ? '✓' : '!';
    title.textContent = result.ready
      ? `可以真实生成 · ${result.model}`
      : '暂时不能真实生成 · 缺少可用的视频或音频渠道';
    const audio = result.native_audio
      ? '优先保留 H3 同步生成的单人对白、情绪、音效和配乐；仅在没有原生音轨时使用 Speech 兜底。'
      : '视频没有原生音频能力时，系统使用独立 Speech 配音兜底。';
    detail.textContent = `${audio}中文字幕按对白时间窗烧录进画面。${latestVideoJobText(result.latest_job)}`;
    return result;
  } catch (error) {
    panel.classList.remove('checking');
    panel.classList.add('blocked');
    mark.textContent = '!';
    title.textContent = '生成状态检查失败';
    detail.textContent = error.message;
    return { ready: false };
  } finally {
    $('#guide-refresh-status').disabled = false;
  }
}

async function showStudioGuide() {
  $('#studio-guide-dialog').showModal();
  return loadVideoReadiness();
}

$('#view-deliverable').addEventListener('click', () => $('#shot-heading').scrollIntoView({ behavior: 'smooth', block: 'start' }));
$('#new-project').addEventListener('click', () => showDirectorStart(false));
$('#start-director-project').addEventListener('click', () => showDirectorStart(false));
$('#one-click-movie').addEventListener('click', () => showDirectorStart(true, activeDirectorProject));
$('#project-settings').addEventListener('click', () => showDirectorStart(
  activeDirectorProject?.one_click || false, activeDirectorProject,
  activeDirectorProject?.status === 'awaiting_confirmation',
));
$('#show-all-shots').addEventListener('click', () => {
  const planned = activeDirectorProject?.planned_shots || activeDirectorProject?.shots?.length || 0;
  notify(planned ? `当前项目共 ${planned} 镜，画板已展示全部真实分镜` : '当前项目还没有生成分镜');
});
$('#refresh-video-gallery').addEventListener('click', loadVideoGallery);
$('#director-start-close').addEventListener('click', () => $('#director-start-dialog').close());
$('#director-start-cancel').addEventListener('click', () => $('#director-start-dialog').close());
$('#director-duration').addEventListener('change', updateDirectorModeSummary);
$('#director-resolution').addEventListener('change', updateDirectorModeSummary);
$('#director-confirm-story').addEventListener('change', updateDirectorModeSummary);
$('#director-start-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const seconds = Number($('#director-duration').value);
  const estimatedShots = Math.ceil(seconds / 12);
  const resolution = $('#director-resolution').value;
  const requireStoryConfirmation = $('#director-confirm-story').checked;
  if (!requireStoryConfirmation && directorOneClickMode) {
    const confirmed = await showDirectorApproval({
      title: '确认跳过故事复核并开始预演',
      premise: $('#director-premise').value.trim(),
      resolution,
      estimatedShots,
    });
    if (!confirmed) return;
  }
  const button = $('#director-start-submit');
  button.disabled = true;
  button.textContent = '正在匹配 Agent 模型…';
  try {
    const values = {
        premise: $('#director-premise').value.trim(),
        target_seconds: Number($('#director-duration').value),
        aspect_ratio: $('#director-ratio').value,
        resolution,
        visual_style: $('#director-style').value.trim(),
        continuity_notes: $('#director-continuity-notes').value.trim(),
    };
    if (!editingDirectorDraft) Object.assign(values, {
      one_click: directorOneClickMode, story_confirmed: !requireStoryConfirmation,
      use_memory: $('#director-use-memory').checked,
    });
    const project = await api(editingDirectorDraft ? `/director/projects/${editingDirectorDraft}` : '/director/projects', {
      method: editingDirectorDraft ? 'PATCH' : 'POST',
      body: JSON.stringify(values),
    });
    $('#director-start-dialog').close();
    renderDirectorProject(project);
    switchWorkspace('studio');
    notify(editingDirectorDraft ? '草案修改已保存。' : requireStoryConfirmation
      ? '故事草案已保存，尚未调用任何 Agent 或视频模型。请核对后点击“确认故事并开始预演”。'
      : '总导演文本预演已启动；只有至少两轮复核且评分达到 90 分才会调用视频模型。');
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
    updateDirectorModeSummary();
  }
});
$('#studio-guide').addEventListener('click', showStudioGuide);
$('#studio-guide-close').addEventListener('click', () => $('#studio-guide-dialog').close());
$('#studio-guide-done').addEventListener('click', () => $('#studio-guide-dialog').close());
$('#guide-refresh-status').addEventListener('click', loadVideoReadiness);
$('#guide-go-chat').addEventListener('click', () => {
  $('#studio-guide-dialog').close();
  switchWorkspace('chat');
  const input = $('#chat-input');
  input.value = '请让视频 Agent 根据已确认的分镜生成一个 4 秒、16:9 的 MiniMax H3 镜头，并保留原生声音。镜头描述：';
  input.dispatchEvent(new Event('input'));
  input.focus();
  notify('镜头指令已准备好；补充画面描述后再发送');
});

updateStudioProgress();

switchWorkspace('studio');

if (resetToken) {
  $('#standard-auth').classList.add('hidden');
  $('#reset-request-view').classList.add('hidden');
  $('#reset-confirm-view').classList.remove('hidden');
  authView.classList.remove('hidden');
  appView.classList.add('hidden');
} else {
  loadCaptcha('user');
  loadSession();
}
