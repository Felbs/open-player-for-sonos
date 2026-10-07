"""Pick which room should lead a group.

The leader receives the music and passes it on to every other speaker in the
group, so a leader with a shaky connection makes *every* room stumble. We rank
rooms by how reliably their main speaker has answered:

1. From the network monitor's history (openplayer-watch), if there's enough:
   the share of checks where the speaker was slow (>150 ms) or silent while
   the laptop's own link to the router was fine, so laptop hiccups don't count.
2. Otherwise from a quick live test: a few pings to each candidate.

Then, regardless of the numbers:
- older "legacy" speakers (Sonos stopped feature updates for them) come
  last: leading a big group is the heaviest job, and on 10/07 a first-gen
  SYMFONISK lamp kept going unresponsive while leading six rooms;
- a speaker joined to the router's crowded 2.4 GHz Wi-Fi comes after 5 GHz
  ones. (On SonosNet every speaker uses 2.4 GHz, so that rule doesn't apply.)
"""
import concurrent.futures as cf
import json
import re
import urllib.request
import statistics
import time

from . import netwatch

HISTORY_HOURS = 24
# Model numbers Sonos lists as legacy (stability updates only): first-gen
# SYMFONISK table lamp (S20) and bookshelf (S21), Play:1/3/5 gen 1, Connect...
LEGACY_MODELS = {"S1", "S3", "S5", "S9", "S12", "S20", "S21", "ZP80", "ZP90", "ZP100", "ZP120"}
MIN_ROUNDS = 200          # below this, the history is too thin to trust
LIVE_PINGS = 5


def key(zone):
    """The monitor's name for a room's main speaker, e.g. 'Kitchen·78'."""
    return f"{zone.player_name}·{zone.ip_address.split('.')[-1]}"


def _history(hours=HISTORY_HOURS):
    """{speaker key: (bad_share, median_ms, on_24ghz)} from the monitor, or {}."""
    since = time.time() - hours * 3600
    rounds, bad, ms, band = 0, {}, {}, {}
    for f in sorted(netwatch.DIR.glob("*.jsonl"))[-3:] if netwatch.DIR.exists() else []:
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r["t"] < since:
                continue
            if r["k"] == "ping":
                router = r["ms"].get("router")
                if router is None or router >= 50:      # laptop-side hiccup: skip
                    continue
                rounds += 1
                for n, v in r["ms"].items():
                    if n == "router":
                        continue
                    bad[n] = bad.get(n, 0) + (v is None or v > netwatch.SLOW_MS)
                    if v is not None:
                        ms.setdefault(n, []).append(v)
            elif r["k"] == "radio":
                for n, v in r["speakers"].items():
                    if v.get("mhz"):
                        band[n] = v["mhz"] < 3000 and "station" in v.get("mode", "station")
    if rounds < MIN_ROUNDS:
        return {}
    return {n: (bad.get(n, 0) / rounds, statistics.median(ms[n]) if ms.get(n) else 999, band.get(n, False))
            for n in set(bad) | set(ms)}


def _live(zones):
    """Fallback: {speaker key: (bad_share, median_ms, False)} from a few pings each."""
    def probe(z):
        vals = [netwatch._ping(z.ip_address) for _ in range(LIVE_PINGS)]
        ok = [v for v in vals if v is not None]
        bad = sum(v is None or v > netwatch.SLOW_MS for v in vals) / LIVE_PINGS
        return key(z), (bad, statistics.median(ok) if ok else 999, False)
    with cf.ThreadPoolExecutor(8) as ex:
        return dict(ex.map(probe, zones))


def _legacy(zone):
    """True for models Sonos only gives stability updates (see LEGACY_MODELS)."""
    try:
        with urllib.request.urlopen(f"http://{zone.ip_address}:1400/xml/device_description.xml", timeout=3) as r:
            m = re.search(r"<modelNumber>(.*?)</modelNumber>", r.read().decode(errors="replace"))
        return bool(m) and m.group(1).strip().upper() in LEGACY_MODELS
    except OSError:
        return False


def rank(zones):
    """Zones best-first, plus where the scores came from ('history' or 'live')."""
    scores, source = _history(), "history"
    if not all(key(z) in scores for z in zones):
        scores, source = _live(zones), "live"

    legacy = {key(z): _legacy(z) for z in zones}

    def score(z):
        bad, med, slow_band = scores.get(key(z), (1.0, 999, False))
        return (legacy[key(z)], slow_band, round(bad, 4), med)
    info = {key(z): (*scores.get(key(z), (1.0, 999, False)), legacy[key(z)]) for z in zones}
    return sorted(zones, key=score), source, info


def best(zones):
    ordered, source, _ = rank(zones)
    return ordered[0], source


def handover(group_member, new_leader=None):
    """Make new_leader (default: the best-connected member) lead the group
    that group_member is in, without stopping the music. Returns the leader."""
    coord = group_member.group.coordinator
    members = [m for m in coord.group.members if m.is_visible]
    new_leader = new_leader or best(members)[0]
    if new_leader != coord:
        coord.avTransport.DelegateGroupCoordinationTo([
            ("InstanceID", 0), ("NewCoordinator", new_leader.uid), ("RejoinGroup", 1)])
    return new_leader
