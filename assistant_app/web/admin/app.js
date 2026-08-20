const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let packagesCache = [];
let channelsCache = [];
let videoChannelsCache = [];

async function api(path, options = {}) {
  const response = await fetch(`/api/v1/admin${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败（${response.status}）`);
  return body;
}
function escapeHtml(value) { const node = document.createElement('div'); node.textContent = value ?? ''; return node.innerHTML; }
function notify(message) { const toast=$('#toast'); toast.textContent=message; toast.classList.remove('hidden'); clearTimeout(notify.timer); notify.timer=setTimeout(()=>toast.classList.add('hidden'),2800); }
function formatDate(value) { return value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '—'; }

async function establishSession() {
  try {
    const session = await api('/auth/session');
    $('#admin-name').textContent = session.username;
    $('#login-view').classList.add('hidden');
    $('#console-view').classList.remove('hidden');
    await loadOverview();
  } catch { $('#login-view').classList.remove('hidden'); }
}
$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const result = await api('/auth/login',{method:'POST',body:JSON.stringify({username:$('#username').value,password:$('#admin-password').value})});
    $('#admin-name').textContent=result.username; $('#login-view').classList.add('hidden'); $('#console-view').classList.remove('hidden'); await loadOverview(); notify('登录成功');
  } catch(error){notify(error.message);}
});
$('#logout').addEventListener('click',async()=>{await api('/auth/logout',{method:'POST'}).catch(()=>null);location.reload();});

const pageNames={overview:'概览',users:'用户管理',packages:'积分套餐',channels:'大模型渠道','video-channels':'视频生成渠道'};
$$('.nav-item').forEach((button)=>button.addEventListener('click',async()=>{
  $$('.nav-item').forEach(item=>item.classList.remove('active')); button.classList.add('active');
  $$('.page').forEach(page=>page.classList.add('hidden')); $(`#page-${button.dataset.page}`).classList.remove('hidden'); $('#page-name').textContent=pageNames[button.dataset.page];
  if(button.dataset.page==='overview')await loadOverview(); if(button.dataset.page==='users')await loadUsers(); if(button.dataset.page==='packages')await loadPackages(); if(button.dataset.page==='channels')await loadChannels(); if(button.dataset.page==='video-channels')await loadVideoChannels();
}));

async function loadOverview(){
  try{const data=await api('/stats');const cards=[['注册用户',data.total_users,'#6657e8'],['正常用户',data.active_users,'#19a77d'],['今日注册',`${data.registrations_today} / 3`,'#e79822'],['上架套餐',data.active_packages,'#348bd5']];$('#stats').innerHTML=cards.map(([label,value,color])=>`<article class="stat-card" style="--accent:${color}"><div class="stat-label">${label}</div><div class="stat-value">${value}</div></article>`).join('');}catch(error){notify(error.message);}
}
$$('.refresh').forEach(button=>button.addEventListener('click',loadOverview));

async function loadUsers(){
  try{const data=await api(`/users?query=${encodeURIComponent($('#user-search').value)}`);$('#user-empty').classList.toggle('hidden',data.items.length>0);$('#user-table').innerHTML=data.items.map(user=>`<tr><td><div class="user-email">${escapeHtml(user.email)}</div><small>${user.id.slice(0,8)}</small></td><td><strong>${user.points.toLocaleString('zh-CN')}</strong></td><td><span class="badge ${user.is_active?'active':'inactive'}">${user.is_active?'正常':'停用'}</span></td><td>${formatDate(user.created_at)}</td><td>${formatDate(user.last_login_at)}</td><td><div class="actions"><button class="small-btn" data-action="points" data-id="${user.id}">调积分</button><button class="small-btn ${user.is_active?'danger':''}" data-action="status" data-id="${user.id}" data-active="${user.is_active}">${user.is_active?'停用':'启用'}</button></div></td></tr>`).join('');}catch(error){notify(error.message);}
}
$('#user-search-btn').addEventListener('click',loadUsers);$('#user-search').addEventListener('keydown',event=>{if(event.key==='Enter')loadUsers();});
$('#user-table').addEventListener('click',async(event)=>{
  const button=event.target.closest('button');if(!button)return;
  try{
    if(button.dataset.action==='status'){const active=button.dataset.active==='true';await api(`/users/${button.dataset.id}/status`,{method:'PATCH',body:JSON.stringify({is_active:!active})});notify(active?'用户已停用':'用户已启用');}
    if(button.dataset.action==='points'){const delta=window.prompt('请输入积分调整值（增加填正数，扣减填负数）');if(delta===null)return;const note=window.prompt('请输入调整原因','运营调整')||'运营调整';await api(`/users/${button.dataset.id}/points`,{method:'POST',body:JSON.stringify({delta:Number(delta),note})});notify('积分调整成功');}
    await loadUsers();
  }catch(error){notify(error.message);}
});

function resetPackageForm(){ $('#package-form').reset();$('#package-id').value='';$('#package-sort').value='0';$('#package-active').checked=true;$('#package-save').textContent='新增套餐';$('#package-cancel').classList.add('hidden'); }
async function loadPackages(){try{packagesCache=await api('/packages');$('#package-grid').innerHTML=packagesCache.map(item=>`<article class="package-card"><div class="card-head"><h3>${escapeHtml(item.name)}</h3><span class="badge ${item.is_active?'active':'inactive'}">${item.is_active?'已上架':'已停用'}</span></div><div class="package-price">¥${item.price_yuan}</div><div class="package-points">${item.points.toLocaleString('zh-CN')} 积分</div><div class="card-actions"><button class="small-btn" data-package-edit="${item.id}">编辑</button></div></article>`).join('');}catch(error){notify(error.message);}}
$('#package-form').addEventListener('submit',async(event)=>{event.preventDefault();const id=$('#package-id').value;const payload={name:$('#package-name').value,price_yuan:Number($('#package-price').value),points:Number($('#package-points').value),sort_order:Number($('#package-sort').value),is_active:$('#package-active').checked};try{await api(id?`/packages/${id}`:'/packages',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});notify(id?'套餐已更新':'套餐已新增');resetPackageForm();await loadPackages();}catch(error){notify(error.message);}});
$('#package-grid').addEventListener('click',event=>{const button=event.target.closest('[data-package-edit]');if(!button)return;const item=packagesCache.find(row=>row.id===button.dataset.packageEdit);$('#package-id').value=item.id;$('#package-name').value=item.name;$('#package-price').value=item.price_yuan;$('#package-points').value=item.points;$('#package-sort').value=item.sort_order;$('#package-active').checked=item.is_active;$('#package-save').textContent='保存修改';$('#package-cancel').classList.remove('hidden');scrollTo({top:0,behavior:'smooth'});});
$('#package-cancel').addEventListener('click',resetPackageForm);

function resetChannelForm(){ $('#channel-form').reset();$('#channel-id').value='';$('#channel-model').value='gpt-4o-mini';$('#channel-qps').value='2';$('#channel-key').required=true;$('#channel-key').placeholder='sk-...';$('#channel-save').textContent='新增渠道';$('#channel-cancel').classList.add('hidden'); }
async function loadChannels(){try{channelsCache=await api('/model-channels');$('#channel-grid').innerHTML=channelsCache.map(item=>`<article class="channel-card ${item.is_active?'active':''}"><div class="card-head"><div><h3>${escapeHtml(item.name)}</h3><span class="badge ${item.is_active?'active':'inactive'}">${item.is_active?'当前渠道':'未启用'}</span></div><span>🔐</span></div><div class="channel-url" title="${escapeHtml(item.base_url)}">${escapeHtml(item.base_url)}</div><div class="channel-meta"><div><small>模型</small><strong>${escapeHtml(item.model_name)}</strong></div><div><small>QPS 上限</small><strong>${item.qps_limit}</strong></div></div><div class="card-actions"><button class="small-btn" data-channel-edit="${item.id}">编辑</button>${item.is_active?`<button class="small-btn danger" data-channel-disable="${item.id}">停用</button>`:`<button class="small-btn" data-channel-activate="${item.id}">设为当前</button>`}</div></article>`).join('')||'<p class="muted">尚未配置模型渠道，先在上方新增一个。</p>';}catch(error){notify(error.message);}}
$('#channel-form').addEventListener('submit',async(event)=>{event.preventDefault();const id=$('#channel-id').value;const payload={name:$('#channel-name').value,base_url:$('#channel-url').value,model_name:$('#channel-model').value,qps_limit:Number($('#channel-qps').value)};if(!id||$('#channel-key').value)payload.api_key=$('#channel-key').value;if(!id)payload.is_active=$('#channel-active').checked;try{await api(id?`/model-channels/${id}`:'/model-channels',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});notify(id?'渠道已更新':'渠道已新增');resetChannelForm();await loadChannels();}catch(error){notify(error.message);}});
$('#channel-grid').addEventListener('click',async(event)=>{const edit=event.target.closest('[data-channel-edit]');const activate=event.target.closest('[data-channel-activate]');const disable=event.target.closest('[data-channel-disable]');try{if(edit){const item=channelsCache.find(row=>row.id===edit.dataset.channelEdit);$('#channel-id').value=item.id;$('#channel-name').value=item.name;$('#channel-url').value=item.base_url;$('#channel-model').value=item.model_name;$('#channel-qps').value=item.qps_limit;$('#channel-key').value='';$('#channel-key').required=false;$('#channel-key').placeholder='留空则保持原 Key';$('#channel-active').checked=item.is_active;$('#channel-active').disabled=true;$('#channel-save').textContent='保存修改';$('#channel-cancel').classList.remove('hidden');scrollTo({top:0,behavior:'smooth'});return;}if(activate){await api(`/model-channels/${activate.dataset.channelActivate}/activate`,{method:'POST'});notify('已切换当前模型渠道');}if(disable){await api(`/model-channels/${disable.dataset.channelDisable}/disable`,{method:'POST'});notify('渠道已停用');}await loadChannels();}catch(error){notify(error.message);}});
$('#channel-cancel').addEventListener('click',()=>{$('#channel-active').disabled=false;resetChannelForm();});

function resetVideoChannelForm(){ $('#video-channel-form').reset();$('#video-channel-id').value='';$('#video-channel-model').value='sora-2';$('#video-channel-qps').value='1';$('#video-channel-seconds').value='4';$('#video-channel-size').value='1280x720';$('#video-channel-key').required=true;$('#video-channel-key').placeholder='sk-...';$('#video-channel-active').disabled=false;$('#video-channel-save').textContent='新增视频渠道';$('#video-channel-cancel').classList.add('hidden'); }
async function loadVideoChannels(){try{videoChannelsCache=await api('/video-channels');$('#video-channel-grid').innerHTML=videoChannelsCache.map(item=>`<article class="channel-card ${item.is_active?'active':''}"><div class="card-head"><div><h3>${escapeHtml(item.name)}</h3><span class="badge ${item.is_active?'active':'inactive'}">${item.is_active?'当前视频渠道':'未启用'}</span></div><span>🔐</span></div><div class="channel-url" title="${escapeHtml(item.base_url)}">${escapeHtml(item.base_url)}</div><div class="channel-meta"><div><small>模型</small><strong>${escapeHtml(item.model_name)}</strong></div><div><small>QPS 上限</small><strong>${item.qps_limit}</strong></div><div><small>默认时长</small><strong>${item.default_seconds} 秒</strong></div><div><small>默认尺寸</small><strong>${item.default_size}</strong></div></div><div class="card-actions"><button class="small-btn" data-video-edit="${item.id}">编辑</button>${item.is_active?`<button class="small-btn danger" data-video-disable="${item.id}">停用</button>`:`<button class="small-btn" data-video-activate="${item.id}">设为当前</button>`}</div></article>`).join('')||'<p class="muted">尚未配置视频生成渠道。</p>';}catch(error){notify(error.message);}}
$('#video-channel-form').addEventListener('submit',async(event)=>{event.preventDefault();const id=$('#video-channel-id').value;const payload={name:$('#video-channel-name').value,base_url:$('#video-channel-url').value,model_name:$('#video-channel-model').value,qps_limit:Number($('#video-channel-qps').value),default_seconds:$('#video-channel-seconds').value,default_size:$('#video-channel-size').value};if(!id||$('#video-channel-key').value)payload.api_key=$('#video-channel-key').value;if(!id)payload.is_active=$('#video-channel-active').checked;try{await api(id?`/video-channels/${id}`:'/video-channels',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});notify(id?'视频渠道已更新':'视频渠道已新增');resetVideoChannelForm();await loadVideoChannels();}catch(error){notify(error.message);}});
$('#video-channel-grid').addEventListener('click',async(event)=>{const edit=event.target.closest('[data-video-edit]');const activate=event.target.closest('[data-video-activate]');const disable=event.target.closest('[data-video-disable]');try{if(edit){const item=videoChannelsCache.find(row=>row.id===edit.dataset.videoEdit);$('#video-channel-id').value=item.id;$('#video-channel-name').value=item.name;$('#video-channel-url').value=item.base_url;$('#video-channel-model').value=item.model_name;$('#video-channel-qps').value=item.qps_limit;$('#video-channel-seconds').value=item.default_seconds;$('#video-channel-size').value=item.default_size;$('#video-channel-key').value='';$('#video-channel-key').required=false;$('#video-channel-key').placeholder='留空则保持原 Key';$('#video-channel-active').checked=item.is_active;$('#video-channel-active').disabled=true;$('#video-channel-save').textContent='保存修改';$('#video-channel-cancel').classList.remove('hidden');scrollTo({top:0,behavior:'smooth'});return;}if(activate){await api(`/video-channels/${activate.dataset.videoActivate}/activate`,{method:'POST'});notify('已切换当前视频渠道');}if(disable){await api(`/video-channels/${disable.dataset.videoDisable}/disable`,{method:'POST'});notify('视频渠道已停用');}await loadVideoChannels();}catch(error){notify(error.message);}});
$('#video-channel-cancel').addEventListener('click',resetVideoChannelForm);

establishSession();
