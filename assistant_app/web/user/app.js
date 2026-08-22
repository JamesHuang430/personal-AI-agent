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
const customModelValue = '__custom__';
const resetToken = new URLSearchParams(window.location.hash.slice(1)).get('reset_token');
const initialWelcomeMarkup = $('#messages').innerHTML;

async function api(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败（${response.status}）`);
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
  $('#auth-title').textContent = mode === 'login' ? '登录你的助理' : '创建你的助理';
  $('#auth-subtitle').textContent = mode === 'login' ? '继续上次的对话和计划' : '每天仅开放 3 个新用户名额';
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
  loadConversations();
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
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `history-item ${item.id === currentConversationId ? 'active' : ''}`;
      button.innerHTML = `<span>✦</span><span>${escapeHtml(item.title)}</span>`;
      button.addEventListener('click', async () => {
        if (sessionNeedsOrganization) await organizeCurrentConversation();
        await openConversation(item.id);
      });
      list.appendChild(button);
    }
  } catch (error) {
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
      addMessage(item.role, item.content, meta);
    }
    sessionNeedsOrganization = false;
    $('#organize-session').disabled = false;
    $('#session-memory-status').textContent = '历史会话已载入；可以继续对话，或再次整理到个人知识库';
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
      target.title = `${assignment.reason} · ${assignment.status === 'matched' ? '能力匹配' : '降级匹配'}`;
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
  $('#points-value').textContent = currentUser.points.toLocaleString('zh-CN');
  $('#profile-email').textContent = currentUser.email;
  $('#profile-btn').textContent = currentUser.email.charAt(0).toUpperCase();
  const checked = Boolean(currentUser.checked_in_today);
  $('#checkin-btn').disabled = checked;
  $('#checkin-btn').textContent = checked ? '今日已签到' : '签到 +100';
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

$('#checkin-btn').addEventListener('click', async () => {
  try {
    const result = await api('/users/check-in', { method: 'POST' });
    updateUser({ points: result.points, checked_in_today: true });
    notify(result.already_checked_in ? '今天已经签到过了' : `签到成功，获得 ${result.awarded} 积分`);
  } catch (error) { notify(error.message); }
});

async function showPackages() {
  try {
    const packages = await api('/packages');
    $('#package-list').innerHTML = packages.map((item, index) => `
      <article class="package-card ${index === 1 ? 'featured' : ''}">
        <h3>${escapeHtml(item.name)}</h3>
        <div class="package-price">¥${item.price_yuan}<small> / 次</small></div>
        <p class="package-points">${item.points.toLocaleString('zh-CN')} 积分</p>
      </article>`).join('') || '<p class="muted">暂无上架套餐</p>';
    $('#packages-dialog').showModal();
  } catch (error) { notify(error.message); }
}
$('#points-btn').addEventListener('click', showPackages);
$('.close-btn').addEventListener('click', () => $('#packages-dialog').close());

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

function renderArtifacts(message, result) {
  const files = result.files || [];
  const jobs = result.video_jobs || [];
  if (!files.length && !jobs.length) return;
  const list = document.createElement('div');
  list.className = 'artifact-list';
  for (const file of files) {
    const card = document.createElement('div');
    card.className = 'artifact-card';
    card.innerHTML = `<div><strong>▤ ${escapeHtml(file.filename)}</strong><small>${Math.max(1, Math.ceil(file.size_bytes / 1024))} KB</small></div><a href="${file.download_url}">下载文件</a>`;
    list.appendChild(card);
  }
  for (const job of jobs) {
    const card = document.createElement('div');
    card.className = 'artifact-card';
    card.innerHTML = `<div><strong>▶ 视频生成任务</strong><small>排队中 · ${escapeHtml(job.seconds)} 秒 · ${escapeHtml(job.size)}</small></div><a href="#">刷新状态</a>`;
    card.querySelector('a').addEventListener('click', (event) => { event.preventDefault(); pollVideoJob(job.id, card); });
    list.appendChild(card);
    window.setTimeout(() => pollVideoJob(job.id, card), 5000);
  }
  message.querySelector('.message-bubble').appendChild(list);
}

async function pollVideoJob(jobId, card) {
  try {
    const job = await api(`/videos/${jobId}`);
    const statusText = { queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[job.status] || job.status;
    card.querySelector('small').textContent = `${statusText} · ${job.seconds} 秒 · ${job.size}`;
    if (job.status === 'completed') {
      card.querySelector('a').textContent = '下载视频';
      card.querySelector('a').href = job.download_url;
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

async function sendMessage(text) {
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
  addMessage('user', content);
  conversation.push({ role: 'user', content });
  $('#chat-input').value = '';
  $('#send-btn').disabled = true;
  const pending = addMessage('assistant', '正在思考…');
  try {
    const result = await api('/chat', {
      method: 'POST',
      body: JSON.stringify({ model: selectedModel, message: content, conversation_id: currentConversationId }),
    });
    pending.remove();
    const responseMessage = addMessage('assistant', result.content, `${result.channel} · ${result.model}`);
    renderArtifacts(responseMessage, result);
    conversation.push({ role: 'assistant', content: result.content });
    currentConversationId = result.conversation_id;
    sessionNeedsOrganization = true;
    $('#organize-session').disabled = false;
    const usedMemories = Number(result.memory?.items_used || 0);
    const usedRelations = Number(result.memory?.graph_edges_used || 0);
    $('#session-memory-status').textContent = usedMemories || usedRelations
      ? `本轮调用了 ${usedMemories} 条记忆、${usedRelations} 条关系；结束后将归纳新素材`
      : '本轮内容已记录；结束会话后将自动提炼并同步到双重知识库';
    loadConversations();
  } catch (error) {
    pending.remove();
    addMessage('assistant', `暂时无法回答：${error.message}`);
  } finally {
    $('#send-btn').disabled = false;
    $('#chat-input').focus();
  }
}

$('#chat-form').addEventListener('submit', (event) => {
  event.preventDefault();
  sendMessage($('#chat-input').value);
});
$('#chat-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
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
  $('#session-memory-status').textContent = '正在提炼素材、想法、目标和实体关系，并写入个人知识库…';
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

function resetConversationView() {
  conversation = [];
  currentConversationId = null;
  sessionNeedsOrganization = false;
  $('#messages').innerHTML = initialWelcomeMarkup;
  $('#organize-session').disabled = true;
  $('#session-memory-status').textContent = '本次会话结束后，将自动提炼素材、想法、目标与关系';
  bindPromptButtons();
  loadConversations();
}

bindPromptButtons();
$('#organize-session').addEventListener('click', () => organizeCurrentConversation(true));
$('#new-chat').addEventListener('click', async () => {
  if (sessionNeedsOrganization) await organizeCurrentConversation();
  resetConversationView();
});
$('#mobile-menu').addEventListener('click', () => $('.sidebar').classList.toggle('open'));

const productionStages = {
  concept: {
    avatar: '策', role: '策划 AGENT · 已完成', status: '总导演已通过',
    title: '核心钩子成立：一次错过，改变两个人的十年',
    summary: '目标受众偏好高密度情绪与明确悬念。用“同站台却没见面”的视觉事件开场，能在前 6 秒建立冲突，并自然引出那封未寄出的信。',
    evidence: ['✓ 前 6 秒出现人物、关系与缺口', '✓ 单集目标可在 100 秒内闭环', '✓ 结尾保留下一集追看问题'],
    deliverable: '项目圣经 · 核心命题与受众假设 v2', meta: '题材：都市情感 · 核心情绪：遗憾 · 单集钩子：错身',
  },
  script: {
    avatar: '文', role: '编剧 AGENT · 已完成', status: '总导演已通过',
    title: '第 3 集的情绪峰值已落在第 78 秒',
    summary: '本集按“误以为等到—确认错过—发现信件”三段推进。删去解释性旁白后，让动作和道具承担信息，能让竖屏观看中的情绪转折更清晰。',
    evidence: ['✓ 每 15–20 秒出现一次新信息', '✓ 台词长度适配口型与节奏', '✓ 人物行为符合前两集动机'],
    deliverable: '第 03 集拍摄剧本 v5', meta: '8 场 · 24 镜 · 预计 1′42″ · 台词 312 字',
  },
  assets: {
    avatar: '角', role: '美术 AGENT · 已完成', status: '资产已锁定',
    title: '角色、场景与视觉规则已建立统一锚点',
    summary: '两位主角均已生成正面、侧面、背面和表情参考；雨夜公交站的色温、雨量与广告灯箱位置写入资产锁，后续镜头不得重新想象这些稳定特征。',
    evidence: ['✓ 每位角色至少 3 个角度参考', '✓ 服装与关键道具具有唯一编号', '✓ 场景光位与色板已锁定'],
    deliverable: '角色与场景资产包 v4', meta: '2 位角色 · 3 个场景 · 7 件关键道具 · 18 张参考',
  },
  storyboard: {
    avatar: '镜', role: '分镜导演 AGENT · 正在工作', status: '等待你确认',
    title: '第 12 镜：反打会削弱“错过”的力度',
    summary: '如果现在切到林夏的正面表情，观众会提前获得情绪答案。保留车窗倒影，让她的反应晚 1.5 秒出现，能把“差一点看见”的遗憾留给观众。',
    evidence: ['✓ 与第 11 镜的视线方向连续', '✓ 发型、耳饰、雨量与资产库一致', '! 镜头时长建议从 4.0s 调整为 5.5s'],
    deliverable: 'SHOT 12 · 车窗倒影构图 v3', meta: '景别：近景 · 焦段：85mm · 运镜：缓慢侧移',
  },
  video: {
    avatar: '帧', role: '摄影 AGENT · 等待上游', status: '等待分镜锁定',
    title: '镜头生成策略已准备，先做关键帧再驱动视频',
    summary: '为控制人物漂移，将先用锁定资产生成每镜起止关键帧，再按 3–6 秒的镜头颗粒度生成动态素材。复杂双人表演会拆成单人反应镜头组合。',
    evidence: ['✓ 人物与场景参考将自动附加', '✓ 每镜生成参数可追溯', '! 双人同框镜头需要优先抽检'],
    deliverable: '24 镜生成任务清单', meta: '关键帧 41 张 · 视频任务 24 个 · 预计 3 轮候选',
  },
  audio: {
    avatar: '声', role: '声音 AGENT · 等待上游', status: '等待画面时长',
    title: '对白、环境声与音乐将围绕情绪节拍分层',
    summary: '先锁定角色声纹与表演意图，再做对白和口型；雨声、公交制动和信纸摩擦作为叙事声音，音乐只在人物确认错过后进入。',
    evidence: ['✓ 角色声纹与年龄匹配', '✓ 对白情绪标注已完成', '! 配乐进入点依赖最终镜头时长'],
    deliverable: '声音设计表 v2', meta: '对白 18 条 · 环境声 6 轨 · 音乐主题 1 个',
  },
  edit: {
    avatar: '剪', role: '剪辑 AGENT · 等待上游', status: '等待可用素材',
    title: '粗剪将先验证叙事，再进入精剪与包装',
    summary: '先按分镜顺序建立无特效粗剪，检查信息是否可懂、节奏是否拖沓；通过后再处理口型、转场、字幕、调色与混音，避免对无效镜头过早精修。',
    evidence: ['✓ 粗剪与精剪验收分离', '✓ 竖屏字幕安全区已设定', '✓ 声画同步将逐镜检查'],
    deliverable: '时间线模板 · Episode 03', meta: '9:16 · 25fps · 1080×1920 · 峰值响度 -1 dBTP',
  },
  quality: {
    avatar: '审', role: '监制 AGENT · 等待上游', status: '等待终版',
    title: '终审将覆盖叙事、连续性、技术与合规四道门',
    summary: '任何角色漂移、穿帮、闪烁、口型错位或关键情节不清都会退回对应 Agent，而不是在最终导出阶段用模糊处理掩盖。发布版还需完成版权、肖像与平台规范检查。',
    evidence: ['✓ 逐镜连续性检查表已创建', '✓ 技术与内容合规独立验收', '✓ 问题可回溯到责任 Agent'],
    deliverable: '终审与发布清单', meta: '叙事 6 项 · 连续性 9 项 · 技术 8 项 · 合规 7 项',
  },
};

function switchWorkspace(mode) {
  const isStudio = mode === 'studio';
  $('#chat-workspace').classList.toggle('hidden', isStudio);
  $('#studio-workspace').classList.toggle('hidden', !isStudio);
  $('#chat-sidebar-context').classList.toggle('hidden', isStudio);
  $('#studio-sidebar-context').classList.toggle('hidden', !isStudio);
  $('#memory-btn').classList.toggle('hidden', isStudio);
  $('#workspace-title').textContent = isStudio ? 'AI 导演工作室' : '知识对话';
  $('#workspace-subtitle').textContent = isStudio ? '一人导演，一组 Agent 把关' : '边问边沉淀你的个人知识库';
  document.querySelectorAll('[data-workspace]').forEach((button) => {
    button.classList.toggle('active', button.dataset.workspace === mode);
  });
  $('.sidebar').classList.remove('open');
  window.localStorage.setItem('assistant-workspace', mode);
}

function renderProductionStage(key) {
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
document.querySelectorAll('[data-stage]').forEach((button) => {
  button.addEventListener('click', () => renderProductionStage(button.dataset.stage));
});
document.querySelectorAll('.shot-card').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.shot-card').forEach((item) => item.classList.remove('selected'));
    button.classList.add('selected');
  });
});
$('#continue-production').addEventListener('click', () => {
  renderProductionStage('storyboard');
  $('#stage-title').scrollIntoView({ behavior: 'smooth', block: 'center' });
});
$('#approve-stage').addEventListener('click', () => {
  const current = document.querySelector('[data-stage][aria-pressed="true"]');
  if (!current) return;
  current.classList.add('done');
  current.querySelector('em').textContent = '已通过';
  $('#stage-status').textContent = '已采纳，正在流转';
  notify('总导演已接收，本环节交付物已进入下一关');
});
$('#request-revision').addEventListener('click', () => notify('已创建修改意见，负责 Agent 将保留当前版本并生成新方案'));
$('#view-deliverable').addEventListener('click', () => $('#shot-heading').scrollIntoView({ behavior: 'smooth', block: 'start' }));
$('#new-project').addEventListener('click', () => notify('新项目向导：将从一句故事创意开始'));
$('#project-settings').addEventListener('click', () => notify('制作设定包含画幅、时长、风格、预算与发布平台'));
$('#show-all-shots').addEventListener('click', () => notify('全片共 24 镜，当前展示场次 07 的关键镜头'));

const initialWorkspace = window.localStorage.getItem('assistant-workspace');
if (initialWorkspace === 'studio') switchWorkspace('studio');

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
