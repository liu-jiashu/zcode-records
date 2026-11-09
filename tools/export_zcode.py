from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "zcode.session-export.v1"
WORKSPACES = [
    {"slug": "shop", "path": r"D:\shop"},
    {"slug": "篡改", "path": r"D:\篡改"},
]
DEFAULT_SOURCE = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
DEFAULT_TASKS = Path.home() / ".zcode" / "v2" / "tasks-index.sqlite"
DEFAULT_OUT = Path(r"D:\zcode-records-work\raw-export")
MAX_SHARD_BYTES = 8_000_000


def iso_ms(value: Any) -> str | None:
    if value is None:
        return None
    return (
        datetime.fromtimestamp(int(value) / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dumps_line(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def parse_json(raw: str, warnings: list[str], label: str) -> Any:
    try:
        return json.loads(raw)
    except Exception as exc:  # pragma: no cover - defensive
        warnings.append(f"{label}: JSON parse failed: {type(exc).__name__}: {exc}")
        return {"__parse_error__": f"{type(exc).__name__}: {exc}", "__raw__": raw}


def row_dict(row: sqlite3.Row, exclude: set[str] | None = None) -> dict[str, Any]:
    exclude = exclude or set()
    return {k: row[k] for k in row.keys() if k not in exclude}


def select_sessions(con: sqlite3.Connection) -> list[sqlite3.Row]:
    exact: list[sqlite3.Row] = []
    for ws in WORKSPACES:
        exact.extend(
            con.execute(
                "SELECT * FROM session WHERE directory = ? ORDER BY time_created, id",
                (ws["path"],),
            ).fetchall()
        )
    seen = {r["id"] for r in exact}
    frontier = list(seen)
    descendants: list[sqlite3.Row] = []
    while frontier:
        placeholders = ",".join("?" * len(frontier))
        rows = con.execute(
            f"SELECT * FROM session WHERE parent_id IN ({placeholders}) ORDER BY time_created, id",
            frontier,
        ).fetchall()
        new_frontier: list[str] = []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            descendants.append(row)
            new_frontier.append(row["id"])
        frontier = new_frontier
    # Sort the combined set by creation time while retaining exact roots.
    return sorted(exact + descendants, key=lambda r: (r["time_created"], r["id"]))


def session_workspace(row: sqlite3.Row) -> dict[str, str]:
    for ws in WORKSPACES:
        p = ws["path"].lower()
        d = (row["directory"] or "").lower()
        if d == p or d.startswith(p + "\\"):
            return ws
    # A descendant in a different directory is attached to the workspace of its
    # nearest ancestor. select_sessions() only adds such a row through a target
    # parent, so use the first workspace as a documented fallback.
    return WORKSPACES[0]


def collect_aux_counts(con: sqlite3.Connection, session_id: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in ("session_input", "input_history", "model_usage", "tool_usage", "session_entry"):
        result[table] = con.execute(
            f"SELECT count(*) FROM {table} WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    return result


def collect_entry_types(con: sqlite3.Connection, session_id: str) -> dict[str, int]:
    return {
        row[0]: row[1]
        for row in con.execute(
            "SELECT type, count(*) FROM session_entry WHERE session_id = ? GROUP BY type",
            (session_id,),
        ).fetchall()
    }


def build_session_export(
    con: sqlite3.Connection,
    tasks: dict[str, sqlite3.Row],
    row: sqlite3.Row,
    workspace: dict[str, str],
    snapshot_sha: str,
    max_shard_bytes: int,
) -> dict[str, Any]:
    sid = row["id"]
    warnings: list[str] = []
    messages = con.execute(
        "SELECT * FROM message WHERE session_id = ? ORDER BY sequence, time_created, id",
        (sid,),
    ).fetchall()
    parts = con.execute(
        "SELECT * FROM part WHERE session_id = ? ORDER BY message_id, sequence, time_created, id",
        (sid,),
    ).fetchall()

    parts_by_message: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for p in parts:
        parts_by_message[p["message_id"]].append(p)
    for plist in parts_by_message.values():
        plist.sort(key=lambda p: (p["sequence"] if p["sequence"] is not None else 10**9, p["time_created"], p["id"]))

    parse_errors = 0
    message_data: dict[str, Any] = {}
    part_data: dict[str, Any] = {}
    for m in messages:
        message_data[m["id"]] = parse_json(m["data"], warnings, f"message {m['id']}")
    for p in parts:
        part_data[p["id"]] = parse_json(p["data"], warnings, f"part {p['id']}")
    parse_errors = sum(1 for w in warnings if "JSON parse failed" in w)

    stats = Counter()
    part_type_counts: Counter[str] = Counter()
    tool_names: Counter[str] = Counter()
    missing_reasoning_messages: list[str] = []
    empty_output_parts: list[str] = []
    error_parts: list[str] = []
    missing_result_parts: list[str] = []
    truncated_parts: list[str] = []
    metadata_only_files = 0
    artifact_files = 0
    inline_preview_files = 0
    local_ref_files = 0
    other_files = 0
    attachment_records: list[dict[str, Any]] = []

    orders: dict[tuple[str, str], int] = {}
    order = 1
    groups: list[dict[str, Any]] = []
    total_message_chars = 0
    total_reasoning_chars = 0

    for m in messages:
        md = message_data[m["id"]]
        role = md.get("role") if isinstance(md, dict) else None
        origin = ((md.get("semantics") or {}).get("origin") if isinstance(md, dict) else None)
        kind = ((md.get("semantics") or {}).get("kind") if isinstance(md, dict) else None)
        parts_here = parts_by_message.get(m["id"], [])
        msg_parts_summary = Counter()
        msg_reasoning = 0
        msg_reasoning_chars = 0
        msg_tool = 0
        msg_tool_error = 0
        msg_attachment = 0

        message_order = order
        order += 1
        orders[("message", m["id"])] = message_order
        for p in parts_here:
            pd = part_data[p["id"]]
            ptype = pd.get("type") if isinstance(pd, dict) else None
            msg_parts_summary[str(ptype)] += 1
            part_type_counts[str(ptype)] += 1
            if ptype == "reasoning":
                msg_reasoning += 1
                text = pd.get("text") if isinstance(pd, dict) else None
                if isinstance(text, str):
                    msg_reasoning_chars += len(text)
                    if text == "":
                        stats["empty_reasoning_parts"] += 1
            if ptype == "tool":
                msg_tool += 1
                st = pd.get("state") or {}
                if st.get("status") == "error":
                    msg_tool_error += 1
            if ptype == "file":
                msg_attachment += 1
            orders[("part", p["id"])] = order
            order += 1

        if role == "assistant" and msg_reasoning == 0:
            missing_reasoning_messages.append(m["id"])
        stats[f"role_{role}"] += 1
        stats[f"origin_{origin}"] += 1
        if role == "user" and origin == "real_user":
            stats["real_user_messages"] += 1
        if role == "user" and origin == "agent_runtime":
            stats["runtime_user_messages"] += 1
        if role == "user" and origin == "system":
            stats["system_user_messages"] += 1
        if role == "assistant" and msg_reasoning > 0:
            stats["assistant_messages_with_reasoning"] += 1
        total_reasoning_chars += msg_reasoning_chars

        text_chars = 0
        for p in parts_here:
            pd = part_data[p["id"]]
            if isinstance(pd, dict) and pd.get("type") == "text" and isinstance(pd.get("text"), str):
                text_chars += len(pd["text"])
        total_message_chars += text_chars

        msg_obj = {
            "record_type": "message",
            "schema_version": SCHEMA,
            "workspace": workspace["slug"],
            "workspace_path": workspace["path"],
            "session_id": sid,
            "parent_session_id": row["parent_id"],
            "task_type": row["task_type"],
            "order": message_order,
            "message_id": m["id"],
            "sequence": m["sequence"],
            "time_created": iso_ms(m["time_created"]),
            "time_updated": iso_ms(m["time_updated"]),
            "time_created_ms": m["time_created"],
            "role": role,
            "origin": origin,
            "kind": kind,
            "data": md,
            "parts_summary": {
                "count": len(parts_here),
                "types": dict(msg_parts_summary),
                "reasoning_parts": msg_reasoning,
                "reasoning_chars": msg_reasoning_chars,
                "tool_calls": msg_tool,
                "tool_errors": msg_tool_error,
                "attachments": msg_attachment,
            },
            "completeness": {
                "parts_present": len(parts_here) > 0,
                "reasoning_present": msg_reasoning > 0,
                "assistant_without_reasoning": bool(role == "assistant" and msg_reasoning == 0),
            },
            "source": {"database": "~/.zcode/cli/db/db.sqlite", "table": "message"},
        }
        lines = [dumps_line(msg_obj)]
        group_first = message_order
        group_last = message_order

        for p in parts_here:
            pd = part_data[p["id"]]
            ptype = pd.get("type") if isinstance(pd, dict) else None
            p_order = orders[("part", p["id"])]
            group_last = p_order
            st = pd.get("state") if isinstance(pd, dict) and isinstance(pd.get("state"), dict) else {}
            ser = ((st.get("metadata") or {}).get("serialization") or {}) if st else {}
            is_tool = ptype == "tool"
            has_output = "output" in st if is_tool else None
            output_empty = bool(is_tool and has_output and st.get("output") in (None, ""))
            has_error = bool(is_tool and st.get("error"))
            result_missing = bool(is_tool and not has_output and not has_error)
            is_error = bool(is_tool and st.get("status") == "error")
            is_truncated = bool(is_tool and ser.get("truncated"))
            if is_tool:
                tool_names[str(pd.get("tool"))] += 1
                stats["tool_calls"] += 1
                if is_error:
                    stats["tool_errors"] += 1
                    error_parts.append(p["id"])
                if result_missing:
                    stats["tool_result_missing"] += 1
                    missing_result_parts.append(p["id"])
                if output_empty:
                    stats["tool_result_empty"] += 1
                    empty_output_parts.append(p["id"])
                if is_truncated:
                    stats["tool_truncated"] += 1
                    truncated_parts.append(p["id"])
                if st.get("status") == "completed":
                    stats["tool_completed"] += 1

            completeness = {
                "empty_text": bool(
                    ptype in ("text", "reasoning")
                    and isinstance(pd.get("text") if isinstance(pd, dict) else None, str)
                    and pd.get("text") == ""
                ),
                "tool_result_missing": result_missing,
                "tool_result_empty": output_empty,
                "tool_error": is_error,
                "tool_output_truncated": is_truncated,
            }
            part_obj = {
                "record_type": "part",
                "schema_version": SCHEMA,
                "workspace": workspace["slug"],
                "workspace_path": workspace["path"],
                "session_id": sid,
                "parent_session_id": row["parent_id"],
                "task_type": row["task_type"],
                "order": p_order,
                "message_id": p["message_id"],
                "part_id": p["id"],
                "sequence": p["sequence"],
                "part_type": ptype,
                "time_created": iso_ms(p["time_created"]),
                "time_updated": iso_ms(p["time_updated"]),
                "time_created_ms": p["time_created"],
                "data": pd,
                "completeness": completeness,
                "source": {"database": "~/.zcode/cli/db/db.sqlite", "table": "part"},
            }
            if is_tool:
                part_obj["tool"] = {
                    "name": pd.get("tool"),
                    "call_id": pd.get("callID"),
                    "status": st.get("status"),
                    "has_result": has_output,
                    "result_empty": output_empty,
                    "result_missing": result_missing,
                    "error": is_error,
                    "output_truncated": is_truncated,
                }
            if ptype == "file":
                meta = pd.get("metadata") or {}
                recovery = meta.get("recoverability")
                storage = meta.get("storageKind")
                if recovery == "metadata_only":
                    metadata_only_files += 1
                elif storage == "artifact":
                    artifact_files += 1
                elif storage == "inline":
                    inline_preview_files += 1
                elif storage == "local_ref":
                    local_ref_files += 1
                else:
                    other_files += 1
                preview = meta.get("preview") or {}
                attachment_records.append(
                    {
                        "part_id": p["id"],
                        "message_id": p["message_id"],
                        "filename": pd.get("filename"),
                        "mime": pd.get("mime"),
                        "recoverability": recovery,
                        "storage_kind": storage,
                        "size_bytes": meta.get("sizeBytes"),
                        "artifact_uri": meta.get("artifactUri") or pd.get("url"),
                        "preview_text_included": bool(storage == "inline" and preview.get("text")),
                        "preview_truncated": preview.get("truncated"),
                        "binary_content_embedded": False,
                    }
                )
            lines.append(dumps_line(part_obj))
        group_bytes = sum(len(line.encode("utf-8")) for line in lines)
        groups.append(
            {
                "lines": lines,
                "bytes": group_bytes,
                "first_order": group_first,
                "last_order": group_last,
                "message_id": m["id"],
            }
        )

    stats["messages"] = len(messages)
    stats["parts"] = len(parts)
    stats["assistant_messages"] = sum(1 for m in messages if message_data[m["id"]].get("role") == "assistant")
    stats["assistant_messages_without_reasoning"] = len(missing_reasoning_messages)
    stats["file_parts"] = sum(v for k, v in part_type_counts.items() if k == "file")
    stats["metadata_only_files"] = metadata_only_files
    stats["artifact_files"] = artifact_files
    stats["inline_preview_files"] = inline_preview_files
    stats["local_ref_files"] = local_ref_files
    stats["other_files"] = other_files
    stats["parse_errors"] = parse_errors
    stats["total_message_text_chars"] = total_message_chars
    stats["total_reasoning_chars"] = total_reasoning_chars
    stats["messages_without_parts"] = sum(1 for m in messages if not parts_by_message.get(m["id"]))
    stats["real_user_messages"] += 0

    task_row = tasks.get(sid)
    task_meta = None
    if task_row is not None:
        raw_meta = task_row["meta_json"]
        try:
            task_meta = json.loads(raw_meta or "{}")
        except Exception as exc:
            warnings.append(f"task {sid}: meta_json parse failed: {type(exc).__name__}: {exc}")
        task = {
            "workspace_key": task_row["workspace_key"],
            "workspace_path": task_row["workspace_path"],
            "task_id": task_row["task_id"],
            "title": task_row["title"],
            "task_status": task_row["task_status"],
            "provider": task_row["provider"],
            "mode": task_row["mode"],
            "model": task_row["model"],
            "forked_from_task_id": task_row["forked_from_task_id"],
            "created_at": iso_ms(task_row["created_at"]),
            "updated_at": iso_ms(task_row["updated_at"]),
            "archived": bool(task_row["archived"]),
            "deleted": bool(task_row["deleted"]),
            "pinned": bool(task_row["pinned"]),
            "title_overridden": bool(task_row["title_overridden"]),
            "cron_automation_id": task_row["cron_automation_id"],
            "off_peak_task_id": task_row["off_peak_task_id"],
            "meta": task_meta,
            "searchable_text_omitted": True,
            "searchable_text_bytes": len((task_row["searchable_text"] or "").encode("utf-8")),
            "searchable_text_sha256": sha256_text(task_row["searchable_text"] or ""),
        }
    else:
        task = None

    parent_title = None
    if row["parent_id"]:
        pr = con.execute("SELECT title FROM session WHERE id = ?", (row["parent_id"],)).fetchone()
        parent_title = pr[0] if pr else None

    archived = bool(row["time_archived"]) or bool(task and task["archived"])
    all_start = min((m["time_created"] for m in messages), default=row["time_created"])
    all_end = max((m["time_updated"] for m in messages), default=row["time_updated"])

    session_meta = {
        "record_type": "session",
        "schema_version": SCHEMA,
        "workspace": workspace["slug"],
        "workspace_path": workspace["path"],
        "session_id": sid,
        "session": row_dict(row),
        "session_created_at": iso_ms(row["time_created"]),
        "session_updated_at": iso_ms(row["time_updated"]),
        "session_archived_at": iso_ms(row["time_archived"]),
        "archived": archived,
        "task_index": task,
        "relationship": {
            "parent_session_id": row["parent_id"],
            "parent_session_title": parent_title,
            "task_type": row["task_type"],
            "is_child": bool(row["parent_id"]),
            "task_index_present": task is not None,
            "forked_from_task_id": task["forked_from_task_id"] if task else None,
        },
        "completeness": {
            "source_message_count": len(messages),
            "source_part_count": len(parts),
            "message_count_exported": len(messages),
            "part_count_exported": len(parts),
            "real_user_message_count": stats["real_user_messages"],
            "runtime_user_message_count": stats["runtime_user_messages"],
            "system_origin_message_count": stats["system_user_messages"],
            "assistant_message_count": stats["assistant_messages"],
            "assistant_messages_with_reasoning": stats["assistant_messages_with_reasoning"],
            "assistant_messages_without_reasoning": stats["assistant_messages_without_reasoning"],
            "messages_without_parts": stats["messages_without_parts"],
            "reasoning_parts": part_type_counts.get("reasoning", 0),
            "reasoning_chars": stats["total_reasoning_chars"],
            "empty_reasoning_parts": stats["empty_reasoning_parts"],
            "tool_calls": stats["tool_calls"],
            "tool_completed": stats["tool_completed"],
            "tool_errors": stats["tool_errors"],
            "tool_result_missing": stats["tool_result_missing"],
            "tool_result_empty": stats["tool_result_empty"],
            "tool_output_truncated": stats["tool_truncated"],
            "file_parts": part_type_counts.get("file", 0),
            "file_parts_metadata_only": metadata_only_files,
            "file_parts_artifact_ref": artifact_files,
            "file_parts_inline_preview": inline_preview_files,
            "file_parts_local_ref": local_ref_files,
            "file_binary_content_exported": False,
            "file_inline_preview_text_included": True,
            "parse_errors": parse_errors,
        },
        "part_types": dict(part_type_counts),
        "tool_names": dict(tool_names),
        "auxiliary_source_counts": collect_aux_counts(con, sid),
        "session_entry_types": collect_entry_types(con, sid),
        "attachments": attachment_records,
        "not_exported": {
            "tables": ["session_entry", "session_input", "input_history", "model_usage", "tool_usage"],
            "reason": (
                "The export scope is the conversation stream (message + part). Runtime state, queue "
                "ledger, token usage, and tool telemetry are counted but not duplicated in JSONL."
            ),
            "binary_attachments": (
                "Attachment metadata and artifact URIs are retained; binary content is not embedded."
            ),
        },
        "extraction": {
            "status": "complete" if parse_errors == 0 else "partial",
            "warnings": warnings,
            "parse_errors": parse_errors,
            "source_snapshot_sha256": snapshot_sha,
            "source_tables": ["session", "message", "part"],
            "message_text_chars": total_message_chars,
            "reasoning_chars": total_reasoning_chars,
        },
        "conversation_time_range": {
            "start_utc": iso_ms(all_start),
            "end_utc": iso_ms(all_end),
        },
        "gaps": {
            "missing_reasoning_message_ids": missing_reasoning_messages,
            "tool_result_missing_part_ids": missing_result_parts,
            "tool_result_empty_part_ids": empty_output_parts,
            "tool_error_part_ids": error_parts,
            "tool_output_truncated_part_ids": truncated_parts,
        },
    }

    # Split at message-group boundaries. A shard always starts with the session
    # header and contains only complete message+parts groups.
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for group in groups:
        if current and current_bytes + group["bytes"] > max_shard_bytes:
            shards.append(current)
            current = []
            current_bytes = 0
        current.append(group)
        current_bytes += group["bytes"]
    if current or not shards:
        shards.append(current)

    return {
        "session_meta": session_meta,
        "shards": shards,
        "stats": dict(stats),
        "part_type_counts": dict(part_type_counts),
        "tool_names": dict(tool_names),
        "groups": groups,
        "messages": messages,
        "parts": parts,
    }


def write_session(
    export: dict[str, Any],
    out_dir: Path,
    max_shard_bytes: int,
) -> dict[str, Any]:
    session_meta = export["session_meta"]
    sid = session_meta["session_id"]
    shards = export["shards"]
    files: list[dict[str, Any]] = []
    total_lines = 0
    total_bytes = 0

    for idx, groups in enumerate(shards, start=1):
        header = dict(session_meta)
        first_order = groups[0]["first_order"] if groups else 0
        last_order = groups[-1]["last_order"] if groups else 0
        record_count = 1 + sum(len(g["lines"]) for g in groups)
        header["shard"] = {
            "index": idx,
            "count": len(shards),
            "first_order": first_order,
            "last_order": last_order,
            "record_count": record_count,
            "message_groups": len(groups),
            "max_shard_bytes": max_shard_bytes,
            "split_at": "message_boundary" if len(shards) > 1 else "none",
        }
        lines = [dumps_line(header)]
        for group in groups:
            lines.extend(group["lines"])
        if len(shards) == 1:
            filename = f"{sid}.jsonl"
        else:
            filename = f"{sid}.part-{idx:03d}-of-{len(shards):03d}.jsonl"
        path = out_dir / filename
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(lines))
        size = path.stat().st_size
        total_bytes += size
        total_lines += len(lines)
        files.append(
            {
                "path": str(path),
                "filename": filename,
                "shard_index": idx,
                "shard_count": len(shards),
                "bytes": size,
                "lines": len(lines),
                "sha256": sha256_file(path),
                "first_order": first_order,
                "last_order": last_order,
            }
        )

    return {
        "session_id": sid,
        "files": files,
        "bytes": total_bytes,
        "lines": total_lines,
        "shard_count": len(shards),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE))
    parser.add_argument("--tasks-db", default=str(DEFAULT_TASKS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--max-shard-bytes", type=int, default=MAX_SHARD_BYTES)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    source_db = Path(args.source_db)
    tasks_db = Path(args.tasks_db)
    out_root = Path(args.out)
    if out_root.exists() and any(out_root.iterdir()):
        raise SystemExit(f"refusing to write into non-empty directory: {out_root}")
    out_root.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect("file:" + source_db.as_posix() + "?mode=ro", uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    tcon = sqlite3.connect("file:" + tasks_db.as_posix() + "?mode=ro", uri=True, timeout=60)
    tcon.row_factory = sqlite3.Row
    tcon.execute("PRAGMA query_only=ON")

    snapshot_sha = sha256_file(source_db)
    sessions = select_sessions(con)
    tasks: dict[str, sqlite3.Row] = {}
    for row in tcon.execute("SELECT * FROM tasks WHERE deleted = 0"):
        if row["workspace_path"] in {w["path"] for w in WORKSPACES}:
            tasks[row["task_id"]] = row

    workspace_exports: dict[str, dict[str, Any]] = {}
    all_files: list[dict[str, Any]] = []
    all_sessions: list[dict[str, Any]] = []

    for ws in WORKSPACES:
        ws_sessions = [s for s in sessions if session_workspace(s)["path"] == ws["path"]]
        ws_dir = out_root / ws["slug"]
        ws_dir.mkdir(parents=True, exist_ok=False)
        ws_summary = {
            "workspace": ws["slug"],
            "workspace_path": ws["path"],
            "session_count": len(ws_sessions),
            "sessions": [],
        }
        for s in ws_sessions:
            export = build_session_export(
                con, tasks, s, ws, snapshot_sha, args.max_shard_bytes
            )
            written = write_session(export, ws_dir, args.max_shard_bytes)
            meta = export["session_meta"]
            summary = {
                "session_id": s["id"],
                "title": s["title"],
                "task_type": s["task_type"],
                "parent_session_id": s["parent_id"],
                "archived": meta["archived"],
                "task_index_present": meta["task_index"] is not None,
                "created_at": meta["session_created_at"],
                "updated_at": meta["session_updated_at"],
                "conversation_start": meta["conversation_time_range"]["start_utc"],
                "conversation_end": meta["conversation_time_range"]["end_utc"],
                "message_count": meta["completeness"]["source_message_count"],
                "part_count": meta["completeness"]["source_part_count"],
                "reasoning_parts": meta["completeness"]["reasoning_parts"],
                "assistant_messages_without_reasoning": meta["completeness"][
                    "assistant_messages_without_reasoning"
                ],
                "tool_calls": meta["completeness"]["tool_calls"],
                "tool_errors": meta["completeness"]["tool_errors"],
                "tool_result_missing": meta["completeness"]["tool_result_missing"],
                "tool_result_empty": meta["completeness"]["tool_result_empty"],
                "tool_output_truncated": meta["completeness"]["tool_output_truncated"],
                "file_parts": meta["completeness"]["file_parts"],
                "file_parts_metadata_only": meta["completeness"]["file_parts_metadata_only"],
                "files": [f["filename"] for f in written["files"]],
                "bytes": written["bytes"],
                "lines": written["lines"],
                "sha256_files": {f["filename"]: f["sha256"] for f in written["files"]},
            }
            ws_summary["sessions"].append(summary)
            all_sessions.append(summary)
            for f in written["files"]:
                all_files.append(
                    {
                        **f,
                        "workspace": ws["slug"],
                        "session_id": s["id"],
                    }
                )
        ws_summary["message_count"] = sum(x["message_count"] for x in ws_summary["sessions"])
        ws_summary["part_count"] = sum(x["part_count"] for x in ws_summary["sessions"])
        ws_summary["bytes"] = sum(x["bytes"] for x in ws_summary["sessions"])
        ws_summary["archived_session_count"] = sum(1 for x in ws_summary["sessions"] if x["archived"])
        ws_summary["child_session_count"] = sum(
            1 for x in ws_summary["sessions"] if x["parent_session_id"]
        )
        starts = [x["conversation_start"] for x in ws_summary["sessions"] if x["conversation_start"]]
        ends = [x["conversation_end"] for x in ws_summary["sessions"] if x["conversation_end"]]
        ws_summary["conversation_time_range"] = {
            "start_utc": min(starts) if starts else None,
            "end_utc": max(ends) if ends else None,
        }
        (ws_dir / "_workspace.json").write_text(
            json.dumps(ws_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        workspace_exports[ws["slug"]] = ws_summary
        print(
            f"{ws['slug']}: {len(ws_sessions)} sessions, "
            f"{ws_summary['message_count']} messages, {ws_summary['part_count']} parts, "
            f"{ws_summary['bytes']} bytes, {len(all_files)} shard files"
        )

    # Global index.
    index_path = out_root / "index.jsonl"
    with index_path.open("w", encoding="utf-8", newline="\n") as fh:
        for item in all_sessions:
            fh.write(
                dumps_line(
                    {
                        "record_type": "session_index",
                        "schema_version": SCHEMA,
                        **item,
                    }
                )
            )

    # Source reconciliation counts.
    source_exact = {}
    source_tasks = {}
    for ws in WORKSPACES:
        source_exact[ws["path"]] = con.execute(
            "SELECT count(*) FROM session WHERE directory = ?", (ws["path"],)
        ).fetchone()[0]
        source_tasks[ws["path"]] = tcon.execute(
            "SELECT count(*) FROM tasks WHERE workspace_path = ? AND deleted = 0", (ws["path"],)
        ).fetchone()[0]
    archived_tasks = {
        row["task_id"]: row["workspace_path"]
        for row in tcon.execute(
            "SELECT task_id, workspace_path FROM tasks WHERE archived = 1 AND deleted = 0"
        ).fetchall()
        if row["workspace_path"] in {w["path"] for w in WORKSPACES}
    }
    selected_ids = {s["id"] for s in sessions}
    missing_task_records = [
        {"task_id": tid, "workspace_path": wp}
        for tid, wp in archived_tasks.items()
        if tid not in selected_ids
    ]

    raw_manifest = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "scope": {
            "label": "shop+篡改",
            "workspaces": WORKSPACES,
            "session_selection": (
                "Sessions whose directory is exactly the target workspace, plus all recursively "
                "linked child sessions (parent_id), including archived tasks."
            ),
            "excluded": "Other ZCode workspaces and the current DSH session.",
        },
        "source": {
            "zcode_app_version": "3.14.4.7912",
            "conversation_db": str(Path(args.source_db)),
            "tasks_db": str(Path(args.tasks_db)),
            "snapshot_conversation_db": str(source_db),
            "snapshot_tasks_db": str(tasks_db),
            "snapshot_conversation_sha256": snapshot_sha,
            "snapshot_tasks_sha256": sha256_file(tasks_db),
            "source_read_only": True,
            "snapshot_method": "SQLite online backup API; source opened mode=ro; query_only=ON",
        },
        "inventory": {
            "session_rows_exact": source_exact,
            "task_rows_exact": source_tasks,
            "archived_task_ids": sorted(archived_tasks),
            "selected_session_count": len(sessions),
            "selected_message_count": sum(x["message_count"] for x in all_sessions),
            "selected_part_count": sum(x["part_count"] for x in all_sessions),
            "archived_sessions_exported": sum(1 for x in all_sessions if x["archived"]),
            "child_sessions_exported": sum(1 for x in all_sessions if x["parent_session_id"]),
            "archived_task_missing_from_sessions": missing_task_records,
        },
        "workspaces": workspace_exports,
        "files": all_files,
        "index_file": {
            "filename": "index.jsonl",
            "bytes": index_path.stat().st_size,
            "sha256": sha256_file(index_path),
            "lines": len(all_sessions),
        },
        "totals": {
            "session_count": len(sessions),
            "message_count": sum(x["message_count"] for x in all_sessions),
            "part_count": sum(x["part_count"] for x in all_sessions),
            "data_bytes": sum(x["bytes"] for x in all_sessions),
            "shard_file_count": len(all_files),
        },
        "sessions": all_sessions,
    }
    manifest_path = out_root.parent / "export-raw-manifest.json"
    manifest_path.write_text(
        json.dumps(raw_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"TOTAL: {len(sessions)} sessions, {raw_manifest['totals']['message_count']} messages, "
        f"{raw_manifest['totals']['part_count']} parts, {raw_manifest['totals']['data_bytes']} bytes"
    )
    print(f"raw manifest: {manifest_path}")
    con.close()
    tcon.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
