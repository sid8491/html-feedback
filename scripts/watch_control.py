#!/usr/bin/env python3
# Tails feedback/control.jsonl and emits one PROCESS event per user-triggered
# batch. Designed to be driven by Claude Code's Monitor tool.

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def already_addressed(history_entries: list[dict], cid: str) -> bool:
    reverted_snaps: set[str] = set()
    last_edit_snap: str | None = None
    for e in history_entries:
        kind = e.get("kind")
        snap = e.get("snapshot_path") or ""
        if kind == "revert" and snap:
            reverted_snaps.add(snap)
        elif kind == "edit" and e.get("comment_id") == cid:
            last_edit_snap = snap
    if last_edit_snap is None:
        return False
    return last_edit_snap not in reverted_snaps


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def format_event(ids: list[str], inbox_path: Path, history_path: Path) -> str:
    if not ids:
        return "PROCESS empty - no open comments"
    inbox = {c.get("id"): c for c in read_jsonl(inbox_path)}
    history = read_jsonl(history_path)
    parts: list[str] = []
    for cid in ids:
        c = inbox.get(cid)
        if not c:
            parts.append(f"{cid}=(missing)")
            continue
        if already_addressed(history, cid):
            continue
        anchor = c.get("anchor") or {}
        atype = anchor.get("type", "?")
        page = c.get("page", "?")
        body = (c.get("comment") or "").replace("\n", " ").strip()
        if len(body) > 100:
            body = body[:97] + "..."
        sel = (anchor.get("selected_text") or "").replace("\n", " ").strip()
        if len(sel) > 40:
            sel = sel[:37] + "..."
        parent = c.get("parent_id")
        ptag = f" reply-to={parent}" if parent else ""
        on = f' on="{sel}"' if sel else ""
        parts.append(f"{cid} [{page} {atype}{ptag}]{on}: {body!r}")
    header = f"PROCESS {len(parts)} comments:"
    return header + "\n" + "\n".join(f"  - {p}" for p in parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", required=True)
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--history", required=True)
    ap.add_argument("--poll-ms", type=int, default=400)
    args = ap.parse_args()

    control = Path(args.control)
    inbox = Path(args.inbox)
    history = Path(args.history)
    control.parent.mkdir(parents=True, exist_ok=True)
    if not control.exists():
        control.touch()

    pos = control.stat().st_size
    print(f"WATCH started control={control} from byte {pos}", flush=True)

    poll = max(0.1, args.poll_ms / 1000.0)
    while True:
        try:
            size = control.stat().st_size
            if size < pos:
                pos = 0
            if size > pos:
                with control.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for raw in chunk.splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") != "process":
                        continue
                    ids = ev.get("pending_ids") or []
                    print(format_event(ids, inbox, history), flush=True)
            time.sleep(poll)
        except KeyboardInterrupt:
            return 0
        except Exception as ex:  # noqa: BLE001
            print(f"WARN watcher exception (continuing): {ex!r}", flush=True)
            time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
