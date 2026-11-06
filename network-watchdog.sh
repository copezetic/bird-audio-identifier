#!/bin/bash
# Network watchdog for the Raspberry Pi. Run every 5 minutes by a systemd timer.
#
# The Pi's wifi can drop and never come back on its own; the listener then keeps recording
# into a queue nobody reads. Escalate: after 2 failed checks (10 min) restart the wifi stack,
# after 6 (30 min) reboot. The counter lives in /run, so it resets on every boot.
set -u
STATE=/run/bird-netcheck.fails
URL=${NETCHECK_URL:?set NETCHECK_URL to your API endpoint}

fails=$(cat "$STATE" 2>/dev/null || echo 0)
if curl -fsS -o /dev/null --max-time 20 -A "BirdListener/1.0 (netcheck)" "$URL"; then
  [ "$fails" -gt 0 ] && echo "network back after $fails failed check(s)"
  echo 0 > "$STATE"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$STATE"
echo "cannot reach the worker (check $fails)"
if [ "$fails" -ge 6 ]; then
  echo "30 minutes offline; rebooting"
  systemctl reboot
elif [ "$fails" -eq 2 ] || [ "$fails" -eq 4 ]; then
  echo "restarting the wifi stack"
  if systemctl is-active --quiet NetworkManager; then
    nmcli radio wifi off; sleep 3; nmcli radio wifi on
    systemctl restart NetworkManager
  else
    ip link set wlan0 down; sleep 3; ip link set wlan0 up
  fi
fi
