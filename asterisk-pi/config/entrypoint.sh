#!/bin/sh
# Host networking: wait until the Tailscale address exists so http.conf can bind to it.
i=0
while ! ip -4 addr | grep -q "inet __PI_IP__/"; do
  i=$((i+1)); [ $i -gt 120 ] && echo "tailscale IP missing, starting anyway" && break
  sleep 1
done
exec asterisk -f -U asterisk -G asterisk -vvv
