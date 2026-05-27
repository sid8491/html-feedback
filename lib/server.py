#!/usr/bin/env python3
# html-feedback local server. Stdlib only. See SPEC.md §2.

from __future__ import annotations

import argparse
import difflib
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Globals (set in main)
# ---------------------------------------------------------------------------

TARGET_DIR: Path
FEEDBACK_DIR: Path
SNAPSHOTS_DIR: Path
INBOX_PATH: Path
HISTORY_PATH: Path
CONTROL_PATH: Path
SESSION_PATH: Path
LIB_DIR: Path = Path(__file__).resolve().parent

TOKEN: str = ""
IDLE_TIMEOUT: float = 600.0
PARENT_PID: int | None = None
LAST_ACTIVITY: float = 0.0
ACTIVITY_LOCK = threading.Lock()
SHUTDOWN_EVENT = threading.Event()

# SSE subscribers and a global lock for jsonl appends
SSE_LOCK = threading.Lock()
SSE_SUBSCRIBERS: list["SSEClient"] = []
APPEND_LOCK = threading.Lock()

# mtime tracker for *.html external-edit detection. Populated lazily.
HTML_MTIMES: dict[str, float] = {}
HTML_MTIME_LOCK = threading.Lock()

# When the server itself records a history entry for a page, remember the new
# mtime so the watcher does not also raise an "external-edit". Same for snapshots.
EXPECTED_MTIMES: dict[str, float] = {}
EXPECTED_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_iso() -> str:
    # Filename-safe ISO (colons replaced with dashes).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def bump_activity() -> None:
    global LAST_ACTIVITY
    with ACTIVITY_LOCK:
        LAST_ACTIVITY = time.monotonic()


def log(msg: str) -> None:
    sys.stderr.write(f"{now_iso()} {msg}\n")
    sys.stderr.flush()


def safe_under(target: Path, candidate: Path) -> Path | None:
    try:
        resolved = (target / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        resolved.relative_to(target.resolve())
        return resolved
    except (ValueError, OSError):
        return None


def atomic_replace_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def append_jsonl(path: Path, obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with APPEND_LOCK:
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# Schema validation (§3)
# ---------------------------------------------------------------------------

COMMENT_TOP_FIELDS = {"id", "parent_id", "page", "anchor", "comment", "ts", "author"}
ANCHOR_FIELDS = {"type", "selector", "selected_text", "text_before", "text_after", "rect"}
ANCHOR_TYPES = {"text", "element", "page"}


def validate_comment(obj: Any) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return False, "body must be a JSON object"
    extra = set(obj.keys()) - COMMENT_TOP_FIELDS
    if extra:
        return False, f"unknown fields: {sorted(extra)}"
    for k in ("page", "comment", "ts", "author"):
        if k not in obj or not isinstance(obj[k], str):
            return False, f"missing/invalid '{k}'"
    if "anchor" not in obj or not isinstance(obj["anchor"], dict):
        return False, "missing 'anchor'"
    anchor = obj["anchor"]
    a_extra = set(anchor.keys()) - ANCHOR_FIELDS
    if a_extra:
        return False, f"unknown anchor fields: {sorted(a_extra)}"
    a_type = anchor.get("type")
    if a_type not in ANCHOR_TYPES:
        return False, f"invalid anchor.type: {a_type!r}"
    for k in ("selector", "selected_text", "text_before", "text_after"):
        if k not in anchor or not isinstance(anchor[k], str):
            return False, f"missing/invalid anchor.{k}"
    if "rect" not in anchor:
        return False, "missing anchor.rect"
    if anchor["rect"] is not None and not isinstance(anchor["rect"], dict):
        return False, "anchor.rect must be object or null"
    if "id" in obj and not isinstance(obj["id"], str):
        return False, "id must be string"
    if "parent_id" in obj and obj["parent_id"] is not None and not isinstance(obj["parent_id"], str):
        return False, "parent_id must be string or null"
    return True, ""


# ---------------------------------------------------------------------------
# Lib-tag injection (in-memory)
# ---------------------------------------------------------------------------

HFB_BEGIN = "<!-- hfb:begin -->"
HFB_END = "<!-- hfb:end -->"
HFB_BLOCK_RE = re.compile(r"<!--\s*hfb:begin\s*-->.*?<!--\s*hfb:end\s*-->", re.DOTALL | re.IGNORECASE)


def build_hfb_block(token: str) -> str:
    return (
        f"{HFB_BEGIN}\n"
        f'<link rel="stylesheet" href="/lib/feedback.css?t={token}">\n'
        f'<script defer src="/lib/feedback.js?t={token}"></script>\n'
        f"{HFB_END}"
    )


def inject_lib_tags(html: str, token: str) -> str:
    block = build_hfb_block(token)
    if HFB_BLOCK_RE.search(html):
        return HFB_BLOCK_RE.sub(block, html, count=1)
    # No existing block: insert before </head> or before </body>.
    m = re.search(r"</head\s*>", html, re.IGNORECASE)
    if m:
        return html[: m.start()] + block + "\n" + html[m.start():]
    m = re.search(r"</body\s*>", html, re.IGNORECASE)
    if m:
        return html[: m.start()] + block + "\n" + html[m.start():]
    return html + "\n" + block + "\n"


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

class SSEClient:
    """A single connected SSE consumer; messages are queued and drained by handler."""

    def __init__(self) -> None:
        self.queue: list[bytes] = []
        self.cv = threading.Condition()
        self.closed = False

    def send(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
        with self.cv:
            self.queue.append(payload)
            self.cv.notify()

    def close(self) -> None:
        with self.cv:
            self.closed = True
            self.cv.notify()


def sse_broadcast(event: str, data: dict) -> None:
    with SSE_LOCK:
        clients = list(SSE_SUBSCRIBERS)
    for c in clients:
        c.send(event, data)


# ---------------------------------------------------------------------------
# Background watchers
# ---------------------------------------------------------------------------

def _watch_jsonl(path: Path, event_name: str) -> None:
    last_size = path.stat().st_size if path.exists() else 0
    last_pos = last_size
    while not SHUTDOWN_EVENT.is_set():
        try:
            if path.exists():
                size = path.stat().st_size
                if size < last_pos:
                    # File shrank/truncated; reset.
                    last_pos = 0
                if size > last_pos:
                    with open(path, "r", encoding="utf-8") as f:
                        f.seek(last_pos)
                        new = f.read()
                        last_pos = f.tell()
                    for line in new.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        sse_broadcast(event_name, obj)
        except OSError:
            pass
        if SHUTDOWN_EVENT.wait(0.2):
            return


def _watch_html_mtimes() -> None:
    # Initialise.
    try:
        for p in TARGET_DIR.glob("*.html"):
            if not p.is_file():
                continue
            HTML_MTIMES[p.name] = p.stat().st_mtime
    except OSError:
        pass
    while not SHUTDOWN_EVENT.is_set():
        try:
            current = {}
            for p in TARGET_DIR.glob("*.html"):
                if not p.is_file():
                    continue
                current[p.name] = p.stat().st_mtime
            with HTML_MTIME_LOCK:
                for name, mtime in current.items():
                    prev = HTML_MTIMES.get(name)
                    if prev is None:
                        HTML_MTIMES[name] = mtime
                        continue
                    if mtime > prev + 1e-6:
                        with EXPECTED_LOCK:
                            expected = EXPECTED_MTIMES.get(name)
                        if expected is not None and abs(expected - mtime) < 1.0:
                            HTML_MTIMES[name] = mtime
                            continue
                        HTML_MTIMES[name] = mtime
                        entry = {
                            "id": f"h-{uuid.uuid4().hex[:12]}",
                            "ts": now_iso(),
                            "page": name,
                            "comment_id": None,
                            "kind": "external-edit",
                            "summary": "page changed outside the comment flow",
                            "before_snippet": "",
                            "after_snippet": "",
                            "snapshot_path": "",
                        }
                        try:
                            append_jsonl(HISTORY_PATH, entry)
                        except OSError:
                            pass
        except OSError:
            pass
        if SHUTDOWN_EVENT.wait(0.2):
            return


def _watch_idle_and_parent() -> None:
    while not SHUTDOWN_EVENT.is_set():
        # Idle timeout
        with ACTIVITY_LOCK:
            last = LAST_ACTIVITY
        if IDLE_TIMEOUT > 0 and (time.monotonic() - last) > IDLE_TIMEOUT:
            log("idle timeout reached; shutting down")
            SHUTDOWN_EVENT.set()
            return
        # Parent PID check
        if PARENT_PID is not None and not _is_pid_alive(PARENT_PID):
            log(f"parent pid {PARENT_PID} died; shutting down")
            SHUTDOWN_EVENT.set()
            return
        if SHUTDOWN_EVENT.wait(5.0):
            return


def _watch_heartbeats() -> None:
    while not SHUTDOWN_EVENT.is_set():
        if SHUTDOWN_EVENT.wait(15.0):
            return
        sse_broadcast("heartbeat", {"ts": now_iso()})


def _compute_patches(before_text: str, after_text: str) -> list[tuple[str, str]]:
    """Per-opcode hunks with 1 line of immediate context. Each non-equal opcode becomes
    one (after_block, before_block) pair anchored by the preceding and following equal
    line. Using minimal context (vs grouped opcodes) keeps independent edits independent."""
    bl = before_text.splitlines(keepends=True)
    al = after_text.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(None, bl, al, autojunk=False)
    ops = sm.get_opcodes()
    patches: list[tuple[str, str]] = []
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            continue
        lead_b = lead_a = ""
        if k > 0 and ops[k - 1][0] == "equal":
            _, pi1, pi2, _, pj2 = ops[k - 1]
            if pi2 > pi1:
                lead_b = bl[pi2 - 1]
                lead_a = al[pj2 - 1]
        trail_b = trail_a = ""
        if k + 1 < len(ops) and ops[k + 1][0] == "equal":
            _, ni1, _, nj1, _ = ops[k + 1]
            if ni1 < len(bl):
                trail_b = bl[ni1]
                trail_a = al[nj1]
        before_block = lead_b + "".join(bl[i1:i2]) + trail_b
        after_block = lead_a + "".join(al[j1:j2]) + trail_a
        if before_block != after_block:
            patches.append((after_block, before_block))
    return patches


def _apply_reverse_patches(current_text: str, patches: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """Replace each after_block with its corresponding before_block in current_text.
    Returns (new_text, failed_reasons). If any hunk fails, no changes are applied."""
    text = current_text
    failed: list[str] = []
    new_text = text
    for after_block, before_block in patches:
        if not after_block:
            # Pure deletion in the original edit; reverse means re-inserting before_block.
            # Without an anchor we cannot place it reliably — flag conflict.
            failed.append(f"cannot re-insert pure deletion: {before_block[:60].strip()!r}")
            continue
        idx = new_text.find(after_block)
        if idx == -1:
            failed.append(f"hunk not found in current file: {after_block[:60].strip()!r}")
            continue
        if new_text.find(after_block, idx + 1) != -1:
            failed.append(f"hunk ambiguous (multiple matches): {after_block[:60].strip()!r}")
            continue
        new_text = new_text[:idx] + before_block + new_text[idx + len(after_block):]
    return new_text, failed


def _is_pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            if not ok:
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False if isinstance(sys.exc_info()[1], ProcessLookupError) else True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "html-feedback/1.0"

    # Silence default stderr logger; we log explicitly.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    # --- helpers --------------------------------------------------------

    def _send(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(status)
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        if "Content-Length" not in (headers or {}):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json(self, status: int, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, data, {"Content-Type": "application/json; charset=utf-8"})

    def _send_text(self, status: int, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
        self._send(status, text.encode("utf-8"), {"Content-Type": ctype})

    def _request_token(self, parsed_qs: dict[str, list[str]]) -> str | None:
        if "t" in parsed_qs and parsed_qs["t"]:
            return parsed_qs["t"][0]
        hdr = self.headers.get("X-Feedback-Token")
        return hdr

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0:
            return b""
        if length > 5_000_000:
            return None
        return self.rfile.read(length)

    def _log_request(self, status: int, path: str) -> None:
        # Strip token from logged path.
        if "?" in path:
            base, q = path.split("?", 1)
            parts = []
            for kv in q.split("&"):
                if kv.startswith("t="):
                    parts.append("t=REDACTED")
                else:
                    parts.append(kv)
            path = base + "?" + "&".join(parts)
        log(f"{self.command} {path} {status}")

    # --- dispatch -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query, keep_blank_values=True)

        # /healthz: no auth
        if path == "/healthz" and self.command == "GET":
            self._send_text(200, "ok")
            self._log_request(200, self.path)
            return

        # Auth on everything else.
        token = self._request_token(qs)
        if not token or not secrets.compare_digest(token, TOKEN):
            self._send_text(403, "forbidden")
            self._log_request(403, self.path)
            return
        bump_activity()

        try:
            status = self._route(path, qs)
        except Exception as e:  # noqa: BLE001
            log(f"handler error: {e!r}")
            self._send_json(500, {"error": "internal"})
            status = 500
        if status is not None:
            self._log_request(status, self.path)

    # --- routes ---------------------------------------------------------

    def _route(self, path: str, qs: dict[str, list[str]]) -> int | None:
        method = self.command

        if path == "/" and method == "GET":
            return self._route_index()
        if path == "/lib/feedback.js" and method == "GET":
            return self._route_static(LIB_DIR / "feedback.js", "application/javascript; charset=utf-8")
        if path == "/lib/feedback.css" and method == "GET":
            return self._route_static(LIB_DIR / "feedback.css", "text/css; charset=utf-8")
        if path == "/api/inbox" and method == "GET":
            return self._route_inbox()
        if path == "/api/history" and method == "GET":
            return self._route_history()
        if path == "/api/session" and method == "GET":
            return self._route_session()
        if path == "/api/file" and method == "GET":
            return self._route_file(qs)
        if path == "/api/events" and method == "GET":
            return self._route_events()
        if path == "/api/feedback" and method == "POST":
            return self._route_post_feedback()
        if path == "/api/redo" and method == "POST":
            return self._route_post_redo()
        if path == "/api/process" and method == "POST":
            return self._route_post_process()
        if path == "/api/feedback/delete" and method == "POST":
            return self._route_post_feedback_delete()
        if path == "/api/feedback/clear-addressed" and method == "POST":
            return self._route_post_clear_addressed()
        if path == "/api/shutdown" and method == "POST":
            return self._route_post_shutdown()
        if path == "/api/revert" and method == "POST":
            return self._route_post_revert()
        if path == "/api/snapshot" and method == "POST":
            return self._route_post_snapshot()
        if path.endswith(".html") and method == "GET":
            return self._route_html(path)

        self._send_json(404, {"error": "not found"})
        return 404

    def _route_index(self) -> int:
        pages = sorted(p.name for p in TARGET_DIR.glob("*.html") if p.is_file())
        items = "\n".join(
            f'  <li><a href="/{name}?t={TOKEN}">{name}</a></li>' for name in pages
        )
        html = (
            "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            "<title>html-feedback</title>"
            "<style>body{font:14px system-ui;margin:2rem;}li{margin:.25rem 0;}</style>"
            "</head><body><h1>html-feedback</h1>"
            f"<p>Target: <code>{TARGET_DIR}</code></p>"
            f"<ul>\n{items}\n</ul></body></html>"
        )
        self._send(200, html.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"})
        return 200

    def _route_static(self, path: Path, ctype: str) -> int:
        if not path.exists() or not path.is_file():
            self._send_json(404, {"error": "not found"})
            return 404
        data = path.read_bytes()
        self._send(200, data, {"Content-Type": ctype, "Cache-Control": "no-cache"})
        return 200

    def _route_html(self, urlpath: str) -> int:
        # urlpath like /foo.html or /sub/bar.html
        rel = urlpath.lstrip("/")
        resolved = safe_under(TARGET_DIR, Path(rel))
        if resolved is None or not resolved.exists() or not resolved.is_file():
            self._send_json(404, {"error": "not found"})
            return 404
        try:
            html = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            self._send_json(500, {"error": "unreadable"})
            return 500
        if HFB_BLOCK_RE.search(html) is None:
            html = inject_lib_tags(html, TOKEN)
        else:
            # Rewrite href/src inside the existing block to carry the token.
            html = HFB_BLOCK_RE.sub(build_hfb_block(TOKEN), html, count=1)
        ctype, _ = mimetypes.guess_type(str(resolved))
        ctype = ctype or "text/html"
        self._send(200, html.encode("utf-8"), {"Content-Type": f"{ctype}; charset=utf-8"})
        return 200

    def _route_inbox(self) -> int:
        raw = read_jsonl(INBOX_PATH)
        # Filter out tombstones and any entries they reference (soft-delete).
        deleted_ids: set[str] = set()
        comments: list[dict] = []
        for c in raw:
            if c.get("_op") == "delete":
                cid = c.get("id")
                if isinstance(cid, str):
                    deleted_ids.add(cid)
                continue
            comments.append(c)
        comments = [c for c in comments if c.get("id") not in deleted_ids]
        history = read_jsonl(HISTORY_PATH)
        reverted_snapshots = {h.get("snapshot_path") for h in history if h.get("kind") == "revert" and h.get("snapshot_path")}
        addressed: dict[str, bool] = {}
        for h in history:
            if h.get("kind") != "edit":
                continue
            cid = h.get("comment_id")
            if not cid:
                continue
            addressed[cid] = h.get("snapshot_path") not in reverted_snapshots
        for c in comments:
            c["status"] = "addressed" if addressed.get(c.get("id")) else "open"
        self._send_json(200, {"comments": comments})
        return 200

    def _route_post_feedback_delete(self) -> int:
        raw_body = self._read_body()
        if raw_body is None:
            self._send_json(400, {"error": "invalid body"})
            return 400
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return 400
        target = body.get("id")
        if not isinstance(target, str) or not target:
            self._send_json(400, {"error": "missing 'id'"})
            return 400
        comments = read_jsonl(INBOX_PATH)
        children: dict[str, list[str]] = {}
        for c in comments:
            pid = c.get("parent_id")
            if isinstance(pid, str):
                children.setdefault(pid, []).append(c.get("id", ""))
        to_delete: set[str] = set()
        stack = [target]
        while stack:
            cid = stack.pop()
            if not cid or cid in to_delete:
                continue
            to_delete.add(cid)
            for ch in children.get(cid, []):
                stack.append(ch)
        ts = now_iso()
        try:
            for cid in to_delete:
                append_jsonl(INBOX_PATH, {"_op": "delete", "id": cid, "ts": ts})
        except OSError as ex:
            self._send_json(500, {"error": f"write failed: {ex}"})
            return 500
        self._send_json(200, {"ok": True, "deleted": sorted(to_delete)})
        return 200

    def _route_post_clear_addressed(self) -> int:
        """Tombstone every comment that is currently 'addressed' (and its descendants).
        Optional body: {"page": "..."} to scope to a single page."""
        raw_body = self._read_body()
        page_filter: str | None = None
        if raw_body:
            try:
                body = json.loads(raw_body.decode("utf-8"))
                if isinstance(body, dict) and isinstance(body.get("page"), str):
                    page_filter = body["page"]
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "invalid json"})
                return 400

        raw_comments = read_jsonl(INBOX_PATH)
        deleted_so_far: set[str] = set()
        for c in raw_comments:
            if c.get("_op") == "delete":
                cid = c.get("id")
                if isinstance(cid, str):
                    deleted_so_far.add(cid)
        comments = [c for c in raw_comments
                    if c.get("_op") != "delete" and c.get("id") not in deleted_so_far]

        history = read_jsonl(HISTORY_PATH)
        reverted = {h.get("snapshot_path") for h in history
                    if h.get("kind") == "revert" and h.get("snapshot_path")}
        addressed_ids: set[str] = set()
        for h in history:
            if h.get("kind") != "edit":
                continue
            cid = h.get("comment_id")
            if not cid:
                continue
            if h.get("snapshot_path") not in reverted:
                addressed_ids.add(cid)
            else:
                addressed_ids.discard(cid)

        children: dict[str, list[str]] = {}
        for c in comments:
            pid = c.get("parent_id")
            if isinstance(pid, str):
                children.setdefault(pid, []).append(c.get("id", ""))

        targets = [c.get("id") for c in comments
                   if c.get("id") in addressed_ids
                   and (page_filter is None or c.get("page") == page_filter)]

        to_delete: set[str] = set()
        stack: list[str] = list(targets)
        while stack:
            cid = stack.pop()
            if not cid or cid in to_delete:
                continue
            to_delete.add(cid)
            for ch in children.get(cid, []):
                stack.append(ch)

        ts = now_iso()
        try:
            for cid in to_delete:
                append_jsonl(INBOX_PATH, {"_op": "delete", "id": cid, "ts": ts})
        except OSError as ex:
            self._send_json(500, {"error": f"write failed: {ex}"})
            return 500
        self._send_json(200, {"ok": True, "deleted": sorted(to_delete), "count": len(to_delete)})
        return 200

    def _route_post_shutdown(self) -> int:
        """Trigger a graceful shutdown. The parent start.py wrapper handles cleanup
        based on a sentinel file we write here."""
        raw_body = self._read_body()
        purge = False
        keep_injected = False
        if raw_body:
            try:
                body = json.loads(raw_body.decode("utf-8"))
                if isinstance(body, dict):
                    purge = bool(body.get("purge_feedback"))
                    keep_injected = bool(body.get("keep_injected"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        sentinel = FEEDBACK_DIR / ".shutdown.json"
        try:
            sentinel.write_text(
                json.dumps({"purge_feedback": purge, "keep_injected": keep_injected, "ts": now_iso()}),
                encoding="utf-8",
            )
        except OSError:
            pass
        self._send_json(200, {"ok": True, "shutting_down": True})

        def _do_shutdown() -> None:
            time.sleep(0.25)  # let the response flush
            log("shutdown requested via /api/shutdown")
            SHUTDOWN_EVENT.set()
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except OSError:
                pass
        threading.Thread(target=_do_shutdown, daemon=True).start()
        return 200

    def _route_history(self) -> int:
        entries = read_jsonl(HISTORY_PATH)
        self._send_json(200, {"entries": entries})
        return 200

    def _route_session(self) -> int:
        if not SESSION_PATH.exists():
            self._send_json(404, {"error": "no session"})
            return 404
        try:
            data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._send_json(500, {"error": "unreadable session"})
            return 500
        self._send_json(200, data)
        return 200

    def _route_file(self, qs: dict[str, list[str]]) -> int:
        rel = (qs.get("path") or [""])[0]
        if not rel:
            self._send_json(400, {"error": "missing path"})
            return 400
        resolved = safe_under(TARGET_DIR, Path(rel))
        if resolved is None or not resolved.exists() or not resolved.is_file():
            self._send_json(404, {"error": "not found"})
            return 404
        data = resolved.read_bytes()
        self._send(200, data, {"Content-Type": "text/plain; charset=utf-8"})
        return 200

    def _route_post_feedback(self) -> int:
        raw = self._read_body()
        if raw is None:
            self._send_json(400, {"error": "invalid body"})
            return 400
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return 400
        ok, msg = validate_comment(obj)
        if not ok:
            self._send_json(400, {"error": msg})
            return 400
        if not obj.get("id"):
            obj["id"] = f"c-{uuid.uuid4().hex[:12]}"
        if obj.get("parent_id") is None:
            obj["parent_id"] = None
        try:
            append_jsonl(INBOX_PATH, obj)
        except OSError as e:
            self._send_json(500, {"error": f"write failed: {e}"})
            return 500
        self._send_json(200, {"id": obj["id"], "status": "open"})
        return 200

    def _route_post_snapshot(self) -> int:
        raw = self._read_body()
        if raw is None:
            self._send_json(400, {"error": "invalid body"})
            return 400
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return 400
        page = body.get("page")
        if not isinstance(page, str) or not page:
            self._send_json(400, {"error": "missing 'page'"})
            return 400
        src = safe_under(TARGET_DIR, Path(page))
        if src is None or not src.exists() or not src.is_file():
            self._send_json(404, {"error": "page not found"})
            return 404
        dest_dir = SNAPSHOTS_DIR / page
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = snapshot_iso()
        dest = dest_dir / f"{ts}.html"
        # Same second can collide when before/after snapshots are taken back-to-back;
        # de-duplicate with a -1, -2, ... suffix.
        n = 1
        while dest.exists():
            dest = dest_dir / f"{ts}-{n}.html"
            n += 1
        shutil.copy2(src, dest)
        rel = dest.relative_to(TARGET_DIR).as_posix()
        self._send_json(200, {"snapshot_path": rel})
        return 200

    def _route_post_revert(self) -> int:
        raw = self._read_body()
        if raw is None:
            self._send_json(400, {"error": "invalid body"})
            return 400
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return 400
        hid = body.get("history_id")
        if not isinstance(hid, str) or not hid:
            self._send_json(400, {"error": "missing 'history_id'"})
            return 400
        entries = read_jsonl(HISTORY_PATH)
        match = next((e for e in entries if e.get("id") == hid), None)
        if match is None:
            self._send_json(404, {"error": "history entry not found"})
            return 404
        page = match.get("page")
        snap_before_rel = match.get("snapshot_path")
        snap_after_rel = match.get("snapshot_after_path")
        if not page or not snap_before_rel:
            self._send_json(400, {"error": "entry missing page or snapshot_path"})
            return 400
        page_path = safe_under(TARGET_DIR, Path(page))
        snap_before = safe_under(TARGET_DIR, Path(snap_before_rel))
        if page_path is None or snap_before is None or not snap_before.exists():
            self._send_json(404, {"error": "page or before snapshot missing"})
            return 404

        summary: str
        if snap_after_rel:
            # Patch-style undo: reverse the diff between before and after, apply to current.
            snap_after = safe_under(TARGET_DIR, Path(snap_after_rel))
            if snap_after is None or not snap_after.exists():
                self._send_json(404, {"error": "after snapshot missing"})
                return 404
            try:
                before_text = snap_before.read_text(encoding="utf-8")
                after_text = snap_after.read_text(encoding="utf-8")
                current_text = page_path.read_text(encoding="utf-8")
            except OSError as ex:
                self._send_json(500, {"error": f"read failed: {ex}"})
                return 500
            patches = _compute_patches(before_text, after_text)
            new_text, failed = _apply_reverse_patches(current_text, patches)
            if failed:
                self._send_json(409, {"error": "revert conflict", "details": failed})
                return 409
            tmp = page_path.with_suffix(page_path.suffix + ".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, page_path)
            summary = f"patch-reverted edit {hid}"
        else:
            # Legacy entry without after-snapshot: full restore.
            shutil.copy2(snap_before, page_path)
            summary = f"reverted to snapshot {snap_before_rel} (legacy full restore)"

        # Mark the expected mtime so the watcher does not flag this as external.
        try:
            with EXPECTED_LOCK:
                EXPECTED_MTIMES[page] = page_path.stat().st_mtime
        except OSError:
            pass
        new_entry = {
            "id": f"h-{uuid.uuid4().hex[:12]}",
            "ts": now_iso(),
            "page": page,
            "comment_id": None,
            "kind": "revert",
            "summary": summary,
            "before_snippet": "",
            "after_snippet": "",
            "snapshot_path": snap_before_rel,
        }
        append_jsonl(HISTORY_PATH, new_entry)
        self._send_json(200, {"ok": True, "page": page})
        return 200

    def _route_post_redo(self) -> int:
        """Re-apply an edit that was previously reverted. Patch-style forward."""
        raw = self._read_body()
        if raw is None:
            self._send_json(400, {"error": "invalid body"})
            return 400
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return 400
        hid = body.get("history_id")
        if not isinstance(hid, str) or not hid:
            self._send_json(400, {"error": "missing 'history_id'"})
            return 400
        entries = read_jsonl(HISTORY_PATH)
        match = next((e for e in entries if e.get("id") == hid), None)
        if match is None or match.get("kind") != "edit":
            self._send_json(404, {"error": "edit entry not found"})
            return 404
        page = match.get("page")
        snap_before_rel = match.get("snapshot_path")
        snap_after_rel = match.get("snapshot_after_path")
        if not page or not snap_before_rel or not snap_after_rel:
            self._send_json(400, {"error": "entry missing required snapshot paths for redo"})
            return 400
        page_path = safe_under(TARGET_DIR, Path(page))
        snap_before = safe_under(TARGET_DIR, Path(snap_before_rel))
        snap_after = safe_under(TARGET_DIR, Path(snap_after_rel))
        if (page_path is None or snap_before is None or snap_after is None
                or not snap_before.exists() or not snap_after.exists()):
            self._send_json(404, {"error": "page or snapshot missing"})
            return 404
        try:
            before_text = snap_before.read_text(encoding="utf-8")
            after_text = snap_after.read_text(encoding="utf-8")
            current_text = page_path.read_text(encoding="utf-8")
        except OSError as ex:
            self._send_json(500, {"error": f"read failed: {ex}"})
            return 500
        # Forward direction: swap roles — find before_block in current, replace with after_block.
        patches = _compute_patches(before_text, after_text)
        forward = [(before_block, after_block) for after_block, before_block in patches]
        new_text, failed = _apply_reverse_patches(current_text, forward)
        if failed:
            self._send_json(409, {"error": "redo conflict", "details": failed})
            return 409

        # Take fresh before+after snapshots so the new edit entry has its own snapshot lineage
        # (decoupled from the original so future reverts on this redo work correctly).
        dest_dir = SNAPSHOTS_DIR / page
        dest_dir.mkdir(parents=True, exist_ok=True)
        def _next_snapshot(state_text: str) -> str:
            ts = snapshot_iso()
            dest = dest_dir / f"{ts}.html"
            n = 1
            while dest.exists():
                dest = dest_dir / f"{ts}-{n}.html"
                n += 1
            dest.write_text(state_text, encoding="utf-8")
            return dest.relative_to(TARGET_DIR).as_posix()

        new_before_rel = _next_snapshot(current_text)
        tmp = page_path.with_suffix(page_path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, page_path)
        new_after_rel = _next_snapshot(new_text)

        try:
            with EXPECTED_LOCK:
                EXPECTED_MTIMES[page] = page_path.stat().st_mtime
        except OSError:
            pass
        new_entry = {
            "id": f"h-{uuid.uuid4().hex[:12]}",
            "ts": now_iso(),
            "page": page,
            "comment_id": match.get("comment_id"),
            "kind": "edit",
            "summary": f"redo of {hid}: {match.get('summary', '')}",
            "before_snippet": match.get("before_snippet", ""),
            "after_snippet": match.get("after_snippet", ""),
            "snapshot_path": new_before_rel,
            "snapshot_after_path": new_after_rel,
        }
        append_jsonl(HISTORY_PATH, new_entry)
        self._send_json(200, {"ok": True, "page": page, "edit_id": new_entry["id"]})
        return 200

    def _route_post_process(self) -> int:
        """Compute open comments and emit a 'process' event to control.jsonl.
        The watcher tails control.jsonl and forwards to Claude."""
        # Body is optional — accept {"page": "..."} to scope, or {} for all pages.
        raw = self._read_body()
        body: dict = {}
        if raw is not None and raw:
            try:
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    body = {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "invalid json"})
                return 400
        page_filter = body.get("page") if isinstance(body.get("page"), str) else None

        comments = read_jsonl(INBOX_PATH)
        history = read_jsonl(HISTORY_PATH)
        reverted_snaps = {h.get("snapshot_path") for h in history
                          if h.get("kind") == "revert" and h.get("snapshot_path")}
        addressed: dict[str, bool] = {}
        for h in history:
            if h.get("kind") != "edit":
                continue
            cid = h.get("comment_id")
            if not cid:
                continue
            addressed[cid] = h.get("snapshot_path") not in reverted_snaps
        open_ids: list[str] = []
        for c in comments:
            if page_filter and c.get("page") != page_filter:
                continue
            if not addressed.get(c.get("id")):
                open_ids.append(c.get("id", ""))
        entry = {
            "type": "process",
            "ts": now_iso(),
            "pending_ids": open_ids,
            "page_filter": page_filter,
        }
        try:
            append_jsonl(CONTROL_PATH, entry)
        except OSError as ex:
            self._send_json(500, {"error": f"write failed: {ex}"})
            return 500
        self._send_json(200, {"ok": True, "pending": len(open_ids), "ids": open_ids})
        return 200

    def _route_events(self) -> int | None:
        # Long-lived SSE response — we manage status logging ourselves.
        client = SSEClient()
        with SSE_LOCK:
            SSE_SUBSCRIBERS.append(client)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self._log_request(200, self.path)
            # Initial hello (also primes some clients).
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return None
            while not SHUTDOWN_EVENT.is_set():
                with client.cv:
                    if not client.queue and not client.closed:
                        client.cv.wait(timeout=1.0)
                    pending = client.queue
                    client.queue = []
                    closed = client.closed
                if SHUTDOWN_EVENT.is_set():
                    break
                try:
                    for chunk in pending:
                        self.wfile.write(chunk)
                    if pending:
                        self.wfile.flush()
                        bump_activity()
                except (BrokenPipeError, ConnectionResetError):
                    break
                if closed:
                    break
            return None
        finally:
            with SSE_LOCK:
                if client in SSE_SUBSCRIBERS:
                    SSE_SUBSCRIBERS.remove(client)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def write_session(url: str, port: int) -> None:
    data = {
        "url": url,
        "token": TOKEN,
        "port": port,
        "pid": os.getpid(),
        "started_at": now_iso(),
        "target_dir": str(TARGET_DIR),
    }
    atomic_replace_write(SESSION_PATH, json.dumps(data, ensure_ascii=False, indent=2))


def remove_session() -> None:
    try:
        SESSION_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def install_signal_handlers() -> None:
    def handler(signum, _frame):
        log(f"signal {signum} received; shutting down")
        SHUTDOWN_EVENT.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, handler)  # type: ignore[attr-defined]
        except (ValueError, OSError):
            pass


def patch_post_feedback_to_broadcast(handler_cls: type[Handler]) -> None:
    # Wrap to fan out an SSE inbox event on success.
    orig = handler_cls._route_post_feedback

    def wrapper(self):  # type: ignore[no-untyped-def]
        # Capture the body again post-route — easier to rebuild via reading the file tail.
        status = orig(self)
        if status == 200:
            try:
                tail = read_jsonl(INBOX_PATH)
                if tail:
                    sse_broadcast("inbox", tail[-1])
            except OSError:
                pass
        return status

    handler_cls._route_post_feedback = wrapper  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global TARGET_DIR, FEEDBACK_DIR, SNAPSHOTS_DIR, INBOX_PATH, HISTORY_PATH, CONTROL_PATH, SESSION_PATH
    global TOKEN, IDLE_TIMEOUT, PARENT_PID, LAST_ACTIVITY

    ap = argparse.ArgumentParser(prog="html-feedback-server")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--idle-timeout", type=float, default=600.0)
    ap.add_argument("--parent-pid", type=int, default=None)
    args = ap.parse_args()

    TARGET_DIR = Path(args.dir).resolve()
    if not TARGET_DIR.exists() or not TARGET_DIR.is_dir():
        sys.stderr.write(f"target dir does not exist: {TARGET_DIR}\n")
        return 2
    FEEDBACK_DIR = TARGET_DIR / "feedback"
    SNAPSHOTS_DIR = FEEDBACK_DIR / ".snapshots"
    INBOX_PATH = FEEDBACK_DIR / "inbox.jsonl"
    HISTORY_PATH = FEEDBACK_DIR / "history.jsonl"
    CONTROL_PATH = FEEDBACK_DIR / "control.jsonl"
    SESSION_PATH = FEEDBACK_DIR / "session.json"
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_PATH.touch(exist_ok=True)
    HISTORY_PATH.touch(exist_ok=True)
    CONTROL_PATH.touch(exist_ok=True)

    IDLE_TIMEOUT = float(args.idle_timeout)
    PARENT_PID = args.parent_pid
    TOKEN = secrets.token_urlsafe(24)
    LAST_ACTIVITY = time.monotonic()

    patch_post_feedback_to_broadcast(Handler)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"
    write_session(url, port)

    install_signal_handlers()

    # Background threads
    threads = [
        threading.Thread(target=_watch_jsonl, args=(HISTORY_PATH, "history"), daemon=True),
        threading.Thread(target=_watch_jsonl, args=(INBOX_PATH, "inbox"), daemon=True),
        threading.Thread(target=_watch_html_mtimes, daemon=True),
        threading.Thread(target=_watch_idle_and_parent, daemon=True),
        threading.Thread(target=_watch_heartbeats, daemon=True),
    ]
    for t in threads:
        t.start()

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    # READY line (exactly one, exactly this format)
    sys.stdout.write(f"READY {url}\n")
    sys.stdout.flush()
    log(f"listening on 127.0.0.1:{port}; target={TARGET_DIR}")

    try:
        while not SHUTDOWN_EVENT.wait(0.5):
            pass
    except KeyboardInterrupt:
        SHUTDOWN_EVENT.set()

    log("shutting down")
    try:
        httpd.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        httpd.server_close()
    except Exception:  # noqa: BLE001
        pass
    # Wake any SSE waiters.
    with SSE_LOCK:
        for c in list(SSE_SUBSCRIBERS):
            c.close()
    remove_session()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
