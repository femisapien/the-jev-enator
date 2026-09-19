#!/usr/bin/env python3
"""Jev-backed PreToolUse gate for Claude Code.

Reads a PreToolUse hook payload on stdin, asks Jev a handful of noul questions
about the pending tool call, and returns allow / ask / deny.

Fails open: any error, timeout, or missing key emits no decision, so Claude Code
falls back to its normal permission flow.

Env:
  TYPESAFE_API_KEY   required, else the hook no-ops
  JEV_GATE_LOG       optional path for a JSONL audit log
  JEV_GATE_DISABLE   set to 1 to bypass entirely
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_client import JevError, api_key, ask_jev, disabled, log, read_payload

# Tools worth paying for a classification on. Read-only tools are skipped before
# any network call.
GATED_TOOLS = {"Bash", "Write", "Edit", "NotebookEdit", "KillShell"}

# Per-question thresholds as (deny_at, ask_at). Tuned against tests/.
#
# A deny_at of 1.01 is unreachable, meaning that question can only ever ask --
# right for operations that are risky but legitimate and routinely intended.
# Hard-denying those would train you to disable the gate.
THRESHOLDS = {
    "destructive": (0.80, 0.50),
    "rewrites_history": (0.90, 0.55),
    "discards_local_work": (1.01, 0.55),
    "exfiltrates_secrets": (0.80, 0.40),
    "outside_workspace": (0.85, 0.55),
}
DEFAULT_THRESHOLD = (0.90, 0.60)

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
    "discards_local_work": {
        "type": "noul",
        "instructions": (
            "Would this tool call throw away uncommitted work in the working "
            "tree that the user has no other copy of?"
        ),
        "criteria": {
            "true": "git reset --hard, git checkout -- ., git clean -fd, git stash drop, discarding a branch with unpushed commits.",
            "false": "Commits, adds, normal checkout of a clean tree, stash push, reads, builds, tests.",
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
    "discards_local_work": "discards uncommitted work with no other copy",
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


def main() -> None:
    if disabled():
        emit(None)

    key = api_key()
    if not key:
        emit(None)

    payload = read_payload()
    if payload is None or payload.get("tool_name") not in GATED_TOOLS:
        emit(None)

    state = build_state(payload)
    try:
        scores, elapsed_ms, usage = ask_jev(state, QUESTIONS, key)
    except JevError as exc:
        log({"hook": "gate", "error": str(exc), "tool": payload.get("tool_name")})
        emit(None)

    log(
        {
            "hook": "gate",
            "tool": payload.get("tool_name"),
            "cwd": payload.get("cwd"),
            "scores": scores,
            "latency_ms": elapsed_ms,
            "usage": usage,
            "state_head": state[:300],
        }
    )

    # Evaluate each question against its own thresholds, then take the most
    # severe outcome. A single question crossing its deny bar outranks any
    # number of questions that merely want to ask.
    denies, asks = [], []
    for name, prob in scores.items():
        deny_at, ask_at = THRESHOLDS.get(name, DEFAULT_THRESHOLD)
        label = f"{REASONS.get(name, name)} (p={prob:.2f})"
        if prob >= deny_at:
            denies.append((prob, label))
        elif prob >= ask_at:
            asks.append((prob, label))

    if denies:
        why = "; ".join(label for _, label in sorted(denies, reverse=True))
        emit(
            "deny",
            f"Jev gate blocked this ({elapsed_ms}ms): {why}. "
            "Do not retry. Explain the intent and let the user run it manually.",
        )
    if asks:
        why = "; ".join(label for _, label in sorted(asks, reverse=True))
        emit("ask", f"Jev gate flagged this ({elapsed_ms}ms): {why}. Confirm before running.")
    emit(None)


if __name__ == "__main__":
    main()
