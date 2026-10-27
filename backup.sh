#!/bin/bash
# Weekly Streamarr backup (servarr-backup.timer):
#   1. settings + app databases + .env → /mnt/data/Backups/Streamarr/servarr-DATE.tar.gz (keep 3)
#   2. commit + push hand-made config (see .gitignore) to GitHub
#   3. mirror that folder to Google Drive (rclone remote "gdrive")
set -euo pipefail
S=/home/karuna/servarr
DEST=/mnt/data/Backups/Streamarr
mkdir -p "$DEST" "$S/system"

# Result → dashboard tile (backup-status.json, served by Tunarr at /api/backup) + desktop popup on failure.
# Runs on any exit, so a crash part-way still shows up as failed.
loc="✗" gh="✗" dr="✗"
report() {
  local status=OK; [ "$loc$gh$dr" = "✓✓✓" ] || status=FAILED
  printf '{"status":"%s","last":"%s","local":"%s","github":"%s","drive":"%s"}\n' \
    "$status" "$(date '+%d %b %H:%M')" "$loc" "$gh" "$dr" > "$S/backup-status.json"
  [ $status = OK ] || notify-send -u critical -i dialog-error "Streamarr backup failed" \
    "Local $loc  GitHub $gh  Drive $dr — journalctl --user -u servarr-backup" 2>/dev/null || true
}
trap report EXIT

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
ls -1t "$DEST"/servarr-*.tar.gz | tail -n +4 | xargs -r rm --

loc="✓"; echo "local ok: $(ls -1t "$DEST"/servarr-*.tar.gz | head -1)"

# Drive and GitHub are independent: one failing doesn't skip the other
failed=""
cd "$S"
if git remote get-url origin >/dev/null 2>&1; then
  if git add -A && { git diff --cached --quiet || git commit -qm "Weekly backup $(date +%F)"; } && timeout 5m git push -q origin HEAD; then
    gh="✓"; echo "github ok"
  else
    failed+=" github"
  fi
fi

if timeout 30m rclone sync "$DEST" gdrive:Streamarr-Backups --include 'servarr-*.tar.gz' --stats-one-line --stats 1m -v; then
  dr="✓"; echo "drive ok"
else
  failed+=" drive"
fi

[ -z "$failed" ] || { echo "FAILED:$failed"; exit 1; }
echo "backup ok"
