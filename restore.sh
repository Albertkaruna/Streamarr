#!/bin/bash
# Streamarr restore — run on the new PC from the cloned repo:
#   git clone <repo> ~/servarr && ~/servarr/restore.sh ~/Downloads/servarr-DATE.tar.gz
# Safe to re-run: every step checks before it changes anything.
# Needs you for: Ubuntu + Docker install, the media disk, Google Drive login, Tailscale login (it tells you when).
set -euo pipefail
S="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="${1:-}"
step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
stop() { printf '\n\033[1;31mSTOP:\033[0m %s\n' "$*"; exit 1; }

step "1/8 Checks"
[ "$S" = /home/karuna/servarr ] || stop "repo must be at /home/karuna/servarr (user 'karuna') — paths in the timers and scripts assume it."
[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || stop "usage: $0 <servarr-DATE.tar.gz>  (newest file from Google Drive 'Streamarr-Backups/')"
for c in docker git python3; do command -v $c >/dev/null || stop "$c missing. Docker: https://docs.docker.com/engine/install/ubuntu/ ; others: sudo apt install -y git python3"; done
docker info >/dev/null 2>&1 || stop "can't talk to Docker. Run: sudo usermod -aG docker \$USER, then log out and back in."

step "2/8 Media disk at /mnt/data"
if ! mountpoint -q /mnt/data; then
  echo "Not mounted. Old fstab line (from the backup):"; tar -xzOf "$ARCHIVE" ./system/fstab-line.txt 2>/dev/null || true
  echo "Find the disk's UUID with: sudo blkid   → put it in that line, add it to /etc/fstab, then:"
  echo "  sudo mkdir -p /mnt/data && sudo mount -a"
  stop "mount the media disk, then re-run this script."
fi
mkdir -p "/mnt/data/Media/"{Movies,"TV Shows",Anime,Music,Songs,Audiobooks,Books,torrents}

step "3/8 Unpack backup (settings, databases, .env, passwords)"
sudo tar -xzpf "$ARCHIVE" -C "$S"
[ -f "$S/.env" ] || stop ".env not in the archive — wrong file?"

step "4/8 Docker waits for the media disk at boot"
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo cp "$S/system/docker-wait-data.conf" /etc/systemd/system/docker.service.d/wait-data.conf
sudo systemctl daemon-reload

step "5/8 Dashboard needs the docker group id"
gid=$(getent group docker | cut -d: -f3)
sed -i "/^  homepage:/,/^  [a-z]/ s/PGID: [0-9]*/PGID: $gid/" "$S/docker-compose.yml"
echo "docker group = $gid"

step "6/8 Start containers (pulls images — slow on mobile data)"
cd "$S"
until docker compose pull; do echo "pull failed (network?) — retrying in 30 s, Ctrl+C to stop"; sleep 30; done
docker compose up -d

step "7/8 Timers: Tunarr, Trakt sync, weekly backup"
mkdir -p ~/.config/systemd/user
cp "$S"/system/*.service "$S"/system/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tunarr.service trakt-sync.timer servarr-backup.timer
sudo loginctl enable-linger "$USER"
echo "Also turn off suspend on lid close / idle (Settings → Power) — suspend drops the data disk."

step "8/8 Logins only you can do"
if command -v rclone >/dev/null && rclone listremotes | grep -qx gdrive:; then echo "Google Drive: ok"
else echo "Google Drive (for backups): sudo apt install -y rclone && rclone config → new remote 'gdrive', type drive, browser login"; fi
if command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1; then echo "Tailscale: ok ($(tailscale status --self --peers=false | awk '{print $2}'))"
else echo "Tailscale: install from tailscale.com, delete the old 'streamarr' device in the admin console, then: sudo tailscale up --hostname=streamarr"; fi

printf '\n\033[1;32mDone.\033[0m Open http://localhost:3000 — every tile should go green within a few minutes. Logins: %s/credentials.txt\n' "$S"
