# Streamarr

Home media server on Ubuntu: wishlist in → downloads at night → watch anywhere. *"Livin' the Stream Life"*

| Service | Port | What it does |
|---|---|---|
| Homepage | 3000 | Dashboard — links + live stats for everything below |
| Jellyfin | 8096 | Watch / download for offline (Findroid app on phone) |
| Sonarr / Radarr | 8989 / 7878 | TV shows / movies from the Trakt watchlist |
| Lidarr | 8686 | Music albums |
| Chaptarr | 8789 | Audiobooks + ebooks |
| Prowlarr | 9696 | Torrent indexers (search sites) |
| qBittorrent | 8080 | Downloads — full speed 22:00–09:00, slow by day, 1 at a time, season order |
| slskd | 5030 | Soulseek client (songs) |
| Tunarr | 5040 | Songs page: YouTube Music playlists → FLAC/320 via Soulseek (host service, not docker) |
| Gluetun + FlareSolverr | — | Proton VPN proxy, only for indexers the ISP blocks |

How it flows:
- **Shows/movies**: add on Trakt (watchlist of `karunakaran-e-e8e6b7`) → `trakt-sync.timer` forces a sync 15 min after boot + hourly →
  Sonarr/Radarr add it (all seasons) and search → qBittorrent (`config/qbittorrent/order_queue.py` slots it in season order) →
  files land in `/mnt/data/Media/{Movies,TV Shows}` → Jellyfin.
  Removed from Trakt → unmonitored (files kept). Pick seasons: nzb360 → Sonarr → untick seasons.
- **Quality**: HD 720p/1080p profile, CAM/TS blocked; music 320 kbps / FLAC.
- **Remote access**: Tailscale, PC name `streamarr` → `http://streamarr:<port>` from anywhere. Phone: nzb360 (manage), Findroid (watch).

Files: `docker-compose.yml` (all containers), `.env` (secrets — not in git), `credentials.txt` (logins — not in git),
`homepage/` (dashboard), `jellyfin-branding/` (theme CSS, logo, splash — paste via Jellyfin Dashboard → Branding),
`tunarr/tunarr.py`, `trakt_sync.py`, `backup.sh`, `system/` (systemd units, fstab line, docker drop-in — copied in by backup.sh).

## Backups (automatic)

Every Sunday 21:00, or ~10 min after next boot if the PC was off (`servarr-backup.timer` → `backup.sh`):
- **This GitHub repo**: hand-made config above. No secrets.
- **`servarr-DATE.tar.gz`** (~90 MB): all of `~/servarr` incl. `.env`, passwords and every app database
  (queues, profiles, Trakt link, Jellyfin users + watch history). Last 4 kept in `/mnt/data/Backups/Streamarr/` and
  Google Drive `Streamarr-Backups/`. **Contains secrets — keep private.**

Media files are not backed up (too big) — re-download if the disk is lost.

## Restore on a new PC

1. Install Ubuntu, then Docker (docker.com/engine/install/ubuntu), git, python3, rclone.
   `sudo usermod -aG docker $USER`, log out/in.
2. Media disk: add the line from `system/fstab-line.txt` to `/etc/fstab` (different disk → new UUID from `sudo blkid`),
   then `sudo mkdir -p /mnt/data && sudo mount -a`. If it's a new empty disk:
   `mkdir -p "/mnt/data/Media/"{Movies,"TV Shows",Anime,Music,Songs,Audiobooks,Books,torrents}`.
   Make Docker wait for the disk:
   `sudo mkdir -p /etc/systemd/system/docker.service.d && sudo cp system/docker-wait-data.conf /etc/systemd/system/docker.service.d/wait-data.conf && sudo systemctl daemon-reload`
3. Restore the files — the archive is the full copy (newest from Google Drive `Streamarr-Backups/`):
   ```
   git clone <this repo> ~/servarr
   sudo tar -xzpf servarr-DATE.tar.gz -C ~/servarr
   ```
4. Docker group id: `getent group docker` → if it isn't `117`, put the new number in `docker-compose.yml`
   (homepage → `PGID`), otherwise the dashboard can't see containers.
5. Start: `cd ~/servarr && docker compose up -d` (pulls all images — slow on mobile data; re-run if a pull times out).
6. Timers (Tunarr, Trakt sync, backup):
   ```
   mkdir -p ~/.config/systemd/user && cp system/*.service system/*.timer ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now tunarr.service trakt-sync.timer servarr-backup.timer
   sudo loginctl enable-linger $USER
   ```
7. Google Drive for backups: `rclone config` → new remote named `gdrive`, type `drive`, defaults, browser login.
8. Tailscale: install from tailscale.com, delete the old `streamarr` device in the Tailscale admin console,
   then `sudo tailscale up --hostname=streamarr` — phone apps keep working unchanged.
9. Power: keep lid-close / idle suspend off (suspend drops the data disk → I/O errors).
10. Check: dashboard http://streamarr:3000 — every tile green. Logins in `credentials.txt`.

ISP quirks (Vi hotspot): blocks torrent-site DNS (Prowlarr uses 1.1.1.1) and some sites by SNI (those go through
the `vpn`/`flaresolverr` tags in Prowlarr); small path MTU (gluetun `OPENVPN_MSSFIX=1250`). All already in the restored config.
