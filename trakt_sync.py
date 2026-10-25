# Force Sonarr + Radarr to re-read their Trakt lists now (they otherwise wait up to 12 h).
# Run by trakt-sync.timer: 15 min after boot, then hourly.
import json, re, urllib.request
from pathlib import Path

CFG = Path(__file__).parent / "config"
for app, port in (("sonarr", 8989), ("radarr", 7878)):
    key = re.search(r"<ApiKey>(\w+)</ApiKey>", (CFG / app / "config.xml").read_text())[1]
    def api(path, body=None):
        req = urllib.request.Request(f"http://localhost:{port}/api/v3/{path}", headers={"X-Api-Key": key, "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body else None)
        return json.load(urllib.request.urlopen(req, timeout=30))
    for lst in api("importlist"):
        # definitionId bypasses the per-list minimum refresh interval
        api("command", {"name": "ImportListSync", "definitionId": lst["id"]})
        print(app, "synced", lst["name"])
