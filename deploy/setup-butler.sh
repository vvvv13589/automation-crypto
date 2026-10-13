#!/usr/bin/env bash
# One-time setup of the Telegram butler's read-only AI user (run as root, safe to re-run).
#   bash deploy/setup-butler.sh
# Afterwards put the token from `claude setup-token` into /etc/cryptobot-butler/claude.env:
#   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
set -euo pipefail

AI_ROOT=/var/lib/cryptobot-ai
apt-get install -y bubblewrap socat >/dev/null  # OS sandbox for the AI's shell commands

if ! id butlerai >/dev/null 2>&1; then
  useradd --system --home-dir "$AI_ROOT/home" --create-home --shell /bin/bash butlerai
  passwd -l butlerai >/dev/null
fi
install -d -o root -g root -m 755 "$AI_ROOT" "$AI_ROOT/work" "$AI_ROOT/inbox" /opt/cryptobot-ai
install -d -o butlerai -g butlerai -m 700 "$AI_ROOT/home"
install -d -o butlerai -g butlerai -m 755 "$AI_ROOT/cwd"  # empty working dir (sandbox placeholders)
install -d -o root -g root -m 700 /etc/cryptobot-butler
chmod 700 /root

if [ ! -x "$AI_ROOT/home/.local/bin/claude" ]; then
  runuser -u butlerai -- env HOME="$AI_ROOT/home" bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
fi
runuser -u butlerai -- env HOME="$AI_ROOT/home" "$AI_ROOT/home/.local/bin/claude" --version

cp deploy/cryptobot-telegram.service /etc/systemd/system/
systemctl daemon-reload
echo "OK. Next: token into /etc/cryptobot-butler/claude.env (chmod 600), then systemctl enable --now cryptobot-telegram"
