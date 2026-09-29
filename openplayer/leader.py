"""Pick which room should lead a group.

The leader receives the music and passes it on to every other speaker in the
group, so a leader with a shaky connection makes *every* room stumble. We rank
rooms by how reliably their main speaker has answered:

1. From the network monitor's history (openplayer-watch), if there's enough:
   the share of checks where the speaker was slow (>150 ms) or silent while
   the laptop's own link to the router was fine, so laptop hiccups don't count.
2. Otherwise from a quick live test: a few pings to each candidate.

Speakers on the crowded 2.4 GHz band are always ranked after 5 GHz ones.
"""
import concurrent.futures as cf
import json
import statistics
import time

from . import netwatch

HISTORY_HOURS = 24
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
                        band[n] = v["mhz"] < 3000
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


def rank(zones):
    """Zones best-first, plus where the scores came from ('history' or 'live')."""
    scores, source = _history(), "history"
    if not all(key(z) in scores for z in zones):
        scores, source = _live(zones), "live"

    def score(z):
        bad, med, slow_band = scores.get(key(z), (1.0, 999, False))
        return (slow_band, round(bad, 4), med)
    return sorted(zones, key=score), source, {key(z): scores.get(key(z)) for z in zones}


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
