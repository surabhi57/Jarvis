(() => {
  const API = 'http://localhost:8000';
  const form = document.querySelector('#goal-form');
  const goalInput = document.querySelector('#goal-input');
  const startButton = document.querySelector('#start-button');
  const panel = document.querySelector('#task-panel');
  const connection = document.querySelector('.connection');
  const connectionLabel = document.querySelector('#connection-label');
  const notice = document.querySelector('#notice');
  const stepsEl = document.querySelector('#steps');
  const humanPanel = document.querySelector('#human-panel');
  let taskId = new URLSearchParams(location.search).get('task');
  let pollTimer = null;
  let latestTask = null;
  let busy = false;

  const pretty = (value) => String(value || 'pending').replaceAll('_', ' ').replaceAll('-', ' ');
  const statusLabel = (status) => ({ completed: 'done', done: 'done', failed_retrying: 'failed-retrying', 'failed-retrying': 'failed-retrying', needs_confirmation: 'needs confirmation', needs_approval: 'needs confirmation', needs_human: 'needs human', cancelled: 'cancelled', pending: 'pending', running: 'running' }[status] || pretty(status));
  const escape = (s) => String(s ?? '');
  function setNotice(message) { notice.textContent = message || ''; notice.hidden = !message; }
  function setConnection(online) { connection.classList.toggle('online', online); connection.classList.toggle('offline', !online); connectionLabel.textContent = online ? 'Backend connected' : 'Backend unavailable'; }
  async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
    return data;
  }
  function make(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = escape(text); return el; }
  function addActionButton(parent, text, className, action) {
    const button = make('button', className, text); button.type = 'button'; button.disabled = busy; button.addEventListener('click', action); parent.append(button); return button;
  }
  function render(task) {
    latestTask = task;
    panel.hidden = false;
    document.querySelector('#task-goal').textContent = task.goal || 'JARVIS task';
    const badge = document.querySelector('#task-status'); badge.textContent = statusLabel(task.status); badge.dataset.status = task.status || 'pending';
    const msg = document.querySelector('#task-message'); msg.textContent = task.message || ''; msg.hidden = !task.message;
    const remembered = document.querySelector('#remembered');
    const prefs = task.preferences_used || task.preferences;
    if (prefs && typeof prefs === 'object' && Object.keys(prefs).length) {
      const parts = Object.entries(prefs).map(([k, v]) => `${k.replaceAll('_', ' ')}: ${Array.isArray(v) ? v.join(', ') : typeof v === 'object' && v !== null ? JSON.stringify(v) : v}`);
      remembered.textContent = `Remembered from last time · ${parts.join(' · ')}`; remembered.hidden = false;
    } else remembered.hidden = true;

    stepsEl.replaceChildren();
    (task.steps || []).forEach((step, index) => {
      const card = make('article', 'step-card');
      const top = make('div', 'step-top'); const title = make('div', 'step-title');
      title.append(make('span', 'step-number', String(step.step_id ?? index + 1)));
      const copy = make('div'); copy.append(make('div', 'step-name', step.description || step.tool || 'Step'));
      if (step.description && step.tool) copy.append(make('p', 'step-description', step.tool));
      title.append(copy); top.append(title);
      const stepBadge = make('span', 'badge', statusLabel(step.status || 'pending')); stepBadge.dataset.status = step.status || 'pending'; top.append(stepBadge); card.append(top);
      if (step.attempts > 1 || ['failed-retrying', 'failed_retrying'].includes(step.status)) card.append(make('div', 'step-detail', `Recovery attempt ${step.attempts || ''}`));
      if (step.verifier_message) card.append(make('div', 'step-detail', `Verified · ${step.verifier_message}`));
      if (step.error) card.append(make('div', 'step-detail error', step.error));
      stepsEl.append(card);
    });
    renderHumanControls(task);
    if (['completed', 'cancelled'].includes(task.status)) stopPolling();
  }
  function renderHumanControls(task) {
    humanPanel.replaceChildren(); humanPanel.hidden = true;
    if (task.status === 'needs_confirmation' || task.status === 'needs_approval') {
      humanPanel.hidden = false;
      humanPanel.append(make('h3', '', task.status === 'needs_approval' ? 'This action needs your approval' : 'JARVIS needs your confirmation'));
      const step = task.steps?.[task.current_step];
      if (step) humanPanel.append(make('p', '', `${step.description || step.tool}${step.risk ? ` · ${step.risk} risk` : ''}`));
      const actions = make('div', 'approval-actions');
      addActionButton(actions, 'Approve', 'primary', () => resume(task.status === 'needs_approval' ? { approved: true } : { confirmed: true }));
      addActionButton(actions, 'Reject', 'danger', () => resume(task.status === 'needs_approval' ? { approved: false } : { confirmed: false }));
      humanPanel.append(actions);
    } else if (task.status === 'needs_human') {
      humanPanel.hidden = false;
      const detail = task.error_details || {};
      humanPanel.append(make('h3', '', 'JARVIS needs your help'));
      if (detail.what_failed) { const p = make('p'); p.append(make('strong', '', 'What failed: '), document.createTextNode(detail.what_failed)); humanPanel.append(p); }
      if (detail.what_was_tried) { const tried = Array.isArray(detail.what_was_tried) ? detail.what_was_tried.join(' · ') : detail.what_was_tried; const p = make('p'); p.append(make('strong', '', 'What was tried: '), document.createTextNode(tried)); humanPanel.append(p); }
      if (detail.what_needed) { const p = make('p'); p.append(make('strong', '', 'What’s needed: '), document.createTextNode(detail.what_needed)); humanPanel.append(p); }
    const options = Array.isArray(detail.options) ? detail.options : [];
      if (options.length) {
        const list = make('div', 'choices'); options.forEach(option => addActionButton(list, option, 'choice', () => resume({ selected_option: option })));
        humanPanel.append(list);
      } else {
        const wrapper = make('form', 'human-form');
        const input = document.createElement('input'); input.type = 'text'; input.placeholder = 'Add details or a choice'; input.setAttribute('aria-label', 'Your response'); input.required = true;
        const submit = make('button', 'secondary', 'Send response'); submit.type = 'submit'; submit.disabled = busy;
        wrapper.append(input, submit); wrapper.addEventListener('submit', event => { event.preventDefault(); if (input.value.trim()) resume({ input: input.value.trim() }); }); humanPanel.append(wrapper);
      }
    }
  }
  async function poll() {
    if (!taskId || busy) return;
    try { const task = await request(`/tasks/${encodeURIComponent(taskId)}`); setConnection(true); setNotice(''); render(task); }
    catch (error) { setConnection(false); setNotice(error.message); }
  }
  function startPolling() { stopPolling(); poll(); pollTimer = setInterval(poll, 1000); }
  function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }
  async function resume(payload) {
    if (!taskId || busy) return;
    busy = true; setNotice('Sending your response…'); render(latestTask);
    try {
      const result = await request(`/tasks/${encodeURIComponent(taskId)}/resume`, { method: 'POST', body: JSON.stringify(payload) });
      setConnection(true); setNotice(''); render(result.state || await request(`/tasks/${encodeURIComponent(taskId)}`));
    } catch (error) { setNotice(error.message); }
    finally { busy = false; if (latestTask) render(latestTask); }
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); const goal = goalInput.value.trim(); if (!goal || busy) return;
    busy = true; startButton.disabled = true; setNotice('Planning your task…');
    try {
      const result = await request('/tasks', { method: 'POST', body: JSON.stringify({ goal }) });
      taskId = result.task_id; const url = new URL(location.href); url.searchParams.set('task', taskId); history.replaceState({}, '', url);
      setConnection(true); setNotice(''); render(result.state || await request(`/tasks/${encodeURIComponent(taskId)}`)); startPolling();
    } catch (error) { setConnection(false); setNotice(error.message); }
    finally { busy = false; startButton.disabled = false; }
  });
  if (taskId) { goalInput.value = ''; startPolling(); }
  else { fetch(`${API}/`).then(() => setConnection(true)).catch(() => setConnection(false)); }
})();
