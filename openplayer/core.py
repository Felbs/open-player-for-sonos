"""Talk to the Sonos speakers on the home network.

Discovery note: the laptop's firewall (ufw) drops the multicast replies that
normal Sonos discovery (SSDP) relies on, so we find speakers by knocking on
their control port (1400) directly and cache what we find.
"""
import concurrent.futures as cf
import ipaddress
import json
import os
import socket
import subprocess
from pathlib import Path

import soco

def _xdg(var, default):
    return Path(os.environ.get(var) or Path.home() / default) / "openplayer"


CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")          # speaker list, Apple account number
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")       # settings.json
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state")    # app.log, network monitor history
CACHE = CACHE_DIR / "speakers.json"
SONOS_PORT = 1400

# Apple Music's service id inside Sonos.
SERVICE_NAMES = {"204": "Apple Music", "77575": "Sonos Radio", "284": "YouTube Music"}


def _port_open(ip, timeout=0.6):
    try:
        with socket.create_connection((ip, SONOS_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _local_networks():
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        net = ipaddress.ip_interface(line.split()[3]).network
        if net.num_addresses <= 4096:
            yield net


def scan():
    """Probe every address on the local network(s) for a Sonos control port."""
    ips = [str(h) for net in _local_networks() for h in net.hosts()]
    with cf.ThreadPoolExecutor(256) as ex:
        found = [ip for ip, ok in zip(ips, ex.map(_port_open, ips)) if ok]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(found))
    return found


def speaker_ips(rescan=False):
    if not rescan and CACHE.exists():
        cached = json.loads(CACHE.read_text())
        # Addresses can change when the router hands out new ones.
        if cached and _port_open(cached[0]):
            return cached
    return scan()


def any_speaker():
    for ip in speaker_ips():
        if _port_open(ip):
            return soco.SoCo(ip)
    for ip in speaker_ips(rescan=True):
        return soco.SoCo(ip)
    raise SystemExit("No Sonos speakers found on this network.")


def rooms():
    """One entry per room (stereo pairs and home-theatre sets count once)."""
    by_name = {z.player_name: z for z in any_speaker().visible_zones}
    return [by_name[n] for n in sorted(by_name)]


def room(name):
    for z in rooms():
        if z.player_name.lower() == name.lower():
            return z
    names = ", ".join(z.player_name for z in rooms())
    raise SystemExit(f"No room called '{name}'. Rooms: {names}")


def favorites():
    fav = any_speaker().music_library.get_sonos_favorites()
    out = []
    for f in fav:
        uri = f.resources[0].uri if f.resources else ""
        blob = uri + (getattr(f, "resource_meta_data", "") or "")
        sid = next((SERVICE_NAMES[k] for k in SERVICE_NAMES
                    if f"sid={k}" in blob or f"SA_RINCON{k}_" in blob), "")
        out.append((f, sid))
    return out


def play_favorite(zone, title):
    for f, _ in favorites():
        if f.title.lower() == title.lower():
            coord = zone.group.coordinator
            if f.resources and f.resources[0].uri:
                coord.play_uri(f.resources[0].uri, f.resource_meta_data)
            else:
                coord.clear_queue()
                coord.add_to_queue(f.reference)
                coord.play_from_queue(0)
            return
    raise SystemExit(f"No favorite called '{title}'.")


def now_playing(zone):
    coord = zone.group.coordinator
    state = coord.get_current_transport_info()["current_transport_state"]
    track = coord.get_current_track_info()
    return state, track.get("title", ""), track.get("artist", "")


def split_room(words):
    """('Living', 'Room', 'Jazz') -> (Living Room zone, ['Jazz']); names may have spaces."""
    zones = rooms()
    for n in range(len(words), 0, -1):
        name = " ".join(words[:n]).lower()
        for z in zones:
            if z.player_name.lower() == name:
                return z, list(words[n:])
    names = ", ".join(z.player_name for z in zones)
    raise SystemExit(f"No room called '{' '.join(words)}'. Rooms: {names}")


# ---------------------------------------------------------------- Apple Music
# Apple's public catalog search finds the song; Sonos then streams it itself
# from the Apple Music account already linked in the Sonos app (lossless).

def apple_country():
    """Apple Music store country: OPENPLAYER_COUNTRY, else from the locale (en_GB -> GB), else US."""
    import locale
    forced = os.environ.get("OPENPLAYER_COUNTRY")
    if forced:
        return forced.upper()
    loc = os.environ.get("LC_ALL") or os.environ.get("LANG") or (locale.getlocale()[0] or "")
    part = loc.split(".")[0].split("_")
    return part[1].upper() if len(part) > 1 and len(part[1]) == 2 else "US"


def apple_search(term, kind="song", limit=15, country=None):
    import urllib.parse
    country = country or apple_country()
    import urllib.request
    entity = {"song": "song", "album": "album"}[kind]
    q = urllib.parse.urlencode({"term": term, "entity": entity, "limit": limit,
                                "country": country, "media": "music"})
    with urllib.request.urlopen("https://itunes.apple.com/search?" + q, timeout=10) as r:
        results = json.load(r)["results"]
    out = []
    for r in results:
        if kind == "song":
            out.append({"title": r["trackName"], "artist": r["artistName"],
                        "album": r.get("collectionName", ""), "url": r["trackViewUrl"]})
        else:
            out.append({"title": r["collectionName"], "artist": r["artistName"],
                        "album": f"{r.get('trackCount', '?')} songs", "url": r["collectionViewUrl"]})
    return out


def play_apple(zone, url, add=False):
    """Play (or add to the queue) an Apple Music song/album/playlist link."""
    from soco.plugins.sharelink import ShareLinkPlugin
    coord = zone.group.coordinator
    if add:
        ShareLinkPlugin(coord).add_share_link_to_queue(url)
        return
    coord.clear_queue()
    ShareLinkPlugin(coord).add_share_link_to_queue(url)
    coord.play_from_queue(0)


def _apple_account_number():
    """Sonos's number for the linked Apple Music account (the 'sn' in its links)."""
    import re
    path = CACHE_DIR / "apple_sn"
    for z in rooms():
        try:
            uris = [z.get_current_track_info().get("uri", "")]
            uris += [i.resources[0].uri for i in z.get_queue(max_items=10) if i.resources]
        except Exception:
            continue
        m = re.search(r"sid=204\S*?sn=(\d+)", " ".join(uris))
        if m:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(m.group(1))
            return m.group(1)
    return path.read_text().strip() if path.exists() else None


def play_apple_station(zone, song_url, title=""):
    """Apple Music station from one song: plays it, then similar music, endlessly."""
    import re
    from xml.sax.saxutils import escape
    song_id = re.search(r"[?&]i=(\d+)", song_url).group(1)
    sn = _apple_account_number()
    if sn is None:
        # Nothing from Apple Music has played yet, so Sonos hasn't told us the
        # account number. Queue the song the normal way; Sonos fills it in.
        play_apple(zone, song_url)
        sn = _apple_account_number()
        if sn is None:
            return      # the song itself is playing; just no station this time
    meta = ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            f'<item id="100c2068radio%3ara.{song_id}" parentID="-1" restricted="true">'
            f'<dc:title>{escape(title or "Apple Music station")}</dc:title>'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
            'SA_RINCON52231_X_#Svc52231-0-Token</desc></item></DIDL-Lite>')
    zone.group.coordinator.play_uri(
        f"x-sonosapi-radio:radio%3ara.{song_id}?sid=204&flags=8300&sn={sn}", meta)
