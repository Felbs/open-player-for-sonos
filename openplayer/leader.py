"""Pick which room should lead a group.

The leader receives the music and passes it on to every other speaker in the
group, so a leader with a shaky connection makes *every* room stumble. We rank
rooms by how reliably their main speaker has answered:

1. From the network monitor's history (openplayer-watch), if there's enough:
   the share of checks where the speaker was slow (>150 ms) or silent while
   the laptop's own link to the router was fine, so laptop hiccups don't count,
   then its recent radio errors per minute.
2. Otherwise from a quick live test: a few pings to each candidate.

Then, regardless of the numbers:
- a speaker wired to the router with Ethernet always leads: it gets the music
  over the cable and sends it over the air only once (Sonos's own advice;
  on 10/07 it ended the cut-outs);
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
PHY_HOURS = 2
# Model numbers Sonos lists as legacy (stability updates only): first-gen
# SYMFONISK table lamp (S20) and bookshelf (S21), Play:1/3/5 gen 1, Connect...
LEGACY_MODELS = {"S1", "S3", "S5", "S9", "S12", "S20", "S21", "ZP80", "ZP90", "ZP100", "ZP120"}
MIN_ROUNDS = 200          # below this, the history is too thin to trust
LIVE_PINGS = 5


def key(zone):
    """The monitor's name for a room's main speaker, e.g. 'Kitchen·78'."""
    return f"{zone.player_name}·{zone.ip_address.split('.')[-1]}"


def _history(hours=HISTORY_HOURS):
    """{speaker key: (bad_share, median_ms, on_24ghz, radio_errors_per_min)} from
    the monitor, or {}. Radio errors use only the last PHY_HOURS, because they
    change completely when the system switches between Wi-Fi and SonosNet."""
    since = time.time() - hours * 3600
    rounds, bad, ms, band, phy = 0, {}, {}, {}, {}
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
                    if v.get("phy") is not None and r["t"] >= time.time() - PHY_HOURS * 3600:
                        phy.setdefault(n, []).append(v["phy"])
    if rounds < MIN_ROUNDS:
        return {}
    return {n: (bad.get(n, 0) / rounds, statistics.median(ms[n]) if ms.get(n) else 999, band.get(n, False),
                statistics.median(phy[n]) if phy.get(n) else 0)
            for n in set(bad) | set(ms)}


def _live(zones):
    """Fallback: {speaker key: (bad_share, median_ms, False)} from a few pings each."""
    def probe(z):
        vals = [netwatch._ping(z.ip_address) for _ in range(LIVE_PINGS)]
        ok = [v for v in vals if v is not None]
        bad = sum(v is None or v > netwatch.SLOW_MS for v in vals) / LIVE_PINGS
        return key(z), (bad, statistics.median(ok) if ok else 999, False, 0)
    with cf.ThreadPoolExecutor(8) as ex:
        return dict(ex.map(probe, zones))


def _eth_rx(ip):
    try:
        with urllib.request.urlopen(f"http://{ip}:1400/status/ifconfig", timeout=3) as r:
            text = re.sub(r"<[^>]*>", "\n", r.read().decode(errors="replace"))
        m = re.search(r"^eth0\b.*?RX packets:(\d+)", text, re.S | re.M)
        return int(m.group(1)) if m else None
    except OSError:
        return None


def _wired(zones):
    """{speaker key: True} for speakers with traffic on their Ethernet port
    right now (a cable that was plugged in earlier still leaves old counts)."""
    with cf.ThreadPoolExecutor(8) as ex:
        before = dict(zip([key(z) for z in zones], ex.map(_eth_rx, [z.ip_address for z in zones])))
        time.sleep(2)
        after = dict(zip([key(z) for z in zones], ex.map(_eth_rx, [z.ip_address for z in zones])))
    return {k: before[k] is not None and after[k] is not None and after[k] > before[k] for k in before}


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
    wired = _wired(zones)

    def score(z):
        bad, med, slow_band, phy = scores.get(key(z), (1.0, 999, False, 0))
        # Whole-percent trouble buckets: below that, radio errors decide. In
        # testing, one speaker answered every check yet cut out, logging ~110k
        # radio errors a minute while leading.
        return (not wired[key(z)], legacy[key(z)], slow_band, round(bad * 100), round(phy, -4), med)
    info = {key(z): (*scores.get(key(z), (1.0, 999, False, 0)), legacy[key(z)], wired[key(z)]) for z in zones}
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
