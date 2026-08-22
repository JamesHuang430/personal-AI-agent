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
const customModelValue = '__custom__';
const resetToken = new URLSearchParams(window.location.hash.slice(1)).get('reset_token');

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
      button.addEventListener('click', () => openConversation(item.id));
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
    const [graph, memories] = await Promise.all([api('/memory/graph'), api('/memory/items')]);
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
document.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => sendMessage(button.dataset.prompt)));
$('#new-chat').addEventListener('click', () => {
  conversation = [];
  currentConversationId = null;
  $('#messages').innerHTML = '<div class="welcome-block"><div class="assistant-logo">知</div><h2>新的对话</h2><p>随时开始，我在这里。</p></div>';
  loadConversations();
});
$('#mobile-menu').addEventListener('click', () => $('.sidebar').classList.toggle('open'));

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
