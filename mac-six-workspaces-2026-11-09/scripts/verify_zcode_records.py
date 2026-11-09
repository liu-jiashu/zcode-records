#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify a ZCode JSONL export against its read-only source snapshot.

This script checks structure, source parity (after the same deterministic
redaction), file hashes, parent/child metadata, and residual credential shapes.
It never prints or writes credential values.
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import export_zcode as ex  # noqa: E402


def die(msg: str) -> int:
    print("FAIL: " + msg, file=sys.stderr)
    return 1


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.endswith("\n"):
                raise ValueError(f"{path}:{lineno}: missing trailing newline")
            out.append(json.loads(line))
    return out


def compare_struct(a: Any, b: Any, path: str = "") -> Optional[str]:
    if type(a) is not type(b):
        return f"{path}: type {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        if list(a.keys()) != list(b.keys()):
            return f"{path}: key order/content differs"
        for k in a:
            err = compare_struct(a[k], b[k], path + "." + str(k))
            if err:
                return err
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            err = compare_struct(x, y, f"{path}[{i}]")
            if err:
                return err
        return None
    if a != b:
        return f"{path}: value differs"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--tasks-db", required=True)
    args = ap.parse_args()

    root = Path(args.export).resolve()
    db_path = Path(args.db).resolve()
    tasks_path = Path(args.tasks_db).resolve()
    manifest = json.load((root / "MANIFEST.json").open(encoding="utf-8"))
    failures: List[str] = []

    # 1) File hashes in manifest.
    for item in manifest.get("files", []):
        rel = item.get("path") or item.get("file")
        if not rel:
            failures.append("manifest file entry without path")
            continue
        fp = root / rel
        if not fp.exists():
            failures.append(f"missing file {rel}")
            continue
        h = hashlib.sha256(fp.read_bytes()).hexdigest()
        if h != item["sha256"]:
            failures.append(f"hash mismatch {rel}")
        if fp.stat().st_size != item["bytes"]:
            failures.append(f"size mismatch {rel}")

    # 2) Source inventory with the same selection logic as the exporter.
    con = ex.connect_ro(db_path)
    tasks_con = ex.connect_ro(tasks_path)
    target_paths = [p for _, p in ex.TARGET_WORKSPACES]
    archived_paths = [p for _, p in ex.RELATED_ARCHIVED_WORKSPACES]
    target_sessions = ex.load_sessions(con, target_paths)
    archived_sessions = ex.load_related_archived(con, tasks_con, archived_paths)
    all_sessions = sorted(target_sessions + archived_sessions, key=lambda r: (r["time_created"], r["id"]))
    sids = [s["id"] for s in all_sessions]
    qm = ex.placeholders(len(sids))
    tasks = ex.load_tasks(tasks_con, sids, target_paths, archived_paths)
    task_by_id = {t["task_id"]: t for t in tasks}
    messages = ex.fetch_all(
        con, f"SELECT * FROM message WHERE session_id IN ({qm}) ORDER BY session_id, sequence, time_created, id", sids
    )
    parts = ex.fetch_all(
        con, f"SELECT * FROM part WHERE session_id IN ({qm}) ORDER BY session_id, message_id, sequence, time_created, id", sids
    )
    inputs = ex.fetch_all(
        con, f"SELECT * FROM session_input WHERE session_id IN ({qm}) ORDER BY session_id, time_created, id", sids
    )
    entries = ex.fetch_all(
        con, f"SELECT * FROM session_entry WHERE session_id IN ({qm}) ORDER BY session_id, time_created, id", sids
    )
    usage = ex.fetch_all(
        con, f"SELECT * FROM model_usage WHERE session_id IN ({qm}) ORDER BY session_id, started_at, id", sids
    )
    parts_by_message = collections.defaultdict(list)
    for p in parts:
        parts_by_message[p["message_id"]].append(p)
    messages_by_session = collections.defaultdict(list)
    for m in messages:
        messages_by_session[m["session_id"]].append(m)

    pre_objects: List[Any] = []
    pre_objects.extend(ex.row_to_dict(x) for x in all_sessions)
    pre_objects.extend(ex.row_to_dict(x) for x in tasks)
    pre_objects.extend(ex.json_loads_maybe(x["data"]) for x in messages)
    pre_objects.extend(ex.json_loads_maybe(x["data"]) for x in parts)
    pre_objects.extend(ex.json_loads_maybe(x["payload"]) for x in inputs)
    pre_objects.extend(ex.json_loads_maybe(x["data"]) for x in entries)
    pre_objects.extend(ex.row_to_dict(x) for x in usage)
    known_secrets = ex.collect_secret_literals(pre_objects)
    redactor = ex.Redactor(known_secrets)

    if len(all_sessions) != manifest["counts"]["sessions_exported_total"]:
        failures.append("source/export session count differs")
    if len(messages) != manifest["counts"]["messages_exported"]:
        failures.append("source/export message count differs")
    if len(parts) != manifest["counts"]["parts_exported"]:
        failures.append("source/export part count differs")

    # 3) Parse each session file and compare against source.
    files = sorted(glob.glob(str(root / "workspaces/*/sessions/*.jsonl")))
    seen_sessions = set()
    seen_messages = set()
    seen_parts = set()
    total_messages = 0
    total_parts = 0
    all_export_text_chunks: List[str] = []

    for fp_str in files:
        fp = Path(fp_str)
        records = read_jsonl(fp)
        if not records or records[0].get("record_type") != "session_header":
            failures.append(f"{fp}: missing session_header")
            continue
        if records[-1].get("record_type") != "session_footer":
            failures.append(f"{fp}: missing session_footer")
            continue
        all_export_text_chunks.append(fp.read_text(encoding="utf-8"))
        header = records[0]
        footer = records[-1]
        sid = header["session_id"]
        seen_sessions.add(sid)
        if sid not in set(sids):
            failures.append(f"{fp}: unknown session id")
            continue
        s = next(x for x in all_sessions if x["id"] == sid)
        expected_header_session = redactor.obj(ex.row_to_dict(s), f"session[{sid}]")
        err = compare_struct(header["session"], expected_header_session)
        if err:
            failures.append(f"{fp}: session metadata differs at {err}")
        expected_task = redactor.obj(ex.row_to_dict(task_by_id[sid]), f"session[{sid}].task") if sid in task_by_id else None
        err = compare_struct(header.get("task"), expected_task)
        if err:
            failures.append(f"{fp}: task metadata differs at {err}")

        mids = messages_by_session.get(sid, [])
        if len(records) != len(mids) + 2:
            failures.append(f"{fp}: record count {len(records)} != messages+2 {len(mids)+2}")
        if footer.get("counts") != header.get("counts"):
            failures.append(f"{fp}: footer/header counts differ")

        for idx, m in enumerate(mids):
            if idx + 1 >= len(records):
                failures.append(f"{fp}: missing message {m['id']}")
                continue
            rec = records[idx + 1]
            if rec.get("record_type") != "message":
                failures.append(f"{fp}: unexpected record at {idx+1}")
                continue
            if rec.get("message_id") != m["id"]:
                failures.append(f"{fp}: message order/id mismatch at line {idx+2}")
                continue
            seen_messages.add(m["id"])
            total_messages += 1
            if rec.get("sequence") != m["sequence"]:
                failures.append(f"{fp}: message sequence mismatch {m['id']}")
            if rec.get("time_created_ms") != m["time_created"] or rec.get("time_updated_ms") != m["time_updated"]:
                failures.append(f"{fp}: message timestamps mismatch {m['id']}")
            md = ex.json_loads_maybe(m["data"])
            expected_md = redactor.obj(md, f"message[{m['id']}]")
            err = compare_struct(rec.get("message"), expected_md)
            if err:
                failures.append(f"{fp}: message data differs {m['id']}: {err}")
            src_parts = sorted(parts_by_message.get(m["id"], []), key=lambda x: (x["sequence"], x["time_created"], x["id"]))
            exp_parts = rec.get("parts", [])
            if len(src_parts) != len(exp_parts):
                failures.append(f"{fp}: part count mismatch {m['id']}")
                continue
            for j, p in enumerate(src_parts):
                pr = exp_parts[j]
                if pr.get("part_id") != p["id"]:
                    failures.append(f"{fp}: part order/id mismatch {m['id']}")
                    continue
                seen_parts.add(p["id"])
                total_parts += 1
                if pr.get("sequence") != p["sequence"]:
                    failures.append(f"{fp}: part sequence mismatch {p['id']}")
                if pr.get("time_created_ms") != p["time_created"] or pr.get("time_updated_ms") != p["time_updated"]:
                    failures.append(f"{fp}: part timestamps mismatch {p['id']}")
                pd = ex.json_loads_maybe(p["data"])
                expected_pd = redactor.obj(pd, f"part[{p['id']}]")
                err = compare_struct(pr.get("part"), expected_pd)
                if err:
                    failures.append(f"{fp}: part data differs {p['id']}: {err}")

    if seen_sessions != set(sids):
        failures.append(f"session set differs: missing={len(set(sids)-seen_sessions)} extra={len(seen_sessions-set(sids))}")
    if len(seen_messages) != len(messages):
        failures.append(f"message set differs: {len(seen_messages)}/{len(messages)}")
    if len(seen_parts) != len(parts):
        failures.append(f"part set differs: {len(seen_parts)}/{len(parts)}")
    if total_messages != len(messages) or total_parts != len(parts):
        failures.append("recounted message/part totals differ")

    # 4) No literal high-confidence secret from the source may survive.
    export_text = "\n".join(all_export_text_chunks)
    for secret in known_secrets:
        if secret in export_text:
            failures.append(f"residual high-confidence secret of length {len(secret)}")

    # 5) Residual token-shape scan (does not treat REDACTED markers as secret).
    residual_patterns = {
        "private_key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        "anthropic": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
        "openai": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
        "github": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
        "slack": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
        "aws": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "google": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
        "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
        "encrypted": re.compile(r"\benc:v1:[A-Za-z0-9._~+/=\-]{8,}"),
        "creator": re.compile(r"\bcrk_[A-Za-z0-9_\-]{12,}"),
        "bearer": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{16,}"),
        "url_credentials": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@[^\s\"'<>]+"),
    }
    residuals = collections.Counter()
    for name, pat in residual_patterns.items():
        for m in pat.finditer(export_text):
            if "[REDACTED:" in m.group(0):
                continue
            residuals[name] += 1
    if residuals:
        failures.append(f"residual token shapes: {dict(residuals)}")

    # 6) Raw databases / credential files must not be in the deliverable.
    forbidden = []
    for pattern in ("**/*.sqlite", "**/*.sqlite-wal", "**/*.sqlite-shm", "**/apikey.txt", "**/jwt.txt", "**/credentials.json"):
        for fp in root.glob(pattern):
            if ".work" in fp.parts:
                continue
            forbidden.append(str(fp.relative_to(root)))
    if forbidden:
        failures.append("raw database/credential files in deliverable: " + ", ".join(forbidden[:5]))

    # 7) Basic manifest invariants.
    c = manifest["counts"]
    if c["target_sessions_identified"] != c["target_sessions_exported"]:
        failures.append("target session counts differ in manifest")
    if c["sessions_exported_total"] != c["target_sessions_exported"] + c["related_archived_sessions_exported"]:
        failures.append("session total does not add up")
    if [s["session_id"] for s in manifest["sessions"]] != [r["session_id"] for r in sorted(manifest["sessions"], key=lambda x: x["time_created_ms"])]:
        failures.append("manifest session order is not chronological")

    print(json.dumps({
        "ok": not failures,
        "sessions": len(seen_sessions),
        "messages": total_messages,
        "parts": total_parts,
        "known_secret_literals_checked": len(known_secrets),
        "failures": failures[:20],
        "failure_count": len(failures),
    }, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
