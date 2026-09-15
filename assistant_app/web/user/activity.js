/* User-facing execution records. All provider text is rendered as escaped text. */
function activityTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('zh-CN', { hour12: false });
}

function activityStatus(value) {
  return ({ processing: '执行中', pending: '等待中', queued: '排队中', completed: '已完成',
    failed: '失败', awaiting_confirmation: '等待故事确认', awaiting_storyboard: '等待分镜确认' })[value] || value;
}

function activityPanel(host, events, { live = false, title = 'Agent 执行记录' } = {}) {
  let panel = host.querySelector(':scope > .execution-panel');
  if (!panel) {
    panel = document.createElement('details');
    panel.className = 'execution-panel';
    panel.open = live;
    host.appendChild(panel);
  }
  const logsOpen = panel.querySelector('.execution-logs')?.open || false;
  const rows = events.slice(-200);
  const failed = rows.some(e => e.status === 'failed');
  panel.innerHTML = `<summary><span>✦ ${escapeHtml(title)}</span><span class="execution-badge ${live ? 'live' : ''}">${live ? '实时' : failed ? '含异常记录' : '可回看'}</span><small>${rows.length} 条记录</small></summary>
    <div class="execution-body"><div class="execution-timeline" aria-label="执行时间线">${rows.map(e => `
      <article class="execution-event ${escapeHtml(e.status)}"><span class="execution-dot"></span>
      <div><strong>${escapeHtml(e.name)}</strong><span class="execution-state">${escapeHtml(activityStatus(e.status))}</span>
      <p>${escapeHtml(e.detail || '')}</p><small>${activityTime(e.time)}${Number.isFinite(e.duration_ms) ? ` · ${(e.duration_ms / 1000).toFixed(2)} 秒` : ''}</small></div></article>`).join('') || '<p class="execution-empty">等待服务器执行记录…</p>'}</div>
      <details class="execution-logs" ${logsOpen ? 'open' : ''}><summary>执行日志 · ${rows.length} 条</summary>
        <pre>${escapeHtml(rows.map(e => `${activityTime(e.time)} ${e.status === 'failed' ? 'ERROR' : 'INFO'} [${e.kind}] ${e.name} · ${activityStatus(e.status)}${e.detail ? ` — ${e.detail}` : ''}`).join('\n') || '暂无日志')}</pre>
      </details><p class="execution-note">记录实际执行步骤与结果；判断摘要由 Agent 单独提供。</p></div>`;
  return panel;
}

function startLiveChatActivity(message, model, key) {
  const bubble = message.querySelector('.message-bubble');
  bubble.innerHTML = '<div class="chat-live-heading" role="status"><span class="execution-pulse"></span><strong>正在连接执行记录…</strong><small></small></div>';
  const started = Date.now();
  let stopped = false;
  let timer;
  let events = [];
  const poll = async () => {
    if (stopped || !message.isConnected) return;
    try {
      const result = await api(key.runId ? `/chat/runs/${key.runId}` : `/chat/activity?key=${encodeURIComponent(key)}`);
      if (stopped || !message.isConnected) return;
      if (result.activity?.length) events = result.activity;
      message.activityEvents = events;
      if (key.runId && ['completed', 'failed'].includes(result.status)) {
        stopped = true;
        bubble.textContent = result.result?.content || result.error || '执行已结束';
        renderArtifacts(message, { ...(result.result || {}), activity: events });
        return;
      }
      const last = events.at(-1);
      bubble.querySelector('strong').textContent = last ? `${last.name} · ${activityStatus(last.status)}` : '等待服务器接收请求';
      bubble.querySelector('small').textContent = `${model} · 已等待 ${Math.floor((Date.now() - started) / 1000)} 秒`;
      activityPanel(bubble, events, { live: result.status === 'processing' || result.status === 'pending' });
    } catch (_) {
      if (!stopped && message.isConnected) bubble.querySelector('strong').textContent = '执行记录暂时无法同步，仍在等待回答';
    }
    if (!stopped && message.isConnected) timer = window.setTimeout(poll, 1500);
  };
  poll();
  return () => { stopped = true; window.clearTimeout(timer); };
}

function directorExecutionDetails(project, card) {
  let host = card.querySelector('.director-execution');
  if (!host) {
    host = document.createElement('details');
    host.className = 'director-execution';
    host.innerHTML = '<summary>总导演与子 Agent 调用明细</summary><div class="director-execution-content"></div>';
    card.appendChild(host);
  }
  const body = host.querySelector('.director-execution-content');
  const logPanelOpen = body.querySelector('.execution-panel')?.open || false;
  const logDetailsOpen = body.querySelector('.execution-logs')?.open || false;
  const openNodes = new Set([...body.querySelectorAll('details[open][data-execution-node]')].map(n => n.dataset.executionNode));
  const agents = project.agents || [];
  const preflight = project.director_preflight || {};
  const attempts = preflight.attempts || [];
  const logs = [];
  body.innerHTML = `<div class="execution-orchestrator"><strong>总导演编排器</strong><span>${escapeHtml(activityStatus(project.status))} · ${project.progress}%</span><p>${escapeHtml(project.error_message || project.final_summary || directorStageText(project))}</p><small>${agents.filter(a => a.status === 'completed').length}/${agents.length} 个执行 Agent 已完成 · ${project.completed_shots || 0}/${project.planned_shots || 0} 镜交付</small></div>
    <div class="execution-agent-tree">${agents.map(a => {
      if (a.updated_at && a.status !== 'pending') logs.push({ time: a.updated_at, name: a.agent_name, status: a.status, kind: 'agent', detail: a.error_message || `模型 / 执行器：${a.model}` });
      return `<details data-execution-node="${escapeHtml(a.id)}" class="execution-agent ${escapeHtml(a.status)}" ${openNodes.has(a.id) ? 'open' : ''}>
        <summary><span class="execution-dot"></span><strong>${escapeHtml(a.agent_name)}</strong><span>${escapeHtml(activityStatus(a.status))}</span></summary>
        <div><small>模型 / 执行器：${escapeHtml(a.model || '待分配')} · 更新时间 ${activityTime(a.updated_at)}</small>
        <h4>判断摘要</h4><p>${escapeHtml(a.decision_summary || '等待 Agent 提交判断摘要')}</p>
        ${a.error_message ? `<p class="execution-error">${escapeHtml(a.error_message)}</p>` : ''}
        <details data-execution-node="delivery-${escapeHtml(a.id)}" ${openNodes.has(`delivery-${a.id}`) ? 'open' : ''}><summary>查看交付明细</summary><pre>${escapeHtml(a.deliverable || '尚无交付物')}</pre></details></div></details>`;
    }).join('')}</div>
    ${attempts.length ? `<section class="execution-preflight"><h4>总导演文本预演 · ${attempts.length} 轮</h4>${attempts.map(a => `<article><strong>第 ${a.attempt} 轮 · ${a.score} 分 · ${a.approved ? '通过' : '需修订'}</strong><p>${escapeHtml(a.verdict || '')}</p>${(a.risks || []).map(r => `<p class="execution-error">${escapeHtml(r)}</p>`).join('')}</article>`).join('')}</section>` : ''}
    ${(project.shots || []).length ? `<section class="execution-preflight"><h4>媒体与镜头任务</h4>${project.shots.map(s => `<article><strong>第 ${s.sequence} 镜 · ${escapeHtml(s.title)} · ${escapeHtml(activityStatus(s.status))}</strong><p>${escapeHtml(s.speech_text || '')}</p><small>视频任务 ${escapeHtml(s.video?.id || '未创建')} · 语音任务 ${escapeHtml(s.speech_job_id || '原生音轨 / 未创建')}</small>${s.error_message ? `<p class="execution-error">${escapeHtml(s.error_message)}</p>` : ''}</article>`).join('')}</section>` : ''}`;
  if (project.error_message) logs.push({ time: agents.map(a => a.updated_at).filter(Boolean).sort().at(-1), name: '总导演', status: 'failed', kind: 'workflow', detail: project.error_message });
  const panel = activityPanel(body, project.activity?.length ? project.activity : logs, {
    title: project.activity?.length ? '导演执行日志' : '阶段状态记录',
    live: ['queued', 'processing'].includes(project.status),
  });
  panel.open = logPanelOpen;
  panel.querySelector('.execution-logs').open = logDetailsOpen;
}
