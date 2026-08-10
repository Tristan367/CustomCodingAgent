/* CodeAgent front-end: SSE streaming, tool approval, dictation, tabs. */
'use strict';

const App = {
  sessionId: null,
  streaming: false,
  ttsAvailable: false,
  timers: new Set(),
  abortController: null,
  els: {},
};

/* ── Boot ────────────────────────────────────────────────────────────────── */

function initSession() {
  const view = document.getElementById('session-view');
  const previous = App.sessionId;
  App.sessionId = view ? view.dataset.sessionId : null;
  if (previous && previous !== App.sessionId) {
    // Switching tabs must not leave the old session's reader running: it would
    // keep writing into whichever transcript is on screen now, and it holds
    // App.streaming true so the new tab refuses to attach to its own run.
    // The server run is untouched -- only this page stops listening.
    detachStream();
    pendingImages = [];
    renderAttachments();
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
  restorePending();
  attachIfRunning();
  Dictation.init();
  Speech.settings().then((ok) => { App.ttsAvailable = ok; attachPlayButtons(); });
  markSessionSeen();
  updateComposerButtons();
  if (!Persist.restore()) scrollToBottom(true);
}

document.addEventListener('DOMContentLoaded', () => {
  Notifier.init();
  initSession();
  refreshTabBar();
  Notifier.poll();
  setInterval(() => Notifier.tick(), 1000);
});

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
  attachPlayButtons();
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
  attachPlayButtons();
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
          // Reuse the bubble holding the thinking block. The server stores
          // reasoning and content on one row, so keeping them in one bubble
          // means the page does not rearrange itself when reloaded.
          stream.assistantEl = stream.reasoningEl.closest('.message');
          collapseReasoning(stream.reasoningEl);
          stream.reasoningEl = null;
          stream.contentEl = el('div', 'content-text');
          stream.assistantEl.querySelector('.msg-content').appendChild(stream.contentEl);
        } else {
          stream.assistantEl = appendMessage('assistant', '');
          stream.contentEl = stream.assistantEl.querySelector('.content-text');
        }
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
      const node = appendUserMessage(event.content, []);
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
      renderChangeSummary(event.changes);
      attachPlayButtons();
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

/* ── Message rendering ───────────────────────────────────────────────────── */

function el(tag, className, html) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (html !== undefined) node.innerHTML = html;
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

/* Same markup the server renders in chat_messages.html, so refreshing the page
   does not change how a thinking block looks. */
function appendReasoning() {
  const node = el('div', 'message assistant');
  node.appendChild(roleEl('thinking'));
  const body = el('div', 'msg-content');
  const details = el('details', 'tool-details reasoning-details');
  details.open = true;
  const text = el('pre', 'reasoning-text');
  details.append(el('summary', 'tool-summary', 'Thinking'), text);
  body.appendChild(details);
  node.appendChild(body);
  node.appendChild(el('span', 'msg-time', clockTime()));
  App.els.messages.appendChild(node);
  return text;
}

function collapseReasoning(textEl) {
  const details = textEl.closest('details');
  if (details) details.open = false;
}

function appendToolCall(event) {
  const existing = App.els.messages.querySelector(
    `.message.tool[data-tool-call-id="${cssEscape(event.tool_call_id)}"]`);
  if (existing) return existing;
  const node = el('div', 'message tool pending');
  node.dataset.toolCallId = event.tool_call_id;
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
  if (label) label.textContent = event.title || event.name;
  const dot = node.querySelector('.spinner-dot');
  if (dot) dot.remove();

  const details = node.querySelector('.tool-details');
  if (event.diff) {
    // The diff box carries its own title, so keeping the plain details too
    // would show the same line twice.
    details.replaceWith(renderDiff(event.diff, event.title));
    autoscroll();
    return;
  }
  const result = el('div', 'tool-result');
  result.appendChild(el('div', 'tool-result-label', event.is_error ? 'error' : 'output'));
  result.appendChild(el('pre', 'tool-raw', md.escapeHtml(event.output || '(no output)')));
  details.appendChild(result);
  autoscroll();
}

/* Everything the turn touched, in one place, so the user does not have to
   scroll back through the transcript to see what changed. */
function renderChangeSummary(changes) {
  if (!changes || !changes.files || !changes.files.length) return;
  document.querySelectorAll('.change-summary').forEach((n) => n.remove());
  const node = el('div', 'message change-summary');
  node.appendChild(roleEl('changes'));
  const body = el('div', 'msg-content');
  const outer = el('details', 'tool-details');
  outer.open = true;
  const summary = el('summary', 'tool-summary');
  const count = `${changes.files.length} file${changes.files.length === 1 ? '' : 's'} changed`;
  const stat = el('span', 'diff-stat');
  stat.append(el('span', 'diff-stat-add', `+${changes.added}`),
              el('span', 'diff-stat-del', `\u2212${changes.removed}`));
  summary.append(el('span', 'tool-label', count), stat);
  outer.appendChild(summary);
  for (const file of changes.files) {
    const combined = file.diffs.join('\n');
    outer.appendChild(renderDiff(combined, shortPath(file.path), false));
  }
  body.appendChild(outer);
  node.appendChild(body);
  node.appendChild(el('span', 'msg-time', clockTime()));
  App.els.messages.appendChild(node);
  autoscroll();
}

/* Unified diff with per-line colouring, in a collapsible box that starts open.
   Mirrors the server-side render in chat_messages.html so a reloaded page looks
   the same as the streamed one. */
function renderDiff(diff, title, open = true) {
  const box = el('pre', 'diff-block');
  let added = 0;
  let removed = 0;
  for (const line of diff.replace(/\n+$/, '').split('\n')) {
    let cls = 'diff-ctx';
    if (line.startsWith('@@')) cls = 'diff-hunk';
    else if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-meta';
    else if (line.startsWith('+')) { cls = 'diff-add'; added++; }
    else if (line.startsWith('-')) { cls = 'diff-del'; removed++; }
    const row = el('span', cls);
    // The spans are display:block; appending \n as well would double the
    // line height. A blank line still needs a space to keep its box.
    row.textContent = line || ' ';
    box.appendChild(row);
  }
  const details = el('details', 'tool-details diff-details');
  details.open = open;
  const summary = el('summary', 'tool-summary');
  const stat = el('span', 'diff-stat');
  stat.append(el('span', 'diff-stat-add', `+${added}`),
              el('span', 'diff-stat-del', `\u2212${removed}`));
  summary.append(el('span', 'tool-label', title || 'diff'), stat);
  details.append(summary, box);
  return details;
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
    default: return name;
  }
}

function appendNotice(kind, text) {
  const node = el('div', `message notice notice-${kind}`);
  node.appendChild(roleEl(kind));
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
  loadCompactPrompt(pause && pause.instructions);
}

async function confirmCompaction() {
  const extra = document.getElementById('compact-extra').value;
  const promptBox = document.getElementById('compact-prompt');
  const override = promptBox && promptBox.value !== promptBox.dataset.saved
    ? promptBox.value
    : '';
  closeModal('compact-modal');
  const resume = !!compactionPause;
  compactionPause = null;

  const form = new FormData();
  form.append('extra_instructions', extra);
  form.append('prompt_override', override);
  form.append('resume', resume ? 'true' : 'false');

  // Summarising a long transcript is slow, so show the summary as it is
  // written. Previously this was a static notice that vanished on failure.
  await streamRequest(`/api/sessions/${App.sessionId}/compact`, { method: 'POST', body: form });
  if (!resume) refreshMeta();
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

async function saveAutoCompact(enabled) {
  const form = new FormData();
  form.append('enabled', enabled ? 'true' : 'false');
  await fetch(`/api/sessions/${App.sessionId}/auto-compact`, { method: 'POST', body: form })
    .catch(() => appendNotice('error', 'Could not save that setting.'));
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
  const auto = document.getElementById('compact-auto');
  const meta = document.getElementById('session-meta');
  if (auto && meta) auto.checked = meta.dataset.autoCompact === '1';
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

function saveBashRules() {
  const text = document.getElementById('bash-rules').value;
  const rules = text.split('\n').filter(l => l.trim()).map(line => {
    const m = line.match(/^(.+?)\s*[→>]\s*(allow|deny|ask)$/);
    if (m) return { pattern: m[1].trim(), action: m[2] };
    return null;
  }).filter(Boolean);
  fetch('/_settings/bash_rules', { method: 'POST', body: JSON.stringify(rules), headers: {'Content-Type': 'application/json'} });
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
      alert(data.error || 'Upload failed');
    }
  } catch (e) {
    alert('Upload failed: ' + e);
  }
  input.value = '';
}

async function initProject() {
  const dir = prompt('Project directory (leave blank for home):', '~/' + (App.sessionId ? '' : ''));
  if (!dir && dir !== '') return;
  const form = new FormData();
  form.append('dir', dir || '~/');
  try {
    const r = await fetch('/_init', { method: 'POST', body: form });
    const data = await r.json();
    if (data.ok) {
      alert('Wrote ' + data.path + '\n\n' + data.preview);
    } else {
      alert(data.error || 'Failed');
    }
  } catch (e) {
    alert('Failed: ' + e);
  }
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

const MIC_TITLE = 'Dictate \u2014 click to toggle, or hold Right Ctrl to talk';

const Dictation = {
  recording: false,
  starting: false,      // set synchronously, before any await
  pushToTalk: false,
  recorder: null,
  chunks: [],
  streamRef: null,
  audioCtx: null,
  analyser: null,
  rafId: null,
  meterGeneration: 0,   // stale animation loops check this and exit
  transcribeTimer: null,
  els: {},

  init() {
    this.els.button = document.getElementById('mic-btn');
    this.els.meter = document.getElementById('mic-meter');
    this.els.status = document.getElementById('stt-status');
    this.els.elapsed = document.getElementById('stt-elapsed');
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

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
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
      const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
        .find((t) => MediaRecorder.isTypeSupported(t)) || '';
      this.recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
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
    if (!this.recording || !this.recorder) {
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

  releaseStream() {
    if (this.streamRef) {
      this.streamRef.getTracks().forEach((t) => t.stop());
      this.streamRef = null;
    }
  },

  startMeter() {
    if (!this.els.meter || !this.streamRef) return;
    this.stopMeter();
    const generation = ++this.meterGeneration;

    try {
      // One context, reused. Chrome caps concurrent AudioContexts at a handful
      // and a leaked one is never reclaimed.
      if (!this.audioCtx || this.audioCtx.state === 'closed') {
        this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (this.audioCtx.state === 'suspended') this.audioCtx.resume();
      const source = this.audioCtx.createMediaStreamSource(this.streamRef);
      this.analyser = this.audioCtx.createAnalyser();
      this.analyser.fftSize = 512;
      source.connect(this.analyser);
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
      const level = Math.min(1, Math.sqrt(sum / data.length) / 34);
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
    App.els.send.disabled = !canSend;
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
  if (e.target.id === 'chat-textarea' && e.key === 'Enter' && !e.shiftKey) {
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
  if (!resp.ok) { alert(data.detail || 'Could not allow that path'); return; }
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

/* Give every assistant reply a play control. Done by scanning rather than at
 * each creation site, because assistant bubbles are built in three different
 * places and a reloaded transcript is built by Jinja in a fourth. */
function attachPlayButtons() {
  if (!App.ttsAvailable) return;
  document.querySelectorAll('.message.assistant:not([data-play])').forEach((node) => {
    const text = node.querySelector('.content-text');
    if (!text || !text.dataset.raw?.trim()) return;   // reasoning-only bubble
    node.dataset.play = '1';
    const actions = el('span', 'msg-actions');
    actions.appendChild(button('play', 'play-btn', () => Speech.toggle(node)));
    const vol = el('span', 'vol-pop');
    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = '0'; slider.max = '1'; slider.step = '0.01';
    slider.value = String(Speech.volume);
    slider.setAttribute('orient', 'vertical');       // Firefox
    slider.title = 'Volume';
    slider.addEventListener('input', () => Speech.setVolume(parseFloat(slider.value)));
    vol.appendChild(slider);
    actions.appendChild(vol);
    // Time above, control below. The gutter is only as wide as a timestamp
    // and has to stay that way, because it is mirrored on the left.
    const side = el('span', 'msg-side stacked');
    const time = node.querySelector(':scope > .msg-time');
    side.append(time || el('span', 'msg-time', clockTime()), actions);
    node.appendChild(side);
  });
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
