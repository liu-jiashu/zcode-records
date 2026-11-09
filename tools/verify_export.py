from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

RAW = Path(r"D:\zcode-records-work\raw-export")
REPO = Path(r"D:\zcode-records")
RAW_MANIFEST = Path(r"D:\zcode-records-work\export-raw-manifest.json")
CREDENTIALS = Path.home() / ".zcode" / "v2" / "credentials.json"
REPORT = REPO / "verification-report.json"

SECRET_PATTERNS = {
    "openai_sk": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "gitlab_token": re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "huggingface_token": re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    "npm_token": re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    "pypi_token": re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "bigmodel_key": re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b"),
    "env_assignment": re.compile(
        r"(?i)\b([A-Z][A-Z0-9_]{2,})(\s*=\s*)(?:([\"'])([^\s\"'`]{3,})\3|([^\s\"'`]{3,}))"
    ),
    "jsonish_secret": re.compile(
        r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret[_-]?key|password|passwd|pwd|cookie|authorization)[\"']?\s*[:=]\s*[\"'])([^\"'\s,}]{4,})([\"'])"
    ),
    "query_param": re.compile(
        r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|sig|signature)=)([^&\s\"'<>]{4,})"
    ),
    "chinese_label": re.compile(
        r"(API\s*[Kk]ey|接口密钥|访问令牌|刷新令牌|私钥|密钥|令牌|密码|口令)(\s*[:：=]\s*)([^\s，。；;\"'`]{4,})"
    ),
}
PEM = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )?PRIVATE KEY)-----(.*?)-----END \1-----", re.S)
SENSITIVE_ENV_NAME = re.compile(
    r"(?i)(?:^|_)(?:API_?KEY|ACCESS_?KEY|ACCESS_?TOKEN|REFRESH_?TOKEN|SECRET(?:_?KEY)?|PASSWORD|PASSWD|COOKIE|PRIVATE_?KEY|AUTH_?TOKEN)(?:_|$)"
)
CHINESE_NON_SECRET_VALUES = {
    "通过",
    "不通过",
    "已配置",
    "未配置",
    "成功",
    "失败",
    "是",
    "否",
    "true",
    "false",
}


def looks_like_code_value(value: str) -> bool:
    v = value.strip()
    if not v:
        return True
    if v.lower() in {"true", "false", "none", "null", "yes", "no"}:
        return True
    if v.startswith("...") and set(v) <= set(".\\nrt "):
        return True
    if v[0] in "$({[`\"'":
        return True
    return bool(
        re.match(
            r"(?i)^(?:os\.|process\.|environ\b|getenv\b|config\.|settings\.|secrets\.|re\.|none$|null$|true$|false$|yes$|no$|\.\.\.$|\*\*\*$)",
            v,
        )
    )


def walk(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)
    elif isinstance(obj, str):
        yield obj


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compare_one(rel: Path) -> tuple[list[str], Counter, int]:
    raw_path = RAW / rel
    red_path = REPO / rel
    errors: list[str] = []
    stats: Counter = Counter()
    markers = 0
    raw_lines = raw_path.read_text(encoding="utf-8").splitlines()
    red_lines = red_path.read_text(encoding="utf-8").splitlines()
    if len(raw_lines) != len(red_lines):
        errors.append(f"{rel}: line count {len(raw_lines)} != {len(red_lines)}")
    for i, (ra, rb) in enumerate(zip(raw_lines, red_lines), start=1):
        a = json.loads(ra)
        b = json.loads(rb)
        stats[f"record_type_{a.get('record_type')}"] += 1
        for key in (
            "record_type",
            "session_id",
            "workspace",
            "workspace_path",
            "message_id",
            "part_id",
            "sequence",
            "part_type",
            "role",
            "origin",
            "kind",
            "order",
            "time_created",
            "time_updated",
            "time_created_ms",
            "parent_session_id",
            "task_type",
        ):
            if a.get(key) != b.get(key):
                errors.append(f"{rel}:{i}: field {key} changed")
        if a.get("record_type") == "session":
            for key in (
                "session_id",
                "session_created_at",
                "session_updated_at",
                "session_archived_at",
                "archived",
                "relationship",
                "completeness",
            ):
                if a.get(key) != b.get(key):
                    errors.append(f"{rel}:{i}: session field {key} changed")
        if a.get("record_type") == "message":
            if (a.get("parts_summary") or {}).get("count") != (b.get("parts_summary") or {}).get("count"):
                errors.append(f"{rel}:{i}: parts_summary count changed")
            if (a.get("completeness") or {}).get("parts_present") != (b.get("completeness") or {}).get("parts_present"):
                errors.append(f"{rel}:{i}: parts_present changed")
            if (a.get("completeness") or {}).get("reasoning_present") != (b.get("completeness") or {}).get("reasoning_present"):
                errors.append(f"{rel}:{i}: reasoning_present changed")
        if a.get("record_type") == "part":
            if a.get("part_type") == "tool":
                at = a.get("tool") or {}
                bt = b.get("tool") or {}
                for key in ("name", "call_id", "status", "has_result", "result_missing", "output_truncated"):
                    if at.get(key) != bt.get(key):
                        errors.append(f"{rel}:{i}: tool.{key} changed")
            if a.get("part_type") == "file":
                am = dict((a.get("data") or {}).get("metadata") or {})
                bm = dict((b.get("data") or {}).get("metadata") or {})
                # Preview text may legitimately contain a redacted secret; compare
                # its availability/truncation flags, not its body.
                ap = dict(am.pop("preview", {}) or {})
                bp = dict(bm.pop("preview", {}) or {})
                for key in ("recoverability", "storageKind", "sizeBytes", "artifactUri"):
                    if am.get(key) != bm.get(key):
                        errors.append(f"{rel}:{i}: file metadata {key} changed")
                for key in ("truncated", "originalBytes", "startLine", "totalLines"):
                    if ap.get(key) != bp.get(key):
                        errors.append(f"{rel}:{i}: file preview {key} changed")
        markers += rb.count("[[REDACTED:")
    return errors, stats, markers


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    raw_manifest = json.loads(RAW_MANIFEST.read_text(encoding="utf-8"))
    errors: list[str] = []
    record_stats: Counter = Counter()
    total_markers = 0
    file_rows = []

    expected = {f["filename"] for f in raw_manifest["files"]}
    actual = {p.name for p in REPO.rglob("*.jsonl") if p.parent.name in ("shop", "篡改")}
    if expected != actual:
        errors.append(f"shard file set mismatch: missing={sorted(expected-actual)} extra={sorted(actual-expected)}")

    for f in raw_manifest["files"]:
        rel = Path(f["workspace"]) / f["filename"]
        e, stats, markers = compare_one(rel)
        errors.extend(e)
        record_stats.update(stats)
        total_markers += markers
        red_path = REPO / rel
        file_rows.append(
            {
                "path": rel.as_posix(),
                "bytes": red_path.stat().st_size,
                "sha256": sha256_file(red_path),
                "lines": len(red_path.read_text(encoding="utf-8").splitlines()),
                "redaction_markers": markers,
            }
        )
        if red_path.stat().st_size > 9_000_000:
            errors.append(f"{rel}: shard exceeds 9MB")

    # Validate index/workspace JSON docs and global JSONL.
    for rel in ("index.jsonl", "shop/_workspace.json", "篡改/_workspace.json"):
        p = REPO / rel
        if not p.is_file():
            errors.append(f"missing {rel}")
        if p.suffix == ".jsonl":
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    json.loads(line)
                except Exception as exc:
                    errors.append(f"{rel}:{i}: invalid JSON: {exc}")
        else:
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"{rel}: invalid JSON: {exc}")

    # Reconstruct per-session order and counts from redacted JSONL.
    sessions: dict[str, dict[str, Any]] = {}
    for path in sorted(REPO.rglob("*.jsonl")):
        if path.name == "index.jsonl" or path.parent.name not in ("shop", "篡改"):
            continue
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows or rows[0].get("record_type") != "session":
            errors.append(f"{path}: missing session header")
            continue
        sid = rows[0]["session_id"]
        info = sessions.setdefault(
            sid,
            {"workspace": rows[0]["workspace"], "headers": [], "messages": set(), "parts": set(), "orders": [], "message_count": 0, "part_count": 0},
        )
        if rows[0]["workspace"] != info["workspace"]:
            errors.append(f"{path}: workspace mismatch")
        info["headers"].append(rows[0])
        for r in rows[1:]:
            if r.get("session_id") != sid:
                errors.append(f"{path}: record session_id mismatch")
            if r.get("record_type") == "message":
                info["message_count"] += 1
                info["messages"].add(r.get("message_id"))
            elif r.get("record_type") == "part":
                info["part_count"] += 1
                info["parts"].add(r.get("part_id"))
            info["orders"].append(r.get("order"))

    for sid, info in sessions.items():
        orders = info["orders"]
        if orders != sorted(orders) or len(set(orders)) != len(orders):
            errors.append(f"{sid}: order is not strictly monotonic")
        if len(info["messages"]) != info["message_count"]:
            errors.append(f"{sid}: duplicate message IDs")
        if len(info["parts"]) != info["part_count"]:
            errors.append(f"{sid}: duplicate part IDs")

    # Compare aggregate counts to the raw manifest.
    by_id = {s["session_id"]: s for s in raw_manifest["sessions"]}
    if set(by_id) != set(sessions):
        errors.append(f"session set mismatch: {sorted(set(by_id) ^ set(sessions))}")
    for sid, info in sessions.items():
        expected_s = by_id.get(sid)
        if not expected_s:
            continue
        if info["message_count"] != expected_s["message_count"]:
            errors.append(f"{sid}: message count mismatch")
        if info["part_count"] != expected_s["part_count"]:
            errors.append(f"{sid}: part count mismatch")

    # Secret scan over every committed text file.
    residual: Counter = Counter()
    files_scanned = 0
    repo_concat = bytearray()
    for path in sorted(REPO.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        data = path.read_bytes()
        repo_concat.extend(data)
        files_scanned += 1
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if path.suffix == ".jsonl":
            texts = []
            for line in text.splitlines():
                try:
                    texts.extend(walk(json.loads(line)))
                except Exception:
                    texts.append(line)
        elif path.suffix == ".json":
            try:
                texts = list(walk(json.loads(text)))
            except Exception:
                texts = [text]
        else:
            texts = [text]
        for s in texts:
            for name, rx in SECRET_PATTERNS.items():
                for m in rx.finditer(s):
                    if "[[REDACTED" in m.group(0):
                        continue
                    if name == "env_assignment":
                        env_name = m.group(1).upper()
                        if env_name.endswith("_ENV"):
                            continue
                        if not SENSITIVE_ENV_NAME.search(env_name):
                            continue
                        value = m.group(4) if m.group(4) is not None else (m.group(5) or "")
                        if env_name.startswith("SESSION_COOKIE_"):
                            vv = value.strip().strip("\"',").lower()
                            if vv in {"true", "false", "lax", "strict", "none"}:
                                continue
                        if looks_like_code_value(value):
                            continue
                    if name == "chinese_label":
                        value = (m.groups()[-1] or "").strip()
                        if value.rstrip("。；;，,") in CHINESE_NON_SECRET_VALUES:
                            continue
                        if value.startswith(("通过", "不通过", "已配置", "未配置")):
                            continue
                    residual[name] += 1
            for m in PEM.finditer(s):
                body = m.group(2).strip()
                if body and "[[REDACTED:PRIVATE_KEY]]" not in body:
                    residual["unredacted_private_key_block"] += 1

    cred_hits = 0
    if CREDENTIALS.is_file():
        cred = json.loads(CREDENTIALS.read_text(encoding="utf-8"))

        def flatten(o: Any):
            if isinstance(o, dict):
                for v in o.values():
                    yield from flatten(v)
            elif isinstance(o, list):
                for v in o:
                    yield from flatten(v)
            elif isinstance(o, str):
                yield o

        for value in flatten(cred):
            if len(value) >= 12 and value.encode("utf-8") in repo_concat:
                cred_hits += 1

    forbidden = []
    for path in REPO.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_file() and (
            path.suffix.lower() in (".sqlite", ".db", ".sqlite3", ".pem", ".key")
            or path.name.lower().startswith("credentials")
            or path.name.lower() == ".env"
        ):
            forbidden.append(path.relative_to(REPO).as_posix())

    if residual:
        errors.append(f"residual secret patterns: {dict(residual)}")
    if cred_hits:
        errors.append(f"local credential values found in repo: {cred_hits}")
    if forbidden:
        errors.append(f"forbidden local-only files: {forbidden}")

    # UTF-8 and BOM check.
    for path in REPO.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                errors.append(f"UTF-8 BOM in {path.relative_to(REPO)}")
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                errors.append(f"not UTF-8: {path.relative_to(REPO)}: {exc}")

    data_bytes = sum(f["bytes"] for f in file_rows)
    report = {
        "schema": "zcode.verification.v1",
        "status": "PASS" if not errors else "FAIL",
        "checked_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(timespec="milliseconds"),
        "checks": {
            "source_session_count": raw_manifest["totals"]["session_count"],
            "exported_session_count": len(sessions),
            "source_message_count": raw_manifest["totals"]["message_count"],
            "exported_message_count": sum(i["message_count"] for i in sessions.values()),
            "source_part_count": raw_manifest["totals"]["part_count"],
            "exported_part_count": sum(i["part_count"] for i in sessions.values()),
            "shard_file_count": len(file_rows),
            "jsonl_and_json_parse": "PASS" if not any("invalid JSON" in e for e in errors) else "FAIL",
            "utf8_without_bom": "PASS" if not any(("not UTF-8" in e or "BOM" in e) for e in errors) else "FAIL",
            "ordering_unique_and_monotonic": "PASS" if not any("order" in e for e in errors) else "FAIL",
            "redaction_markers": total_markers,
            "residual_secret_categories": dict(residual),
            "local_credential_value_matches": cred_hits,
            "forbidden_local_files": forbidden,
            "files_scanned": files_scanned,
            "session_data_bytes": data_bytes,
        },
        "record_stats": dict(record_stats),
        "files": file_rows,
        "errors": errors,
    }
    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report["checks"], ensure_ascii=False, indent=2))
    print("STATUS", report["status"], "errors", len(errors))
    for e in errors[:20]:
        print("ERROR", e)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
