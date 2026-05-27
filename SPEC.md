# html-feedback — Internal Spec (v1)

This document is the contract between the server, client, scripts, and skill docs.
Every component must conform exactly. If you (a subagent) need to deviate, note it in a `// SPEC-NOTE:` comment in your file and proceed with the closest reasonable interpretation — do not invent new endpoints or schema fields silently.

---

## 1. Project layout (final)

```
html-feedback/
├── SKILL.md
├── README.md
├── SPEC.md                  (this file — kept in the repo for contributors)
├── LICENSE                  (MIT)
├── .gitignore
├── lib/
│   ├── feedback.js
│   ├── feedback.css
│   └── server.py
└── scripts/
    ├── inject.py
    └── start.py
```

Working tree on the user's machine when active:

```
<target-dir>/
├── *.html                   (the user's pages, with auto-injected lib tags)
└── feedback/
    ├── inbox.jsonl          (comments — append-only)
    ├── history.jsonl        (edits Claude made — append-only)
    ├── session.json         (live URL, token, PID — overwritten each start)
    └── .snapshots/
        └── <page>/<iso-ts>.html   (pre-edit snapshots for revert)
```

`feedback/` MUST be appended to `.gitignore` on first run.

---

## 2. Server

### Binding & auth
- Bind **127.0.0.1 only**. Never 0.0.0.0. Bind to port 0 and read the assigned port back.
- Generate a 32-char URL-safe token at startup (`secrets.token_urlsafe(24)`).
- Every request MUST carry the token via `?t=<token>` OR `X-Feedback-Token` header.
- Reject (403) any request without a valid token. The only exception is `GET /healthz` (returns `ok`, no token needed, used by `start.py`).
- Write `feedback/session.json` at startup with `{url, token, port, pid, started_at}`. Overwrite atomically.

### Stdlib only
Use `http.server`, `socketserver`, `threading`, `json`, `secrets`, `pathlib`, `time`, `os`, `signal`, `sys`, `argparse`, `urllib.parse`, `mimetypes`, `re`, `uuid`. **No third-party deps.**

### CLI
```
python lib/server.py --dir <target-dir> [--idle-timeout 600] [--parent-pid <pid>]
```
- `--dir` required. Resolved to absolute path. Server only serves files under this path.
- `--idle-timeout` default 600s. Last activity timestamp is bumped on every authenticated request and every SSE heartbeat-ack.
- `--parent-pid` optional. If set, server self-terminates when that PID dies (poll every 5s).
- On startup, print to stdout exactly one line: `READY <url>` where url is `http://127.0.0.1:<port>/?t=<token>`. This is the contract `start.py` reads.

### Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Returns `ok`. No auth. |
| GET | `/` | HTML index listing `*.html` files in target dir, each linked with token query. |
| GET | `/<filename>.html` | Serve the page. If lib tags are absent, inject them on-the-fly (do NOT mutate the source file — that's `inject.py`'s job). |
| GET | `/lib/feedback.js` | Static. |
| GET | `/lib/feedback.css` | Static. |
| GET | `/api/inbox` | Returns `{comments: [...]}` — full current inbox state with `status` reflecting whether `history.jsonl` has any entry referencing that `comment_id` (then `status:"addressed"`). |
| GET | `/api/history` | Returns `{entries: [...]}` from `history.jsonl`. |
| POST | `/api/feedback` | Append a comment. Body = comment JSON (see §3). Returns `{id, status:"open"}`. Validates schema; rejects unknown fields. |
| POST | `/api/revert` | Body `{history_id}`. Copies the matching `.snapshots/<page>/<ts>.html` back over the page file. Writes a new history entry with `kind:"revert"`. Returns `{ok:true, page}`. |
| GET | `/api/events` | **Server-Sent Events** stream. See below. |
| GET | `/api/session` | Returns the contents of `session.json` (useful for client to learn its own token from URL on first load, but token must already be in URL to reach this). |
| GET | `/api/file?path=<rel>` | Returns raw HTML source of a page (for client-side diff display). Path must be under target dir. |

All JSON responses are `Content-Type: application/json; charset=utf-8`. All POST bodies are JSON.

### SSE (`/api/events`)
- `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
- Events:
  - `event: history\ndata: {entry}` — emitted within 200ms of a new line appended to `history.jsonl`.
  - `event: inbox\ndata: {comment}` — emitted when a new comment is POSTed (so other open tabs see it).
  - `event: heartbeat\ndata: {"ts": "..."}\n\n` — every 15s.
- Server tracks `history.jsonl` and `inbox.jsonl` size; on growth, reads the new lines and dispatches.
- Use a polling watcher in a background thread (200ms) — no `watchdog` dep.

### Edit detection
- The server does NOT itself edit pages. Claude (the agent) edits HTML files directly via the Edit/Write tools.
- Before each edit, the agent is expected to write a snapshot. To make this safe even if the agent forgets, the server SHOULD also watch `*.html` files in the target dir (200ms polling, mtime-based) and, on detecting a change without a prior matching history entry, append a synthetic history entry `{kind:"external-edit", page, ts, comment_id:null, summary:"page changed outside the comment flow"}` so the client still reloads.

### Snapshot helper
Provide a `POST /api/snapshot` route that takes `{page}`, copies the current file to `.snapshots/<page>/<iso-ts>.html`, and returns `{snapshot_path}`. This is what Claude calls before editing.

### Shutdown
- SIGINT / SIGTERM: graceful close, delete `session.json`.
- Idle timeout: same.
- Parent PID died: same.

### Logging
- Log to stderr only, one line per request: `<ts> <method> <path> <status>`.
- Never log token values.

---

## 3. Schemas

### Comment (`inbox.jsonl`, one JSON per line)

```json
{
  "id": "c-<uuid4-hex-12>",
  "parent_id": null,
  "page": "report.html",
  "anchor": {
    "type": "text",
    "selector": "main > section:nth-of-type(2) > p:nth-of-type(1)",
    "selected_text": "the highlighted phrase",
    "text_before": "...up to 40 chars before...",
    "text_after": "...up to 40 chars after...",
    "rect": {"x": 120, "y": 340, "w": 220, "h": 18}
  },
  "comment": "user message",
  "ts": "2026-05-27T12:34:56Z",
  "author": "local"
}
```

- `anchor.type` ∈ `"text" | "element" | "page"`.
- For `"element"`: `selected_text` is empty; `text_before`/`text_after` are the first 40 chars of element's text content and the first 40 chars after the element in document order; `rect` is the bounding rect at capture time.
- For `"page"`: `selector` is `""`, `text_before`/`text_after`/`selected_text` empty, `rect` null.
- `parent_id` references another comment's `id` for thread replies.
- `id` format: `c-` followed by `uuid.uuid4().hex[:12]`.
- `ts` is UTC ISO-8601 with seconds precision, suffix `Z`.

Validation: server rejects unknown top-level fields and unknown `anchor.type` values.

### History entry (`history.jsonl`, one JSON per line)

```json
{
  "id": "h-<uuid4-hex-12>",
  "ts": "2026-05-27T12:35:10Z",
  "page": "report.html",
  "comment_id": "c-...",
  "kind": "edit",
  "summary": "Tightened the intro paragraph and removed the redundant subtitle.",
  "before_snippet": "first ~200 chars of removed/changed text",
  "after_snippet":  "first ~200 chars of replacement text",
  "snapshot_path": "feedback/.snapshots/report.html/2026-05-27T12-35-08Z.html"
}
```

- `kind` ∈ `"edit" | "revert" | "external-edit"`.
- `comment_id` may be `null` for `external-edit` and for the revert action itself.
- `snapshot_path` is relative to the target dir; for `revert` entries, it's the `snapshot_path` of the edit being reverted (used by the addressed-status rule).
  May be `""` (empty string) for entries with no associated snapshot (e.g. `external-edit`, or a failure-mode entry where Claude couldn't locate the anchor).
- `snapshot_after_path` (optional, new in v1.1): the snapshot Claude takes *after* applying the edit. When present, the revert route does a **patch-style undo** — it computes the diff between `snapshot_path` (before) and `snapshot_after_path` (after) and reverse-applies it to the current file, so reverting one edit no longer wipes out unrelated later edits. Entries without `snapshot_after_path` fall back to legacy full-snapshot restore.
- **Addressed-status rule**: a comment is `status:"addressed"` if and only if the most recent `kind:"edit"` entry referencing its `id` has *not* been undone by a later `kind:"revert"` entry whose `snapshot_path` matches that edit's `snapshot_path`. After a revert, the comment returns to `"open"` so the user (and Claude) can iterate again.

### Session (`session.json`)

```json
{
  "url": "http://127.0.0.1:54321/?t=...",
  "token": "...",
  "port": 54321,
  "pid": 12345,
  "started_at": "2026-05-27T12:00:00Z",
  "target_dir": "C:\\path\\to\\pages"
}
```

---

## 4. Client (feedback.js + feedback.css)

### Initialization
- The script must be safe to include via `<script defer src="/lib/feedback.js?t=TOKEN"></script>`.
- It reads its own token from its `<script>` `src` query param (`?t=...`). All client→server requests append it as `?t=`.
- It boots after `DOMContentLoaded`.
- It must not pollute global namespace beyond a single `window.__hfb` object.

### UI elements (added to body)
- A **floating action button** (bottom-right): toggles the sidebar. Shows unread count.
- A **sidebar drawer** (right side, ~340px): threaded list of comments for this page, plus orphans.
- A **selection toolbar** that appears next to any text selection inside the body, with a "💬 Comment" button.
- An **element-pick mode**: activated via keyboard `e` or sidebar button — outlines elements on hover, click to comment.
- A **page-comment** button in the sidebar header — leaves a comment tied to the page itself.
- A **change-walkthrough overlay**: when new history entries arrive for this page, briefly highlight changed regions and show "1 of N changes ◀ ▶".
- An **orphan tray** in the sidebar: comments whose anchors no longer resolve.

### Keyboard shortcuts
- `c` while text is selected: open comment composer.
- `e`: toggle element-pick mode.
- `?`: open shortcut help.
- `Esc`: cancel current action.
- `j` / `k`: walk through changes after a reload.

### Anchor resolution (on load, for each comment)
Run in order, accept first that succeeds:
1. **Selector match**: query the stored CSS selector. If element exists and its text contains `selected_text` (for text-type) or its `text_before` matches, locate the exact text range and use it.
2. **Fingerprint scan**: get `document.body.innerText`, search for `text_before + selected_text + text_after`. If found, walk the DOM to convert the text offset to a Range. (Tolerate ±5% whitespace variation by collapsing runs of whitespace before comparing.)
3. **Loose fingerprint**: try `text_before + text_after` alone. If unique, highlight the seam.
4. **Orphan**: stash in the orphan tray with the original `selected_text` displayed.

For `element`-type: same but use only `text_before` and tag name in selector.
For `page`-type: always resolves.

### Submitting a comment
- Composer is a small popover with a `<textarea>`, a "Reply to" badge if it's a thread reply, and a Send button.
- On send: POST to `/api/feedback` with the full comment JSON. The client computes `id`, `ts`, `selector`, and `text_before/after` itself. Server is the authority on persistence — client treats the 200 response as confirmation.
- Optimistic UI: insert into sidebar immediately, mark "sending"; on 200 mark "sent"; on error mark "failed" with retry.

### SSE handling
- Open `/api/events` on load. On any disconnect, exponential backoff reconnect (1s, 2s, 4s, capped 15s).
- On `event: history` for current page:
  - Re-fetch the page source via `/api/file` and the rendered DOM is reloaded by setting `location.reload()` ONLY after staging a walkthrough flag in `sessionStorage` so the overlay re-shows post-reload.
  - Simpler alternative for v1: just `location.reload()` and let the post-reload init read the latest N history entries and highlight any regions matching their `after_snippet` text. Pick this — it's robust.
- On `event: inbox` for current page from another tab: insert into sidebar without reloading.

### Walkthrough overlay (post-reload)
- Read history entries from `/api/history`, filter to current page, find any whose `ts` is newer than the `last_seen_history_ts` stored in `localStorage`.
- For each, search the page for `after_snippet[:80]` (fallback: any unique 40-char prefix). Wrap matched range in a `<mark class="hfb-change">`, attach a tooltip with `summary`.
- Show a floating "N changes — j/k to walk through" pill bottom-center.
- Each highlight has a "↶ Revert this edit" button that POSTs to `/api/revert`.
- Update `last_seen_history_ts` after the user closes the walkthrough or after 30s.

### Threading
- Sidebar groups by root anchor: parent comment, then indented replies in chronological order.
- Once Claude addresses a comment (server marks `status:"addressed"`), show a small ✓ badge but keep the thread expandable. User can hit "Reply" to push back — that posts a new comment with `parent_id` set.

### Orphan handling
- Orphan tray shows: original `selected_text` (or element text) + original comment + a "Re-pin" button.
- Re-pin enters a special selection mode: next text-selection or element click re-anchors the comment. Client POSTs a new comment with `parent_id` set to the orphan's id and the new anchor, then archives the orphan.

### Visuals (feedback.css)
- All classes prefixed `hfb-`. Use CSS custom properties for theming.
- Sidebar slides from right, ~340px wide, soft shadow.
- Selection toolbar is a small pill with one button.
- Change highlights use `background: rgba(255, 220, 100, .55)` with a 1px dashed border, fading after 8s to a subtle outline so they stay visible but don't dominate.
- Respects `prefers-color-scheme: dark`.

### Non-goals (v1)
- No screenshots, no SPA hooks, no real-time collaboration cursors, no auth beyond local token.

---

## 5. Scripts

### `scripts/inject.py`
```
python scripts/inject.py inject --dir <target-dir>
python scripts/inject.py remove --dir <target-dir>
python scripts/inject.py status --dir <target-dir>
```

- `inject`: for each `*.html` file under `--dir` (recursive), add these tags inside `<head>` (or before `</body>` if no `<head>`), idempotently:
  ```html
  <!-- hfb:begin -->
  <link rel="stylesheet" href="/lib/feedback.css">
  <script defer src="/lib/feedback.js"></script>
  <!-- hfb:end -->
  ```
- Idempotency: detect existing `<!-- hfb:begin -->...<!-- hfb:end -->` block and replace it. If present and identical, no-op.
- `remove`: strip the `hfb:begin/end` block.
- `status`: print a table of pages and whether injected.
- Tokens are appended by the server at request-time when it rewrites/serves the page — the injected source files don't store them.
- Preserve original file encoding (default utf-8). Preserve trailing newline.

### `scripts/start.py`
```
python scripts/start.py --dir <target-dir> [--no-open] [--no-inject]
```

- Resolve `--dir` to absolute path. Create `<dir>/feedback/` and `.snapshots/` if missing.
- Append `feedback/` to `<dir>/.gitignore` if there's a `.gitignore` and the entry is missing. If there is no `.gitignore`, do nothing (don't presume git).
- Unless `--no-inject`, run `inject.py inject --dir <dir>`.
- Spawn `python lib/server.py --dir <dir> --parent-pid <self-pid>` as a subprocess with stdout pipe.
- Read lines from server stdout until one begins with `READY `; capture the URL. If the server exits before READY, print its stderr and exit non-zero.
- Unless `--no-open`, open the URL in the default browser (`webbrowser.open`).
- Then stream the server's stderr through.
- On Ctrl-C: send SIGTERM/terminate to the server subprocess and exit cleanly.

Cross-platform: works on Windows (no `os.fork`, no `os.setpgrp`). Use `subprocess.Popen` with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` on Windows so Ctrl-C handling is clean.

### `.gitignore` (in the html-feedback repo itself)
```
__pycache__/
*.pyc
.DS_Store
.idea/
.vscode/
```

---

## 6. SKILL.md & README.md

### SKILL.md
- Frontmatter declaring the skill name `html-feedback`, description, activation phrases ("make these pages reviewable", "start html feedback on <dir>", "open html feedback", `/html-feedback`).
- Workflow Claude follows:
  1. Identify the target directory (ask if ambiguous; default to CWD if it has `*.html` files).
  2. Run `python scripts/start.py --dir <dir>`.
  3. Read the printed URL and share it with the user.
  4. Poll `feedback/inbox.jsonl` for new comments (read the tail, track which IDs have history entries already).
  5. For each new comment: read `feedback/session.json` to learn the server URL + token. Locate the anchor in the source HTML (use the selector first, then text_before+selected+text_after), then:
     a. `POST /api/snapshot` with `{"page": "<page>.html"}` — capture the returned path as `snapshot_path` (the **before** snapshot).
     b. Edit the file with the Edit tool.
     c. `POST /api/snapshot` again with the same page — capture as `snapshot_after_path` (the **after** snapshot). This pair is what enables true per-edit revert.
     d. Append a history entry to `feedback/history.jsonl` (open with `"a"`, write one `json.dumps(entry, ensure_ascii=False) + "\n"`, fsync) including BOTH `snapshot_path` and `snapshot_after_path`. There is no `POST /api/history` route — direct append is the only path.
  6. For thread replies (`parent_id` set), only act if the reply asks for further change. Treat acknowledgements ("thanks", "ok", "looks good", "perfect", "👍", short approvals) as no-ops — do not write any history entry.
  7. If the comment's anchor cannot be located in the source HTML at all, do not edit. Append a history entry with `kind:"edit"`, `snapshot_path:""`, `before_snippet:""`, `after_snippet:""`, and a `summary` like "couldn't locate anchor in source; please re-pin via the orphan tray". This marks the comment addressed so it doesn't loop.
- Explicit guidance: don't edit pages that have no corresponding open comment.
- How to stop: hit Ctrl-C in the terminal running `start.py`, or kill its PID from `session.json`.

### README.md
- 30-second "what is this" intro.
- Install: clone, ensure Python 3.10+, no other deps.
- Usage: `/html-feedback` in Claude Code, or manually `python scripts/start.py --dir ./pages`.
- Feature list.
- File-layout explanation (target-dir side).
- Security note: localhost-only, token-gated.
- Troubleshooting: port in use, browser didn't open, comments not showing.

---

## 7. Cross-cutting rules

- Python 3.10+ syntax. No f-string `=` debug syntax in user-facing output. Type hints encouraged but not required.
- All file writes are UTF-8.
- All timestamps in UTC, ISO-8601, `Z` suffix.
- All paths are resolved with `pathlib.Path` and bounded under target-dir before any file I/O (path-traversal defense — reject anything that resolves outside).
- All JSON I/O uses `json` stdlib with `ensure_ascii=False`.
- Atomic writes (where needed): write to `*.tmp`, `os.replace` to final.
- Logging: stderr, no token leakage, one line per event.
- Tests: no test framework required for v1; the integration smoke test is manual via `start.py`.

---

## 8. Acceptance smoke test (what integration verifies)

1. Run `python scripts/start.py --dir <demo>` where `<demo>` has 2 HTML files.
2. Browser opens to a page with the sidebar visible.
3. Highlight a sentence, write a comment, click Send.
4. `feedback/inbox.jsonl` has 1 line; schema matches §3.
5. Append a history entry to `feedback/history.jsonl` manually with a `summary` and `after_snippet` that matches text the page already contains.
6. Within ~1s, the page reloads and shows the change highlighted with the summary as tooltip.
7. Click "↶ Revert" — the snapshot is restored (after snapshotting in step 5 manually), page reloads back.
8. Ctrl-C in terminal — server exits, `session.json` is removed.

If all 8 pass, v1 is shipped.
