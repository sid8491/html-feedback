#!/usr/bin/env python3
# Tails a feedback inbox.jsonl and emits one line per new, unaddressed comment.
# Designed to be driven by Claude Code's Monitor tool — each printed line becomes
# a notification.

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def already_addressed(history_path: Path, comment_id: str) -> bool:
    if not history_path.exists():
        return False
    reverted_snaps: set[str] = set()
    addressed_snaps: dict[str, str] = {}
    try:
        with history_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = e.get("kind")
                snap = e.get("snapshot_path") or ""
                if kind == "revert" and snap:
                    reverted_snaps.add(snap)
                elif kind == "edit" and e.get("comment_id") == comment_id:
                    addressed_snaps[comment_id] = snap
    except OSError:
        return False
    if comment_id in addressed_snaps:
        return addressed_snaps[comment_id] not in reverted_snaps
    return False


def format_event(comment: dict) -> str:
    cid = comment.get("id", "?")
    page = comment.get("page", "?")
    anchor = comment.get("anchor") or {}
    atype = anchor.get("type", "?")
    parent = comment.get("parent_id")
    body = (comment.get("comment") or "").replace("\n", " ").strip()
    if len(body) > 140:
        body = body[:137] + "..."
    sel = anchor.get("selected_text") or ""
    sel = sel.replace("\n", " ").strip()
    if len(sel) > 60:
        sel = sel[:57] + "..."
    parts = [f"NEW {cid}", f"page={page}", f"type={atype}"]
    if parent:
        parts.append(f"reply-to={parent}")
    if sel:
        parts.append(f'on="{sel}"')
    parts.append(f"comment={body!r}")
    return " | ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--history", required=True)
    ap.add_argument("--poll-ms", type=int, default=500)
    args = ap.parse_args()

    inbox = Path(args.inbox)
    history = Path(args.history)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    if not inbox.exists():
        inbox.touch()

    # Start at end of file so we only emit comments added from here on.
    pos = inbox.stat().st_size
    print(f"WATCH started inbox={inbox} from byte {pos}", flush=True)

    poll = max(0.1, args.poll_ms / 1000.0)
    seen: set[str] = set()
    while True:
        try:
            size = inbox.stat().st_size
            if size < pos:
                # File was truncated or rewritten; reset.
                pos = 0
            if size > pos:
                with inbox.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for raw in chunk.splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        c = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    cid = c.get("id")
                    if not cid or cid in seen:
                        continue
                    if already_addressed(history, cid):
                        seen.add(cid)
                        continue
                    seen.add(cid)
                    print(format_event(c), flush=True)
            time.sleep(poll)
        except KeyboardInterrupt:
            return 0
        except Exception as ex:  # noqa: BLE001 - keep the watcher alive
            print(f"WARN watcher exception (continuing): {ex!r}", flush=True)
            time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
