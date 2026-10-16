#!/usr/bin/env bash
# Run on a NEW Ubuntu 24.04 VM as root. Does not start the bot or create credentials.
set -euo pipefail
if [[ "$(id -u)" != 0 ]]; then
  echo 'Run with sudo bash install.sh' >&2
  exit 1
fi
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
  echo 'This installer targets Ubuntu 24.04 LTS only.' >&2
  exit 1
fi
revision='355a813943699e6838595c752c17a20eed0beb68'
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
release="/opt/dental-bot/releases/$revision"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv ca-certificates
if ! id dentalbot >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin dentalbot
fi
install -d -m 0755 /opt/dental-bot/releases
install -d -o root -g dentalbot -m 0750 /etc/dental-bot
if [[ ! -d "$release/.git" ]]; then
  git clone https://github.com/penguin765/dental-material_discord-bot.git "$release"
fi
git -C "$release" checkout --detach "$revision"
test "$(git -C "$release" rev-parse HEAD)" = "$revision"
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install -r "$release/requirements.txt"
"$release/.venv/bin/python" -m pip check
(
  cd "$release"
  "$release/.venv/bin/python" -m unittest discover -s tests -q
)
if systemctl is-active --quiet dental-bot; then
  echo 'Existing bot is running. Stop it before changing the release.' >&2
  exit 1
fi
ln -sfn "$release" /opt/dental-bot/current
install -o root -g root -m 0644 "$script_dir/check-config.py" /opt/dental-bot/check-config.py
install -o root -g root -m 0644 "$script_dir/dental-bot.service" /etc/systemd/system/dental-bot.service
if [[ ! -f /etc/dental-bot/bot.env ]]; then
  install -o root -g root -m 0600 "$script_dir/bot.env.example" /etc/dental-bot/bot.env
fi
systemctl daemon-reload
echo 'Installed, but NOT started. Fill bot.env, install creds.json, then follow DEPLOY.md.'
