/* MyKnowledge chat page. Talks only to this app's own /api/chat/* endpoints. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  // Absolute URLs from the origin: fetch() rejects relative URLs when the page was opened
  // with credentials in its address (https://user:password@host/).
  const api = (path) => new URL(path, window.location.origin).href;
  const log = $('log');
  const form = $('composer');
  const input = $('question');
  const sendButton = $('send');
  const newButton = $('new-conversation');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  let sessionId = null;
  let busy = false;
  let ended = false;

  // ---- sanitising: answers can quote document text, so never trust the HTML ----

  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
  const PURIFY = {
    FORBID_ATTR: ['style'],
    FORBID_TAGS: ['style', 'img', 'form', 'input', 'button', 'textarea', 'select'],
  };

  // ---- small helpers -----------------------------------------------------------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function behavior() {
    return reduceMotion.matches ? 'auto' : 'smooth';
  }

  function autosize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight + 2, 144) + 'px';
  }

  function updateControls() {
    const locked = busy || !sessionId || ended;
    input.disabled = locked;
    sendButton.disabled = locked;
    newButton.disabled = busy;
    log.querySelectorAll('.suggestion').forEach((b) => { b.disabled = locked; });
    if (!locked && document.activeElement === document.body) {
      input.focus({ preventScroll: true });
    }
  }

  async function readDetail(res) {
    try {
      const body = await res.json();
      if (typeof body.detail === 'string') return body.detail;
      if (Array.isArray(body.detail) && body.detail[0] && body.detail[0].msg) return body.detail[0].msg;
    } catch (_) { /* not JSON */ }
    return `The assistant could not be reached (error ${res.status}).`;
  }

  // ---- building the transcript -------------------------------------------------

  function makeThinking() {
    const box = el('div', 'thinking');
    box.setAttribute('role', 'status');
    const dots = el('span', 'dots');
    dots.setAttribute('aria-hidden', 'true');
    dots.append(el('i'), el('i'), el('i'));
    box.append(dots, el('span', null, 'Searching the knowledge base'));
    return box;
  }

  function addTurn(question) {
    const root = el('section', 'turn');
    if (question) root.append(el('p', 'question', question));
    const think = makeThinking();
    think.hidden = true;
    root.append(think);
    log.append(root);
    return { root, think };
  }

  function addNotice(turn, message, onRetry) {
    const notice = el('p', 'notice error', message);
    if (onRetry) {
      const retry = el('button', 'link-button', 'Try again');
      retry.type = 'button';
      retry.addEventListener('click', onRetry);
      notice.append(retry);
    }
    turn.root.insertBefore(notice, turn.think);
  }

  function renderSources(citations) {
    const details = el('details', 'sources');
    const label = `View sources (${citations.length})`;
    const summary = el('summary', null, label);
    const list = el('ol', 'source-list');

    citations.forEach((c) => {
      const item = el('li', 'source');
      if (c.position != null) {
        item.dataset.position = String(c.position);
        item.value = c.position;
      }
      item.append(el('h3', 'source-title', c.title));
      if (c.excerpt) item.append(el('blockquote', 'excerpt', c.excerpt));
      list.append(item);
    });

    details.append(summary, list);
    details.addEventListener('toggle', () => {
      summary.textContent = details.open ? 'Hide sources' : label;
    });

    function open(position) {
      details.open = true;
      const target = list.querySelector(`[data-position="${position}"]`);
      if (!target) return;
      list.querySelectorAll('.is-active').forEach((n) => n.classList.remove('is-active'));
      void target.offsetWidth; // restart the highlight animation
      target.classList.add('is-active');
      target.scrollIntoView({ block: 'nearest', behavior: behavior() });
    }

    return { root: details, open };
  }

  // Turn "[2]" in the answer text into a button that opens source 2.
  function linkCitationMarkers(root, citations, sources) {
    const known = new Set(citations.map((c) => c.position).filter((p) => p != null));
    if (!known.size || !sources) return;

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (node.parentElement.closest('a, code, pre')) return NodeFilter.FILTER_REJECT;
        return /\[\d+\]/.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);

    nodes.forEach((node) => {
      const fragment = document.createDocumentFragment();
      node.nodeValue.split(/(\[\d+\])/).forEach((part) => {
        const match = /^\[(\d+)\]$/.exec(part);
        if (match && known.has(Number(match[1]))) {
          const number = Number(match[1]);
          const button = el('button', 'cite-ref', match[1]);
          button.type = 'button';
          button.setAttribute('aria-label', `Show source ${number}`);
          button.addEventListener('click', () => sources.open(number));
          const sup = el('sup');
          sup.append(button);
          fragment.append(sup);
        } else if (part) {
          fragment.append(document.createTextNode(part));
        }
      });
      node.replaceWith(fragment);
    });
  }

  function renderSuggestions(labels) {
    const row = el('div', 'suggestions');
    labels.forEach((label) => {
      const button = el('button', 'suggestion', label);
      button.type = 'button';
      button.addEventListener('click', () => submit(label));
      row.append(button);
    });
    return row;
  }

  function appendAnswer(turn, event) {
    const citations = event.citations || [];
    const reply = el('div', 'reply');
    const sources = citations.length ? renderSources(citations) : null;

    if (event.markdown) {
      const answer = el('div', 'answer');
      answer.innerHTML = DOMPurify.sanitize(marked.parse(event.markdown, { gfm: true }), PURIFY);
      linkCitationMarkers(answer, citations, sources);
      reply.append(answer);
    }
    if (sources) reply.append(sources.root);
    if (event.suggestions && event.suggestions.length) reply.append(renderSuggestions(event.suggestions));

    turn.root.insertBefore(reply, turn.think);
    updateControls();
  }

  // ---- talking to the server ---------------------------------------------------

  async function startSession() {
    const res = await fetch(api('/api/chat/start'), { method: 'POST' });
    if (!res.ok) throw new Error(await readDetail(res));
    const data = await res.json();
    sessionId = data.session_id;
    return data;
  }

  async function readEvents(body, onEvent) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const data = block
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart())
          .join('\n');
        if (!data) continue;
        try {
          onEvent(JSON.parse(data));
        } catch (err) {
          console.error('Could not read a server event', err);
        }
      }
    }
  }

  function handleEvent(event, turn, text) {
    switch (event.type) {
      case 'thinking':
        turn.think.hidden = false;
        break;
      case 'answer':
        appendAnswer(turn, event);
        break;
      case 'done':
        turn.think.hidden = true;
        break;
      case 'ended': {
        turn.think.hidden = true;
        ended = true;
        const notice = el('p', 'notice', 'The assistant ended this conversation.');
        const again = el('button', 'link-button', 'Start a new conversation');
        again.type = 'button';
        again.addEventListener('click', begin);
        notice.append(again);
        turn.root.insertBefore(notice, turn.think);
        break;
      }
      case 'error':
        turn.think.hidden = true;
        addNotice(turn, event.message, () => retry(turn, text));
        break;
      default:
        break;
    }
  }

  function retry(turn, text) {
    turn.root.remove();
    submit(text);
  }

  async function submit(raw) {
    const text = raw.trim();
    if (!text || busy || !sessionId || ended) return;

    log.querySelectorAll('.suggestions').forEach((n) => n.remove());
    input.value = '';
    autosize();

    const turn = addTurn(text);
    turn.think.hidden = false;
    turn.root.scrollIntoView({ block: 'start', behavior: behavior() });
    busy = true;
    updateControls();

    try {
      const res = await fetch(api('/api/chat/message'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, text }),
      });

      if (res.status === 404) {
        // The server forgot this conversation (timeout or restart). Start a new one
        // and put the question back so nothing is lost.
        turn.root.remove();
        await startSession();
        const note = addTurn(null);
        note.root.insertBefore(
          el('p', 'notice', 'This conversation timed out, so a new one was started. Send your question again.'),
          note.think,
        );
        input.value = text;
        autosize();
      } else if (!res.ok) {
        addNotice(turn, await readDetail(res), () => retry(turn, text));
      } else {
        await readEvents(res.body, (event) => handleEvent(event, turn, text));
      }
    } catch (err) {
      // If the failed turn was already removed (expired-session path), show the notice in a fresh block.
      const target = turn.root.isConnected ? turn : addTurn(null);
      addNotice(
        target,
        'The connection dropped before the answer finished. Check your network, then try again.',
        () => retry(target, text),
      );
    } finally {
      turn.think.hidden = true;
      busy = false;
      updateControls();
    }
  }

  async function begin() {
    log.replaceChildren();
    sessionId = null;
    ended = false;
    updateControls();

    const intro = addTurn(null);
    intro.think.querySelector('span:last-child').textContent = 'Connecting to the assistant';
    intro.think.hidden = false;

    try {
      const data = await startSession();
      intro.think.hidden = true;
      appendAnswer(intro, {
        markdown: data.greeting || 'Ask a question about the knowledge base.',
        citations: [],
        suggestions: [],
      });
    } catch (err) {
      intro.think.hidden = true;
      const message = err instanceof TypeError ? 'The assistant could not be reached.' : err.message;
      addNotice(intro, message, begin);
    }
    updateControls();
  }

  // ---- wiring ------------------------------------------------------------------

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submit(input.value);
  });

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  input.addEventListener('input', autosize);
  newButton.addEventListener('click', () => { if (!busy) begin(); });

  begin();
})();
