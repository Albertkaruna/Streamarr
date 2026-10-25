#!/bin/bash
# Weekly Streamarr backup (servarr-backup.timer):
#   1. settings + app databases + .env → /mnt/data/Backups/Streamarr/servarr-DATE.tar.gz (keep 4)
#   2. mirror that folder to Google Drive (rclone remote "gdrive")
#   3. commit + push hand-made config (see .gitignore) to GitHub
set -euo pipefail
S=/home/karuna/servarr
DEST=/mnt/data/Backups/Streamarr
mkdir -p "$DEST" "$S/system"

# files that live outside ~/servarr, copied in so both the archive and git carry them
cp ~/.config/systemd/user/{tunarr.service,trakt-sync.service,trakt-sync.timer,servarr-backup.service,servarr-backup.timer} "$S/system/"
cp /etc/systemd/system/docker.service.d/wait-data.conf "$S/system/docker-wait-data.conf"
grep /mnt/data /etc/fstab > "$S/system/fstab-line.txt"

# config/ has root-owned files → tar inside a container (radarr image is already on disk)
docker run --rm --entrypoint tar -v "$S":/s:ro -v "$DEST":/out lscr.io/linuxserver/radarr:latest \
  -czf "/out/servarr-$(date +%F).tar.gz" -C /s \
  --exclude=./config/jellyfin/data/metadata --exclude=./config/jellyfin/cache \
  --exclude='./config/*/MediaCover' --exclude='./config/*/logs' --exclude='*/logs.db*' \
  --exclude='*/__pycache__' --exclude=./tunarr/.cache .
ls -1t "$DEST"/servarr-*.tar.gz | tail -n +5 | xargs -r rm --

rclone sync "$DEST" gdrive:Streamarr-Backups --include 'servarr-*.tar.gz'

cd "$S"
if git remote get-url origin >/dev/null 2>&1; then
  git add -A
  git diff --cached --quiet || git commit -qm "Weekly backup $(date +%F)"
  git push -q origin HEAD
fi
echo "backup ok: $(ls -1t "$DEST"/servarr-*.tar.gz | head -1)"
