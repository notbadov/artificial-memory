#!/bin/sh
set -eu
reinstall=
guard=0
'{ROOT}/runtime 1.2/python/bin/python3' -E -s '{ROOT}/home of O'"'"'Neil & Сын/.agent-platform/client'/sync-client-guard.py --lock-wait 30 --self-check-limit 60 || guard=$?
case "$guard" in
  0) ;;
  10) reinstall=1 ;;
  11) exit 0 ;;
  74|75) exit "$guard" ;;
  *)
    printf '{"event":"%s","at":"%s"}\n' guard_failed "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> '{ROOT}/home of O'"'"'Neil & Сын/.agent-platform/client/launcher-events.jsonl'
    echo 'agent-platform: the launch guard did not run; starting the sync client unchecked' >&2
    ;;
esac
IFS= read -r AGENT_PLATFORM_CLIENT_TOKEN < '{ROOT}/home of O'"'"'Neil & Сын/.agent-platform/client/credential'
export AGENT_PLATFORM_CLIENT_TOKEN
if [ -n "$reinstall" ]; then
  echo 'agent-platform: no sync client can start; installing the approved release again' >&2
  exec '{ROOT}/runtime 1.2/python/bin/python3' -E -s '{ROOT}/runtime 1.2/bootstrap/companion_install.py' --control-plane-url https://service.test/agent-api --client-id golden-computer --display-name 'Мария'"'"'s "Mac" & PC' --update-policy automatic --release-trust-file '{ROOT}/home of O'"'"'Neil & Сын/trust.json' --release-cache '{ROOT}/home of O'"'"'Neil & Сын/cache' --scheduler os --schedule-layout legacy --provider codex
fi
exec '{ROOT}/runtime 1.2/python/bin/python3' '{ROOT}/home of O'"'"'Neil & Сын/.agent-platform/client/sync_client.py' --client-id golden-computer --display-name 'Мария'"'"'s "Mac" & PC' --update-policy automatic --control-plane-url https://service.test/agent-api --release-trust-file '{ROOT}/home of O'"'"'Neil & Сын/trust.json' --release-cache '{ROOT}/home of O'"'"'Neil & Сын/cache' --codex-root '{ROOT}/home of O'"'"'Neil & Сын/.codex' --claude-root '{ROOT}/home of O'"'"'Neil & Сын/.claude' --claude-config '{ROOT}/home of O'"'"'Neil & Сын/.claude.json' --bin-dir '{ROOT}/home of O'"'"'Neil & Сын/.local/bin' --project-mappings '{ROOT}/home of O'"'"'Neil & Сын/.agent-platform/project-mappings.json' --provider codex
