# jev-gate

A `PreToolUse` hook for Claude Code that classifies risky tool calls with
[Jev](https://docs.typesafe.ai) (TypeSafe System One) before they execute.

Claude Code already gates dangerous actions, but that classifier is closed. This
puts an explicit, tunable one in front of `Bash`, `Write`, `Edit`, and
`NotebookEdit` — one Jev request, four parallel noul questions, ~1 cent per
thousand calls.

## Setup

```bash
cd ~/jev-gate
cp .env.example .env    # add your TYPESAFE_API_KEY
source .env
python3 tests/test_jev_gate.py
```

The test fires 9 fixture payloads (3 safe, 6 risky) and prints the decision and
probability for each. Tune thresholds against that output before wiring it live.

## Wiring it in

Append to the `PreToolUse` array in `~/.claude/settings.json` — alongside any
existing hooks, not replacing them:

```json
{
  "matcher": "Bash|Write|Edit|NotebookEdit",
  "hooks": [
    { "type": "command", "command": "/Users/Jake.Reardon@MLSE.com/jev-gate/src/jev_gate.py" }
  ]
}
```

`TYPESAFE_API_KEY` must be visible to the hook process. Either add it to the
`env` block in `settings.json` or export it from your shell profile.

## How it decides

| Max probability across the 4 questions | Decision |
| --- | --- |
| >= 0.90 | `deny` — blocked, Claude is told to explain instead of run |
| >= 0.60 | `ask` — surfaces the normal permission prompt |
| < 0.60 | no output — normal Claude Code flow, no interruption |

The four questions: irreversible destruction, git history rewrite / shared-remote
push, secret exfiltration, writes outside the workspace. Jev evaluates all four
in one round trip.

Thresholds live in `src/jev_gate.py` as `DENY_AT` and `ASK_AT`.

## Safety properties

- **Fails open.** Missing key, network error, timeout (6s), or malformed response
  emits no decision, so Claude Code behaves exactly as it does without the hook.
- **Read-only tools are skipped** before any network call, so no cost on Read,
  Grep, Glob, or WebFetch.
- **`JEV_GATE_DISABLE=1`** bypasses everything.
- **`JEV_GATE_LOG`** writes a JSONL audit line per classification with scores,
  latency, and token usage — useful for tuning thresholds from real traffic.

## Layout

```
src/jev_gate.py          the hook
tests/test_jev_gate.py   9 fixture payloads
.env.example             config template
```
