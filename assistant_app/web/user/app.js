const $ = (selector) => document.querySelector(selector);
const authView = $('#auth-view');
const appView = $('#app-view');
const toast = $('#toast');
let authMode = 'login';
let currentUser = null;
let conversation = [];

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
  $('#login-tab').classList.toggle('active', mode === 'login');
  $('#register-tab').classList.toggle('active', mode === 'register');
  $('#auth-title').textContent = mode === 'login' ? '登录你的助理' : '创建你的助理';
  $('#auth-subtitle').textContent = mode === 'login' ? '继续上次的对话和计划' : '每天仅开放 3 个新用户名额';
  $('#auth-submit').textContent = mode === 'login' ? '进入知伴' : '注册并进入';
  $('#password').autocomplete = mode === 'login' ? 'current-password' : 'new-password';
  $('#register-tip').classList.toggle('hidden', mode !== 'register');
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
$('#auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#auth-submit');
  button.disabled = true;
  try {
    const user = await api(`/auth/${authMode}`, {
      method: 'POST',
      body: JSON.stringify({ email: $('#email').value, password: $('#password').value }),
    });
    showApp(user);
    notify(authMode === 'register' ? '注册成功，欢迎加入' : '登录成功');
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
    addMessage('assistant', result.content, `${result.channel} · ${result.model}`);
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

loadSession();
