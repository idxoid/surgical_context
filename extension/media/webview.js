(function() {
  const vscode = acquireVsCodeApi();
  const log    = document.getElementById('chat-log');
  const symIn  = document.getElementById('symbol-input');
  const qIn    = document.getElementById('question-input');
  const btn    = document.getElementById('submit-btn');

  btn.addEventListener('click', () => {
    const symbol   = symIn.value.trim();
    const question = qIn.value.trim();
    if (!symbol || !question) {
      appendError('Please fill in both symbol and question.');
      return;
    }
    appendEntry(symbol, question);
    btn.disabled = true;
    vscode.postMessage({ type: 'ask', symbol, question });
  });

  qIn.addEventListener('keypress', (e) => {
    if (e.ctrlKey && e.key === 'Enter') {
      btn.click();
    }
  });

  log.addEventListener('click', (e) => {
    const target = e.target;
    if (target instanceof HTMLElement && target.dataset.filePath) {
      vscode.postMessage({ type: 'openFile', filePath: target.dataset.filePath });
    }
  });

  window.addEventListener('message', ({ data }) => {
    if (data.type === 'answer') {
      const last = log.lastElementChild;
      if (last) renderAnswer(last, data.payload);
      btn.disabled = false;
    }
    if (data.type === 'error') {
      appendError('Error: ' + data.message);
      btn.disabled = false;
    }
    if (data.type === 'prefill') {
      symIn.value = data.symbol;
      qIn.focus();
    }
  });

  function appendEntry(symbol, question) {
    const div = document.createElement('div');
    div.className = 'entry';
    div.innerHTML = `
      <div class="q">[${escHtml(symbol)}] ${escHtml(question)}</div>
      <div class="a">…waiting…</div>`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function appendError(msg) {
    const div = document.createElement('div');
    div.className = 'error';
    div.textContent = msg;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function renderAnswer(entryEl, payload) {
    const a = entryEl.querySelector('.a');
    a.textContent = payload.answer ?? '(no answer)';
    const existing = entryEl.querySelector('.ctx');
    if (existing) existing.remove();
    if (payload.context) entryEl.appendChild(renderContextInspector(payload));
  }

  function renderContextInspector(payload) {
    const context = payload.context;
    const meta = context.metadata ?? {};
    const assembly = meta.assembly ?? {};
    const div = document.createElement('div');
    div.className = 'ctx';

    const title = document.createElement('div');
    title.className = 'ctx-title';
    title.textContent = 'Why this answer used this context';
    div.appendChild(title);

    const metaRow = document.createElement('div');
    metaRow.className = 'ctx-meta';
    addBadge(metaRow, `intent: ${context.intent || 'unknown'}`);
    addBadge(metaRow, `mode: ${context.mode || 'unknown'}`);
    addBadge(metaRow, `route: ${routeLabel(payload.model_route ?? assembly.model_route)}`);
    addBadge(metaRow, `tokens: ${tokenLabel(meta)}`);
    if (payload.trace_id ?? assembly.trace_id) addBadge(metaRow, `trace: ${payload.trace_id ?? assembly.trace_id}`);
    div.appendChild(metaRow);

    div.appendChild(section('Target', [symbolLine(context.primary_source, true)]));
    div.appendChild(section('Graph Neighbors', (context.graph_context ?? []).map(s => symbolLine(s, true))));
    div.appendChild(section('Docs', (context.documentation ?? []).map(docLine)));

    const timings = assembly.stage_timings_ms ?? {};
    const timingLines = Object.keys(timings).map(k => `${k}: ${timings[k]}ms`);
    if (timingLines.length) div.appendChild(section('Timing', timingLines));

    const pruning = meta.pruning_reasons ?? [];
    if (pruning.length) div.appendChild(section('Pruning', pruning));
    return div;
  }

  function section(title, lines) {
    const wrap = document.createElement('div');
    const label = document.createElement('div');
    label.className = 'ctx-title';
    label.textContent = `${title} (${lines.length})`;
    wrap.appendChild(label);

    const ul = document.createElement('ul');
    ul.className = 'ctx-list';
    if (!lines.length) {
      const li = document.createElement('li');
      li.textContent = 'none selected';
      ul.appendChild(li);
    } else {
      for (const line of lines) {
        const li = document.createElement('li');
        if (typeof line === 'string') {
          li.textContent = line;
        } else {
          li.appendChild(line);
        }
        ul.appendChild(li);
      }
    }
    wrap.appendChild(ul);
    return wrap;
  }

  function symbolLine(symbol, includeFile) {
    const span = document.createElement('span');
    const score = fmtScore(symbol?.relevance_score ?? symbol?.scores?.relevance);
    span.appendChild(text(`${symbol?.symbol ?? 'unknown'} ${score}`));
    if (symbol?.relation) span.appendChild(text(` [${symbol.relation}${symbol.depth !== undefined ? ', d=' + symbol.depth : ''}]`));
    if (symbol?.is_dirty) span.appendChild(badge('dirty', 'dirty'));
    if (includeFile && symbol?.file_path) {
      span.appendChild(text(' · '));
      span.appendChild(fileLink(symbol.file_path));
    }
    return span;
  }

  function docLine(doc) {
    const span = document.createElement('span');
    span.appendChild(text(`${doc?.chunk_id ?? 'doc'} ${fmtScore(doc?.score ?? doc?.scores?.semantic)} · `));
    if (doc?.source_file) span.appendChild(fileLink(doc.source_file));
    return span;
  }

  function fileLink(filePath) {
    const button = document.createElement('button');
    button.className = 'file-link';
    button.dataset.filePath = filePath;
    button.textContent = filePath;
    return button;
  }

  function addBadge(parent, label) {
    parent.appendChild(badge(label));
  }

  function badge(label, extraClass) {
    const b = document.createElement('span');
    b.className = `badge${extraClass ? ' ' + extraClass : ''}`;
    b.textContent = label;
    return b;
  }

  function text(value) {
    return document.createTextNode(value);
  }

  function routeLabel(route) {
    if (!route) return 'unknown';
    const provider = route.provider ?? route.preference ?? 'unknown';
    return route.model ? `${provider}/${route.model}` : String(provider);
  }

  function tokenLabel(meta) {
    const primary = meta.tokens_primary ?? 0;
    const graph = meta.tokens_graph ?? 0;
    const docs = meta.tokens_docs ?? 0;
    return `${primary + graph + docs} (${primary}/${graph}/${docs})`;
  }

  function fmtScore(value) {
    return typeof value === 'number' ? `score=${value.toFixed(2)}` : 'score=n/a';
  }

  function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
}());
