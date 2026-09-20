"""Shared argv safety checks for external model and publishing commands."""

from __future__ import annotations

from pathlib import Path
import re
import shlex
from typing import Optional, Sequence


SHELL_EXECUTABLES = {
    "ash",
    "bash",
    "cmd",
    "cmd.exe",
    "csh",
    "dash",
    "fish",
    "ksh",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "tcsh",
    "zsh",
}
ENV_OPTIONS_WITH_VALUE = {
    "-C",
    "--chdir",
    "-u",
    "--unset",
}
ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def explicit_shell_executable(command: Sequence[str]) -> Optional[str]:
    """Return an explicitly selected shell executable, including common env wrappers."""

    selected = _unwrap_env(command)
    if not selected:
        return None
    executable = Path(selected[0]).name.lower()
    if executable in SHELL_EXECUTABLES:
        return executable
    if executable == "busybox" and len(selected) > 1:
        applet = Path(selected[1]).name.lower()
        if applet in SHELL_EXECUTABLES:
            return f"busybox {applet}"
    return None


def _unwrap_env(command: Sequence[str]) -> list[str]:
    tokens = [str(part) for part in command]
    if not tokens or Path(tokens[0]).name.lower() != "env":
        return tokens
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if token in {"-S", "--split-string"}:
            if index + 1 >= len(tokens):
                return []
            return shlex.split(tokens[index + 1]) + tokens[index + 2 :]
        if token in ENV_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-") or ENV_ASSIGNMENT_RE.fullmatch(token):
            index += 1
            continue
        return tokens[index:]
    return []
