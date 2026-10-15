(() => {
  const API = `http://${location.hostname}:8000`;
  const page1 = document.querySelector('#page1');
  const page2 = document.querySelector('#page2');
  const page3 = document.querySelector('#page3');
  const connection = document.querySelector('#connection');
  const connectionLabel = document.querySelector('#connection-label');
  const notice = document.querySelector('#notice');
  const feed = document.querySelector('#chat-feed');
  const messageArea = document.querySelector('#message-input');
  const messageForm = document.querySelector('#message-form');
  const responseActions = document.querySelector('#response-actions');
  const progress = document.querySelector('#task-progress');
  const progressLabel = document.querySelector('#progress-label');
  const progressBar = document.querySelector('#progress-bar');
  const working = document.querySelector('#working-indicator');
  const plannerLabel = document.querySelector('#planner-label');
  let taskId = new URLSearchParams(location.search).get('task');
  let latestTask = null;
  let pollTimer = null;
  let polling = false;
  let busy = false;
  let eventsRendered = new Set();
  let stepCards = new Map();
  let lastQuestion = '';
  let resultSnapshot = '';
  let feedGoalRendered = false;

  const statusText = status => ({
    completed: 'Completed', done: 'Completed', running: 'Running', pending: 'Pending',
    needs_confirmation: 'Needs confirmation', needs_approval: 'Needs confirmation',
    needs_human: 'Needs your help', failed: 'Failed', failed_retrying: 'Retrying', 'failed-retrying': 'Retrying', verifying: 'Verifying',
    cancelled: 'Cancelled', partially_complete: 'Partially complete',
  }[status] || String(status || 'Pending').replaceAll('_', ' '));
  const text = value => String(value ?? '');
  const humanize = value => text(value)
    .replaceAll('find_file', 'file search').replaceAll('read_file', 'file reading')
    .replaceAll('list_files', 'folder listing').replaceAll('download_file', 'file download')
    .replaceAll('move_file', 'file move').replaceAll('create_reminder', 'reminder creation')
    .replaceAll('send_email', 'email sending');
  const make = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = text(value);
    return node;
  };
  function showPage(page) {
    [page1, page2, page3].forEach(section => {
      const hidden = section !== page;
      if (section.classList.contains('hidden') !== hidden) section.classList.toggle('hidden', hidden);
    });
  }
  function setNotice(message) {
    if (notice.textContent !== (message || '')) notice.textContent = message || '';
    const hidden = !message;
    if (notice.classList.contains('hidden') !== hidden) notice.classList.toggle('hidden', hidden);
  }
  function setConnection(connected, message = 'Backend unavailable') {
    if (connection.classList.contains('hidden') !== connected) connection.classList.toggle('hidden', connected);
    if (connection.classList.contains('offline') === connected) connection.classList.toggle('offline', !connected);
    if (connectionLabel.textContent !== message) connectionLabel.textContent = message;
  }
  function setTaskUrl(id) {
    const url = new URL(location.href);
    if (id) url.searchParams.set('task', id);
    else url.searchParams.delete('task');
    history.replaceState({}, '', url);
  }
  async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
    return data;
  }
  function resetTaskView() {
    stopPolling();
    taskId = null;
    latestTask = null;
    eventsRendered = new Set();
    stepCards = new Map();
    lastQuestion = '';
    resultSnapshot = '';
    responseActions.dataset.signature = '';
    feedGoalRendered = false;
    feed.replaceChildren();
    progress.classList.add('hidden');
    progressLabel.textContent = '';
    progressBar.max = 1;
    progressBar.value = 0;
    working.classList.add('hidden');
    responseActions.replaceChildren();
    plannerLabel.textContent = '';
    messageArea.value = '';
    setNotice('');
    setTaskUrl(null);
  }
  function addEvent(event, fallback = '') {
    const id = String(event.id || event.event_id || `${event.timestamp || fallback}|${event.type || ''}|${event.step_id || ''}|${event.message || ''}`);
    if (eventsRendered.has(id)) return false;
    eventsRendered.add(id);
    const item = make('article', `feed-entry event-${event.type || 'event'}`);
    item.dataset.eventId = id;
    item.dataset.kind = event.type || 'event';
    const step = event.step_id ? `Step ${event.step_id}: ` : '';
    const raw = text(event.message || event.type || 'Task updated');
    let human = humanize(raw);
    if (event.type === 'execute') human = `${step}JARVIS is carrying out this step.`;
    else if (event.type === 'verify') human = `${step}Verification: ${human.replace(/^.*?verified successfully,?\s*/i, 'The action was checked. ')}`;
    else if (event.type === 'retry') human = `${step}JARVIS is retrying after an issue: ${human}`;
    else if (event.type === 'recover') human = `${step}JARVIS recovered: ${human}`;
    else if (event.type === 'plan' && !feedGoalRendered) human = `Goal: ${human.replace(/^Plan created with \d+ step\(s\) using the \w+ planner\.?\s*/i, '')}`;
    else if (event.type === 'escalate') human = `${step}JARVIS needs your help: ${human}`;
    item.append(make('p', '', human));
    feed.append(item);
    return true;
  }
  function createStepCard(step, index) {
    const card = make('article', 'feed-step');
    const head = make('div', 'feed-step-head');
    const title = make('strong', 'feed-step-title');
    const spinner = make('span', 'step-spinner hidden', '◌');
    const badge = make('span', 'badge');
    head.append(title, spinner, badge);
    const verify = make('p', 'feed-detail');
    const counts = make('p', 'feed-detail');
    const result = make('p', 'feed-detail');
    const issue = make('p', 'feed-detail failure-text');
    const recovery = make('p', 'feed-detail');
    card.append(head, verify, counts, result, issue, recovery);
    card.dataset.stepId = String(step.step_id ?? index + 1);
    return { card, title, spinner, badge, verify, counts, result, issue, recovery, snapshot: '' };
  }
  function updateStep(step, index, task) {
    const id = String(step.step_id ?? index + 1);
    let entry = stepCards.get(id);
    if (!entry) {
      entry = createStepCard(step, index);
      stepCards.set(id, entry);
      feed.append(entry.card);
    }
    const stepEvents = (task.events || []).filter(event => String(event.step_id) === id);
    const snapshot = JSON.stringify([step, stepEvents]);
    if (entry.snapshot === snapshot) return false;
    entry.snapshot = snapshot;
    entry.title.textContent = `Step ${id} · ${humanize(step.description || 'Task step')}`;
    entry.badge.textContent = statusText(step.status);
    entry.badge.dataset.status = step.status || 'pending';
    entry.spinner.classList.toggle('hidden', !['running', 'verifying'].includes(step.status));
    const retries = stepEvents.filter(event => event.type === 'retry').length;
    const failures = stepEvents.filter(event => event.type === 'retry' || (event.type === 'escalate' && /failed|exception|verification/i.test(event.message || ''))).length;
    entry.verify.textContent = step.verifier_message ? `Verification: ${humanize(step.verifier_message)}` : '';
    entry.counts.textContent = `Retries: ${retries} · Failures: ${failures}`;
    entry.result.textContent = step.result == null ? '' : `Result: ${humanize(typeof step.result === 'string' ? step.result : JSON.stringify(step.result))}`;
    entry.issue.textContent = step.error ? `Issue: ${humanize(step.error)}` : '';
    entry.recovery.textContent = step.recovery_notes ? `Recovery: ${humanize(step.recovery_notes)}` : '';
    return true;
  }
  function addTaskUpdates(task) {
    let changed = false;
    if (!feedGoalRendered) {
      const goalEvent = { type: 'plan', message: `Goal: ${task.goal || 'New task'}` };
      changed = addEvent(goalEvent, task.task_id) || changed;
      feedGoalRendered = true;
    }
    (task.events || []).forEach(event => { changed = addEvent(event) || changed; });
    (task.steps || []).forEach((step, index) => { changed = updateStep(step, index, task) || changed; });
    if (task.error_details) {
      const { options, ...questionDetails } = task.error_details;
      const key = JSON.stringify([questionDetails, task.message]);
      if (key !== lastQuestion) {
        lastQuestion = key;
        const question = make('article', 'feed-question');
        question.append(make('strong', '', humanize(task.error_details.what_failed || 'JARVIS needs your help.')));
        if (task.error_details.what_was_tried) question.append(make('p', '', Array.isArray(task.error_details.what_was_tried) ? humanize(task.error_details.what_was_tried.join(' · ')) : humanize(task.error_details.what_was_tried)));
        if (task.error_details.what_needed) question.append(make('p', '', humanize(task.error_details.what_needed)));
        question.append(make('p', '', humanize(task.message || 'What would you like me to do?')));
        feed.append(question);
        changed = true;
      }
    }
    const remembered = (task.preferences_used || []).length ? ` · Remembered from last time: ${task.preferences_used.join(', ')}` : '';
    const plannerText = task.planner ? `Planner: ${task.planner}${remembered}` : '';
    if (plannerLabel.textContent !== plannerText) plannerLabel.textContent = plannerText;
    if (changed) feed.scrollTop = feed.scrollHeight;
  }
  function addAction(label, className, payload) {
    const button = make('button', className, label);
    button.type = 'button';
    button.addEventListener('click', () => resume(payload));
    responseActions.append(button);
  }
  function updateActions(task) {
    const signature = JSON.stringify([task.status, task.error_details?.type || "", task.error_details?.options || []]);
    if (responseActions.dataset.signature === signature) return;
    responseActions.dataset.signature = signature;
    responseActions.replaceChildren();
    if (task.status === 'needs_confirmation' || task.status === 'needs_approval') {
      addAction('Approve', 'approve', { confirm: true });
      addAction('Reject', 'reject', { confirm: false });
    } else if (task.status === 'needs_human' && task.error_details?.type === 'goal_coverage') {
      addAction('Continue', 'approve', { confirm: true });
      addAction('Stop here', 'reject', { confirm: false });
    }
    (task.error_details?.options || []).forEach(name => addAction(`Choose: ${name}`, 'choice', { selected_file: name }));
  }
  function updatePrompt(task) {
    const prompt = document.querySelector('#you-prompt');
    let value = 'Tell me what you would like to accomplish.';
    if (task?.status === 'needs_human') value = 'JARVIS needs your help. Choose an option or type a reply below.';
    else if (task?.status === 'needs_confirmation' || task?.status === 'needs_approval') value = 'Approve or reject this action.';
    else if (task?.status === 'running') value = 'JARVIS is working on your goal.';
    else if (task?.message) value = humanize(task.message);
    if (prompt.textContent !== value) prompt.textContent = value;
  }
  function updateResult(task) {
    const snapshot = JSON.stringify(task);
    if (snapshot === resultSnapshot) return;
    resultSnapshot = snapshot;
    const gaps = task.missing_parts || [];
    document.querySelector('#result-title').textContent = task.status === 'completed' && gaps.length === 0 ? 'Task complete' : 'Partially complete';
    const missing = document.querySelector('#result-missing');
    const missingText = gaps.length ? 'Missing: ' + gaps.join(', ') + '.' : (task.status === 'completed' ? '' : 'Some requested parts were not completed.');
    if (missing.textContent !== missingText) missing.textContent = missingText;
    missing.classList.toggle('hidden', !missingText);
    document.querySelector('#result-answer').textContent = task.final_answer || task.message || 'No output was returned.';
    document.querySelector('#result-goal').textContent = task.goal || 'Your task';
    const badge = document.querySelector('#result-status');
    badge.textContent = statusText(task.status);
    badge.dataset.status = task.status || 'completed';
    document.querySelector('#result-summary').textContent = humanize(task.final_summary || task.message || 'The task has finished.');
    const steps = document.querySelector('#result-steps');
    steps.replaceChildren();
    (task.steps || []).forEach((step, index) => {
      const row = make('article', 'result-item');
      const head = make('div', 'result-step-head');
      head.append(make('strong', '', `Step ${step.step_id ?? index + 1} · ${humanize(step.description || 'Task step')}`));
      const status = make('span', 'badge', statusText(step.status));
      status.dataset.status = step.status || 'completed';
      head.append(status);
      row.append(head);
      if (step.verifier_message) row.append(make('p', '', `Verification: ${humanize(step.verifier_message)}`));
      if (step.result != null) row.append(make('p', '', `Result: ${humanize(typeof step.result === 'string' ? step.result : JSON.stringify(step.result))}`));
      if (step.error) row.append(make('p', 'failure-text', `Issue: ${humanize(step.error)}`));
      steps.append(row);
    });
    const list = document.querySelector('#result-failures');
    list.replaceChildren();
    const failures = task.failures_handled || [];
    if (failures.length) failures.forEach(message => list.append(make('article', 'result-item failure-text', humanize(message))));
    else list.append(make('article', 'result-item', 'No issues required recovery.'));
  }
  function renderTask(task) {
    if (!task || task.task_id !== taskId) return;
    latestTask = task;
    addTaskUpdates(task);
    updatePrompt(task);
    updateActions(task);
    const done = task.status === 'completed' || task.status === 'cancelled' || task.status === 'partially_complete';
    const total = (task.steps || []).length;
    const completed = (task.steps || []).filter(step => step.status === 'completed').length;
    const progressText = `Progress: ${completed} / ${total} steps`;
    if (progressLabel.textContent !== progressText) progressLabel.textContent = progressText;
    if (progressBar.max !== total) progressBar.max = total || 1;
    if (progressBar.value !== completed) progressBar.value = completed;
    if (progress.classList.contains('hidden') === Boolean(total)) progress.classList.toggle('hidden', !total);
    const isWorking = task.status === 'running' || task.status === 'planning';
    if (working.classList.contains('hidden') === isWorking) working.classList.toggle('hidden', !isWorking);
    if (done) {
      stopPolling();
      updateResult(task);
      showPage(page3);
    } else showPage(page2);
  }
  async function poll() {
    const requestedId = taskId;
    if (!requestedId || polling || busy) return;
    polling = true;
    try {
      const task = await request(`/tasks/${encodeURIComponent(requestedId)}`);
      if (taskId !== requestedId || task.task_id !== requestedId) return;
      setConnection(true);
      setNotice('');
      renderTask(task);
    } catch (error) {
      if (taskId === requestedId) {
        const unavailable = error instanceof TypeError;
        setConnection(!unavailable);
        setNotice(unavailable ? '' : humanize(error.message));
      }
    } finally { polling = false; }
  }
  function startPolling() {
    stopPolling();
    if (['completed', 'cancelled', 'partially_complete'].includes(latestTask?.status)) return;
    poll();
    pollTimer = setInterval(poll, 800);
  }
  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }
  async function resume(payload) {
    const requestedId = taskId;
    if (!requestedId || busy) return;
    busy = true;
    setNotice('Sending your response…');
    try {
      const result = await request(`/tasks/${encodeURIComponent(requestedId)}/resume`, { method: 'POST', body: JSON.stringify(payload) });
      if (taskId !== requestedId || result.task_id !== requestedId) return;
      setConnection(true);
      setNotice('');
      if (result.state) renderTask(result.state);
      startPolling();
    } catch (error) {
      const unavailable = error instanceof TypeError;
      setConnection(!unavailable);
      setNotice(unavailable ? '' : humanize(error.message));
    }
    finally { busy = false; }
  }
  async function startTask(goal) {
    if (!goal || busy) return;
    resetTaskView();
    showPage(page2);
    busy = true;
    setNotice('Creating your task…');
    try {
      const result = await request('/tasks', { method: 'POST', body: JSON.stringify({ goal }) });
      if (!result.task_id) throw new Error('The server did not return a task ID.');
      taskId = result.task_id;
      setTaskUrl(taskId);
      setConnection(true);
      setNotice('');
      if (result.state) renderTask(result.state);
      startPolling();
    } catch (error) {
      const unavailable = error instanceof TypeError;
      setConnection(!unavailable);
      setNotice(unavailable ? '' : humanize(error.message));
    } finally { busy = false; }
  }
  document.querySelector('#get-started').addEventListener('click', () => showPage(page2));
  messageForm.addEventListener('keydown', event => {
    if (event.target !== messageArea || event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    messageForm.requestSubmit();
  });
  messageForm.addEventListener('submit', async event => {
    event.preventDefault();
    const value = messageArea.value.trim();
    if (!value || busy) return;
    if (!taskId) return startTask(value);
    if (latestTask?.status === 'needs_human') return resume({ input: value });
    await resume({ input: value });
  });
  document.querySelector('#new-task').addEventListener('click', () => {
    resetTaskView();
    showPage(page2);
  });
  document.querySelector('#back-welcome').addEventListener('click', () => {
    resetTaskView();
    showPage(page1);
  });
  if (taskId) {
    showPage(page2);
    startPolling();
  } else {
    showPage(page1);
    fetch(`${API}/`).then(() => setConnection(true)).catch(() => setConnection(false));
  }
})();
