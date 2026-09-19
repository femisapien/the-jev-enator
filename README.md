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

If every case comes back `allow (no decision)`, the gate is failing open on an
error. Set `JEV_GATE_LOG` and check the log — it records the cause per call.

## TLS on macOS

python.org interpreters ship without a CA bundle wired into `urllib`, which
surfaces as `CERTIFICATE_VERIFY_FAILED`. The gate handles this itself: it looks
for `$SSL_CERT_FILE`, then `/etc/ssl/cert.pem`, then the Homebrew bundle, then
`certifi`. No shell setup needed.

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

`TYPESAFE_API_KEY` must be visible to the hook process; the `env` block in
`settings.json` is the reliable place, since the hook does not inherit your
shell profile. Note that puts the key in plaintext in that file.

Restart Claude Code after editing `settings.json`. A backup of the pre-install
settings is at `~/.claude/settings.json.bak-jevgate`.

## How it decides

Five noul questions, evaluated in parallel in one round trip. Each has its own
deny/ask thresholds, and the most severe outcome across all five wins.

| Question | deny at | ask at |
| --- | --- | --- |
| `exfiltrates_secrets` — credentials leaving the machine | 0.80 | 0.40 |
| `destructive` — irreversible data or resource loss | 0.80 | 0.50 |
| `outside_workspace` — writes to system paths | 0.85 | 0.55 |
| `rewrites_history` — force push, shared-remote rewrite | 0.90 | 0.55 |
| `discards_local_work` — uncommitted work with no other copy | never | 0.55 |

`discards_local_work` can only ever ask. `git reset --hard` is destructive but
routinely intended; hard-denying it would just train you to disable the gate.

Thresholds live in `THRESHOLDS` in `src/jev_gate.py`. Measured behaviour on the
fixture set: 9/9 correct, ~330ms per call, ~760 tokens.

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
