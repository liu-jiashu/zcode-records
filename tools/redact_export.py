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
OUT = Path(r"D:\zcode-records")
CREDENTIALS = Path.home() / ".zcode" / "v2" / "credentials.json"

MARKER = {
    "API_KEY": "[[REDACTED:API_KEY]]",
    "ACCESS_TOKEN": "[[REDACTED:ACCESS_TOKEN]]",
    "REFRESH_TOKEN": "[[REDACTED:REFRESH_TOKEN]]",
    "JWT": "[[REDACTED:JWT]]",
    "PASSWORD": "[[REDACTED:PASSWORD]]",
    "PASSWORD_HASH": "[[REDACTED:PASSWORD_HASH]]",
    "COOKIE": "[[REDACTED:COOKIE]]",
    "AUTHORIZATION": "[[REDACTED:AUTHORIZATION]]",
    "PRIVATE_KEY": "[[REDACTED:PRIVATE_KEY]]",
    "SECRET": "[[REDACTED:SECRET]]",
    "CREDENTIAL": "[[REDACTED:CREDENTIAL]]",
}

PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN ((?:[A-Z0-9]+ )?PRIVATE KEY)-----.*?-----END \1-----", re.S
)
PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )?PRIVATE KEY)-----")
AUTH_HEADER = re.compile(
    r"(?im)^(\s*(?:proxy-)?authorization\s*:\s*)((?:bearer|basic|token)\s+)?([^\s\"',;]+)"
)
COOKIE_HEADER = re.compile(r"(?im)^(\s*(?:set-)?cookie\s*:\s*)([^\r\n]+)")
ENV_ASSIGN = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]{2,})(\s*=\s*)(?:([\"'])([^\s\"'`]{3,})\3|([^\s\"'`]{3,}))"
)
CLI_FLAG = re.compile(
    r"(?i)(--(?:api[-_]?key|access[-_]?token|refresh[-_]?token|token|secret|password|passwd|cookie)(?:=|\s+))([^\s\"'`]{4,})"
)
QUERY_PARAM = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|sig|signature)=)([^&\s\"'<>]{4,})"
)
JSONISH_SECRET = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret[_-]?key|password|passwd|pwd|cookie|authorization)[\"']?\s*[:=]\s*[\"'])([^\"'\s,}]{4,})([\"'])"
)
URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]{3,})(@)")
CHINESE_LABEL = re.compile(
    r"(API\s*[Kk]ey|接口密钥|访问令牌|刷新令牌|私钥|密钥|令牌|密码|口令)(\s*[:：=]\s*)([^\s，。；;\"'`]{4,})"
)
BEARER_ANY = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
BASIC_ANY = re.compile(r"(?i)\b(Basic\s+)([A-Za-z0-9+/=]{8,})")
OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
GITLAB_TOKEN = re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b")
SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
GOOGLE_KEY = re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")
AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
HF_TOKEN = re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")
NPM_TOKEN = re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b")
PYPI_TOKEN = re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
BIGMODEL_KEY = re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b")


def category_for_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    if "jwt" in n:
        return "JWT"
    if "refresh" in n:
        return "REFRESH_TOKEN"
    if "pass" in n and "hash" in n:
        return "PASSWORD_HASH"
    if "password" in n or "passwd" in n or n.endswith("pwd"):
        return "PASSWORD"
    if "cookie" in n:
        return "COOKIE"
    if "authorization" in n:
        return "AUTHORIZATION"
    if "privatekey" in n or n.endswith("privatekey"):
        return "PRIVATE_KEY"
    if "secret" in n:
        return "SECRET"
    if "token" in n:
        return "ACCESS_TOKEN"
    if "apikey" in n:
        return "API_KEY"
    return "CREDENTIAL"


def sensitive_key(name: str) -> str | None:
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    if not n:
        return None
    excluded_ends = (
        "env",
        "name",
        "type",
        "url",
        "uri",
        "path",
        "file",
        "length",
        "len",
        "count",
        "budget",
        "enabled",
        "required",
        "hint",
        "label",
        "format",
        "help",
    )
    if n.endswith(excluded_ends):
        return None
    exact = {
        "apikey",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "authtoken",
        "bearertoken",
        "sessiontoken",
        "oauthtoken",
        "token",
        "secret",
        "clientsecret",
        "secretkey",
        "password",
        "passwd",
        "pwd",
        "passhash",
        "passwordhash",
        "adminpassword",
        "dbpassword",
        "cookie",
        "setcookie",
        "authorization",
        "proxyauthorization",
        "privatekey",
    }
    if n in exact:
        return category_for_name(n)
    for suffix in (
        "apikey",
        "accesstoken",
        "refreshtoken",
        "password",
        "passwd",
        "clientsecret",
        "secretkey",
        "secret",
        "privatekey",
        "cookie",
        "authorization",
        "token",
    ):
        if n.endswith(suffix):
            return category_for_name(suffix)
    return None


def flatten_strings(obj: Any, path: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(flatten_strings(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(flatten_strings(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        out.append((path, obj))
    return out


def collect_exact_secrets() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if CREDENTIALS.is_file():
        try:
            data = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for path, value in flatten_strings(data):
            leaf = path.split(".")[-1]
            cat = sensitive_key(leaf)
            if cat and len(value) >= 12:
                result.append((value, cat))
    for name, value in os.environ.items():
        cat = sensitive_key(name)
        if cat and isinstance(value, str) and len(value) >= 12:
            result.append((value, cat))
    # Longest first so a token that contains another token cannot leave a suffix.
    dedup: dict[str, str] = {}
    for value, cat in result:
        if value and value not in dedup:
            dedup[value] = cat
    return sorted(dedup.items(), key=lambda kv: len(kv[0]), reverse=True)


def looks_like_code(value: str) -> bool:
    v = value.strip()
    if not v:
        return True
    if v.lower() in {"true", "false", "none", "null", "yes", "no"}:
        return True
    if v.startswith("...") and set(v) <= set(".\\nrt "):
        return True
    if v[0] in "$({[`\"'":
        return True
    if re.match(
        r"(?i)^(?:os\.|process\.|environ\b|getenv\b|config\.|settings\.|secrets\.|re\.|none$|null$|true$|false$|yes$|no$|\.\.\.$|\*\*\*$)",
        v,
    ):
        return True
    return False


def replace_with_marker(text: str, marker: str) -> str:
    return text.replace(marker, marker)  # idempotent guard


def apply_pattern(text: str, pattern: re.Pattern, category: str, counts: Counter) -> str:
    def repl(m: re.Match) -> str:
        marker = MARKER[category]
        if marker in m.group(0):
            return m.group(0)
        counts[category] += 1
        groups = m.groups()
        if category == "PRIVATE_KEY":
            return f"-----BEGIN {m.group(1)}-----\n{marker}\n-----END {m.group(1)}-----"
        if len(groups) == 1:
            return marker
        if len(groups) == 2:
            # Prefix-preserving rules: keep group 1, replace group 2.
            return f"{groups[0]}{marker}"
        if len(groups) == 3:
            # Header/optional scheme/value, or key/separator/quote/value/quote family.
            return f"{groups[0]}{groups[1] or ''}{marker}{groups[2] if groups[2].startswith(chr(39)) or groups[2].startswith(chr(34)) else ''}"
        return marker

    return pattern.sub(repl, text)


def redact_text(text: str, exact: list[tuple[str, str]], counts: Counter) -> str:
    if not isinstance(text, str) or not text:
        return text
    for value, cat in exact:
        if value and value in text:
            n = text.count(value)
            counts[f"exact_{cat.lower()}"] += n
            text = text.replace(value, MARKER[cat])

    block_count = len(PRIVATE_KEY_BLOCK.findall(text))
    if block_count:
        counts["private_key_block"] += block_count
        text = PRIVATE_KEY_BLOCK.sub(
            lambda m: (
                f"-----BEGIN {m.group(1)}-----\n{MARKER['PRIVATE_KEY']}\n-----END {m.group(1)}-----"
            ),
            text,
        )

    # A header without a key body is still a private-key marker. Redact it
    # unless it is the retained header of a complete, already-redacted block.
    def repl_lone_header(m: re.Match) -> str:
        tail = text[m.end() : m.end() + 80]
        if MARKER["PRIVATE_KEY"] in tail:
            return m.group(0)
        counts["private_key_header_only"] += 1
        return "[[REDACTED:PRIVATE_KEY_HEADER]]"

    text = PRIVATE_KEY_HEADER.sub(repl_lone_header, text)

    def simple(pattern: re.Pattern, cat: str) -> None:
        nonlocal text
        def repl(m: re.Match) -> str:
            marker = MARKER[cat]
            if marker in m.group(0):
                return m.group(0)
            counts[cat.lower()] += 1
            return marker
        text = pattern.sub(repl, text)

    # Sensitive headers and key labels preserve their labels/keys.
    def repl_auth(m: re.Match) -> str:
        if MARKER["ACCESS_TOKEN"] in m.group(0) or MARKER["AUTHORIZATION"] in m.group(0):
            return m.group(0)
        counts["authorization_header"] += 1
        return f"{m.group(1)}{m.group(2) or ''}{MARKER['ACCESS_TOKEN']}"

    text = AUTH_HEADER.sub(repl_auth, text)

    def repl_cookie(m: re.Match) -> str:
        if MARKER["COOKIE"] in m.group(0):
            return m.group(0)
        counts["cookie_header"] += 1
        return f"{m.group(1)}{MARKER['COOKIE']}"

    text = COOKIE_HEADER.sub(repl_cookie, text)

    def repl_env(m: re.Match) -> str:
        if any(marker in m.group(0) for marker in MARKER.values()):
            return m.group(0)
        cat = sensitive_key(m.group(1))
        if not cat:
            return m.group(0)
        value = m.group(4) if m.group(4) is not None else (m.group(5) or "")
        if looks_like_code(value):
            return m.group(0)
        counts["env_assignment"] += 1
        quote = m.group(3) or ""
        return f"{m.group(1)}{m.group(2)}{quote}{MARKER[cat]}{quote}"

    text = ENV_ASSIGN.sub(repl_env, text)

    def repl_flag(m: re.Match) -> str:
        if any(marker in m.group(0) for marker in MARKER.values()):
            return m.group(0)
        counts["cli_flag"] += 1
        return f"{m.group(1)}{MARKER['SECRET']}"

    text = CLI_FLAG.sub(repl_flag, text)

    def repl_query(m: re.Match) -> str:
        if any(marker in m.group(0) for marker in MARKER.values()):
            return m.group(0)
        counts["query_param"] += 1
        return f"{m.group(1)}{MARKER['SECRET']}"

    text = QUERY_PARAM.sub(repl_query, text)

    def repl_jsonish(m: re.Match) -> str:
        if any(marker in m.group(0) for marker in MARKER.values()):
            return m.group(0)
        counts["jsonish_secret"] += 1
        return f"{m.group(1)}{MARKER['SECRET']}{m.group(3)}"

    text = JSONISH_SECRET.sub(repl_jsonish, text)

    def repl_url(m: re.Match) -> str:
        if any(marker in m.group(0) for marker in MARKER.values()):
            return m.group(0)
        counts["url_password"] += 1
        return f"{m.group(1)}{MARKER['PASSWORD']}{m.group(3)}"

    text = URL_USERINFO.sub(repl_url, text)

    def repl_cn(m: re.Match) -> str:
        if any(marker in m.group(0) for marker in MARKER.values()):
            return m.group(0)
        counts["chinese_label"] += 1
        cat = "PRIVATE_KEY" if "私钥" in m.group(1) else "SECRET"
        return f"{m.group(1)}{m.group(2)}{MARKER[cat]}"

    text = CHINESE_LABEL.sub(repl_cn, text)

    def repl_bearer(m: re.Match) -> str:
        if MARKER["ACCESS_TOKEN"] in m.group(0):
            return m.group(0)
        counts["bearer_token"] += 1
        return f"{m.group(1)}{MARKER['ACCESS_TOKEN']}"

    text = BEARER_ANY.sub(repl_bearer, text)
    text = BASIC_ANY.sub(
        lambda m: m.group(0)
        if MARKER["AUTHORIZATION"] in m.group(0)
        else (counts.update(["basic_auth"]) or f"{m.group(1)}{MARKER['AUTHORIZATION']}"),
        text,
    )

    simple(OPENAI_KEY, "API_KEY")
    simple(GITHUB_TOKEN, "ACCESS_TOKEN")
    simple(GITLAB_TOKEN, "ACCESS_TOKEN")
    simple(SLACK_TOKEN, "ACCESS_TOKEN")
    simple(GOOGLE_KEY, "API_KEY")
    simple(AWS_KEY, "ACCESS_TOKEN")
    simple(HF_TOKEN, "ACCESS_TOKEN")
    simple(NPM_TOKEN, "ACCESS_TOKEN")
    simple(PYPI_TOKEN, "ACCESS_TOKEN")
    simple(JWT, "JWT")
    simple(BIGMODEL_KEY, "API_KEY")
    return text


def redact_scalar(value: Any, key: str, exact: list[tuple[str, str]], counts: Counter) -> Any:
    cat = sensitive_key(key)
    if cat is None:
        return value
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return value
    sval = str(value)
    if len(sval) < 3:
        return value
    if any(marker in sval for marker in MARKER.values()):
        return value
    counts[f"field_{cat.lower()}"] += 1
    if sval.lower().startswith("bearer "):
        return "Bearer " + MARKER["ACCESS_TOKEN"]
    if sval.lower().startswith("basic "):
        return "Basic " + MARKER["AUTHORIZATION"]
    return MARKER[cat]


def walk(obj: Any, exact: list[tuple[str, str]], counts: Counter, key: str = "") -> Any:
    if isinstance(obj, dict):
        return {k: walk(v, exact, counts, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk(v, exact, counts, key) for v in obj]
    if isinstance(obj, str):
        if sensitive_key(key):
            return redact_scalar(obj, key, exact, counts)
        return redact_text(obj, exact, counts)
    return redact_scalar(obj, key, exact, counts)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if OUT.exists() and any(OUT.iterdir()):
        raise SystemExit(f"refusing to write into non-empty directory: {OUT}")
    OUT.mkdir(parents=True, exist_ok=True)
    for sub in ("shop", "篡改"):
        (OUT / sub).mkdir(parents=True, exist_ok=False)

    exact = collect_exact_secrets()
    counts: Counter[str] = Counter()
    files: list[dict[str, Any]] = []
    total_before = 0
    total_after = 0

    for path in sorted(RAW.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in (".jsonl", ".json"):
            raise SystemExit(f"unexpected file in raw export: {path}")
        rel = path.relative_to(RAW)
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        before = path.stat().st_size
        total_before += before
        file_counts: Counter[str] = Counter()
        if path.suffix == ".jsonl":
            out_lines: list[str] = []
            with path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    cleaned = walk(obj, exact, file_counts)
                    out_lines.append(
                        json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
            dst.write_text("".join(out_lines), encoding="utf-8", newline="\n")
        else:
            obj = json.loads(path.read_text(encoding="utf-8"))
            cleaned = walk(obj, exact, file_counts)
            dst.write_text(
                json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        after = dst.stat().st_size
        total_after += after
        counts.update(file_counts)
        files.append(
            {
                "path": rel.as_posix(),
                "bytes_before": before,
                "bytes_after": after,
                "sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
                "substitutions": sum(file_counts.values()),
                "rules": dict(sorted(file_counts.items())),
            }
        )
        print(f"redacted {rel.as_posix()}: {before} -> {after} bytes, {sum(file_counts.values())} events")

    report = {
        "schema": "zcode.redaction.v1",
        "method": (
            "Parsed every exported JSON/JSONL record, replaced exact locally stored credential "
            "values and credential-like substrings with explicit [[REDACTED:*]] markers. "
            "Non-secret text, IDs, timestamps, and ordering were left unchanged."
        ),
        "rules": {
            "exact_local_credentials": "Values read in memory from local ZCode credentials and sensitive environment variables.",
            "private_keys": "PEM/OpenSSH private key bodies replaced, BEGIN/END markers retained.",
            "api_keys": "Provider-prefixed keys (sk-, AIza, AKIA/ASIA, hf_, npm_, pypi-, BigModel format).",
            "tokens": "OAuth/JWT/GitHub/GitLab/Slack tokens and bearer/basic authorization values.",
            "passwords": "Password/passwd/pwd fields, URL userinfo, CLI flags, query parameters, and labeled values.",
            "cookies": "Cookie and Set-Cookie header values.",
            "secret_fields": "Sensitive JSON field values identified by key name.",
        },
        "totals": {
            "substitution_events": sum(counts.values()),
            "bytes_before": total_before,
            "bytes_after": total_after,
        },
        "by_rule": dict(sorted(counts.items())),
        "files": files,
        "notice": "No credential values are included in this report.",
    }
    (OUT / "redaction-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"TOTAL redaction events: {sum(counts.values())}; {total_before} -> {total_after} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
