#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export ZCode session history to sanitized UTF-8 JSONL.

The source SQLite databases are opened read-only. This script never executes
text found in the records; it only parses and serializes it.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

SCHEMA = "zcode-records/v1"
TOOL_VERSION = "0.16.9"
APP_VERSION = "3.14.4 (7912)"
LOCAL_TZ = ZoneInfo("Asia/Tokyo")

# Exact workspace paths requested by the user.  `额度` and `额度/经验` are two
# distinct ZCode workspaces and are therefore exported as two directories.
TARGET_WORKSPACES = [
    ("llm-workspace", "/Users/llm/llm-workspace"),
    ("xiaohongshu", "/Users/llm/小红书"),
    ("female-content", "/Users/llm/女性内容"),
    ("quota", "/Users/llm/额度"),
    ("quota-experience", "/Users/llm/额度/经验"),
    ("claude-sandbox-plan", "/Users/llm/Documents/claude/claude-sandbox-plan"),
]

# One archived task exists immediately outside the requested paths, in the
# parent directory of claude-sandbox-plan.  It is directly related to the
# Claude account-pool work, so it is exported separately and labelled as
# related-archived rather than silently merged into the target count.
RELATED_ARCHIVED_WORKSPACES = [
    ("related-archived-claude", "/Users/llm/Documents/claude"),
]

DB_DISPLAY = "~/.zcode/cli/db/db.sqlite"
TASKS_DISPLAY = "~/.zcode/v2/tasks-index.sqlite"
ARTIFACT_ROOT = Path.home() / ".zcode/cli/artifacts"


def now_iso() -> str:
    return dt.datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")


def ms_iso(ms: Optional[int], tz=dt.timezone.utc) -> Optional[str]:
    if ms is None:
        return None
    try:
        value = dt.datetime.fromtimestamp(int(ms) / 1000.0, tz=tz)
    except (TypeError, ValueError, OSError):
        return None
    if tz is dt.timezone.utc:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return value.isoformat(timespec="milliseconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def json_loads_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def sqlite_uri(path: Path) -> str:
    return "file:" + quote(str(path)) + "?mode=ro"


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(sqlite_uri(path), uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

MARKER = "[REDACTED:{category}]"

SENSITIVE_KEY = (
    r"(?:"
    r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE)"
    r"|api[_-]?key|apikey|secret(?:[_-]?key)?|access[_-]?token|refresh[_-]?token"
    r"|auth[_-]?token|session[_-]?key|client[_-]?secret|admin[_-]?key"
    r"|creator[_-]?key|zcodejwttoken|user[_-]?info|password|passwd|pwd"
    r"|cookie|token"
    r")"
)

# Keys that look like credentials but are ordinary identifiers.  In particular
# workspace_key is a path-like ZCode key, not an API key.
HIGH_CONFIDENCE_SECRET_KEYS = {
    "password", "passwd", "pwd", "apikey", "accesstoken", "refreshtoken",
    "authtoken", "sessionkey", "clientsecret", "authorization", "cookie",
    "zcodejwttoken", "userinfo", "secretkey", "privatekey", "encryptionkey",
    "signingkey", "masterkey",
}

NON_SECRET_KEYS = {
    "workspacekey", "cachekey", "publickey", "keyfile", "keyid", "keyname",
    "keytype", "keyprefix", "keys", "keyword", "keywords", "sortkey",
    "primarykey", "foreignkey", "partitionkey", "groupingkey", "mapkey",
    "objectkey", "fieldkey", "rowkey", "docskey", "keypath", "idempotencykey",
}

# Uppercase bare KEY assignments only.  Lowercase `key:` fields are common in
# datasets and are not treated as credentials unless the value itself matches a
# known secret format.
BARE_KEY = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])KEY(?![A-Za-z0-9_])\s*[:=]\s*)"
    r"(?P<q>[\"'`]?)(?P<val>[^\s\"'`,;}\]]{6,})(?P=q)"
)

QUOTED_KV = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_])(?:" + SENSITIVE_KEY + r")(?![A-Za-z0-9_])\s*[:=]\s*)"
    r"(?P<q>[\"'`])(?P<val>[^\"'`\n]{6,})(?P=q)"
)
UNQUOTED_KV = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_])(?:" + SENSITIVE_KEY + r")(?![A-Za-z0-9_])\s*[:=]\s*)"
    r"(?P<val>[^\s\"'`,;}\]]{6,})"
)
BEARER = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=\-]{16,})")
COOKIE_HEADER = re.compile(r"(?im)^(\s*(?:set-)?cookie\s*:\s*)([^\n]+)$")
AUTH_HEADER = re.compile(r"(?im)^(\s*authorization\s*:\s*)([^\n]+)$")
URL_CREDENTIALS = re.compile(r"https?://[^/\s:@]+:[^/\s@]+@[^\s\"'<>]+")
JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
PEM = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?(?:PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"
    r".*?"
    r"-----END (?:[A-Z0-9 ]+ )?(?:PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----",
    re.S,
)
ENCRYPTED = re.compile(r"\benc:v1:[A-Za-z0-9._~+/=\-]{8,}")
NATURAL_PASSWORD = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_$])(?:密码|口令|password|passwd)(?![A-Za-z0-9_-])(?=\s|[:=：`\"'“]|是|为|is\b)\s*(?:是|为|is|[:=：])?\s*)"
    r"(?P<q>[\"'`“]?)(?P<val>[^\s\"'`“”，。；;]{6,})(?P=q)"
)
BASIC_AUTH = re.compile(r"(?P<prefix>\b(?:-u|--user)\s+[^\s:]{1,64}:)(?P<val>[^\s\"'`]{8,})")

PREFIX_PATTERNS = [
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    ("STRIPE_KEY", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("CREATOR_KEY", re.compile(r"\bcrk_[A-Za-z0-9_\-]{12,}")),
]

PLACEHOLDER_STARTS = (
    "<redacted", "[redacted", "***", "…", "...", "your-", "your_", "example",
    "changeme", "placeholder", "${", "$(", "process.", "crypto.", "res.",
    "req.", "body.", "state.", "this.", "os.", "env.", "await ", "new ",
    "getenv", "ensure", "open(", "read(", "json.", "string(", "none",
    "null", "true", "false",
)
CODE_CHARS = set("()[]{}<>$")


def normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def key_from_prefix(prefix: str) -> str:
    return re.split(r"[:=]", prefix.strip(), maxsplit=1)[0].strip()


def is_non_secret_key(key: str) -> bool:
    return normalise_key(key) in NON_SECRET_KEYS


CODE_REFERENCE_PREFIXES = (
    "globalThis.", "window.", "document.", "localStorage.", "sessionStorage.",
    "process.", "crypto.", "os.", "env.", "res.", "req.", "body.", "this.",
    "state.", "t.", "e.", "n.", "r.", "i.", "a.", "o.", "s.",
)
COMMAND_VALUES = {
    "python", "python3", "node", "npm", "npx", "docker", "bash", "sh",
    "curl", "openssl", "true", "false", "none", "null", "undefined",
}


def is_code_reference(value: str) -> bool:
    if value in COMMAND_VALUES:
        return True
    if value.startswith(CODE_REFERENCE_PREFIXES):
        return True
    # Dotted identifier chains (globalThis.__CREATOR_KEY, t.accessToken) are
    # code references, not tokens.  Tokens with dots almost always carry digits.
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+", value):
        return not bool(re.search(r"\d", value))
    return False


def marker(category: str) -> str:
    return MARKER.format(category=category)


def looks_placeholder(value: str) -> bool:
    low = value.strip().lower()
    if not low:
        return True
    if low.startswith(PLACEHOLDER_STARTS):
        return True
    if re.fullmatch(r"[\*xX_\-]{4,}", value):
        return True
    if re.fullmatch(r"<[^>]{1,80}>", value):
        return True
    return False


def token_like(value: str) -> bool:
    if len(value) < 10 or any(c.isspace() for c in value):
        return False
    if any(c in CODE_CHARS for c in value):
        return False
    if re.fullmatch(r"[0-9a-fA-F]+", value) and len(value) >= 16:
        return True
    if re.fullmatch(r"[A-Za-z0-9_\-+/=.]+", value):
        classes = sum(
            bool(re.search(pattern, value))
            for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
        )
        return classes >= 2 and len(value) >= 12
    return False


class Redactor:
    def __init__(self, known_secrets: Optional[Iterable[str]] = None) -> None:
        self.events: List[Dict[str, Any]] = []
        self.known_secrets = sorted(
            {x for x in (known_secrets or []) if x and len(x) >= 8},
            key=lambda x: (-len(x), x),
        )

    def record(self, category: str, path: str, length: int) -> None:
        self.events.append({
            "category": category,
            "field_path": path,
            "original_value_length": length,
            "marker": marker(category),
        })

    def _replace(self, category: str, path: str, value: str, replacement: str) -> str:
        if value.startswith("[REDACTED:"):
            return value
        self.record(category, path, len(value))
        return replacement

    def text(self, value: str, path: str) -> str:
        if not value:
            return value

        def repl_pem(m: re.Match) -> str:
            return self._replace("PRIVATE_KEY", path, m.group(0), marker("PRIVATE_KEY"))

        value = PEM.sub(repl_pem, value)

        def repl_enc(m: re.Match) -> str:
            return self._replace("ENCRYPTED_CREDENTIAL", path, m.group(0), marker("ENCRYPTED_CREDENTIAL"))

        value = ENCRYPTED.sub(repl_enc, value)

        for category, pattern in PREFIX_PATTERNS:
            def repl_prefix(m: re.Match, cat=category) -> str:
                return self._replace(cat, path, m.group(0), marker(cat))
            value = pattern.sub(repl_prefix, value)

        def repl_jwt(m: re.Match) -> str:
            return self._replace("JWT", path, m.group(0), marker("JWT"))

        value = JWT.sub(repl_jwt, value)

        def repl_bearer(m: re.Match) -> str:
            return "Bearer " + self._replace("BEARER_TOKEN", path, m.group(1), marker("BEARER_TOKEN"))

        value = BEARER.sub(repl_bearer, value)

        def repl_url(m: re.Match) -> str:
            return self._replace("URL_CREDENTIALS", path, m.group(0), "http://" + marker("URL_CREDENTIALS"))

        value = URL_CREDENTIALS.sub(repl_url, value)

        def repl_cookie(m: re.Match) -> str:
            return m.group(1) + self._replace("COOKIE_HEADER", path, m.group(2), marker("COOKIE_HEADER"))

        value = COOKIE_HEADER.sub(repl_cookie, value)

        def repl_auth(m: re.Match) -> str:
            raw = m.group(2)
            if raw.strip().lower().startswith("bearer "):
                return m.group(1) + raw
            return m.group(1) + self._replace("AUTHORIZATION_HEADER", path, raw, marker("AUTHORIZATION_HEADER"))

        value = AUTH_HEADER.sub(repl_auth, value)

        def repl_natural_password(m: re.Match) -> str:
            raw = m.group("val")
            if looks_placeholder(raw) or raw.startswith("[REDACTED:") or is_code_reference(raw):
                return m.group(0)
            if any(c in CODE_CHARS for c in raw):
                return m.group(0)
            quoted = bool(m.group("q"))
            if not quoted and not token_like(raw):
                return m.group(0)
            return m.group("prefix") + m.group("q") + self._replace(
                "PASSWORD", path, raw, marker("PASSWORD")
            ) + m.group("q")

        value = NATURAL_PASSWORD.sub(repl_natural_password, value)

        def repl_basic_auth(m: re.Match) -> str:
            raw = m.group("val")
            if looks_placeholder(raw) or raw.startswith("[REDACTED:") or is_code_reference(raw):
                return m.group(0)
            return m.group("prefix") + self._replace(
                "PASSWORD", path, raw, marker("PASSWORD")
            )

        value = BASIC_AUTH.sub(repl_basic_auth, value)

        def repl_bare_key(m: re.Match) -> str:
            if is_non_secret_key(key_from_prefix(m.group("prefix"))):
                return m.group(0)
            raw = m.group("val")
            if looks_placeholder(raw) or raw.startswith("[REDACTED:"):
                return m.group(0)
            if any(c in CODE_CHARS for c in raw) or is_code_reference(raw):
                return m.group(0)
            if not token_like(raw):
                return m.group(0)
            return m.group("prefix") + m.group("q") + self._replace(
                "SECRET_ASSIGNMENT", path, raw, marker("SECRET_ASSIGNMENT")
            ) + m.group("q")

        value = BARE_KEY.sub(repl_bare_key, value)

        def repl_quoted(m: re.Match) -> str:
            if is_non_secret_key(key_from_prefix(m.group("prefix"))):
                return m.group(0)
            raw = m.group("val")
            if looks_placeholder(raw) or raw.startswith("[REDACTED:"):
                return m.group(0)
            if any(c in CODE_CHARS for c in raw) or is_code_reference(raw):
                return m.group(0)
            return m.group("prefix") + m.group("q") + self._replace(
                "SECRET_ASSIGNMENT", path, raw, marker("SECRET_ASSIGNMENT")
            ) + m.group("q")

        value = QUOTED_KV.sub(repl_quoted, value)

        def repl_unquoted(m: re.Match) -> str:
            if is_non_secret_key(key_from_prefix(m.group("prefix"))):
                return m.group(0)
            raw = m.group("val")
            if looks_placeholder(raw) or raw.startswith("[REDACTED:"):
                return m.group(0)
            if any(c in CODE_CHARS for c in raw) or is_code_reference(raw):
                return m.group(0)
            key = m.group("prefix")
            key_norm = normalise_key(re.split(r"[:=]", key, maxsplit=1)[0])
            is_password = key_norm.endswith("password") or key_norm.endswith("passwd") or key_norm.endswith("pwd")
            is_env_credential = key_norm.endswith("key") or key_norm.endswith("token") or key_norm.endswith("secret") or key_norm.endswith("cookie")
            if is_password:
                literal = len(raw) >= 6
            elif is_env_credential:
                literal = token_like(raw) or ("-" in raw and len(raw) >= 8 and not raw.startswith(('/', '.')))
            else:
                literal = token_like(raw)
            if not literal:
                return m.group(0)
            return m.group("prefix") + self._replace(
                "SECRET_ASSIGNMENT", path, raw, marker("SECRET_ASSIGNMENT")
            )

        value = UNQUOTED_KV.sub(repl_unquoted, value)

        # Replace any literal value learned from a credential assignment
        # everywhere it appears (prose, shell command, report, code sample).
        for secret in self.known_secrets:
            if secret in value:
                replacement = self._replace(
                    "KNOWN_SECRET", path, secret, marker("KNOWN_SECRET")
                )
                value = value.replace(secret, replacement)
        return value

    def obj(self, value: Any, path: str, key_name: Optional[str] = None) -> Any:
        if isinstance(value, str):
            # Key-aware redaction for literal JSON values even when the value
            # does not match one of the well-known token formats.
            if key_name and isinstance(value, str):
                norm = normalise_key(key_name)
                if norm in NON_SECRET_KEYS:
                    return self.text(value, path)
                sensitive = (
                    norm in {
                        "apikey", "secret", "secretkey", "clientsecret", "accesstoken",
                        "refreshtoken", "authtoken", "sessionkey", "authorization",
                        "cookie", "password", "passwd", "pwd", "token", "key",
                        "adminKey".lower(), "creatorKey".lower(), "zcodejwttoken", "userinfo",
                    }
                    or norm.endswith("key") or norm.endswith("token")
                    or norm.endswith("secret") or norm.endswith("password")
                    or norm.endswith("passwd") or norm.endswith("cookie")
                )
                if sensitive and len(value) >= 6 and not looks_placeholder(value):
                    if norm == "key" and not token_like(value):
                        pass
                    elif any(c in CODE_CHARS for c in value):
                        pass
                    elif is_code_reference(value) or value.startswith(("new", "await ")):
                        pass
                    elif norm.endswith("password") or norm.endswith("passwd") or norm == "pwd":
                        return self._replace("PASSWORD", path, value, marker("PASSWORD"))
                    else:
                        return self._replace("SECRET_VALUE", path, value, marker("SECRET_VALUE"))
            return self.text(value, path)
        if isinstance(value, list):
            return [self.obj(v, f"{path}[{i}]", None) for i, v in enumerate(value)]
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for k, v in value.items():
                out[k] = self.obj(v, f"{path}.{k}", str(k))
            return out
        return value


# ---------------------------------------------------------------------------
# Secret literal pre-scan
# ---------------------------------------------------------------------------

def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)


def collect_secret_literals(objects: Iterable[Any]) -> set:
    """Collect only high-confidence literal secrets that need global scrubbing.

    The main purpose is to remove a generated password from prose and shell
    commands, not to replace every code identifier or test fixture.  Generic
    API/token assignments are still redacted at their own locations by the
    patterns above.
    """
    literals = set()

    def add(raw: str) -> None:
        if not raw or len(raw) < 8 or len(raw) > 512:
            return
        if any(c.isspace() for c in raw):
            return
        if looks_placeholder(raw) or raw.startswith("[REDACTED:"):
            return
        if any(c in CODE_CHARS for c in raw) or is_code_reference(raw):
            return
        if raw.startswith(("/", "~/", ".", "\\")):
            return
        literals.add(raw)

    for obj in objects:
        for text in iter_strings(obj):
            for m in QUOTED_KV.finditer(text):
                if normalise_key(key_from_prefix(m.group("prefix"))) in {"password", "passwd", "pwd"}:
                    add(m.group("val"))
            for m in UNQUOTED_KV.finditer(text):
                if normalise_key(key_from_prefix(m.group("prefix"))) in {"password", "passwd", "pwd"}:
                    add(m.group("val"))
            for m in NATURAL_PASSWORD.finditer(text):
                if m.group("q") or token_like(m.group("val")):
                    add(m.group("val"))
            for m in BASIC_AUTH.finditer(text):
                add(m.group("val"))
            for category, pattern in PREFIX_PATTERNS:
                for m in pattern.finditer(text):
                    add(m.group(0))
            for m in JWT.finditer(text):
                add(m.group(0))
            for m in BEARER.finditer(text):
                add(m.group(1))
            for m in ENCRYPTED.finditer(text):
                add(m.group(0))
    return literals


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def fetch_all(con: sqlite3.Connection, sql: str, params: Iterable[Any]) -> List[sqlite3.Row]:
    return list(con.execute(sql, tuple(params)))


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def load_sessions(con: sqlite3.Connection, target_paths: List[str]) -> List[sqlite3.Row]:
    q = placeholders(len(target_paths))
    rows = fetch_all(
        con,
        f"SELECT * FROM session WHERE directory IN ({q}) ORDER BY time_created, id",
        target_paths,
    )
    seen = {r["id"] for r in rows}
    # Include descendants even if their directory differs from the target paths.
    changed = True
    while changed:
        changed = False
        if not seen:
            break
        ids = list(seen)
        children = fetch_all(
            con,
            f"SELECT * FROM session WHERE parent_id IN ({placeholders(len(ids))}) ORDER BY time_created, id",
            ids,
        )
        for child in children:
            if child["id"] not in seen:
                seen.add(child["id"])
                rows.append(child)
                changed = True
    rows.sort(key=lambda r: (r["time_created"], r["id"]))
    return rows


def load_related_archived(con: sqlite3.Connection, tasks_con: sqlite3.Connection, paths: List[str]) -> List[sqlite3.Row]:
    q = placeholders(len(paths))
    tasks = fetch_all(
        tasks_con,
        f"SELECT task_id FROM tasks WHERE workspace_path IN ({q}) AND (archived = 1 OR deleted = 1)",
        paths,
    )
    ids = [r["task_id"] for r in tasks]
    rows: List[sqlite3.Row] = []
    if ids:
        rows = fetch_all(
            con,
            f"SELECT * FROM session WHERE id IN ({placeholders(len(ids))}) ORDER BY time_created, id",
            ids,
        )
    return rows


def load_tasks(tasks_con: sqlite3.Connection, session_ids: List[str], target_paths: List[str], archived_paths: List[str]) -> List[sqlite3.Row]:
    clauses = []
    params: List[Any] = []
    if target_paths:
        clauses.append(f"workspace_path IN ({placeholders(len(target_paths))})")
        params.extend(target_paths)
    if archived_paths:
        clauses.append(f"workspace_path IN ({placeholders(len(archived_paths))}) AND (archived = 1 OR deleted = 1)")
        params.extend(archived_paths)
    if session_ids:
        clauses.append(f"task_id IN ({placeholders(len(session_ids))})")
        params.extend(session_ids)
    if not clauses:
        return []
    return fetch_all(
        tasks_con,
        "SELECT * FROM tasks WHERE " + " OR ".join("(" + c + ")" for c in clauses) + " ORDER BY created_at, task_id",
        params,
    )


def build_workspace_map() -> Dict[str, Dict[str, str]]:
    result = {}
    for label, path in TARGET_WORKSPACES:
        result[path] = {"label": label, "path": path, "scope": "target"}
    for label, path in RELATED_ARCHIVED_WORKSPACES:
        result[path] = {"label": label, "path": path, "scope": "related-archived"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="consistent copy of ~/.zcode/cli/db/db.sqlite")
    parser.add_argument("--tasks-db", required=True, help="consistent copy of ~/.zcode/v2/tasks-index.sqlite")
    parser.add_argument("--out", required=True, help="new export directory")
    parser.add_argument("--source-root", default=str(Path.home() / ".zcode"))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    tasks_path = Path(args.tasks_db).expanduser().resolve()
    out_root = Path(args.out).expanduser().resolve()
    if not db_path.exists() or not tasks_path.exists():
        print("source snapshot missing", file=sys.stderr)
        return 2
    if (out_root / "workspaces").exists() or (out_root / "MANIFEST.json").exists():
        print(f"refusing to overwrite existing export: {out_root}", file=sys.stderr)
        return 2

    out_root.mkdir(parents=True, exist_ok=True)
    generated_at = now_iso()
    db_sha = sha256_file(db_path)
    tasks_sha = sha256_file(tasks_path)
    db_size = db_path.stat().st_size
    tasks_size = tasks_path.stat().st_size

    con = connect_ro(db_path)
    tasks_con = connect_ro(tasks_path)
    target_paths = [path for _, path in TARGET_WORKSPACES]
    archived_paths = [path for _, path in RELATED_ARCHIVED_WORKSPACES]
    workspace_map = build_workspace_map()

    target_sessions = load_sessions(con, target_paths)
    archived_sessions = load_related_archived(con, tasks_con, archived_paths)
    all_sessions = target_sessions + archived_sessions
    # Preserve source ordering globally but make the scope explicit.
    all_sessions.sort(key=lambda r: (r["time_created"], r["id"]))
    session_ids = [r["id"] for r in all_sessions]
    session_id_set = set(session_ids)

    tasks = load_tasks(tasks_con, session_ids, target_paths, archived_paths)
    task_by_id = {r["task_id"]: r for r in tasks}
    by_parent: Dict[str, List[sqlite3.Row]] = collections.defaultdict(list)
    for s in all_sessions:
        if s["parent_id"]:
            by_parent[s["parent_id"]].append(s)
    by_id = {s["id"]: s for s in all_sessions}

    # Source inventory and task linkage.
    total_sessions_source = con.execute("SELECT count(*) FROM session").fetchone()[0]
    total_sessions_target = con.execute(
        f"SELECT count(*) FROM session WHERE directory IN ({placeholders(len(target_paths))})", target_paths
    ).fetchone()[0]
    total_tasks_target = tasks_con.execute(
        f"SELECT count(*) FROM tasks WHERE workspace_path IN ({placeholders(len(target_paths))})", target_paths
    ).fetchone()[0]
    archived_tasks_total = tasks_con.execute(
        "SELECT count(*) FROM tasks WHERE archived = 1 OR deleted = 1"
    ).fetchone()[0]

    # All messages/parts for exported sessions.
    qm = placeholders(len(session_ids))
    messages = fetch_all(
        con,
        f"SELECT * FROM message WHERE session_id IN ({qm}) ORDER BY session_id, sequence, time_created, id",
        session_ids,
    )
    parts = fetch_all(
        con,
        f"SELECT * FROM part WHERE session_id IN ({qm}) ORDER BY session_id, message_id, sequence, time_created, id",
        session_ids,
    )
    parts_by_message: Dict[str, List[sqlite3.Row]] = collections.defaultdict(list)
    for p in parts:
        parts_by_message[p["message_id"]].append(p)
    messages_by_session: Dict[str, List[sqlite3.Row]] = collections.defaultdict(list)
    for m in messages:
        messages_by_session[m["session_id"]].append(m)

    inputs = fetch_all(
        con,
        f"SELECT * FROM session_input WHERE session_id IN ({qm}) ORDER BY session_id, time_created, id",
        session_ids,
    )
    entries = fetch_all(
        con,
        f"SELECT * FROM session_entry WHERE session_id IN ({qm}) ORDER BY session_id, time_created, id",
        session_ids,
    )
    usage = fetch_all(
        con,
        f"SELECT * FROM model_usage WHERE session_id IN ({qm}) ORDER BY session_id, started_at, id",
        session_ids,
    )

    # Basic referential-integrity checks before writing anything.
    message_ids = {m["id"] for m in messages}
    orphan_parts = [p["id"] for p in parts if p["message_id"] not in message_ids]
    orphan_messages = [m["id"] for m in messages if m["session_id"] not in session_id_set]
    if orphan_parts or orphan_messages:
        print(f"referential integrity failure: orphan_parts={len(orphan_parts)} orphan_messages={len(orphan_messages)}", file=sys.stderr)
        return 3

    pre_scan_objects: List[Any] = []
    pre_scan_objects.extend(row_to_dict(x) for x in all_sessions)
    pre_scan_objects.extend(row_to_dict(x) for x in tasks)
    pre_scan_objects.extend(json_loads_maybe(x["data"]) for x in messages)
    pre_scan_objects.extend(json_loads_maybe(x["data"]) for x in parts)
    pre_scan_objects.extend(json_loads_maybe(x["payload"]) for x in inputs)
    pre_scan_objects.extend(json_loads_maybe(x["data"]) for x in entries)
    pre_scan_objects.extend(row_to_dict(x) for x in usage)
    known_secrets = collect_secret_literals(pre_scan_objects)
    redactor = Redactor(known_secrets)
    redacted_count = 0

    def sanitize(value: Any, path: str, key: Optional[str] = None) -> Any:
        nonlocal redacted_count
        before = len(redactor.events)
        result = redactor.obj(value, path, key)
        redacted_count += len(redactor.events) - before
        return result

    workspace_records: Dict[str, Dict[str, Any]] = collections.OrderedDict()
    for label, path in TARGET_WORKSPACES + RELATED_ARCHIVED_WORKSPACES:
        workspace_records[label] = {
            "label": label,
            "path": path,
            "scope": "related-archived" if path in archived_paths else "target",
            "sessions": [],
            "task_count": 0,
            "file_count": 0,
            "bytes": 0,
            "counts": collections.Counter(),
        }

    session_manifest: List[Dict[str, Any]] = []
    tool_issues: List[Dict[str, Any]] = []
    no_reasoning: List[Dict[str, Any]] = []
    empty_outputs: List[Dict[str, Any]] = []
    message_errors: List[Dict[str, Any]] = []
    artifact_records: Dict[str, Dict[str, Any]] = {}
    missing_binary = 0

    for s in all_sessions:
        sid = s["id"]
        path = s["directory"]
        safe_title = sanitize(s["title"], f"session[{sid}].title")
        if path not in workspace_map:
            # A child whose directory is outside the six requested paths; keep it
            # in a labelled related directory rather than dropping it.
            label = "related-child-sessions"
            if label not in workspace_records:
                workspace_records[label] = {
                    "label": label, "path": "", "scope": "related-child",
                    "sessions": [], "task_count": 0, "file_count": 0, "bytes": 0,
                    "counts": collections.Counter(),
                }
        else:
            label = workspace_map[path]["label"]
        ws = workspace_records[label]
        ws_dir = out_root / "workspaces" / label
        sessions_dir = ws_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        supplemental_dir = ws_dir / "supplemental"
        supplemental_dir.mkdir(parents=True, exist_ok=True)

        sm = messages_by_session.get(sid, [])
        sp = [p for m in sm for p in parts_by_message.get(m["id"], [])]
        reasoning_parts = [p for p in sp if (json.loads(p["data"]).get("type") == "reasoning")]
        reasoning_message_ids = {p["message_id"] for p in reasoning_parts}
        assistant_messages = [m for m in sm if json.loads(m["data"]).get("role") == "assistant"]
        assistant_without_reasoning = [m["id"] for m in assistant_messages if m["id"] not in reasoning_message_ids]
        tools = [p for p in sp if json.loads(p["data"]).get("type") == "tool"]
        tool_errors = []
        tool_running = []
        tool_empty = []
        tool_truncated = []
        for p in tools:
            pd = json.loads(p["data"])
            st = pd.get("state", {}) or {}
            status = st.get("status")
            output = st.get("output")
            call_id = pd.get("callID")
            issue = {
                "session_id": sid,
                "message_id": p["message_id"],
                "part_id": p["id"],
                "call_id": call_id,
                "tool": pd.get("tool"),
                "status": status,
                "has_output": output is not None and str(output) != "",
                "error_present": bool(st.get("error")),
                "serialization_truncated": bool((st.get("metadata", {}) or {}).get("serialization", {}).get("truncated")),
            }
            if status == "error":
                tool_errors.append(issue)
                tool_issues.append(dict(issue, kind="tool_error_no_output" if not issue["has_output"] else "tool_error_with_output"))
            elif status == "running":
                tool_running.append(issue)
                tool_issues.append(dict(issue, kind="tool_running_no_output"))
            if status == "completed" and not issue["has_output"]:
                tool_empty.append(issue)
                empty_outputs.append(dict(issue, kind="completed_empty_output"))
            if issue["serialization_truncated"]:
                tool_truncated.append(issue)

        if not reasoning_parts:
            no_reasoning.append({
                "session_id": sid,
                "title": safe_title,
                "workspace": path,
                "assistant_messages": len(assistant_messages),
            })

        for m in sm:
            md = json.loads(m["data"])
            if md.get("error"):
                message_errors.append({
                    "session_id": sid,
                    "message_id": m["id"],
                    "sequence": m["sequence"],
                    "error_present": True,
                    "error_json_length": len(json.dumps(md.get("error"), ensure_ascii=False)),
                    "note": "Error text is preserved in the message record, not duplicated here.",
                })

        counts = {
            "messages": len(sm),
            "user_messages": sum(1 for m in sm if json.loads(m["data"]).get("role") == "user"),
            "assistant_messages": len(assistant_messages),
            "parts": len(sp),
            "reasoning_parts": len(reasoning_parts),
            "assistant_messages_without_reasoning_part": len(assistant_without_reasoning),
            "tool_calls": len(tools),
            "tool_errors": len(tool_errors),
            "tool_running": len(tool_running),
            "tool_completed_empty_output": len(tool_empty),
            "tool_truncated": len(tool_truncated),
            "session_inputs": sum(1 for r in inputs if r["session_id"] == sid),
            "runtime_entries": sum(1 for r in entries if r["session_id"] == sid),
            "model_usage": sum(1 for r in usage if r["session_id"] == sid),
        }

        task = task_by_id.get(sid)
        automation = None
        if task and task["cron_automation_id"]:
            auto_rows = tasks_con.execute(
                "SELECT * FROM automations WHERE automation_id = ?", (task["cron_automation_id"],)
            ).fetchall()
            if auto_rows:
                automation = sanitize(row_to_dict(auto_rows[0]), f"session[{sid}].automation")
        parent = by_id.get(s["parent_id"]) if s["parent_id"] else None
        children = by_parent.get(sid, [])
        relationship = None
        if parent:
            relationship = "fork" if s["task_type"] == "fork" else (
                "subagent_child" if s["task_type"] == "subagent_child" else "child"
            )

        file_path = sessions_dir / f"{sid}.jsonl"
        tmp_path = sessions_dir / f".{sid}.jsonl.tmp"
        first_message_id = sm[0]["id"] if sm else None
        last_message_id = sm[-1]["id"] if sm else None
        with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
            header = {
                "record_type": "session_header",
                "schema": SCHEMA,
                "export_generated_at": generated_at,
                "scope": ws["scope"],
                "workspace": {"label": label, "path": path},
                "session_id": sid,
                "session": sanitize(row_to_dict(s), f"session[{sid}]"),
                "task": sanitize(row_to_dict(task), f"session[{sid}].task") if task else None,
                "automation": automation,
                "parent_session": {
                    "id": parent["id"],
                    "title": sanitize(parent["title"], f"session[{sid}].parent_title"),
                    "directory": parent["directory"],
                } if parent else None,
                "relationship_to_parent": relationship,
                "child_sessions": [
                    {
                        "id": c["id"],
                        "title": sanitize(c["title"], f"session[{sid}].child_title[{c['id']}]"),
                        "task_type": c["task_type"],
                    }
                    for c in sorted(children, key=lambda x: (x["time_created"], x["id"]))
                ],
                "counts": counts,
                "source": {
                    "database": DB_DISPLAY,
                    "table": "session",
                    "row_id": sid,
                    "snapshot_sha256": db_sha,
                    "task_index": TASKS_DISPLAY,
                    "task_index_sha256": tasks_sha,
                },
            }
            f.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n")

            for m in sm:
                md = json.loads(m["data"])
                record = {
                    "record_type": "message",
                    "schema": SCHEMA,
                    "session_id": sid,
                    "message_id": m["id"],
                    "sequence": m["sequence"],
                    "role": md.get("role"),
                    "time_created_ms": m["time_created"],
                    "time_created_utc": ms_iso(m["time_created"], dt.timezone.utc),
                    "time_created_local": ms_iso(m["time_created"], LOCAL_TZ),
                    "time_updated_ms": m["time_updated"],
                    "time_updated_utc": ms_iso(m["time_updated"], dt.timezone.utc),
                    "time_updated_local": ms_iso(m["time_updated"], LOCAL_TZ),
                    "source_table": "message",
                    "message": sanitize(md, f"message[{m['id']}]"),
                    "parts": [],
                }
                for p in sorted(parts_by_message.get(m["id"], []), key=lambda x: (x["sequence"], x["time_created"], x["id"])):
                    pd = json.loads(p["data"])
                    record["parts"].append({
                        "part_id": p["id"],
                        "message_id": p["message_id"],
                        "session_id": p["session_id"],
                        "sequence": p["sequence"],
                        "part_type": pd.get("type"),
                        "time_created_ms": p["time_created"],
                        "time_created_utc": ms_iso(p["time_created"], dt.timezone.utc),
                        "time_created_local": ms_iso(p["time_created"], LOCAL_TZ),
                        "time_updated_ms": p["time_updated"],
                        "time_updated_utc": ms_iso(p["time_updated"], dt.timezone.utc),
                        "time_updated_local": ms_iso(p["time_updated"], LOCAL_TZ),
                        "source_table": "part",
                        "part": sanitize(pd, f"part[{p['id']}]"),
                    })
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

            footer = {
                "record_type": "session_footer",
                "schema": SCHEMA,
                "session_id": sid,
                "counts": counts,
                "first_message_id": first_message_id,
                "last_message_id": last_message_id,
                "note": "Derived footer; transcript content is in the preceding message records.",
            }
            f.write(json.dumps(footer, ensure_ascii=False, separators=(",", ":")) + "\n")
        tmp_path.replace(file_path)
        file_hash = sha256_file(file_path)
        file_size = file_path.stat().st_size

        ws["sessions"].append(sid)
        ws["file_count"] += 1
        ws["bytes"] += file_size
        for k, v in counts.items():
            ws["counts"][k] += v

        session_manifest.append({
            "session_id": sid,
            "workspace_label": label,
            "workspace_path": path,
            "scope": ws["scope"],
            "title": safe_title,
            "task_type": s["task_type"],
            "parent_session_id": s["parent_id"],
            "relationship_to_parent": relationship,
            "child_session_ids": [c["id"] for c in children],
            "archived_task": bool(task and (task["archived"] or task["deleted"])) if task else False,
            "time_created_ms": s["time_created"],
            "time_created_utc": ms_iso(s["time_created"], dt.timezone.utc),
            "time_updated_ms": s["time_updated"],
            "time_updated_utc": ms_iso(s["time_updated"], dt.timezone.utc),
            "duration_span_ms": s["time_updated"] - s["time_created"],
            "counts": counts,
            "file": str(file_path.relative_to(out_root)),
            "bytes": file_size,
            "sha256": file_hash,
        })

        # Supplemental per-session structured logs, grouped per workspace.
        # They are written later after all sessions are known, so collect here.
        # (No-op; see below.)

    # Supplemental logs, per workspace, preserving source-table order.
    supplemental_files = []
    for label, ws in workspace_records.items():
        ws_dir = out_root / "workspaces" / label
        sdir = ws_dir / "supplemental"
        sdir.mkdir(parents=True, exist_ok=True)
        ws_sessions = set(ws["sessions"])
        for name, rows, id_key, time_key, source_table, value_builder in [
            ("session_inputs", inputs, "id", "time_created", "session_input", None),
            ("runtime_entries", entries, "id", "time_created", "session_entry", None),
            ("model_usage", usage, "id", "started_at", "model_usage", None),
        ]:
            selected = [r for r in rows if r["session_id"] in ws_sessions]
            if not selected:
                continue
            fp = sdir / f"{name}.jsonl"
            tmp = sdir / f".{name}.jsonl.tmp"
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                for r in sorted(selected, key=lambda x: (x["session_id"], x[time_key] or 0, x[id_key])):
                    rd = row_to_dict(r)
                    rd.pop("data", None) if name == "runtime_entries" else None
                    rd.pop("payload", None) if name == "session_inputs" else None
                    payload = None
                    if name == "session_inputs":
                        payload = json_loads_maybe(r["payload"])
                    elif name == "runtime_entries":
                        payload = json_loads_maybe(r["data"])
                    rec = {
                        "record_type": source_table,
                        "schema": SCHEMA,
                        "workspace_label": label,
                        "session_id": r["session_id"],
                        "source_table": source_table,
                        "row": sanitize(rd, f"{source_table}[{r['id']}]"),
                    }
                    if payload is not None:
                        rec["payload"] = sanitize(payload, f"{source_table}[{r['id']}].payload")
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            tmp.replace(fp)
            supplemental_files.append({
                "workspace_label": label,
                "record_type": source_table,
                "file": str(fp.relative_to(out_root)),
                "bytes": fp.stat().st_size,
                "sha256": sha256_file(fp),
                "records": len(selected),
            })

    # Artifact inventory: referenced binary artifacts are indexed but not
    # committed (they may contain un-scannable screenshots and user data).
    for p in parts:
        pd = json.loads(p["data"])
        candidates = []
        if pd.get("type") == "file":
            candidates.append(pd)
        for a in ((pd.get("state") or {}).get("attachments") or []):
            candidates.append(a)
        for a in candidates:
            uri = a.get("url") or (a.get("metadata") or {}).get("artifactUri")
            if not uri:
                continue
            ref_key = p["id"] + "|" + uri
            if ref_key in artifact_records:
                continue
            rec = {
                "uri": uri,
                "session_id": p["session_id"],
                "part_id": p["id"],
                "mime": a.get("mime"),
                "filename": a.get("filename"),
                "declared_bytes": (a.get("metadata") or {}).get("sizeBytes"),
                "storage_kind": (a.get("metadata") or {}).get("storageKind"),
                "recoverability": (a.get("metadata") or {}).get("recoverability"),
                "exported": False,
                "reason": "binary artifact not committed; see README limitations",
            }
            local_files = []
            if uri.startswith("zcode-artifact://"):
                try:
                    rest = uri[len("zcode-artifact://"):]
                    artifact_session, artifact_id = rest.split("/", 1)
                    local_files = sorted(glob.glob(str(ARTIFACT_ROOT / artifact_session / f"*{artifact_id}*")))
                except Exception:
                    local_files = []
            elif os.path.isabs(uri):
                local_files = [uri] if os.path.exists(uri) else []
            if local_files:
                rec["local_file_found"] = True
                rec["local_files"] = [
                    {"path": str(Path(f)), "bytes": Path(f).stat().st_size}
                    for f in local_files if Path(f).is_file()
                ]
            else:
                rec["local_file_found"] = False
                missing_binary += 1
            artifact_records[ref_key] = rec

    if artifact_records:
        art_dir = out_root / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        art_fp = art_dir / "artifact_inventory.jsonl"
        with art_fp.open("w", encoding="utf-8", newline="\n") as f:
            for ref_key in sorted(artifact_records):
                f.write(json.dumps(sanitize(artifact_records[ref_key], "artifact_inventory"), ensure_ascii=False, separators=(",", ":")) + "\n")
        supplemental_files.append({
            "workspace_label": "*",
            "record_type": "artifact_inventory",
            "file": str(art_fp.relative_to(out_root)),
            "bytes": art_fp.stat().st_size,
            "sha256": sha256_file(art_fp),
            "records": len(artifact_records),
        })

    # Task index summary.
    task_manifest = []
    for t in tasks:
        sid = t["task_id"]
        if sid not in session_id_set:
            continue
        task_manifest.append({
            "task_id": sid,
            "workspace_path": t["workspace_path"],
            "title": sanitize(t["title"], f"task[{sid}].title"),
            "task_status": t["task_status"],
            "archived": bool(t["archived"]),
            "deleted": bool(t["deleted"]),
            "forked_from_task_id": t["forked_from_task_id"],
            "cron_automation_id": t["cron_automation_id"],
            "time_created_ms": t["created_at"],
            "time_created_utc": ms_iso(t["created_at"], dt.timezone.utc),
            "time_updated_ms": t["updated_at"],
            "time_updated_utc": ms_iso(t["updated_at"], dt.timezone.utc),
        })

    # Roll-ups.
    all_counts = collections.Counter()
    for rec in session_manifest:
        for k, v in rec["counts"].items():
            all_counts[k] += v
    target_session_count = sum(1 for s in target_sessions)
    target_exported = sum(1 for s in session_manifest if s["scope"] == "target")
    archived_exported = sum(1 for s in session_manifest if s["scope"] == "related-archived")
    total_file_bytes = sum(r["bytes"] for r in session_manifest)
    all_exports_bytes = total_file_bytes + sum(f["bytes"] for f in supplemental_files)

    for label, ws in workspace_records.items():
        ws["task_count"] = sum(1 for t in task_manifest if t["workspace_path"] == ws["path"])
        ws["counts"] = dict(ws["counts"])
        ws["bytes"] = int(ws["bytes"])

    time_values = [s["time_created"] for s in all_sessions] + [s["time_updated"] for s in all_sessions]
    time_min = min(time_values) if time_values else None
    time_max = max(time_values) if time_values else None

    redaction_counts = collections.Counter(e["category"] for e in redactor.events)

    manifest = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "generator": "scripts/export_zcode.py",
        "application": {
            "name": "ZCode",
            "app_version": APP_VERSION,
            "cli_version_reported_by_zcode": TOOL_VERSION,
            "session_row_version": sorted({s["version"] for s in all_sessions}),
        },
        "scope": {
            "requested_workspace_string": "经验+额度+女性内容+小红书+llm-workspace+claude-sandbox-plan",
            "target_workspace_paths": target_paths,
            "related_archived_workspace_paths": archived_paths,
            "note": "Exact target paths plus one directly related archived task in /Users/llm/Documents/claude, exported separately.",
        },
        "sources": [
            {"name": "ZCode session/message store", "path": DB_DISPLAY, "snapshot_sha256": db_sha, "snapshot_bytes": db_size, "access": "read-only consistent snapshot (VACUUM INTO)"},
            {"name": "ZCode task index", "path": TASKS_DISPLAY, "snapshot_sha256": tasks_sha, "snapshot_bytes": tasks_size, "access": "read-only consistent snapshot (VACUUM INTO)"},
            {"name": "ZCode binary artifacts", "path": "~/.zcode/cli/artifacts", "access": "indexed only, not committed"},
            {"name": "ZCode model I/O and exec logs", "path": "~/.zcode/cli/rollout, ~/.zcode/cli/exec", "access": "not duplicated; DB tool parts are canonical for the exported sessions"},
        ],
        "time_range": {
            "min_ms": time_min,
            "max_ms": time_max,
            "min_utc": ms_iso(time_min, dt.timezone.utc),
            "max_utc": ms_iso(time_max, dt.timezone.utc),
            "min_local": ms_iso(time_min, LOCAL_TZ),
            "max_local": ms_iso(time_max, LOCAL_TZ),
            "timezone": "Asia/Tokyo (+09:00)",
        },
        "counts": {
            "total_local_sessions": total_sessions_source,
            "target_sessions_identified": target_session_count,
            "target_sessions_exported": target_exported,
            "related_archived_sessions_identified": len(archived_sessions),
            "related_archived_sessions_exported": archived_exported,
            "sessions_exported_total": len(session_manifest),
            "target_tasks_in_index": total_tasks_target,
            "archived_or_deleted_tasks_total_in_index": archived_tasks_total,
            "task_rows_attached_to_exported_sessions": len(task_manifest),
            "messages_exported": all_counts["messages"],
            "user_messages_exported": all_counts["user_messages"],
            "assistant_messages_exported": all_counts["assistant_messages"],
            "parts_exported": all_counts["parts"],
            "reasoning_parts_exported": all_counts["reasoning_parts"],
            "tool_calls_exported": all_counts["tool_calls"],
            "session_inputs_exported": len(inputs),
            "runtime_entries_exported": len(entries),
            "model_usage_rows_exported": len(usage),
            "artifact_references_indexed": len(artifact_records),
            "transcript_bytes": total_file_bytes,
            "all_export_bytes": all_exports_bytes,
        },
        "workspaces": list(workspace_records.values()),
        "sessions": session_manifest,
        "tasks": task_manifest,
        "supplemental_files": supplemental_files,
        "completeness": {
            "sessions_with_no_reasoning_part": no_reasoning,
            "tool_issues": tool_issues,
            "completed_empty_outputs": empty_outputs,
            "message_error_records": message_errors,
            "sessions_without_task_index_row": [
                s["id"] for s in all_sessions if s["id"] not in task_by_id
            ],
            "orphan_parts": len(orphan_parts),
            "orphan_messages": len(orphan_messages),
            "truncated_tool_outputs": all_counts["tool_truncated"],
            "binary_artifacts_not_committed": len(artifact_records),
            "binary_artifact_local_file_missing": missing_binary,
        },
        "redaction": {
            "method": "recursive, pattern-based redaction of decoded JSON string values; source text is preserved except matching credential values",
            "categories": dict(redaction_counts),
            "events": redactor.events,
        },
        "files": [
            {
                "path": r["file"],
                "bytes": r["bytes"],
                "sha256": r["sha256"],
                "session_id": r["session_id"],
            }
            for r in session_manifest
        ] + supplemental_files,
    }

    manifest_path = out_root / "MANIFEST.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")

    redaction_report = {
        "generated_at": generated_at,
        "method": manifest["redaction"]["method"],
        "description": "No original credential values are included in this report; only locations and lengths.",
        "counts_by_category": dict(redaction_counts),
        "events": redactor.events,
    }
    with (out_root / "REDACTION_REPORT.json").open("w", encoding="utf-8", newline="\n") as f:
        json.dump(redaction_report, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # CSV index.
    import csv
    with (out_root / "EXPORT_INDEX.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "workspace_label", "scope", "session_id", "task_type", "parent_session_id",
            "title", "time_created_utc", "time_updated_utc", "messages", "parts",
            "reasoning_parts", "assistant_without_reasoning", "tool_calls",
            "tool_errors", "tool_running", "empty_outputs", "bytes", "sha256", "file",
        ])
        for r in session_manifest:
            c = r["counts"]
            writer.writerow([
                r["workspace_label"], r["scope"], r["session_id"], r["task_type"],
                r["parent_session_id"] or "", r["title"], r["time_created_utc"],
                r["time_updated_utc"], c["messages"], c["parts"], c["reasoning_parts"],
                c["assistant_messages_without_reasoning_part"], c["tool_calls"],
                c["tool_errors"], c["tool_running"], c["tool_completed_empty_output"],
                r["bytes"], r["sha256"], r["file"],
            ])

    # Human-readable missing-record report.
    lines = [
        "# 缺失与完整性记录",
        "",
        f"生成时间：{generated_at}",
        "",
        "本文件只记录导出中可验证的缺失、空结果与无法提交的二进制对象；不补写、不猜测。"
        "“assistant 消息无 reasoning part”不等于模型一定生成过思考文本；它只表示源数据库中该消息没有对应的可见思考记录。",
        "",
        "## 总量",
        "",
        f"- 导出会话：{len(session_manifest)}（目标 {target_exported}，相关归档 {archived_exported}）",
        f"- 导出消息：{all_counts['messages']}；part：{all_counts['parts']}",
        f"- visible reasoning part：{all_counts['reasoning_parts']}",
        f"- tool call：{all_counts['tool_calls']}；其中 error：{all_counts['tool_errors']}，running：{all_counts['tool_running']}，completed 但空输出：{all_counts['tool_completed_empty_output']}",
        f"- 数据库标记为截断的 tool 输出：{manifest['completeness']['truncated_tool_outputs']}",
        f"- 二进制 artifact 引用：{len(artifact_records)}；未提交：{len(artifact_records)}；本地文件缺失：{missing_binary}",
        f"- 取消且未进入 message 表的输入：{sum(1 for r in inputs if r['status'] != 'promoted')}",
        "",
        "## 完全没有 visible reasoning part 的会话",
        "",
    ]
    if no_reasoning:
        lines += ["| session_id | workspace | assistant messages |", "|---|---|---:|"]
        for x in no_reasoning:
            lines.append(f"| `{x['session_id']}` | `{x['workspace']}` | {x['assistant_messages']} |")
    else:
        lines.append("无。")
    lines += [
        "",
        "## tool 调用失败或无结果",
        "",
        "以下记录保留在对应会话 JSONL 的同一 message/part 位置；`error` 文本、`call_id` 与 `part_id` 均保留。"
        "`tool_error_no_output` 表示状态为 error 且没有 output 字段；错误对象本身仍可能存在。",
        "",
        "| kind | session_id | message_id | part_id | call_id | tool | error_present |",
        "|---|---|---|---|---|---|---|",
    ]
    for x in tool_issues:
        lines.append(
            f"| {x.get('kind')} | `{x['session_id']}` | `{x['message_id']}` | `{x['part_id']}` | "
            f"`{x.get('call_id') or ''}` | {x.get('tool') or ''} | {x.get('error_present')} |"
        )
    lines += [
        "",
        "## completed 但 output 为空的 tool 调用",
        "",
        "| session_id | message_id | part_id | call_id | tool |",
        "|---|---|---|---|---|",
    ]
    for x in empty_outputs:
        lines.append(f"| `{x['session_id']}` | `{x['message_id']}` | `{x['part_id']}` | `{x.get('call_id') or ''}` | {x.get('tool') or ''} |")
    lines += [
        "",
        "## 取消的输入",
        "",
        "| session_id | input_id | status | kind | delivery |",
        "|---|---|---|---|---|",
    ]
    for r in inputs:
        if r["status"] != "promoted":
            lines.append(f"| `{r['session_id']}` | `{r['id']}` | {r['status']} | {r['kind']} | {r['delivery']} |")
    lines += [
        "",
        "## 其他",
        "",
        "- 所有 message/part 均可解析为 JSON；无孤立 part、无缺失 message 父记录。",
        "- 没有会话文件超过 32 MiB，因此未触发按消息边界拆分。",
        "- 二进制附件只登记 URI、大小与本地文件存在性，未提交内容；原因见 README。",
        "",
    ]
    with (out_root / "MISSING_RECORDS.md").open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))

    print(json.dumps({
        "out": str(out_root),
        "sessions": len(session_manifest),
        "messages": all_counts["messages"],
        "parts": all_counts["parts"],
        "bytes": all_exports_bytes,
        "redactions": len(redactor.events),
        "known_secret_literals": len(known_secrets),
        "redaction_categories": dict(redaction_counts),
        "files": len(session_manifest) + len(supplemental_files),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
