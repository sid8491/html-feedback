# Changelog

All notable changes to **html-feedback** are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/) conventions and uses [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-05-27

First public release. Stable surface for everyday use.

### Added

- **Inline commenting on rendered HTML pages.** Highlight text, click elements with `e`, or comment on the whole page from the sidebar header. Each comment carries a robust multi-strategy anchor (CSS selector + text fingerprint + whitespace-tolerant fallback) so it survives surrounding edits.
- **Batch processing.** Comments accumulate in the sidebar. Click the blue **▶ Process (N)** button to send the whole batch to Claude in one shot — no per-comment round-trips through the CLI.
- **Live processing pill.** A top-of-page indicator shows `Processing N/M…` with a spinner while Claude works, then `✓ All N processed`. Survives the auto-reloads that happen between edits via `sessionStorage`.
- **Threaded replies.** Reply to any comment to push back or refine; the conversation stays attached to the same anchor. Acknowledgements ("thanks", "ok") are no-ops by Claude's side.
- **Auto-reload with change walkthrough.** When Claude edits a page, the browser reloads via SSE and highlights every changed region in yellow with Claude's summary as a tooltip. Step through with `j` / `k`. Press `r` to revert the change you're currently focused on.
- **Patch-style per-edit Revert.** Every edit captures both a *before* and *after* snapshot. Reverting an edit reverse-applies just that edit's diff to the current file — unrelated later edits are preserved untouched. Conflict detection returns a 409 with details if a hunk can't be located.
- **Redo.** After a revert, the sidebar link flips from ↶ Revert to ↺ Redo so you can re-apply that exact change in one click.
- **WhatsApp-style unread bubble** on the floating 💬 button shows the count of open (unprocessed) comments. Stays in sync with the Process button counter via a shared updater called from every render.
- **Newest-first sidebar sort** by root timestamp.
- **Per-comment delete** (small ✕ on each card) and **clear all addressed (N)** in the section header — both implemented as append-only tombstones in `inbox.jsonl` (no rewrites, no race conditions).
- **Orphan tray with re-pin.** When a comment's anchor no longer resolves, it lands in an Orphans section. Click *Re-pin* and the next selection or element click re-anchors it as a reply to the original.
- **Polished modals + popups.** Native browser `confirm()` replaced with custom styled modals (Enter confirms, Esc cancels, backdrop click cancels, confirm button auto-focused). All popovers share a consistent animation library and dark-mode-aware palette.
- **Session cleanup.** Three ways to end a session, all auto-clean:
  - Click **⏻** in the sidebar header → modal asks whether to also delete history → server stops → injection tags stripped from every HTML file
  - Tell Claude *"stop html feedback"* / *"clean up"* → Claude POSTs `/api/shutdown` and reports back
  - Ctrl-C in the terminal → same auto-cleanup runs
  Opt-out via `--keep-injected`; opt-in to feedback/ deletion via `--purge-on-exit`.
- **Server.** Stdlib-only Python HTTP+SSE server (`lib/server.py`). 127.0.0.1 bind, 32-char URL-safe per-session token, idle + parent-PID shutdown, SSE event stream, snapshot/revert/redo/process/shutdown routes, soft-delete via tombstones.
- **Client.** Vanilla JS + CSS (`lib/feedback.js`, `lib/feedback.css`) — no frameworks, no build step, no external resources, dark-mode-aware via `prefers-color-scheme`.
- **Scripts.** `inject.py` (idempotent injector that skips `feedback/` subdirs), `start.py` (one-command launcher with auto-cleanup on exit), `watch_control.py` (trigger watcher for the Monitor tool).
- **Documentation.** Full landing page at `docs/index.html` (1218 lines, mobile-responsive 360→1440px, dark-mode-aware, self-contained) with hero, install tabs, features grid, architecture diagram, use cases, walkthrough mockups, and FAQ. Spec at `SPEC.md`. Skill specification at `SKILL.md`.

### Security

- Localhost-only bind (`127.0.0.1`), never `0.0.0.0`.
- 32-character URL-safe random token per session, never logged.
- Path-traversal defense on all file routes.
- Server auto-shuts down on idle (default 600s) and when the parent process dies.
- Designed for single-user local use; explicitly not hardened for shared servers.

---

[1.0.0]: https://github.com/sid8491/html-feedback/releases/tag/v1.0.0
