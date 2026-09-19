# jev-gate

Claude Code hooks that use [Jev](https://docs.typesafe.ai) to make cheap,
calibrated judgement calls in the agent loop — where a full LLM call would be
too slow and too expensive to sit in the hot path.

Two hooks so far:

| Hook | Event | What it does | Default |
| --- | --- | --- | --- |
| **danger gate** | `PreToolUse` | Blocks destructive tool calls before they run | **enforcing** |
| **completion check** | `Stop` | Judges whether Claude actually finished its turn | **log-only** |

Jev isn't a chat model — it returns calibrated probabilities on typed questions
instead of generating text. That makes each check ~350ms and ~$0.00004, cheap
enough to run on every tool call and every turn.

**The completion check does not block anything by default.** It records a verdict
per turn and gets out of the way. Whether it's accurate enough to act on is an
open question — it depends on intent that isn't visible in the transcript, which
is where cheap classification is weakest. So it collects evidence first, and you
decide from your own data whether to enforce it. See
[Evaluating the completion check](#evaluating-the-completion-check).

The danger gate has the easier job and enforces immediately: `rm -rf` is
destructive in every context, no intent required.

### Maturity

Honest status, so you can decide whether to trust it:

- **Danger gate** — works. 10/10 fixtures, ~350ms, one real-world false positive
  found and fixed so far (`--force-with-lease`). Tuned against a few hundred
  classifications, nearly all from one developer's machine. Expect to hit a false
  positive specific to your stack and to fix it in about five minutes.
- **Completion check** — unproven, which is why it ships log-only. Its fixtures
  were written by the same author as the questions they test, so they demonstrate
  the plumbing and nothing about real-world accuracy.

Neither has been validated across a team yet. If you're the second person to run
this, read [Tune it on yourself first](#tune-it-on-yourself-first).

---

## 1. Install

Requires Python 3.10+ and an existing Claude Code install. No dependencies.

```bash
git clone <repo-url> ~/jev-gate
cd ~/jev-gate
cp .env.example .env          # paste your TYPESAFE_API_KEY
./install.sh
./verify.sh                   # should print 6 OKs
```

Then **restart Claude Code** — `settings.json` is only read at startup.

Get a key at [typesafe.ai](https://typesafe.ai). Pricing is $0.042 per million
input tokens, output free.

`install.sh` appends to the `PreToolUse` and `Stop` arrays without touching hooks
you already have, backs up `settings.json` to `settings.json.bak-jevgate`, and is
safe to run twice. It can live anywhere — paths are resolved relative to the
script, so `~/jev-gate` is a suggestion, not a requirement.

To remove it:

```bash
./install.sh --uninstall
```

That unregisters both hooks and removes the key and log path it added. Your
original `settings.json` is at `~/.claude/settings.json.bak-jevgate`.

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

Runs when Claude ends a turn. Reconstructs the turn from the transcript — what
you asked for, every tool call and its result, and the closing message — and
judges whether the work was actually done or just declared done.

**In the default log-only mode it writes one line to the audit log and lets the
turn end.** You won't notice it. Four things it looks for:

| Flagged | Example |
| --- | --- |
| Claimed without verifying | "Fixed, tests should pass now" — but no test ever ran |
| Left work undone | You named three files, it edited one and said "done" |
| Left placeholder code | New `TODO`s or `throw new Error('Not implemented')` you didn't ask for |
| Ignored a failure | A test failed and the closing message never mentions it |

A fifth question, `awaiting_user_input`, **vetoes** all of the above at p≥0.55.
If Claude is asking you a question, presenting options, or reporting a blocker it
can't resolve, the verdict is "waiting on you" and nothing is flagged. Without
that veto, enforcing would trap you in a loop with an agent that can't proceed
and isn't allowed to stop and ask.

It also honours `stop_hook_active`, so even when enforcing it can only block once
per turn.

### Turning things off

```bash
export JEV_FINISH_OFF=1       # completion check off entirely, gate stays on
export JEV_GATE_DISABLE=1     # both hooks off for this shell
./install.sh --uninstall      # both off for good
```

Or set either in the `env` block of `settings.json` to make it persistent.

Since the completion check is log-only by default, `JEV_FINISH_OFF` is mostly for
when you don't want to spend the tokens.

## 3. Knowing it's on

```bash
./verify.sh
```

Six checks plus live usage stats:

```
  OK    danger gate registered as a PreToolUse hook
  OK    completion check registered as a Stop hook
  OK    TYPESAFE_API_KEY present in settings.json env
  OK    live API call blocked a destructive command
  OK    completion check correctly flagged an unverified claim
  OK    completion check is log-only (records verdicts, blocks nothing)

  danger gate         296 calls, median 357ms
  completion check     83 calls, median 361ms
  total spend        ~$0.0173  (412359 input tokens)
  errors             9 historical, none recent

  Gate is on and working.
```

The two live-API checks matter most: they push a real `rm -rf /` payload and a
real "tests should pass now" transcript through the real hooks and fail if either
isn't caught. Registration alone proves nothing, because **both hooks fail open**
— if the API is down, the key is wrong, or TLS breaks, they emit no decision and
Claude Code behaves exactly as if they weren't installed.

That's deliberate: a hook that blocks your work when a third-party API hiccups
gets uninstalled within a day. But it means silent failure is possible, and
`verify.sh` is how you rule it out.

For what it has actually been doing:

```bash
./report.sh
```

```
  DANGER GATE  (PreToolUse)

  297 tool calls classified

    blocked                 36   12.1%  ###.....................
    asked to confirm        11    3.7%  #.......................
    passed silently        250   84.2%  ####################....

  Why calls were flagged:
    destructive              19
    outside_workspace        14
    discards_local_work      14
    exfiltrates_secrets      9
    rewrites_history         8

  median 357ms, p95 425ms
```

The most useful number is **passed silently**. If that isn't well above 90%, the
gate is too chatty for the work you do and the thresholds need raising.

The 84.2% above is not a real-world rate: 6 of the 10 fixtures in
`tests/test_jev_gate.py` are dangerous by construction, and repeated test runs
dominate this log. Judge your own number from a log you built by working, not by
running the suite.

Raw log if you want it: `tail -f ~/jev-gate.jsonl | jq -c '{hook, scores}'`.

## Evaluating the completion check

The completion check ships log-only because I can't tell you whether it's
accurate on real work. Its ten fixtures were written by the same author as the
questions they test, which proves the wiring works and nothing about accuracy.

So it gathers evidence instead. Use Claude Code normally for a week, then:

```bash
./report.sh
```

```
  COMPLETION CHECK  (Stop)

  53 turns judged  [log-only]

    looked complete             15   28.3%  #######.................
    WOULD have blocked           8   15.1%  ####....................
    waiting on you (vetoed)     11   20.8%  #####...................

  Reasons:
    claimed_without_verifying  13
    left_work_undone            8
    left_placeholder_code       6
    ignored_failure             3
```

(A `[mixed]` mode label means some rows were logged while enforcing — usually
from running the fixture suite, which forces `JEV_FINISH_ENFORCE=1`.)

Then read the individual calls and judge them yourself:

```bash
./report.sh --turns
```

```
  would_block  left_work_undone
    request: Update all three chart components in src/components/Statistics: Bar, Line, and Pie.
    scores:  waiting=0.03  unverified=0.83  undone=0.93  stubs=0.08  ignored-fail=0.06
```

For each one, ask: was that flag right? Then:

- **Mostly right** → `JEV_FINISH_ENFORCE=1` is earning its keep. Set it in the
  `env` block of `settings.json`.
- **Mostly wrong** → raise the offending threshold in `BLOCK_AT`, or add a
  `criteria` example for the false case that covers your situation. Re-run
  `tests/test_jev_finish.py`, then collect another week.
- **`would_block` is a large share of turns** → it's too sensitive regardless of
  whether individual calls were defensible. Enforcing at that rate would be
  miserable.

This is the honest way to find out. Turning enforcement on before you've read
your own data is how you end up uninstalling it on day two.

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

### Tune it on yourself first

Run it alone for a week before sharing. Every false positive is a five-minute
fix — read the score in the log, sharpen the question's `criteria`, add a fixture
— and each one you catch is one your colleagues don't hit.

That matters more than it sounds. The first time the gate blocks something
legitimate, most people won't debug it. They'll uninstall it and tell a colleague
it was annoying. You get one first impression.

A worked example, from the first real false positive this repo hit:

> `git push --force-with-lease origin rebased:spike/experiment` was denied at
> p=0.91. The criteria said "force push" was dangerous, full stop — which taught
> the classifier the command's *shape* instead of the actual harm.
> `--force-with-lease` aborts rather than overwriting commits it hasn't seen, so
> it can't destroy anyone's work, and it's the normal way to push after a rebase.
>
> The fix was to reframe the question around the harm — "would this destroy
> commits another developer could lose work from?" — and name the safe form
> explicitly as a false case. Same command now scores 0.07. Plain
> `push --force origin develop` still denies at 0.93.

Check readiness with `./report.sh`: **passed silently** should be well above 95%
for the work you actually do. If it isn't, the gate is too chatty to share.

### What does it cost?

**It adds a little to API spend. It is not a cost reduction.** Be straight about
that if you're pitching it internally. Each check is ~800–950 input tokens at
$0.042/Mtok:

| Volume | Added cost |
| --- | --- |
| 1,000 checks | $0.035 |
| 10 devs × 500 tool calls + 100 turns/day × 20 days | ~$4/month |

For reference, developing this whole repo — ~380 classifications across heavy
testing — cost about **1.7 cents**.

**The payback is time, not tokens.** The danger gate is insurance: one force-push
over a colleague's work, one `DROP TABLE` against a live database, one `.env`
posted to an external host. Each costs hours, and the last one costs a credential
rotation and possibly a security review. At ~$4/month for a team, it pays for
itself preventing roughly one such event per decade.

The audit log is a second, quieter argument: a per-call record of what agents
across the team were about to do. Most teams running coding agents have no such
record at all.

**Where Jev genuinely would cut spend** is model routing — classify the task and
send easy ones to a cheaper model. That's a real reduction, but hooks can't
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

Thresholds are higher here, because a false block costs the user a whole turn.
Crossing one of these is a "hit": logged in log-only mode, blocking under
`JEV_FINISH_ENFORCE=1`.

| Question | hit at |
| --- | --- |
| `claimed_without_verifying` | 0.85 |
| `left_work_undone` | 0.85 |
| `left_placeholder_code` | 0.85 |
| `ignored_failure` | 0.80 |
| `awaiting_user_input` | **vetoes** all of the above at 0.55 |

Margins on the fixture set are wide — legitimate turns score ≤0.27 on every
blocking question, seeded failures 0.81–0.95 — but those fixtures are synthetic.
Treat the thresholds as a starting point to validate against your own log, not as
a calibrated result.

### Tuning

Edit the thresholds or `QUESTIONS` criteria, then run the matching fixtures:

```bash
source .env
python3 tests/test_jev_gate.py     # 10 cases: 4 safe, 6 dangerous
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

**The completion check blocks something legitimate.** Only possible if you set
`JEV_FINISH_ENFORCE=1`. Remove it to go back to log-only, then raise that
question's threshold in `BLOCK_AT` or add a `criteria` example covering the false
case.

**`report.sh` shows fewer calls than expected.** Tests and real sessions may be
writing to different files. `JEV_GATE_LOG` in `.env` must match the one
`install.sh` wrote into `settings.json`; if they differ, `cat` one onto the other
and fix `.env`.

## Layout

```
src/jev_client.py        shared Jev client: TLS, timeouts, logging, fail-open
src/jev_gate.py          PreToolUse  — danger gate (enforcing)
src/jev_finish.py        Stop        — completion check (log-only)
tests/test_jev_gate.py   10 fixture payloads, 4 safe and 6 dangerous
tests/test_jev_finish.py 10 synthetic transcripts, 5 legitimate and 5 early stops
install.sh               wire into / out of settings.json
verify.sh                prove both hooks are on and working
report.sh                read the audit log: what fired, and would it have been right
.env.example             config template
```

Standard library only, no dependencies.

## Adding another hook

`jev_client.py` holds everything reusable, so a new hook is roughly: define
`QUESTIONS` and thresholds, build a state string from the hook payload, call
`ask_jev`, emit the event's decision JSON, and add it to `WIRING` in
`install.sh`. Follow the fail-open contract — on `JevError`, log and allow.

**The rule these two hooks taught:** use Jev where the answer is contained in the
state you hand it. "Is this command destructive?" is fully determined by the
command text, so the danger gate works and enforces on day one. "Did Claude
finish?" depends on what you meant and what you'd already agreed — not in the
transcript — so the completion check is shakier and ships log-only.

When a question needs intent the classifier can't see, cheap classification is
the wrong primitive no matter how fast it is. If you're unsure which kind you
have, ship it log-only and let the log tell you.

Ideas that fit this pattern, none built yet. The first two are state-contained
and should behave like the danger gate; the last two need intent and would want
log-only first:

- **`PostToolUse`** — read Bash output for swallowed errors and failing tests
  that Claude is about to plow past
- **`UserPromptSubmit`** — classify the request and auto-inject the matching
  skill, so debugging work pulls in the debugging discipline without you
  remembering to ask
- **`PostToolUse` on edits** — scope creep: is this edit beyond what was asked?
- **`PreToolUse` on writes** — repo conventions as probabilities instead of a
  CLAUDE.md file the agent sometimes skims
