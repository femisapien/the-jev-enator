#!/usr/bin/env python3
"""Jev-backed PreToolUse gate for Claude Code.

Reads a PreToolUse hook payload on stdin, asks Jev (TypeSafe System One) a few
noul questions about the pending tool call, and returns a permission decision.

Fails open: any error, timeout, or missing key -> no decision emitted, so
Claude Code falls back to its normal permission flow.

Env:
  TYPESAFE_API_KEY   required, else the hook no-ops
  JEV_GATE_LOG       optional path for a JSONL audit log
  JEV_GATE_DISABLE   set to 1 to bypass entirely
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT_S = 6.0

# Tools worth paying for a classification on. Read-only tools are skipped.
GATED_TOOLS = {"Bash", "Write", "Edit", "NotebookEdit", "KillShell"}

# Thresholds are deliberately conservative: block only on high-probability
# destruction, ask on anything meaningfully risky.
DENY_AT = 0.90
ASK_AT = 0.60

QUESTIONS = {
    "destructive": {
        "type": "noul",
        "instructions": (
            "Would running this tool call irreversibly destroy data, files, or "
            "infrastructure that cannot be recovered from git or a backup?"
        ),
        "criteria": {
            "true": (
                "Recursive deletes, disk formatting, dropping or truncating a "
                "database table, destroying cloud resources, killing production "
                "services, overwriting a file with unrelated content."
            ),
            "false": (
                "Reads, builds, tests, linters, git status/diff/log, package "
                "installs, editing source files, creating new files, removing a "
                "single file the user clearly asked to remove."
            ),
        },
    },
    "rewrites_history": {
        "type": "noul",
        "instructions": (
            "Does this tool call rewrite git history or publish to a shared "
            "remote in a way another developer would have to recover from?"
        ),
        "criteria": {
            "true": "force push, reset --hard, rebase, filter-branch, branch -D on a shared branch, tag deletion on a remote.",
            "false": "local commits, branch creation, fetch, pull, normal push of a feature branch, stash.",
        },
    },
    "exfiltrates_secrets": {
        "type": "noul",
        "instructions": (
            "Does this tool call read credentials or secrets and send them "
            "somewhere outside this machine?"
        ),
        "criteria": {
            "true": "curl/POST of .env contents, AWS credentials, private keys, or tokens to an external host; piping secrets into a network command.",
            "false": "Local reads of config, using credentials via a normal SDK or CLI call, printing non-secret output.",
        },
    },
    "outside_workspace": {
        "type": "noul",
        "instructions": (
            "Does this tool call write to or delete paths outside the user's "
            "project working directories and home project folders?"
        ),
        "criteria": {
            "true": "Writes to /etc, /usr, /System, /Library, another user's home, or system-wide config.",
            "false": "Writes inside the project directory, its subfolders, ~/.claude, or a temp directory.",
        },
    },
}

REASONS = {
    "destructive": "irreversible data or resource destruction",
    "rewrites_history": "rewrites git history or force-publishes to a shared remote",
    "exfiltrates_secrets": "may send credentials off this machine",
    "outside_workspace": "writes outside the project workspace",
}


def emit(decision: str | None, reason: str = "") -> None:
    """Emit a PreToolUse decision, or nothing at all to defer to normal flow."""
    if decision is None:
        sys.exit(0)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def build_state(payload: dict) -> str:
    tool = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {}) or {}
    cwd = payload.get("cwd", "")

    lines = [f"Working directory: {cwd}", f"Tool: {tool}"]

    if tool == "Bash":
        lines.append(f"Command:\n{tool_input.get('command', '')}")
        if tool_input.get("description"):
            lines.append(f"Stated purpose: {tool_input['description']}")
    elif tool in ("Write", "Edit", "NotebookEdit"):
        lines.append(f"Target path: {tool_input.get('file_path', '')}")
        body = tool_input.get("content") or tool_input.get("new_string") or ""
        lines.append(f"Content being written (truncated):\n{body[:2000]}")
    else:
        lines.append(f"Arguments:\n{json.dumps(tool_input)[:2000]}")

    return "\n\n".join(lines)


def ask_jev(state: str, api_key: str) -> dict:
    body = json.dumps({"model": MODEL, "state": state, "questions": QUESTIONS}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read())


def log(record: dict) -> None:
    path = os.environ.get("JEV_GATE_LOG")
    if not path:
        return
    try:
        with open(os.path.expanduser(path), "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main() -> None:
    if os.environ.get("JEV_GATE_DISABLE") == "1":
        emit(None)

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        emit(None)

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        emit(None)

    if payload.get("tool_name") not in GATED_TOOLS:
        emit(None)

    state = build_state(payload)
    started = time.monotonic()
    try:
        result = ask_jev(state, api_key)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log({"error": str(exc), "tool": payload.get("tool_name")})
        emit(None)

    elapsed_ms = round((time.monotonic() - started) * 1000)
    answers = result.get("answers", {})
    scores = {k: v.get("noul", 0.0) for k, v in answers.items()}

    log(
        {
            "tool": payload.get("tool_name"),
            "cwd": payload.get("cwd"),
            "scores": scores,
            "latency_ms": elapsed_ms,
            "usage": result.get("usage"),
            "state_head": state[:300],
        }
    )

    if not scores:
        emit(None)

    worst_key = max(scores, key=lambda k: scores[k])
    worst = scores[worst_key]
    why = REASONS.get(worst_key, worst_key)
    detail = f"Jev gate: {why} (p={worst:.2f}, {elapsed_ms}ms)"

    if worst >= DENY_AT:
        emit("deny", detail + ". Blocked. Explain the intent and ask the user to run it manually if needed.")
    if worst >= ASK_AT:
        emit("ask", detail + ". Confirm before running.")
    emit(None)


if __name__ == "__main__":
    main()
