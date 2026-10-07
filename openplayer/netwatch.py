"""Watch the Sonos network for the moments speakers cut out.

`openplayer watch` runs this (as openplayer-stream's sibling service
openplayer-watch.service). One JSON line per event goes to
~/.local/state/openplayer/netwatch/YYYY-MM-DD.jsonl:

  ping    every 5 s   round-trip ms per speaker (None = no answer in 1 s)
  radio   every 60 s  per speaker: band/channel, PHY errors since the last
                      read (the speaker resets the counter on every read, so
                      this is errors per minute), signal to other speakers
  play    every 10 s  transport state + group of every room (only on change)
  wifi    every 5 min access points the laptop can see
  mark    on demand   "I heard a dropout" (openplayer dropout / key d in the app)

`openplayer netreport [HOURS]` summarises it.
"""
import concurrent.futures as cf
import json
import re
import statistics
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import core

DIR = core.STATE_DIR / "netwatch"
KEEP_DAYS = 14
SLOW_MS = 150


def _file(day=None):
    DIR.mkdir(parents=True, exist_ok=True)
    return DIR / f"{(day or datetime.now()).strftime('%Y-%m-%d')}.jsonl"


def write(kind, **data):
    with open(_file(), "a") as f:
        f.write(json.dumps({"t": round(time.time(), 1), "k": kind, **data}) + "\n")


def mark(note=""):
    write("mark", note=note)


# ---------------------------------------------------------------- speakers

def _get(ip, path, timeout=3):
    with urllib.request.urlopen(f"http://{ip}:{core.SONOS_PORT}{path}", timeout=timeout) as r:
        return r.read().decode(errors="replace")


KNOWN = DIR / "speakers.json"


def speakers(known=None):
    """ip -> {'name': 'Kitchen·78', 'mac5': first 5 MAC bytes, 'model': 'S33'}.

    Scans the whole network (not the cache) and keeps every speaker in
    `known` that doesn't answer right now, so a speaker that drops off is
    still pinged (and shows up as lost) instead of silently disappearing.
    """
    known = dict(known or {})
    out = {}
    for ip in sorted(set(core.scan()) | set(known)):
        try:
            x = _get(ip, "/xml/device_description.xml")
        except OSError:
            if ip in known:
                out[ip] = known[ip]
            continue
        g = lambda k: (re.search(f"<{k}>(.*?)</{k}>", x) or [None, "?"])[1]
        rincon = re.search(r"RINCON_([0-9A-F]{12})", x)
        out[ip] = {"name": f"{g('roomName')}·{ip.split('.')[-1]}", "model": g("modelNumber"),
                   "mac5": rincon.group(1)[:10] if rincon else "", "seen": True}
    # A speaker that moved to a new address: drop its old, silent entry.
    live = {v["mac5"] for v in out.values() if v.get("seen")}
    out = {ip: v for ip, v in out.items() if v.get("seen") or v["mac5"] not in live}
    for v in out.values():
        v.pop("seen", None)
    return out


def _load_known():
    try:
        return json.loads(KNOWN.read_text())
    except (OSError, ValueError):
        return {}


def _ping(ip):
    r = subprocess.run(["ping", "-c1", "-W1", "-n", ip], capture_output=True, text=True)
    m = re.search(r"time=([\d.]+)", r.stdout)
    return round(float(m.group(1)), 1) if m else None


def _radio(ip, by_mac5):
    s = re.sub(r"<[^>]*>", "\n", _get(ip, "/status/proc/ath_rincon/status"))
    mode = (re.search(r"Mode:\s*(.*)", s) or [None, "?"])[1].strip()
    chan = re.search(r"Operating on channel (\d+)", s)
    phy = re.search(r"PHY errors since last reading/reset:\s*(\d+)", s)
    links = {}
    for m in re.finditer(r"Node ([0-9A-F:]{17}) - FROM (\d+).*?: TO (\d+)\s*: STP (\d+)", s):
        who = by_mac5.get(m.group(1).replace(":", "")[:10], m.group(1))
        links[who] = [int(m.group(2)), int(m.group(3)), int(m.group(4))]
    return {"mode": mode, "mhz": int(chan.group(1)) if chan else None,
            "phy": int(phy.group(1)) if phy else None, "links": links}


def _wifi():
    out = subprocess.run(["nmcli", "-t", "-f", "SSID,BSSID,CHAN,SIGNAL,ACTIVE", "dev", "wifi",
                          "list", "--rescan", "no"], capture_output=True, text=True).stdout
    aps = []
    for line in out.splitlines():
        parts = line.replace("\\:", "-").split(":")
        if len(parts) == 5:
            aps.append(dict(zip(("ssid", "bssid", "chan", "signal", "active"), parts)))
    return aps


def _gateway():
    out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout.split()
    return out[2] if len(out) > 2 else None


def run():
    """The monitor loop (openplayer-watch.service). Never exits on its own:
    problems are written as "error" lines and the loop carries on."""
    spk = speakers(_load_known())
    while not spk:                      # e.g. started before Wi-Fi came up
        write("error", where="start", msg="no speakers found; retrying in 30 s")
        time.sleep(30)
        spk = speakers(_load_known())
    write("start", speakers=spk)
    for old in DIR.glob("*.jsonl"):
        if old.stat().st_mtime < time.time() - KEEP_DAYS * 86400:
            old.unlink()
    last = {"radio": 0, "play": 0, "wifi": 0, "spk": 0}
    last_play = None
    pool = cf.ThreadPoolExecutor(12)
    while True:
        t0 = time.time()
        try:
            if t0 - last["spk"] >= 300:     # speakers come and go; addresses change
                last["spk"] = t0
                new = speakers(spk) or spk
                if new != spk:
                    write("speakers", speakers=new)
                spk = new
                KNOWN.write_text(json.dumps(spk))
                names = {ip: v["name"] for ip, v in spk.items()}
                by_mac5 = {v["mac5"]: v["name"] for v in spk.values()}
            targets = dict(names)
            gw = _gateway()
            if gw:
                targets[gw] = "router"       # laptop <-> router: tells laptop-side hiccups apart
            rtts = dict(zip(targets.values(), pool.map(_ping, targets)))
            write("ping", ms=rtts)
            if t0 - last["play"] >= 10:
                last["play"] = t0
                try:
                    play = {}
                    for z in core.rooms():
                        c = z.group.coordinator
                        play[z.player_name] = [c.get_current_transport_info()["current_transport_state"],
                                               c.player_name]
                    if play != last_play:
                        write("play", rooms=play)
                        last_play = play
                except (Exception, SystemExit) as e:
                    write("error", where="play", msg=str(e)[:200])
            if t0 - last["radio"] >= 60:
                last["radio"] = t0

                def radio(ip):
                    try:
                        return names[ip], _radio(ip, by_mac5)
                    except Exception as e:
                        return names[ip], {"error": str(e)[:120]}
                write("radio", speakers=dict(pool.map(radio, names)))
            if t0 - last["wifi"] >= 300:
                last["wifi"] = t0
                write("wifi", aps=_wifi())
        except Exception as e:
            write("error", where="loop", msg=f"{type(e).__name__}: {e}"[:200])
        time.sleep(max(0.0, 5 - (time.time() - t0)))


# ---------------------------------------------------------------- report

def _load(hours):
    since = time.time() - hours * 3600
    rows = []
    day = datetime.now() - timedelta(hours=hours)
    while day.date() <= datetime.now().date():
        f = _file(day)
        if f.exists():
            for line in f.read_text().splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r["t"] >= since:
                    rows.append(r)
        day += timedelta(days=1)
    return rows


def _hm(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def report(hours=24):
    rows = _load(hours)
    pings = [r for r in rows if r["k"] == "ping"]
    radios = [r for r in rows if r["k"] == "radio"]
    marks = [r for r in rows if r["k"] == "mark"]
    if not pings:
        return "No monitor data yet. Is it running?  systemctl --user status openplayer-watch"
    out = [f"Sonos network, last {hours} h — {len(pings)} ping rounds "
           f"({_hm(pings[0]['t'])} → {_hm(pings[-1]['t'])}), {len(marks)} dropouts marked", ""]

    names = sorted({n for p in pings for n in p["ms"] if n != "router"})
    router = [p["ms"]["router"] for p in pings if "router" in p["ms"]]
    out.append(f"{'speaker':16} {'lost':>6} {'slow':>6} {'median':>7} {'p95':>6} {'worst':>6}   radio errors/min (median / max)   band")
    phy = {n: [r["speakers"].get(n, {}).get("phy") for r in radios] for n in names}
    band = {}
    for r in radios:
        for n, v in r["speakers"].items():
            if v.get("mhz"):
                mhz = v["mhz"]
                ch = (mhz - 2407) // 5 if mhz < 3000 else (mhz - 5000) // 5
                band[n] = f"{'2.4' if mhz < 3000 else '5'} GHz ch {ch}"
                band[n] += " (TV sat)" if "satellite" in v.get("mode", "") else ""
    for n in names:
        v = [p["ms"].get(n) for p in pings if n in p["ms"]]
        ok = sorted(x for x in v if x is not None)
        lost = 100 * sum(x is None for x in v) / max(1, len(v))
        slow = 100 * sum(x is not None and x > SLOW_MS for x in v) / max(1, len(v))
        p95 = ok[int(len(ok) * 0.95) - 1] if ok else 0
        ph = [x for x in phy[n] if x is not None]
        out.append(f"{n:16} {lost:5.1f}% {slow:5.1f}% {statistics.median(ok) if ok else 0:6.0f}ms {p95:5.0f}ms "
                   f"{max(ok) if ok else 0:5.0f}ms   {statistics.median(ph) if ph else 0:10.0f} / {max(ph) if ph else 0:<10.0f}  {band.get(n, '')}")

    if router:
        ok = sorted(x for x in router if x is not None)
        out.append(f"{'(laptop→router)':16} {100 * sum(x is None for x in router) / len(router):5.1f}% "
                   f"{100 * sum(x is not None and x > SLOW_MS for x in router) / len(router):5.1f}% "
                   f"{statistics.median(ok) if ok else 0:6.0f}ms {ok[int(len(ok) * .95) - 1] if ok else 0:5.0f}ms "
                   f"{max(ok) if ok else 0:5.0f}ms   ← if this is slow too, the hiccup is the laptop's own Wi-Fi")

    # episodes: consecutive rounds where any speaker was lost or slow
    out += ["", "Trouble moments (a speaker didn't answer or took >150 ms):"]
    episodes, cur = [], None
    for p in pings:
        bad = sorted(n for n, x in p["ms"].items() if n != "router" and (x is None or x > SLOW_MS))
        if bad:
            if cur and p["t"] - cur["end"] <= 11:
                cur["end"] = p["t"]
                cur["who"].update(bad)
                cur["n"] += 1
            else:
                cur = {"start": p["t"], "end": p["t"], "who": set(bad), "n": 1}
                episodes.append(cur)
    big = [e for e in episodes if e["n"] >= 2 or len(e["who"]) >= 3]
    for e in big[-25:]:
        out.append(f"  {_hm(e['start'])}–{_hm(e['end'])}  {len(e['who'])} speakers: {', '.join(sorted(e['who']))}")
    if not big:
        out.append("  none — every speaker answered promptly the whole time")
    out.append(f"  ({len(episodes)} brief single blips not listed)" if len(episodes) > len(big) else "")

    if marks:
        out += ["", "Around each dropout you marked (±60 s):"]
        for m in marks:
            near = [p for p in pings if abs(p["t"] - m["t"]) <= 60]
            bad = {}
            for p in near:
                for n, x in p["ms"].items():
                    if n != "router" and (x is None or x > SLOW_MS):
                        bad[n] = bad.get(n, 0) + 1
            rad = min(radios, key=lambda r: abs(r["t"] - m["t"]), default=None)
            hot = ""
            if rad and abs(rad["t"] - m["t"]) < 90:
                errs = sorted(((v.get("phy") or 0, n) for n, v in rad["speakers"].items()), reverse=True)[:3]
                hot = "; most radio errors: " + ", ".join(f"{n} {e}/min" for e, n in errs)
            what = ", ".join(f"{n} ({c}×)" for n, c in sorted(bad.items(), key=lambda kv: -kv[1])) or "network looked normal"
            out.append(f"  {_hm(m['t'])} {m.get('note') or ''}: {what}{hot}")

    # weakest speaker-to-speaker links (latest radio sample)
    if radios:
        weak = []
        for n, v in radios[-1]["speakers"].items():
            for other, (frm, to, stp) in v.get("links", {}).items():
                if frm and to and other in names:
                    weak.append((min(frm, to), n, other))
        weak = sorted(set(tuple(sorted((a, b))) + (s,) for s, a, b in weak), key=lambda x: x[2])[:6]
        if weak:
            out += ["", "Weakest speaker-to-speaker signals now (higher is better; under ~20 is weak):"]
            out += [f"  {a} ↔ {b}: {s}" for a, b, s in weak]
    return "\n".join(out)
