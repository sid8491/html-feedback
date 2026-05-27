// html-feedback client library — vanilla JS, no framework, no build.
// Single global: window.__hfb. Self-bootstraps after DOMContentLoaded.

(() => {
  if (window.__hfb) return; // idempotent

  // ─── Token & URL helpers ───
  const SCRIPT_EL = document.currentScript
    || Array.from(document.scripts).find(s => /feedback\.js(\?|$)/.test(s.src || ''));
  const SCRIPT_SRC = SCRIPT_EL ? new URL(SCRIPT_EL.src, location.href) : new URL(location.href);
  const TOKEN =
    SCRIPT_SRC.searchParams.get('t')
    || new URLSearchParams(location.search).get('t')
    || '';

  const PAGE = decodeURIComponent(location.pathname.replace(/^\/+/, '')) || 'index.html';

  const api = (path, opts = {}) => {
    const u = new URL(path, location.origin);
    if (TOKEN) u.searchParams.set('t', TOKEN);
    return fetch(u.toString(), {
      ...opts,
      headers: {
        'Content-Type': 'application/json',
        ...(opts.headers || {}),
      },
    });
  };

  // ─── ID & timestamp helpers ───
  const newId = () => 'c-' + crypto.randomUUID().replace(/-/g, '').slice(0, 12);
  const nowTs = () => new Date().toISOString().replace(/\.\d+Z$/, 'Z');

  // ─── DOM helpers ───
  const h = (tag, attrs = {}, ...children) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (v !== null && v !== undefined) el.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return el;
  };

  // ─── Selector computation ───
  const computeSelector = (el) => {
    if (!el || el === document.body || el.nodeType !== 1) return 'body';
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && cur.nodeType === 1) {
      if (cur.id) {
        parts.unshift('#' + CSS.escape(cur.id));
        break;
      }
      const tag = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (!parent) { parts.unshift(tag); break; }
      const siblings = Array.from(parent.children).filter(s => s.tagName === cur.tagName);
      if (siblings.length === 1) {
        parts.unshift(tag);
      } else {
        const idx = siblings.indexOf(cur) + 1;
        parts.unshift(`${tag}:nth-of-type(${idx})`);
      }
      cur = parent;
    }
    return parts.join(' > ') || 'body';
  };

  // ─── Text-context capture ───
  const trimCtx = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 40);

  const captureTextContext = (range) => {
    // Walk back/forward in body text to gather up to 40 chars of context.
    const root = document.body;
    const before = document.createRange();
    before.setStart(root, 0);
    before.setEnd(range.startContainer, range.startOffset);
    const after = document.createRange();
    after.setStart(range.endContainer, range.endOffset);
    after.setEnd(root, root.childNodes.length);
    const beforeText = before.toString();
    const afterText = after.toString();
    return {
      text_before: trimCtx(beforeText.slice(-80).slice(-40)),
      text_after: trimCtx(afterText.slice(0, 80).slice(0, 40)),
    };
  };

  const rectFromRange = (range) => {
    const r = range.getBoundingClientRect();
    return {
      x: Math.round(r.left + window.scrollX),
      y: Math.round(r.top + window.scrollY),
      w: Math.round(r.width),
      h: Math.round(r.height),
    };
  };
  const rectFromEl = (el) => {
    const r = el.getBoundingClientRect();
    return {
      x: Math.round(r.left + window.scrollX),
      y: Math.round(r.top + window.scrollY),
      w: Math.round(r.width),
      h: Math.round(r.height),
    };
  };

  // ─── Whitespace-tolerant text matching ───
  const collapseWs = (s) => (s || '').replace(/\s+/g, ' ').trim();

  // ─── State ───
  const state = {
    comments: [],            // raw comments from inbox
    pending: new Map(),      // tempId -> { comment, status }
    orphans: [],             // resolved orphans
    threads: new Map(),      // rootId -> { root, replies[] }
    anchorMap: new Map(),    // commentId -> resolved DOM Range or Element (or null)
    sidebarOpen: false,
    pickMode: false,
    pickHoverEl: null,
    rePinFor: null,          // orphan comment_id awaiting re-pin
    composer: { open: false, ctx: null },
    changes: [],             // [{el, entry}]
    changeIdx: -1,
    historyByComment: new Map(),  // commentId -> latest non-reverted edit entry
    redoableByComment: new Map(), // commentId -> latest reverted edit (re-apply candidate)
    sse: null,
    sseRetry: 0,
  };

  // ─── Root mount ───
  const root = h('div', { class: 'hfb-root', id: 'hfb-root' });
  document.documentElement.appendChild(root);
  // SPEC-NOTE: mount on documentElement so we live above <body>; some pages may have z-stacking quirks.

  // ─── FAB ───
  const fabBadge = h('span', { class: 'hfb-fab-badge', dataset: { count: '0' } }, '0');
  const fab = h('button', {
    class: 'hfb-fab',
    title: 'Open feedback (toggle)',
    onclick: () => toggleSidebar(),
  }, '💬', fabBadge);
  root.appendChild(fab);

  // ─── Sidebar ───
  const sidebarBody = h('div', { class: 'hfb-sidebar-body' });
  const processBtnLabel = h('span', {}, 'Process (0)');
  const processBtn = h('button', {
    class: 'hfb-btn hfb-btn-sm hfb-btn-process',
    title: 'Send all open comments to Claude for processing',
    onclick: () => triggerProcess(),
    disabled: true,
  }, '▶ ', processBtnLabel);
  const sidebar = h('aside', { class: 'hfb-sidebar' },
    h('div', { class: 'hfb-sidebar-header' },
      h('div', { class: 'hfb-sidebar-title' }, 'Feedback'),
      h('div', { class: 'hfb-sidebar-actions' },
        processBtn,
        h('button', {
          class: 'hfb-btn hfb-btn-sm',
          title: 'Comment on the whole page',
          onclick: () => openComposerForPage(),
        }, '+ Page'),
        h('button', {
          class: 'hfb-btn hfb-btn-sm',
          title: 'Element-pick mode (e)',
          onclick: () => togglePickMode(),
        }, '⌖ Pick'),
        h('button', {
          class: 'hfb-btn hfb-btn-sm hfb-btn-ghost',
          title: 'Help (?)',
          onclick: () => toggleHelp(),
        }, '?'),
        h('button', {
          class: 'hfb-btn hfb-btn-sm hfb-btn-ghost',
          title: 'End session and clean up',
          onclick: () => endSession(),
        }, '⏻'),
        h('button', {
          class: 'hfb-btn hfb-btn-sm hfb-btn-ghost',
          title: 'Close',
          onclick: () => toggleSidebar(false),
        }, '×'),
      ),
    ),
    sidebarBody,
  );
  root.appendChild(sidebar);

  // ─── Selection toolbar ───
  const selToolbar = h('div', { class: 'hfb-sel-toolbar' },
    h('button', {
      onclick: (e) => { e.preventDefault(); commentCurrentSelection(); },
    }, '💬 Comment'),
  );
  root.appendChild(selToolbar);

  // ─── Composer ───
  const composerEl = h('div', { class: 'hfb-composer' });
  root.appendChild(composerEl);

  // ─── Pick banner ───
  const pickBanner = h('div', { class: 'hfb-pick-banner' }, 'Click any element to comment — Esc to cancel');
  root.appendChild(pickBanner);

  // ─── Change pill & popover ───
  const changePillLabel = h('span', {}, '');
  const changePill = h('div', { class: 'hfb-change-pill' },
    changePillLabel,
    h('button', { onclick: () => walkChange(-1) }, '◀ k'),
    h('button', { onclick: () => walkChange(1) }, 'j ▶'),
    h('button', { onclick: () => dismissChanges() }, '✕'),
  );
  root.appendChild(changePill);

  // ─── Help overlay ───
  const helpOverlay = h('div', { class: 'hfb-help', onclick: (e) => { if (e.target === helpOverlay) toggleHelp(false); } },
    h('div', { class: 'hfb-help-card' },
      h('h2', {}, 'Keyboard shortcuts'),
      h('dl', {},
        h('dt', {}, 'c'), h('dd', {}, 'Comment current text selection'),
        h('dt', {}, 'e'), h('dd', {}, 'Toggle element-pick mode'),
        h('dt', {}, 'j / k'), h('dd', {}, 'Walk through changes'),
        h('dt', {}, 'r'), h('dd', {}, 'Revert the current change (during walkthrough)'),
        h('dt', {}, '?'), h('dd', {}, 'This help'),
        h('dt', {}, 'Esc'), h('dd', {}, 'Cancel current action / close'),
      ),
    ),
  );
  root.appendChild(helpOverlay);

  // ─── Toast ───
  const toastEl = h('div', { class: 'hfb-toast' });
  root.appendChild(toastEl);
  let toastTimer = null;
  const toast = (msg, ms = 2200) => {
    toastEl.textContent = msg;
    toastEl.classList.add('hfb-visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('hfb-visible'), ms);
  };

  // ─── Sidebar/UI toggles ───
  const toggleSidebar = (force) => {
    state.sidebarOpen = typeof force === 'boolean' ? force : !state.sidebarOpen;
    sidebar.classList.toggle('hfb-open', state.sidebarOpen);
    if (state.sidebarOpen) markUnreadSeen();
  };
  const toggleHelp = (force) => {
    const open = typeof force === 'boolean' ? force : !helpOverlay.classList.contains('hfb-visible');
    helpOverlay.classList.toggle('hfb-visible', open);
  };

  // ─── Selection tracking ───
  const updateSelectionToolbar = () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) {
      selToolbar.classList.remove('hfb-visible');
      return;
    }
    const range = sel.getRangeAt(0);
    if (root.contains(range.commonAncestorContainer)) {
      selToolbar.classList.remove('hfb-visible');
      return;
    }
    const txt = sel.toString();
    if (!txt.trim()) {
      selToolbar.classList.remove('hfb-visible');
      return;
    }
    const r = range.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    const top = window.scrollY + r.top - 34;
    const left = window.scrollX + r.left + r.width / 2 - 50;
    selToolbar.style.top = `${Math.max(0, top)}px`;
    selToolbar.style.left = `${Math.max(4, left)}px`;
    selToolbar.classList.add('hfb-visible');
  };

  document.addEventListener('selectionchange', () => {
    // Defer to allow selection to settle.
    requestAnimationFrame(updateSelectionToolbar);
  });
  window.addEventListener('scroll', updateSelectionToolbar, { passive: true });
  window.addEventListener('resize', updateSelectionToolbar);

  // ─── Comment current selection ───
  const commentCurrentSelection = () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) {
      toast('Select some text first');
      return;
    }
    const range = sel.getRangeAt(0).cloneRange();
    if (root.contains(range.commonAncestorContainer)) return;
    const selectedText = sel.toString();
    if (!selectedText.trim()) return;
    const startEl = (range.startContainer.nodeType === 3 ? range.startContainer.parentElement : range.startContainer);
    const ctx = captureTextContext(range);
    const anchor = {
      type: 'text',
      selector: computeSelector(startEl),
      selected_text: selectedText,
      text_before: ctx.text_before,
      text_after: ctx.text_after,
      rect: rectFromRange(range),
    };
    openComposer(anchor, range);
  };

  // ─── Element-pick mode ───
  const togglePickMode = (force) => {
    const next = typeof force === 'boolean' ? force : !state.pickMode;
    state.pickMode = next;
    document.documentElement.classList.toggle('hfb-pick-active', next);
    pickBanner.classList.toggle('hfb-visible', next);
    if (!next && state.pickHoverEl) {
      state.pickHoverEl.classList.remove('hfb-pick-outline');
      state.pickHoverEl = null;
    }
  };

  const onPickMove = (e) => {
    if (!state.pickMode) return;
    let target = e.target;
    if (!target || root.contains(target)) return;
    if (state.pickHoverEl && state.pickHoverEl !== target) {
      state.pickHoverEl.classList.remove('hfb-pick-outline');
    }
    target.classList.add('hfb-pick-outline');
    state.pickHoverEl = target;
  };

  const onPickClick = (e) => {
    if (!state.pickMode) return;
    const target = e.target;
    if (!target || root.contains(target)) return;
    e.preventDefault();
    e.stopPropagation();
    togglePickMode(false);
    // Re-pin path
    if (state.rePinFor) {
      const orphanId = state.rePinFor;
      state.rePinFor = null;
      const anchor = buildElementAnchor(target);
      openComposer(anchor, null, orphanId);
      return;
    }
    const anchor = buildElementAnchor(target);
    openComposer(anchor, null);
  };

  document.addEventListener('mousemove', onPickMove, true);
  document.addEventListener('click', onPickClick, true);

  const buildElementAnchor = (el) => {
    const text = (el.innerText || el.textContent || '').trim();
    // text_after = first 40 chars of text after the element in document order
    const range = document.createRange();
    range.setStartAfter(el);
    range.setEnd(document.body, document.body.childNodes.length);
    return {
      type: 'element',
      selector: computeSelector(el),
      selected_text: '',
      text_before: trimCtx(text.slice(0, 80)),
      text_after: trimCtx(range.toString().slice(0, 80)),
      rect: rectFromEl(el),
    };
  };

  // ─── Composer ───
  const openComposer = (anchor, range, parentId = null) => {
    state.composer = { open: true, ctx: { anchor, range, parentId } };
    composerEl.innerHTML = '';
    const snippet = anchor.type === 'text'
      ? anchor.selected_text
      : anchor.type === 'element'
        ? `<${anchor.selector}> ${anchor.text_before}`
        : '(whole page)';
    const replyBadge = parentId
      ? h('div', { class: 'hfb-composer-reply-badge' }, `Reply to ${parentId.slice(0, 8)}…`)
      : null;
    const snippetEl = h('div', { class: 'hfb-composer-snippet' }, snippet.length > 120 ? snippet.slice(0, 117) + '…' : snippet);
    const ta = h('textarea', { placeholder: 'Leave a comment… (Cmd/Ctrl+Enter to send)' });
    const sendBtn = h('button', { class: 'hfb-btn hfb-btn-primary', onclick: () => submitComposer() }, 'Send');
    const cancelBtn = h('button', { class: 'hfb-btn', onclick: () => closeComposer() }, 'Cancel');
    composerEl.appendChild(replyBadge || document.createComment(''));
    composerEl.appendChild(snippetEl);
    composerEl.appendChild(ta);
    composerEl.appendChild(h('div', { class: 'hfb-composer-actions' }, cancelBtn, sendBtn));

    // Position near anchor.rect
    positionComposer(anchor.rect);
    composerEl.classList.add('hfb-visible');
    setTimeout(() => ta.focus(), 30);
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submitComposer(); }
      else if (e.key === 'Escape') { e.preventDefault(); closeComposer(); }
    });
  };

  const positionComposer = (rect) => {
    if (!rect) {
      composerEl.style.top = `${window.scrollY + 100}px`;
      composerEl.style.left = `${window.scrollX + (window.innerWidth / 2) - 160}px`;
      return;
    }
    const top = rect.y + rect.h + 8;
    const left = Math.max(window.scrollX + 8, rect.x);
    const maxLeft = window.scrollX + window.innerWidth - 332;
    composerEl.style.top = `${top}px`;
    composerEl.style.left = `${Math.min(left, maxLeft)}px`;
  };

  const closeComposer = () => {
    composerEl.classList.remove('hfb-visible');
    state.composer = { open: false, ctx: null };
  };

  const openComposerForPage = () => {
    openComposer({
      type: 'page',
      selector: '',
      selected_text: '',
      text_before: '',
      text_after: '',
      rect: null,
    }, null);
  };

  const submitComposer = () => {
    if (!state.composer.open) return;
    const ta = composerEl.querySelector('textarea');
    const text = (ta?.value || '').trim();
    if (!text) { toast('Write a message first'); return; }
    const { anchor, parentId } = state.composer.ctx;
    const comment = {
      id: newId(),
      parent_id: parentId || null,
      page: PAGE,
      anchor,
      comment: text,
      ts: nowTs(),
      author: 'local',
    };
    closeComposer();
    sendComment(comment);
  };

  // ─── Send comment with optimistic UI ───
  const sendComment = async (comment, retry = false) => {
    state.pending.set(comment.id, { comment, status: 'sending' });
    if (!retry) {
      state.comments.push({ ...comment, _local: true });
      renderSidebar();
    } else {
      renderSidebar();
    }
    try {
      const res = await api('/api/feedback', { method: 'POST', body: JSON.stringify(comment) });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      state.pending.set(comment.id, { comment, status: 'sent' });
      setTimeout(() => { state.pending.delete(comment.id); renderSidebar(); }, 1000);
      toast('Comment sent');
    } catch (e) {
      state.pending.set(comment.id, { comment, status: 'failed' });
      toast('Failed to send — retry from sidebar');
    }
    renderSidebar();
  };

  // ─── Inbox / threads / render ───
  const loadInbox = async () => {
    try {
      const res = await api('/api/inbox');
      if (!res.ok) return;
      const data = await res.json();
      const incoming = (data.comments || []).filter(c => c.page === PAGE);
      const localOnly = state.comments.filter(c => c._local && !incoming.find(x => x.id === c.id));
      state.comments = [...incoming, ...localOnly];
      await loadHistoryMap();
      resolveAllAnchors();
      renderSidebar();
      updateBadge();
      reconcileProcessing();
    } catch (e) { /* ignore */ }
  };

  const loadHistoryMap = async () => {
    try {
      const res = await api('/api/history');
      if (!res.ok) return;
      const data = await res.json();
      const entries = (data.entries || []).filter(e => e.page === PAGE);
      const revertedSnaps = new Set(entries.filter(e => e.kind === 'revert' && e.snapshot_path).map(e => e.snapshot_path));
      const live = new Map();
      const redoable = new Map();
      for (const e of entries) {
        if (e.kind !== 'edit' || !e.comment_id) continue;
        if (revertedSnaps.has(e.snapshot_path)) {
          // Reverted edit — eligible for redo if it has both snapshot paths
          if (e.snapshot_after_path) redoable.set(e.comment_id, e);
          live.delete(e.comment_id);
        } else {
          live.set(e.comment_id, e);
          redoable.delete(e.comment_id);
        }
      }
      state.historyByComment = live;
      state.redoableByComment = redoable;
    } catch (_) { /* ignore */ }
  };

  const buildThreads = () => {
    const byId = new Map(state.comments.map(c => [c.id, c]));
    const threads = new Map(); // rootId -> {root, replies[]}
    const orphanThreads = [];
    for (const c of state.comments) {
      if (c.parent_id && byId.has(c.parent_id)) continue;
      threads.set(c.id, { root: c, replies: [] });
    }
    for (const c of state.comments) {
      if (c.parent_id && byId.has(c.parent_id)) {
        // climb to root
        let root = c;
        while (root.parent_id && byId.has(root.parent_id)) root = byId.get(root.parent_id);
        const t = threads.get(root.id);
        if (t) t.replies.push(c);
      }
    }
    for (const t of threads.values()) {
      t.replies.sort((a, b) => (a.ts || '').localeCompare(b.ts || ''));
    }
    return threads;
  };

  const renderSidebar = () => {
    sidebarBody.innerHTML = '';
    const threads = buildThreads();
    const live = [];
    const orphans = [];
    for (const t of threads.values()) {
      const resolved = state.anchorMap.get(t.root.id);
      if (t.root.anchor?.type === 'page' || resolved) live.push(t);
      else orphans.push(t);
    }
    // Refresh the Process button + FAB badge on every render so state changes
    // (optimistic insert, dedup, addressed flip) always reflect in the UI.
    updateBadge();
    if (live.length === 0 && orphans.length === 0) {
      sidebarBody.appendChild(h('div', { class: 'hfb-empty' },
        'No comments yet.\nSelect text or pick an element to leave one.'));
      return;
    }
    // Sort newest-first by root timestamp.
    const byTsDesc = (a, b) => (b.root.ts || '').localeCompare(a.root.ts || '');
    live.sort(byTsDesc);
    orphans.sort(byTsDesc);
    if (live.length > 0) {
      const addressedCount = state.comments.filter(c => c.status === 'addressed' && !c.parent_id).length;
      const headerChildren = [
        h('span', {}, `Comments (${live.length})`),
        addressedCount > 0
          ? h('button', {
              class: 'hfb-clear-addressed',
              title: `Delete ${addressedCount} addressed comment${addressedCount === 1 ? '' : 's'}`,
              onclick: () => clearAddressed(),
            }, `clear addressed (${addressedCount})`)
          : null,
      ].filter(Boolean);
      sidebarBody.appendChild(h('div', { class: 'hfb-section-header hfb-section-header-row' }, ...headerChildren));
      for (const t of live) sidebarBody.appendChild(renderThread(t, false));
    }
    if (orphans.length > 0) {
      sidebarBody.appendChild(h('div', { class: 'hfb-section-header' }, `Orphans (${orphans.length})`));
      for (const t of orphans) sidebarBody.appendChild(renderThread(t, true));
    }
  };

  const renderThread = (t, isOrphan) => {
    const root = t.root;
    const anchor = root.anchor || {};
    const anchorLabel = anchor.type === 'page'
      ? '📄 Whole page'
      : anchor.type === 'element'
        ? `⌖ ${anchor.selector || 'element'}`
        : `“${(anchor.selected_text || '').slice(0, 60)}”`;

    const card = h('div', { class: 'hfb-thread' + (isOrphan ? ' hfb-orphan' : '') });
    const anchorRow = h('div', {
      class: 'hfb-thread-anchor',
      onclick: () => focusAnchor(root.id),
    },
      h('span', { class: 'hfb-anchor-icon' }, isOrphan ? '⚠️' : '📍'),
      h('span', { class: 'hfb-anchor-text' }, anchorLabel),
    );
    card.appendChild(anchorRow);
    card.appendChild(renderComment(root, false, isOrphan));
    for (const r of t.replies) card.appendChild(renderComment(r, true, false));

    // Reply input area
    const replyBtn = h('button', { class: 'hfb-btn hfb-btn-sm', onclick: () => {
      openComposer(root.anchor || pageAnchor(), null, root.id);
    } }, 'Reply');
    const actions = h('div', { class: 'hfb-comment-actions', style: { padding: '6px 10px' } }, replyBtn);
    if (isOrphan) {
      actions.appendChild(h('button', {
        class: 'hfb-btn hfb-btn-sm',
        onclick: () => startRePin(root.id),
      }, 'Re-pin'));
    }
    card.appendChild(actions);
    return card;
  };

  const renderComment = (c, isReply, isOrphan) => {
    const pending = state.pending.get(c.id);
    let statusBadge = null;
    if (pending) {
      const cls = pending.status === 'sending' ? 'hfb-status-sending'
        : pending.status === 'sent' ? 'hfb-status-sent'
        : 'hfb-status-failed';
      statusBadge = h('span', { class: 'hfb-status-badge ' + cls }, pending.status);
    } else if (c.status === 'addressed') {
      statusBadge = h('span', { class: 'hfb-status-badge hfb-status-addressed' }, '✓ addressed');
    }
    const tsShort = (c.ts || '').replace('T', ' ').replace('Z', '');
    const deleteBtn = h('button', {
      class: 'hfb-delete-btn',
      title: 'Delete this comment',
      onclick: (ev) => { ev.stopPropagation(); deleteComment(c); },
    }, '×');
    const node = h('div', { class: 'hfb-comment' + (isReply ? ' hfb-comment-reply' : '') },
      h('div', { class: 'hfb-comment-meta' },
        h('span', { class: 'hfb-comment-author' }, c.author || 'local'),
        h('span', {}, tsShort),
        statusBadge || '',
        h('span', { class: 'hfb-meta-spacer' }, ''),
        deleteBtn,
      ),
      h('div', { class: 'hfb-comment-body' }, c.comment || ''),
    );
    const liveEdit = c.status === 'addressed' ? state.historyByComment.get(c.id) : null;
    const redoable = c.status !== 'addressed' ? state.redoableByComment.get(c.id) : null;
    if (liveEdit) {
      const revertLink = h('button', {
        class: 'hfb-revert-link',
        title: liveEdit.summary || 'Revert this edit',
        onclick: () => revertEntry(liveEdit),
      }, '↶ Revert');
      node.appendChild(h('div', { class: 'hfb-comment-actions' }, revertLink));
    } else if (redoable) {
      const redoLink = h('button', {
        class: 'hfb-revert-link hfb-redo-link',
        title: redoable.summary || 'Re-apply this edit',
        onclick: () => redoEntry(redoable),
      }, '↺ Redo');
      node.appendChild(h('div', { class: 'hfb-comment-actions' }, redoLink));
    }
    if (pending && pending.status === 'failed') {
      const actions = h('div', { class: 'hfb-comment-actions' },
        h('button', { class: 'hfb-btn hfb-btn-sm', onclick: () => sendComment(c, true) }, 'Retry'),
      );
      node.appendChild(actions);
    }
    return node;
  };

  const pageAnchor = () => ({ type: 'page', selector: '', selected_text: '', text_before: '', text_after: '', rect: null });

  const startRePin = (orphanId) => {
    state.rePinFor = orphanId;
    toast('Re-pin: select text or pick an element');
    togglePickMode(true);
  };

  // ─── Focus anchor in page ───
  const focusAnchor = (commentId) => {
    const target = state.anchorMap.get(commentId);
    if (!target) { toast('Anchor not found'); return; }
    let el = target;
    if (target instanceof Range) {
      el = target.startContainer.nodeType === 3 ? target.startContainer.parentElement : target.startContainer;
    }
    if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    // brief flash
    const orig = el?.style?.outline || '';
    if (el?.style) {
      el.style.outline = '3px solid rgba(37,99,235,0.7)';
      el.style.outlineOffset = '2px';
      setTimeout(() => { el.style.outline = orig; }, 1200);
    }
  };

  // ─── Pending-count tracking ───
  // The FAB badge and the Process button both show the count of OPEN (unprocessed)
  // root comments on this page — WhatsApp-style unread bubble.
  const updateBadge = () => {
    const openCount = state.comments.filter(c => c.status !== 'addressed' && !c.parent_id).length;
    fabBadge.textContent = String(openCount);
    fabBadge.dataset.count = String(openCount);
    processBtnLabel.textContent = `Process (${openCount})`;
    processBtn.disabled = openCount === 0;
  };

  // ─── Custom confirm modal (replaces browser-native confirm) ───
  const showConfirm = ({ title, message, confirmLabel = 'Confirm', cancelLabel = 'Cancel', danger = false }) => {
    return new Promise((resolve) => {
      const close = (ok) => {
        backdrop.classList.remove('hfb-visible');
        document.removeEventListener('keydown', onKey, true);
        setTimeout(() => backdrop.remove(), 180);
        resolve(ok);
      };
      const onKey = (e) => {
        if (e.key === 'Escape') { e.stopPropagation(); close(false); }
        else if (e.key === 'Enter') { e.stopPropagation(); close(true); }
      };
      const cancelBtn = h('button', { class: 'hfb-btn hfb-btn-sm hfb-btn-ghost', onclick: () => close(false) }, cancelLabel);
      const confirmBtn = h('button', {
        class: 'hfb-btn hfb-btn-sm ' + (danger ? 'hfb-btn-danger-solid' : 'hfb-btn-primary'),
        onclick: () => close(true),
      }, confirmLabel);
      const card = h('div', { class: 'hfb-modal-card' },
        title ? h('div', { class: 'hfb-modal-title' }, title) : null,
        h('div', { class: 'hfb-modal-message' }, message),
        h('div', { class: 'hfb-modal-actions' }, cancelBtn, confirmBtn),
      );
      const backdrop = h('div', {
        class: 'hfb-modal-backdrop',
        onclick: (e) => { if (e.target === backdrop) close(false); },
      }, card);
      root.appendChild(backdrop);
      requestAnimationFrame(() => {
        backdrop.classList.add('hfb-visible');
        confirmBtn.focus();
      });
      document.addEventListener('keydown', onKey, true);
    });
  };

  const deleteComment = async (c) => {
    const label = c.comment ? `"${c.comment.slice(0, 60)}${c.comment.length > 60 ? '…' : ''}"` : 'this comment';
    const ok = await showConfirm({ title: 'Delete comment?', message: `Delete ${label}? This cannot be undone.`, confirmLabel: 'Delete', danger: true });
    if (!ok) return;
    // Optimistic: drop locally first.
    state.comments = state.comments.filter(x => x.id !== c.id && x.parent_id !== c.id);
    state.pending.delete(c.id);
    resolveAllAnchors();
    renderSidebar();
    try {
      const res = await api('/api/feedback/delete', { method: 'POST', body: JSON.stringify({ id: c.id }) });
      if (!res.ok) {
        toast('Delete failed — reloading state');
        await loadInbox();
        return;
      }
      toast('Comment deleted');
    } catch (e) {
      toast('Delete failed — reloading state');
      await loadInbox();
    }
  };

  const clearAddressed = async () => {
    const addressed = state.comments.filter(c => c.status === 'addressed' && !c.parent_id);
    if (addressed.length === 0) return;
    try {
      const res = await api('/api/feedback/clear-addressed', { method: 'POST', body: JSON.stringify({ page: PAGE }) });
      if (!res.ok) { toast('Clear failed'); return; }
      const data = await res.json();
      toast(`Cleared ${data.count || 0} comments`);
      await loadInbox();
    } catch (e) {
      toast('Clear failed');
    }
  };

  // ─── Processing status (persisted across reloads via sessionStorage) ───
  const PROCESSING_KEY = `hfb_processing_${PAGE}`;
  const loadProcessingQueue = () => {
    try { return new Set(JSON.parse(sessionStorage.getItem(PROCESSING_KEY) || '[]')); }
    catch (_) { return new Set(); }
  };
  const saveProcessingQueue = (set) => {
    if (set.size === 0) sessionStorage.removeItem(PROCESSING_KEY);
    else sessionStorage.setItem(PROCESSING_KEY, JSON.stringify([...set]));
  };
  state.processing = loadProcessingQueue();

  const processingBanner = h('div', { class: 'hfb-processing-banner' });
  root.appendChild(processingBanner);

  const renderProcessingBanner = () => {
    if (state.processing.size === 0) {
      processingBanner.classList.remove('hfb-visible');
      processingBanner.innerHTML = '';
      return;
    }
    // Count how many of the queued IDs are still NOT addressed yet.
    const ids = state.processing;
    const stillOpen = state.comments.filter(c => ids.has(c.id) && c.status !== 'addressed').length;
    const total = ids.size;
    const done = total - stillOpen;
    processingBanner.innerHTML = '';
    processingBanner.appendChild(h('span', { class: 'hfb-spinner' }, ''));
    processingBanner.appendChild(h('span', {},
      stillOpen === 0
        ? `✓ All ${total} processed — reloading…`
        : `Processing ${done}/${total}… waiting for Claude`));
    processingBanner.classList.add('hfb-visible');
  };

  const reconcileProcessing = () => {
    // Drop IDs that are now addressed from the queue; toast when empty.
    if (state.processing.size === 0) return;
    const addressedIds = new Set(state.comments.filter(c => c.status === 'addressed').map(c => c.id));
    let changed = false;
    for (const id of [...state.processing]) {
      if (addressedIds.has(id)) { state.processing.delete(id); changed = true; }
    }
    if (changed) saveProcessingQueue(state.processing);
    renderProcessingBanner();
    if (state.processing.size === 0 && changed) {
      toast('All comments processed');
    }
  };

  const endSession = async () => {
    const purge = await showConfirm({
      title: 'End session?',
      message: 'Stop the server and remove the injection tags from your HTML files. Optionally also delete the feedback/ folder (comments, history, snapshots) — uncheck if you want to keep them for next time.',
      confirmLabel: 'End & delete history',
      cancelLabel: 'Keep history',
      danger: true,
    });
    // Two-stage: confirm dialog returns true for "end + purge", false for "keep history".
    // But the user might want to fully cancel. We treat Esc-as-cancel by adding a third path:
    // if they want to just close, they hit the × button instead. Here we always proceed with
    // a shutdown; only the purge flag varies.
    try {
      await api('/api/shutdown', { method: 'POST', body: JSON.stringify({ purge_feedback: purge }) });
      toast('Session ending — cleaning up…');
      setTimeout(() => {
        document.body.innerHTML = '<div style="font-family:system-ui;text-align:center;padding:80px 20px;color:#666"><h1 style="font-size:28px;margin-bottom:12px">Session ended</h1><p>The server has stopped and the injection tags have been removed from your HTML files.</p><p style="margin-top:24px;font-size:13px;color:#999">You can close this tab.</p></div>';
      }, 800);
    } catch (e) {
      toast('Shutdown request failed');
    }
  };

  const triggerProcess = async () => {
    try {
      const res = await api('/api/process', { method: 'POST', body: JSON.stringify({ page: PAGE }) });
      if (!res.ok) { toast('Trigger failed'); return; }
      const data = await res.json();
      const n = data.pending || 0;
      if (n === 0) { toast('Nothing pending'); return; }
      for (const id of (data.ids || [])) state.processing.add(id);
      saveProcessingQueue(state.processing);
      toast(`Sent ${n} comment${n === 1 ? '' : 's'} — watching for edits`);
      renderProcessingBanner();
    } catch (e) {
      toast('Trigger failed');
    }
  };
  const markUnreadSeen = () => { /* deprecated: badge now reflects pending count */ };

  // ─── Anchor resolution ───
  const resolveAllAnchors = () => {
    state.anchorMap.clear();
    for (const c of state.comments) {
      if (c.parent_id) continue;
      const r = resolveAnchor(c.anchor);
      if (r) state.anchorMap.set(c.id, r);
    }
  };

  const resolveAnchor = (anchor) => {
    if (!anchor) return null;
    if (anchor.type === 'page') return document.body;
    // 1. Selector match
    if (anchor.selector) {
      try {
        const el = document.querySelector(anchor.selector);
        if (el && !root.contains(el)) {
          if (anchor.type === 'element') return el;
          const range = findRangeInElement(el, anchor.selected_text);
          if (range) return range;
          // Try matching via text_before within element
          if (anchor.text_before) {
            const r2 = findRangeInElement(el, anchor.text_before);
            if (r2) return r2;
          }
        }
      } catch (_) { /* invalid selector */ }
    }
    // 2. Fingerprint scan. Range.toString() at capture time does not insert
    //    separators at element boundaries, but the tree-walk at resolve time
    //    picks up whitespace text nodes between elements (which collapse to a
    //    single space). Try several variants to absorb that mismatch.
    const tb = anchor.text_before || '';
    const sel = anchor.selected_text || '';
    const ta = anchor.text_after || '';
    const variants = [
      sel,                          // selected alone — often unique enough
      tb + ' ' + sel + ' ' + ta,    // with spaces at boundaries (handles cross-element captures)
      tb + sel + ta,                // original no-separator concat
    ];
    for (const v of variants) {
      const vc = collapseWs(v);
      if (!vc || vc.length < 4) continue;
      const range = findTextInBody(vc);
      if (range) return range;
    }
    // 3. Loose fingerprint (text_before + text_after, with and without space)
    const looseVariants = [tb + ' ' + ta, tb + ta];
    for (const lv of looseVariants) {
      const lvc = collapseWs(lv);
      if (!lvc || lvc.length < 5) continue;
      const r = findTextInBody(lvc);
      if (r) return r;
    }
    // 4. Orphan
    return null;
  };

  const findRangeInElement = (el, needle) => {
    if (!needle) return null;
    const want = collapseWs(needle);
    if (!want) return null;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let acc = '';
    while (walker.nextNode()) {
      const n = walker.currentNode;
      if (root.contains(n)) continue;
      nodes.push({ node: n, start: acc.length, text: n.textContent });
      acc += n.textContent;
    }
    const collapsed = collapseWs(acc);
    const idx = collapsed.indexOf(want);
    if (idx === -1) return null;
    // Approximate: walk the raw acc to find a matching offset (whitespace-tolerant).
    return findRawRange(nodes, acc, want);
  };

  const findTextInBody = (needle) => {
    return findRangeInElement(document.body, needle);
  };

  const findRawRange = (nodes, rawAcc, wantCollapsed) => {
    // Convert collapsed index to raw range by walking char-by-char.
    // Build a map: rawIndex -> collapsedIndex.
    const wantLen = wantCollapsed.length;
    let collapsedIdx = 0;
    let lastWs = true;
    const map = new Array(rawAcc.length); // raw -> collapsed index (or -1 if eaten)
    for (let i = 0; i < rawAcc.length; i++) {
      const ch = rawAcc[i];
      const isWs = /\s/.test(ch);
      if (isWs) {
        if (lastWs) { map[i] = -1; continue; }
        map[i] = collapsedIdx;
        collapsedIdx++;
        lastWs = true;
      } else {
        map[i] = collapsedIdx;
        collapsedIdx++;
        lastWs = false;
      }
    }
    // Find first raw index whose collapsed position equals target start.
    // Also need collapsed string at that point to equal wantCollapsed.
    const collapsed = (() => {
      let out = '', last = true;
      for (const ch of rawAcc) {
        const ws = /\s/.test(ch);
        if (ws) { if (!last) out += ' '; last = true; }
        else { out += ch; last = false; }
      }
      return out.replace(/^\s+|\s+$/g, '');
    })();
    const matchIdx = collapsed.indexOf(wantCollapsed);
    if (matchIdx === -1) return null;
    // Walk to find raw start & end
    let rawStart = -1, rawEnd = -1;
    for (let i = 0; i < rawAcc.length; i++) {
      if (map[i] === matchIdx) { rawStart = i; break; }
    }
    for (let i = rawAcc.length - 1; i >= 0; i--) {
      if (map[i] === matchIdx + wantLen - 1) { rawEnd = i + 1; break; }
    }
    if (rawStart < 0 || rawEnd <= rawStart) return null;
    // Find nodes containing rawStart/rawEnd
    const locate = (pos) => {
      for (const n of nodes) {
        if (pos >= n.start && pos <= n.start + n.text.length) {
          return { node: n.node, offset: Math.max(0, Math.min(n.text.length, pos - n.start)) };
        }
      }
      return null;
    };
    const a = locate(rawStart);
    const b = locate(rawEnd);
    if (!a || !b) return null;
    const range = document.createRange();
    try {
      range.setStart(a.node, a.offset);
      range.setEnd(b.node, b.offset);
      return range;
    } catch (_) { return null; }
  };

  // ─── Changes: walkthrough overlay ───
  const HISTORY_FLAG_KEY = 'hfb_pending_walkthrough';
  const lastSeenHistoryKey = `hfb_last_seen_history_${PAGE}`;

  const runWalkthrough = async () => {
    try {
      const res = await api('/api/history');
      if (!res.ok) return;
      const data = await res.json();
      const entries = (data.entries || []).filter(e => e.page === PAGE);
      const since = parseInt(localStorage.getItem(lastSeenHistoryKey) || '0', 10);
      const recent = entries.filter(e => Date.parse(e.ts || '') > since);
      const changes = [];
      for (const e of recent) {
        const snippet = (e.after_snippet || '').slice(0, 80);
        if (!snippet.trim()) continue;
        const el = highlightSnippet(snippet, e);
        if (el) changes.push({ el, entry: e });
      }
      state.changes = changes;
      if (changes.length > 0) {
        state.changeIdx = 0;
        showChangePill();
        focusChange(0);
      }
      // Schedule mark-as-seen
      setTimeout(() => {
        localStorage.setItem(lastSeenHistoryKey, String(Date.now()));
      }, 30000);
    } catch (_) { /* ignore */ }
  };

  const BLOCK_TAGS = new Set(['DIV','P','H1','H2','H3','H4','H5','H6','UL','OL','LI','TABLE','TR','TD','TH','THEAD','TBODY','SECTION','ARTICLE','HEADER','FOOTER','NAV','ASIDE','BLOCKQUOTE','PRE','FORM','FIELDSET','HR','FIGURE','FIGCAPTION']);
  const containsBlock = (el) => {
    for (const child of el.querySelectorAll('*')) {
      if (BLOCK_TAGS.has(child.tagName)) return true;
    }
    return false;
  };
  const unwrap = (mark) => {
    const parent = mark.parentNode;
    if (!parent) return;
    while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
    parent.removeChild(mark);
    parent.normalize?.();
  };

  const highlightSnippet = (snippet, entry) => {
    const want = collapseWs(snippet);
    if (!want) return null;
    const tries = [want, want.slice(0, 40), want.slice(0, 24)];
    const wrap = (range) => {
      // surroundContents throws InvalidStateError when the range straddles non-text
      // nodes (i.e. crosses element boundaries). That is the right behavior — we abort
      // cleanly instead of falling back to extractContents, which permanently splits
      // and re-orders the source DOM.
      const mark = document.createElement('mark');
      mark.className = 'hfb-change';
      mark.dataset.historyId = entry.id || '';
      mark.title = entry.summary || '';
      try {
        range.surroundContents(mark);
      } catch (_) {
        return null;
      }
      // Safety net for the rare case surroundContents succeeds on a range whose end
      // happens to align with a block boundary inside its common ancestor.
      if (containsBlock(mark)) {
        unwrap(mark);
        return null;
      }
      const text = mark.textContent || '';
      if (text.trim().length < 4) {
        unwrap(mark);
        return null;
      }
      return mark;
    };
    for (const t of tries) {
      if (t.length < 8) continue;
      const range = findTextInBody(t);
      if (range) {
        const mark = wrap(range);
        if (mark) return mark;
      }
    }
    return null;
  };

  const revertEntry = async (entry) => {
    const ok = await showConfirm({ title: 'Revert this edit?', message: 'The change will roll back. The page will reload.', confirmLabel: 'Revert', danger: true });
    if (!ok) return;
    try {
      const res = await api('/api/revert', { method: 'POST', body: JSON.stringify({ history_id: entry.id }) });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        if (res.status === 409) toast('Revert conflict: ' + (detail.details || []).join('; '));
        else toast('Revert failed');
        return;
      }
      toast('Reverted — reloading');
      setTimeout(() => location.reload(), 500);
    } catch (e) {
      toast('Revert failed');
    }
  };

  const redoEntry = async (entry) => {
    const ok = await showConfirm({ title: 'Re-apply this edit?', message: 'The change will be reapplied. The page will reload.', confirmLabel: 'Redo' });
    if (!ok) return;
    try {
      const res = await api('/api/redo', { method: 'POST', body: JSON.stringify({ history_id: entry.id }) });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        if (res.status === 409) toast('Redo conflict: ' + (detail.details || []).join('; '));
        else toast('Redo failed');
        return;
      }
      toast('Re-applied — reloading');
      setTimeout(() => location.reload(), 500);
    } catch (e) {
      toast('Redo failed');
    }
  };

  const showChangePill = () => {
    changePillLabel.textContent = `${state.changes.length} change${state.changes.length === 1 ? '' : 's'} — j/k to walk`;
    changePill.classList.add('hfb-visible');
  };

  const dismissChanges = () => {
    changePill.classList.remove('hfb-visible');
    localStorage.setItem(lastSeenHistoryKey, String(Date.now()));
  };

  const focusChange = (i) => {
    state.changes.forEach(c => c.el.classList.remove('hfb-change-current'));
    const c = state.changes[i];
    if (!c) return;
    c.el.classList.add('hfb-change-current');
    c.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  };

  const revertCurrentChange = () => {
    const c = state.changes[state.changeIdx];
    if (!c) { toast('No change focused'); return; }
    revertEntry(c.entry);
  };

  const walkChange = (dir) => {
    if (state.changes.length === 0) return;
    state.changeIdx = (state.changeIdx + dir + state.changes.length) % state.changes.length;
    focusChange(state.changeIdx);
  };

  // ─── SSE ───
  const connectSSE = () => {
    if (!('EventSource' in window)) return;
    try {
      const u = new URL('/api/events', location.origin);
      if (TOKEN) u.searchParams.set('t', TOKEN);
      const es = new EventSource(u.toString());
      state.sse = es;
      es.addEventListener('open', () => { state.sseRetry = 0; });
      es.addEventListener('heartbeat', () => { /* keepalive */ });
      es.addEventListener('history', (ev) => {
        try {
          const entry = JSON.parse(ev.data);
          if (entry.page !== PAGE) return;
          // Stash & reload — robust v1 path.
          sessionStorage.setItem(HISTORY_FLAG_KEY, '1');
          location.reload();
        } catch (_) {}
      });
      es.addEventListener('inbox', (ev) => {
        try {
          const c = JSON.parse(ev.data);
          if (c.page !== PAGE) return;
          if (state.comments.find(x => x.id === c.id)) return;
          state.comments.push(c);
          resolveAllAnchors();
          renderSidebar();
          updateBadge();
        } catch (_) {}
      });
      es.addEventListener('error', () => {
        es.close();
        state.sse = null;
        const delay = Math.min(15000, 1000 * Math.pow(2, state.sseRetry));
        state.sseRetry++;
        setTimeout(connectSSE, delay);
      });
    } catch (_) {
      setTimeout(connectSSE, 2000);
    }
  };

  // ─── Keyboard shortcuts ───
  const isTypingTarget = (el) => {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  };

  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented) return;
    const inField = isTypingTarget(e.target);
    // Always handle Esc
    if (e.key === 'Escape') {
      if (state.composer.open) { closeComposer(); return; }
      if (helpOverlay.classList.contains('hfb-visible')) { toggleHelp(false); return; }
      if (state.pickMode) { togglePickMode(false); return; }
      if (changePopover.classList.contains('hfb-visible')) { changePopover.classList.remove('hfb-visible'); return; }
      if (state.sidebarOpen) { toggleSidebar(false); return; }
      return;
    }
    if (inField) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'c') { commentCurrentSelection(); }
    else if (e.key === 'e') { togglePickMode(); }
    else if (e.key === '?' || (e.key === '/' && e.shiftKey)) { e.preventDefault(); toggleHelp(); }
    else if (e.key === 'j') { walkChange(1); }
    else if (e.key === 'k') { walkChange(-1); }
    else if (e.key === 'r') { if (state.changes.length > 0) revertCurrentChange(); }
  });

  // ─── Init ───
  const init = () => {
    loadInbox();
    connectSSE();
    // Post-reload walkthrough if flagged, OR always check for newer history.
    if (sessionStorage.getItem(HISTORY_FLAG_KEY)) {
      sessionStorage.removeItem(HISTORY_FLAG_KEY);
      // Slight delay to let DOM settle.
      setTimeout(runWalkthrough, 100);
    } else {
      setTimeout(runWalkthrough, 300);
    }
  };

  // ─── Public surface ───
  window.__hfb = {
    token: TOKEN,
    page: PAGE,
    state,
    open: () => toggleSidebar(true),
    close: () => toggleSidebar(false),
    pick: () => togglePickMode(true),
    reload: loadInbox,
    version: '1.0.0',
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
