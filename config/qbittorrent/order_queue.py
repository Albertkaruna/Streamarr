#!/usr/bin/env python3
# Run by qBittorrent on every torrent add: python3 /config/order_queue.py %I
# Slots ONLY the new torrent into the queue; existing order (incl. manual "move to top") is kept.
# New torrent starts at the bottom and moves up past torrents that sort after it:
# movies/music first, then TV by season, episode (S01 of every show, then S02, ...).
# Needs "Bypass authentication for clients on localhost" (runs inside the container).
import json, re, sys, urllib.parse, urllib.request

API = "http://localhost:8080/api/v2"


def call(path, **form):
    data = urllib.parse.urlencode(form).encode() if form else None
    with urllib.request.urlopen(urllib.request.Request(f"{API}/{path}", data)) as r:
        return r.read().decode()


def key(t):
    if t["category"] != "tv":
        return (0, 0, 0)
    m = re.search(r"\bS(\d{1,2})(?:E(\d{1,3}))?", t["name"], re.I) or re.search(r"Season[ ._-]?(\d{1,2})()", t["name"], re.I)
    if not m:
        return (1, 99, 0)  # ponytail: unparseable name goes last
    return (1, int(m.group(1)), int(m.group(2) or 0))


def steps_up(queue, new):
    # queue: incomplete torrents in current queue order, `new` last. Count how far it moves up.
    # Only moves within the sorted tail of the queue: anything above the first out-of-order
    # spot was arranged by hand and is never overtaken.
    old = queue[:-1]
    tail = len(old)
    while tail > 0 and (tail == len(old) or key(old[tail - 1]) <= key(old[tail])):
        tail -= 1
    n = 0
    for t in reversed(old[tail:]):
        if key(t) <= key(new):
            break
        n += 1
    return n


def demo():
    T = lambda n, c="tv": {"name": n, "category": c}
    q = [T("Dark.S03"), T("BoJack.S01"), T("Dark.S02"), T("Show.S04")]  # user pinned Dark.S03 on top
    assert steps_up(q + [T("New.S02E01")], T("New.S02E01")) == 1          # lands after Dark.S02, before S04
    assert steps_up(q + [T("New.S01")], T("New.S01")) == 2                # after BoJack.S01; Dark.S03 stays on top
    assert steps_up(q + [T("Movie.2026", "movies")], T("Movie.2026", "movies")) == 3  # stops below pinned Dark.S03
    s = [T("Movie", "movies"), T("A.S01"), T("A.S02")]                    # untouched, sorted queue
    assert steps_up(s + [T("B.S01")], T("B.S01")) == 1
    assert steps_up(s + [T("M2", "movies")], T("M2", "movies")) == 2      # still goes up to just below first movie
    assert steps_up([] + [T("X.S01")], T("X.S01")) == 0
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        demo()
        sys.exit()
    new_hash = sys.argv[1].lower()
    queue = sorted((t for t in json.loads(call("torrents/info")) if t["progress"] < 1 and t["priority"] > 0),
                   key=lambda t: t["priority"])
    queue = [t for t in queue if t["hash"] != new_hash] + [t for t in queue if t["hash"] == new_hash]
    if queue and queue[-1]["hash"] == new_hash:
        call("torrents/bottomPrio", hashes=new_hash)
        for _ in range(steps_up(queue, queue[-1])):
            call("torrents/increasePrio", hashes=new_hash)
