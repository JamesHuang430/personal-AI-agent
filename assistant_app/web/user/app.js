const $ = (selector) => document.querySelector(selector);
const authView = $('#auth-view');
const appView = $('#app-view');
const toast = $('#toast');
let authMode = 'login';
let currentUser = null;
let conversation = [];
let userCaptchaId = '';
let resetCaptchaId = '';
let registrationCodeSent = false;
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
  addMessage('user', content);
  const priorHistory = conversation.slice(-20);
  conversation.push({ role: 'user', content });
  $('#chat-input').value = '';
  $('#send-btn').disabled = true;
  const pending = addMessage('assistant', '正在思考…');
  try {
    const result = await api('/chat', {
      method: 'POST',
      body: JSON.stringify({ message: content, history: priorHistory }),
    });
    pending.remove();
    const responseMessage = addMessage('assistant', result.content, `${result.channel} · ${result.model}`);
    renderArtifacts(responseMessage, result);
    conversation.push({ role: 'assistant', content: result.content });
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
  $('#messages').innerHTML = '<div class="welcome-block"><div class="assistant-logo">知</div><h2>新的对话</h2><p>随时开始，我在这里。</p></div>';
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
