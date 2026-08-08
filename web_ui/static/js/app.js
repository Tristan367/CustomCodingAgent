/* CodeAgent front-end: SSE streaming, tool approval, dictation, tabs. */
'use strict';

const App = {
  sessionId: null,
  streaming: false,
  abortController: null,
  els: {},
};

/* ── Boot ────────────────────────────────────────────────────────────────── */

function initSession() {
  const view = document.getElementById('session-view');
  App.sessionId = view ? view.dataset.sessionId : null;
  App.els = {
    form: document.getElementById('chat-form'),
    textarea: document.getElementById('chat-textarea'),
    messages: document.getElementById('messages'),
    scroller: document.getElementById('chat-container'),
    send: document.getElementById('send-btn'),
    stop: document.getElementById('stop-btn'),
  };

  if (App.els.form && !App.els.form.dataset.bound) {
    App.els.form.dataset.bound = '1';
    App.els.form.addEventListener('submit', onSubmit);
  }
  if (App.els.scroller && !App.els.scroller.dataset.bound) {
    App.els.scroller.dataset.bound = '1';
    App.els.scroller.addEventListener('scroll', saveScrollSoon, { passive: true });
  }
  renderStoredMessages();
  restorePending();
  Dictation.init();
  markSessionSeen();
  if (!Persist.restore()) scrollToBottom(true);
}

document.addEventListener('DOMContentLoaded', () => {
  Notifier.init();
  initSession();
  refreshTabBar();
  Notifier.poll();
  setInterval(() => Notifier.poll(), 2000);
});

window.addEventListener('focus', markSessionSeen);
window.addEventListener('beforeunload', () => { Persist.saveDraft(); Persist.saveScroll(); });

document.addEventListener('htmx:beforeSwap', (e) => {
  if (e.detail.target && e.detail.target.id === 'main-content') {
    Persist.saveDraft();
    Persist.saveScroll();
  }
});

document.addEventListener('htmx:afterSwap', (e) => {
  const id = e.detail.target && e.detail.target.id;
  if (id === 'main-content' || id === 'chat-container') {
    initSession();
    if (id === 'main-content') refreshTabBar();
  }
});

/* Render markdown for server-rendered message bodies. */
function renderStoredMessages() {
  document.querySelectorAll('[data-markdown]:not([data-rendered])').forEach((el) => {
    el.dataset.raw = el.textContent;
    el.innerHTML = md.render(el.textContent);
    el.dataset.rendered = '1';
  });
}

/* ── Sending ─────────────────────────────────────────────────────────────── */

async function onSubmit(event) {
  event.preventDefault();
  if (!App.sessionId || App.streaming) return;

  // Enter while dictating: stop, transcribe, then send what was said.
  if (Dictation.recording) {
    const text = await Dictation.stop();
    if (text) insertAtCursor(App.els.textarea, text);
  }

  const message = App.els.textarea.value.trim();
  const imageInput = document.getElementById('image-input');
  const hasImage = imageInput && imageInput.files.length > 0;
  if (!message && !hasImage) return;

  appendMessage('user', message || '(image)');
  App.els.textarea.value = '';
  Persist.clearDraft();
  autosize(App.els.textarea);

  let endpoint, body, headers = {};
  if (hasImage) {
    endpoint = `/api/sessions/${App.sessionId}/chat-with-image`;
    body = new FormData();
    body.append('message', message);
    body.append('image', imageInput.files[0]);
    imageInput.value = '';
  } else {
    endpoint = `/api/sessions/${App.sessionId}/chat`;
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify({ message });
  }

  await streamRequest(endpoint, { method: 'POST', headers, body });
}

/* Resume the loop after the user answers a paused tool call. */
async function resolveToolCall(toolCallId, action, value, scope, grantPath) {
  if (App.streaming) return;
  await streamRequest(`/api/sessions/${App.sessionId}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      tool_call_id: toolCallId,
      action,
      value: value || '',
      scope: scope || 'once',
      grant_path: grantPath || '',
    }),
  });
}

async function streamRequest(url, options) {
  setStreaming(true);
  App.abortController = new AbortController();

  const stream = { assistantEl: null, contentEl: null, text: '', reasoningEl: null };

  try {
    const resp = await fetch(url, { ...options, signal: App.abortController.signal });
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const data = await resp.json();
        detail = data.detail || detail;
      } catch (_) { /* non-JSON error body */ }
      appendNotice('error', detail);
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const chunks = buffer.split('\n\n');
      buffer = chunks.pop() || '';
      for (const chunk of chunks) {
        const line = chunk.split('\n').find((l) => l.startsWith('data: '));
        if (!line) continue;
        let event;
        try {
          event = JSON.parse(line.slice(6));
        } catch (_) {
          continue;
        }
        handleEvent(event, stream);
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') appendNotice('error', err.message);
  } finally {
    if (stream.assistantEl) stream.assistantEl.classList.remove('streaming');
    setStreaming(false);
    App.abortController = null;
    refreshMeta();
  }
}

function handleEvent(event, stream) {
  switch (event.type) {
    case 'reasoning':
      if (!stream.reasoningEl) stream.reasoningEl = appendReasoning();
      stream.reasoningEl.textContent += event.text;
      autoscroll();
      break;

    case 'content':
      if (stream.reasoningEl) {
        collapseReasoning(stream.reasoningEl);
        stream.reasoningEl = null;
      }
      if (!stream.assistantEl) {
        stream.assistantEl = appendMessage('assistant', '');
        stream.assistantEl.classList.add('streaming');
        stream.contentEl = stream.assistantEl.querySelector('.content-text');
      }
      stream.text += event.text;
      stream.contentEl.innerHTML = md.render(stream.text);
      autoscroll();
      break;

    case 'tool_start':
      if (stream.reasoningEl) { collapseReasoning(stream.reasoningEl); stream.reasoningEl = null; }
      stream.assistantEl = null;
      stream.contentEl = null;
      stream.text = '';
      appendToolCall(event);
      break;

    case 'tool_end':
      completeToolCall(event);
      break;

    case 'permission':
      appendPermissionCard(event);
      break;

    case 'question':
      appendQuestionCard(event);
      break;

    case 'compaction_required':
      openCompactModal(event);
      break;

    case 'usage':
      break;

    case 'error':
      appendNotice('error', event.message);
      break;

    case 'aborted':
      appendNotice('aborted', 'Stopped.');
      break;

    case 'done':
      break;
  }
}

function setStreaming(active) {
  App.streaming = active;
  if (App.els.send) App.els.send.hidden = active;
  if (App.els.stop) App.els.stop.hidden = !active;
}

async function stopStreaming() {
  if (!App.sessionId) return;
  // Tell the server to abort so it stops spending tokens, then drop the reader.
  await fetch(`/api/sessions/${App.sessionId}/cancel`, { method: 'POST' }).catch(() => {});
  if (App.abortController) App.abortController.abort();
}

/* ── Message rendering ───────────────────────────────────────────────────── */

function el(tag, className, html) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

function appendMessage(role, text) {
  const node = el('div', `message ${role}`);
  node.appendChild(el('div', 'msg-role', role));
  const body = el('div', 'msg-content');
  const content = el('div', 'content-text');
  content.innerHTML = md.render(text);
  body.appendChild(content);
  node.appendChild(body);
  App.els.messages.appendChild(node);
  autoscroll();
  return node;
}

function appendReasoning() {
  const node = el('div', 'message reasoning');
  node.appendChild(el('div', 'msg-role', 'thinking'));
  const body = el('div', 'msg-content');
  const toggle = el('button', 'reasoning-toggle', 'Hide thinking');
  toggle.type = 'button';
  const text = el('pre', 'reasoning-text');
  toggle.addEventListener('click', () => {
    const hidden = text.hidden;
    text.hidden = !hidden;
    toggle.textContent = hidden ? 'Hide thinking' : 'Show thinking';
  });
  body.append(toggle, text);
  node.appendChild(body);
  App.els.messages.appendChild(node);
  return text;
}

function collapseReasoning(textEl) {
  textEl.hidden = true;
  const toggle = textEl.parentElement.querySelector('.reasoning-toggle');
  if (toggle) toggle.textContent = 'Show thinking';
}

function appendToolCall(event) {
  const node = el('div', 'message tool pending');
  node.dataset.toolCallId = event.tool_call_id;
  node.appendChild(el('div', 'msg-role', event.name));
  const body = el('div', 'msg-content');
  const details = el('details', 'tool-details');
  const summary = el('summary', 'tool-summary');
  summary.append(el('span', 'spinner-dot'), document.createTextNode(toolSummary(event.name, event.args)));
  details.appendChild(summary);
  details.appendChild(el('pre', 'tool-raw', md.escapeHtml(JSON.stringify(event.args, null, 2))));
  body.appendChild(details);
  node.appendChild(body);
  App.els.messages.appendChild(node);
  autoscroll();
  return node;
}

function completeToolCall(event) {
  const node = App.els.messages.querySelector(`.message.tool[data-tool-call-id="${cssEscape(event.tool_call_id)}"]`);
  if (!node) return;
  node.classList.remove('pending');
  if (event.is_error) node.classList.add('tool-error');

  const summary = node.querySelector('.tool-summary');
  if (summary) summary.textContent = event.title || event.name;

  const details = node.querySelector('.tool-details');
  if (event.diff) node.querySelector('.msg-content').appendChild(renderDiff(event.diff));
  const result = el('div', 'tool-result');
  result.appendChild(el('div', 'tool-result-label', event.is_error ? 'error' : 'output'));
  result.appendChild(el('pre', 'tool-raw', md.escapeHtml(event.output || '(no output)')));
  details.appendChild(result);
  autoscroll();
}

/* Unified diff with per-line colouring. */
function renderDiff(diff) {
  const box = el('pre', 'diff-block');
  for (const line of diff.replace(/\n+$/, '').split('\n')) {
    let cls = 'diff-ctx';
    if (line.startsWith('@@')) cls = 'diff-hunk';
    else if (line.startsWith('+')) cls = 'diff-add';
    else if (line.startsWith('-')) cls = 'diff-del';
    const row = el('span', cls);
    row.textContent = line + '\n';
    box.appendChild(row);
  }
  return box;
}

function toolSummary(name, args) {
  args = args || {};
  switch (name) {
    case 'read': return `Reading ${args.filePath || ''}`;
    case 'edit': return `Editing ${args.filePath || ''}`;
    case 'write': return `Writing ${args.filePath || ''}`;
    case 'bash': return `Running ${truncate(args.command, 90)}`;
    case 'grep': return `Searching for ${truncate(args.pattern, 70)}`;
    case 'glob': return `Finding ${args.pattern || ''}`;
    case 'webfetch': return `Fetching ${truncate(args.url, 80)}`;
    case 'vision': return `Looking at ${truncate(args.url, 70)}`;
    case 'task': return `Subagent: ${args.description || ''}`;
    case 'todowrite': return 'Updating task list';
    default: return name;
  }
}

function appendNotice(kind, text) {
  const node = el('div', `message notice notice-${kind}`);
  node.appendChild(el('div', 'msg-role', kind));
  const body = el('div', 'msg-content');
  body.appendChild(el('div', 'content-text', md.escapeHtml(text)));
  node.appendChild(body);
  App.els.messages.appendChild(node);
  autoscroll();
}

/* ── Interactive cards ───────────────────────────────────────────────────── */

function appendPermissionCard(event) {
  const node = el('div', 'message permission-card');
  node.dataset.toolCallId = event.tool_call_id;

  const kind = event.kind || 'shell';
  const head = el('div', 'permission-head');
  const detail = el('pre', 'permission-command');
  const sub = el('div', 'permission-dir');
  const actions = el('div', 'permission-actions');

  if (kind === 'denied') {
    // Nothing to grant: this location is permanently off limits.
    node.classList.add('permission-denied');
    head.textContent = 'Blocked write';
    detail.textContent = event.path;
    sub.textContent = 'This location is on the permanent deny list and cannot be allowed.';
    actions.append(button('Tell the agent', 'btn-reject', () => finish('reject', 'That path is off limits.', 'once')));
  } else if (kind === 'path') {
    node.classList.add('permission-path');
    head.textContent = `Write outside the project directory?`;
    detail.textContent = `${event.tool}  ${event.path}`;
    sub.textContent = `Project is ${event.project_dir}. This file is not inside it.`;
    actions.append(
      button('Allow once', 'btn-approve', () => finish('approve', '', 'once')),
      button(`Always allow ${shortPath(event.scope)}`, 'btn-approve-all',
        () => finish('approve', '', 'directory')),
      button('Reject', 'btn-reject', reject),
    );
  } else {
    head.textContent = 'Run this command?';
    detail.textContent = event.command || JSON.stringify(event.args);
    sub.textContent = event.workdir || '';
    actions.append(
      button('Approve', 'btn-approve', () => finish('approve', '', 'once')),
      button('Approve all this session', 'btn-approve-all', () => finish('approve', '', 'session')),
      button('Reject', 'btn-reject', reject),
    );
  }

  function reject() {
    const why = prompt('Optional: tell the agent why, so it can try something else.', '');
    if (why === null) return;
    finish('reject', why, 'once');
  }

  function finish(action, value, scope) {
    actions.remove();
    node.classList.add('resolved');
    head.textContent = action === 'approve'
      ? (scope === 'directory' ? `Allowed — ${event.scope} is now writable` : 'Approved')
      : 'Rejected';
    if (scope === 'session') markAutoApprove(true);
    resolveToolCall(event.tool_call_id, action, value, scope, event.scope);
  }

  node.append(head, detail, sub, actions);
  App.els.messages.appendChild(node);
  autoscroll();
  actions.querySelector('button')?.focus();
}

function shortPath(path) {
  if (!path) return 'this directory';
  const parts = String(path).split('/').filter(Boolean);
  return parts.length > 2 ? `.../${parts.slice(-2).join('/')}` : path;
}

function appendQuestionCard(event) {
  const node = el('div', 'message question-card');
  node.appendChild(el('div', 'question-head', md.render(event.question)));

  const actions = el('div', 'question-actions');
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'question-input';
  input.placeholder = 'Your answer...';

  function submit(value) {
    const answer = (value || input.value).trim();
    if (!answer) return;
    actions.remove();
    node.classList.add('resolved');
    node.appendChild(el('div', 'question-answer', md.escapeHtml(answer)));
    resolveToolCall(event.tool_call_id, 'answer', answer);
  }

  (event.options || []).forEach((option) => {
    actions.appendChild(button(option, 'btn-option', () => submit(option)));
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
  });
  actions.append(input, button('Send', 'btn-approve', () => submit()));

  node.appendChild(actions);
  App.els.messages.appendChild(node);
  autoscroll();
  input.focus();
}

/* A paused run survives a page reload: re-render the card it stopped on. */
function restorePending() {
  const holder = document.getElementById('pending-restore');
  if (!holder || holder.dataset.restored) return;
  holder.dataset.restored = '1';
  let event;
  try {
    event = JSON.parse(holder.dataset.pending);
  } catch (_) {
    return;
  }
  holder.remove();
  // App.els may not be populated yet on first paint.
  App.els.messages = App.els.messages || document.getElementById('messages');
  App.els.scroller = App.els.scroller || document.getElementById('chat-container');
  if (event.type === 'permission') appendPermissionCard(event);
  else if (event.type === 'question') appendQuestionCard(event);
}

/* ── Compaction ──────────────────────────────────────────────────────────── */

const THRESHOLD_STEPS = [4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1000000];

/* Set when the agent loop stops and asks to compact, so Confirm knows to resume
 * the interrupted run rather than just compacting in place. */
let compactionPause = null;

function formatTokens(n) {
  n = Number(n) || 0;
  if (n >= 1000000) return `${(n / 1000000).toFixed(n % 1000000 ? 2 : 0)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

function currentUsage() {
  const meta = document.getElementById('session-meta');
  return {
    threshold: Number(meta?.dataset.threshold) || 262144,
    maxContext: Number(meta?.dataset.maxContext) || 1000000,
  };
}

function openCompactModal(pause) {
  compactionPause = pause || null;
  const stats = document.getElementById('compact-stats');
  if (pause) {
    stats.textContent =
      `This conversation has reached ${Number(pause.context).toLocaleString()} tokens, `
      + `past your ${formatTokens(pause.threshold)} threshold. `
      + `The run is paused until you decide.`;
  } else {
    const ring = document.querySelector('.context-ring');
    stats.textContent = ring ? ring.title.split('\n')[0] : '';
  }
  document.getElementById('compact-extra').value = '';
  document.getElementById('compact-modal').hidden = false;
  document.getElementById('compact-extra').focus();
}

async function confirmCompaction() {
  const extra = document.getElementById('compact-extra').value;
  closeModal('compact-modal');
  const resume = !!compactionPause;
  compactionPause = null;

  const form = new FormData();
  form.append('extra_instructions', extra);
  form.append('resume', resume ? 'true' : 'false');

  if (resume) {
    // Compaction and the continuation stream in one request.
    appendNotice('info', 'Compacting, then continuing...');
    await streamRequest(`/api/sessions/${App.sessionId}/compact`, { method: 'POST', body: form });
    htmx.ajax('GET', `/_messages/${App.sessionId}`, { target: '#chat-container', swap: 'innerHTML' });
    return;
  }

  appendNotice('info', 'Compacting...');
  const resp = await fetch(`/api/sessions/${App.sessionId}/compact`, { method: 'POST', body: form });
  const data = await resp.json();
  if (!data.ok) { appendNotice('error', data.reason || 'Compaction failed'); return; }
  htmx.ajax('GET', `/_messages/${App.sessionId}`, { target: '#chat-container', swap: 'innerHTML' });
  refreshMeta();
}

function openThresholdModal() {
  closeModal('compact-modal');
  document.querySelectorAll('.dropdown-menu').forEach((m) => { m.hidden = true; });

  const { threshold, maxContext } = currentUsage();
  const usable = THRESHOLD_STEPS.filter((s) => s <= maxContext);
  const slider = document.getElementById('threshold-slider');
  slider.max = String(usable.length - 1);
  let index = usable.findIndex((s) => s >= threshold);
  slider.value = String(index < 0 ? usable.length - 1 : index);

  document.getElementById('threshold-max').textContent = Number(maxContext).toLocaleString();
  updateThresholdLabel();
  document.getElementById('threshold-modal').hidden = false;
}

function updateThresholdLabel() {
  const { maxContext } = currentUsage();
  const usable = THRESHOLD_STEPS.filter((s) => s <= maxContext);
  const value = usable[Number(document.getElementById('threshold-slider').value)] || usable.at(-1);
  document.getElementById('threshold-value').textContent = formatTokens(value);
  // Rough guide: a cached full-context request at V4 Pro's cache-hit rate.
  const perRequest = (value * 0.003625) / 1000000;
  document.getElementById('threshold-cost').textContent =
    `At this size a cached request costs about $${perRequest.toFixed(4)}; `
    + `an uncached one about $${((value * 0.435) / 1000000).toFixed(3)}.`;
}

async function saveThreshold() {
  const { maxContext } = currentUsage();
  const usable = THRESHOLD_STEPS.filter((s) => s <= maxContext);
  const value = usable[Number(document.getElementById('threshold-slider').value)] || usable.at(-1);
  closeModal('threshold-modal');

  const resume = !!compactionPause;
  compactionPause = null;
  const form = new FormData();
  form.append('threshold', String(value));
  form.append('resume', resume ? 'true' : 'false');

  if (resume) {
    await streamRequest(`/api/sessions/${App.sessionId}/compact-threshold`, { method: 'POST', body: form });
  } else {
    await fetch(`/api/sessions/${App.sessionId}/compact-threshold`, { method: 'POST', body: form });
  }
  refreshMeta();
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.hidden = true;
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.querySelectorAll('.modal').forEach((m) => { m.hidden = true; });
});

/* ── Retry and edit ──────────────────────────────────────────────────────── */

async function retryFrom(messageId) {
  if (App.streaming) return;
  if (!confirm('Re-run from this message? Everything after it will be discarded.')) return;
  dropMessagesAfter(messageId);
  await streamRequest(`/api/sessions/${App.sessionId}/messages/${messageId}/retry`, { method: 'POST' });
}

function editMessage(messageId) {
  if (App.streaming) return;
  const node = document.getElementById(`msg-${messageId}`);
  const body = node?.querySelector('.content-text');
  if (!body || node.dataset.editing) return;
  node.dataset.editing = '1';

  const original = body.dataset.raw ?? body.textContent;
  const editor = document.createElement('textarea');
  editor.className = 'message-editor';
  editor.value = original;
  const actions = el('div', 'message-edit-actions');
  actions.append(
    button('Save & re-run', 'btn-primary', save),
    button('Cancel', 'btn-secondary', cancel),
  );

  body.hidden = true;
  body.after(editor, actions);
  editor.focus();
  editor.style.height = `${Math.min(editor.scrollHeight + 4, 300)}px`;

  function cleanup() {
    editor.remove();
    actions.remove();
    body.hidden = false;
    delete node.dataset.editing;
  }
  function cancel() { cleanup(); }
  async function save() {
    const content = editor.value.trim();
    if (!content) return;
    cleanup();
    body.textContent = content;
    body.dataset.raw = content;
    body.innerHTML = md.render(content);
    dropMessagesAfter(messageId);
    await streamRequest(`/api/sessions/${App.sessionId}/messages/${messageId}/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
  }
}

/* Remove the stale DOM for turns the server is about to delete. */
function dropMessagesAfter(messageId) {
  const anchor = document.getElementById(`msg-${messageId}`);
  if (!anchor) return;
  let node = anchor.nextElementSibling;
  while (node) {
    const next = node.nextElementSibling;
    node.remove();
    node = next;
  }
}

/* ── Session status + notification sounds ────────────────────────────────── */

const Notifier = {
  enabled: true,
  ctx: null,
  lastUnseen: {},

  init() {
    this.enabled = document.body.dataset.sound !== 'off';
  },

  /* Synthesised so there is no audio file to ship or load. */
  play(kind) {
    if (!this.enabled) return;
    try {
      this.ctx = this.ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === 'suspended') this.ctx.resume();
      const tones = kind === 'waiting' ? [660, 880] : kind === 'error' ? [300, 220] : [880];
      tones.forEach((freq, i) => {
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();
        const start = this.ctx.currentTime + i * 0.11;
        osc.type = 'sine';
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(0.10, start + 0.012);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.10);
        osc.connect(gain).connect(this.ctx.destination);
        osc.start(start);
        osc.stop(start + 0.12);
      });
    } catch (_) { /* audio unavailable */ }
  },

  async poll() {
    let data;
    try {
      data = await (await fetch('/api/status')).json();
    } catch (_) {
      return;
    }
    const sessions = data.sessions || {};

    document.querySelectorAll('#tab-scroll .tab-wrap').forEach((tab) => {
      const info = sessions[tab.dataset.sid] || { status: 'idle', unseen: '' };
      const active = tab.dataset.sid === App.sessionId;
      // Don't badge the tab you're already looking at.
      const state = info.status === 'running' ? 'running'
        : (active ? '' : info.unseen);
      tab.dataset.state = state || '';
    });

    for (const [sid, info] of Object.entries(sessions)) {
      const previous = this.lastUnseen[sid] || '';
      const isActive = sid === App.sessionId && document.hasFocus();
      if (info.unseen && info.unseen !== previous && !isActive) this.play(info.unseen);
      this.lastUnseen[sid] = info.unseen;
    }
    for (const sid of Object.keys(this.lastUnseen)) {
      if (!sessions[sid]) delete this.lastUnseen[sid];
    }
  },
};

function markSessionSeen() {
  if (!App.sessionId) return;
  Notifier.lastUnseen[App.sessionId] = '';
  fetch(`/api/sessions/${App.sessionId}/seen`, { method: 'POST' }).catch(() => {});
}

async function toggleSound(enabled) {
  Notifier.enabled = enabled;
  document.body.dataset.sound = enabled ? 'on' : 'off';
  const form = new FormData();
  form.append('enabled', enabled ? '1' : '0');
  await fetch('/_settings/sound', { method: 'POST', body: form });
  if (enabled) Notifier.play('done');
}

/* ── Drafts and scroll position ──────────────────────────────────────────── */

const Persist = {
  key(kind) { return `codeagent:${kind}:${App.sessionId}`; },

  restore() {
    if (!App.sessionId) return;
    const draft = localStorage.getItem(this.key('draft'));
    if (draft && App.els.textarea && !App.els.textarea.value) {
      App.els.textarea.value = draft;
      autosize(App.els.textarea);
    }
    const top = Number(localStorage.getItem(this.key('scroll')));
    if (top && App.els.scroller) {
      requestAnimationFrame(() => { App.els.scroller.scrollTop = top; });
      return true;
    }
    return false;
  },

  saveDraft() {
    if (!App.sessionId || !App.els.textarea) return;
    const value = App.els.textarea.value;
    if (value) localStorage.setItem(this.key('draft'), value);
    else localStorage.removeItem(this.key('draft'));
  },

  clearDraft() {
    if (App.sessionId) localStorage.removeItem(this.key('draft'));
  },

  saveScroll() {
    if (!App.sessionId || !App.els.scroller) return;
    const box = App.els.scroller;
    // At the bottom is the default; don't pin the user there artificially.
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    if (atBottom) localStorage.removeItem(this.key('scroll'));
    else localStorage.setItem(this.key('scroll'), String(box.scrollTop));
  },
};

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

const saveDraftSoon = debounce(() => Persist.saveDraft(), 400);
const saveScrollSoon = debounce(() => Persist.saveScroll(), 250);

function button(label, className, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = className;
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

/* ── Dictation ───────────────────────────────────────────────────────────── */

const Dictation = {
  recording: false,
  recorder: null,
  chunks: [],
  streamRef: null,
  audioCtx: null,
  analyser: null,
  rafId: null,
  els: {},

  init() {
    this.els.button = document.getElementById('mic-btn');
    this.els.meter = document.getElementById('mic-meter');
    if (!this.els.button || this.els.button.dataset.bound) return;
    this.els.button.dataset.bound = '1';
    this.els.button.addEventListener('click', () => this.toggle());
  },

  async toggle() {
    if (this.recording) {
      const text = await this.stop();
      if (text) insertAtCursor(App.els.textarea, text);
    } else {
      await this.start();
    }
  },

  async start() {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      appendNotice('error', 'This browser cannot record audio.');
      return;
    }
    try {
      this.streamRef = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
    } catch (err) {
      appendNotice('error', `Microphone unavailable: ${err.message}`);
      return;
    }

    this.chunks = [];
    const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
      .find((t) => MediaRecorder.isTypeSupported(t)) || '';
    this.recorder = new MediaRecorder(this.streamRef, mime ? { mimeType: mime } : undefined);
    this.recorder.ondataavailable = (e) => { if (e.data.size) this.chunks.push(e.data); };
    this.recorder.start(250);

    this.recording = true;
    this.els.button.classList.add('recording');
    this.els.button.title = 'Stop dictation';
    this.startMeter();
  },

  async stop() {
    if (!this.recording) return '';
    this.recording = false;
    this.els.button.classList.remove('recording');
    this.els.button.classList.add('transcribing');
    this.stopMeter();

    const blob = await new Promise((resolve) => {
      this.recorder.onstop = () => resolve(new Blob(this.chunks, { type: this.recorder.mimeType }));
      this.recorder.stop();
    });
    this.streamRef.getTracks().forEach((t) => t.stop());
    this.streamRef = null;

    let text = '';
    try {
      const form = new FormData();
      form.append('audio', blob, mimeToName(this.recorder.mimeType));
      const resp = await fetch('/api/stt', { method: 'POST', body: form });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || 'transcription failed');
      text = (data.text || '').trim();
      if (!text) flashButton(this.els.button, 'no speech detected');
    } catch (err) {
      appendNotice('error', `Transcription failed: ${err.message}`);
    } finally {
      this.els.button.classList.remove('transcribing');
      this.els.button.title = 'Dictate';
    }
    return text;
  },

  startMeter() {
    if (!this.els.meter) return;
    this.els.meter.hidden = false;
    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = this.audioCtx.createMediaStreamSource(this.streamRef);
    this.analyser = this.audioCtx.createAnalyser();
    this.analyser.fftSize = 512;
    source.connect(this.analyser);

    const bars = Array.from(this.els.meter.querySelectorAll('.mic-bar'));
    const data = new Uint8Array(this.analyser.frequencyBinCount);

    const tick = () => {
      this.analyser.getByteTimeDomainData(data);
      // RMS deviation from the 128 midpoint, scaled to something visible.
      let sum = 0;
      for (const v of data) sum += (v - 128) ** 2;
      const level = Math.min(1, Math.sqrt(sum / data.length) / 40);
      bars.forEach((bar, i) => {
        const threshold = (i + 1) / bars.length;
        const height = 20 + Math.max(0, level - threshold * 0.35) * 220;
        bar.style.height = `${Math.min(100, height)}%`;
      });
      this.rafId = requestAnimationFrame(tick);
    };
    tick();
  },

  stopMeter() {
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = null;
    if (this.audioCtx) this.audioCtx.close().catch(() => {});
    this.audioCtx = null;
    if (this.els.meter) this.els.meter.hidden = true;
  },
};

function mimeToName(mime) {
  if (!mime) return 'audio.webm';
  if (mime.includes('ogg')) return 'audio.ogg';
  if (mime.includes('mp4')) return 'audio.mp4';
  return 'audio.webm';
}

function flashButton(button, message) {
  const original = button.title;
  button.title = message;
  button.classList.add('flash');
  setTimeout(() => { button.classList.remove('flash'); button.title = original; }, 1500);
}

function insertAtCursor(textarea, text) {
  if (!textarea) return;
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  const before = textarea.value.slice(0, start);
  const after = textarea.value.slice(end);
  const spacer = before && !/\s$/.test(before) ? ' ' : '';
  textarea.value = before + spacer + text + after;
  const caret = (before + spacer + text).length;
  textarea.setSelectionRange(caret, caret);
  textarea.focus();
  autosize(textarea);
}

/* ── Session controls ────────────────────────────────────────────────────── */

function toggleMenu(button) {
  const menu = button.nextElementSibling;
  const opening = menu.hidden;
  document.querySelectorAll('.dropdown-menu').forEach((m) => { m.hidden = true; });
  menu.hidden = !opening;
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('.dropdown')) {
    document.querySelectorAll('.dropdown-menu').forEach((m) => { m.hidden = true; });
  }
});

async function setAutoApprove(enabled, persist) {
  await fetch(`/api/sessions/${App.sessionId}/auto-approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled, persist: !!persist }),
  });
  markAutoApprove(enabled);
}

function markAutoApprove(enabled) {
  document.querySelectorAll('[data-auto-approve]').forEach((node) => {
    node.dataset.autoApprove = enabled ? '1' : '0';
    node.textContent = enabled ? 'Shell: auto-approved' : 'Shell: ask first';
  });
}

async function applySessionSettings(form) {
  const payload = {};
  new FormData(form).forEach((value, key) => { payload[key] = value; });
  await fetch(`/api/sessions/${App.sessionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  location.reload();
}

async function renameSession() {  const current = document.querySelector('.session-title')?.textContent.trim() || '';
  const name = prompt('Session name:', current);
  if (!name || name === current) return;
  await fetch(`/api/sessions/${App.sessionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  location.reload();
}

async function deleteSession(sessionId, name) {
  if (!confirm(`Delete session "${name}"? This cannot be undone.`)) return;
  await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
  if (sessionId === App.sessionId) location.href = '/';
  else location.reload();
}

async function compactSession() {
  if (!confirm('Summarise the older part of this conversation to free up context?')) return;
  const resp = await fetch(`/api/sessions/${App.sessionId}/compact`, {
    method: 'POST', body: new FormData(),
  });
  const data = await resp.json();
  if (!data.ok) { alert(data.reason || 'Compaction failed'); return; }
  htmx.ajax('GET', `/_messages/${App.sessionId}`, { target: '#chat-container', swap: 'innerHTML' });
  refreshMeta();
}

function refreshMeta() {
  if (!App.sessionId) return;
  htmx.ajax('GET', `/_session_meta/${App.sessionId}`, { target: '#session-meta', swap: 'outerHTML' });
}

/* ── Tabs ────────────────────────────────────────────────────────────────── */

function refreshTabBar() {
  const current = App.sessionId || '';
  htmx.ajax('GET', `/_tab_bar?current=${encodeURIComponent(current)}`, {
    target: '#tab-bar', swap: 'outerHTML',
  }).then(setupTabs);
}

function closeTab(event, sessionId) {
  event.preventDefault();
  event.stopPropagation();
  fetch(`/_tab_close/${sessionId}`, { method: 'POST' });
  event.target.closest('.tab-wrap')?.remove();
  if (sessionId === App.sessionId) location.href = '/';
}

function setupTabs() {
  const scroll = document.getElementById('tab-scroll');
  if (!scroll) return;

  // Horizontal wheel scrolling: the whole reason tabs bled off screen before.
  if (!scroll.dataset.wheelBound) {
    scroll.dataset.wheelBound = '1';
    scroll.addEventListener('wheel', (e) => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        scroll.scrollLeft += e.deltaY;
      }
    }, { passive: false });
  }

  let dragged = null;
  scroll.querySelectorAll('.tab-wrap').forEach((tab) => {
    tab.addEventListener('dragstart', () => { dragged = tab; tab.classList.add('dragging'); });
    tab.addEventListener('dragend', () => {
      tab.classList.remove('dragging');
      dragged = null;
      persistTabOrder();
    });
    tab.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (!dragged || dragged === tab) return;
      const after = e.clientX > tab.getBoundingClientRect().left + tab.offsetWidth / 2;
      scroll.insertBefore(dragged, after ? tab.nextSibling : tab);
    });
  });

  document.querySelector('.tab.active')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function persistTabOrder() {
  const ids = Array.from(document.querySelectorAll('#tab-scroll .tab-wrap')).map((t) => t.dataset.sid);
  fetch('/_tab_order', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
}

/* ── Misc helpers ────────────────────────────────────────────────────────── */

function autosize(textarea) {
  if (!textarea) return;
  textarea.style.height = 'auto';
  textarea.style.height = `${Math.min(textarea.scrollHeight, 260)}px`;
}

function autoscroll() {
  const box = App.els.scroller;
  if (!box) return;
  // Only follow the stream if the user is already near the bottom.
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 200) scrollToBottom();
}

function scrollToBottom(instant) {
  const box = App.els.scroller;
  if (!box) return;
  requestAnimationFrame(() => {
    box.scrollTo({ top: box.scrollHeight, behavior: instant ? 'auto' : 'smooth' });
  });
}

function truncate(text, n) {
  text = String(text || '');
  return text.length > n ? `${text.slice(0, n)}...` : text;
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, '\\$&');
}

document.addEventListener('keydown', (e) => {
  if (e.target.id === 'chat-textarea' && e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    App.els.form?.requestSubmit();
  }
});

document.addEventListener('input', (e) => {
  if (e.target.id === 'chat-textarea') {
    autosize(e.target);
    saveDraftSoon();
  }
});

/* ── Image attachment ────────────────────────────────────────────────────── */

let pendingImage = null;

function handleImageAttach(input) {
  if (!input.files.length) return;
  pendingImage = input.files[0];
  const reader = new FileReader();
  reader.onload = (e) => {
    document.getElementById('image-preview').src = e.target.result;
    document.getElementById('image-modal').hidden = false;
    document.getElementById('vision-modal-prompt').focus();
  };
  reader.readAsDataURL(pendingImage);
  input.value = '';
}

function cancelImageAttach() {
  pendingImage = null;
  document.getElementById('image-modal').hidden = true;
}

async function submitImageAttach() {
  if (!pendingImage) return;
  const prompt = document.getElementById('vision-modal-prompt').value.trim()
    || 'Describe this image in detail.';
  document.getElementById('image-modal').hidden = true;

  const form = new FormData();
  form.append('image', pendingImage);
  form.append('prompt', prompt);
  pendingImage = null;

  const btn = document.querySelector('.upload-label');
  btn?.classList.add('busy');
  try {
    const resp = await fetch(`/api/sessions/${App.sessionId}/analyze-image`, {
      method: 'POST', body: form,
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'vision failed');
    insertAtCursor(App.els.textarea, `\n\n[Image description] ${data.description}\n`);
  } catch (err) {
    appendNotice('error', `Vision analysis failed: ${err.message}`);
  } finally {
    btn?.classList.remove('busy');
  }
}
