/* CodeAgent front-end: SSE streaming, tool approval, dictation, tabs. */
'use strict';

/* ── Dialogs ─────────────────────────────────────────────────────────────── */
/* Drop-in replacements for window.alert/confirm/prompt, which cannot be styled,
   truncate long paths, and on a tiling compositor open wherever the window
   manager decides. These return promises, so callers must await them. */
const ui = (() => {
  function build({ title, body, input, confirmLabel, cancelLabel, danger }) {
    const dialog = document.createElement('dialog');
    dialog.className = 'dialog';

    const form = document.createElement('form');
    form.method = 'dialog';

    if (title) {
      const h = document.createElement('h3');
      h.textContent = title;
      form.appendChild(h);
    }
    if (body) {
      const p = document.createElement('p');
      p.textContent = body;
      form.appendChild(p);
    }

    let field = null;
    if (input) {
      field = document.createElement('input');
      field.type = 'text';
      field.value = input.value || '';
      field.placeholder = input.placeholder || '';
      if (input.pattern) field.pattern = input.pattern;
      form.appendChild(field);
    }

    const actions = document.createElement('div');
    actions.className = 'modal-actions';
    if (cancelLabel !== null) {
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.textContent = cancelLabel || 'Cancel';
      cancel.onclick = () => { dialog.returnValue = ''; dialog.close(); };
      actions.appendChild(cancel);
    }
    const ok = document.createElement('button');
    ok.type = 'submit';
    ok.className = danger ? 'btn-danger' : 'btn-primary';
    ok.textContent = confirmLabel || 'OK';
    ok.value = 'ok';
    actions.appendChild(ok);
    form.appendChild(actions);

    // A submit button's value only reaches returnValue when the form is
    // submitted by that button, which is what method="dialog" gives us.
    form.addEventListener('submit', () => { dialog.returnValue = 'ok'; });

    dialog.appendChild(form);
    document.body.appendChild(dialog);
    return { dialog, field };
  }

  function open({ input, ...opts }) {
    return new Promise((resolve) => {
      const { dialog, field } = build({ input, ...opts });
      dialog.addEventListener('close', () => {
        const ok = dialog.returnValue === 'ok';
        const value = field ? field.value.trim() : null;
        dialog.remove();
        resolve(input ? (ok && value ? value : null) : ok);
      });
      dialog.showModal();
      if (field) field.select();
    });
  }

  return {
    alert: (body, title) => open({ title: title || 'Heads up', body, cancelLabel: null }),
    confirm: (body, opts = {}) => open({
      title: opts.title || 'Are you sure?',
      body,
      confirmLabel: opts.confirmLabel || 'Confirm',
      danger: opts.danger !== false,
    }),
    prompt: (body, opts = {}) => open({
      title: opts.title || body,
      body: opts.title ? body : '',
      input: { value: opts.value || '', placeholder: opts.placeholder || '', pattern: opts.pattern },
      confirmLabel: opts.confirmLabel || 'OK',
      danger: false,
    }),
  };
})();

/* Every hx-confirm in the templates routes through the same dialog rather than
   htmx's default window.confirm. */
document.body.addEventListener('htmx:confirm', (e) => {
  if (!e.detail.question) return;
  e.preventDefault();
  ui.confirm(e.detail.question).then((ok) => { if (ok) e.detail.issueRequest(true); });
});

const App = {
  sessionId: null,
  projectDir: null,
  streaming: false,
  ttsAvailable: false,
  timers: new Set(),
  abortController: null,
  els: {},
  expandTools: [],
};

/* ── Boot ────────────────────────────────────────────────────────────────── */

function initSession() {
  const view = document.getElementById('session-view');
  const previous = App.sessionId;
  App.sessionId = view ? view.dataset.sessionId : null;
  App.projectDir = view ? view.dataset.projectDir : null;
  try { App.expandTools = view && view.dataset.expandTools ? JSON.parse(view.dataset.expandTools) : []; }
  catch (_) { App.expandTools = []; }
  if (previous && previous !== App.sessionId) {
    // Switching tabs must not leave the old session's reader running: it would
    // keep writing into whichever transcript is on screen now, and it holds
    // App.streaming true so the new tab refuses to attach to its own run.
    // The server run is untouched -- only this page stops listening.
    detachStream();
    pendingImages = [];
    renderAttachments();
    // The editor lives inside the old session view, which this swap replaces;
    // close it so switching back starts with a fresh session, not a stale editor.
    FileEditor.forceClose();
  }
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
    initJumpButton();
  }
  stopAllElapsed();
  Speech.stop();
  renderStoredMessages();
  loadChangeSummary();
  restorePending();
  attachIfRunning();
  Dictation.init();
  Speech.settings().then((ok) => { App.ttsAvailable = ok; attachPlayButtons(); });
  setupDragDrop();
  markSessionSeen();
  updateComposerButtons();
  if (!Persist.restore()) scrollToBottom(true);
}

document.addEventListener('DOMContentLoaded', () => {
  Notifier.init();
  initSession();
  initHomePage();
  refreshTabBar();
  Notifier.poll();
  setInterval(() => Notifier.tick(), 1000);
});

/* Home-page wiring. Separate from initSession because the two pages never
   coexist, and because this must re-run after an htmx swap replaces the form. */
function initHomePage() {
  MicTest.init();
  const select = document.getElementById('model-select');
  if (!select || select.dataset.bound) return;
  select.dataset.bound = '1';

  // A custom endpoint knows its own model ids; nothing here can list them.
  const customModel = document.getElementById('custom-model-input');
  const sync = () => {
    const opt = select.selectedOptions[0];
    const needed = opt && opt.dataset.needsModelId === '1';
    customModel.hidden = !needed;
    customModel.required = !!needed;
  };
  select.addEventListener('change', () => {
    sync();
    if (!customModel.hidden) customModel.focus();
  });
  sync();
}

window.addEventListener('focus', () => {
  markSessionSeen();
  // Retry at once: if the user came back, they want current state.
  Notifier.nextAttemptAt = 0;
  Notifier.poll();
});
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
  // The home page is re-rendered wholesale by every settings save, which
  // replaces the model select and drops its listener with it.
  if (id === 'main-content' || id === 'home-page') initHomePage();
});

/* Pull the transcript fresh from the server. Used after following a run that
   was already in progress, where the earlier part was never streamed here. */
async function refreshTranscript() {
  if (!App.sessionId) return;
  try {
    const html = await (await fetch(`/_messages/${App.sessionId}`)).text();
    const holder = document.createElement('div');
    holder.innerHTML = html;
    const fresh = holder.querySelector('#messages');
    if (fresh && App.els.messages) {
      App.els.messages.replaceWith(fresh);
      App.els.messages = fresh;
      renderStoredMessages();
      scrollToBottom(true);
    }
  } catch (_) { /* leave what is on screen */ }
}

/* Render markdown for server-rendered message bodies. */
function renderStoredMessages() {
  document.querySelectorAll('[data-markdown]:not([data-rendered])').forEach((el) => {
    el.dataset.raw = el.textContent;
    el.innerHTML = md.render(el.textContent);
    el.dataset.rendered = '1';
  });
  highlightToolCode(document);
  markOpenableTools(document);
  setupMessageSide();
  refreshRevertButtons();
}

/* ── Sending ─────────────────────────────────────────────────────────────── */

async function onSubmit(event) {
  event.preventDefault();
  if (!App.sessionId) return;

  // Enter while dictating: stop, transcribe, then send what was said.
  if (Dictation.recording) {
    const text = await Dictation.stop();
    if (text) insertAtCursor(App.els.textarea, text);
  }

  const message = App.els.textarea.value.trim();
  const attachments = pendingImages.slice();
  if (!message && !attachments.length) return;

  // Typing while it works: hand the message to the running turn instead of
  // starting a second one. The agent picks it up at the next turn boundary,
  // so it keeps going and sees the message on its next request.
  if (App.streaming) {
    if (attachments.length) {
      appendNotice('error', 'Finish the current run before attaching an image.');
      return;
    }
    const resp = await fetch(`/api/sessions/${App.sessionId}/queue`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    }).catch(() => null);
    if (!resp || !resp.ok) {
      appendNotice('error', 'Could not deliver that message; the run may have just finished.');
      return;
    }
    const { queue_id: queueId } = await resp.json();
    addQueuedBubble(message, queueId);
    App.els.textarea.value = '';
    Persist.clearDraft();
    autosize(App.els.textarea);
    return;
  }

  appendUserMessage(message, attachments);
  App.els.textarea.value = '';
  Persist.clearDraft();
  autosize(App.els.textarea);
  pendingImages = [];
  renderAttachments();

  let endpoint, body, headers = {};
  if (attachments.length) {
    endpoint = `/api/sessions/${App.sessionId}/chat-with-image`;
    body = new FormData();
    body.append('message', message);
    for (const file of attachments) body.append('images', file, file.name);
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

/* Reattach to a turn that is still running server-side, after a reload or a
   tab switch. Without this the run continues but the page shows nothing, which
   looks exactly like it was cancelled. */
async function attachIfRunning() {
  if (!App.sessionId || App.streaming) return;
  const target = App.sessionId;
  let running = false;
  try {
    const data = await (await fetch('/api/status')).json();
    running = (data.sessions || {})[App.sessionId]?.status === 'running';
  } catch (_) {
    return;
  }
  // The user may have moved on while /api/status was in flight.
  if (!running || App.sessionId !== target) return;
  await streamRequest(`/api/sessions/${target}/attach`, { method: 'GET' }, true);
}

async function streamRequest(url, options, attached = false) {
  setStreaming(true);
  App.abortController = new AbortController();

  const stream = {
    assistantEl: null, contentEl: null, text: '', reasoningEl: null, attached,
    sessionId: App.sessionId,
  };
  const status = showStatus(attached ? 'Reattaching' : 'Sending');

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
        // A late event from a session the user has navigated away from must not
        // be drawn into the transcript now on screen.
        if (stream.sessionId !== App.sessionId) continue;
        handleEvent(event, stream);
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') appendNotice('error', err.message);
  } finally {
    status.remove();
    flushRender(stream);
    endAssistantSegment(stream);
    clearToolProgress(stream);
    document.querySelectorAll('.message.tool.pending')
      .forEach((n) => { stopElapsed(n); n.classList.remove('pending'); });
    setStreaming(false);
    App.abortController = null;
    refreshRevertButtons();
    refreshMeta();
  }
}

/* While the model streams a tool call's arguments there is no content and no
   reasoning, so a large `write` looks like a 30-second freeze. Show what is
   being assembled and how big it has got. */
function showToolProgress(event, stream) {
  const calls = (event.calls || []).filter((c) => c.name);
  if (!calls.length) return;
  if (!stream.progressEl) {
    stream.progressEl = el('div', 'message notice tool-progress');
    stream.progressEl.append(roleEl('working'));
    const body = el('div', 'msg-content');
    body.appendChild(el('div', 'content-text'));
    stream.progressEl.appendChild(body);
    App.els.messages.appendChild(stream.progressEl);
    autoscroll();
  }
  const text = calls
    .map((c) => `${c.name}\u2026 ${formatBytes(c.chars)}`)
    .join('   ');
  stream.progressEl.querySelector('.content-text').textContent = text;
}

function clearToolProgress(stream) {
  if (stream.progressEl) {
    stream.progressEl.remove();
    stream.progressEl = null;
  }
}

function formatBytes(n) {
  return n < 1024 ? `${n} chars` : `${(n / 1024).toFixed(1)} KB`;
}

/* Close off the current assistant bubble: stop its cursor, and drop it entirely
   if the model produced a tool call without any prose, which would otherwise
   leave an empty bubble with a blinking cursor in it. */
function endAssistantSegment(stream) {
  const node = stream.assistantEl;
  stream.assistantEl = null;
  stream.contentEl = null;
  stream.text = '';
  if (!node) return;
  const text = node.querySelector('.content-text');
  if (text && !text.textContent.trim()
      && !node.querySelector('.diff-block, .msg-attachments, .reasoning-details')) {
    node.remove();
    return;
  }
  setupMessageSide();
}

function handleEvent(event, stream) {
  // Any event means the request landed; the placeholder has done its job.
  if (event.type !== 'turn_start') clearStatus();

  switch (event.type) {
    case 'turn_start':
      setStatusText('Waiting for the model');
      attachMessageActions(event.user_message_id);
      break;

    case 'reasoning':
      if (!stream.reasoningEl) stream.reasoningEl = appendReasoning();
      stream.reasoningEl.textContent += event.text;
      autoscroll();
      break;

    case 'content':
      clearToolProgress(stream);
      if (!stream.assistantEl) {
        if (stream.reasoningEl) {
          // Thinking is its own row (matches chat_messages.html). Leave it
          // as-is and create a fresh assistant bubble for the content.
          collapseReasoning(stream.reasoningEl);
          stream.reasoningEl = null;
        }
        stream.assistantEl = appendMessage('assistant', '');
        stream.contentEl = stream.assistantEl.querySelector('.content-text');
      } else if (stream.reasoningEl) {
        collapseReasoning(stream.reasoningEl);
        stream.reasoningEl = null;
      }
      stream.text += event.text;
      scheduleRender(stream);
      break;

    case 'queued_message': {
      // Several pending messages are delivered as one, so replace all of them.
      App.els.messages.querySelectorAll('.message.user.queued')
        .forEach((n) => n.remove());
      const node = event.from_name
        ? appendMailMessage(event.from_name, event.content)
        : appendUserMessage(event.content, []);
      node.id = `msg-${event.message_id}`;
      break;
    }

    case 'compacting':
      appendNotice('info', 'Compacting the conversation...');
      break;

    case 'compact_delta':
      if (!stream.compactEl) stream.compactEl = appendCompactionDraft();
      stream.compactEl.textContent += event.text;
      autoscroll();
      break;

    case 'compact_done':
    case 'compacted': {
      if (stream.compactEl) {
        stream.compactEl.closest('.message')?.remove();
        stream.compactEl = null;
      }
      if (!event.ok) {
        appendNotice('error', event.reason || 'Compaction failed.');
        break;
      }
      // Re-render: the compacted turns are gone from what the model sees, and
      // the transcript must show the same thing.
      refreshTranscript();
      refreshMeta();
      break;
    }

    case 'attached':
      for (const call of event.inflight || []) appendToolCall(call);
      break;

    case 'tool_progress':
      showToolProgress(event, stream);
      break;

    case 'tool_start':
      if (stream.reasoningEl) { collapseReasoning(stream.reasoningEl); stream.reasoningEl = null; }
      endAssistantSegment(stream);
      clearToolProgress(stream);
      appendToolCall(event);
      break;

    case 'tool_end':
      completeToolCall(event);
      break;

    case 'permission':
      appendPermissionCard(event);
      break;

    case 'notice':
      appendNotice(event.level === 'warn' ? 'error' : 'info', event.message);
      break;

    case 'cache_warning':
      openCacheModal(event);
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
      renderChangeSummary(event.changes);
      setupMessageSide();
      break;
  }
}

/* Re-rendering markdown on every token is O(n^2): each token re-parses and
 * re-highlights the whole message so far. On a long answer with code blocks that
 * is enough to lock up the tab. Coalesce into at most one render per frame, and
 * no more often than RENDER_INTERVAL_MS. */
const RENDER_INTERVAL_MS = 90;
let renderQueued = false;
let lastRenderAt = 0;

function scheduleRender(stream) {
  stream.dirty = true;
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    const now = performance.now();
    if (now - lastRenderAt < RENDER_INTERVAL_MS) {
      setTimeout(() => scheduleRender(stream), RENDER_INTERVAL_MS - (now - lastRenderAt));
      return;
    }
    lastRenderAt = now;
    flushRender(stream);
  });
}

function flushRender(stream) {
  if (!stream.dirty || !stream.contentEl) return;
  stream.dirty = false;
  stream.contentEl.dataset.raw = stream.text;
  stream.contentEl.innerHTML = md.render(stream.text);
  autoscroll();
}

function setStreaming(active) {
  App.streaming = active;
  if (App.els.textarea) {
    App.els.textarea.placeholder = active
      ? 'Message the agent \u2014 sent at the next step'
      : 'Message the agent';
  }
  updateComposerButtons();
}

/* Stop listening without stopping the run. */
function detachStream() {
  if (App.abortController) App.abortController.abort();
  App.abortController = null;
  stopAllElapsed();
  setStreaming(false);
}

async function stopStreaming() {
  if (!App.sessionId) return;
  // Ask the server to stop, then keep reading. Dropping the reader here is
  // what made in-flight tool rows vanish: the run carried on server-side and
  // its results only reappeared on a manual refresh.
  setStatusText('Stopping');
  await fetch(`/api/sessions/${App.sessionId}/cancel`, { method: 'POST' }).catch(() => {});
}

async function stopAll() {
  const ok = await ui.confirm(
    'This stops every running agent and clears all pending messages between sessions. There is no undo.',
    { title: 'Stop everything?', confirmLabel: 'Stop all', danger: true },
  );
  if (!ok) return;
  const resp = await fetch('/api/stop-all', { method: 'POST' }).catch(() => null);
  if (!resp || !resp.ok) { appendNotice('error', 'Could not stop all agents.'); return; }
  const data = await resp.json();
  appendNotice('info', `Stopped ${data.stopped} running agent${data.stopped === 1 ? '' : 's'}; pending messages cleared.`);
}

/* ── Broadcast ────────────────────────────────────────────────────────────── */

let broadcastMessage = '';
let broadcastSessions = [];
let broadcastSelected = new Set();

async function openBroadcast() {
  const message = App.els.textarea.value.trim();
  if (!message) return;
  broadcastMessage = message;

  let sessions = [];
  try {
    sessions = await (await fetch('/api/sessions')).json();
  } catch (_) { return; }

  broadcastSessions = sessions.filter((s) => s.id !== App.sessionId && !s.is_archived);
  const saved = JSON.parse(localStorage.getItem('broadcast-selection') || '[]');
  broadcastSelected = new Set(saved.filter((id) => broadcastSessions.some((s) => s.id === id)));

  document.getElementById('broadcast-message-preview').textContent = message;
  renderBroadcastList();
  updateBroadcastToggle();
  document.getElementById('broadcast-modal').hidden = false;
}

function renderBroadcastList() {
  const container = document.getElementById('broadcast-session-list');
  if (!container) return;
  container.innerHTML = '';
  const checked = broadcastSessions.filter((s) => broadcastSelected.has(s.id));
  const unchecked = broadcastSessions.filter((s) => !broadcastSelected.has(s.id));
  for (const s of [...checked, ...unchecked]) {
    const label = el('label', 'broadcast-session');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = broadcastSelected.has(s.id);
    cb.addEventListener('change', () => {
      if (cb.checked) broadcastSelected.add(s.id); else broadcastSelected.delete(s.id);
      saveBroadcastSelection();
      renderBroadcastList();
      updateBroadcastToggle();
    });
    label.append(cb, el('span', 'broadcast-name', s.name));
    container.appendChild(label);
  }
}

function saveBroadcastSelection() {
  localStorage.setItem('broadcast-selection', JSON.stringify([...broadcastSelected]));
}

function updateBroadcastToggle() {
  const btn = document.getElementById('broadcast-toggle-all');
  if (!btn) return;
  const all = broadcastSessions.length > 0 && broadcastSelected.size === broadcastSessions.length;
  btn.textContent = all ? 'Deselect all' : 'Select all';
}

function broadcastToggleAll() {
  if (broadcastSelected.size === broadcastSessions.length) {
    broadcastSelected.clear();
  } else {
    broadcastSelected = new Set(broadcastSessions.map((s) => s.id));
  }
  saveBroadcastSelection();
  renderBroadcastList();
  updateBroadcastToggle();
}

async function sendBroadcast() {
  if (!broadcastSelected.size) return;
  const resp = await fetch('/api/broadcast', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: broadcastMessage, session_ids: [...broadcastSelected] }),
  }).catch(() => null);
  if (!resp || !resp.ok) {
    appendNotice('error', 'Broadcast failed.');
    return;
  }
  const data = await resp.json();
  App.els.textarea.value = '';
  Persist.clearDraft();
  autosize(App.els.textarea);
  closeModal('broadcast-modal');
  appendNotice('info', `Broadcast to ${data.sent} session${data.sent === 1 ? '' : 's'}.`);
}

/* ── Drag and drop into the composer ──────────────────────────────────────── */

function setupDragDrop() {
  const textarea = App.els.textarea;
  if (!textarea || textarea.dataset.dropBound) return;
  textarea.dataset.dropBound = '1';

  textarea.addEventListener('dragover', (e) => {
    if (!e.dataTransfer?.types.includes('Files')) return;
    e.preventDefault();
    textarea.classList.add('dragging');
  });
  textarea.addEventListener('dragleave', () => textarea.classList.remove('dragging'));
  textarea.addEventListener('drop', async (e) => {
    e.preventDefault();
    textarea.classList.remove('dragging');
    await handleDroppedFiles(e.dataTransfer?.files);
  });
}

async function handleDroppedFiles(fileList) {
  if (!fileList || !fileList.length) return;
  const images = [];
  for (const file of fileList) {
    if (file.type.startsWith('image/')) {
      images.push(file);
    } else if (file.name.endsWith('.txt') || file.name.endsWith('.md')) {
      const text = await file.text();
      insertAtCursor(App.els.textarea, text);
      Persist.saveDraft();
    }
  }
  for (const file of images) {
    if (pendingImages.length >= 6) break;
    pendingImages.push(file);
  }
  if (images.length) renderAttachments();
}

/* ── Message rendering ───────────────────────────────────────────────────── */

/* Text, never markup. Every caller passes plain text, but this used to assign
   to innerHTML, and several of those strings are written by the model: a tool
   name, a summary interpolating args.command or args.filePath, a subagent
   prompt. A page fetched by `webfetch` could therefore put script into the
   transcript, and the tool summary is rendered *before* the call is approved.
   Markdown is the only thing that legitimately produces HTML here, and it goes
   through md.render at its own call sites. */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* The role gutter is narrow enough to clip the longer names, so the full one
 * always goes on the title. Anything short enough to fit shows the same text
 * twice, which costs nothing. */
function roleEl(text) {
  const node = el('div', 'msg-role', text);
  node.title = text;
  return node;
}

/* A transient "Sending / Waiting" line, so there is feedback in the second or
 * two before the first token arrives. */
let statusEl = null;

function showStatus(text) {
  clearStatus();
  const node = el('div', 'message status-line');
  node.appendChild(el('div', 'msg-role', ''));
  const body = el('div', 'msg-content');
  body.append(el('span', 'spinner-dot'), el('span', 'status-text', text));
  node.appendChild(body);
  App.els.messages.appendChild(node);
  statusEl = node;
  autoscroll();
  return { remove: clearStatus };
}

function setStatusText(text) {
  const label = statusEl && statusEl.querySelector('.status-text');
  if (label) label.textContent = text;
}

function clearStatus() {
  if (statusEl) statusEl.remove();
  statusEl = null;
}

function appendMessage(role, text) {
  const node = el('div', `message ${role}`);
  node.appendChild(roleEl(role));
  const body = el('div', 'msg-content');
  const content = el('div', 'content-text');
  content.dataset.raw = text;
  content.innerHTML = md.render(text);
  body.appendChild(content);
  node.appendChild(body);
  node.appendChild(el('span', 'msg-time', clockTime()));
  App.els.messages.appendChild(node);
  autoscroll();
  return node;
}

/* Give the just-sent user bubble its database id, so later events can find it. */
function attachMessageActions(messageId) {
  if (!messageId) return;
  const bubbles = App.els.messages.querySelectorAll('.message.user:not(.queued)');
  const node = bubbles[bubbles.length - 1];
  if (!node || node.id) return;
  node.id = `msg-${messageId}`;
}

/* The summary as it is written, replaced by the real card when it lands. */
function appendCompactionDraft() {
  const node = el('div', 'message compaction');
  node.appendChild(roleEl('summarising'));
  const body = el('div', 'msg-content');
  const text = el('pre', 'reasoning-text');
  body.appendChild(text);
  node.appendChild(body);
  App.els.messages.appendChild(node);
  autoscroll();
  return text;
}

/* A message handed to a running turn. Nothing has been persisted or sent, so
   it can still be taken back: the model never learns it existed. */
function addQueuedBubble(text, queueId) {
  const node = appendUserMessage(text, []);
  node.classList.add('queued');
  node.dataset.queueId = queueId;
  const side = el('span', 'msg-side');
  const actions = el('span', 'msg-actions');
  actions.appendChild(button('undo', '', () => undoQueued(queueId)));
  side.append(actions, node.querySelector(':scope > .msg-time') || el('span', 'msg-time', clockTime()));
  node.appendChild(side);
  // This node already carries its side column; a later setupMessageSide pass
  // must not append a second one.
  node.dataset.sideDone = '1';
  return node;
}

async function undoQueued(queueId) {
  const node = App.els.messages.querySelector(
    `.message.user.queued[data-queue-id="${cssEscape(queueId)}"]`);
  const resp = await fetch(`/api/sessions/${App.sessionId}/queue/${queueId}`, {
    method: 'DELETE',
  }).catch(() => null);
  if (!resp || !resp.ok) {
    // It reached the model between rendering the button and clicking it.
    if (node) node.classList.remove('queued');
    appendNotice('error', 'Too late to take that back; it has already been sent.');
    return;
  }
  const { message } = await resp.json();
  if (node) node.remove();
  // Prepend rather than replace: the box may already have something in it.
  const box = App.els.textarea;
  const existing = box.value;
  box.value = existing.trim() ? `${message}\n\n${existing}` : message;
  autosize(box);
  box.focus();
  box.setSelectionRange(message.length, message.length);
  Persist.saveDraft();
}

/* The user's own bubble, with thumbnails for anything attached. */
function appendUserMessage(text, attachments) {
  // Sending clears the previous run's change summary; the next run's changes
  // will replace it when that turn finishes.
  document.querySelectorAll('.change-summary').forEach((n) => n.remove());
  const node = appendMessage('user', text || '');
  if (!attachments || !attachments.length) return node;
  const tray = el('div', 'msg-attachments');
  for (const file of attachments) {
    const img = document.createElement('img');
    img.alt = file.name;
    img.title = file.name;
    const reader = new FileReader();
    reader.onload = (e) => { img.src = e.target.result; };
    reader.readAsDataURL(file);
    tray.appendChild(img);
  }
  node.querySelector('.msg-content').appendChild(tray);
  return node;
}

/* An inter-session message, styled like a user bubble but blue and labelled
   with the sender's session name instead of "user". */
function appendMailMessage(fromName, text) {
  const node = el('div', 'message user mail');
  node.appendChild(roleEl(fromName));
  const body = el('div', 'msg-content');
  const content = el('div', 'content-text');
  content.dataset.raw = text || '';
  content.innerHTML = md.render(text || '');
  body.appendChild(content);
  node.appendChild(body);
  node.appendChild(el('span', 'msg-time', clockTime()));
  App.els.messages.appendChild(node);
  autoscroll();
  return node;
}

/* Same markup the server renders in chat_messages.html, so refreshing the page
   does not change how a reasoning block looks. */
function appendReasoning() {
  const node = el('div', 'message thinking');
  // The "Reasoning" summary already names the row; the role gutter stays empty
  // so it does not read "reasoning" twice (matches chat_messages.html).
  const role = el('div', 'msg-role');
  role.title = 'reasoning';
  node.appendChild(role);
  const body = el('div', 'msg-content');
  const details = el('details', 'tool-details reasoning-details');
  details.open = true;
  const text = el('pre', 'reasoning-text');
  details.append(el('summary', 'tool-summary', 'Reasoning'), text);
  body.appendChild(details);
  node.appendChild(body);
  node.appendChild(el('span', 'msg-time', clockTime()));
  App.els.messages.appendChild(node);
  return text;
}

function collapseReasoning(textEl) {
  // Auto-expand overrides the "collapse when the reply starts" behaviour.
  if (App.expandTools.includes('reasoning')) return;
  const details = textEl.closest('details');
  if (details) details.open = false;
}

function appendToolCall(event) {
  const existing = App.els.messages.querySelector(
    `.message.tool[data-tool-call-id="${cssEscape(event.tool_call_id)}"]`);
  if (existing) return existing;
  const node = el('div', 'message tool pending');
  node.dataset.toolCallId = event.tool_call_id;
  node._args = event.args;
  node._name = event.name;
  node.appendChild(roleEl(event.name));
  const body = el('div', 'msg-content');
  const details = el('details', 'tool-details');
  const summary = el('summary', 'tool-summary');
  const label = el('span', 'tool-label', toolSummary(event.name, event.args));
  const elapsed = el('span', 'tool-elapsed', '0.0s');
  summary.append(el('span', 'spinner-dot'), label, elapsed);
  details.appendChild(summary);
  // The raw argument JSON used to be dumped here: noise. A subagent is the
  // exception, because its prompt is the only way to see what it was asked
  // while it works.
  if (event.name === 'task' && event.args && event.args.prompt) {
    details.appendChild(el('pre', 'tool-raw subagent-prompt', event.args.prompt));
  }
  body.appendChild(details);
  node.appendChild(body);
  node.appendChild(el('span', 'msg-time', clockTime()));
  App.els.messages.appendChild(node);
  startElapsed(node, elapsed);
  autoscroll();
  return node;
}

/* Tick a running duration so a slow tool never looks like a frozen UI.
 *
 * These timers must be able to end themselves. If the transcript is swapped out
 * while a tool is still running -- switching tabs mid-run, an htmx swap -- then
 * stopElapsed is never called for those nodes, and without the isConnected
 * check each one keeps waking up ten times a second, forever, writing to a
 * detached element. They accumulate for the life of the page. */
const ELAPSED_GUARD_MS = 60 * 60 * 1000;

function startElapsed(node, target) {
  const began = performance.now();
  const id = setInterval(() => {
    const age = performance.now() - began;
    if (!node.isConnected || age > ELAPSED_GUARD_MS) {
      clearElapsed(id);
      return;
    }
    target.textContent = (age / 1000).toFixed(1) + 's';
  }, 100);
  node._elapsedTimer = id;
  node._elapsedBegan = began;
  App.timers.add(id);
}

function clearElapsed(id) {
  clearInterval(id);
  App.timers.delete(id);
}

/* Belt and braces: drop every timer when the view is torn down. */
function stopAllElapsed() {
  [...App.timers].forEach(clearElapsed);
}

function stopElapsed(node, durationMs) {
  if (!node) return;
  if (node._elapsedTimer) {
    clearElapsed(node._elapsedTimer);
    node._elapsedTimer = null;
  }
  const target = node.querySelector('.tool-elapsed');
  if (!target) return;
  const secs = durationMs != null
    ? durationMs / 1000
    : (performance.now() - node._elapsedBegan) / 1000;
  // Sub-second calls are not interesting; drop the label entirely.
  if (secs < 1) target.remove();
  else target.textContent = secs.toFixed(1) + 's';
}

function shouldExpand(name) {
  return App.expandTools.includes(name);
}

function completeToolCall(event) {
  const node = App.els.messages.querySelector(`.message.tool[data-tool-call-id="${cssEscape(event.tool_call_id)}"]`);
  if (!node) return;
  node.classList.remove('pending');
  if (event.is_error) node.classList.add('tool-error');

  stopElapsed(node, event.duration_ms);
  const finished = node.querySelector(':scope > .msg-time');
  if (finished) {
    finished.textContent = clockTime();
    finished.title = `finished at ${clockTime()}`;
  }
  const label = node.querySelector('.tool-label');
  if (label) {
    label.textContent = event.title || event.name;
    if (['read', 'write', 'edit'].includes(node._name)) {
      const path = toolFilePath(event.title);
      if (path) { node.dataset.path = path; node.classList.add('fe-openable'); }
    }
  }
  const dot = node.querySelector('.spinner-dot');
  if (dot) dot.remove();

  const details = node.querySelector('.tool-details');
  details.open = shouldExpand(node._name);

  // The input the model passed, above the result, so a call reads like
  // "here is what it was asked, here is what came back".
  const hasInput = appendToolInput(details, node._name, node._args);

  if (event.diff && node._name === 'edit') {
    const { box, added, removed } = buildDiffBox(event.diff, event.lang);
    details.classList.add('diff-details');
    details.querySelector('.tool-summary').appendChild(diffStatNode(added, removed));
    details.appendChild(box);
    highlightToolCode(details);
    autoscroll();
    return;
  }
  const result = el('div', 'tool-result');
  // A label only earns its place when there is an input to distinguish it
  // from, or when the call failed.
  if (hasInput || event.is_error) {
    result.appendChild(el('div', 'tool-result-label', event.is_error ? 'error' : 'output'));
  }
  if (event.code) {
    const pre = el('pre', 'tool-raw code-lines');
    if (event.lang) pre.dataset.lang = event.lang;
    pre.dataset.start = String(event.code_start || 1);
    const code = el('code');
    code.textContent = event.code;
    pre.appendChild(code);
    result.appendChild(pre);
    highlightToolCode(result);
  } else {
    result.appendChild(el('pre', 'tool-raw', event.output || '(no output)'));
  }
  details.appendChild(result);
  autoscroll();
}

/* Everything the turn touched, in one place, so the user does not have to
   scroll back through the transcript to see what changed. */
const MAX_VISIBLE_CHANGES = 10;

function renderChangeSummary(changes) {
  if (!changes || !changes.files || !changes.files.length) return;
  document.querySelectorAll('.change-summary').forEach((n) => n.remove());
  const node = el('div', 'message change-summary');
  node.appendChild(roleEl('changes'));
  const body = el('div', 'msg-content');

  const files = changes.files;
  const overflow = files.length > MAX_VISIBLE_CHANGES;

  const head = el('div', 'change-head');
  head.appendChild(el('span', 'change-count',
    `${files.length} file${files.length === 1 ? '' : 's'} changed`));
  const total = el('span', 'diff-stat');
  total.append(el('span', 'diff-stat-add', `+${changes.added}`),
               el('span', 'diff-stat-del', `\u2212${changes.removed}`));
  head.appendChild(total);

  const list = el('div', 'change-files');
  if (overflow) {
    const toggle = el('button', 'change-toggle', `Show all ${files.length}`);
    toggle.addEventListener('click', () => {
      const show = node.dataset.all !== '1';
      node.dataset.all = show ? '1' : '0';
      list.querySelectorAll('.change-file').forEach((d, i) => {
        if (i >= MAX_VISIBLE_CHANGES) d.hidden = !show;
      });
      toggle.textContent = show ? 'Show less' : `Show all ${files.length}`;
    });
    head.appendChild(toggle);
  }
  body.appendChild(head);

  // Each file is its own dropdown, grouped like the edit/write blocks in the
  // transcript but all in one place.
  files.forEach((file, i) => {
    const details = el('details', 'tool-details change-file fe-openable');
    details.dataset.path = file.path;
    if (overflow && i >= MAX_VISIBLE_CHANGES) details.hidden = true;
    const summary = el('summary', 'tool-summary');
    const fstat = el('span', 'diff-stat');
    fstat.append(el('span', 'diff-stat-add', `+${file.added}`),
                 el('span', 'diff-stat-del', `\u2212${file.removed}`));
    summary.append(el('span', 'tool-label', shortPath(file.path)), fstat);
    details.appendChild(summary);
    details.appendChild(buildDiffBox(file.diffs.join('\n'), langForPath(file.path)).box);
    list.appendChild(details);
  });
  body.appendChild(list);
  highlightToolCode(list);

  node.appendChild(body);
  node.appendChild(el('span', 'msg-time', clockTime()));
  App.els.messages.appendChild(node);
  autoscroll();
}

/* Re-render the change summary from the database after a reload, so the list of
 * changed files survives navigating away and back. */
async function loadChangeSummary() {
  if (!App.sessionId) return;
  try {
    const resp = await fetch(`/api/sessions/${App.sessionId}/changes`);
    if (!resp.ok) return;
    renderChangeSummary(await resp.json());
  } catch (_) { /* leave whatever is on screen */ }
}

/* Unified diff with per-line colouring and a line-number gutter, in a
   collapsible box that starts open. Mirrors the server-side render in
   chat_messages.html so a reloaded page looks the same as the streamed one. */
function lnWidth(maxNum) {
  return String(Math.max(1, maxNum)).length + 'ch';
}

function numberedRow(num, cls) {
  const row = el('span', 'row' + (cls ? ' ' + cls : ''));
  row.append(el('span', 'ln', String(num)), el('span', 'lc'));
  return row;
}

function buildDiffBox(diff, lang) {
  const box = el('pre', 'diff-block');
  if (lang) box.dataset.lang = lang;
  let added = 0;
  let removed = 0;
  let oldNum = 0;
  let newNum = 0;
  let maxNum = 0;
  const hunkRe = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;
  for (const line of diff.replace(/\n+$/, '').split('\n')) {
    const m = hunkRe.exec(line);
    if (m) { oldNum = Number(m[1]); newNum = Number(m[2]); continue; }
    if (line.startsWith('+++') || line.startsWith('---')) continue;
    let cls = 'diff-ctx';
    let text = line;
    let num;
    if (line.startsWith('+')) { cls = 'diff-add'; added++; text = line.slice(1); num = newNum++; }
    else if (line.startsWith('-')) { cls = 'diff-del'; removed++; text = line.slice(1); num = oldNum++; }
    else { if (line.startsWith(' ')) text = line.slice(1); num = newNum; oldNum++; newNum++; }
    maxNum = Math.max(maxNum, num);
    const row = numberedRow(num, cls);
    row.querySelector('.lc').textContent = text || ' ';
    box.appendChild(row);
  }
  box.style.setProperty('--lnw', lnWidth(maxNum));
  return { box, added, removed };
}

function diffStatNode(added, removed) {
  const stat = el('span', 'diff-stat');
  stat.append(el('span', 'diff-stat-add', `+${added}`),
              el('span', 'diff-stat-del', `\u2212${removed}`));
  return stat;
}

/* Syntax-highlight read/write code and edit diffs after they hit the DOM, and
 * add the line-number gutter. Both are rendered as escaped text (Jinja or
 * textContent) and upgraded here; hljs takes raw text and returns token-wrapped
 * HTML. A data-hl flag makes the pass idempotent. */
function highlightToolCode(root) {
  const scope = root || document;

  /* Every code block gets a line-number gutter, highlighted or not. A written
   * .txt or .env has no language, but it still deserves numbered lines so the
   * user can point the model at "line 7". */
  scope.querySelectorAll('pre.code-lines').forEach((pre) => {
    if (pre.dataset.hl) return;
    pre.dataset.hl = '1';
    const code = pre.querySelector('code');
    if (!code) return;
    const lang = pre.dataset.lang || '';
    const start = Number(pre.dataset.start || 1);
    const lines = code.textContent.split('\n');
    if (lines.length && lines[lines.length - 1] === '') lines.pop();
    pre.style.setProperty('--lnw', lnWidth(start + Math.max(0, lines.length - 1)));
    const frag = document.createDocumentFragment();
    lines.forEach((line, i) => {
      const row = numberedRow(start + i);
      row.querySelector('.lc').innerHTML = md.highlight(line, lang) || ' ';
      frag.appendChild(row);
    });
    code.replaceChildren(frag);
  });

  scope.querySelectorAll('pre.diff-block[data-lang]').forEach((pre) => {
    if (pre.dataset.hl) return;
    pre.dataset.hl = '1';
    const lang = pre.dataset.lang;
    pre.querySelectorAll('.lc').forEach((lc) => {
      lc.innerHTML = md.highlight(lc.textContent, lang) || ' ';
    });
  });
}

/* Mirror of the server's _EXT_LANG, just enough to colour the change-summary
 * diffs whose language never came through a tool event. */
const EXT_LANG = {
  py: 'python', pyw: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript',
  jsx: 'javascript', ts: 'typescript', tsx: 'typescript', json: 'json', jsonc: 'json',
  sh: 'bash', bash: 'bash', zsh: 'bash', html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml',
  css: 'css', scss: 'css', sass: 'css', less: 'css', md: 'markdown', markdown: 'markdown',
  go: 'go', rs: 'rust', c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp',
  hh: 'cpp', java: 'java', kt: 'kotlin', sql: 'sql', yaml: 'yaml', yml: 'yaml',
  toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini', rb: 'ruby', php: 'php', cs: 'csharp',
  swift: 'swift', lua: 'lua', r: 'r', pl: 'perl', vim: 'vim', diff: 'diff', patch: 'diff',
};
function langForPath(path) {
  const name = String(path || '').toLowerCase();
  const base = name.split(/[\\/]/).pop();
  if (base === 'dockerfile') return 'dockerfile';
  if (base === 'makefile') return 'makefile';
  const dot = name.lastIndexOf('.');
  if (dot === -1) return '';
  return EXT_LANG[name.slice(dot + 1)] || '';
}

function toolSummary(name, args) {
  args = args || {};
  switch (name) {
    case 'read': return `Reading ${truncateStart(args.filePath, 60)}`;
    case 'edit': return `Editing ${truncateStart(args.filePath, 60)}`;
    case 'write': return `Writing ${truncateStart(args.filePath, 60)}`;
    case 'bash': return `Running ${truncate(args.command, 90)}`;
    case 'grep': return `Searching for ${truncate(args.pattern, 70)}`;
    case 'glob': return `Finding ${args.pattern || ''}`;
    case 'webfetch': return `Fetching ${truncate(args.url, 80)}`;
    case 'vision': return `Looking at ${truncate(args.url, 70)}`;
    case 'task': return `Subagent: ${args.description || ''}`;
    case 'send_message': return `To ${args.session || ''}: ${truncate(args.message, 70)}`;
    default: return name;
  }
}

/* The arguments the model passed to the tool, shown in the expanded row.
   Only tools whose input is not already obvious from the summary line show it:
   the bash command and the send_message body. Everything else (read, edit,
   write, grep, ...) already names its input in the summary, so repeating it is
   noise. */
function toolInputText(name, args) {
  args = args || {};
  if (name === 'bash') return args.command || '';
  if (name === 'send_message') return args.message || '';
  return null;
}

function formatToolInput(name, args) {
  if (!args || !Object.keys(args).length) return null;
  const text = toolInputText(name, args);
  if (!text) return null;
  return text.length > 3000 ? text.slice(0, 3000) + '\n\u2026 [truncated]' : text;
}

function appendToolInput(details, name, args) {
  const text = formatToolInput(name, args);
  if (!text) return false;
  const block = el('div', 'tool-result');
  block.appendChild(el('div', 'tool-result-label', 'input'));
  block.appendChild(el('pre', 'tool-raw', text));
  details.appendChild(block);
  return true;
}

function appendNotice(kind, text) {
  const node = el('div', `message notice notice-${kind}`);
  node.appendChild(roleEl(kind));
  const body = el('div', 'msg-content');
  body.appendChild(el('div', 'content-text', text));
  node.appendChild(body);
  App.els.messages.appendChild(node);
  autoscroll();
}

/* ── Interactive cards ───────────────────────────────────────────────────── */

function appendPermissionCard(event) {
  const existing = App.els.messages.querySelector(
    `.message.permission-card[data-tool-call-id="${cssEscape(event.tool_call_id)}"]`);
  if (existing) return existing;
  const node = el('div', 'message permission-card');
  node.dataset.toolCallId = event.tool_call_id;

  const kind = event.kind || 'shell';
  const head = el('div', 'permission-head');
  const detail = el('pre', 'permission-command');
  const sub = el('div', 'permission-dir');
  const actions = el('div', 'permission-actions');
  let pwWrap = null;

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
  } else if (kind === 'sudo') {
    head.textContent = 'Sudo password required';
    detail.textContent = event.command || JSON.stringify(event.args);
    sub.textContent = event.workdir || '';
    pwWrap = el('div', 'permission-sudo-pw');
    const pwInput = el('input');
    pwInput.type = 'password';
    pwInput.placeholder = 'Your sudo password';
    pwInput.autocomplete = 'off';
    pwInput.className = 'sudo-pw-input';
    const showBtn = el('button', 'sudo-show-btn');
    showBtn.textContent = '\u{1F441}';
    showBtn.title = 'Show password';
    showBtn.onclick = () => {
      pwInput.type = pwInput.type === 'password' ? 'text' : 'password';
    };
    pwWrap.append(pwInput, showBtn);
    const approveOnce = button('Approve', 'btn-approve', () => {
      const pwd = pwInput.value.trim();
      if (!pwd) { pwInput.focus(); return; }
      finish('approve', pwd, 'once');
    });
    const approveAll = button('Approve all this session', 'btn-approve-all');
    approveAll.disabled = true;
    approveAll.title = 'Sudo passwords are never saved';
    actions.append(approveOnce, approveAll, button('Reject', 'btn-reject', reject));
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

  async function reject() {
    const why = await ui.prompt('Optional: tell the agent why, so it can try something else.', {
      title: 'Reject this call',
      placeholder: 'e.g. use the staging database instead',
      confirmLabel: 'Reject',
    });
    if (why === null) return;
    finish('reject', why, 'once');
  }

  function finish(action, value, scope) {
    if (action === 'approve') {
      // The tool call bubble that follows already shows the command, so an
      // "Approved" card is pure noise taking up several lines.
      node.remove();
    } else {
      // A rejection leaves no other trace, so keep one compact line.
      actions.remove();
      detail.remove();
      sub.remove();
      node.className = 'message notice permission-resolved';
      head.textContent = `Rejected: ${truncate(event.command || event.path || 'tool call', 90)}`;
    }
    if (scope === 'session') markAutoApprove(true);
    resolveToolCall(event.tool_call_id, action, value, scope, event.scope);
  }

  node.append(head, detail, sub, ...(pwWrap ? [pwWrap] : []), actions);
  App.els.messages.appendChild(node);
  autoscroll();
  actions.querySelector('button')?.focus();
}

function shortPath(path) {
  if (!path) return 'this directory';
  const parts = String(path).split('/').filter(Boolean);
  return parts.length > 2 ? `.../${parts.slice(-2).join('/')}` : path;
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
}

/* ── Compaction ──────────────────────────────────────────────────────────── */

const THRESHOLD_STEPS = [4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1000000];

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

/* Everything before the last message is what gets re-read, so a change up
   there is charged again in full. Better to ask than to surprise. */
function openCacheModal(pause) {
  const money = (n) => `$${Number(n).toFixed(4)}`;
  document.getElementById('cache-detail').textContent =
    `Up to ${Number(pause.lost).toLocaleString()} cached tokens will be re-read at the `
    + `uncached rate: at most ${money(pause.cost)}, against ${money(pause.saved_cost)} `
    + `had the cache held. Cause: ${pause.reason}.`;
  document.getElementById('cache-modal').hidden = false;
}

async function acceptCacheWarning() {
  closeModal('cache-modal');
  await streamRequest(`/api/sessions/${App.sessionId}/accept-cache-warning`,
                      { method: 'POST' });
}

function openCompactModal() {
  const ring = document.querySelector('.context-ring');
  const stats = document.getElementById('compact-stats');
  stats.textContent = ring ? ring.title.split('\n')[0] : '';
  document.getElementById('compact-extra').value = '';
  document.getElementById('compact-modal').hidden = false;
  document.getElementById('compact-extra').focus();
  loadCompactPrompt();
}

async function confirmCompaction() {
  const extra = document.getElementById('compact-extra').value;
  const promptBox = document.getElementById('compact-prompt');
  const override = promptBox && promptBox.value !== promptBox.dataset.saved
    ? promptBox.value
    : '';
  closeModal('compact-modal');

  const form = new FormData();
  form.append('extra_instructions', extra);
  form.append('prompt_override', override);

  // Summarising a long transcript is slow, so show the summary as it is
  // written. Previously this was a static notice that vanished on failure.
  await streamRequest(`/api/sessions/${App.sessionId}/compact`, { method: 'POST', body: form });
  refreshMeta();
}

/* A preset is a setting on the session, not a one-off: it holds until it is
   changed again. The text box below stays per-run. */
async function switchCompactPreset(name) {
  const box = document.getElementById('compact-prompt');
  const body = new FormData();
  const resp = await fetch(`/api/sessions/${App.sessionId}/compact-profile`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }).then((r) => r.json()).catch(() => null);
  if (!resp || !resp.ok) {
    appendNotice('error', 'Could not switch the summarising prompt.');
    return;
  }
  box.dataset.saved = resp.prompt || '';
  box.value = resp.prompt || '';
}

function resetCompactPrompt() {
  const box = document.getElementById('compact-prompt');
  if (box) box.value = box.dataset.saved || '';
}

/* Show the prompt that will actually be used, so it can be adjusted for this
   run without editing the saved one. */
async function loadCompactPrompt(known) {
  const box = document.getElementById('compact-prompt');
  if (!box) return;
  const data = await fetch(`/api/sessions/${App.sessionId}/compact-prompt`)
    .then((r) => r.json())
    .catch(() => ({ prompt: '', presets: [], selected: 'default' }));
  const text = known || data.prompt || '';

  const picker = document.getElementById('compact-preset');
  if (picker) {
    picker.innerHTML = '';
    (data.presets || []).forEach((name) => {
      picker.appendChild(new Option(name, name, false, name === data.selected));
    });
    // Nothing to switch between until a second one exists.
    const row = document.getElementById('compact-preset-row');
    if (row) row.hidden = (data.presets || []).length < 2;
  }
  box.dataset.saved = text;
  box.value = text;
}

/* Which prompt is running, and which one is waiting, are different questions.
   The modal answers both explicitly rather than showing one and describing the
   other in a sentence. */
let promptState = { live: '', pending: null, view: 'live' };

async function openSystemPrompt() {
  document.querySelectorAll('.dropdown-menu').forEach((m) => { m.hidden = true; });
  const box = document.getElementById('session-prompt');
  box.value = 'Loading...';
  document.getElementById('prompt-modal').hidden = false;
  const data = await fetch(`/api/sessions/${App.sessionId}/system-prompt`)
    .then((r) => r.json()).catch(() => null);
  if (!data) { box.value = ''; return; }
  promptState = { ...data, view: data.pending ? 'pending' : 'live' };
  renderPromptModal();
}

function renderPromptModal() {
  const { live, pending, profile, custom, started, view } = promptState;
  const origin = document.getElementById('prompt-origin');
  const tabs = document.getElementById('prompt-tabs');

  const source = custom
    ? 'a prompt written for this session'
    : `the shared "${profile}" prompt`;
  origin.textContent = pending
    ? `Running ${source}. A change is queued and swaps in at the next compaction, `
      + 'which rebuilds the prefix anyway so the switch is free.'
    : started
      ? `Running ${source}. A change saved here is queued until the next compaction, `
        + 'because switching mid-conversation re-reads every token of it.'
      : `Running ${source}. Nothing has been sent yet, so a change applies immediately.`;

  tabs.hidden = !pending;
  tabs.querySelectorAll('.prompt-tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.view === view);
  });
  const box = document.getElementById('session-prompt');
  box.value = (view === 'pending' ? pending : live) || '';
  box.dataset.original = box.value;
  markPendingPrompt(!!pending);
}

function showPromptView(view) {
  promptState.view = view;
  renderPromptModal();
}

/* The menu is rendered server-side, so without this the queued marker only
   appeared after a full page reload. */
function markPendingPrompt(pending) {
  const btn = document.getElementById('system-prompt-item');
  if (!btn) return;
  const badge = btn.querySelector('.menu-badge');
  if (badge) badge.remove();
  if (pending) {
    const span = document.createElement('span');
    span.className = 'menu-badge';
    span.textContent = ' \u2022 update queued';
    btn.appendChild(span);
  }
}

async function discardPendingPrompt() {
  const resp = await fetch(`/api/sessions/${App.sessionId}/system-prompt/pending`, {
    method: 'DELETE',
  }).catch(() => null);
  if (!resp || !resp.ok) { appendNotice('error', 'Could not discard the queued change.'); return; }
  promptState.pending = null;
  promptState.view = 'live';
  renderPromptModal();
  appendNotice('info', 'Queued prompt change discarded. This session stays on the prompt it is using.');
}

const PROMPT_SAVE_MESSAGE = {
  queued: 'System prompt saved. It swaps in at the next compaction \u2014 switching now '
        + 'would re-read the whole conversation and bill it again.',
  applied: 'System prompt updated. Nothing has been sent yet, so it is already in use.',
  unchanged: 'That is already the prompt this session is using \u2014 nothing to change.',
  already_queued: 'That change is already queued for the next compaction.',
  cancelled: 'Back to the prompt already in use, so the queued change was dropped.',
};

async function saveSystemPrompt(text) {
  const box = document.getElementById('session-prompt');
  const prompt = text !== undefined ? text : box.value;
  const resp = await fetch(`/api/sessions/${App.sessionId}/system-prompt`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt }),
  }).catch(() => null);
  if (!resp || !resp.ok) { appendNotice('error', 'Could not save the prompt.'); return; }
  const data = await resp.json().catch(() => ({}));
  closeModal('prompt-modal');
  markPendingPrompt(data.status === 'queued' || data.status === 'already_queued');
  appendNotice('info', PROMPT_SAVE_MESSAGE[data.status] || 'System prompt saved.');
}

/* Empty means "whatever the shared prompt renders to now". */
function resetSystemPrompt() {
  saveSystemPrompt('');
}

function openThresholdModal() {
  closeModal('compact-modal');
  document.querySelectorAll('.dropdown-menu').forEach((m) => { m.hidden = true; });

  const { threshold } = currentUsage();
  const slider = document.getElementById('threshold-slider');
  slider.max = String(THRESHOLD_STEPS.length - 1);
  let index = THRESHOLD_STEPS.findIndex((s) => s >= threshold);
  slider.value = String(index < 0 ? THRESHOLD_STEPS.length - 1 : index);

  updateThresholdLabel();
  document.getElementById('threshold-modal').hidden = false;
}

function updateThresholdLabel() {
  const value = THRESHOLD_STEPS[Number(document.getElementById('threshold-slider').value)] || THRESHOLD_STEPS.at(-1);
  document.getElementById('threshold-value').textContent = formatTokens(value);
  // Rough guide: a cached full-context request at V4 Pro's cache-hit rate.
  const perRequest = (value * 0.003625) / 1000000;
  document.getElementById('threshold-cost').textContent =
    `At this size a cached request costs about $${perRequest.toFixed(4)}; `
    + `an uncached one about $${((value * 0.435) / 1000000).toFixed(3)}.`;
}

async function saveThreshold() {
  const value = THRESHOLD_STEPS[Number(document.getElementById('threshold-slider').value)] || THRESHOLD_STEPS.at(-1);
  closeModal('threshold-modal');

  const form = new FormData();
  form.append('threshold', String(value));

  await fetch(`/api/sessions/${App.sessionId}/compact-threshold`, { method: 'POST', body: form });
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

/* Remove the stale DOM for turns the server is about to delete. */
/* ── Session status + notification sounds ────────────────────────────────── */

const POLL_INTERVAL_MS = 2000;
/* Escalating so a long outage is not thousands of failed requests. */
const RETRY_BACKOFF_MS = [2000, 5000, 10000, 30000];

const Notifier = {
  enabled: true,
  ctx: null,
  lastUnseen: {},

  volume: 0.5,
  kind: 'click',

  init() {
    this.enabled = document.body.dataset.sound !== 'off';
    this.volume = parseFloat(document.body.dataset.soundVolume || '0.5');
    this.kind = document.body.dataset.soundKind || 'click';
  },

  play(kind) {
    if (!this.enabled) return;
    const sound = kind || this.kind || 'click';
    try {
      this.ctx = this.ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === 'suspended') this.ctx.resume();
      const vol = this.volume || 0.5;
      let tones;
      if (sound === 'chime') tones = [1047, 1319, 1568];
      else if (sound === 'knock') tones = [200, 300, 200];
      else if (sound === 'click') tones = [1200, 1500];
      else if (kind === 'waiting') tones = [660, 880];
      else if (kind === 'error') tones = [300, 220];
      else tones = [880];
      tones.forEach((freq, i) => {
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();
        const start = this.ctx.currentTime + i * 0.11;
        osc.type = 'sine';
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(vol * 0.2, start + 0.012);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.10);
        osc.connect(gain).connect(this.ctx.destination);
        osc.start(start);
        osc.stop(start + 0.12);
      });
    } catch (_) {}
  },

  failures: 0,
  nextAttemptAt: 0,

  /* One timer drives everything: it counts down the retry and decides when the
     next poll is due. Creating a timer per failure is how you end up with a
     page full of intervals nobody owns. */
  tick() {
    const now = performance.now();
    if (this.failures) this.showOffline(now);
    if (now >= this.nextAttemptAt) this.poll();
  },

  showOffline(now) {
    const banner = document.getElementById('offline-banner');
    if (!banner) return;
    const secs = Math.max(0, Math.ceil((this.nextAttemptAt - now) / 1000));
    banner.hidden = false;
    banner.textContent = secs
      ? `Can't reach the server \u2014 retrying in ${secs}s (attempt ${this.failures})`
      : `Reconnecting\u2026 (attempt ${this.failures + 1})`;
  },

  hideOffline() {
    const banner = document.getElementById('offline-banner');
    if (banner) { banner.hidden = true; banner.textContent = ''; }
  },

  async poll() {
    // Claim the next slot up front so a slow request cannot overlap itself.
    this.nextAttemptAt = performance.now() + POLL_INTERVAL_MS;
    let data;
    try {
      data = await (await fetch('/api/status')).json();
    } catch (_) {
      this.failures += 1;
      const wait = RETRY_BACKOFF_MS[Math.min(this.failures - 1, RETRY_BACKOFF_MS.length - 1)];
      this.nextAttemptAt = performance.now() + wait;
      this.showOffline(performance.now());
      return;
    }
    if (this.failures) {
      this.failures = 0;
      this.hideOffline();
      // Pick up anything that happened while we were disconnected.
      refreshTabBar();
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

function previewSound() {
  const sel = document.getElementById('sound-choice');
  const vol = parseFloat(document.getElementById('sound-volume')?.value || '0.5');
  const val = sel.value;
  if (['click', 'chime', 'knock'].includes(val)) {
    Notifier.volume = vol;
    Notifier.play(val);
  } else {
    const audio = new Audio('/_settings/sounds/' + encodeURIComponent(val) + '/play');
    audio.volume = vol;
    audio.play().catch(() => {});
  }
}

async function saveSoundSetting() {
  const sel = document.getElementById('sound-choice');
  const vol = document.getElementById('sound-volume');
  Notifier.kind = sel.value;
  Notifier.volume = parseFloat(vol.value || '0.5');
  const form = new FormData();
  form.append('sound', sel.value);
  form.append('volume', vol.value);
  await fetch('/_settings/sound', { method: 'POST', body: form });
}

async function uploadSound(input) {
  const file = input.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const r = await fetch('/_settings/sounds/upload', { method: 'POST', body: form });
    const data = await r.json();
    if (data.ok) {
      const sel = document.getElementById('sound-choice');
      const opt = new Option(data.name, data.name, true, true);
      sel.appendChild(opt);
      sel.value = data.name;
      saveSoundSetting();
    } else {
      ui.alert(data.error || 'Upload failed', 'Upload failed');
    }
  } catch (e) {
    ui.alert(String(e), 'Upload failed');
  }
  input.value = '';
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
      updateComposerButtons();
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

const MIC_TITLE = 'Dictate \u2014 click to toggle, or hold Right Ctrl to talk';

/* Persisted microphone preferences: input gain (dB) and the chosen input
 * device. Both live in localStorage because they are browser/device concerns --
 * a deviceId is per-browser and per-origin, so a server round-trip would not
 * round-trip meaningfully across machines, and gain is applied by a Web Audio
 * GainNode on this side. */
function micGainDb() {
  const v = localStorage.getItem('micGain');
  return v === null ? 0 : Number(v) || 0;
}
function saveMicGain(db) { localStorage.setItem('micGain', String(db)); }
function micDeviceId() { return localStorage.getItem('micDeviceId') || ''; }
function saveMicDeviceId(id) {
  if (id) localStorage.setItem('micDeviceId', id);
  else localStorage.removeItem('micDeviceId');
}
function withMicDevice(audio) {
  const deviceId = micDeviceId();
  return deviceId ? { ...audio, deviceId: { exact: deviceId } } : audio;
}

const Dictation = {
  recording: false,
  starting: false,      // set synchronously, before any await
  pushToTalk: false,
  recorder: null,
  chunks: [],
  streamRef: null,
  audioCtx: null,
  gain: null,
  dest: null,
  src: null,
  streamCtx: null,     // 16 kHz context used by the streaming worklet
  workletNode: null,
  ws: null,
  partial: '',
  finalText: '',
  insertAt: 0,         // caret offset where the live dictation segment starts
  insertedLen: 0,      // length of the text currently occupying that segment
  lastInserted: '',    // last partial written to the textarea (skip no-op writes)
  analyser: null,
  rafId: null,
  meterGeneration: 0,   // stale animation loops check this and exit
  meterLevel: 0,       // smoothed 0..1, so the bars don't jitter frame to frame
  transcribeTimer: null,
  els: {},

  init() {
    this.els.button = document.getElementById('mic-btn');
    this.els.meter = document.getElementById('mic-meter');
    this.els.status = document.getElementById('stt-status');
    this.els.elapsed = document.getElementById('stt-elapsed');
    // Streaming dictation needs the sherpa-onnx model server-side; the session
    // page carries a flag only when it is available.
    const view = document.getElementById('session-view');
    this.streamingAvailable = !!(view && view.dataset.sttStreaming === '1');
    if (!this.els.button || this.els.button.dataset.bound) return;
    this.els.button.dataset.bound = '1';
    this.els.button.addEventListener('click', () => this.toggle());
  },

  async toggle() {
    this.pushToTalk = false;
    if (this.recording) {
      const text = await this.stop();
      if (text) insertAtCursor(App.els.textarea, text);
    } else {
      await this.start();
    }
  },

  /* Hold to talk: start on keydown, transcribe and insert on release. */
  async hold() {
    if (this.recording || this.starting) return;
    this.pushToTalk = true;
    await this.start();
  },

  async release() {
    if (!this.pushToTalk) return;
    this.pushToTalk = false;
    if (!this.recording) return;
    const text = await this.stop();
    if (text) insertAtCursor(App.els.textarea, text);
  },

  async start() {
    // Guard synchronously. Setting `recording` after the await let two quick
    // triggers both get through, which spawned a second meter loop that nothing
    // tracked and that then ran forever.
    if (this.recording || this.starting) return;
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      appendNotice('error', 'This browser cannot record audio.');
      return;
    }
    this.starting = true;

    if (this.streamingAvailable) {
      await this.startStreaming();
      this.starting = false;
      return;
    }

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        // autoGainControl is off: browser AGC boosts silence up to a target
        // level, which is why quiet pauses read as loud on the meter.
        audio: withMicDevice({ echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1 }),
      });
    } catch (err) {
      this.starting = false;
      appendNotice('error', `Microphone unavailable: ${err.message}`);
      return;
    }

    // A toggle-off may have landed while getUserMedia was in flight.
    if (!this.starting) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }

    try {
      this.streamRef = stream;
      this.chunks = [];
      this.ensureAudioGraph();
      const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
        .find((t) => MediaRecorder.isTypeSupported(t)) || '';
      this.recorder = new MediaRecorder(this.dest.stream, mime ? { mimeType: mime } : undefined);
      this.recorder.ondataavailable = (e) => { if (e.data.size) this.chunks.push(e.data); };
      this.recorder.onerror = () => { this.teardown(); };
      this.recorder.start(250);
      this.recording = true;
      updateComposerButtons();
      this.els.button.classList.add('recording');
      this.startMeter();
    } catch (err) {
      appendNotice('error', `Could not start recording: ${err.message}`);
      this.teardown();
    } finally {
      this.starting = false;
    }
  },

  async stop() {
    this.starting = false;
    if (!this.recording) {
      this.teardown();
      return '';
    }
    if (this.streamingAvailable && this.ws) {
      return await this.stopStreaming();
    }
    if (!this.recorder) {
      this.teardown();
      return '';
    }
    this.recording = false;
    updateComposerButtons();
    this.els.button.classList.remove('recording');
    this.els.button.classList.add('transcribing');
    this.stopMeter();
    this.showTranscribing();

    let blob;
    try {
      blob = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('recorder did not stop')), 5000);
        this.recorder.onstop = () => {
          clearTimeout(timer);
          resolve(new Blob(this.chunks, { type: this.recorder.mimeType }));
        };
        this.recorder.stop();
      });
    } catch (err) {
      appendNotice('error', `Recording failed: ${err.message}`);
      this.teardown();
      return '';
    }

    this.releaseStream();

    let text = '';
    try {
      const form = new FormData();
      form.append('audio', blob, mimeToName(this.recorder && this.recorder.mimeType));
      const resp = await fetch('/api/stt', { method: 'POST', body: form });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || 'transcription failed');
      text = (data.text || '').trim();
      if (!text) flashButton(this.els.button, 'no speech detected');
    } catch (err) {
      appendNotice('error', `Transcription failed: ${err.message}`);
    } finally {
      this.teardown();
    }
    return text;
  },

  /* ── Streaming dictation (sherpa-onnx) ─────────────────────────────────── */

  async loadWorklet() {
    if (!this.streamCtx || !this.streamCtx.audioWorklet) return false;
    // addModule is per-context; only re-add when the context is new.
    if (this._workletContext === this.streamCtx) return true;
    try {
      await this.streamCtx.audioWorklet.addModule('/static/js/stt-worklet.js');
      this._workletContext = this.streamCtx;
      return true;
    } catch (_) { return false; }
  },

  async startStreaming() {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: withMicDevice({ echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1 }),
      });
    } catch (err) {
      appendNotice('error', `Microphone unavailable: ${err.message}`);
      return;
    }
    // A toggle-off may have landed while getUserMedia was in flight.
    if (!this.starting) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }

    this.streamRef = stream;
    this.partial = '';
    const ta = App.els.textarea;
    this.insertAt = ta ? (ta.selectionStart ?? ta.value.length) : 0;
    this.insertedLen = 0;
    this.lastInserted = '';

    try {
      // A 16 kHz context, reused across dictation sessions: Chrome resamples
      // the input, and keeping one context means the worklet module stays
      // registered and addModule() is not re-run every toggle.
      if (!this.streamCtx || this.streamCtx.state === 'closed') {
        this.streamCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      }
      if (this.streamCtx.state === 'suspended') this.streamCtx.resume();
      const src = this.streamCtx.createMediaStreamSource(stream);
      this.gain = this.streamCtx.createGain();
      this.gain.gain.value = Math.pow(10, micGainDb() / 20);
      src.connect(this.gain);

      if (!(await this.loadWorklet())) throw new Error('AudioWorklet is not supported');
      this.workletNode = new AudioWorkletNode(this.streamCtx, 'stt-capture');
      this.workletNode.port.onmessage = (e) => {
        if (this.ws && this.ws.readyState === 1) this.ws.send(e.data);
      };
      this.gain.connect(this.workletNode);

      this.ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/stt/stream');
      this.ws.binaryType = 'arraybuffer';
      this.ws.onmessage = (e) => this.onStreamMessage(e);
      this.ws.onclose = () => { this.ws = null; };
      this.ws.onerror = () => { appendNotice('error', 'Streaming dictation connection failed.'); this.teardown(); };
    } catch (err) {
      appendNotice('error', `Could not start dictation: ${err.message}`);
      this.teardown();
      return;
    }

    this.recording = true;
    updateComposerButtons();
    this.els.button.classList.add('recording');
    this.audioCtx = this.streamCtx;
    this.startMeter();
  },

  async stopStreaming() {
    this.recording = false;
    updateComposerButtons();
    this.els.button.classList.remove('recording');
    this.els.button.classList.add('transcribing');
    this.stopMeter();
    this.showTranscribing();

    // Ask the server to flush its tail of silence and return the final text.
    const final = await new Promise((resolve) => {
      const ws = this.ws;
      if (!ws || ws.readyState !== 1) { resolve(this.partial); return; }
      const timeout = setTimeout(() => resolve(this.partial), 8000);
      const prev = ws.onmessage;
      ws.onmessage = (e) => {
        let data;
        try { data = JSON.parse(e.data); } catch (_) { return; }
        if (data.partial === false) {
          clearTimeout(timeout);
          ws.onmessage = prev;
          resolve((data.text || '').trim());
        }
      };
      ws.send('end');
    });

    // The text is already in the textarea; the final flush only tightens it.
    this.updateDictationSegment(final);
    this.teardown();
    if (!final) flashButton(this.els.button, 'no speech detected');
    return '';   // nothing to insert separately
  },

  onStreamMessage(e) {
    let data;
    try { data = JSON.parse(e.data); } catch (_) { return; }
    if (data.error) { appendNotice('error', data.error); this.teardown(); return; }
    this.partial = (data.text || '').trim();
    this.updateDictationSegment(this.partial);
  },

  /* Live dictation writes straight into the textarea at the caret: replace the
   * segment that started at insertAt with the latest hypothesis, so the rest of
   * the draft is untouched and the caret stays at the end of the new words. */
  updateDictationSegment(text) {
    const ta = App.els.textarea;
    if (!ta) return;
    // Only rewrite the textarea when the hypothesis actually changed: rewriting
    // on every partial would clear any selection the user is dragging and stop
    // them from typing alongside the recording.
    if (text === this.lastInserted) return;
    this.lastInserted = text;
    const start = this.insertAt;
    const end = this.insertAt + this.insertedLen;
    const before = ta.value.slice(0, start);
    const after = ta.value.slice(end);
    ta.value = before + text + after;
    this.insertedLen = text.length;
    ta.focus();
    ta.setSelectionRange(start + text.length, start + text.length);
    autosize(ta);
  },

  /* Transcription is the one stretch with no feedback: the recorder has stopped,
     the meter is gone, and whisper can take seconds on a long take. Say so, and
     count, so a slow one reads as slow rather than as nothing happening. */
  showTranscribing() {
    if (!this.els.status) return;
    const started = Date.now();
    this.els.status.hidden = false;
    if (this.els.elapsed) this.els.elapsed.textContent = '';
    clearInterval(this.transcribeTimer);
    this.transcribeTimer = setInterval(() => {
      // The node goes away when the session is swapped out; stop with it.
      if (!this.els.status || !this.els.status.isConnected) {
        clearInterval(this.transcribeTimer);
        return;
      }
      const secs = Math.floor((Date.now() - started) / 1000);
      if (this.els.elapsed) this.els.elapsed.textContent = secs >= 2 ? ` ${secs}s` : '';
    }, 250);
  },

  hideTranscribing() {
    clearInterval(this.transcribeTimer);
    this.transcribeTimer = null;
    if (this.els.status) this.els.status.hidden = true;
    if (this.els.elapsed) this.els.elapsed.textContent = '';
  },

  /* Return to a known-clean state from any path. */
  teardown() {
    this.recording = false;
    this.hideTranscribing();
    updateComposerButtons();
    this.starting = false;
    this.pushToTalk = false;
    this.stopMeter();
    this.releaseStream();
    this.recorder = null;
    this.chunks = [];
    if (this.els.button) {
      this.els.button.classList.remove('recording', 'transcribing');
      this.els.button.title = MIC_TITLE;
    }
  },

  /* Reuse one AudioContext and route the mic through a GainNode so the
   * persisted gain affects what is recorded, not just the meter. */
  ensureAudioGraph() {
    if (!this.audioCtx || this.audioCtx.state === 'closed') {
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (this.audioCtx.state === 'suspended') this.audioCtx.resume();
    this.src = this.audioCtx.createMediaStreamSource(this.streamRef);
    this.gain = this.audioCtx.createGain();
    this.gain.gain.value = Math.pow(10, micGainDb() / 20);
    this.dest = this.audioCtx.createMediaStreamDestination();
    this.src.connect(this.gain);
    this.gain.connect(this.dest);
  },

  releaseStream() {
    if (this.ws) { try { this.ws.close(); } catch (_) {} }
    this.ws = null;
    if (this.workletNode) { try { this.workletNode.disconnect(); } catch (_) {} }
    this.workletNode = null;
    for (const node of [this.src, this.gain, this.dest]) {
      if (node) { try { node.disconnect(); } catch (_) {} }
    }
    this.src = null;
    this.gain = null;
    this.dest = null;
    // streamCtx is deliberately kept open: AudioWorklet modules are registered
    // per-context, so closing it would require addModule() again before the next
    // AudioWorkletNode, and a fresh node would otherwise throw "Unknown name".
    if (this.streamRef) {
      this.streamRef.getTracks().forEach((t) => t.stop());
      this.streamRef = null;
    }
  },

  startMeter() {
    if (!this.els.meter || !this.gain) return;
    this.stopMeter();
    this.meterLevel = 0;
    const generation = ++this.meterGeneration;

    try {
      this.analyser = this.audioCtx.createAnalyser();
      this.analyser.fftSize = 512;
      // Tap the meter off the gain node, so it shows the same signal the
      // recorder captures (gain already applied).
      this.gain.connect(this.analyser);
    } catch (err) {
      this.analyser = null;
      return;   // no meter is fine; recording still works
    }

    this.els.meter.hidden = false;
    const bars = Array.from(this.els.meter.querySelectorAll('.mic-bar'));
    const data = new Uint8Array(this.analyser.frequencyBinCount);

    const tick = () => {
      // A superseded loop must not keep running: that was the runaway.
      if (generation !== this.meterGeneration || !this.recording || !this.analyser) return;
      this.analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) sum += (v - 128) ** 2;
      const rms = Math.sqrt(sum / data.length);
      // Map to dB (loudness is logarithmic) and smooth, so it reads as level
      // rather than as a jittery per-frame spike.
      const db = rms > 0.5 ? 20 * Math.log10(rms / 128) : -60;
      const target = Math.max(0, Math.min(1, (db + 45) / 45));
      this.meterLevel = this.meterLevel * 0.7 + target * 0.3;
      const level = this.meterLevel;
      bars.forEach((bar, i) => {
        const bias = 1 - Math.abs(i - (bars.length - 1) / 2) / bars.length;
        bar.style.height = `${Math.max(12, Math.min(100, level * 150 * bias))}%`;
      });
      this.rafId = requestAnimationFrame(tick);
    };
    this.rafId = requestAnimationFrame(tick);
  },

  stopMeter() {
    this.meterGeneration += 1;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = null;
    this.analyser = null;
    if (this.els.meter) {
      this.els.meter.hidden = true;
      this.els.meter.querySelectorAll('.mic-bar').forEach((b) => { b.style.height = ''; });
    }
  },
};

/* ── Microphone test (home page) ───────────────────────────────────────────── */

/* Lets you hear exactly what the mic sends into dictation, and trim the input
 * gain. It captures the raw microphone (no browser echo/noise/auto-gain), runs
 * it through a software GainNode, shows a smoothed dB meter, and can record a
 * short clip for playback. The browser cannot set hardware gain, but a GainNode
 * changes the recorded signal, which is what matters here. */
const MicTest = {
  active: false,
  recording: false,
  level: 0,
  stream: null,
  ctx: null,
  gain: null,
  analyser: null,
  dest: null,
  recorder: null,
  chunks: [],
  blob: null,
  rafId: null,
  autoStop: null,
  els: {},

  init() {
    this.release();
    const toggle = document.getElementById('mic-test-toggle');
    if (!toggle) return;
    this.els.toggle = toggle;
    this.els.meter = document.getElementById('mic-test-meter');
    this.els.gain = document.getElementById('mic-test-gain');
    this.els.gainOut = document.getElementById('mic-test-gain-out');
    this.els.record = document.getElementById('mic-test-record');
    this.els.play = document.getElementById('mic-test-play');
    this.els.audio = document.getElementById('mic-test-audio');
    this.els.device = document.getElementById('mic-test-device');

    if (this.els.meter) {
      this.els.meter.textContent = '';
      for (let i = 0; i < 18; i++) this.els.meter.appendChild(el('span', 'mic-test-bar'));
    }
    toggle.addEventListener('click', () => (this.active ? this.stop() : this.start()));
    if (this.els.gain) {
      // The gain is global (dictation uses it too), so load and persist it.
      this.els.gain.value = String(micGainDb());
      this.els.gain.addEventListener('input', () => {
        saveMicGain(Number(this.els.gain.value));
        this.setGain(Number(this.els.gain.value));
      });
      this.setGain(Number(this.els.gain.value));
    }
    if (this.els.record) this.els.record.addEventListener('click', () => this.toggleRecord());
    if (this.els.play) this.els.play.addEventListener('click', () => this.play());
    this.refreshDevices();
    if (this.els.device) this.els.device.addEventListener('change', () => this.selectDevice());
  },

  async refreshDevices() {
    if (!this.els.device) return;
    let devices = [];
    try {
      devices = (await navigator.mediaDevices.enumerateDevices())
        .filter((d) => d.kind === 'audioinput');
    } catch (_) { /* no list is fine; the default device still works */ }
    const current = micDeviceId();
    this.els.device.textContent = '';
    this.els.device.appendChild(new Option('Default microphone', ''));
    for (const d of devices) {
      const opt = new Option(d.label || `Microphone ${d.deviceId.slice(0, 8)}`, d.deviceId);
      opt.selected = d.deviceId === current;
      this.els.device.appendChild(opt);
    }
  },

  async selectDevice() {
    saveMicDeviceId(this.els.device ? this.els.device.value : '');
    // Restart the capture so the newly chosen device takes effect immediately.
    if (this.active) {
      this.stop();
      await this.start();
    }
  },

  release() {
    this.active = false;
    this.recording = false;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = null;
    if (this.autoStop) clearTimeout(this.autoStop);
    this.autoStop = null;
    if (this.recorder && this.recorder.state !== 'inactive') { try { this.recorder.stop(); } catch (_) {} }
    this.recorder = null;
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.ctx) { try { this.ctx.close(); } catch (_) {} }
    this.ctx = this.gain = this.analyser = this.dest = null;
    this.chunks = [];
    this.blob = null;
  },

  async start() {
    if (this.active) return;
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      appendNotice('error', 'This browser cannot record audio.');
      return;
    }
    let stream;
    try {
      // Raw: no echo/noise suppression and, crucially, no auto-gain, so the
      // meter and the recording reflect what the microphone actually hears.
      stream = await navigator.mediaDevices.getUserMedia({
        audio: withMicDevice({ echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 }),
      });
    } catch (err) {
      appendNotice('error', `Microphone unavailable: ${err.message}`);
      return;
    }
    this.active = true;
    this.stream = stream;
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    this.src = this.ctx.createMediaStreamSource(stream);
    this.gain = this.ctx.createGain();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.analyser.smoothingTimeConstant = 0.6;
    this.dest = this.ctx.createMediaStreamDestination();
    this.src.connect(this.gain);
    this.gain.connect(this.analyser);
    this.gain.connect(this.dest);
    this.setGain(this.els.gain ? Number(this.els.gain.value) : 0);

    this.els.meter.hidden = false;
    this.els.record.hidden = false;
    this.els.toggle.textContent = 'Stop mic test';
    this.meterLoop();
  },

  stop() {
    this.release();
    if (this.els.meter) this.els.meter.hidden = true;
    if (this.els.record) { this.els.record.hidden = true; this.els.record.textContent = 'Record'; }
    if (this.els.play) this.els.play.hidden = true;
    if (this.els.toggle) this.els.toggle.textContent = 'Start mic test';
    if (this.els.audio) this.els.audio.removeAttribute('src');
  },

  setGain(db) {
    const v = Number(db);
    if (this.els.gainOut) this.els.gainOut.textContent = v > 0 ? `+${v}` : String(v);
    if (this.gain) this.gain.gain.value = Math.pow(10, v / 20);
  },

  /* dB-scaled and exponentially smoothed, so the bars track loudness the way
   * the ear does instead of jittering frame to frame like a raw RMS readout. */
  meterLoop() {
    const bars = Array.from(this.els.meter.querySelectorAll('.mic-test-bar'));
    const data = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = () => {
      if (!this.active || !this.analyser) return;
      this.analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) sum += (v - 128) ** 2;
      const rms = Math.sqrt(sum / data.length);
      const db = rms > 0.5 ? 20 * Math.log10(rms / 128) : -60;
      const target = Math.max(0, Math.min(1, (db + 60) / 60));
      this.level = this.level * 0.75 + target * 0.25;
      bars.forEach((bar, i) => {
        const bias = 1 - Math.abs(i - (bars.length - 1) / 2) / bars.length;
        bar.style.height = `${Math.max(4, this.level * 100 * (0.4 + 0.6 * bias))}%`;
      });
      this.rafId = requestAnimationFrame(tick);
    };
    this.rafId = requestAnimationFrame(tick);
  },

  toggleRecord() {
    if (this.recording) this.stopRecording();
    else this.startRecording();
  },

  startRecording() {
    if (!this.active || this.recording) return;
    const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
      .find((t) => MediaRecorder.isTypeSupported(t)) || '';
    this.recorder = new MediaRecorder(this.dest.stream, mime ? { mimeType: mime } : undefined);
    this.chunks = [];
    this.recorder.ondataavailable = (e) => { if (e.data.size) this.chunks.push(e.data); };
    this.recorder.onstop = () => {
      this.blob = new Blob(this.chunks, { type: this.recorder.mimeType });
      if (this.els.audio) this.els.audio.src = URL.createObjectURL(this.blob);
      if (this.els.play) this.els.play.hidden = false;
    };
    this.recorder.start(250);
    this.recording = true;
    this.els.record.textContent = 'Stop & save';
    this.els.play.hidden = true;
    this.autoStop = setTimeout(() => { if (this.recording) this.stopRecording(); }, 10000);
  },

  stopRecording() {
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    this.recording = false;
    if (this.autoStop) clearTimeout(this.autoStop);
    this.autoStop = null;
    if (this.els.record) this.els.record.textContent = 'Record';
  },

  play() {
    if (this.els.audio && this.els.audio.src) this.els.audio.play();
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
  textarea.focus();
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  const before = textarea.value.slice(0, start);
  const spacer = before && !/\s$/.test(before) ? ' ' : '';
  textarea.setSelectionRange(start, end);
  // insertText goes through the browser's undo stack; assigning .value wipes it.
  if (!document.execCommand('insertText', false, spacer + text)) {
    const after = textarea.value.slice(end);
    textarea.value = before + spacer + text + after;
    const caret = (before + spacer + text).length;
    textarea.setSelectionRange(caret, caret);
  }
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
  // The threshold is a bare number; send it as one, or not at all when blank.
  if ('compact_threshold' in payload) {
    const n = Number(payload.compact_threshold);
    if (Number.isFinite(n) && n > 0) payload.compact_threshold = n;
    else delete payload.compact_threshold;
  }
  await fetch(`/api/sessions/${App.sessionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  location.reload();
}

async function renameSession() {
  const current = document.querySelector('.session-title')?.textContent.trim() || '';
  const name = await ui.prompt('Session name', { value: current, confirmLabel: 'Rename' });
  if (!name || name === current) return;
  await fetch(`/api/sessions/${App.sessionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  location.reload();
}

async function deleteSession(sessionId, name) {
  const ok = await ui.confirm(`"${name}" and its whole transcript will be deleted. This cannot be undone.`,
    { title: 'Delete session', confirmLabel: 'Delete' });
  if (!ok) return;
  await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
  if (sessionId === App.sessionId) location.href = '/';
  else location.reload();
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

async function closeTab(event, sessionId) {
  event.preventDefault();
  event.stopPropagation();
  // Remove from the DOM immediately for a snappy feel, but await the server so a
  // concurrent refreshTabBar can't re-render the tab from stale open_tabs state.
  event.target.closest('.tab-wrap')?.remove();
  if (sessionId === App.sessionId) location.href = '/';
  await fetch(`/_tab_close/${sessionId}`, { method: 'POST' }).catch(() => {});
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
  let height = Math.min(textarea.scrollHeight, 260);
  if (textarea.id === 'chat-textarea') {
    // Never shorter than the button column beside it, or the composer grows a
    // dead gap under the box whenever that column gains a row.
    const actions = document.querySelector('.composer-actions');
    if (actions) height = Math.max(height, actions.offsetHeight);
  }
  textarea.style.height = `${height}px`;
}

/* One button in that slot at a time.
 *
 * Send is live whenever there is something to send -- text, an attachment, or
 * speech being dictated -- and that includes during a run, where it queues.
 * With nothing to send during a run the same slot becomes Stop, so the column
 * never changes height and the composer never jumps after a message goes. */
function updateComposerButtons() {
  const box = App.els.textarea;
  const canSend = !!(
    (box && box.value.trim())
    || pendingImages.length
    || (typeof Dictation !== 'undefined' && Dictation.recording)
  );
  const showStop = App.streaming && !canSend;
  if (App.els.send) {
    App.els.send.hidden = showStop;
  }
  if (App.els.stop) App.els.stop.hidden = !showStop;
  autosize(box);
}

let scrollQueued = false;

function autoscroll() {
  const box = App.els.scroller;
  if (!box) return;
  updateJumpButton();
  // Only follow the stream if the user is already near the bottom.
  if (box.scrollHeight - box.scrollTop - box.clientHeight >= 200) return;
  // Smooth scrolling per token queues hundreds of overlapping animations, so
  // during a stream jump straight to the bottom instead.
  scrollToBottom(App.streaming);
}

/* Jump-to-bottom affordance. Autoscroll deliberately stops following the stream
   once the user scrolls up; without this there is no way back except dragging. */
function updateJumpButton() {
  const box = App.els.scroller;
  const btn = App.els.jump;
  if (!box || !btn) return;
  const away = box.scrollHeight - box.scrollTop - box.clientHeight;
  btn.classList.toggle('visible', away >= 200);
  btn.classList.toggle('pulsing', away >= 200 && App.streaming);
}

function initJumpButton() {
  App.els.jump = document.getElementById('jump-bottom');
  const box = App.els.scroller;
  if (!App.els.jump || !box || App.els.jump.dataset.bound) return;
  App.els.jump.dataset.bound = '1';
  App.els.jump.addEventListener('click', () => {
    scrollToBottom(false);
    App.els.jump.classList.remove('visible', 'pulsing');
  });
  box.addEventListener('scroll', updateJumpButton, { passive: true });
  updateJumpButton();
}

function scrollToBottom(instant) {
  const box = App.els.scroller;
  if (!box || scrollQueued) return;
  scrollQueued = true;
  requestAnimationFrame(() => {
    scrollQueued = false;
    box.scrollTo({ top: box.scrollHeight, behavior: instant ? 'auto' : 'smooth' });
    updateJumpButton();
  });
}

/* Matches the clocktime Jinja filter: 12-hour, no seconds. */
function clockTime(date) {
  return (date || new Date())
    .toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
}

function truncate(text, n) {
  text = String(text || '');
  return text.length > n ? `${text.slice(0, n)}...` : text;
}

/* Left-truncate so the end (a filename, a tail of a path) stays visible. */
function truncateStart(text, n) {
  text = String(text || '');
  return text.length > n ? `\u2026${text.slice(-(n - 1))}` : text;
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, '\\$&');
}

/* Right Ctrl, because a modifier is the only kind of key that is safe to hold.
 *
 * Caps Lock was the obvious candidate and is unusable: a page can read the caps
 * state but cannot set it, so holding it to talk leaves caps stuck on with no
 * way to put it back. A modifier has no state and no default action. */
const PUSH_TO_TALK = { code: 'ControlRight', label: 'Right Ctrl' };

function isPushToTalk(e) {
  // ctrlKey is set by this very key, so it cannot be a disqualifier.
  return e.code === PUSH_TO_TALK.code && !e.altKey && !e.metaKey && !e.shiftKey;
}

document.addEventListener('keydown', (e) => {
  if (e.target.id === 'chat-textarea' && e.key === 'Enter') {
    if (e.ctrlKey && e.shiftKey) {
      e.preventDefault();
      openBroadcast();
      return;
    }
    if (e.shiftKey) {
      // Line break: fall through to the textarea's default newline.
      return;
    }
    e.preventDefault();
    App.els.form?.requestSubmit();
    return;
  }
  if (isPushToTalk(e)) {
    e.preventDefault();
    if (e.repeat) return;          // key auto-repeat, not a new press
    Dictation.hold();
    return;
  }
  // Space reads the newest reply, the way it plays and pauses a video. Right
  // Alt was the plan until it turned out Firefox owns it. Only when the caret
  // is not in a field, and Space must still scroll nothing.
  if (e.code === 'Space' && !e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey
      && !isTyping(e.target)) {
    e.preventDefault();
    if (!e.repeat) playLatestReply();
    return;
  }
  if (e.key === 'Escape' && App.streaming) {
    e.preventDefault();
    stopStreaming();
  }
});

document.addEventListener('keyup', (e) => {
  if (e.code === PUSH_TO_TALK.code) Dictation.release();
});

// Releasing the key outside the window would otherwise leave the mic hot.
window.addEventListener('blur', () => Dictation.release());
// Last-resort teardown: never leave a mic stream or animation loop running.
window.addEventListener('pagehide', () => { Dictation.teardown(); stopAllElapsed(); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden && Dictation.pushToTalk) Dictation.release();
});

document.addEventListener('input', (e) => {
  if (e.target.id === 'chat-textarea') {
    // Typing during a run turns Stop back into Send, so a queued message can
    // go without stopping the work first.
    updateComposerButtons();
    saveDraftSoon();
  }
});

/* ── Image attachment ────────────────────────────────────────────────────── */

/* Images ride along with the message and are referenced by path. The agent
 * decides whether and how to look at them with the `vision` tool, rather than
 * the UI converting them to text up front and discarding the original. */
let pendingImages = [];

function handleImageAttach(input) {
  for (const file of input.files) {
    if (pendingImages.length >= 6) break;
    pendingImages.push(file);
  }
  input.value = '';
  renderAttachments();
}

function removeAttachment(index) {
  pendingImages.splice(index, 1);
  renderAttachments();
}

function renderAttachments() {
  updateComposerButtons();
  const tray = document.getElementById('attachments');
  if (!tray) return;
  tray.innerHTML = '';
  tray.hidden = pendingImages.length === 0;

  pendingImages.forEach((file, i) => {
    const chip = el('span', 'attachment');
    const img = document.createElement('img');
    img.alt = file.name;
    const reader = new FileReader();
    reader.onload = (e) => { img.src = e.target.result; };
    reader.readAsDataURL(file);

    const name = el('span', 'attachment-name');
    name.textContent = file.name;
    chip.append(img, name, button('\u2715', 'attachment-remove', () => removeAttachment(i)));
    tray.appendChild(chip);
  });
}

/* ── Per-session writable directories ────────────────────────────────────── */

async function addWriteDir(form) {
  const input = form.elements.path;
  const path = input.value.trim();
  if (!path) return;
  const resp = await fetch(`/api/sessions/${App.sessionId}/write-dirs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  const data = await resp.json();
  if (!resp.ok) { ui.alert(data.detail || 'Could not allow that path', 'Not allowed'); return; }
  input.value = '';
  refreshMeta();
}

async function revokeWriteDir(path) {
  await fetch(`/api/sessions/${App.sessionId}/write-dirs?path=${encodeURIComponent(path)}`,
    { method: 'DELETE' });
  refreshMeta();
}

/* ── Speech ──────────────────────────────────────────────────────────────── */

/* Reading a reply aloud, one growing chunk at a time.
 *
 * Synthesis is a few times faster than playback, so the trick is to start
 * speaking after a single sentence and then let the chunks get longer as a
 * buffer builds. A chunk boundary is a small unnatural pause; early on that is
 * a fair price for starting quickly, but once there is audio in reserve there
 * is no reason to keep paying it. So the next chunk takes one more sentence for
 * every clip already waiting, which is what stops a bulleted list being read as
 * three fast items, pause, three fast items.
 *
 * A short sentence is never a chunk on its own regardless of reserve -- "Yes."
 * followed by a gap sounds broken. RealtimeTTS calls this minimum_sentence_length
 * and lands on the same idea from the other direction.
 *
 * There is exactly one playback in the app. Starting a new one discards the old
 * one entirely, including its pause position: nothing about speech is stored
 * per message. */
const Speech = {
  el: null,            // the .message element being read
  sentences: [],
  cursor: 0,           // next sentence awaiting synthesis
  queue: [],           // { url } clips ready but not yet played
  current: null,       // the clip in the element right now
  token: 0,            // bumped on every stop, to strand in-flight fetches
  producing: false,
  audioEl: null, ctx: null, filter: null, gainNode: null,
  voice: '',
  speed: 1,
  volume: 0.66,
  tone: 20000,         // lowpass cutoff in Hz; 20k is effectively off
  MIN_CHARS: 60,
  MAX_SENTENCES: 5,
  ORPHAN_WORDS: 3,
  HARD_MAX: 8,

  async settings() {
    try {
      const s = await (await fetch('/api/tts/status')).json();
      this.voice = s.voice || s.default_voice || '';
      this.speed = s.speed || 1;
      this.volume = s.volume != null ? s.volume : 0.66;
      this.tone = s.tone || 20000;
      return s.available;
    } catch (_) { return false; }
  },

  /* One element for the whole app, reused for every clip.
   *
   * It has to be one, because a media element can only be adopted into a Web
   * Audio graph once. Building the graph gives us the tone control -- a browser
   * lowpass costs nothing and can be moved while a clip is playing, where
   * filtering during synthesis would mean re-rendering the audio to change it. */
  engine() {
    if (this.audioEl) return this.audioEl;
    const a = new Audio();
    a.addEventListener('ended', () => this.advance());
    a.addEventListener('error', () => this.advance());
    this.audioEl = a;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      this.ctx = new Ctx();
      this.filter = this.ctx.createBiquadFilter();
      this.filter.type = 'lowpass';
      this.filter.frequency.value = this.tone;
      this.filter.Q.value = 0.7;
      this.gainNode = this.ctx.createGain();
      this.gainNode.gain.value = this.volume;
      this.ctx.createMediaElementSource(a)
        .connect(this.filter).connect(this.gainNode).connect(this.ctx.destination);
      a.volume = 1;                       // level is the gain node's job now
    } catch (_) {
      // No Web Audio: still plays, just without the tone control.
      this.ctx = null;
      a.volume = this.volume;
    }
    return a;
  },

  playing() { return !!this.current && this.audioEl && !this.audioEl.paused; },

  /* Click the control on the reply that is already talking to pause it, click
   * again to carry on, click a different one to switch. */
  async toggle(el) {
    if (this.el === el && this.current) {
      if (this.audioEl.paused) this.resume(); else this.audioEl.pause();
      this.paint();
      return;
    }
    await this.start(el);
  },

  resume() {
    if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume();
    this.audioEl.play().catch(() => {});
  },

  async start(el) {
    this.stop();
    const raw = el.querySelector('.content-text')?.dataset.raw || '';
    if (!raw.trim()) return;
    const token = this.token;
    this.el = el;
    this.paint();

    let sentences = [];
    try {
      const resp = await fetch('/api/tts/plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: raw }),
      });
      sentences = (await resp.json()).sentences || [];
    } catch (_) { /* handled below */ }

    if (token !== this.token) return;          // superseded while planning
    if (!sentences.length) { this.stop(); return; }
    this.sentences = sentences;
    this.produce();
  },

  /* How many sentences the next clip should cover. One to begin with, plus one
   * for every clip already in reserve, then extended twice over: until it is
   * long enough to be worth speaking, and until it is not about to leave a stub
   * behind. A boundary before "Correct?" puts a pause in the worst place. */
  nextChunk() {
    const words = (s) => s.split(/\s+/).filter(Boolean).length;
    const size = Math.min(1 + this.queue.length, this.MAX_SENTENCES);
    let end = Math.min(this.cursor + size, this.sentences.length);
    while (end < this.sentences.length && end - this.cursor < this.HARD_MAX
           && this.sentences.slice(this.cursor, end).join(' ').length < this.MIN_CHARS) {
      end += 1;
    }
    while (end < this.sentences.length && end - this.cursor < this.HARD_MAX
           && words(this.sentences[end]) <= this.ORPHAN_WORDS) {
      end += 1;
    }
    return this.sentences.slice(this.cursor, end);
  },

  async produce() {
    if (this.producing) return;
    this.producing = true;
    const token = this.token;
    try {
      while (token === this.token && this.cursor < this.sentences.length) {
        // Two clips in hand covers the next synthesis with room to spare.
        // Stopping rather than waiting matters: a paused reply would otherwise
        // leave this loop awake forever, and advance() starts it again the
        // moment the reserve is drawn down.
        if (this.queue.length >= 2) break;
        const chunk = this.nextChunk();
        this.cursor += chunk.length;
        const url = await this.render(chunk.join(' '), token);
        if (token !== this.token) { if (url) URL.revokeObjectURL(url); return; }
        if (url) this.queue.push({ url });
        if (!this.current) this.playNext();
      }
    } finally {
      if (token === this.token) this.producing = false;
    }
  },

  async render(text, token) {
    try {
      const resp = await fetch('/api/tts/speak', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice: this.voice, speed: this.speed }),
      });
      if (!resp.ok || token !== this.token) return null;
      return URL.createObjectURL(await resp.blob());
    } catch (_) { return null; }
  },

  advance() {
    this.releaseCurrent();
    this.playNext();
    this.paint();
  },

  releaseCurrent() {
    if (this.current) { URL.revokeObjectURL(this.current.url); this.current = null; }
  },

  playNext() {
    const clip = this.queue.shift();
    if (!clip) {
      // Nothing buffered and nothing left to make: the reply is finished.
      if (this.cursor >= this.sentences.length) this.stop();
      return;
    }
    this.current = clip;
    const a = this.engine();
    a.src = clip.url;
    this.resume();
    this.paint();
    // Keep the pipeline turning once playback has drawn down the reserve.
    if (!this.producing) this.produce();
  },

  stop() {
    this.token += 1;
    if (this.audioEl) { this.audioEl.pause(); this.audioEl.removeAttribute('src'); }
    this.releaseCurrent();
    this.queue.forEach((c) => URL.revokeObjectURL(c.url));
    this.queue = [];
    this.sentences = [];
    this.cursor = 0;
    this.producing = false;
    const was = this.el;
    this.el = null;
    if (was) this.paint(was);
  },

  setVolume(v) {
    this.volume = v;
    if (this.gainNode) this.gainNode.gain.value = v;
    else if (this.audioEl) this.audioEl.volume = v;
    document.querySelectorAll('.vol-pop input').forEach((s) => { s.value = String(v); });
    this.save({ volume: v });
  },

  setTone(hz) {
    this.tone = hz;
    if (this.filter) this.filter.frequency.value = hz;
    this.save({ tone: hz });
  },

  save(fields) {
    const form = new FormData();
    Object.entries(fields).forEach(([k, v]) => form.append(k, String(v)));
    fetch('/_settings/tts', { method: 'POST', body: form }).catch(() => {});
  },

  /* The control reflects three states, and the reply being read is marked so it
   * can be found again without storing an id anywhere. */
  paint(target) {
    const el = target || this.el;
    document.querySelectorAll('.message.speaking').forEach((m) => {
      if (m !== this.el) m.classList.remove('speaking');
    });
    if (!el) return;
    const btn = el.querySelector('.play-btn');
    const active = this.el === el;
    const playing = active && this.playing();
    el.classList.toggle('speaking', !!active);
    if (btn) {
      btn.textContent = playing ? 'pause' : (active ? 'resume' : 'play');
      btn.title = playing ? 'Pause' : 'Read this reply aloud';
    }
  },
};

/* Dragging to select chat text has to stay intact as the cursor passes over
   messages whose copy/play/time controls appear on hover. Those buttons are
   form controls, and the browser drops the selection when a drag crosses one.
   While a plain-text drag is in progress we flag the body so CSS hides the
   controls; the highlight then survives the whole sweep. */
let selectingText = false;
document.addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  const interactive = e.target.closest('button, a, input, select, textarea, .msg-side, [contenteditable]');
  selectingText = !interactive;
  document.body.classList.toggle('selecting', selectingText);
});
document.addEventListener('mouseup', () => {
  selectingText = false;
  document.body.classList.remove('selecting');
});
// If the pointer is released outside the window, mouseup can be missed.
window.addEventListener('blur', () => {
  selectingText = false;
  document.body.classList.remove('selecting');
});

function setupMessageSide() {
  document.querySelectorAll('.message:not([data-side-done])').forEach((node) => {
    node.dataset.sideDone = '1';
    const side = el('span', 'msg-side');
    const time = node.querySelector('.msg-time');
    if (time) side.appendChild(time);

    const wrap = el('span', 'msg-copy');
    const btn = button('copy', '', () => copyMessage(node));
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
    btn.title = 'Copy to clipboard';
    wrap.appendChild(btn);
    side.appendChild(wrap);

    // Copy button only appears on expanded tool/thinking rows, or always on user/assistant.
    const details = node.querySelector('.tool-details');
    if (details) {
      wrap.style.display = 'none';
      details.addEventListener('toggle', () => {
        wrap.style.display = details.open ? '' : 'none';
      });
    }

    const nodeSide = side;
    // Position copy/play from the bottom of the side column so they never
    // overflow into the next message on short rows.
    const copyEl = side.querySelector('.msg-copy');
    const actionsEl = side.querySelector('.msg-actions');
    if (copyEl && actionsEl) {
      copyEl.style.top = '';
      actionsEl.style.top = '';
      const observer = new ResizeObserver(() => {
        const h = side.clientHeight;
        if (h < 58) {
          // Buttons are ~40px tall. Stack from bottom so they stay inside.
          actionsEl.style.top = Math.max(0, h - 20) + 'px';
          copyEl.style.top = Math.max(0, h - 42) + 'px';
        } else {
          actionsEl.style.top = '';
          copyEl.style.top = '';
        }
      });
      observer.observe(side);
      node._resizeObserver = observer;
    }

    node.appendChild(nodeSide);
  });
}

function attachPlayButtons() {
  if (!App.ttsAvailable) return;
  document.querySelectorAll('.message.assistant:not([data-play])').forEach((node) => {
    const text = node.querySelector('.content-text');
    if (!text || !text.dataset.raw?.trim()) return;
    node.dataset.play = '1';
    const side = node.querySelector('.msg-side');
    if (!side) return;

    const actions = el('span', 'msg-actions');
    actions.appendChild(button('play', 'play-btn', () => Speech.toggle(node)));
    const vol = el('span', 'vol-pop');
    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = '0'; slider.max = '1'; slider.step = '0.01';
    slider.value = String(Speech.volume);
    slider.setAttribute('orient', 'vertical');
    slider.title = 'Volume';
    slider.addEventListener('input', () => Speech.setVolume(parseFloat(slider.value)));
    vol.appendChild(slider);
    actions.appendChild(vol);
    side.appendChild(actions);
  });
}

function copyMessage(node) {
  const content = node.querySelector('.content-text');
  if (content) {
    const text = content.dataset.raw || content.textContent || '';
    navigator.clipboard.writeText(text.trim()).then(() => showCopyToast()).catch(() => {});
    return;
  }
  const toolOut = node.querySelector('.tool-raw, .diff-block');
  if (toolOut) {
    navigator.clipboard.writeText(toolOut.textContent.trim()).then(() => showCopyToast()).catch(() => {});
    return;
  }
  navigator.clipboard.writeText(node.textContent.trim()).then(() => showCopyToast()).catch(() => {});
}

/* Offer to take back the last user message, but only while nothing has answered
 * it and the agent is not running. Once the model has replied, deleting the
 * message would orphan that reply and silently invalidate the cache, so the
 * button simply never appears. */
function refreshRevertButtons() {
  document.querySelectorAll('.revert-btn').forEach((b) => b.remove());
  if (App.streaming || !App.els.messages) return;
  const users = [...App.els.messages.querySelectorAll('.message.user:not(.queued)')];
  const last = users[users.length - 1];
  if (!last) return;

  // Replied = an assistant message with visible text follows this one.
  let sibling = last.nextElementSibling;
  while (sibling) {
    const text = sibling.querySelector('.content-text');
    if (sibling.classList.contains('assistant') && text && text.textContent.trim()) return;
    sibling = sibling.nextElementSibling;
  }

  let side = last.querySelector('.msg-side');
  if (!side) return;
  let actions = side.querySelector('.msg-actions');
  if (!actions) {
    actions = el('span', 'msg-actions');
    side.appendChild(actions);
  }
  const btn = button('revert', 'revert-btn', () => revertLastMessage(last));
  btn.title = 'Take this message back — nothing has answered it yet';
  actions.appendChild(btn);
}

async function revertLastMessage(node) {
  const resp = await fetch(`/api/sessions/${App.sessionId}/last-message`, {
    method: 'DELETE',
  }).catch(() => null);
  if (!resp || !resp.ok) {
    let reason = 'Could not take that message back.';
    if (resp) {
      try {
        const data = await resp.json();
        if (data.reason === 'still running') reason = 'Stop the agent first, then take it back.';
        else if (data.reason === 'already replied') reason = 'Too late — the model has already replied.';
      } catch (_) { /* keep the generic message */ }
    }
    appendNotice('error', reason);
    return;
  }
  const { message } = await resp.json();
  const box = App.els.textarea;
  box.value = message + (box.value.trim() ? '\n\n' + box.value : '');
  autosize(box);
  box.focus();
  refreshTranscript();
}

let _copyToastTimer = null;
function showCopyToast() {
  let toast = document.getElementById('copy-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'copy-toast';
    toast.className = 'copy-toast';
    toast.textContent = 'Copied to clipboard';
    document.body.appendChild(toast);
  }
  toast.hidden = false;
  clearTimeout(_copyToastTimer);
  _copyToastTimer = setTimeout(() => { toast.hidden = true; }, 1600);
}

function isTyping(node) {
  if (!node) return false;
  if (node.isContentEditable) return true;
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(node.tagName);
}

/* Space reads the newest reply. Pressed again it pauses, and again resumes --
 * unless the agent has answered since, in which case the newest reply wins and
 * the old position is forgotten. Two quick presses start it over. */
const REPLAY_DOUBLE_TAP_MS = 400;
let lastPlayPress = 0;

function playLatestReply() {
  const all = document.querySelectorAll('.message.assistant[data-play]');
  const latest = all[all.length - 1];
  if (!latest) return;
  scrollToBottom();
  const now = performance.now();
  const doubleTap = now - lastPlayPress < REPLAY_DOUBLE_TAP_MS;
  lastPlayPress = now;
  if (doubleTap) { Speech.start(latest); return; }
  Speech.toggle(latest);
}

/* ── In-app file editor & browser ─────────────────────────────────────────── */

/* A read/write/edit title is the file path, optionally with a " (6 lines)" or
 * " (+2/-1)" summary tacked on. Strip that suffix to recover the path. */
function toolFilePath(title) {
  const t = String(title || '').trim().replace(/ \([^)]*\)$/, '').trim();
  // A left-truncated title ("\u2026tail") is not the real path; do not link it.
  if (!t || t.startsWith('\u2026')) return null;
  return t;
}

/* Tag read/write/edit tool blocks so their expanded body opens the editor. */
function markOpenableTools(root) {
  (root || document).querySelectorAll('.message.tool').forEach((node) => {
    if (node.dataset.pathSet) return;
    node.dataset.pathSet = '1';
    const role = node.querySelector('.msg-role');
    const name = role ? role.textContent.trim().toLowerCase() : '';
    if (!['read', 'write', 'edit'].includes(name)) return;
    const label = node.querySelector('.tool-label');
    const path = toolFilePath(label ? label.textContent : '');
    if (!path) return;
    node.dataset.path = path;
    node.classList.add('fe-openable');
  });
}

/* Open the editor when a file path is clicked in prose or a tool block body. */
document.addEventListener('click', (e) => {
  const ref = e.target.closest('a.file-ref');
  if (ref) {
    e.preventDefault();
    openFileRef(ref);
    return;
  }
  const tool = e.target.closest('.fe-openable[data-path]');
  if (tool && tool.dataset.path && !e.target.closest('.tool-summary, button, a, .msg-actions')) {
    // Selecting text (e.g. to copy) must not open the editor.
    const sel = window.getSelection();
    if (sel && sel.toString()) return;
    FileEditor.open(tool.dataset.path, {});
  }
});

/* A prose file reference may point at a directory, in which case the file
 * manager opens on it rather than the text editor. Ask the server which one it
 * is; if the lookup fails, fall through to the editor, which reports a clear
 * "does not exist" for anything that is not a file. */
async function openFileRef(ref) {
  const path = ref.dataset.path;
  const line = ref.dataset.line ? Number(ref.dataset.line) : null;
  const lineEnd = ref.dataset.lineEnd ? Number(ref.dataset.lineEnd) : null;
  try {
    const resp = await fetch(
      `/api/files/stat?session_id=${encodeURIComponent(App.sessionId)}&path=${encodeURIComponent(path)}`);
    if (resp.ok) {
      const st = await resp.json();
      if (st.is_dir) { FileBrowser.open(st.path); return; }
    }
  } catch (_) { /* fall through to the editor */ }
  FileEditor.open(path, { line, lineEnd });
}

/* Right-click a file path (in prose or a tool block) to copy it to the clipboard. */
const CopyPathMenu = (() => {
  let menu = null;
  let path = '';

  function ensure() {
    if (menu) return;
    menu = document.createElement('div');
    menu.id = 'copy-path-menu';
    menu.className = 'ctx-menu';
    menu.hidden = true;
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'ctx-item';
    item.textContent = 'Copy path';
    item.addEventListener('click', () => {
      navigator.clipboard.writeText(path).then(showCopyToast).catch(() => {});
      hide();
    });
    menu.appendChild(item);
    document.body.appendChild(menu);
  }

  function show(x, y, p) {
    ensure();
    path = p;
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    menu.hidden = false;
  }

  function hide() {
    if (menu) menu.hidden = true;
  }

  return { show, hide };
})();

document.addEventListener('contextmenu', (e) => {
  const ref = e.target.closest('a.file-ref');
  if (ref) { e.preventDefault(); CopyPathMenu.show(e.clientX, e.clientY, ref.dataset.path); return; }
  const tool = e.target.closest('.message.tool.fe-openable');
  if (tool && tool.dataset.path) {
    e.preventDefault();
    CopyPathMenu.show(e.clientX, e.clientY, tool.dataset.path);
  }
});

document.addEventListener('click', (e) => {
  if (!e.target.closest('.ctx-menu')) CopyPathMenu.hide();
});

const FileEditor = (() => {
  let dlg = null;
  // Wrap preference persists across file opens, until the app restarts.
  let wrapOn = true;
  // Whether the caret has been placed in the current file, so "Copy ref" knows
  // whether to append a line number even after the button takes focus away.
  let caretPlaced = false;
  const s = {
    sessionId: null,
    path: null,
    lang: '',
    content: '',
    truncated: false,
    readonly: false,
    dirty: false,
    line: null,
    lineEnd: null,
  };

  let bodyEl, highlightEl, textareaEl, statusEl, pathEl, wrapBtn, saveBtn, formatBtn;

  function mountEditor() {
    // Lives inside the session view, between the chat history and the composer,
    // so the session bar above and the composer below stay visible around it.
    const view = document.getElementById('session-view');
    const inputArea = document.getElementById('chat-input-area');
    if (view && inputArea) view.insertBefore(dlg, inputArea);
    else document.body.appendChild(dlg);
  }

  function ensure() {
    if (dlg) {
      // A tab switch replaces #session-view and detaches the editor; re-insert.
      if (!dlg.isConnected) mountEditor();
      return;
    }
    dlg = document.createElement('div');
    dlg.id = 'file-editor';
    dlg.className = 'file-editor';
    dlg.innerHTML =
      '<div class="fe-head">' +
        '<span class="fe-path"></span>' +
        '<span class="fe-actions">' +
          '<button type="button" class="fe-btn" data-fe="copy" title="Copy path and line number">Copy path</button>' +
          '<button type="button" class="fe-btn" data-fe="wrap" title="Toggle line wrap">Wrap</button>' +
          '<button type="button" class="fe-btn" data-fe="format" title="Format document">Format</button>' +
          '<button type="button" class="fe-btn fe-save" data-fe="save" title="Save (Ctrl+S)">Save</button>' +
          '<button type="button" class="fe-btn" data-fe="close" title="Close">&times;</button>' +
        '</span>' +
      '</div>' +
      '<div class="fe-body">' +
        '<div class="fe-highlight" aria-hidden="true"></div>' +
        '<textarea class="fe-textarea" spellcheck="false" wrap="off"></textarea>' +
      '</div>' +
      '<div class="fe-status"></div>';
    mountEditor();

    pathEl = dlg.querySelector('.fe-path');
    wrapBtn = dlg.querySelector('[data-fe=wrap]');
    saveBtn = dlg.querySelector('[data-fe=save]');
    formatBtn = dlg.querySelector('[data-fe=format]');
    bodyEl = dlg.querySelector('.fe-body');
    highlightEl = dlg.querySelector('.fe-highlight');
    textareaEl = dlg.querySelector('.fe-textarea');
    statusEl = dlg.querySelector('.fe-status');

    dlg.querySelector('[data-fe=close]').addEventListener('click', close);
    dlg.querySelector('[data-fe=copy]').addEventListener('click', copyRef);
    wrapBtn.addEventListener('click', toggleWrap);
    saveBtn.addEventListener('click', save);
    formatBtn.addEventListener('click', formatDocument);
    // Header buttons must not steal focus, or the caret/selection highlight in
    // the textarea vanishes when one is clicked.
    dlg.querySelectorAll('.fe-actions button').forEach((b) => {
      b.addEventListener('mousedown', (e) => e.preventDefault());
    });
    textareaEl.addEventListener('focus', () => { caretPlaced = true; });
    textareaEl.addEventListener('input', onInput);
    textareaEl.addEventListener('scroll', syncScroll);
    textareaEl.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        save();
      }
    });
  }

  function show() {
    ensure();
    if (!dlg.classList.contains('open')) {
      dlg.classList.add('open');
      document.body.classList.add('editor-open');
    }
  }

  async function close() {
    if (!dlg) return;
    if (s.dirty) {
      const ok = await ui.confirm('Discard unsaved changes?', {
        title: 'Unsaved changes', confirmLabel: 'Discard', danger: true,
      });
      if (!ok) return;
    }
    dlg.classList.remove('open');
    document.body.classList.remove('editor-open');
  }

  /* The clipboard reference is the absolute path, with the cursor's line (or the
   * selected range) appended once the caret has been placed in this file. */
  function lineAt(index) {
    return textareaEl.value.slice(0, index).split('\n').length;
  }
  function copyRef() {
    let ref = s.path;
    if (caretPlaced) {
      const start = lineAt(textareaEl.selectionStart);
      let end = lineAt(textareaEl.selectionEnd);
      if (textareaEl.selectionEnd > textareaEl.selectionStart
          && textareaEl.value[textareaEl.selectionEnd - 1] === '\n') {
        end--;
      }
      ref += ':' + start + (end > start ? '-' + end : '');
    }
    navigator.clipboard.writeText(ref).then(showCopyToast).catch(() => {});
  }

  function setStatus(text, error) {
    statusEl.textContent = text;
    statusEl.classList.toggle('fe-status-error', !!error);
  }

  function updateButtons() {
    saveBtn.hidden = s.readonly || !s.dirty;
    formatBtn.hidden = s.readonly;
    wrapBtn.classList.toggle('active', wrapOn);
  }

  async function open(path, opts = {}) {
    if (!App.sessionId) return;
    ensure();
    s.sessionId = App.sessionId;
    s.path = path;
    s.line = opts.line || null;
    s.lineEnd = opts.lineEnd || null;
    s.dirty = false;
    s.readonly = false;
    caretPlaced = false;
    applyWrap();
    s.content = '';
    s.lang = '';
    s.truncated = false;

    // Load first, so a missing file never flashes an empty editor.
    let data;
    try {
      const resp = await fetch(
        `/api/files/read?session_id=${encodeURIComponent(App.sessionId)}&path=${encodeURIComponent(path)}`);
      if (resp.status === 404) {
        await ui.alert('Sorry, that file does not exist, so it cannot be opened or edited.', 'File not found');
        return;
      }
      if (!resp.ok) {
        let msg = `Could not open ${path}`;
        try { const j = await resp.json(); msg = j.detail || msg; } catch (_) {}
        await ui.alert(msg, 'Could not open file');
        return;
      }
      data = await resp.json();
    } catch (err) {
      await ui.alert(`Could not open ${path}: ${err}`, 'Could not open file');
      return;
    }

    s.path = data.path;
    s.content = data.content;
    s.lang = data.lang || '';
    s.truncated = data.truncated;
    // A truncated file is view-only: saving would silently discard the tail.
    s.readonly = !!data.truncated;
    pathEl.textContent = data.path;
    pathEl.title = data.path;

    textareaEl.value = s.content;
    renderHighlight();
    updateStatus();
    updateButtons();
    show();
    scrollToTarget();
  }

  function lineCount() {
    return textareaEl.value ? textareaEl.value.split('\n').length : 1;
  }

  function updateStatus() {
    let msg = `${lineCount()} lines`;
    if (s.truncated) msg += ' \u00b7 too large to edit';
    if (s.line) msg += ` \u00b7 jump to line ${s.line}`;
    setStatus(msg);
  }

  function inRange(n) {
    if (!s.line) return false;
    const end = s.lineEnd || s.line;
    return n >= s.line && n <= end;
  }

  /* Rebuild the highlighted layer and the line-number gutter. The gutter is a
   * fixed flex column, so a wrapped line keeps its number on the first visual
   * line and the continuation lines start under the code. Files past the size
   * limit skip highlighting and render plain escaped text, so a 2MB file still
   * edits without re-tokenising the whole thing every keystroke. */
  const HIGHLIGHT_LIMIT = 100000;
  function renderHighlight() {
    const text = textareaEl.value;
    // Keep the trailing empty line from a final newline, so the cursor's line
    // has a number even before anything is typed there.
    const lines = text.split('\n');
    if (!lines.length) lines.push('');
    bodyEl.style.setProperty('--lnw', lnWidth(Math.max(1, lines.length)));
    const plain = text.length > HIGHLIGHT_LIMIT;
    const frag = document.createDocumentFragment();
    lines.forEach((line, i) => {
      const n = i + 1;
      const row = el('div', 'fe-line' + (inRange(n) ? ' fe-hl' : ''));
      row.appendChild(el('span', 'fe-ln', String(n)));
      const code = el('span', 'fe-code');
      code.innerHTML = plain ? (md.escapeHtml(line) || ' ') : (md.highlight(line, s.lang) || ' ');
      row.appendChild(code);
      frag.appendChild(row);
    });
    highlightEl.replaceChildren(frag);
    syncScroll();
  }

  function syncScroll() {
    highlightEl.scrollTop = textareaEl.scrollTop;
    highlightEl.scrollLeft = textareaEl.scrollLeft;
  }

  /* Coalesce to at most one re-highlight per animation frame, so live syntax
   * colouring keeps up with fast typing without re-tokenising per key event. */
  let highlightFrame = null;
  function onInput() {
    const changed = textareaEl.value !== s.content;
    s.dirty = changed;
    // Editing clears the jump-to highlights: the text they marked has changed.
    if (changed && s.line) { s.line = null; s.lineEnd = null; }
    if (highlightFrame === null) {
      highlightFrame = requestAnimationFrame(() => {
        highlightFrame = null;
        renderHighlight();
      });
    }
    updateButtons();
  }

  function toggleWrap() {
    wrapOn = !wrapOn;
    applyWrap();
    renderHighlight();
    syncScroll();
  }

  function applyWrap() {
    textareaEl.wrap = wrapOn ? 'soft' : 'off';
    textareaEl.classList.toggle('wrap', wrapOn);
    highlightEl.classList.toggle('wrap', wrapOn);
    wrapBtn.classList.toggle('active', wrapOn);
  }

  async function save() {
    if (s.readonly) return;
    let resp;
    try {
      resp = await fetch('/api/files/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: s.sessionId,
          path: s.path,
          content: textareaEl.value,
        }),
      });
    } catch (err) {
      setStatus(`Could not save: ${err}`, true);
      return;
    }
    if (!resp.ok) {
      let msg = 'Could not save';
      try { const j = await resp.json(); msg = j.detail || msg; } catch (_) {}
      setStatus(msg, true);
      return;
    }
    s.content = textareaEl.value;
    s.dirty = false;
    renderHighlight();
    updateStatus();
    updateButtons();
  }

  /* ── Formatting ─────────────────────────────────────────────────────────── */

  /* The formatter is chosen server-side by file extension, so a .c gets
     clang-format, a .py gets black, a .css gets prettier, and so on. */
  async function formatDocument() {
    if (s.readonly) return;
    const before = textareaEl.value;
    setStatus('Formatting\u2026');
    let resp;
    try {
      resp = await fetch('/api/files/format', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: s.sessionId, path: s.path, content: before }),
      });
    } catch (err) {
      setStatus(`Could not format: ${err}`, true);
      return;
    }
    if (!resp.ok) {
      let msg = 'Could not format';
      try { const j = await resp.json(); msg = j.detail || msg; } catch (_) {}
      setStatus(msg, true);
      return;
    }
    let out;
    try { out = (await resp.json()).content; }
    catch (_) { setStatus('Could not format', true); return; }
    textareaEl.value = out;
    s.dirty = out !== s.content;
    // Formatting rewrites the text the jump-to highlight pointed at.
    if (s.line) { s.line = null; s.lineEnd = null; }
    renderHighlight();
    syncScroll();
    updateButtons();
    setStatus(out === before ? 'Already formatted' : `Formatted ${lineCount()} lines \u2014 save to keep`);
  }

  function scrollToTarget() {
    if (!s.line) return;
    const lineH = parseFloat(getComputedStyle(textareaEl).lineHeight) || 18.75;
    const padTop = parseFloat(getComputedStyle(textareaEl).paddingTop) || 6;
    textareaEl.scrollTop = Math.max(0, (s.line - 1) * lineH + padTop - textareaEl.clientHeight / 2);
    syncScroll();
  }

  // Esc closes the editor. Registered here (once, at script load) rather than in
  // ensure(), so it exists even before the first file has been opened.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!dlg || !dlg.classList.contains('open')) return;
    e.preventDefault();
    close();
  });

  // Close without the discard confirmation, for session switches that must not
  // block on a dialog.
  function forceClose() {
    if (!dlg) return;
    dlg.classList.remove('open');
    document.body.classList.remove('editor-open');
    s.dirty = false;
  }

  return { open, close, forceClose };
})();

const FileBrowser = (() => {
  let dlg = null;
  let listEl, pathEl, upBtn, backBtn, fwdBtn, actionsEl, moveEl, selEl, moveInfoEl;
  // Each session remembers its own last directory and its navigation history,
  // so switching away and back reopens where that session left off. The home
  // button jumps to the working directory.
  const lastDirs = {};
  const nav = {};             // sessionId -> { stack: [dir,...], i: number }
  const selected = new Set();  // full paths currently selected
  let entries = [];            // current listing
  let basePath = '';           // current directory
  let lastIndex = null;        // for shift-range selection
  let moving = null;           // Set of paths being moved, or null
  let ctxMenu = null;
  let ctxPath = null;

  function here() { return lastDirs[App.sessionId] || workingDir(); }

  function ensure() {
    if (dlg) return;
    dlg = document.createElement('dialog');
    dlg.id = 'file-browser';
    dlg.className = 'file-browser';
    dlg.innerHTML =
      '<div class="fb-head">' +
        '<button type="button" class="fe-btn" data-fb="back" title="Back">&#8592;</button>' +
        '<button type="button" class="fe-btn" data-fb="fwd" title="Forward">&#8594;</button>' +
        '<button type="button" class="fe-btn" data-fb="up" title="Up one level">&#8593;</button>' +
        '<input type="text" class="fb-path" placeholder="Path\u2026" spellcheck="false">' +
        '<button type="button" class="fe-btn" data-fb="home" title="Working directory">&#127968;</button>' +
        '<button type="button" class="fe-btn" data-fb="newfile" title="New file in this folder">New file</button>' +
        '<button type="button" class="fe-btn" data-fb="newdir" title="New folder here">New folder</button>' +
        '<button type="button" class="fe-btn" data-fb="close" title="Close">&times;</button>' +
      '</div>' +
      '<div class="fb-list"></div>' +
      '<div class="fb-actions" hidden>' +
        '<span class="fb-sel"></span>' +
        '<button type="button" class="fe-btn" data-fb="open">Open</button>' +
        '<button type="button" class="fe-btn" data-fb="rename">Rename</button>' +
        '<button type="button" class="fe-btn" data-fb="copy">Copy</button>' +
        '<button type="button" class="fe-btn" data-fb="delete">Delete</button>' +
        '<button type="button" class="fe-btn" data-fb="move">Move\u2026</button>' +
      '</div>' +
      '<div class="fb-move" hidden>' +
        '<span class="fb-move-info"></span>' +
        '<button type="button" class="fe-btn fe-save" data-fb="movehere">Move here</button>' +
        '<button type="button" class="fe-btn" data-fb="cancelmove">Cancel</button>' +
      '</div>';
    document.body.appendChild(dlg);

    pathEl = dlg.querySelector('.fb-path');
    listEl = dlg.querySelector('.fb-list');
    upBtn = dlg.querySelector('[data-fb=up]');
    backBtn = dlg.querySelector('[data-fb=back]');
    fwdBtn = dlg.querySelector('[data-fb=fwd]');
    actionsEl = dlg.querySelector('.fb-actions');
    moveEl = dlg.querySelector('.fb-move');
    selEl = dlg.querySelector('.fb-sel');
    moveInfoEl = dlg.querySelector('.fb-move-info');

    // Type a raw path and Enter to jump straight there.
    pathEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        open(pathEl.value.trim());
      }
    });

    backBtn.addEventListener('click', goBack);
    fwdBtn.addEventListener('click', goForward);
    upBtn.addEventListener('click', () => open(dirname(basePath)));
    dlg.querySelector('[data-fb=home]').addEventListener('click', () => open(workingDir()));
    dlg.querySelector('[data-fb=newfile]').addEventListener('click', newFile);
    dlg.querySelector('[data-fb=newdir]').addEventListener('click', newFolder);
    dlg.querySelector('[data-fb=close]').addEventListener('click', () => dlg.close());

    dlg.querySelector('[data-fb=open]').addEventListener('click', openSelected);
    dlg.querySelector('[data-fb=rename]').addEventListener('click', () => doRename([...selected][0]));
    dlg.querySelector('[data-fb=copy]').addEventListener('click', () => doCopy([...selected]));
    dlg.querySelector('[data-fb=delete]').addEventListener('click', () => doDelete([...selected]));
    dlg.querySelector('[data-fb=move]').addEventListener('click', () => startMove([...selected]));
    dlg.querySelector('[data-fb=movehere]').addEventListener('click', confirmMove);
    dlg.querySelector('[data-fb=cancelmove]').addEventListener('click', cancelMove);

    listEl.addEventListener('contextmenu', onCtx);
    dlg.addEventListener('close', () => { selected.clear(); moving = null; hideCtx(); });
  }

  function dirname(path) {
    const p = String(path || '');
    const i = p.lastIndexOf('/');
    return i > 0 ? p.slice(0, i) : (p.startsWith('/') ? '/' : '.');
  }
  function join(base, name) {
    return String(base).replace(/\/+$/, '') + '/' + name;
  }
  function fmtSize(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }
  function workingDir() { return App.projectDir || '/'; }
  function fullPath(name) { return join(basePath, name); }

  function navFor() {
    if (!nav[App.sessionId]) nav[App.sessionId] = { stack: [], i: -1 };
    return nav[App.sessionId];
  }
  function record(dir) {
    const n = navFor();
    if (n.stack[n.i] === dir) return;  // already at this directory
    n.stack = n.stack.slice(0, n.i + 1);
    n.stack.push(dir);
    n.i = n.stack.length - 1;
  }
  function goBack() {
    const n = navFor();
    if (n.i <= 0) return;
    n.i--;
    open(n.stack[n.i], { record: false });
  }
  function goForward() {
    const n = navFor();
    if (n.i >= n.stack.length - 1) return;
    n.i++;
    open(n.stack[n.i], { record: false });
  }
  function updateNavButtons() {
    const n = navFor();
    backBtn.disabled = n.i <= 0;
    fwdBtn.disabled = n.i >= n.stack.length - 1;
  }
  function openPath(path) {
    const entry = entries.find((e) => fullPath(e.name) === path);
    if (entry) onRowOpen(entry);
  }
  function openSelected() {
    if (selected.size !== 1) return;
    openPath([...selected][0]);
  }

  async function open(path, opts = {}) {
    if (!App.sessionId) return;
    ensure();
    const recordIt = opts.record !== false;
    if (!path) path = here();
    if (!dlg.open) dlg.showModal();
    pathEl.value = path;
    listEl.textContent = '';
    const spinner = el('div', 'fb-note', 'Loading\u2026');
    listEl.appendChild(spinner);

    let data;
    try {
      const resp = await fetch(
        `/api/files/list?session_id=${encodeURIComponent(App.sessionId)}&path=${encodeURIComponent(path)}`);
      if (!resp.ok) {
        let msg = 'Could not list';
        try { const j = await resp.json(); msg = j.detail || msg; } catch (_) {}
        spinner.textContent = msg;
        spinner.classList.add('fe-status-error');
        return;
      }
      data = await resp.json();
    } catch (err) {
      spinner.textContent = `Could not list: ${err}`;
      spinner.classList.add('fe-status-error');
      return;
    }

    basePath = data.path;
    lastDirs[App.sessionId] = data.path;
    if (recordIt) record(data.path);
    entries = data.entries;
    selected.clear();
    lastIndex = null;
    pathEl.value = data.path;
    listEl.textContent = '';
    upBtn.disabled = !data.parent || data.parent === data.path;

    const frag = document.createDocumentFragment();
    entries.forEach((entry, i) => {
      const path = fullPath(entry.name);
      const row = el('div', 'fb-row' + (entry.is_dir ? ' fb-dir' : ''));
      row.dataset.path = path;
      row.dataset.isDir = entry.is_dir ? '1' : '0';
      if (selected.has(path)) row.classList.add('selected');
      const icon = el('span', 'fb-icon', entry.is_dir ? '\u25B8' : '');
      const name = el('span', 'fb-name', entry.name);
      if (entry.size != null) row.appendChild(el('span', 'fb-size', fmtSize(entry.size)));
      row.prepend(icon, name);
      row.addEventListener('click', (e) => onRowClick(e, entry, i));
      row.addEventListener('dblclick', () => onRowOpen(entry));
      frag.appendChild(row);
    });
    listEl.appendChild(frag);
    updateUI();
    updateNavButtons();
  }

  function onRowClick(e, entry, i) {
    if (moving) {
      // Move mode: clicking navigates into folders, nothing else.
      if (entry.is_dir) open(fullPath(entry.name));
      return;
    }
    const path = fullPath(entry.name);
    if (e.shiftKey && lastIndex != null) {
      const [a, b] = [Math.min(lastIndex, i), Math.max(lastIndex, i)];
      selected.clear();
      for (let k = a; k <= b; k++) selected.add(fullPath(entries[k].name));
    } else if (e.ctrlKey || e.metaKey) {
      selected.has(path) ? selected.delete(path) : selected.add(path);
      lastIndex = i;
    } else {
      selected.clear();
      selected.add(path);
      lastIndex = i;
    }
    updateUI();
  }

  function onRowOpen(entry) {
    if (moving) return;
    const child = fullPath(entry.name);
    if (entry.is_dir) open(child);
    else { dlg.close(); FileEditor.open(child, {}); }
  }

  function updateUI() {
    listEl.querySelectorAll('.fb-row').forEach((row) => {
      row.classList.toggle('selected', selected.has(row.dataset.path));
    });
    const n = selected.size;
    actionsEl.hidden = n === 0 || moving != null;
    selEl.textContent = n === 1 ? '1 selected' : `${n} selected`;
    actionsEl.querySelector('[data-fb=open]').disabled = n !== 1;
    actionsEl.querySelector('[data-fb=rename]').disabled = n !== 1;
    moveEl.hidden = moving == null;
    if (moving != null) {
      const paths = [...moving];
      const from = paths.length === 1
        ? `'${paths[0].split('/').pop()}'`
        : `${paths.length} items`;
      moveInfoEl.textContent = `Moving ${from} \u2192 ${basePath}`;
    }
  }

  async function apiPost(url, body) {
    try {
      return await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (err) {
      await ui.alert(`Request failed: ${err}`, 'File manager');
      return null;
    }
  }

  async function checkOk(resp, fallback) {
    if (resp && resp.ok) return true;
    let msg = fallback;
    if (resp) { try { const j = await resp.json(); msg = j.detail || msg; } catch (_) {} }
    await ui.alert(msg, 'File manager');
    return false;
  }

  async function doRename(path) {
    const name = await ui.prompt('Rename to', {
      title: 'Rename', value: path.split('/').pop(),
    });
    if (!name) return;
    const resp = await apiPost('/api/files/rename',
      { session_id: App.sessionId, path, name });
    if (!(await checkOk(resp, 'Could not rename'))) return;
    open(basePath);
  }

  async function doCopy(paths) {
    for (const path of paths) {
      const resp = await apiPost('/api/files/copy', { session_id: App.sessionId, path });
      if (!(await checkOk(resp, 'Could not copy'))) return;
    }
    open(basePath);
  }

  async function doDelete(paths) {
    const n = paths.length;
    const ok = await ui.confirm(
      `Delete ${n} item${n === 1 ? '' : 's'}? This cannot be undone.`,
      { title: 'Delete', confirmLabel: 'Delete', danger: true },
    );
    if (!ok) return;
    for (const path of paths) {
      const resp = await apiPost('/api/files/delete', { session_id: App.sessionId, path });
      if (!(await checkOk(resp, 'Could not delete'))) return;
    }
    open(basePath);
  }

  function startMove(paths) {
    moving = new Set(paths);
    selected.clear();
    lastIndex = null;
    updateUI();
  }

  async function confirmMove() {
    const resp = await apiPost('/api/files/move',
      { session_id: App.sessionId, paths: [...moving], dest: basePath });
    if (!(await checkOk(resp, 'Could not move'))) return;
    moving = null;
    open(basePath);
  }

  function cancelMove() {
    moving = null;
    updateUI();
  }

  async function newFile() {
    const name = await ui.prompt('Create a new file in this folder', {
      title: 'New file', placeholder: 'name.ext',
    });
    if (!name) return;
    const path = join(here(), name);
    const resp = await apiPost('/api/files/save',
      { session_id: App.sessionId, path, content: '' });
    if (!(await checkOk(resp, 'Could not create the file'))) return;
    dlg.close();
    FileEditor.open(path);
  }

  async function newFolder() {
    const name = await ui.prompt('Create a new folder here', {
      title: 'New folder', placeholder: 'name',
    });
    if (!name) return;
    const path = join(here(), name);
    const resp = await apiPost('/api/files/mkdir', { session_id: App.sessionId, path });
    if (!(await checkOk(resp, 'Could not create the folder'))) return;
    open(here());
  }

  /* Right-click context menu for a single item. */
  function ensureCtx() {
    if (ctxMenu) return;
    ctxMenu = document.createElement('div');
    ctxMenu.className = 'ctx-menu';
    ctxMenu.hidden = true;
    const items = [
      ['Open', () => { hideCtx(); openPath(ctxPath); }],
      ['Rename', () => { hideCtx(); doRename(ctxPath); }],
      ['Copy', () => { hideCtx(); doCopy([ctxPath]); }],
      ['Move here', () => { hideCtx(); startMove([ctxPath]); }],
      ['Delete', () => { hideCtx(); doDelete([ctxPath]); }],
    ];
    for (const [label, fn] of items) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ctx-item';
      b.textContent = label;
      b.addEventListener('click', fn);
      ctxMenu.appendChild(b);
    }
    document.body.appendChild(ctxMenu);
  }

  function showCtx(x, y, path) {
    ensureCtx();
    ctxPath = path;
    ctxMenu.style.left = x + 'px';
    ctxMenu.style.top = y + 'px';
    ctxMenu.hidden = false;
  }
  function hideCtx() { if (ctxMenu) ctxMenu.hidden = true; }

  function onCtx(e) {
    const row = e.target.closest('.fb-row');
    if (!row) return;
    e.preventDefault();
    showCtx(e.clientX, e.clientY, row.dataset.path);
  }

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.ctx-menu')) hideCtx();
  });

  return { open };
})();

/* Session bar button: open the shared file manager at its last directory. */
function openFileManager() {
  FileBrowser.open();
}
