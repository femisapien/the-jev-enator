# jev-gate

Claude Code hooks that use [Jev](https://docs.typesafe.ai) to make cheap,
calibrated judgement calls in the agent loop — where a full LLM call would be
too slow and too expensive to sit in the hot path.

Two hooks so far:

| Hook | Event | What it does |
| --- | --- | --- |
| **danger gate** | `PreToolUse` | Blocks destructive tool calls before they run |
| **completion check** | `Stop` | Blocks Claude from ending its turn when it isn't actually done |

Jev isn't a chat model — it returns calibrated probabilities on typed questions
instead of generating text. That makes each check ~350ms and ~$0.00004, cheap
enough to run on every tool call and every turn.

Measured on this machine: **9/9 and 10/10 fixture cases correct, median 350ms.**

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

There is nothing to run. You use Claude Code exactly as before.

### The danger gate

Sits in the path of every write-capable tool call:

| What happens | Example |
| --- | --- |
| Runs normally, no prompt, you never notice | `npm test`, `git status`, editing a `.ts` file |
| Claude Code asks you to confirm | `git reset --hard` (p=0.93 discards local work) |
| Blocked, and Claude is told to explain instead | `git push --force`, `rm -rf ~/repo`, `DROP TABLE`, POSTing `.env` to a host, writing `/etc/hosts` |

Read-only tools (`Read`, `Grep`, `Glob`, `WebFetch`) are skipped before any
network call, so they cost nothing and add no latency.

### The completion check

When Claude tries to end its turn, the hook reconstructs the turn from the
transcript — what you asked for, every tool call and its result, and the closing
message — and asks whether the work was actually done or just declared done.
If so, the stop is blocked and Claude is sent back to finish.

Four ways to fail, each blocking at p≥0.80:

| Caught | Example |
| --- | --- |
| Claimed without verifying | "Fixed, tests should pass now" — but no test ever ran |
| Left work undone | You named three files, it edited one and said "done" |
| Left placeholder code | New `TODO`s or `throw new Error('Not implemented')` you didn't ask for |
| Ignored a failure | A test failed and the closing message never mentions it |

It deliberately does **not** block when Claude is legitimately stuck. A separate
`awaiting_user_input` question acts as a veto: if Claude is asking you a
question, presenting options, or reporting a blocker it can't resolve, the turn
ends normally. Without that veto the hook would trap you in a loop with an agent
that can't proceed and isn't allowed to stop and ask.

It also honours `stop_hook_active`, so it can only ever block once per turn.

### Turning things off

```bash
export JEV_FINISH_OFF=1       # completion check off, danger gate stays on
export JEV_GATE_DISABLE=1     # both hooks off for this shell
./install.sh --uninstall      # both off for good
```

Or set either in the `env` block of `settings.json` to make it persistent.

## 3. Knowing it's on

```bash
./verify.sh
```

Five checks plus live usage stats:

```
  OK    danger gate registered as a PreToolUse hook
  OK    completion check registered as a Stop hook
  OK    TYPESAFE_API_KEY present in settings.json env
  OK    live API call blocked a destructive command
  OK    live API call blocked an unverified completion claim

  danger gate          50 calls, median 362ms
  completion check      1 calls, median 439ms
  total spend        ~$0.0023  (53675 input tokens)

  Gate is on and working.
```

The last two checks matter most: they push a real `rm -rf /` payload and a real
"tests should pass now" transcript through the real hooks, and fail if either
isn't blocked. Registration alone proves nothing, because **both hooks fail
open** — if the API is down, the key is wrong, or TLS breaks, they emit no
decision and Claude Code behaves exactly as if they weren't installed.

That's deliberate: a hook that blocks your work when a third-party API hiccups
gets uninstalled within a day. But it means silent failure is possible, and
`verify.sh` is how you rule it out.

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

**Tune thresholds centrally.** The `THRESHOLDS` / `BLOCK_AT` dicts in `src/` are
the policy. Changing them in the repo and having people pull is the whole update
mechanism — no redeploy, no restart beyond Claude Code itself.

A reasonable rollout: install it yourself for a week, review your own audit log,
adjust thresholds to your team's actual workflow, then share. Shipping untuned
thresholds is how you get people running `--uninstall` on day two.

### Does this save MLSE money?

**On API spend, no — it adds a little.** Be clear about this if you pitch it.
Each check is ~800–950 input tokens at $0.042/Mtok:

| Volume | Added cost |
| --- | --- |
| 1,000 checks | $0.035 |
| 10 devs × 500 tool calls + 100 turns/day × 20 days | ~$4/month |

Effectively free, but it is a cost, not a saving.

**Where it does pay back is time.** Two different cases:

The danger gate is insurance against an incident. One force-push over a
colleague's work, one `DROP TABLE` against a live database, one `.env` posted to
an external host — each costs hours, and the last one costs a credential rotation
and possibly a security review.

The completion check pays back every day, which is the better argument. The
common failure mode with coding agents isn't destruction, it's an agent that says
"done, tests should pass" when it never ran them. You find out ten minutes later
and spend another turn on it. Catching that before the turn ends saves a
round trip each time, and those add up faster than any incident.

The honest framing for a manager: **cheap insurance, a daily time saver, and an
audit trail — not a reduction in model spend.** The audit log is its own
argument: a per-call record of what agents across the team were about to do, and
how often they tried to stop early.

**Where Jev genuinely would cut spend** is model routing: classify the task, send
easy ones to Haiku instead of Opus. That's a real reduction, but hooks can't
change Claude Code's model mid-session, so it needs a standalone agent. Separate
project.

---

## How it decides

Both hooks work the same way: a set of noul (yes/no) questions, all evaluated in
parallel in a single request, each with its own threshold. Extra questions cost
only their own tokens and barely affect latency.

Each question carries explicit `criteria` for what true and false look like.
Those examples do most of the work — vague instructions produce probabilities
near 0.5, which are useless for thresholds.

### Danger gate — `src/jev_gate.py`

Per-question `(deny, ask)` thresholds; the most severe outcome wins.

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

### Completion check — `src/jev_finish.py`

Thresholds are higher here, because a false block costs the user a whole extra
turn.

| Question | blocks at |
| --- | --- |
| `claimed_without_verifying` | 0.85 |
| `left_work_undone` | 0.85 |
| `left_placeholder_code` | 0.85 |
| `ignored_failure` | 0.80 |
| `awaiting_user_input` | **vetoes** all of the above at 0.55 |

Observed margins on the fixture set are wide: legitimate turns score ≤0.27 on
every blocking question, real failures score 0.81–0.95. That gap is what makes
0.80–0.85 safe.

### Tuning

Edit the thresholds or `QUESTIONS` criteria, then run the matching fixtures:

```bash
source .env
python3 tests/test_jev_gate.py     # 9 cases: 3 safe, 6 dangerous
python3 tests/test_jev_finish.py   # 10 cases: 5 legitimate, 5 early stops
```

`test_jev_finish.py` builds real transcript JSONL in a temp file per case, so the
hook's own parsing is exercised rather than mocked. Three of its five "allow"
cases are false-positive guards — waiting on a decision, reporting a blocker,
explicitly deferring work. Those are the ones that break when you raise
sensitivity, and the reason to run the suite before changing anything.

To add a question: add it to `QUESTIONS`, add a human-readable phrase to
`REASONS`, and add a threshold. Extra questions are nearly free.

## Troubleshooting

**Everything is allowed, nothing is ever caught.** A hook is failing open. Check
the audit log: `tail -3 ~/jev-gate.jsonl`. The most likely cause on macOS is
`CERTIFICATE_VERIFY_FAILED` — python.org builds ship without a CA bundle wired
into `urllib`. `jev_client.py` resolves this itself by trying `$SSL_CERT_FILE`,
then `/etc/ssl/cert.pem`, then the Homebrew bundle, then `certifi`, so if you're
still seeing it, none of those exist on that machine.

**`HTTP 401`** means a bad or expired key. **`HTTP 422`** means a malformed
question definition — check a recent edit to `QUESTIONS`.

**Nothing in the log at all.** `JEV_GATE_LOG` isn't set in the `env` block of
`settings.json`, or Claude Code hasn't been restarted since install.

**`"skipped": "could not reconstruct turn"`** from the completion check means it
couldn't find a human prompt in the transcript. It allows the stop in that case.
Expected on `/compact`, resumed sessions, and subagent turns.

**The completion check blocks something legitimate.** Check the scores in the log
and raise that question's threshold in `BLOCK_AT`, or add a `criteria` example
for the false case that covers your situation. Use `JEV_FINISH_OFF=1` in the
meantime — it leaves the danger gate running.

## Layout

```
src/jev_client.py        shared Jev client: TLS, timeouts, logging, fail-open
src/jev_gate.py          PreToolUse  — danger gate
src/jev_finish.py        Stop        — completion check
tests/test_jev_gate.py   9 fixture payloads, 3 safe and 6 dangerous
tests/test_jev_finish.py 10 synthetic transcripts, 5 legitimate and 5 early stops
install.sh               wire into / out of settings.json
verify.sh                prove both hooks are on and working
.env.example             config template
```

Standard library only, no dependencies.

## Adding another hook

`jev_client.py` holds everything reusable, so a new hook is roughly: define
`QUESTIONS` and thresholds, build a state string from the hook payload, call
`ask_jev`, emit the event's decision JSON, and add it to `WIRING` in
`install.sh`. Follow the fail-open contract — on `JevError`, log and allow.

Ideas that fit this pattern, none built yet:

- **`PostToolUse`** — read Bash output for swallowed errors and failing tests
  that Claude is about to plow past
- **`UserPromptSubmit`** — classify the request and auto-inject the matching
  skill, so debugging work pulls in the debugging discipline without you
  remembering to ask
- **`PostToolUse` on edits** — scope creep: is this edit beyond what was asked?
- **`PreToolUse` on writes** — repo conventions as probabilities instead of a
  CLAUDE.md file the agent sometimes skims
