#!/bin/sh
set -eu
guard=0
'/opt/golden runtime/python/bin/python3' -E -s '/golden home/it'"'"'s/.agent-platform/client'/sync-client-guard.py --lock-wait 30 --self-check-limit 60 || guard=$?
case "$guard" in
  0) ;;
  10) : ;;
  11) exit 0 ;;
  74|75) exit "$guard" ;;
  *)
    printf '{"event":"%s","at":"%s"}\n' guard_failed "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> '/golden home/it'"'"'s/.agent-platform/client/launcher-events.jsonl'
    echo 'agent-platform: the launch guard did not run; starting the sync client unchecked' >&2
    ;;
esac
IFS= read -r AGENT_PLATFORM_CLIENT_TOKEN < /golden/credential
export AGENT_PLATFORM_CLIENT_TOKEN
exec '/opt/golden runtime/python/bin/python3' '/golden home/it'"'"'s/.agent-platform/client/sync_client.py' --client-id golden-computer --display-name 'Мария'"'"'s "Mac" & PC'
