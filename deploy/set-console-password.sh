#!/usr/bin/env bash
# Set a new password for the Kaiba console. You type it here, on the VPS; it is written only to
# ~/.config/kaiba/dashboard.env (mode 600, the console's EnvironmentFile) and never printed.
# Changing it signs every existing session out (the session key is derived from the password).
#
# Run from your PC:  ssh -t -i ~/.ssh/kaiba_ultimate_agent_ed25519 ubuntu@203.0.113.10 'bash ~/kaiba/deploy/set-console-password.sh'
set -euo pipefail

f="$HOME/.config/kaiba/dashboard.env"
read -rsp "New console password (12+ characters): " p1; echo
read -rsp "Type it again: " p2; echo
if [ "$p1" != "$p2" ]; then echo "They do not match. Nothing changed."; exit 1; fi
if [ "${#p1}" -lt 12 ]; then echo "Use at least 12 characters. Nothing changed."; exit 1; fi
case "$p1" in
  *[\"\'\\\$\`]*|" "*|*" ") echo "Avoid quotes, backslashes, \$, backticks and leading/trailing spaces. Nothing changed."; exit 1;;
esac

umask 077
mkdir -p "$(dirname "$f")"
touch "$f"
tmp=$(mktemp "$f.XXXXXX")
grep -v '^KAIBA_DASHBOARD_PASSWORD=' "$f" > "$tmp" || true
printf 'KAIBA_DASHBOARD_PASSWORD=%s\n' "$p1" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$f"
unset p1 p2

systemctl --user restart kaiba-dashboard
sleep 3
echo "Done: console $(systemctl --user is-active kaiba-dashboard). Sign in with the new password."
