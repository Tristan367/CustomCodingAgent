// app.js — SSE streaming, image upload, tab scroll

document.addEventListener('DOMContentLoaded', () => {
    setupChatStreaming();
    setupTabActive();
});

// Re-initialize after HTMX swaps (tab navigation)
document.addEventListener('htmx:afterSwap', (e) => {
    if (e.detail.target.id === 'main-content') {
        setupChatStreaming();
        setupTabActive();
        // Update active tab
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        const path = window.location.pathname;
        document.querySelectorAll('.tab').forEach(t => {
            if (t.getAttribute('href') === path || t.getAttribute('hx-get') === '/_session' + path.slice('/sessions'.length)) {
                t.classList.add('active');
            }
        });
    }
});

function setupChatStreaming() {
    const chatForm = document.getElementById('chat-form');
    if (!chatForm) return;

    let currentAbortController = null;

    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const sessionId = document.getElementById('session-view')?.dataset.sessionId;
        if (!sessionId) return;

        const textarea = document.getElementById('chat-textarea');
        const message = textarea.value.trim();
        const imageInput = document.getElementById('image-input');
        const visionPrompt = document.getElementById('vision-prompt-input')?.value || '';
        const hasImage = imageInput && imageInput.files.length > 0;

        if (!message && !hasImage) return;

        // Toggle to Stop button
        const sendBtn = document.getElementById('send-btn');
        const stopBtn = document.getElementById('stop-btn');
        sendBtn.style.display = 'none';
        stopBtn.style.display = '';

        // Show user message
        let userDisplay = message || '';
        if (hasImage) userDisplay = '[Image attached] ' + userDisplay;
        appendUserMessage(userDisplay);
        textarea.value = '';
        textarea.style.height = 'auto';
        if (imageInput) imageInput.value = '';
        const vp = document.getElementById('vision-prompt-input');
        if (vp) vp.value = '';

        // Build request
        let endpoint, body, headers = {};
        if (hasImage) {
            endpoint = `/api/sessions/${sessionId}/chat-with-image`;
            body = new FormData();
            body.append('message', message);
            body.append('image', imageInput.files[0]);
            if (visionPrompt) body.append('vision_prompt', visionPrompt);
        } else {
            endpoint = `/api/sessions/${sessionId}/chat`;
            headers['Content-Type'] = 'application/json';
            body = JSON.stringify({ message });
        }

        currentAbortController = new AbortController();
        const assistantEl = appendAssistantPlaceholder();

        try {
            const resp = await fetch(endpoint, { method: 'POST', headers, body, signal: currentAbortController.signal });
            if (!resp.ok) {
                let errText = '';
                try { errText = await resp.text(); } catch(e) {}
                assistantEl.querySelector('.content-text').textContent = 'Error: ' + (errText || `HTTP ${resp.status}`);
                assistantEl.classList.remove('streaming-cursor');
                return;
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '', assistantContent = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const data = line.slice(6);
                    if (!data.trim()) continue;
                    try {
                        const parsed = JSON.parse(data);
                        switch (parsed.type) {
                            case 'reasoning': appendOrUpdateReasoning(parsed.text); break;
                            case 'confirm_bash': handleBashConfirm(sessionId, parsed.tool_call_id, parsed.command); return;
                            case 'content': finalizeReasoning(); assistantContent += parsed.text; assistantEl.querySelector('.content-text').textContent = assistantContent; break;
                            case 'tool_call': appendToolMessage(parsed.name, parsed.args); break;
                            case 'tool_result': appendToolResult(parsed.content); break;
                            case 'done': assistantEl.classList.remove('streaming-cursor'); refreshMessages(sessionId); break;
                            case 'error': assistantEl.querySelector('.content-text').textContent = 'Error: ' + parsed.text; assistantEl.classList.remove('streaming-cursor'); break;
                        }
                    } catch(e) {}
                }
            }
            if (assistantContent) { assistantEl.classList.remove('streaming-cursor'); refreshMessages(sessionId); }
            scrollToBottom();
        } catch (err) {
            if (err.name !== 'AbortError') {
                assistantEl.querySelector('.content-text').textContent = 'Error: ' + err.message;
                assistantEl.classList.remove('streaming-cursor');
            }
        } finally {
            sendBtn.style.display = '';
            stopBtn.style.display = 'none';
            currentAbortController = null;
        }
    });
}

// Stop streaming
function stopStreaming() {
    // Reload the page to abort the SSE stream
    location.reload();
}

function appendUserMessage(text) {
    const container = document.getElementById('messages');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'message user';
    div.innerHTML = `<div class="msg-role">user</div><div class="msg-content"><div class="content-text">${escapeHtml(text)}</div></div>`;
    container.appendChild(div);
}

function appendAssistantPlaceholder() {
    const container = document.getElementById('messages');
    if (!container) return document.createElement('div');
    const div = document.createElement('div');
    div.className = 'message assistant streaming-cursor';
    div.innerHTML = `<div class="msg-role">assistant</div><div class="msg-content"><div class="content-text"></div></div>`;
    container.appendChild(div);
    return div;
}

function appendToolMessage(label, args) {
    const container = document.getElementById('messages');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'message tool';
    const summary = getToolSummary(label, args);
    div.innerHTML = `<div class="msg-role">tool</div>
        <div class="msg-content">
            <details class="tool-details" open>
                <summary class="tool-summary">${escapeHtml(summary)}</summary>
                <pre class="tool-raw">${escapeHtml(JSON.stringify(args, null, 2))}</pre>
            </details>
        </div>
        <span class="msg-time">${new Date().toISOString().slice(0,19)}</span>`;
    container.appendChild(div);
}

function getToolSummary(name, args) {
    switch (name) {
        case 'read': return `🔧 Reading ${args.filePath || 'file'}...`;
        case 'edit': return `🔧 Editing ${args.filePath || 'file'}...`;
        case 'write': return `🔧 Writing ${args.filePath || 'file'}...`;
        case 'bash': return `🔧 Running: ${(args.command || '').slice(0, 80)}`;
        case 'grep': return `🔧 Searching: ${args.pattern || ''}`;
        case 'glob': return `🔧 Finding: ${args.pattern || ''}`;
        case 'webfetch': return `🔧 Fetching ${args.url || 'URL'}...`;
        case 'vision': return `🔧 Vision: ${args.url || 'screenshot'} — ${(args.prompt || '').slice(0, 60)}`;
        case 'question': return `🔧 Asking: ${args.question || ''}`;
        default: return `🔧 Calling ${name}...`;
    }
}

function appendToolResult(content) {
    const container = document.getElementById('messages');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'message tool';
    const truncated = content.length > 5000 ? content.slice(0, 5000) + '\n... [truncated]' : content;
    div.innerHTML = `<div class="msg-role">result</div>
        <div class="msg-content">
            <details class="tool-details">
                <summary class="tool-summary">TOOL RESULT</summary>
                <pre class="tool-raw">${escapeHtml(truncated)}</pre>
            </details>
        </div>
        <span class="msg-time">${new Date().toISOString().slice(0,19)}</span>`;
    container.appendChild(div);
}

function refreshMessages(sessionId) {
    const container = document.getElementById('chat-container');
    if (!container) return;
    htmx.ajax('GET', `/_messages/${sessionId}`, { target: '#chat-container', swap: 'innerHTML' });
}

function scrollToBottom() {
    const container = document.getElementById('chat-container');
    if (container) {
        setTimeout(() => { container.scrollTop = container.scrollHeight; }, 50);
    }
}

function setupTabActive() {
    const activeTab = document.querySelector('.tab.active');
    if (activeTab) {
        activeTab.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Close tab — persists to DB, removes from DOM
function closeTab(event, sessionId) {
    event.preventDefault();
    event.stopPropagation();
    fetch('/_tab_close/' + sessionId, {method:'POST'});
    event.target.closest('.tab-wrap')?.remove();
}

// When a session page loads, register it as an open tab
document.addEventListener('DOMContentLoaded', () => {
    refreshTabBar();
    const sid = document.getElementById('session-view')?.dataset.sessionId;
    if (sid) fetch('/_tab_open/' + sid, {method:'POST'}).then(() => refreshTabBar());
});

// Also register on HTMX session swaps
document.addEventListener('htmx:afterSwap', (e) => {
    if (e.detail.target.id === 'main-content') {
        const sid = document.getElementById('session-view')?.dataset.sessionId;
        if (sid) fetch('/_tab_open/' + sid, {method:'POST'}).then(() => refreshTabBar());
    }
});

function refreshTabBar() {
    const sid = document.getElementById('session-view')?.dataset.sessionId || '';
    htmx.ajax('GET', '/_tab_bar?current=' + sid, {target:'#tab-bar', swap:'outerHTML'});
}

// Session card click — registers tab then navigates
function openSession(sid) {
    fetch('/_tab_open/' + sid, {method:'POST'}).then(() => {
        location.href = '/sessions/' + sid;
    });
}

// Auto-resize textarea + Enter to send
document.addEventListener('keydown', (e) => {
    if (e.target.id === 'chat-textarea' && e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        const form = document.getElementById('chat-form');
        if (form) form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
    }
});

document.addEventListener('input', (e) => {
    if (e.target.id === 'chat-textarea') {
        e.target.style.height = 'auto';
        e.target.style.height = Math.min(e.target.scrollHeight, 200) + 'px';
    }
});

// Show/hide vision prompt input when image is selected
const imageInput = document.getElementById('image-input');
const vpInput = document.getElementById('vision-prompt-input');
if (imageInput && vpInput) {
    vpInput.style.display = 'none';
    imageInput.addEventListener('change', () => {
        vpInput.style.display = imageInput.files.length > 0 ? '' : 'none';
    });
}

// Re-init after HTMX swaps
document.addEventListener('htmx:afterSwap', () => {
    const img = document.getElementById('image-input');
    const vp = document.getElementById('vision-prompt-input');
    if (img && vp) {
        vp.style.display = 'none';
        img.addEventListener('change', () => { vp.style.display = img.files.length > 0 ? '' : 'none'; });
    }
});

// API key masking — show first 6 + last 4, hide middle
function maskApiKey() {
    const input = document.getElementById('api-key-input');
    if (!input) return;
    const val = input.value;
    if (val.length > 16) {
        input.type = 'text';
        input.dataset.fullKey = val;
        input.value = val.slice(0, 6) + '••••••••' + val.slice(-4);
        input.addEventListener('focus', () => {
            input.value = input.dataset.fullKey || '';
        }, {once: true});
    }
}
document.addEventListener('DOMContentLoaded', maskApiKey);
document.addEventListener('htmx:afterSwap', maskApiKey);

// Image attach flow
let _pendingImageFile = null;

function handleImageAttach(input) {
    if (!input.files.length) return;
    _pendingImageFile = input.files[0];
    const reader = new FileReader();
    reader.onload = (e) => {
        document.getElementById('image-preview').src = e.target.result;
        document.getElementById('image-modal').style.display = 'flex';
        document.getElementById('vision-modal-prompt').focus();
    };
    reader.readAsDataURL(_pendingImageFile);
    input.value = '';
}

function cancelImageAttach() {
    _pendingImageFile = null;
    document.getElementById('image-modal').style.display = 'none';
}

async function submitImageAttach() {
    if (!_pendingImageFile) return;
    const prompt = document.getElementById('vision-modal-prompt').value || 'Describe this image in detail.';
    document.getElementById('image-modal').style.display = 'none';

    const formData = new FormData();
    formData.append('image', _pendingImageFile);
    formData.append('prompt', prompt);
    const sid = document.getElementById('session-view')?.dataset.sessionId;

    try {
        const resp = await fetch(`/api/sessions/${sid}/analyze-image`, { method: 'POST', body: formData });
        const data = await resp.json();
        if (data.description) {
            const ta = document.getElementById('chat-textarea');
            const text = `[Image description]: ${data.description}`;
            const start = ta.selectionStart;
            ta.value = ta.value.slice(0, start) + text + ta.value.slice(ta.selectionEnd);
            ta.focus();
        }
    } catch(e) {
        alert('Vision analysis failed: ' + e.message);
    }
    _pendingImageFile = null;
}

// Reasoning (CoT) display
let _reasoningEl = null;

function appendOrUpdateReasoning(text) {
    if (!_reasoningEl) {
        const container = document.getElementById('messages');
        const div = document.createElement('div');
        div.className = 'message reasoning';
        div.innerHTML = `
            <div class="msg-role">thinking</div>
            <div class="msg-content">
                <div class="reasoning-toggle" onclick="toggleReasoning(this)">Show thinking ▸</div>
                <div class="reasoning-text" style="display:none;"></div>
            </div>`;
        container.appendChild(div);
        _reasoningEl = div.querySelector('.reasoning-text');
    }
    _reasoningEl.textContent += text;
    _reasoningEl.style.display = 'block';
    scrollToBottom();
}

function finalizeReasoning() {
    if (_reasoningEl) {
        const toggle = _reasoningEl.parentElement.querySelector('.reasoning-toggle');
        if (toggle) toggle.textContent = 'Thinking ▸';
        _reasoningEl.style.display = 'none';
        _reasoningEl = null;
    }
}

function toggleReasoning(el) {
    const text = el.nextElementSibling;
    const isVisible = text.style.display !== 'none';
    text.style.display = isVisible ? 'none' : 'block';
    el.textContent = isVisible ? 'Show thinking ▸' : 'Hide thinking ▾';
}

// Bash approval handler
async function handleBashConfirm(sessionId, toolCallId, command) {
    const approved = confirm(`Approve bash command?\n\n${command}`);
    if (!approved) {
        // TODO: reject
        return;
    }
    const formData = new FormData();
    formData.append('tool_call_id', toolCallId);
    formData.append('command', command);

    const resp = await fetch(`/api/sessions/${sessionId}/approve-bash`, { method: 'POST', body: formData });
    if (!resp.ok) return;
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const assistantEl = appendAssistantPlaceholder();
    let assistantContent = '';

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const data = line.slice(6);
            if (!data.trim()) continue;
            try {
                const parsed = JSON.parse(data);
                switch (parsed.type) {
                    case 'reasoning': appendOrUpdateReasoning(parsed.text); break;
                    case 'content': finalizeReasoning(); assistantContent += parsed.text; assistantEl.querySelector('.content-text').textContent = assistantContent; break;
                    case 'tool_call': appendToolMessage(parsed.name, parsed.args); break;
                    case 'tool_result': appendToolResult(parsed.content); break;
                    case 'done': assistantEl.classList.remove('streaming-cursor'); refreshMessages(sessionId); break;
                    case 'error': assistantEl.querySelector('.content-text').textContent = 'Error: ' + parsed.text; assistantEl.classList.remove('streaming-cursor'); break;
                    case 'confirm_bash': handleBashConfirm(sessionId, parsed.tool_call_id, parsed.command); return; break;
                }
            } catch(e) {}
        }
    }
    if (assistantContent) { assistantEl.classList.remove('streaming-cursor'); refreshMessages(sessionId); }
    scrollToBottom();
}

// Compact button handler (override the HTMX post with a confirm)
document.addEventListener('click', (e) => {
    if (e.target.matches('button[title="Compact conversation"]')) {
        e.preventDefault();
        e.stopPropagation();
        const sessionId = document.getElementById('session-view')?.dataset.sessionId;
        if (!sessionId) return;
        const summary = prompt('Edit compaction summary (optional):', 'Summarize the conversation so far, preserving all important facts, decisions, and code changes.');
        if (summary === null) return; // cancelled
        const formData = new FormData();
        formData.append('summary', summary || '');
        fetch(`/api/sessions/${sessionId}/compact`, { method: 'POST', body: formData })
            .then(() => refreshMessages(sessionId));
    }
});

// Tab drag-and-drop reorder
function setupTabDrag() {
    document.querySelectorAll('.tab-wrap[draggable]').forEach(el => {
        el.addEventListener('dragstart', e => {
            e.dataTransfer.setData('text/plain', el.dataset.sid);
            el.style.opacity = '0.4';
        });
        el.addEventListener('dragend', () => { el.style.opacity = '1'; });
        el.addEventListener('dragover', e => e.preventDefault());
        el.addEventListener('drop', e => {
            e.preventDefault();
            const fromId = e.dataTransfer.getData('text/plain');
            const from = document.querySelector(`.tab-wrap[data-sid="${fromId}"]`);
            const to = el;
            if (from && to && from !== to) {
                const parent = from.parentNode;
                const siblings = [...parent.querySelectorAll('.tab-wrap')];
                const fromIdx = siblings.indexOf(from);
                const toIdx = siblings.indexOf(to);
                parent.insertBefore(from, fromIdx < toIdx ? to.nextSibling : to);
            }
        });
    });
}
document.addEventListener('DOMContentLoaded', setupTabDrag);
document.addEventListener('htmx:afterSwap', () => setupTabDrag());
