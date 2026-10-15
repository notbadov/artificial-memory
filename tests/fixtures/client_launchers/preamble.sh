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
