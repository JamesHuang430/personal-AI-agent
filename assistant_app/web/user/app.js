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
  loadVideoGallery();
  loadDirectorProjects();
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
      addMessage(item.role, item.content, meta);
    }
    sessionNeedsOrganization = false;
    $('#organize-session').disabled = false;
    $('#session-memory-status').textContent = '历史会话已载入；可以继续对话，或再次整理到个人知识库';
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

function renderArtifacts(message, result) {
  const files = result.files || [];
  const jobs = result.video_jobs || [];
  const musicJobs = result.music_jobs || [];
  const speechJobs = result.speech_jobs || [];
  const directorProjects = result.director_projects || [];
  if (!files.length && !jobs.length && !musicJobs.length && !speechJobs.length && !directorProjects.length) return;
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
    card.innerHTML = `<div><strong>🎬 导演项目已启动</strong><small>总导演编排器 + 4 位执行 Agent · ${escapeHtml(project.aspect_ratio)} · ${escapeHtml(project.resolution || '768P')} · ${project.target_seconds} 秒</small></div><a href="#">打开工作室</a>`;
    card.querySelector('a').addEventListener('click', (event) => {
      event.preventDefault();
      switchWorkspace('studio');
      loadDirectorProject(project.id);
    });
    list.appendChild(card);
  }
  message.querySelector('.message-bubble').appendChild(list);
}

async function pollSpeechJob(jobId, card) {
  try {
    const job = await api(`/speech/${jobId}`);
    const statusText = { queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[job.status] || job.status;
    const duration = job.duration_ms ? ` · ${(job.duration_ms / 1000).toFixed(1)} 秒` : '';
    card.querySelector('small').textContent = `${statusText} · ${job.voice_id}${duration}`;
    if (job.status === 'completed') {
      card.querySelector('a').textContent = '下载语音';
      card.querySelector('a').href = job.download_url;
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
  try {
    const job = await api(`/music/${jobId}`);
    const statusText = { queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[job.status] || job.status;
    const duration = job.duration_ms ? ` · ${Math.round(job.duration_ms / 1000)} 秒` : '';
    card.querySelector('small').textContent = `${statusText} · ${job.is_instrumental ? '纯配乐' : '带人声歌曲'}${duration}`;
    if (job.status === 'completed') {
      card.querySelector('a').textContent = '下载音乐';
      card.querySelector('a').href = job.download_url;
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
  try {
    const job = await api(`/videos/${jobId}`);
    const statusText = { queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[job.status] || job.status;
    card.querySelector('small').textContent = `${statusText} · ${job.seconds} 秒 · ${job.size} · ${job.resolution || '768P'}`;
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
  addMessage('user', content);
  conversation.push({ role: 'user', content });
  $('#chat-input').value = '';
  const controller = new AbortController();
  activeChatController = controller;
  setChatGenerating(true);
  const pending = addMessage('assistant', '正在思考…');
  try {
    const result = await api('/chat', {
      method: 'POST',
      body: JSON.stringify({ model: selectedModel, message: content, conversation_id: currentConversationId }),
      signal: controller.signal,
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
    if (error.name === 'AbortError') {
      pending.querySelector('.message-bubble').textContent = '已停止生成。你的提问仍保存在最近对话中，可以稍后重新发送。';
      sessionNeedsOrganization = true;
      $('#session-memory-status').textContent = '本轮回复已停止；已发送的内容仍会保存在会话中';
      loadConversations();
      notify('已停止当前回复');
    } else {
      pending.remove();
      addMessage('assistant', `暂时无法回答：${error.message}`);
    }
  } finally {
    if (activeChatController === controller) activeChatController = null;
    setChatGenerating(false);
    $('#chat-input').focus();
  }
}

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

function resetConversationView(reloadHistory = true) {
  conversation = [];
  currentConversationId = null;
  sessionNeedsOrganization = false;
  $('#messages').innerHTML = initialWelcomeMarkup;
  $('#organize-session').disabled = true;
  $('#session-memory-status').textContent = '本次会话结束后，将自动提炼素材、想法、目标与关系';
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
    avatar: '镜', role: '视觉 AGENT · 结构化规划', status: '等待上游',
    title: '连续性、分镜、台词与视频提示词', summary: '逐镜方案会直接驱动视频生成、语音生成和字幕烧录。',
    evidence: ['✓ 连续性圣经', '✓ 独立镜头提示词', '✓ 每镜语音与字幕'], deliverable: '视觉 JSON', meta: '资产 / 分镜 / speech_text',
  },
  media: {
    avatar: '制', role: '媒体制作 AGENT · 工具执行', status: '等待上游',
    title: '真实视频、语音、混音和字幕', summary: '调用已配置的视频与语音渠道，再由 FFmpeg 合成带对白和烧录字幕的镜头。',
    evidence: ['✓ 视频渠道任务', '✓ Speech 配音任务', '✓ FFmpeg 混音与字幕'], deliverable: '可播放 MP4', meta: '视频轨 / 语音轨 / 烧录字幕',
  },
  quality: {
    avatar: '审', role: '质检 AGENT · 否决门禁', status: '等待媒体',
    title: '真实文件技术质检', summary: '通过 ffprobe 检查视频轨、语音轨、字幕标记和时长；失败时项目不会完成。',
    evidence: ['✓ 视频轨', '✓ 语音轨', '✓ 字幕与时长'], deliverable: '结构化质检报告', meta: 'pass / reject / issues',
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
  setSidebarOpen(false);
  window.localStorage.setItem('assistant-workspace', mode);
  if (isStudio && currentUser) {
    loadVideoGallery();
    loadDirectorProjects();
  }
}

function directorStatusLabel(status, oneClick = false) {
  return { queued: '准备派单', processing: 'Agent 制作中', completed: oneClick ? '一键成片完成' : '预制作完成', failed: '制作失败' }[status] || status;
}

function directorAgentStatusLabel(status) {
  return { pending: '等待上游', processing: '正在工作', completed: '已通过', failed: '需要处理' }[status] || status;
}

async function loadDirectorProjects() {
  if (!currentUser) return;
  try {
    const projects = await api('/director/projects');
    if (!projects.length) return;
    if (!activeDirectorProject || !projects.some((item) => item.id === activeDirectorProject.id)) {
      renderDirectorProject(projects[0]);
    }
  } catch (error) {
    notify(error.message);
  }
}

async function loadDirectorProject(projectId) {
  window.clearTimeout(directorProjectTimer);
  try {
    const project = await api(`/director/projects/${projectId}`);
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
  $('#production-kicker').innerHTML = `<span class="live-dot"></span> DIRECTOR PROJECT · ${escapeHtml(project.id.slice(0, 8).toUpperCase())}`;
  $('#production-title').textContent = project.title;
  const modeLabel = project.one_click ? '一键成片' : '逐镜制作';
  const shotProgress = project.planned_shots ? ` · ${project.completed_shots}/${project.planned_shots} 镜` : '';
  $('#production-meta').textContent = `${project.visual_style} · ${project.aspect_ratio} · ${project.resolution || '768P'} · ${modeLabel} · 目标 ${project.target_seconds} 秒${shotProgress}`;
  $('#director-working-badge').textContent = directorStatusLabel(project.status, project.one_click);
  $('.director-progress strong').textContent = `${project.progress}%`;
  $('.director-progress i').style.width = `${project.progress}%`;

  const directorRun = project.agents.find((run) => run.agent === 'story');
  const activeRun = project.agents.find((run) => run.agent === project.current_stage);
  $('#director-heading').textContent = project.status === 'completed'
    ? (project.one_click ? '总导演已完成全片生成与合片交付' : '总导演已完成预制作与首镜交付')
    : project.status === 'failed'
      ? '导演项目暂停，需要处理失败任务'
      : `${activeRun?.agent_name || '总导演 Agent'}正在处理当前交付物`;
  $('#director-summary').textContent = project.error_message
    || project.final_summary
    || directorRun?.decision_summary
    || '总导演编排器正在调度 4 位执行 Agent。页面展示结构化交付和真实工具执行结果。';

  document.querySelectorAll('[data-stage]').forEach((button) => {
    const run = project.agents.find((item) => item.agent === button.dataset.stage);
    button.classList.toggle('done', run?.status === 'completed');
    button.classList.toggle('active', run?.status === 'processing' || run?.status === 'failed');
    button.setAttribute('aria-pressed', String(run?.agent === project.current_stage));
    if (run) button.querySelector('em').textContent = directorAgentStatusLabel(run.status);
  });

  const sidebarProject = document.querySelector('.project-item.active');
  if (sidebarProject) {
    sidebarProject.querySelector('strong').textContent = project.title;
    sidebarProject.querySelector('small').textContent = `${directorStatusLabel(project.status, project.one_click)} · ${project.progress}%`;
  }

  renderContinuityBible(project);
  renderDirectorShots(project);
  if (activeRun) renderProductionStage(activeRun.agent);
  window.clearTimeout(directorProjectTimer);
  if (['queued', 'processing'].includes(project.status)) {
    directorProjectTimer = window.setTimeout(() => loadDirectorProject(project.id), 3500);
  }
}

function renderContinuityBible(project) {
  const bible = project.continuity_bible || {};
  const characters = Array.isArray(bible.characters) ? bible.characters : [];
  const relationships = Array.isArray(bible.relationships) ? bible.relationships : [];
  const references = characters.filter((character) => character.reference_image_url).length;
  const score = characters.length ? Math.min(96, 80 + Math.round((references / characters.length) * 16)) : 0;
  $('#continuity-score-value').textContent = characters.length ? String(score) : '—';
  $('#continuity-score-label').textContent = characters.length
    ? (references === characters.length ? '定妆参考已登记 · 等待兼容模型硬锁' : '文字连续性已锁定 · 定妆参考待补')
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
  if (!project.shots?.length) return;
  $('#director-shot-strip').innerHTML = project.shots.map((shot) => `
    <button class="shot-card ${shot.status === 'completed' ? 'approved' : shot.status === 'processing' ? 'selected' : ''}" type="button" data-video-job="${escapeHtml(shot.video?.id || '')}">
      <div class="shot-frame frame-wide"><span>${shot.sequence}</span><i></i></div>
      <strong>${escapeHtml(shot.title)}</strong><small>${escapeHtml(shot.seconds)}s · ${escapeHtml(videoStatusLabel(shot.status))}${shot.has_burned_subtitles ? ' · 配音+字幕' : ''}</small>
      ${shot.speech_text ? `<small>${escapeHtml(shot.speaker || '旁白')}：${escapeHtml(shot.speech_text)}</small>` : ''}
    </button>`).join('');
  $('#shot-heading').nextElementSibling.textContent = `${project.completed_shots} / ${project.planned_shots} 镜已生成`;
}

function showDirectorStart(oneClick = false) {
  directorOneClickMode = oneClick;
  const title = $('#director-start-dialog h2');
  title.textContent = oneClick ? '一键成片' : '开始制作电影';
  $('#director-start-intro').textContent = oneClick
    ? '总导演编排器将调度 4 位执行 Agent，逐镜生成视频和语音、烧录字幕并最终合片。'
    : '总导演编排器会生成一个带真实配音和烧录字幕的预览镜头。';
  updateDirectorModeSummary();
  $('#director-start-dialog').showModal();
  window.setTimeout(() => $('#director-premise').focus(), 80);
}

function updateDirectorModeSummary() {
  const seconds = Number($('#director-duration').value);
  const resolution = $('#director-resolution').value;
  const estimatedShots = Math.ceil(seconds / 12);
  const panel = $('#director-start-boundary');
  panel.classList.toggle('one-click', directorOneClickMode);
  panel.querySelector('strong').textContent = directorOneClickMode ? '一键成片 · 额度确认' : '常规制作 · 先看预览';
  panel.querySelector('span').textContent = directorOneClickMode
    ? `预计以 ${resolution} 调用视频模型生成约 ${estimatedShots} 个片段，再自动合成为约 ${seconds} 秒影片。2K 会增加生成耗时和额度消耗；定妆照仅在兼容主体参考模型时可硬锁，仍需质检 Agent 检查漂移。`
    : `由 4 位执行 Agent 以 ${resolution} 生成第一个带配音和字幕的 4 秒预览镜头，额度更可控。`;
  $('#director-start-submit').textContent = directorOneClickMode ? `确认并生成约 ${estimatedShots} 个镜头` : '启动总导演编排器 + 4 位 Agent';
}

function videoStatusLabel(status) {
  return { queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[status] || status;
}

function videoCreatedAt(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false });
}

function renderStudioVideo(job) {
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
  heading.innerHTML = `<strong>真实生成镜头</strong><em>${escapeHtml(videoStatusLabel(job.status))}</em>`;
  const meta = document.createElement('small');
  meta.textContent = `${job.seconds} 秒 · ${job.size} · ${job.resolution || '768P'}${videoCreatedAt(job.created_at) ? ` · ${videoCreatedAt(job.created_at)}` : ''}`;
  const prompt = document.createElement('p');
  prompt.textContent = job.prompt;
  detail.append(heading, meta, prompt);

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
    const finalMovies = projects.filter((project) => project.final_video);
    list.replaceChildren();
    if (!jobs.length && !finalMovies.length) {
      list.innerHTML = '<div class="video-gallery-empty"><span>▶</span><strong>还没有真实成片</strong><small>视频任务完成后会自动出现在这里</small></div>';
      summary.textContent = '生成中的视频会自动刷新，完成后可直接播放和下载';
      return;
    }

    const visibleJobs = jobs.slice(0, 12);
    list.append(...finalMovies.map(renderDirectorMovie), ...visibleJobs.map(renderStudioVideo));
    const completed = jobs.filter((job) => job.status === 'completed').length;
    const active = jobs.filter((job) => ['queued', 'processing'].includes(job.status)).length;
    summary.textContent = `${finalMovies.length} 部合片 · ${completed} 个生成片段${active ? ` · ${active} 条正在生成` : ''} · 仅你本人可查看`;
    if (active) videoGalleryTimer = window.setTimeout(loadVideoGallery, 5000);
  } catch (error) {
    list.innerHTML = `<div class="video-gallery-empty failed"><span>!</span><strong>成片记录加载失败</strong><small>${escapeHtml(error.message)}</small></div>`;
    summary.textContent = '暂时无法读取真实生成记录';
  } finally {
    refresh.disabled = false;
  }
}

function renderProductionStage(key) {
  const liveRun = activeDirectorProject?.agents.find((run) => run.agent === key);
  if (liveRun) {
    const fallback = productionStages[key];
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
  const current = document.querySelector('[data-stage][aria-pressed="true"]');
  renderProductionStage(current?.dataset.stage || 'story');
  $('#stage-title').scrollIntoView({ behavior: 'smooth', block: 'center' });
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
  detail.textContent = '确认是否已经同时启用视频和语音渠道。';
  $('#guide-refresh-status').disabled = true;
  try {
    const result = await api('/videos/status');
    panel.classList.remove('checking');
    panel.classList.add(result.ready ? 'ready' : 'blocked');
    mark.textContent = result.ready ? '✓' : '!';
    title.textContent = result.ready
      ? `可以真实生成 · ${result.model}`
      : '暂时不能真实生成 · 视频或语音渠道未启用';
    const audio = result.native_audio ? '原生音轨会降至背景音量，并混入独立 Speech 配音。' : '系统会混入独立 Speech 配音。';
    detail.textContent = `${audio}中文字幕将直接烧录进画面。${latestVideoJobText(result.latest_job)}`;
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
$('#one-click-movie').addEventListener('click', () => showDirectorStart(true));
$('#project-settings').addEventListener('click', () => showDirectorStart(false));
$('#show-all-shots').addEventListener('click', () => notify('全片共 24 镜，当前展示场次 07 的关键镜头'));
$('#refresh-video-gallery').addEventListener('click', loadVideoGallery);
$('#director-start-close').addEventListener('click', () => $('#director-start-dialog').close());
$('#director-start-cancel').addEventListener('click', () => $('#director-start-dialog').close());
$('#director-duration').addEventListener('change', updateDirectorModeSummary);
$('#director-resolution').addEventListener('change', updateDirectorModeSummary);
$('#director-start-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const seconds = Number($('#director-duration').value);
  const estimatedShots = Math.ceil(seconds / 12);
  const resolution = $('#director-resolution').value;
  if (directorOneClickMode && !window.confirm(`一键成片将以 ${resolution} 生成约 ${estimatedShots} 个视频片段，会消耗 MiniMax 额度并可能需要较长时间。确认继续吗？`)) return;
  const button = $('#director-start-submit');
  button.disabled = true;
  button.textContent = '正在匹配 Agent 模型…';
  try {
    const project = await api('/director/projects', {
      method: 'POST',
      body: JSON.stringify({
        premise: $('#director-premise').value.trim(),
        target_seconds: Number($('#director-duration').value),
        aspect_ratio: $('#director-ratio').value,
        resolution,
        visual_style: $('#director-style').value,
        continuity_notes: $('#director-continuity-notes').value.trim(),
        one_click: directorOneClickMode,
      }),
    });
    $('#director-start-dialog').close();
    renderDirectorProject(project);
    switchWorkspace('studio');
    notify(directorOneClickMode
      ? `一键成片已启动：4 位执行 Agent 将以 ${resolution} 生成约 ${estimatedShots} 个带配音和字幕的镜头并自动合片`
      : '导演项目已启动：总导演开始派发 8 道专业任务');
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
