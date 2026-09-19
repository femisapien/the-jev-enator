#!/usr/bin/env bash
# Install jev-gate as a PreToolUse hook in Claude Code.
#
#   ./install.sh              install for the current user
#   ./install.sh --uninstall  remove the hook, leave the repo in place
#
# Appends to the PreToolUse array without touching existing hooks, and backs up
# settings.json first. Safe to run twice.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
GATE="$REPO/src/jev_gate.py"

if [[ ! -f "$SETTINGS" ]]; then
  echo "No $SETTINGS found. Start Claude Code once, then re-run." >&2
  exit 1
fi

chmod +x "$GATE"
cp "$SETTINGS" "$SETTINGS.bak-jevgate"

MODE="install"
[[ "${1:-}" == "--uninstall" ]] && MODE="uninstall"

KEY="${TYPESAFE_API_KEY:-}"
if [[ "$MODE" == "install" && -z "$KEY" && -f "$REPO/.env" ]]; then
  # shellcheck disable=SC1091
  source "$REPO/.env"
  KEY="${TYPESAFE_API_KEY:-}"
fi

if [[ "$MODE" == "install" && -z "$KEY" ]]; then
  echo "TYPESAFE_API_KEY is not set and $REPO/.env has no key." >&2
  echo "Get a key at https://typesafe.ai, then: cp .env.example .env" >&2
  exit 1
fi

MODE="$MODE" GATE="$GATE" KEY="$KEY" SETTINGS="$SETTINGS" LOG="$HOME/jev-gate.jsonl" \
python3 - <<'PY'
import json, os, pathlib

mode = os.environ["MODE"]
gate = os.environ["GATE"]
path = pathlib.Path(os.environ["SETTINGS"])

data = json.loads(path.read_text())
hooks = data.setdefault("hooks", {}).setdefault("PreToolUse", [])


def owns(entry):
    return any(h.get("command") == gate for h in entry.get("hooks", []))


if mode == "uninstall":
    before = len(hooks)
    data["hooks"]["PreToolUse"] = [e for e in hooks if not owns(e)]
    for k in ("TYPESAFE_API_KEY", "JEV_GATE_LOG"):
        data.get("env", {}).pop(k, None)
    path.write_text(json.dumps(data, indent=2) + "\n")
    print("removed" if len(data["hooks"]["PreToolUse"]) < before else "was not installed")
else:
    if not any(owns(e) for e in hooks):
        hooks.append({
            "matcher": "Bash|Write|Edit|NotebookEdit",
            "hooks": [{"type": "command", "command": gate}],
        })
    env = data.setdefault("env", {})
    env["TYPESAFE_API_KEY"] = os.environ["KEY"]
    env.setdefault("JEV_GATE_LOG", os.environ["LOG"])
    path.write_text(json.dumps(data, indent=2) + "\n")
    print("installed")
PY

echo
echo "Backup: $SETTINGS.bak-jevgate"
echo "Restart Claude Code to apply."
if [[ "$MODE" == "install" ]]; then
  echo
  echo "Verify with:  $REPO/verify.sh"
fi
