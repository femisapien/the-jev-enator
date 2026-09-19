#!/usr/bin/env bash
# Answer one question: is the gate on and working right now?
#
#   ./verify.sh
#
# Checks registration, key visibility, and a live API round trip, then prints
# recent activity from the audit log.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
GATE="$REPO/src/jev_gate.py"
PASS=0

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; PASS=1; }
note() { printf '        %s\n' "$1"; }

echo
echo "jev-gate status"
echo

# 1. Registered as a hook?
if python3 -c "
import json,sys,pathlib
d=json.loads(pathlib.Path('$SETTINGS').read_text())
h=d.get('hooks',{}).get('PreToolUse',[])
sys.exit(0 if any(x.get('command')=='$GATE' for e in h for x in e.get('hooks',[])) else 1)
" 2>/dev/null; then
  ok "registered in settings.json as a PreToolUse hook"
else
  bad "not registered in settings.json"
  note "run: $REPO/install.sh"
fi

# 2. Executable?
if [[ -x "$GATE" ]]; then
  ok "hook script is executable"
else
  bad "hook script is not executable"
  note "run: chmod +x $GATE"
fi

# 3. Key reachable by the hook process?
KEY="$(python3 -c "
import json,pathlib
d=json.loads(pathlib.Path('$SETTINGS').read_text())
print(d.get('env',{}).get('TYPESAFE_API_KEY',''))
" 2>/dev/null)"
if [[ -n "$KEY" ]]; then
  ok "TYPESAFE_API_KEY present in settings.json env"
else
  bad "TYPESAFE_API_KEY missing from settings.json env"
  note "the hook does not inherit your shell, so it must be set there"
fi

# 4. Live round trip through the real hook, with a payload that must be denied.
if [[ -n "$KEY" ]]; then
  OUT="$(echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","cwd":"'"$HOME"'","tool_input":{"command":"rm -rf / --no-preserve-root","description":"cleanup"}}' \
    | TYPESAFE_API_KEY="$KEY" python3 "$GATE" 2>&1)"
  DECISION="$(echo "$OUT" | python3 -c "
import json,sys
try: print(json.load(sys.stdin)['hookSpecificOutput']['permissionDecision'])
except Exception: print('none')
" 2>/dev/null)"
  if [[ "$DECISION" == "deny" ]]; then
    ok "live API call blocked a destructive command"
  else
    bad "live API call did not block 'rm -rf /' (got: $DECISION)"
    note "gate is failing open — set JEV_GATE_LOG and check the error"
  fi
fi

# 5. Disabled by env?
if [[ "${JEV_GATE_DISABLE:-}" == "1" ]]; then
  bad "JEV_GATE_DISABLE=1 is set — gate is bypassed"
fi

# 6. Recent real traffic.
LOG="$(python3 -c "
import json,pathlib
d=json.loads(pathlib.Path('$SETTINGS').read_text())
print(d.get('env',{}).get('JEV_GATE_LOG',''))
" 2>/dev/null)"
echo
if [[ -n "$LOG" && -f "$LOG" ]]; then
  python3 - "$LOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
scored = [r for r in rows if "scores" in r]
errs = [r for r in rows if "error" in r]
print(f"  {len(scored)} classifications logged, {len(errs)} errors")
if scored:
    lat = sorted(r["latency_ms"] for r in scored)
    toks = sum(r.get("usage", {}).get("input_tokens", 0) for r in scored)
    print(f"  median {lat[len(lat)//2]}ms, {toks} input tokens total, "
          f"~${toks / 1e6 * 0.042:.4f} spent")
if errs:
    print(f"  last error: {errs[-1].get('error')}")
PY
else
  echo "  no audit log yet (set JEV_GATE_LOG to record one)"
fi

echo
if [[ $PASS -eq 0 ]]; then
  printf '  \033[32mGate is on and working.\033[0m\n\n'
else
  printf '  \033[31mGate is NOT protecting you.\033[0m See failures above.\n\n'
fi
exit $PASS
