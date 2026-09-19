#!/usr/bin/env python3
"""Jev-backed PostToolUse notice for Claude Code.

Reads the output of a Bash call and, if it contains a failure, says so in the
agent's context before the agent gets to interpret it.

This is the one hook in this repo aimed at making the agent better rather than
stopping it doing damage. The failure mode it targets is real and common: a test
runner exits 0 while printing failures, `| tail -3` truncates the failure off
the screen, a build prints one error under two hundred warnings. The agent skims
the visible part, sees nothing red, and reports success.

It does not block. It injects a plain sentence, which is the whole point -- the
correction lands while there is still time to act on it, instead of costing the
user a turn afterwards.

Fails open: any error, timeout, or missing key emits nothing.

Env:
  TYPESAFE_API_KEY   required, else the hook no-ops
  JEV_GATE_LOG       optional path for a JSONL audit log
  JEV_GATE_DISABLE   set to 1 to bypass entirely
  JEV_NOTICE_OFF     set to 1 to disable just this hook
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_client import JevError, api_key, ask_jev, disabled, log, read_payload

# Below this, there is nothing to misread. Saves a call on the many Bash results
# that are a single line or empty.
MIN_OUTPUT_CHARS = 40

# Jev is priced per input token, and command output is the largest state in this
# repo. Keep the head and the tail: the head holds compile errors, the tail holds
# test summaries, and the middle is usually a file list.
MAX_HEAD = 4000
MAX_TAIL = 4000

# Say something at all.
NOTICE_AT = 0.85
# Say it emphatically: the output is shaped so that skimming it would give the
# wrong impression, which is exactly when a reminder changes the outcome.
EMPHATIC_AT = 0.45

QUESTIONS = {
    "output_shows_failure": {
        "type": "noul",
        "instructions": (
            "Does this command output report a failure, error, or unmet "
            "expectation that the developer needs to act on?"
        ),
        "criteria": {
            "true": (
                "A failing test or assertion; a compile or type error; a stack "
                "trace or unhandled exception; a non-zero failure count; a "
                "linter error as opposed to a warning; 'command not found'; a "
                "connection refused or permission denied; output that is "
                "truncated in a way that shows a failure count without showing "
                "which tests failed."
            ),
            "false": (
                "All tests passing; a clean build; a successful install; "
                "deprecation warnings or peer-dependency notices with no error; "
                "informational log lines; a linter reporting only warnings; "
                "normal output of a read-only command such as git status, ls, "
                "or grep finding no matches."
            ),
        },
    },
    "exit_status_misleads": {
        "type": "noul",
        "instructions": (
            "Would someone skimming this output conclude the command succeeded "
            "when it actually did not?"
        ),
        "criteria": {
            "true": (
                "The command exited 0 but the output text contains failures; a "
                "failure appears early and is followed by many lines of normal "
                "output; the output is piped through tail or head so the "
                "failure detail is cut off and only a summary count remains; a "
                "test runner reports 'Tests: 1 failed, 3 passed' with no other "
                "visible sign of a problem."
            ),
            "false": (
                "The failure is stated plainly and prominently, so it cannot be "
                "missed; or there is no failure at all and the output is "
                "genuinely a success."
            ),
        },
    },
}


def emit(context: str | None) -> None:
    """Inject additional context, or nothing at all."""
    if context is None:
        sys.exit(0)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )
    )
    sys.exit(0)


def tool_output(payload: dict) -> str:
    """Pull the Bash result text out of a PostToolUse payload.

    tool_response is a dict for Bash (stdout/stderr/interrupted) but hook
    payloads vary by tool and by Claude Code version, so fall back to a string.
    """
    resp = payload.get("tool_response")
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = [str(resp.get(k, "")) for k in ("stdout", "stderr", "output", "content")]
        joined = "\n".join(p for p in parts if p.strip())
        return joined or json.dumps(resp)[:MAX_HEAD]
    return ""


def truncate(text: str) -> str:
    if len(text) <= MAX_HEAD + MAX_TAIL:
        return text
    cut = len(text) - MAX_HEAD - MAX_TAIL
    return f"{text[:MAX_HEAD]}\n\n[... {cut} characters omitted ...]\n\n{text[-MAX_TAIL:]}"


def build_state(payload: dict, output: str) -> str:
    tool_input = payload.get("tool_input", {}) or {}
    command = tool_input.get("command", "")
    resp = payload.get("tool_response")
    code = resp.get("exit_code", "unknown") if isinstance(resp, dict) else "unknown"
    return "\n\n".join(
        [
            f"Command that was run:\n{command}",
            f"Exit code: {code}",
            f"Output:\n{truncate(output)}",
        ]
    )


def main() -> None:
    if disabled() or os.environ.get("JEV_NOTICE_OFF") == "1":
        emit(None)

    key = api_key()
    if not key:
        emit(None)

    payload = read_payload()
    if payload is None or payload.get("tool_name") != "Bash":
        emit(None)

    output = tool_output(payload)
    if len(output.strip()) < MIN_OUTPUT_CHARS:
        emit(None)

    state = build_state(payload, output)
    try:
        scores, elapsed_ms, usage = ask_jev(state, QUESTIONS, key)
    except JevError as exc:
        log({"hook": "notice", "error": str(exc)})
        emit(None)

    fail = scores.get("output_shows_failure", 0.0)
    misleads = scores.get("exit_status_misleads", 0.0)
    noticed = fail >= NOTICE_AT

    log(
        {
            "hook": "notice",
            "scores": scores,
            "latency_ms": elapsed_ms,
            "usage": usage,
            "noticed": noticed,
            "emphatic": noticed and misleads >= EMPHATIC_AT,
            "command": (payload.get("tool_input", {}) or {}).get("command", "")[:200],
        }
    )

    if not noticed:
        emit(None)

    if misleads >= EMPHATIC_AT:
        emit(
            f"[jev-notice, {elapsed_ms}ms] This output contains a failure that is "
            f"easy to miss on a skim (p={fail:.2f} failure, p={misleads:.2f} "
            "misleading). The command may have exited 0, or the failure may be "
            "truncated or buried. Read the output again before describing this as "
            "working, and do not report success unless you can point to the line "
            "that shows it."
        )
    emit(
        f"[jev-notice, {elapsed_ms}ms] This output reports a failure "
        f"(p={fail:.2f}). Address it or say so plainly; do not describe this step "
        "as successful."
    )


if __name__ == "__main__":
    main()
