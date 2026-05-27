---
name: html-feedback
description: Turn a folder of static HTML pages into a collaborative review surface. Users highlight text or click elements, leave comments, click ▶ Process to batch them, and Claude edits the pages in response with auto-reload, patch-style per-edit revert, and redo.
triggers:
  - "make these pages reviewable"
  - "start html feedback"
  - "review these pages"
  - "open html feedback on <dir>"
  - "/html-feedback"
---

# html-feedback

Activate this skill when a user has a folder of `*.html` files and wants an iterative review-and-edit loop: they comment on the rendered pages in a browser, click ▶ Process when they're ready, and Claude edits the source HTML in response. The whole interaction lives in the browser — the user should not need to return to the CLI between comments.

## When to activate

- The user explicitly invokes `/html-feedback` or uses one of the trigger phrases.
- The user describes a workflow involving reviewing, annotating, or commenting on static HTML pages they want Claude to update.
- The user points at a directory of `*.html` files and asks for a feedback loop.

Do not activate for general HTML editing, single-file edits, or live web apps. This skill is specifically for a folder of static pages with a comment-driven edit cycle.

## Workflow Claude follows

### 1. Identify the target directory

If the current working directory contains `*.html` files, use it as the default. Otherwise ask the user for a path. If the directory contains hundreds of HTML files, confirm with the user before starting — the inject step touches every page.

### 2. Start the server

Run `python scripts/start.py --dir <target>` with the resolved absolute path. The script prints exactly one line beginning with `READY ` followed by a URL of the form `http://127.0.0.1:<port>/?t=<token>`. Share that URL with the user verbatim. The browser is opened automatically unless the user passed `--no-open`.

### 3. Arm the trigger watcher

The user **does not poll** — they click the **▶ Process (N)** button in the sidebar to send batches. That writes a `{"type":"process","pending_ids":[...]}` line to `<target>/feedback/control.jsonl`. The recommended way to receive triggers is the Monitor tool:

```
python scripts/watch_control.py
  --control <target>/feedback/control.jsonl
  --inbox   <target>/feedback/inbox.jsonl
  --history <target>/feedback/history.jsonl
```

It emits one `PROCESS N comments: …` line per click. Run it via Monitor with `persistent: true` so it lives for the session. Each emitted line wakes Claude with the full batch of pending comment IDs and a snippet of each comment's body.

### 4. Address each unaddressed comment in the batch

For every comment in the `pending_ids` of the trigger event:

a. **Read the comment** from `<target>/feedback/inbox.jsonl`. Skip any line where `_op == "delete"` (soft-delete tombstone) — those are not comments. Also skip any comment whose `id` appears in a `_op:"delete"` entry.

b. **Locate the anchor in the source HTML.** Read the page file. Use `anchor.selected_text` together with `anchor.text_before` and `anchor.text_after` to find the exact span. If those don't match (whitespace drift, edits since the comment was made), fall back to `anchor.selector`. For `anchor.type == "element"`, use `text_before` plus tag name from the selector. For `anchor.type == "page"`, treat the whole document as the target.

c. **Snapshot before editing.** Read `<target>/feedback/session.json` to get the server `url` and `token`. POST to `<base>/api/snapshot?t=<token>` with body `{"page": "<page>.html"}`. The response is `{"snapshot_path": "feedback/.snapshots/<page>/<iso-ts>.html"}`. Keep that path as **`snapshot_path`** — the *before* snapshot.

d. **Edit the file** using the Edit tool. Make the smallest change that satisfies the comment.

e. **Snapshot after editing.** POST to `<base>/api/snapshot?t=<token>` again with the same `{"page": "<page>.html"}` body. Keep the returned path as **`snapshot_after_path`** — the *after* snapshot. The pair is what powers patch-style revert and redo.

f. **Append a history entry.** Open `<target>/feedback/history.jsonl` in append mode (`"a"`) and write exactly one line:

```json
{"id":"h-<uuid4-hex-12>","ts":"<UTC-ISO-8601-Z>","page":"<page>.html","comment_id":"<c-...>","kind":"edit","summary":"<one-sentence what changed>","before_snippet":"<~200 chars of the visible text where the change occurred>","after_snippet":"<~200 chars of the visible text after the change>","snapshot_path":"<from step c>","snapshot_after_path":"<from step e>"}
```

Use `json.dumps(entry, ensure_ascii=False)` and write with a trailing `\n`. Append-only; never rewrite the file.

**`after_snippet` must be visible text from the rendered page**, not HTML markup. The browser walkthrough searches for it in `document.body.innerText` to draw the yellow highlight, so any HTML tags or escape sequences will silently fail to match.

### 5. Thread replies

If a comment has `parent_id` set, it's a reply to an earlier comment. Only act on it if it asks for a further change ("also make it bold", "no, keep the old wording", "shorter please"). Treat acknowledgements ("thanks", "looks good", "ok", "perfect", short approvals) as no-ops — do not produce a history entry for them.

### 6. Stay scoped

Never edit a page that has no matching open comment. Do not preemptively improve pages. Do not edit anything outside the target directory.

### 7. Do not auto-process

Comments arriving in `inbox.jsonl` do **not** trigger work on their own. Wait for the user to click ▶ Process. The watcher only emits when a "process" line is written to `control.jsonl`.

### 8. Stopping & cleaning up

When the user asks to **stop**, **clean up**, **end session**, **remove html-feedback**, or anything similar:

1. Read `<target>/feedback/session.json` to get `url` and `token`.
2. POST to `<base>/api/shutdown?t=<token>` with body `{}` for a *keep-history* shutdown, or `{"purge_feedback": true}` to also delete `feedback/` (comments, history, snapshots).
3. The server schedules a graceful exit. The wrapping `start.py` detects the subprocess exit and **automatically removes the `<!-- hfb:begin --> ... <!-- hfb:end -->` injection tags from every HTML file** in the target directory.
4. Tell the user what was cleaned: injection tags always; `feedback/` only if they asked.

If the user hits Ctrl-C in the terminal directly, the same auto-cleanup runs — they don't need to call you. If they passed `--keep-injected` to `start.py`, the injection tags stay.

Phrases that trigger this workflow:
- "stop html feedback"
- "clean up"
- "end the session"
- "remove the injection tags"
- "uninstall"
- "we're done"

## Server API reference (used by Claude + UI)

| Method | Path | Used by | Purpose |
|---|---|---|---|
| POST | `/api/snapshot` | Claude | Copy current page to `.snapshots/<page>/<ts>.html`, return relative path. Call **before AND after** every edit. |
| POST | `/api/feedback` | UI | Append a new comment to `inbox.jsonl`. Claude should never call this. |
| POST | `/api/feedback/delete` | UI | Soft-delete a comment (and its replies) by appending a tombstone. |
| POST | `/api/feedback/clear-addressed` | UI | Soft-delete every addressed comment on the page in one go. |
| POST | `/api/process` | UI | Append a `{"type":"process","pending_ids":[...]}` line to `control.jsonl`. The Monitor watcher forwards this to Claude. |
| POST | `/api/revert` | UI | Patch-style undo of a specific edit. Server reverse-applies the diff between `snapshot_path` and `snapshot_after_path`. |
| POST | `/api/redo` | UI | Re-apply a previously reverted edit by forward-applying the same diff. |
| GET | `/api/inbox` | UI | Returns the inbox with `status` computed (`open` or `addressed`). Filters out tombstones. |
| GET | `/api/history` | UI | Returns all history entries. |
| GET | `/api/events` | UI | SSE stream — emits `history`, `inbox`, `heartbeat` events. |
| POST | `/api/shutdown` | UI / Claude | Trigger graceful server exit. Body: `{}` (keep history) or `{"purge_feedback": true}` (also delete `feedback/`). `start.py` always strips injection tags from HTML files on subprocess exit. |

Claude only needs `POST /api/snapshot`. Everything else is the UI's job.

## Schemas

The canonical definitions live in [SPEC.md §3](./SPEC.md#3-schemas). Reproduced for quick reference:

### Comment (`inbox.jsonl`)

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

- `anchor.type` is one of `"text"`, `"element"`, `"page"`.
- For `"element"`: `selected_text` is empty; `text_before`/`text_after` are the first 40 chars of element text and the first 40 chars after the element in document order.
- For `"page"`: `selector` is `""`; text fields are empty; `rect` is `null`.
- `parent_id` references another comment's `id` for thread replies.

### Delete tombstone (`inbox.jsonl`)

```json
{ "_op": "delete", "id": "c-<id-being-deleted>", "ts": "<utc-iso>" }
```

When iterating `inbox.jsonl`, treat these as instructions to drop the referenced comment from the active set. Never process a deleted comment.

### History entry (`history.jsonl`)

```json
{
  "id": "h-<uuid4-hex-12>",
  "ts": "2026-05-27T12:35:10Z",
  "page": "report.html",
  "comment_id": "c-...",
  "kind": "edit",
  "summary": "Tightened the intro paragraph and removed the redundant subtitle.",
  "before_snippet": "first ~200 chars of visible text being changed",
  "after_snippet": "first ~200 chars of visible text after the change",
  "snapshot_path": "feedback/.snapshots/report.html/2026-05-27T12-35-08Z.html",
  "snapshot_after_path": "feedback/.snapshots/report.html/2026-05-27T12-35-12Z.html"
}
```

- `kind` is one of `"edit"`, `"revert"`, `"external-edit"`.
- `comment_id` may be `null` for `external-edit` entries and for revert actions.
- `snapshot_path` is the BEFORE snapshot; `snapshot_after_path` is the AFTER snapshot (added in v1.1 for patch-style revert/redo). Both are relative to the target directory.

### Addressed-status rule

A comment is `status:"addressed"` if and only if the most recent `kind:"edit"` entry referencing its `id` has *not* been undone by a later `kind:"revert"` entry whose `snapshot_path` matches that edit's `snapshot_path`. After a revert, the comment returns to `"open"`. After a redo, a *new* edit entry is written and the comment returns to `"addressed"`.

## Conventions

- **Timestamps** are UTC, ISO-8601, second precision, with a `Z` suffix. Example: `2026-05-27T12:35:10Z`.
- **IDs** are 12 lowercase hex chars from `uuid.uuid4().hex[:12]`, prefixed `c-` for comments and `h-` for history entries.
- **JSONL files are append-only.** Open with mode `"a"`, write `json.dumps(obj, ensure_ascii=False) + "\n"`, close. Never rewrite, never edit existing lines.
- **All file writes are UTF-8.**
- **Paths** are resolved through `pathlib.Path` and must stay inside the target directory.

## Failure modes & recovery

**Anchor cannot be located in the source HTML.** This happens when the page was edited externally between comment creation and Claude's read, or when the selected text was inside content that has since been removed. Do not guess. Append a history entry with `kind:"edit"`, `comment_id` set, `summary` like `"couldn't locate anchor; please re-pin via the orphan tray"`, leave `before_snippet`/`after_snippet` empty strings, and set `snapshot_path` to `""` (no `snapshot_after_path`). This marks the comment as addressed so it stops appearing in the unaddressed queue; the user can re-pin from the orphan tray.

**User reverts an edit.** A revert produces its own history line with `kind:"revert"` and `snapshot_path` matching the reverted edit's. The comment's status flips back to `"open"`. **Do not** re-apply the edit on the next batch — wait for the user to either click ↺ Redo (which the UI handles automatically) or post a new comment.

**External edit detected.** The server may insert `kind:"external-edit"` entries when it sees a page change outside the comment flow. Ignore these for the purposes of comment tracking — they exist only so the client reloads.

**Server not responding to `/api/snapshot`.** Check that `session.json` still exists and that the URL is reachable. If the server has exited, ask the user to restart `start.py`. Do not edit pages without first snapshotting — both revert and redo depend on it.

**Reply with no actionable content.** Skip it. Do not write a history entry. The comment remains "unaddressed" in the strict sense but has no open work; this is fine.

**Style-only change with no visible-text delta.** If the edit changes only an attribute (color, font-weight) and no visible text, set `after_snippet` to the text that surrounds the change anyway — the walkthrough just needs *something* findable to anchor the yellow highlight. Pick a phrase that uniquely identifies the location.
