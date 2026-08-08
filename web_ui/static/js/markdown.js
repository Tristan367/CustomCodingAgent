/* Minimal markdown renderer with syntax highlighting.
 *
 * Self-contained on purpose: no CDN, no build step, works offline. Everything
 * is HTML-escaped before any markup is generated, so model output can never
 * inject nodes into the page.
 */
(function (global) {
  'use strict';

  const KEYWORDS = {
    common: 'return|if|else|for|while|break|continue|new|try|catch|finally|throw|switch|case|default|do|in|of|null|true|false',
    js: 'const|let|var|function|class|extends|import|export|from|as|async|await|yield|typeof|instanceof|delete|void|this|super|static|get|set|undefined|NaN',
    ts: 'interface|type|enum|implements|public|private|protected|readonly|namespace|declare|abstract|keyof|infer|satisfies',
    py: 'def|class|import|from|as|lambda|pass|raise|with|global|nonlocal|assert|del|yield|async|await|elif|not|and|or|is|None|True|False|self|cls|match|case',
    go: 'func|package|import|var|const|type|struct|interface|go|defer|chan|select|range|map|nil|make',
    rust: 'fn|let|mut|pub|use|mod|impl|trait|struct|enum|match|ref|move|unsafe|crate|self|Some|None|Ok|Err|where|dyn',
    sh: 'echo|cd|export|source|alias|sudo|then|fi|done|esac|elif|local|readonly|unset|exit',
    css: 'important|media|keyframes|import|supports|font-face|root',
  };

  const LANG_ALIASES = {
    js: 'js', javascript: 'js', jsx: 'js', mjs: 'js', cjs: 'js', node: 'js',
    ts: 'ts', typescript: 'ts', tsx: 'ts',
    py: 'py', python: 'py', py3: 'py',
    sh: 'sh', bash: 'sh', shell: 'sh', zsh: 'sh', console: 'sh', terminal: 'sh',
    go: 'go', golang: 'go',
    rs: 'rust', rust: 'rust',
    json: 'json', jsonc: 'json',
    html: 'html', xml: 'html', svg: 'html', vue: 'html',
    css: 'css', scss: 'css', sass: 'css', less: 'css',
    sql: 'sql', yaml: 'yaml', yml: 'yaml', toml: 'yaml', ini: 'yaml',
    c: 'js', cpp: 'js', java: 'js', kt: 'js', cs: 'js', php: 'js', rb: 'py',
  };

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* Highlight by tokenizing in one pass so a keyword inside a string is not
   * re-highlighted. Input must already be escaped. */
  // Above this, tokenising costs more than the colour is worth.
  const MAX_HIGHLIGHT_CHARS = 40000;

  function highlight(escaped, lang) {
    const key = LANG_ALIASES[(lang || '').toLowerCase()];
    if (!key || escaped.length > MAX_HIGHLIGHT_CHARS) return escaped;

    const keywords = key === 'json' || key === 'yaml'
      ? 'true|false|null'
      : [KEYWORDS.common, KEYWORDS[key] || ''].filter(Boolean).join('|');

    const patterns = [
      // Comments
      { re: /(&#39;&#39;&#39;[\s\S]*?&#39;&#39;&#39;|&quot;&quot;&quot;[\s\S]*?&quot;&quot;&quot;)/g, cls: 'hl-str' },
      { re: /(\/\*[\s\S]*?\*\/)/g, cls: 'hl-com' },
      { re: /(^|[^:\\])(\/\/[^\n]*)/g, cls: 'hl-com', group: 2 },
      { re: /(#[^\n]*)/g, cls: 'hl-com', only: ['py', 'sh', 'yaml'] },
      // Strings (escaped quotes are &quot; / &#39;)
      { re: /(&quot;(?:[^&\\\n]|\\.|&(?!quot;))*&quot;)/g, cls: 'hl-str' },
      { re: /(&#39;(?:[^&\\\n]|\\.|&(?!#39;))*&#39;)/g, cls: 'hl-str' },
      { re: /(`(?:[^`\\]|\\.)*`)/g, cls: 'hl-str' },
      // Numbers
      { re: /\b(0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?)\b/g, cls: 'hl-num' },
      // Keywords / functions
      { re: new RegExp('\\b(' + keywords + ')\\b', 'g'), cls: 'hl-kw' },
      { re: /\b([A-Za-z_$][\w$]*)(?=\s*\()/g, cls: 'hl-fn' },
    ].filter((p) => !p.only || p.only.includes(key));

    // Collect non-overlapping matches, earliest and longest first.
    const marks = [];
    for (const p of patterns) {
      p.re.lastIndex = 0;
      let m;
      while ((m = p.re.exec(escaped)) !== null) {
        if (m[0] === '') { p.re.lastIndex++; continue; }
        const g = p.group || (p.re.source.startsWith('\\b(') || p.group === undefined ? 1 : 0);
        const text = m[g] !== undefined ? m[g] : m[0];
        if (!text) continue;
        const start = m.index + m[0].indexOf(text);
        marks.push({ start, end: start + text.length, cls: p.cls });
      }
    }
    marks.sort((a, b) => a.start - b.start || b.end - a.end);

    let out = '';
    let cursor = 0;
    for (const mark of marks) {
      if (mark.start < cursor) continue;
      out += escaped.slice(cursor, mark.start);
      out += '<span class="' + mark.cls + '">' + escaped.slice(mark.start, mark.end) + '</span>';
      cursor = mark.end;
    }
    return out + escaped.slice(cursor);
  }

  function inline(text) {
    let out = escapeHtml(text);
    // Inline code first so its contents are not treated as markup.
    const codes = [];
    out = out.replace(/`([^`\n]+)`/g, (_, code) => {
      codes.push(code);
      return '\u0000CODE' + (codes.length - 1) + '\u0000';
    });

    out = out
      .replace(/!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g,
        (_, alt, src) => `<img src="${src}" alt="${alt}" loading="lazy">`)
      .replace(/\[([^\]]+)\]\(([^)\s]+)[^)]*\)/g,
        (_, label, href) => /^(https?:|\/|#)/.test(href)
          ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
          : label);

    // Bare URLs become links. The lookbehind skips anything already inside a
    // tag we just generated: after escapeHtml, a literal " or > can only have
    // come from our own markup. Stashed so the emphasis pass below cannot eat
    // underscores inside a URL.
    const links = [];
    out = out.replace(/(?<!["=>])\bhttps?:\/\/[^\s<>"'`]+/g, (url) => {
      // Sentence punctuation and trailing emphasis markers are almost never
      // part of the URL. Only a trailing run is stripped, so underscores and
      // asterisks *inside* a path survive.
      const tail = (url.match(/[.,;:!?)\]}*_~]+$/) || [''])[0];
      const href = tail ? url.slice(0, -tail.length) : url;
      if (!/^https?:\/\/[^/]/.test(href)) return url;
      links.push(`<a href="${href}" target="_blank" rel="noopener noreferrer">${href}</a>`);
      return '\u0000LINK' + (links.length - 1) + '\u0000' + tail;
    });

    out = out
      .replace(/(\*\*|__)(?=\S)([\s\S]*?\S)\1/g, '<strong>$2</strong>')
      .replace(/(^|[\s(])(\*|_)(?=\S)([^*_]*?\S)\2/g, '$1<em>$3</em>')
      .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '<del>$1</del>')
      // Bare file:line references become clickable-looking code spans.
      .replace(/(^|[\s(])((?:[\w.\-]+\/)+[\w.\-]+\.\w+:\d+)/g,
        '$1<code class="file-ref">$2</code>');

    return out
      .replace(/\u0000LINK(\d+)\u0000/g, (_, i) => links[+i])
      .replace(/\u0000CODE(\d+)\u0000/g,
        (_, i) => '<code>' + escapeHtml(codes[+i]) + '</code>');
  }

  function render(src) {
    if (!src) return '';
    const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
    const html = [];
    let i = 0;

    const listStack = [];
    function closeLists(toDepth) {
      while (listStack.length > toDepth) html.push(listStack.pop() === 'ol' ? '</ol>' : '</ul>');
    }

    while (i < lines.length) {
      const line = lines[i];

      // Fenced code block
      const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/);
      if (fence) {
        closeLists(0);
        const marker = fence[1][0];
        const lang = fence[2] || '';
        const body = [];
        i++;
        while (i < lines.length && !new RegExp('^\\s*' + marker + '{3,}\\s*$').test(lines[i])) {
          body.push(lines[i]);
          i++;
        }
        i++;
        const code = body.join('\n');
        html.push(
          '<div class="code-block" data-code="' + escapeHtml(code) + '">' +
          '<div class="code-head"><span class="code-lang">' + escapeHtml(lang || 'text') + '</span>' +
          '<button type="button" class="code-copy" onclick="copyCode(this)">Copy</button></div>' +
          '<pre><code>' + highlight(escapeHtml(code), lang) + '</code></pre></div>'
        );
        continue;
      }

      // Heading
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        closeLists(0);
        const level = heading[1].length;
        html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        i++;
        continue;
      }

      // Horizontal rule
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        closeLists(0);
        html.push('<hr>');
        i++;
        continue;
      }

      // Blockquote
      if (/^\s*>\s?/.test(line)) {
        closeLists(0);
        const quote = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ''));
          i++;
        }
        html.push('<blockquote>' + render(quote.join('\n')) + '</blockquote>');
        continue;
      }

      // Table
      if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
        closeLists(0);
        const cells = (row) => row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|');
        html.push('<table><thead><tr>' +
          cells(line).map((c) => `<th>${inline(c.trim())}</th>`).join('') +
          '</tr></thead><tbody>');
        i += 2;
        while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) {
          html.push('<tr>' + cells(lines[i]).map((c) => `<td>${inline(c.trim())}</td>`).join('') + '</tr>');
          i++;
        }
        html.push('</tbody></table>');
        continue;
      }

      // List item
      const item = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (item) {
        const depth = Math.floor(item[1].replace(/\t/g, '  ').length / 2) + 1;
        const kind = /^\d/.test(item[2]) ? 'ol' : 'ul';
        while (listStack.length > depth) closeLists(listStack.length - 1);
        while (listStack.length < depth) {
          html.push(kind === 'ol' ? '<ol>' : '<ul>');
          listStack.push(kind);
        }
        html.push('<li>' + inline(item[3]) + '</li>');
        i++;
        continue;
      }

      // Blank line
      if (!line.trim()) {
        closeLists(0);
        i++;
        continue;
      }

      // Paragraph
      closeLists(0);
      const para = [];
      while (i < lines.length && lines[i].trim() &&
             !/^\s*(`{3,}|~{3,})/.test(lines[i]) &&
             !/^(#{1,6})\s/.test(lines[i]) &&
             !/^\s*([-*+]|\d+[.)])\s/.test(lines[i]) &&
             !/^\s*>/.test(lines[i])) {
        para.push(lines[i]);
        i++;
      }
      html.push('<p>' + inline(para.join('\n')).replace(/\n/g, '<br>') + '</p>');
    }

    closeLists(0);
    return html.join('\n');
  }

  global.md = { render, escapeHtml, highlight };
})(window);

function copyCode(button) {
  const block = button.closest('.code-block');
  const code = block ? block.dataset.code : '';
  navigator.clipboard.writeText(code).then(() => {
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = 'Copy'; }, 1400);
  });
}
