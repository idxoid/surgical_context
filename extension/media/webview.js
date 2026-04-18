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
    if (payload.context?.primary_source) {
      const ctx = document.createElement('div');
      ctx.className = 'ctx';
      ctx.textContent = `source: ${payload.context.primary_source}`;
      entryEl.appendChild(ctx);
    }
  }

  function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
}());
