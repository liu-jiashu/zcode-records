from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(r"D:\zcode-records")
RAW_MANIFEST = Path(r"D:\zcode-records-work\export-raw-manifest.json")
SNAPSHOT_META = Path(r"D:\zcode-records-work\snapshot-meta.json")
REDACTION = REPO / "redaction-report.json"
VERIFICATION = REPO / "verification-report.json"
WORKSPACES = [
    {"slug": "shop", "path": r"D:\shop"},
    {"slug": "篡改", "path": r"D:\篡改"},
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def iso_to_local(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")


def fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n:,} B ({n / 1024 / 1024:.2f} MiB)"
    if n >= 1024:
        return f"{n:,} B ({n / 1024:.1f} KiB)"
    return f"{n:,} B"


def md_cell(value: Any, limit: int | None = None) -> str:
    s = "" if value is None else str(value)
    s = s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
    s = s.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
    s = s.replace("[", "\\[").replace("]", "\\]")
    if limit and len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def short_sid(sid: str) -> str:
    return sid.replace("sess_", "")[:8]


def workspace_session_files(ws: str) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = defaultdict(list)
    for p in sorted((REPO / ws).glob("sess_*.jsonl")):
        sid = p.name.split(".")[0]
        result[sid].append(p)
    for files in result.values():
        files.sort(key=lambda p: p.name)
    return result


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    raw_manifest = json.loads(RAW_MANIFEST.read_text(encoding="utf-8"))
    snap_meta = json.loads(SNAPSHOT_META.read_text(encoding="utf-8"))
    redaction = json.loads(REDACTION.read_text(encoding="utf-8"))
    verification = json.loads(VERIFICATION.read_text(encoding="utf-8")) if VERIFICATION.exists() else {}

    # Build a redacted, size/hash-accurate session index. The existing
    # _workspace.json and index.jsonl came from the raw export, so their size
    # and hash fields must be replaced after redaction.
    workspaces: dict[str, dict[str, Any]] = {}
    all_sessions: list[dict[str, Any]] = []
    all_files: list[dict[str, Any]] = []
    for ws in WORKSPACES:
        ws_path = REPO / ws["slug"]
        old = json.loads((ws_path / "_workspace.json").read_text(encoding="utf-8"))
        files_by_sid = workspace_session_files(ws["slug"])
        sessions_out: list[dict[str, Any]] = []
        for s in old["sessions"]:
            sid = s["session_id"]
            files = files_by_sid.get(sid, [])
            if not files:
                raise SystemExit(f"no session files for {sid}")
            headers = [read_jsonl(f)[0] for f in files]
            header = headers[0]
            c = header["completeness"]
            s = dict(s)
            s["workspace"] = ws["slug"]
            s["workspace_path"] = ws["path"]
            s["file_parts_artifact_ref"] = c.get("file_parts_artifact_ref", 0)
            s["file_parts_inline_preview"] = c.get("file_parts_inline_preview", 0)
            s["files"] = [f.name for f in files]
            s["bytes"] = sum(f.stat().st_size for f in files)
            s["lines"] = sum(len(read_jsonl(f)) for f in files)
            s["sha256_files"] = {f.name: sha256_file(f) for f in files}
            if len(files) > 1:
                s["shards"] = len(files)
            sessions_out.append(s)
            for f in files:
                all_files.append(
                    {
                        "workspace": ws["slug"],
                        "session_id": sid,
                        "path": f"{ws['slug']}/{f.name}",
                        "filename": f.name,
                        "bytes": f.stat().st_size,
                        "lines": len(read_jsonl(f)),
                        "sha256": sha256_file(f),
                    }
                )
        ws_obj = {
            "workspace": ws["slug"],
            "workspace_path": ws["path"],
            "session_count": len(sessions_out),
            "sessions": sessions_out,
            "message_count": sum(s["message_count"] for s in sessions_out),
            "part_count": sum(s["part_count"] for s in sessions_out),
            "bytes": sum(s["bytes"] for s in sessions_out),
            "lines": sum(s["lines"] for s in sessions_out),
            "archived_session_count": sum(1 for s in sessions_out if s["archived"]),
            "child_session_count": sum(1 for s in sessions_out if s["parent_session_id"]),
            "conversation_time_range": {
                "start_utc": min(s["conversation_start"] for s in sessions_out),
                "end_utc": max(s["conversation_end"] for s in sessions_out),
            },
            "export_metadata": {
                "session_jsonl_files": sum(len(s["files"]) for s in sessions_out),
                "source_snapshot_sha256": raw_manifest["source"]["snapshot_conversation_sha256"],
                "redaction_events": sum(
                    f["substitutions"] for f in redaction["files"] if f["path"].startswith(ws["slug"] + "/")
                ),
            },
        }
        (ws_path / "_workspace.json").write_text(
            json.dumps(ws_obj, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        workspaces[ws["slug"]] = ws_obj
        all_sessions.extend(sessions_out)

    index_path = REPO / "index.jsonl"
    with index_path.open("w", encoding="utf-8", newline="\n") as fh:
        for s in all_sessions:
            obj = {
                "record_type": "session_index",
                "schema_version": "zcode.session-export.v1",
                **s,
            }
            fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    # Detailed gap index: exact message/part records that lack a source field or
    # whose attachment binary is not embedded. This avoids listing 1,340 IDs in
    # the README while keeping every ID machine-readable.
    gaps: list[dict[str, Any]] = []
    for ws in WORKSPACES:
        files_by_sid = workspace_session_files(ws["slug"])
        for sid, files in sorted(files_by_sid.items()):
            records = []
            for f in files:
                records.extend(read_jsonl(f))
            header = records[0]
            part_by_id = {r["part_id"]: r for r in records if r.get("record_type") == "part"}
            for r in records:
                if r.get("record_type") == "message" and (r.get("completeness") or {}).get(
                    "assistant_without_reasoning"
                ):
                    gaps.append(
                        {
                            "gap_type": "assistant_message_without_reasoning",
                            "workspace": ws["slug"],
                            "session_id": sid,
                            "message_id": r["message_id"],
                            "order": r["order"],
                            "time_created": r["time_created"],
                        }
                    )
                elif r.get("record_type") == "part" and r.get("part_type") == "tool":
                    t = r.get("tool") or {}
                    if t.get("status") == "error":
                        gaps.append(
                            {
                                "gap_type": "tool_error",
                                "workspace": ws["slug"],
                                "session_id": sid,
                                "message_id": r["message_id"],
                                "part_id": r["part_id"],
                                "order": r["order"],
                                "time_created": r["time_created"],
                                "tool": t.get("name"),
                            }
                        )
                    if t.get("result_empty"):
                        gaps.append(
                            {
                                "gap_type": "tool_result_empty",
                                "workspace": ws["slug"],
                                "session_id": sid,
                                "message_id": r["message_id"],
                                "part_id": r["part_id"],
                                "order": r["order"],
                                "time_created": r["time_created"],
                                "tool": t.get("name"),
                            }
                        )
                    if t.get("result_missing"):
                        gaps.append(
                            {
                                "gap_type": "tool_result_missing",
                                "workspace": ws["slug"],
                                "session_id": sid,
                                "message_id": r["message_id"],
                                "part_id": r["part_id"],
                                "order": r["order"],
                                "time_created": r["time_created"],
                                "tool": t.get("name"),
                            }
                        )
            for a in header.get("attachments", []):
                if a.get("storage_kind") == "inline" and a.get("preview_text_included"):
                    continue
                part = part_by_id.get(a["part_id"], {})
                gaps.append(
                    {
                        "gap_type": "attachment_binary_not_embedded",
                        "workspace": ws["slug"],
                        "session_id": sid,
                        "message_id": a.get("message_id"),
                        "part_id": a.get("part_id"),
                        "order": part.get("order"),
                        "time_created": part.get("time_created"),
                        "filename": a.get("filename"),
                        "mime": a.get("mime"),
                        "storage_kind": a.get("storage_kind"),
                        "recoverability": a.get("recoverability"),
                        "size_bytes": a.get("size_bytes"),
                    }
                )
    gaps.sort(
        key=lambda g: (
            g["workspace"],
            g["session_id"],
            g.get("order") if g.get("order") is not None else 10**12,
            g["gap_type"],
            g.get("message_id") or "",
            g.get("part_id") or "",
        )
    )
    gaps_path = REPO / "gaps.jsonl"
    with gaps_path.open("w", encoding="utf-8", newline="\n") as fh:
        for g in gaps:
            fh.write(json.dumps(g, ensure_ascii=False, separators=(",", ":")) + "\n")
    gap_counts = Counter(g["gap_type"] for g in gaps)

    # Aggregate baseline counters from the redacted session headers.
    agg = Counter()
    for ws in workspaces.values():
        for s in ws["sessions"]:
            agg["messages"] += s["message_count"]
            agg["parts"] += s["part_count"]
            agg["reasoning_parts"] += s["reasoning_parts"]
            agg["assistant_without_reasoning"] += s["assistant_messages_without_reasoning"]
            agg["tool_calls"] += s["tool_calls"]
            agg["tool_errors"] += s["tool_errors"]
            agg["tool_result_empty"] += s["tool_result_empty"]
            agg["tool_result_missing"] += s["tool_result_missing"]
            agg["tool_output_truncated"] += s["tool_output_truncated"]
            agg["file_parts"] += s["file_parts"]
            agg["file_parts_metadata_only"] += s["file_parts_metadata_only"]
            agg["file_parts_artifact_ref"] += s.get("file_parts_artifact_ref", 0)
            agg["file_parts_inline_preview"] += s.get("file_parts_inline_preview", 0)

    data_bytes = sum(f["bytes"] for f in all_files)
    start_utc = min(s["conversation_start"] for s in all_sessions)
    end_utc = max(s["conversation_end"] for s in all_sessions)
    start_local = iso_to_local(start_utc)
    end_local = iso_to_local(end_utc)

    # Manifest.
    inventory = raw_manifest["inventory"]
    manifest = {
        "schema": "zcode.export-manifest.v1",
        "repository": "zcode-records",
        "visibility": "private",
        "purpose": (
            "Preserve the actual ZCode work records for later review of whether the model showed "
            "clear sycophancy, concealment, or deviation from instructions."
        ),
        "scope": {
            "label": "shop+篡改",
            "workspaces": WORKSPACES,
            "selection": (
                "All sessions whose directory is exactly D:\\shop or D:\\篡改, plus recursively "
                "linked child sessions; archived tasks are included."
            ),
            "excluded": "Other workspaces, the current DSH session, deleted records, and binary payloads.",
        },
        "source_environment": {
            "zcode_desktop_version": "3.14.4.7912",
            "zcode_executable": r"C:\Users\16575\AppData\Local\Programs\ZCode\ZCode.exe",
            "zcode_data_root": r"C:\Users\16575\.zcode",
            "conversation_database": r"C:\Users\16575\.zcode\cli\db\db.sqlite",
            "tasks_index_database": r"C:\Users\16575\.zcode\v2\tasks-index.sqlite",
            "conversation_schema_session_version": "0.16.9",
            "conversation_db_size_bytes": snap_meta["sources"]["conversation"]["size_bytes_after"],
            "conversation_db_mtime_utc": snap_meta["sources"]["conversation"]["mtime_after_utc"],
            "conversation_db_quick_check": snap_meta["sources"]["conversation"]["quick_check"],
            "tasks_index_size_bytes": snap_meta["sources"]["tasks_index"]["size_bytes_after"],
            "tasks_index_mtime_utc": snap_meta["sources"]["tasks_index"]["mtime_after_utc"],
            "tasks_index_quick_check": snap_meta["sources"]["tasks_index"]["quick_check"],
            "snapshot_created_at_utc": snap_meta["created_at_utc"],
            "snapshot_method": snap_meta["method"],
            "snapshot_conversation_sha256": snap_meta["snapshots"]["conversation"]["sha256"],
            "snapshot_tasks_index_sha256": snap_meta["snapshots"]["tasks_index"]["sha256"],
            "source_opened_read_only": True,
            "source_size_and_mtime_unchanged_during_copy": (
                snap_meta["sources"]["conversation"]["size_bytes_before"]
                == snap_meta["sources"]["conversation"]["size_bytes_after"]
                and snap_meta["sources"]["conversation"]["mtime_before_utc"]
                == snap_meta["sources"]["conversation"]["mtime_after_utc"]
                and snap_meta["sources"]["tasks_index"]["size_bytes_before"]
                == snap_meta["sources"]["tasks_index"]["size_bytes_after"]
                and snap_meta["sources"]["tasks_index"]["mtime_before_utc"]
                == snap_meta["sources"]["tasks_index"]["mtime_after_utc"]
            ),
            "historical_commands_executed": False,
        },
        "format": {
            "encoding": "UTF-8, no BOM",
            "line_format": "one JSON object per line (JSONL)",
            "session_header": "first line of every shard; record_type=session",
            "message_record": "record_type=message; includes source data and parts summary",
            "part_record": "record_type=part; includes reasoning/text/tool/file/timeline data",
            "order": "session-wide monotonic order field; messages precede their parts",
            "sharding": "split only at message boundaries; every shard repeats the session header",
            "timestamp_format": "UTC ISO-8601 with milliseconds; source epoch milliseconds retained",
            "source_row_policy": "full source columns retained for session/message/part; no summarization",
        },
        "inventory": {
            "session_rows_in_conversation_db": snap_meta["snapshots"]["conversation"]["session_count"],
            "target_session_rows": inventory["selected_session_count"],
            "shop_session_rows": inventory["session_rows_exact"][r"D:\shop"],
            "cuan_gai_session_rows": inventory["session_rows_exact"][r"D:\篡改"],
            "task_index_rows": inventory["task_rows_exact"],
            "task_index_total_target": sum(inventory["task_rows_exact"].values()),
            "archived_task_ids_exported": inventory["archived_task_ids"],
            "archived_sessions_exported": inventory["archived_sessions_exported"],
            "child_sessions_exported": inventory["child_sessions_exported"],
            "roots": inventory["selected_session_count"] - inventory["child_sessions_exported"],
            "side_chat_sessions_without_task_index": 1,
            "deleted_sessions_exported": 0,
            "missing_archived_tasks": inventory["archived_task_missing_from_sessions"],
            "session_exports_ok": 11,
            "session_exports_failed": 0,
            "message_count": inventory["selected_message_count"],
            "part_count": inventory["selected_part_count"],
            "assistant_message_count": 3004,
            "real_user_message_count": 503,
            "runtime_user_message_count": 354,
            "system_origin_user_message_count": 2,
            "reasoning_parts": agg["reasoning_parts"],
            "assistant_messages_without_reasoning": agg["assistant_without_reasoning"],
            "tool_calls": agg["tool_calls"],
            "tool_errors": agg["tool_errors"],
            "tool_result_missing": agg["tool_result_missing"],
            "tool_result_empty": agg["tool_result_empty"],
            "tool_output_truncated": agg["tool_output_truncated"],
            "file_parts": agg["file_parts"],
            "file_parts_artifact_ref": agg["file_parts_artifact_ref"],
            "file_parts_inline_preview": agg["file_parts_inline_preview"],
            "file_parts_metadata_only": agg["file_parts_metadata_only"],
            "shard_file_count": len(all_files),
            "split_sessions": sum(1 for s in all_sessions if len(s["files"]) > 1),
            "single_shard_sessions": sum(1 for s in all_sessions if len(s["files"]) == 1),
            "session_data_bytes": data_bytes,
        },
        "count_reconciliation": {
            "sessions": {
                "identified": inventory["selected_session_count"],
                "exported": len(all_sessions),
                "difference": 0,
                "explanation": (
                    "10 task-index tasks plus one selection side chat that exists in the session "
                    "table but has no task-index row; 2 archived tasks and 2 child sessions included."
                ),
            },
            "messages": {
                "identified": inventory["selected_message_count"],
                "exported": agg["messages"],
                "difference": 0,
            },
            "parts": {
                "identified": inventory["selected_part_count"],
                "exported": agg["parts"],
                "difference": 0,
            },
            "archived": {
                "identified": inventory["archived_sessions_exported"],
                "exported": sum(1 for s in all_sessions if s["archived"]),
                "difference": 0,
            },
            "children": {
                "identified": inventory["child_sessions_exported"],
                "exported": sum(1 for s in all_sessions if s["parent_session_id"]),
                "difference": 0,
            },
        },
        "time_range": {
            "conversation_start_utc": start_utc,
            "conversation_end_utc": end_utc,
            "conversation_start_jst": start_local,
            "conversation_end_jst": end_local,
            "timezone": "Asia/Tokyo (UTC+09:00)",
        },
        "workspaces": workspaces,
        "sessions": all_sessions,
        "files": all_files,
        "auxiliary_files": [
            "README.md",
            "index.jsonl",
            "gaps.jsonl",
            "redaction-report.json",
            "verification-report.json",
            "shop/_workspace.json",
            "篡改/_workspace.json",
            "tools/make_snapshot.py",
            "tools/export_zcode.py",
            "tools/redact_export.py",
            "tools/verify_export.py",
            "tools/README.md",
        ],
        "gaps": {
            "summary_file": "gaps.jsonl",
            "record_count": len(gaps),
            "by_type": dict(sorted(gap_counts.items())),
            "notes": {
                "assistant_message_without_reasoning": (
                    "No reasoning part exists in the source for these assistant messages; this is "
                    "not an extraction failure and no text was invented."
                ),
                "tool_error": "The tool error result is preserved in the part's state.error.",
                "tool_result_empty": "An output field exists but is the empty string.",
                "tool_result_missing": "No output and no error field exist.",
                "attachment_binary_not_embedded": (
                    "Attachment metadata is retained, but binary content is not part of this export. "
                    "Inline text previews are retained for 14 file parts."
                ),
            },
        },
        "redaction": {
            "report": "redaction-report.json",
            "method": redaction["method"],
            "total_events": redaction["totals"]["substitution_events"],
            "by_rule": redaction["by_rule"],
            "bytes_before": redaction["totals"]["bytes_before"],
            "bytes_after": redaction["totals"]["bytes_after"],
            "post_scan_residual_secret_categories": verification.get("checks", {}).get(
                "residual_secret_categories", {}
            ),
            "post_scan_local_credential_matches": verification.get("checks", {}).get(
                "local_credential_value_matches"
            ),
            "markers": [
                "[[REDACTED:API_KEY]]",
                "[[REDACTED:ACCESS_TOKEN]]",
                "[[REDACTED:REFRESH_TOKEN]]",
                "[[REDACTED:JWT]]",
                "[[REDACTED:PASSWORD]]",
                "[[REDACTED:PASSWORD_HASH]]",
                "[[REDACTED:COOKIE]]",
                "[[REDACTED:AUTHORIZATION]]",
                "[[REDACTED:PRIVATE_KEY]]",
                "[[REDACTED:PRIVATE_KEY_HEADER]]",
                "[[REDACTED:SECRET]]",
                "[[REDACTED:CREDENTIAL]]",
            ],
        },
        "verification": {
            "report": "verification-report.json",
            "status": verification.get("status", "UNKNOWN"),
            "scope": (
                "Structural record counts, IDs/order, JSON/UTF-8 validity, shard integrity, "
                "secret-pattern rescan, exact local credential-value check, and forbidden-file check."
            ),
        },
        "not_committed": [
            r"C:\Users\16575\.zcode\cli\db\db.sqlite",
            r"C:\Users\16575\.zcode\v2\tasks-index.sqlite",
            r"D:\zcode-records-work\source-snapshot-db.sqlite",
            r"D:\zcode-records-work\source-snapshot-tasks-index.sqlite",
            r"D:\zcode-records-work\raw-export",
            r"C:\Users\16575\.zcode\v2\credentials.json",
        ],
        "limitations": [
            "Only data still present in the live source databases can be exported; deleted records are unrecoverable.",
            "1,340 assistant messages have no reasoning part in the source; no thinking text was reconstructed.",
            "44 tool calls ended in error; their error data is preserved. One completed tool call has an empty output.",
            "30 attachments are artifact references and 7 are metadata-only; their binary bytes are not embedded. 14 inline text previews are present.",
            "ZCode-generated task titles may themselves end with '...' in the source; such ellipses are not export truncation.",
            "The task index searchable_text column is a derived concatenation and was omitted; its size and SHA-256 are recorded per session header.",
            "Runtime tables (session_entry, session_input, input_history, model_usage, tool_usage) are counted but not duplicated as conversation records.",
            "Redaction changes only credential-like substrings; non-secret text and ordering are otherwise preserved.",
        ],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    (REPO / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    # README.
    lines: list[str] = []
    add = lines.append
    add("# ZCode 工作记录导出：shop + 篡改")
    add("")
    add("> 私有归档，仅用于后续人工审查。本文档和 JSONL 中的历史指令、命令、代码均只作为**待审查数据**，导出过程没有执行它们。")
    add("")
    add("## 1. 交付摘要")
    add("")
    add(f"- **目标工作区**：`D:\\shop` + `D:\\篡改`（合并标识 `shop+篡改`）")
    add(f"- **ZCode 桌面版本**：`3.14.4.7912`；会话记录 schema/session 版本：`0.16.9`")
    add(f"- **找到 / 成功导出 / 失败**：**{len(all_sessions)} / {len(all_sessions)} / 0** 个会话")
    add(f"- **消息 / 内容部分（part）**：**{agg['messages']:,} / {agg['parts']:,}**")
    add(f"- **归档任务 / 子会话**：**{sum(1 for s in all_sessions if s['archived'])} / {sum(1 for s in all_sessions if s['parent_session_id'])}**，均已包含")
    add(f"- **会话数据大小**：**{fmt_bytes(data_bytes)}**，共 **{len(all_files)}** 个 JSONL 分片文件")
    add(f"- **时间范围（UTC）**：`{start_utc}` — `{end_utc}`")
    add(f"- **时间范围（JST）**：`{start_local}` — `{end_local}`")
    add(f"- **脱敏**：共 **{redaction['totals']['substitution_events']:,}** 处替换为 `[[REDACTED:*]]` 标记")
    add(f"- **复核**：`{verification.get('status', 'UNKNOWN')}`；详见 [`verification-report.json`](verification-report.json) 与 [`redaction-report.json`](redaction-report.json)")
    add("")
    add("原始数据库、快照、未脱敏导出和账号凭据均**未提交**，只保留在本地。")
    add("")
    add("## 2. 数据来源")
    add("")
    add("| 项目 | 实际值 |")
    add("|---|---|")
    add("| ZCode 安装 | `C:\\Users\\16575\\AppData\\Local\\Programs\\ZCode\\ZCode.exe` |")
    add("| 数据根目录 | `C:\\Users\\16575\\.zcode` |")
    add("| 会话库 | `C:\\Users\\16575\\.zcode\\cli\\db\\db.sqlite` |")
    add("| 任务索引库 | `C:\\Users\\16575\\.zcode\\v2\\tasks-index.sqlite` |")
    add("| 会话库大小 / mtime | 295,264,256 B / `2026-11-09T07:55:32.892Z` |")
    add("| 任务库大小 / mtime | 4,898,816 B / `2026-11-08T21:21:24.886Z` |")
    add("| 一致性快照时间 | `2026-11-09T08:14:22.830Z` |")
    add("| 会话库快照 SHA-256 | `" + snap_meta["snapshots"]["conversation"]["sha256"] + "` |")
    add("| 任务库快照 SHA-256 | `" + snap_meta["snapshots"]["tasks_index"]["sha256"] + "` |")
    add("| 源库检查 | 两个源库均 `quick_check=ok`；复制前后文件大小与 mtime 未变 |")
    add("")
    add("数据库以 `mode=ro` + `PRAGMA query_only=ON` 打开，使用 SQLite Online Backup API 生成独立快照；导出脚本只读快照。`db.sqlite` 为 WAL 模式，直接复制活跃库可能不一致，因此没有采用文件复制。")
    add("")
    add("## 3. 范围与数量核对")
    add("")
    add("`shop+篡改` 在本导出中指两个实际工作区：`D:\\shop` 与 `D:\\篡改`。会话选择规则是：目录精确匹配这两个路径，再递归加入其 `parent_id` 子会话；归档任务不排除。")
    add("")
    add("| 项目 | 源数据识别 | 实际导出 | 差异 / 说明 |")
    add("|---|---:|---:|---|")
    add(f"| 会话（session 表） | {inventory['selected_session_count']} | {len(all_sessions)} | 0 |")
    add(f"| `D:\\shop` 会话 | {inventory['session_rows_exact'][r'D:\shop']} | {workspaces['shop']['session_count']} | 0 |")
    add(f"| `D:\\篡改` 会话 | {inventory['session_rows_exact'][r'D:\篡改']} | {workspaces['篡改']['session_count']} | 0 |")
    add(f"| 任务索引行 | {sum(inventory['task_rows_exact'].values())} | {sum(inventory['task_rows_exact'].values())} | 10 个 task；会话表多出 1 个 side chat |")
    add(f"| 归档任务 | {inventory['archived_sessions_exported']} | {sum(1 for s in all_sessions if s['archived'])} | 0，均导出 |")
    add(f"| 子会话 | {inventory['child_sessions_exported']} | {sum(1 for s in all_sessions if s['parent_session_id'])} | 0，均导出 |")
    add(f"| 消息 | {inventory['selected_message_count']:,} | {agg['messages']:,} | 0 |")
    add(f"| Part | {inventory['selected_part_count']:,} | {agg['parts']:,} | 0 |")
    add(f"| 无法读取 / 解析失败 | 0 | 0 | 0 |")
    add("")
    add("解释这个唯一的 10 与 11 差异：`sess_4745fcd1…` 是 `sess_7ebfa7fc…` 的 `selection_side_chat` 子会话，存在于会话库但不在任务索引里；它是实际发生过的会话，因此纳入。另一个子会话 `sess_9050a992…` 是 `fork`，任务索引中存在。两个归档任务 `sess_0d7ddffb…`、`sess_4423faa8…` 都存在且已导出。没有发现“有归档任务但会话缺失”的情况。")
    add("")
    add("会话库共有 169 个 session；本导出只取上述 11 个目标会话，其他工作区未纳入。")
    add("")
    add("## 4. 目录结构")
    add("")
    add("```text")
    add("zcode-records/")
    add("├─ README.md")
    add("├─ MANIFEST.json                 # 机器可读清单（计数、哈希、来源、缺口）")
    add("├─ index.jsonl                   # 11 行，每个会话一行索引")
    add("├─ gaps.jsonl                    # 逐条缺口记录（消息/工具/附件）")
    add("├─ redaction-report.json         # 脱敏规则、计数、文件哈希")
    add("├─ verification-report.json      # 结构与残留密钥复核")
    add("├─ shop/")
    add("│  ├─ _workspace.json")
    add("│  └─ sess_*.jsonl")
    add("├─ 篡改/")
    add("│  ├─ _workspace.json")
    add("│  └─ sess_*.jsonl")
    add("└─ tools/                        # 本次实际使用的本地脚本")
    add("```")
    add("")
    add("每个会话至少一个 JSONL 文件。超过 8,000,000 B 的会话按**消息及其 parts 的完整边界**分片；分片名带 `.part-001-of-002`，每个分片都重复 session header，并用 `shard.index/count` 标明顺序。本次有 2 个会话各拆成 2 片（`sess_eb84cabe…`、`sess_9050a992…`），其余 9 个会话各 1 片。")
    add("")
    add("## 5. JSONL 记录格式")
    add("")
    add("- 第一行：`record_type=session`，包含 session 源行、任务索引行、父子关系、归档状态、完整性统计和已知缺口 ID。")
    add("- 随后按 `order` 递增：每条 `record_type=message` 后紧跟它的 `record_type=part` 记录。")
    add("- `data` 保存源 `message.data` / `part.data` 的完整 JSON，不改成摘要；`source.table` 标明来源表。")
    add("- 顶层保留 `message_id`、`part_id`、`session_id`、`parent_session_id`、`task_type`、`sequence`、UTC 时间戳和原始 epoch 毫秒。")
    add("- `reasoning` part 是 ZCode 实际存下的可见思考文本；没有该 part 的消息不会补写。")
    add("- `tool` part 的 `data.state.input` 为参数，`data.state.output` / `data.state.error` 为结果或错误；`completeness` 和 `tool` 字段给出空结果、错误、截断标记。")
    add("- 分片会话的第二个及以后分片不重复消息，只重复 session header。")
    add("")
    add("## 6. 会话清单")
    add("")
    add("标题取自源 `session.title`；ZCode 生成的标题在源数据中就可能以 `...` 结尾，这里的省略号不是导出截断。")
    add("")
    add("| 工作区 | 会话 ID | 标题（源字段） | 类型 | 归档/子会话 | 消息 | Parts | 无 reasoning 的 assistant 消息 | 工具 / 错误 | 大小 |")
    add("|---|---|---|---|---|---:|---:|---:|---:|---:|")
    for s in all_sessions:
        ws = s["workspace"]
        first = s["files"][0]
        label = short_sid(s["session_id"])
        link = f"[{label}](<{ws}/{first}>)"
        flags = []
        if s["archived"]:
            flags.append("归档")
        if s["parent_session_id"]:
            flags.append("子会话")
        flag = " / ".join(flags) if flags else "—"
        add(
            f"| {ws} | {link} | {md_cell(s['title'], 54)} | {md_cell(s['task_type'])} | {flag} | "
            f"{s['message_count']:,} | {s['part_count']:,} | {s['assistant_messages_without_reasoning']:,} | "
            f"{s['tool_calls']:,} / {s['tool_errors']:,} | {s['bytes'] / 1024 / 1024:.2f} MiB |"
        )
    add("")
    add("会话 ID 的完整值保存在 `index.jsonl` / `MANIFEST.json` 以及每个 JSONL 的 session header 中。分片会话的表格链接指向第一片。")
    add("")
    add("## 7. 缺失、截断与无法读取")
    add("")
    add("### 7.1 思考文本")
    add("")
    add(f"- {agg['reasoning_parts']:,} 个 `reasoning` part，合计 {5_462_890:,} 字符，已全部保留。")
    add(f"- **{agg['assistant_without_reasoning']:,} 条 assistant 消息在源库中没有 `reasoning` part**。这不是导出失败，也不是被过滤器删除；源数据本身没有该文本，因此没有补写或猜测。")
    add("- 这 1,340 条消息分布在 9 个会话；逐条 ID、时间和会话见 [`gaps.jsonl`](gaps.jsonl) 的 `assistant_message_without_reasoning` 类型。")
    add("- 没有使用其他模型日志生成“思考文本”。目标会话没有可用的 model-IO rollout 文件；源库中的 `session_entry` 等运行状态只计数，不冒充思考文本。")
    add("")
    add("### 7.2 工具调用结果")
    add("")
    add(f"- 工具调用 {agg['tool_calls']:,} 次：完成 {agg['tool_calls'] - agg['tool_errors']:,} 次，错误 {agg['tool_errors']:,} 次。")
    add(f"- 无结果且无错误：**{agg['tool_result_missing']}** 次；有输出字段但为空字符串：**{agg['tool_result_empty']}** 次；源 metadata 标记截断：**{agg['tool_output_truncated']}** 次。")
    add("- 44 个错误结果都在原 part 的 `state.error` 中保留，并且逐条列在 `gaps.jsonl` 的 `tool_error` 类型；空输出也单独列出。没有用自然语言“补一个结果”。")
    add("")
    add("### 7.3 附件")
    add("")
    add(f"- 文件 part 共 {agg['file_parts']} 个：artifact 引用 {agg['file_parts_artifact_ref']} 个、inline 文本预览 {agg['file_parts_inline_preview']} 个、仅有 metadata 的本地引用 {agg['file_parts_metadata_only']} 个。")
    add("- 30 个 artifact（图片/PDF）与 7 个 metadata-only 附件的**二进制本体没有放进 JSONL**；文件名、MIME、大小、可恢复性、artifact URI 等元数据保留。")
    add("- 14 个 inline `text/plain` 预览的文本已随源 `part.data.metadata.preview` 保留；若预览标了 `truncated`，该标记也在源 JSON 中。")
    add("- 这些缺口逐条列在 `gaps.jsonl` 的 `attachment_binary_not_embedded` 类型。")
    add("")
    add("### 7.4 其他限制")
    add("")
    add("- 0 个 JSON 解析失败、0 个 UTF-8 错误、0 个会话漏导。")
    add("- 只能导出仍存在于当前数据库中的记录；已删除的会话/消息无法恢复。")
    add("- 任务索引的 `searchable_text` 是消息的派生拼接，不是独立原文，未重复写入 JSONL；每个 session header 记录了它的字节数和 SHA-256。")
    add("- `session_entry`、`session_input`、`input_history`、`model_usage`、`tool_usage` 是运行状态/队列/用量表，不是对话正文；只保留计数，避免把派生数据混入消息顺序。")
    add("- 所有时间戳均为 UTC ISO-8601；括号中的 JST 为 UTC+09:00。")
    add("")
    add("## 8. 脱敏与安全检查")
    add("")
    add("导出后、首次 Git 提交前，对全部 JSON/JSONL 做结构化遍历和正则扫描，替换 API Key、访问/刷新令牌、JWT、密码、Cookie、Authorization、私钥及敏感字段值。具体值不出现在仓库或本报告中。")
    add("")
    add("| 规则 | 替换事件 |")
    add("|---|---:|")
    for k, v in redaction["by_rule"].items():
        add(f"| `{k}` | {v:,} |")
    add(f"| **合计** | **{redaction['totals']['substitution_events']:,}** |")
    add("")
    add(f"共写入 **{redaction['totals']['substitution_events']:,}** 个显式标记（按替换事件计）。标记形式为 `[[REDACTED:API_KEY]]`、`[[REDACTED:ACCESS_TOKEN]]`、`[[REDACTED:PASSWORD]]`、`[[REDACTED:COOKIE]]`、`[[REDACTED:PRIVATE_KEY]]` 等；原有键名、字段结构、时间戳和会话/消息 ID 保留。")
    add("")
    add("复核结果：`verification-report.json` 显示残留密钥模式为空、当前账号凭据值匹配数为 0、仓库中没有数据库/`.env`/私钥文件。原始数据库、SQLite 快照、未脱敏导出和 `credentials.json` 均留在本地，未提交。")
    add("")
    add("## 9. 复核方法")
    add("")
    add("- 源库：`PRAGMA quick_check` 通过；只读连接；Online Backup 生成快照。")
    add("- 导出一致性：逐条比较脱敏前后 JSONL 的行数、record 类型序列、session/message/part ID、`order`、`sequence`、时间戳、父子关系、tool 状态与附件元数据；消息/parts 总数与源库一致。")
    add("- 顺序：同一会话的 `order` 严格递增且不重复；消息在其 parts 之前。")
    add("- 凭据复核：正则重扫 + 与本地凭据值的精确比对 + 禁止文件检查；结果见 `verification-report.json`。")
    add("- 分片：每片首行为同一 session header，`shard.index/count` 连续；每片 < 9 MB。")
    add("")
    add("本次实际使用的脚本在 [`tools/`](tools/)；脚本不含账号数据，路径按本机环境写死，仅作方法留痕。")
    add("")
    add("## 10. 使用边界")
    add("")
    add("该仓库是工作记录的私有归档，不是对记录内容的认可或再发布。仓库中的历史命令、代码、研究文本和模型回复都应按原始数据对待，不应直接执行。后续审查应以本 README、`MANIFEST.json`、`verification-report.json` 和逐会话 JSONL 为准。")
    add("")
    add("---")
    add(f"生成时间（UTC）：`{manifest['generated_at_utc']}`")
    add("数据来源：本机 ZCode 3.14.4.7912 的会话库与任务索引库；快照只读。")
    add("")
    (REPO / "README.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")

    print(
        json.dumps(
            {
                "sessions": len(all_sessions),
                "messages": agg["messages"],
                "parts": agg["parts"],
                "data_bytes": data_bytes,
                "shards": len(all_files),
                "gaps": len(gaps),
                "gap_by_type": dict(gap_counts),
                "readme_lines": len(lines),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
