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
  renderStoredMessages();
  restorePending();
  Dictation.init();
  scrollToBottom(true);
}

document.addEventListener('DOMContentLoaded', () => {
  initSession();
  refreshTabBar();
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
async function resolveToolCall(toolCallId, action, value, scope) {
  if (App.streaming) return;
  await streamRequest(`/api/sessions/${App.sessionId}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      tool_call_id: toolCallId, action, value: value || '', scope: scope || 'once',
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
  const result = el('div', 'tool-result');
  result.appendChild(el('div', 'tool-result-label', event.is_error ? 'error' : 'output'));
  result.appendChild(el('pre', 'tool-raw', md.escapeHtml(event.output || '(no output)')));
  details.appendChild(result);
  autoscroll();
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

  const head = el('div', 'permission-head', 'Run this command?');
  const cmd = el('pre', 'permission-command', md.escapeHtml(event.command || JSON.stringify(event.args)));
  const dir = el('div', 'permission-dir', md.escapeHtml(event.workdir || ''));

  const actions = el('div', 'permission-actions');
  const approve = button('Approve', 'btn-approve', () => finish('approve', '', 'once'));
  const approveAll = button('Approve all this session', 'btn-approve-all', () => finish('approve', '', 'session'));
  const reject = button('Reject', 'btn-reject', () => {
    const why = prompt('Optional: tell the agent why, so it can try something else.', '');
    if (why === null) return;
    finish('reject', why, 'once');
  });
  actions.append(approve, approveAll, reject);

  function finish(action, value, scope) {
    actions.remove();
    node.classList.add('resolved');
    head.textContent = action === 'approve' ? 'Approved' : 'Rejected';
    if (scope === 'session') markAutoApprove(true);
    resolveToolCall(event.tool_call_id, action, value, scope);
  }

  node.append(head, cmd, dir, actions);
  App.els.messages.appendChild(node);
  autoscroll();
  approve.focus();
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
  if (e.target.id === 'chat-textarea') autosize(e.target);
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
