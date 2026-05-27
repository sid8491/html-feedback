# html-feedback

Turn a folder of static HTML pages into a collaborative review surface. Open your pages in a browser, highlight text or click elements to leave comments, and Claude edits the source HTML in response — with batch processing, per-edit revert/redo, a walkthrough of every change, and an "all live in the browser" experience.

## Demo

> TODO: screenshot here

## Features

- **Inline comments on rendered pages.** Highlight any text span to attach a comment. Press `e` to pick an element instead. Comment on the whole page from the sidebar header.
- **Batch processing.** Comments accumulate quietly. Click the **▶ Process (N)** button when you're ready to send the whole batch to Claude in one shot. No need to return to the CLI.
- **Live processing indicator.** A pill at the top of the page shows `Processing N/M…` with a spinner while Claude works, then `✓ All N processed` when done. Survives the auto-reloads that happen between edits.
- **WhatsApp-style unread bubble.** The 💬 floating button shows a red badge with the count of open (unprocessed) comments.
- **Newest-first sidebar.** Comments are sorted by timestamp descending, so the latest is always on top.
- **Delete comments individually** with a small ✕ on each card, or wipe everything addressed at once with the **clear addressed (N)** link in the section header. Both use soft-delete (tombstones in `inbox.jsonl`), preserving append-only semantics.
- **Threaded replies.** Push back on Claude's edit by replying in the same thread. Replies that ask for further changes get picked up on the next pass; acknowledgements like "thanks" are no-ops.
- **Auto-reload with change walkthrough.** When Claude edits a page, the browser reloads and highlights every changed region. Step through with `j`/`k`. Each highlight carries Claude's one-line summary as a tooltip.
- **Patch-style per-edit revert.** Every edit takes a **before AND after snapshot**. Reverting one edit reverse-applies just that edit's diff — *without* clobbering unrelated later edits. A small **↶ Revert** link sits next to each addressed comment in the sidebar; press `r` while a change is focused in the walkthrough.
- **Redo.** After you revert, the link flips to **↺ Redo** so you can re-apply that exact change with one click.
- **Orphan re-pinning.** When a comment's anchor no longer resolves (the text it pointed at is gone), it lands in an orphan tray. Click "Re-pin", then select new text or an element to re-anchor it.
- **Polished modals & popups.** Native browser `confirm()` is replaced with a custom styled modal (Enter to confirm, Esc to cancel, backdrop click cancels).
- **Keyboard shortcuts** for everything you'd otherwise reach for the mouse for.
- **Local-only, token-gated.** The server binds to 127.0.0.1, generates a random per-session token, and refuses any request without it. Idle-shutdown is on by default.

## Quick start

```sh
git clone <this repo>
cd html-feedback
python scripts/start.py --dir /path/to/your/html
```

The script injects the lib tags into your pages (idempotently), spawns the server, and opens your browser to the first page.

From inside Claude Code:

```
/html-feedback
```

or in natural language:

> start html feedback on ./pages

Claude will run `start.py`, share the URL, and start watching `feedback/control.jsonl` for your process triggers.

## Typical workflow

1. **You** open the URL in your browser.
2. **You** highlight some text, click 💬 Comment, type a request. Repeat for as many comments as you want — they sit in the sidebar with a red `Process (N)` badge.
3. **You** click **▶ Process (N)** when you're ready.
4. **Claude** picks up the batch, edits the source HTML, snapshots before+after each edit, and appends to `history.jsonl`.
5. **Your browser** auto-reloads with the changes wrapped in yellow highlights. The processing pill counts down in real time.
6. **You** revert any edit you don't like (the ↶ Revert link in the sidebar), or redo it later (↺ Redo).
7. Repeat from step 2 — or hit **clear addressed (N)** to wipe the processed comments and start clean.

## Requirements

- Python 3.10 or newer. No third-party dependencies — the server is stdlib only.
- A modern Chromium- or Firefox-based browser.
- For the Claude Code path: a working Claude Code install with this repo's `SKILL.md` discoverable as a skill.

## How it works

```
+--------------------+        HTTP + SSE         +-------------------+
|  Browser           |  <---------------------->  |  Python server   |
|  feedback.js/.css  |     127.0.0.1:<port>      |  lib/server.py   |
+--------------------+      ?t=<token>            +---------+--------+
                                                            |
                                                            | reads/writes
                                                            v
                                                  +-------------------+
                                                  |  feedback/        |
                                                  |   inbox.jsonl     |  (your comments + tombstones)
                                                  |   control.jsonl   |  (process triggers from UI)
                                                  |   history.jsonl   |  (Claude's edits + reverts)
                                                  |   session.json    |  (live URL/token/PID)
                                                  |   .snapshots/     |  (before+after per edit)
                                                  +---------+---------+
                                                            |
                                                            | tails control.jsonl
                                                            v
                                                  +-------------------+
                                                  |  Claude (Edit)    |
                                                  +-------------------+
```

Only the browser writes to `inbox.jsonl` and `control.jsonl` (via the server). Only Claude writes to `history.jsonl` and edits `*.html`. The server watches both JSONL files and pushes Server-Sent Events to the browser so it can reload the page and run the change walkthrough.

## File layout (target directory)

```
<target-dir>/
├── *.html                   the user's pages, with auto-injected lib tags
└── feedback/
    ├── inbox.jsonl          comments + delete tombstones — append-only
    ├── control.jsonl        process triggers from the UI — append-only
    ├── history.jsonl        edits + reverts — append-only
    ├── session.json         live URL/token/PID — overwritten each start
    └── .snapshots/
        └── <page>/<iso-ts>.html   pre/post-edit snapshots for revert+redo
```

`feedback/` is added to your `.gitignore` on first run (only if a `.gitignore` already exists — we don't presume git).

## Keyboard shortcuts

| Key | Action |
|---|---|
| `c` | Open comment composer for the current text selection |
| `e` | Toggle element-pick mode |
| `j` / `k` | Walk forward/back through changes after a reload |
| `r` | Revert the change currently focused in the walkthrough |
| `?` | Show shortcut help |
| `Esc` | Cancel current action (composer, element-pick, modal, walkthrough) |
| `Enter` (in modal) | Confirm |

## Security

- The server binds to **127.0.0.1 only** — never `0.0.0.0`. Remote machines on your LAN cannot reach it.
- A 32-character URL-safe token is generated at startup. Every request (except `GET /healthz`) must carry it via `?t=<token>` or the `X-Feedback-Token` header. Requests without it get a 403.
- The token is never logged. Request logs go to stderr with method, path, and status only.
- The server self-shuts down on idle (default 10 minutes of no authenticated activity) and when its parent process dies.
- This is designed for **single-user, local use**. It is not hardened for shared servers, multi-user setups, or exposure beyond `localhost`.

## Troubleshooting

**Port already in use.** The server binds to port `0` and asks the OS for a free port, so this should be rare. If you see a bind error, another instance is likely still running — check `feedback/session.json` for the PID and stop it, then restart.

**Browser didn't open.** Pass `--no-open` and copy the URL from the `READY` line `start.py` prints to your terminal. The URL includes the token query string and is the only way in.

**Process button stuck on a stale count.** The button shows the count of open root comments. After Claude edits, the count drops to 0 once the page reloads. If it doesn't, the SSE channel may have disconnected — refresh the page.

**Comments don't appear in Claude's queue.** Check that `feedback/inbox.jsonl` is actually being written to when you click Send. Then click ▶ Process — Claude only picks comments up when you trigger a batch, not automatically.

**The page reloaded but nothing is highlighted.** The walkthrough overlay looks for `after_snippet` text in the live DOM. If Claude wrote a history entry whose `after_snippet` doesn't appear verbatim on the page (style-only changes, for example), no highlight will render. The change is still there — just not annotated.

**Revert / Redo conflict.** Patch-style revert requires the diffed hunk to still be present in the current file. If a later edit overlapped the same region, you'll get a conflict toast instead of a silent failure. Resolve by editing manually or by reverting the conflicting later edit first.

## Contributing

The single source of truth for the wire format, file layout, and component contracts is [`SPEC.md`](./SPEC.md). Any contribution that changes behavior should update the spec first. If you're a subagent implementing a component, follow the spec exactly; deviations need a `// SPEC-NOTE:` comment in the file you touch.

A visual guide for end users lives at [`docs/index.html`](./docs/index.html) — open it in any browser to see use cases, the typical workflow, the architecture diagram, and a FAQ.

## License

MIT. See [`LICENSE`](./LICENSE).
