#!/usr/bin/env python3
"""End-to-end smoke test for html-feedback.

Spawns lib/server.py against a temp dir of two HTML pages, walks the full
server API surface, and asserts the documented behaviour from SPEC.md and
SKILL.md. Stdlib only. Cross-platform (Windows + POSIX).

Run:  python tests/smoke.py
Exit 0 on success, non-zero on any failed assertion.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = REPO_ROOT / "lib" / "server.py"
INJECT_PY = REPO_ROOT / "scripts" / "inject.py"

INDEX_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Index</title></head>'
    "<body><h1>Index page</h1>"
    "<p>The quick brown fox jumps over the lazy dog.</p>"
    "<p>Second paragraph with more original text in it.</p>"
    "</body></html>\n"
)
PAGE2_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Page 2</title></head>'
    "<body><h1>Page Two</h1><p>Some content on page two.</p></body></html>\n"
)
EDITED_INDEX_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Index</title></head>'
    "<body><h1>Index page</h1>"
    "<p>The swift red fox leaps over the sleepy dog.</p>"
    "<p>Second paragraph with more original text in it.</p>"
    "</body></html>\n"
)

step_count = 0


def step(msg: str) -> None:
    global step_count
    step_count += 1
    print(f"[{step_count}] {msg}")


def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def assert_eq(actual, expected, label: str) -> None:
    if actual != expected:
        fail(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(cond: bool, label: str) -> None:
    if not cond:
        fail(label)


def http(method: str, url: str, body: dict | None = None,
         expect: int | None = None, timeout: float = 10.0) -> tuple[int, bytes]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, content = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, content = e.code, e.read()
    if expect is not None and status != expect:
        fail(f"{method} {url}: expected {expect}, got {status}; body={content[:200]!r}")
    return status, content


def http_json(method: str, url: str, body: dict | None = None, expect: int = 200) -> dict:
    _, content = http(method, url, body=body, expect=expect)
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail(f"{method} {url}: non-JSON body: {content[:200]!r}")


def with_token(base: str, path: str, token: str, extra: dict | None = None) -> str:
    params = {"t": token}
    if extra:
        params.update(extra)
    return f"{base}{path}?{urllib.parse.urlencode(params)}"


def spawn_server(target_dir: Path) -> tuple[subprocess.Popen, str, str]:
    cmd = [sys.executable, str(SERVER_PY), "--dir", str(target_dir), "--idle-timeout", "60"]
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                    "bufsize": 1, "text": True, "encoding": "utf-8"}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    proc = subprocess.Popen(cmd, **kwargs)
    assert proc.stdout is not None
    deadline = time.time() + 15.0
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else ""
                fail(f"server exited before READY (rc={proc.returncode}); stderr:\n{err}")
            continue
        line = line.strip()
        if line.startswith("READY "):
            url = line[len("READY "):].strip()
            parsed = urllib.parse.urlparse(url)
            token = urllib.parse.parse_qs(parsed.query).get("t", [""])[0]
            return proc, f"{parsed.scheme}://{parsed.netloc}", token
    fail("timeout waiting for READY line from server")


def shutdown_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    if not SERVER_PY.exists():
        fail(f"server.py not found at {SERVER_PY}")
    if not INJECT_PY.exists():
        fail(f"inject.py not found at {INJECT_PY}")

    tmpdir = Path(tempfile.mkdtemp(prefix="hfb-smoke-"))
    print(f"tmpdir: {tmpdir}")
    proc: subprocess.Popen | None = None
    try:
        (tmpdir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        (tmpdir / "page2.html").write_text(PAGE2_HTML, encoding="utf-8")

        # Inject hfb tags so the source files carry the markers.
        inject_cmd = [sys.executable, str(INJECT_PY), "inject", "--dir", str(tmpdir)]
        r = subprocess.run(inject_cmd, capture_output=True, text=True)
        if r.returncode != 0:
            fail(f"inject.py failed: rc={r.returncode}, stderr={r.stderr}")
        idx_text = (tmpdir / "index.html").read_text(encoding="utf-8")
        assert_true("<!-- hfb:begin -->" in idx_text, "inject did not add hfb tags to index.html")

        proc, base, token = spawn_server(tmpdir)
        print(f"base={base}, token={token[:6]}...")

        step("GET /healthz (no auth) -> 200 'ok'")
        status, content = http("GET", f"{base}/healthz", expect=200)
        assert_eq(content.decode("utf-8"), "ok", "/healthz body")

        step("GET / without token -> 403")
        http("GET", f"{base}/", expect=403)

        step("GET / with token -> 200, lists both files")
        _, content = http("GET", with_token(base, "/", token), expect=200)
        body = content.decode("utf-8")
        assert_true("index.html" in body and "page2.html" in body, "index missing filenames")

        step("GET /index.html?t=... -> 200, hfb block has tokenised lib URLs")
        _, content = http("GET", with_token(base, "/index.html", token), expect=200)
        body = content.decode("utf-8")
        assert_true("<!-- hfb:begin -->" in body, "missing hfb:begin")
        assert_true(f"feedback.js?t={token}" in body, "feedback.js URL not tokenised")
        assert_true(f"feedback.css?t={token}" in body, "feedback.css URL not tokenised")
        assert_true(f"html2canvas.min.js?t={token}" in body, "html2canvas vendor URL missing or not tokenised")

        step("GET /lib/feedback.js?t=... -> 200, content-length > 1000")
        _, content = http("GET", with_token(base, "/lib/feedback.js", token), expect=200)
        assert_true(len(content) > 1000, f"feedback.js too small: {len(content)} bytes")

        step("GET /lib/feedback.css?t=... -> 200, content-length > 1000")
        _, content = http("GET", with_token(base, "/lib/feedback.css", token), expect=200)
        assert_true(len(content) > 1000, f"feedback.css too small: {len(content)} bytes")

        step("GET /api/session?t=... -> 200, expected fields")
        session = http_json("GET", with_token(base, "/api/session", token))
        for key in ("token", "port", "url", "pid", "target_dir"):
            assert_true(key in session, f"/api/session missing {key}")

        step("GET /api/file?path=index.html&t=... -> 200")
        http("GET", with_token(base, "/api/file", token, {"path": "index.html"}), expect=200)

        step("GET /api/file?path=../SPEC.md&t=... -> 404 (path traversal blocked)")
        http("GET", with_token(base, "/api/file", token, {"path": "../SPEC.md"}), expect=404)

        step("POST /api/feedback valid comment -> 200 {id, status:'open'}")
        comment_id_1 = f"c-{uuid.uuid4().hex[:12]}"
        feedback_body = {
            "id": comment_id_1, "parent_id": None, "page": "index.html",
            "anchor": {"type": "text", "selector": "body > p:nth-of-type(1)",
                       "selected_text": "quick brown fox", "text_before": "The ",
                       "text_after": " jumps over",
                       "rect": {"x": 0, "y": 0, "w": 100, "h": 20}},
            "comment": "Make the fox red and swift.",
            "ts": "2026-05-27T12:00:00Z", "author": "local",
        }
        resp = http_json("POST", with_token(base, "/api/feedback", token), body=feedback_body)
        assert_eq(resp.get("id"), comment_id_1, "feedback id mismatch")
        assert_eq(resp.get("status"), "open", "feedback status")

        step("POST /api/feedback unknown field 'weird' -> 400")
        bad = {**feedback_body, "weird": "x", "id": f"c-{uuid.uuid4().hex[:12]}"}
        http("POST", with_token(base, "/api/feedback", token), body=bad, expect=400)

        step("POST /api/feedback bad anchor.type='video' -> 400")
        bad2 = json.loads(json.dumps(feedback_body))
        bad2["anchor"]["type"] = "video"
        bad2["id"] = f"c-{uuid.uuid4().hex[:12]}"
        http("POST", with_token(base, "/api/feedback", token), body=bad2, expect=400)

        step("GET /api/inbox -> 200, has 1 comment with status 'open'")
        inbox = http_json("GET", with_token(base, "/api/inbox", token))
        comments = inbox.get("comments", [])
        assert_eq(len(comments), 1, "inbox comment count")
        assert_eq(comments[0]["id"], comment_id_1, "inbox comment id")
        assert_eq(comments[0]["status"], "open", "inbox comment status")

        step("POST /api/snapshot {page:'index.html'} -> 200, snapshot file exists")
        snap1 = http_json("POST", with_token(base, "/api/snapshot", token), body={"page": "index.html"})
        snap1_rel = snap1.get("snapshot_path")
        assert_true(isinstance(snap1_rel, str) and snap1_rel, "missing snapshot_path")
        snap1_abs = tmpdir / snap1_rel
        assert_true(snap1_abs.exists(), f"snapshot file not on disk: {snap1_abs}")

        step("Simulate Claude edit: rewrite tmpdir/index.html")
        (tmpdir / "index.html").write_text(EDITED_INDEX_HTML, encoding="utf-8")

        step("POST /api/snapshot again -> 200, different path (snapshot dedup)")
        snap2 = http_json("POST", with_token(base, "/api/snapshot", token), body={"page": "index.html"})
        snap2_rel = snap2.get("snapshot_path")
        assert_true(isinstance(snap2_rel, str) and snap2_rel, "missing second snapshot_path")
        assert_true(snap2_rel != snap1_rel, f"snapshot dedup failed: {snap1_rel} == {snap2_rel}")
        snap2_abs = tmpdir / snap2_rel
        assert_true(snap2_abs.exists(), f"second snapshot not on disk: {snap2_abs}")

        step("Append history entry to feedback/history.jsonl (edit kind, both snapshots)")
        history_id = f"h-{uuid.uuid4().hex[:12]}"
        history_entry = {
            "id": history_id, "ts": "2026-05-27T12:00:30Z", "page": "index.html",
            "comment_id": comment_id_1, "kind": "edit",
            "summary": "Made the fox red and swift.",
            "before_snippet": "The quick brown fox jumps over the lazy dog.",
            "after_snippet": "The swift red fox leaps over the sleepy dog.",
            "snapshot_path": snap1_rel, "snapshot_after_path": snap2_rel,
        }
        hist_path = tmpdir / "feedback" / "history.jsonl"
        with open(hist_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(history_entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        time.sleep(0.3)  # let the server's tailer notice

        step("GET /api/inbox -> comment now status 'addressed'")
        inbox = http_json("GET", with_token(base, "/api/inbox", token))
        c = next((c for c in inbox["comments"] if c["id"] == comment_id_1), None)
        assert_true(c is not None, "comment lost from inbox")
        assert_eq(c["status"], "addressed", "comment status after edit")

        step("POST /api/revert {history_id:...} -> 200, file matches BEFORE snapshot")
        http_json("POST", with_token(base, "/api/revert", token), body={"history_id": history_id})
        before_text = snap1_abs.read_text(encoding="utf-8")
        now_text = (tmpdir / "index.html").read_text(encoding="utf-8")
        assert_eq(now_text, before_text, "file content after revert")
        time.sleep(0.3)

        step("GET /api/inbox -> comment back to 'open' after revert")
        inbox = http_json("GET", with_token(base, "/api/inbox", token))
        c = next((c for c in inbox["comments"] if c["id"] == comment_id_1), None)
        assert_true(c is not None, "comment lost after revert")
        assert_eq(c["status"], "open", "comment status after revert")

        step("POST /api/redo {history_id:...} -> 200, file matches AFTER snapshot")
        http_json("POST", with_token(base, "/api/redo", token), body={"history_id": history_id})
        after_text = snap2_abs.read_text(encoding="utf-8")
        now_text = (tmpdir / "index.html").read_text(encoding="utf-8")
        assert_eq(now_text, after_text, "file content after redo")
        time.sleep(0.3)

        step("GET /api/inbox -> comment addressed again after redo")
        inbox = http_json("GET", with_token(base, "/api/inbox", token))
        c = next((c for c in inbox["comments"] if c["id"] == comment_id_1), None)
        assert_true(c is not None, "comment lost after redo")
        assert_eq(c["status"], "addressed", "comment status after redo")

        step("GET /lib/vendor/html2canvas.min.js?t=... -> 200, content-length > 10000")
        _, content = http("GET", with_token(base, "/lib/vendor/html2canvas.min.js", token), expect=200)
        assert_true(len(content) > 10000, f"html2canvas vendor too small: {len(content)} bytes")

        step("GET /lib/vendor/../server.py blocked -> 404")
        # The dispatcher requires a single-segment .js path, so traversal is rejected pre-resolve.
        http("GET", with_token(base, "/lib/vendor/..%2Fserver.py", token), expect=404)

        step("POST /api/screenshot bad comment_id prefix -> 400")
        # 1x1 transparent PNG
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        )
        png_b64 = base64.b64encode(png_bytes).decode("ascii")
        http("POST", with_token(base, "/api/screenshot", token),
             body={"comment_id": "x-bad", "image_b64": png_b64}, expect=400)

        step("POST /api/screenshot unknown field -> 400")
        http("POST", with_token(base, "/api/screenshot", token),
             body={"comment_id": comment_id_1, "image_b64": png_b64, "weird": 1}, expect=400)

        step("POST /api/screenshot valid PNG -> 200, file exists on disk")
        resp = http_json("POST", with_token(base, "/api/screenshot", token),
                         body={"comment_id": comment_id_1, "image_b64": png_b64})
        assert_eq(resp.get("ok"), True, "screenshot ok flag")
        rel = resp.get("screenshot_path")
        assert_eq(rel, f"feedback/.screenshots/{comment_id_1}.png", "screenshot rel path")
        shot_path = tmpdir / rel
        assert_true(shot_path.exists(), f"screenshot not on disk: {shot_path}")
        assert_true(shot_path.read_bytes().startswith(b"\x89PNG"), "screenshot is not PNG")

        step("GET /api/inbox -> comment now carries screenshot_path")
        inbox = http_json("GET", with_token(base, "/api/inbox", token))
        c = next((c for c in inbox["comments"] if c["id"] == comment_id_1), None)
        assert_true(c is not None, "comment lost from inbox before screenshot check")
        assert_eq(c.get("screenshot_path"), f"feedback/.screenshots/{comment_id_1}.png",
                  "screenshot_path missing from inbox comment")

        step("POST /api/screenshot non-PNG bytes -> 400")
        not_png_b64 = base64.b64encode(b"hello world, not a png").decode("ascii")
        http("POST", with_token(base, "/api/screenshot", token),
             body={"comment_id": comment_id_1, "image_b64": not_png_b64}, expect=400)

        step("POST /api/feedback (second comment) -> 200")
        feedback_body_2 = {**feedback_body, "id": f"c-{uuid.uuid4().hex[:12]}",
                           "comment": "Second comment, please review."}
        http_json("POST", with_token(base, "/api/feedback", token), body=feedback_body_2)

        step("POST /api/process {page:'index.html'} -> 200, pending == 1")
        ctrl_path = tmpdir / "feedback" / "control.jsonl"
        before_lines = len(ctrl_path.read_text(encoding="utf-8").splitlines()) if ctrl_path.exists() else 0
        resp = http_json("POST", with_token(base, "/api/process", token), body={"page": "index.html"})
        assert_eq(resp.get("pending"), 1, "process pending count")
        after_lines = len(ctrl_path.read_text(encoding="utf-8").splitlines())
        assert_eq(after_lines, before_lines + 1, "control.jsonl line growth")

        step("POST /api/feedback/delete first comment -> 200, no longer in inbox, screenshot unlinked")
        http_json("POST", with_token(base, "/api/feedback/delete", token), body={"id": comment_id_1})
        inbox = http_json("GET", with_token(base, "/api/inbox", token))
        ids = [c["id"] for c in inbox["comments"]]
        assert_true(comment_id_1 not in ids, f"deleted comment still present: {ids}")
        assert_true(not (tmpdir / "feedback" / ".screenshots" / f"{comment_id_1}.png").exists(),
                    "screenshot file was not unlinked on delete")

        step("POST /api/feedback/clear-addressed {page:'index.html'} -> 200")
        http_json("POST", with_token(base, "/api/feedback/clear-addressed", token), body={"page": "index.html"})

        step("POST /api/shutdown -> proc.poll() not None within 5s")
        http_json("POST", with_token(base, "/api/shutdown", token), body={})
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert_true(proc.poll() is not None, "server did not exit within 5s of /api/shutdown")
        try:
            if proc.stdout:
                proc.stdout.read()
        except Exception:
            pass
        print(f"\nOK all {step_count} steps passed")
        return 0
    finally:
        if proc is not None:
            shutdown_proc(proc)
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
