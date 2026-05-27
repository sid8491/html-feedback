<h1 align="center">html-feedback</h1>

<p align="center">
  <strong>A Claude Code plugin that turns any folder of HTML into a Figma-style review canvas.</strong>
</p>

<p align="center">
  Highlight text. Click elements. Leave comments. Hit <kbd>▶ Process</kbd>.<br/>
  Claude edits the source HTML, the page reloads with the changes highlighted, and you revert anything you don't like with one click.
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#features">Features</a> ·
  <a href="./docs/index.html">User guide</a> ·
  <a href="./SPEC.md">Spec</a>
</p>

<p align="center">
  <img alt="Smoke test" src="https://github.com/sid8491/html-feedback/actions/workflows/test.yml/badge.svg">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10+-blue?style=flat-square">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green?style=flat-square">
  <img alt="Deps" src="https://img.shields.io/badge/deps-stdlib%20only-brightgreen?style=flat-square">
  <img alt="Status" src="https://img.shields.io/badge/local--only-127.0.0.1-orange?style=flat-square">
</p>

---

## Why this exists

Reviewing AI-generated HTML in a chat window is painful. You squint at a diff, type "the third paragraph is too long," and pray Claude finds the right one. **html-feedback flips it around**: you stay in the browser, comment *on the rendered page*, click ▶ Process, and Claude edits the source. The loop is fast, visual, and reversible.

## Install

### Option 1 — As a Claude Code skill (recommended)

Clone the repo into your Claude Code skills folder:

**macOS / Linux**
```sh
git clone https://github.com/sid8491/html-feedback ~/.claude/skills/html-feedback
```

**Windows (PowerShell)**
```powershell
git clone https://github.com/sid8491/html-feedback "$env:USERPROFILE\.claude\skills\html-feedback"
```

That's it. Open Claude Code, type `/html-feedback` (or just say *"start html feedback on ./pages"*), and Claude will spin up the server, open your browser, and start listening.

### Option 2 — Run it standalone

If you don't use Claude Code, you can still run the server yourself and drive the inbox manually:

```sh
git clone https://github.com/sid8491/html-feedback
cd html-feedback
python scripts/start.py --dir /path/to/your/html
```

## Quick start

Once installed, just point Claude at a folder of HTML files:

> *"start html feedback on ./pages"*

Claude responds with a localhost URL. Open it. The page is now annotatable:

1. **Highlight** any sentence → 💬 Comment pill appears → type your request → Send.
2. Leave as many comments as you want — the red bubble on the floating 💬 button tracks them.
3. Click **▶ Process (N)** in the sidebar when you're ready. Claude addresses them as a batch.
4. The page **auto-reloads** with yellow highlights on every changed region. Press `j` / `k` to step through.
5. Don't like an edit? **↶ Revert** in the sidebar undoes just that change. Change your mind? **↺ Redo** brings it back.
6. **clear addressed (N)** wipes the processed comments when you're done.

You never have to switch back to the CLI.

## Features

| | |
|---|---|
| 💬 | **Inline comments** on the rendered page — text, elements, or whole-page notes |
| 📦 | **Batch processing** via a single ▶ Process button — no per-comment CLI round-trips |
| ⚡ | **Live status pill** shows `Processing N/M…` while Claude works |
| 🧵 | **Threaded replies** stay attached to the same anchor |
| ↶ ↺ | **Patch-style Revert / Redo** — reverting one edit doesn't disturb the others |
| 🟡 | **Walkthrough highlights** mark every change; `j`/`k` to navigate |
| 🧹 | **clear addressed (N)** wipes processed comments in one click |
| 🔔 | **WhatsApp-style unread bubble** on the floating 💬 button |
| 📸 | **Visual context** — every comment carries a screenshot of the region you were looking at, so Claude sees what you see |
| 📄 | **Multi-page navigation** — the Pages dropdown in the sidebar lists every HTML file with per-page open/addressed counts and a dot showing pending work elsewhere |
| 🔒 | **Local-only**, 127.0.0.1 bind, random per-session token, idle shutdown |
| 0️⃣ | **Zero dependencies** — Python 3.10 + stdlib only |

## How it works

```
+--------------------+        HTTP + SSE         +-------------------+
|  Browser           |  <---------------------->  |  Python server   |
|  feedback.js/.css  |     127.0.0.1:<port>      |  lib/server.py   |
+--------------------+      ?t=<token>            +---------+--------+
                                                            |
                                                            v
                                                  +-------------------+
                                                  |  feedback/        |
                                                  |   inbox.jsonl     |
                                                  |   control.jsonl   |
                                                  |   history.jsonl   |
                                                  |   .snapshots/     |
                                                  +---------+---------+
                                                            |
                                                            v
                                                  +-------------------+
                                                  |  Claude Code      |
                                                  +-------------------+
```

The browser writes comments to `inbox.jsonl` (via the server). When you click ▶ Process, a trigger is written to `control.jsonl`. Claude tails that file, reads the batch, edits source HTML, snapshots before+after each edit, and appends entries to `history.jsonl`. The server pushes Server-Sent Events back to the browser so the page reloads and shows what changed.

## What's where

```
html-feedback/
├── lib/
│   ├── server.py             Stdlib HTTP + SSE + token auth + patch revert/redo
│   ├── feedback.js           Client: sidebar, composer, walkthrough, modals
│   ├── feedback.css          Polished UI, dark-mode aware
│   └── vendor/
│       └── html2canvas.min.js   Bundled for screenshot capture (MIT)
├── scripts/
│   ├── inject.py             Idempotent <script> tag injector
│   ├── start.py              One-command launcher (inject + spawn + open)
│   └── watch_control.py      Trigger watcher for batch processing
├── tests/
│   └── smoke.py              End-to-end test (34 steps, stdlib-only)
├── .github/workflows/
│   └── test.yml              CI on ubuntu/windows/macos × Python 3.10–3.12
├── docs/index.html           Full user guide (open in any browser)
├── SKILL.md                  Claude Code skill spec — what Claude reads
├── SPEC.md                   Wire protocol + schemas
├── CHANGELOG.md              Release notes
└── LICENSE                   MIT
```

## Keyboard shortcuts

| Key | Action |
|---|---|
| `c` | Comment on the current text selection |
| `e` | Toggle element-pick mode |
| `j` / `k` | Step through changes in the walkthrough |
| `r` | Revert the change currently focused |
| `?` | Show shortcut help overlay |
| `Esc` | Cancel current action |

## Security

- Server binds to **`127.0.0.1` only** — never `0.0.0.0`. LAN machines can't reach it.
- A 32-character random token is generated per session. Every request needs it.
- Token is never logged.
- Server self-shuts on idle (default 10 min) and when the parent process dies.
- Designed for **single-user, local use**. Not hardened for shared servers.

## Stopping & cleanup

There are three equivalent ways to end a session — all clean up after themselves:

1. **The ⏻ button** in the sidebar header. A modal asks whether to also delete history; either way, the server stops and the injection tags are stripped from your HTML files.
2. **Tell Claude** *"stop html feedback"* or *"clean up"*. Claude POSTs `/api/shutdown` and reports back when done.
3. **Ctrl-C** in the terminal running `start.py`. Same auto-cleanup runs.

After cleanup, your HTML files are exactly as you started — minus any *content* edits Claude made (those stay, that's the point). Pass `--keep-injected` to `start.py` if you want to leave the tags in place between sessions; pass `--purge-on-exit` to also wipe the `feedback/` folder.

## When to use this

✅ Iterating on AI-generated reports, research artifacts, marketing copy
✅ Polishing meeting notes, transcripts, generated docs
✅ Reviewing HTML/CSS mockups
✅ Any small-to-medium folder of static HTML that needs many small edits

❌ Live web apps with backend state — the server only serves and edits files
❌ Hundreds of pages at once — the injector touches each file
❌ Multi-user simultaneous editing — single-user local only

## Contributing

The wire format, file layout, and component contracts live in [`SPEC.md`](./SPEC.md). Any behavior change should update the spec first. See [`docs/index.html`](./docs/index.html) for the user-facing guide.

## License

MIT. See [`LICENSE`](./LICENSE).

---

<p align="center">
  Made for people who'd rather <em>show</em> than <em>tell</em> Claude what to change.
</p>
