# jev-gate

A `PreToolUse` hook for Claude Code that classifies risky tool calls with
[Jev](https://docs.typesafe.ai) before they run, and blocks the dangerous ones.

Claude Code already gates dangerous actions, but that classifier lives in the
closed part of the harness. This puts an explicit, tunable, auditable one in
front of `Bash`, `Write`, `Edit`, and `NotebookEdit` — five questions in one
~350ms request, at $0.042 per million input tokens.

Measured on this machine: **9/9 fixture cases classified correctly, median
359ms, about 780 input tokens per call (~$0.000033).**

---

## 1. Install

```bash
git clone <repo-url> ~/jev-gate
cd ~/jev-gate
cp .env.example .env          # paste your TYPESAFE_API_KEY
./install.sh
```

Then **restart Claude Code** — `settings.json` is only read at startup.

`install.sh` appends to the `PreToolUse` array without touching existing hooks,
backs up `settings.json` to `settings.json.bak-jevgate`, and is safe to run
twice. Get a key at [typesafe.ai](https://typesafe.ai).

To remove it:

```bash
./install.sh --uninstall
```

## 2. Using it

There is nothing to run. Once installed you use Claude Code exactly as before,
and the gate sits in the path of every write-capable tool call:

| What happens | Example |
| --- | --- |
| Runs normally, no prompt, you never notice | `npm test`, `git status`, editing a `.ts` file |
| Claude Code asks you to confirm | `git reset --hard` (p=0.93 discards local work) |
| Blocked, and Claude is told to explain instead | `git push --force`, `rm -rf ~/repo`, `DROP TABLE`, POSTing `.env` to a host, writing `/etc/hosts` |

Read-only tools (`Read`, `Grep`, `Glob`, `WebFetch`) are skipped before any
network call, so they cost nothing and add no latency.

The ~350ms only applies to tool calls that could modify something, and it runs
while Claude Code is already setting up the call.

### Turning it off

Pick whichever fits: temporary or permanent.

```bash
export JEV_GATE_DISABLE=1     # off for one shell session
./install.sh --uninstall      # off for good
```

Or set `"JEV_GATE_DISABLE": "1"` in the `env` block of `settings.json`.

## 3. Knowing it's on

```bash
./verify.sh
```

Four checks plus live usage stats:

```
  OK    registered in settings.json as a PreToolUse hook
  OK    hook script is executable
  OK    TYPESAFE_API_KEY present in settings.json env
  OK    live API call blocked a destructive command

  36 classifications logged, 0 errors
  median 359ms, 29453 input tokens total, ~$0.0012 spent

  Gate is on and working.
```

The fourth check matters most: it pipes a real `rm -rf /` payload through the
real hook and fails if it isn't denied. Registration alone doesn't prove the
gate works, because **the gate fails open** — if the API is down, the key is
wrong, or TLS breaks, it emits no decision and Claude Code behaves exactly as if
the hook weren't there. That's deliberate (a gate that blocks your work when a
third-party API hiccups gets uninstalled within a day) but it means silent
failure is possible. `verify.sh` is how you rule it out.

For ongoing visibility, watch the audit log in a second pane:

```bash
tail -f ~/jev-gate.jsonl | jq -c '{tool, scores, latency_ms}'
```

Every classification writes one JSONL line with all five probabilities, latency,
and token usage. This is also your threshold-tuning data — after a week of real
traffic you'll see which questions are too jumpy.

## 4. Sharing with the team

Yes — that's the intended deployment. Three considerations:

**Never commit `.env`.** It's gitignored. `install.sh` copies the key into
`~/.claude/settings.json`, which is per-user and outside the repo.

**The key ends up in plaintext** in each person's `settings.json`. The hook
process doesn't inherit a shell profile, so that's the reliable place. For a
team, prefer a shared org key you can rotate over personal keys, and treat
`settings.json` as a secret-bearing file.

**Tune thresholds centrally.** `THRESHOLDS` in `src/jev_gate.py` is the policy.
Changing it in the repo and having people pull is the whole update mechanism —
no redeploy, no restart beyond Claude Code itself.

A reasonable rollout: install it yourself for a week, review your own audit log,
adjust thresholds to your team's actual workflow, then share. Shipping untuned
thresholds is how you get people running `--uninstall` on day two.

### Does this save MLSE money?

**On tool calls, no — it adds a small cost.** Be clear about this if you pitch
it. Each gated call is an extra ~780 input tokens at $0.042/Mtok:

| Volume | Added cost |
| --- | --- |
| 1,000 gated calls | $0.03 |
| 10 devs × 500 calls/day × 20 days | ~$3.30/month |

Effectively free, but it is a cost, not a saving.

**Where it does save money is the incident.** One force-push over a colleague's
work, one `DROP TABLE` against a live database, one `.env` posted to an external
host — each costs hours of engineering time and, for the last one, a credential
rotation and possibly a security review. At $3/month for ten developers, the
gate pays for itself if it prevents roughly one such event per decade.

The honest framing for a manager: **this is cheap insurance and an audit trail,
not a cost reduction.** The audit log is its own argument — it's a per-call
record of what agents were about to do across the team, which you currently
don't have.

**Where Jev genuinely does cut spend** is model routing: classifying a task and
sending easy ones to Haiku instead of Opus. That's a real reduction, but it
can't be done from a hook — hooks can't change Claude Code's model mid-session.
It needs a standalone agent. Worth a separate project.

---

## How it decides

Five noul (yes/no) questions, evaluated in parallel in a single request. Each
has its own deny/ask thresholds; the most severe outcome across all five wins.

| Question | deny at | ask at |
| --- | --- | --- |
| `exfiltrates_secrets` — credentials leaving the machine | 0.80 | 0.40 |
| `destructive` — irreversible data or resource loss | 0.80 | 0.50 |
| `outside_workspace` — writes to system paths | 0.85 | 0.55 |
| `rewrites_history` — force push, shared-remote rewrite | 0.90 | 0.55 |
| `discards_local_work` — uncommitted work with no other copy | never | 0.55 |

`discards_local_work` can only ever ask, never deny. `git reset --hard` is
destructive but routinely intended; hard-denying it would train people to
disable the gate, which costs more safety than it buys.

Each question carries explicit `criteria` for what true and false look like.
Those examples are doing most of the work — vague instructions produce
probabilities near 0.5, which are useless for thresholds.

### Tuning

Edit `THRESHOLDS` or the `QUESTIONS` criteria in `src/jev_gate.py`, then:

```bash
source .env && python3 tests/test_jev_gate.py
```

Nine fixture payloads, three safe and six dangerous, with the decision and
probability for each. Changing the gate without running this is how false
positives reach the team.

To add a question, add it to `QUESTIONS`, a human-readable phrase to `REASONS`,
and a `(deny_at, ask_at)` pair to `THRESHOLDS`. Extra questions cost only their
own tokens — Jev evaluates them all in parallel, so latency barely moves.

## Troubleshooting

**Every case returns `allow`.** The gate is failing open. Check the audit log:
`tail -3 ~/jev-gate.jsonl`. The most likely cause on macOS is
`CERTIFICATE_VERIFY_FAILED` — python.org builds ship without a CA bundle wired
into `urllib`. The gate resolves this itself by trying `$SSL_CERT_FILE`, then
`/etc/ssl/cert.pem`, then the Homebrew bundle, then `certifi`, so if you're
seeing it, none of those exist on that machine.

**`HTTP 401`** in the log means a bad or expired key. **`HTTP 422`** means a
malformed question definition — check a recent edit to `QUESTIONS`.

**Nothing in the log at all.** `JEV_GATE_LOG` isn't set in the `env` block of
`settings.json`, or Claude Code hasn't been restarted since install.

## Layout

```
src/jev_gate.py          the hook
tests/test_jev_gate.py   9 fixture payloads, 3 safe and 6 dangerous
install.sh               wire into / out of settings.json
verify.sh                prove the gate is on and working
.env.example             config template
```

Standard library only, no dependencies.
