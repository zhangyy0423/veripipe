#!/usr/bin/env python3
"""扫描 tracked 文件中的高置信 secret，输出位置但不回显 secret 值。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Pattern, Tuple


ROOT = Path(__file__).resolve().parents[2]
MAX_TEXT_BYTES = 2 * 1024 * 1024
FORBIDDEN_ENV_NAMES = {".env", ".env.local", ".env.production", ".env.development"}
SECRET_PATTERNS: List[Tuple[str, Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{30,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("credential-url", re.compile(r"https?://[^\s/:@]+:[^\s/@]+@[^\s]+")),
]


def repository_paths() -> Iterable[Path]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    )
    for raw in result.stdout.split(b"\0"):
        if raw:
            yield Path(raw.decode("utf-8"))


def forbidden_env_path(path: Path) -> bool:
    name = path.name.lower()
    return name in FORBIDDEN_ENV_NAMES or name.endswith(".local.env") or (
        path.parts and path.parts[0] == "configs" and name.endswith(".env")
    )


def scan_file(path: Path) -> List[Tuple[int, str]]:
    absolute = ROOT / path
    if not absolute.is_file() or absolute.stat().st_size > MAX_TEXT_BYTES:
        return []
    try:
        text = absolute.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    findings: List[Tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for label, pattern in SECRET_PATTERNS:
            if label == "credential-url" and "://user:pass@" in line:
                continue
            if pattern.search(line):
                findings.append((line_number, label))
    return findings


def main() -> int:
    failures: List[str] = []
    for path in repository_paths():
        if forbidden_env_path(path):
            failures.append(f"{path}: forbidden tracked env file")
        for line_number, label in scan_file(path):
            failures.append(f"{path}:{line_number}: possible {label}")

    if failures:
        print("secret scan FAIL", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("secret scan OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
