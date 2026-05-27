"""Inject/remove/status the html-feedback lib tags in HTML files."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BLOCK = (
    "<!-- hfb:begin -->\n"
    '<link rel="stylesheet" href="/lib/feedback.css">\n'
    '<script defer src="/lib/feedback.js"></script>\n'
    "<!-- hfb:end -->"
)

# Non-greedy match for the marker block; DOTALL so it can span lines.
BLOCK_RE = re.compile(
    r"[ \t]*<!--\s*hfb:begin\s*-->.*?<!--\s*hfb:end\s*-->[ \t]*\n?",
    re.DOTALL | re.IGNORECASE,
)
HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _read(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    return text, text.endswith("\n")


def _atomic_write(path: Path, text: str, trailing_newline: bool) -> None:
    if trailing_newline and not text.endswith("\n"):
        text += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def _insert_block(text: str) -> str:
    block_with_nl = BLOCK + "\n"
    m = HEAD_CLOSE_RE.search(text)
    if m:
        return text[: m.start()] + block_with_nl + text[m.start() :]
    m = BODY_CLOSE_RE.search(text)
    if m:
        return text[: m.start()] + block_with_nl + text[m.start() :]
    # No </head> or </body>; append at end.
    sep = "" if text.endswith("\n") or not text else "\n"
    return text + sep + block_with_nl


def inject_file(path: Path) -> str:
    """Return one of: 'updated', 'inserted', 'unchanged'."""
    text, had_newline = _read(path)
    if BLOCK_RE.search(text):
        new_text = BLOCK_RE.sub(BLOCK + "\n", text, count=1)
        status = "unchanged" if new_text == text else "updated"
    else:
        new_text = _insert_block(text)
        status = "inserted"
    if new_text != text:
        _atomic_write(path, new_text, had_newline)
    return status


def remove_file(path: Path) -> bool:
    text, had_newline = _read(path)
    if not BLOCK_RE.search(text):
        return False
    new_text = BLOCK_RE.sub("", text)
    _atomic_write(path, new_text, had_newline)
    return True


def is_injected(path: Path) -> bool:
    return bool(BLOCK_RE.search(path.read_text(encoding="utf-8")))


def _walk(root: Path):
    for p in sorted(root.rglob("*.html")):
        if not p.is_file():
            continue
        # Skip the html-feedback working tree so we don't inject into snapshots.
        if "feedback" in p.relative_to(root).parts:
            continue
        yield p


def cmd_inject(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    for p in _walk(root):
        status = inject_file(p)
        print(f"{status:>9}  {p.relative_to(root)}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    for p in _walk(root):
        changed = remove_file(p)
        print(f"{'removed' if changed else 'clean':>9}  {p.relative_to(root)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    rows = []
    for p in _walk(root):
        text = p.read_text(encoding="utf-8")
        rows.append((str(p.relative_to(root)), "Y" if BLOCK_RE.search(text) else "N", text.count("\n") + (0 if text.endswith("\n") else 1)))
    path_w = max((len(r[0]) for r in rows), default=4)
    path_w = max(path_w, len("path"))
    print(f"{'path':<{path_w}}  {'injected':<8}  {'lines':>6}")
    print(f"{'-' * path_w}  {'-' * 8}  {'-' * 6}")
    for path_s, inj, lines in rows:
        print(f"{path_s:<{path_w}}  {inj:<8}  {lines:>6}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Inject html-feedback tags into HTML files.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inject", help="Inject lib tags into all HTML files.")
    pi.add_argument("--dir", required=True)
    pi.add_argument("--token-source", default="url", choices=["url"])
    pi.set_defaults(func=cmd_inject)

    pr = sub.add_parser("remove", help="Remove the injected block from all HTML files.")
    pr.add_argument("--dir", required=True)
    pr.set_defaults(func=cmd_remove)

    ps = sub.add_parser("status", help="Show injection status for all HTML files.")
    ps.add_argument("--dir", required=True)
    ps.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
