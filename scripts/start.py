"""Bootstrap the html-feedback server for a target directory."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import inject  # sibling module


SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_PATH = SCRIPT_DIR.parent / "lib" / "server.py"
GITIGNORE_LINE = "feedback/"


def _ensure_dirs(target: Path) -> None:
    (target / "feedback").mkdir(exist_ok=True)
    (target / "feedback" / ".snapshots").mkdir(exist_ok=True)


def _maybe_update_gitignore(target: Path) -> None:
    gi = target / ".gitignore"
    if not gi.exists():
        return
    text = gi.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()]
    if GITIGNORE_LINE in lines or "feedback" in lines:
        return
    sep = "" if text.endswith("\n") or not text else "\n"
    gi.write_text(text + sep + GITIGNORE_LINE + "\n", encoding="utf-8")


def _run_inject(target: Path) -> None:
    rc = inject.main(["inject", "--dir", str(target)])
    if rc != 0:
        raise SystemExit(f"inject failed with code {rc}")


def _spawn_server(target: Path, idle_timeout: int) -> subprocess.Popen:
    cmd = [
        sys.executable,
        str(SERVER_PATH),
        "--dir", str(target),
        "--parent-pid", str(os.getpid()),
        "--idle-timeout", str(idle_timeout),
    ]
    popen_kwargs: dict = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, **popen_kwargs)


def _wait_for_ready(proc: subprocess.Popen) -> str:
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            # Server exited before READY.
            err = proc.stderr.read() if proc.stderr else ""
            sys.stderr.write(err)
            sys.stderr.flush()
            raise SystemExit(proc.wait() or 1)
        line = line.rstrip("\r\n")
        if line.startswith("READY "):
            return line[len("READY "):].strip()
        # Forward any pre-READY chatter so it isn't lost.
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _stream(src, dst) -> None:
    for line in iter(src.readline, ""):
        dst.write(line)
        dst.flush()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Start the html-feedback server.")
    p.add_argument("--dir", required=True, help="Target directory containing HTML files.")
    p.add_argument("--no-open", action="store_true", help="Don't auto-open the browser.")
    p.add_argument("--no-inject", action="store_true", help="Skip auto-injection of lib tags.")
    p.add_argument("--idle-timeout", type=int, default=600, help="Server idle timeout seconds.")
    args = p.parse_args(argv)

    target = Path(args.dir).resolve()
    if not target.is_dir():
        print(f"error: --dir does not exist or is not a directory: {target}", file=sys.stderr)
        return 2
    if not SERVER_PATH.exists():
        print(f"error: server not found at {SERVER_PATH}", file=sys.stderr)
        return 2

    _ensure_dirs(target)
    _maybe_update_gitignore(target)
    if not args.no_inject:
        _run_inject(target)

    proc = _spawn_server(target, args.idle_timeout)
    try:
        url = _wait_for_ready(proc)
    except SystemExit:
        raise
    except Exception as exc:
        proc.terminate()
        print(f"error waiting for server: {exc}", file=sys.stderr)
        return 1

    # Forward server stderr in the background.
    t = threading.Thread(target=_stream, args=(proc.stderr, sys.stderr), daemon=True)
    t.start()

    if not args.no_open:
        webbrowser.open(url)

    print("=" * 60)
    print(f"  html-feedback running")
    print(f"  URL:    {url}")
    print(f"  Dir:    {target}")
    print(f"  Press Ctrl-C to stop.")
    print("=" * 60)
    sys.stdout.flush()

    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        try:
            if os.name == "nt":
                # CTRL_BREAK is deliverable to a process group on Windows.
                try:
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                except OSError:
                    proc.terminate()
            else:
                proc.terminate()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
            rc = proc.wait()
        except Exception:
            rc = 130
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
