"""openplayer — Open Player for Sonos (not affiliated with Sonos, Inc.)

  openplayer                        open the mixer + visualizer app
  openplayer rooms                  list rooms and what they're doing
  openplayer favs                   list Sonos favorites (Apple Music etc.)
  openplayer play ROOM [FAVORITE]   resume, or start a favorite
  openplayer pause ROOM
  openplayer next ROOM | prev ROOM
  openplayer vol ROOM [N | +N | -N] show or change volume (0-100)
  openplayer join ROOM OTHER        make ROOM play along with OTHER
  openplayer leave ROOM             take ROOM out of its group
  openplayer apple ROOM SEARCH WORDSplay the best Apple Music match in ROOM
  openplayer radio ROOM SEARCH WORDSthat song, then similar music (Apple Music station)
  openplayer search SEARCH WORDS    list Apple Music songs
  openplayer laptop ROOM            send the laptop's sound (YouTube etc.) to ROOM
  openplayer laptop off             back to the laptop's own speakers
  openplayer lead [ROOM]            hand a group to its best-connected room (music keeps playing)
  openplayer leaders                rank rooms by connection (best group leader first)
  openplayer dropout [NOTE]         "I just heard a speaker cut out" (for the monitor)
  openplayer netreport [HOURS]      what the network monitor saw (default 24 h)
  openplayer monitor on|off|status  background network monitor (for diagnosing dropouts)
  openplayer rescan                 look for speakers again
"""
import sys
import time

from . import core, laptop


def _vol(zone, arg):
    if arg is None:
        return zone.volume
    target = zone.volume + int(arg) if arg[0] in "+-" else int(arg)
    zone.volume = max(0, min(100, target))
    if zone.volume != max(0, min(100, target)):
        raise SystemExit(f"{zone.player_name} did not accept the new volume.")
    return zone.volume


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["app"]
    cmd, rest = a[0], a[1:]

    if cmd == "app":
        from .app import main as app_main
        app_main()
    elif cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
    elif cmd == "leaders":
        from . import leader
        ordered, source, scores = leader.rank(core.rooms())
        print(f"Best group leader first (from {'the network monitor' if source == 'history' else 'a quick live test'}):")
        for z in ordered:
            bad, med, slow, old = scores[leader.key(z)]
            notes = ", ".join(n for n, on in (("older model", old), ("on crowded 2.4 GHz Wi-Fi", slow)) if on)
            print(f"  {z.player_name:13} trouble {bad * 100:5.2f}%   median {med:5.1f} ms   {notes}")
    elif cmd == "lead":
        from . import leader
        rooms = core.rooms()
        if rest:
            z, _ = core.split_room(rest)
            new = leader.handover(z, z)
        else:
            groups = {r.group.coordinator for r in rooms}
            multi = [c for c in groups if len([m for m in c.group.members if m.is_visible]) > 1]
            if not multi:
                raise SystemExit("No rooms are grouped right now.")
            new = leader.handover(max(multi, key=lambda c: len(c.group.members)))
        time.sleep(2)
        print(f"{new.player_name} now leads: " + ", ".join(
            sorted(m.player_name for m in new.group.members if m.is_visible)))
    elif cmd == "watch":
        from . import netwatch
        netwatch.run()
    elif cmd == "dropout":
        from . import netwatch
        netwatch.mark(" ".join(rest))
        print("Marked. The monitor will show what the network was doing right now.")
    elif cmd == "netreport":
        from . import netwatch
        print(netwatch.report(float(rest[0]) if rest else 24))
    elif cmd == "monitor":
        import subprocess
        what = rest[0] if rest else "status"
        unit = "openplayer-watch.service"
        if what == "on":
            subprocess.run(["systemctl", "--user", "enable", "--now", unit], check=True)
            print("Network monitor is on. Mark dropouts with d in the app or `openplayer dropout`.")
        elif what == "off":
            subprocess.run(["systemctl", "--user", "disable", "--now", unit], check=True)
            print("Network monitor is off.")
        else:
            state = subprocess.run(["systemctl", "--user", "is-active", unit],
                                   capture_output=True, text=True).stdout.strip()
            print(f"Network monitor: {state}")
    elif cmd == "rescan":
        print(f"Found {len(core.scan())} speakers.")
    elif cmd == "rooms":
        for z in core.rooms():
            state, title, artist = core.now_playing(z)
            coord = z.group.coordinator
            grouped = "" if coord == z else f"  (with {coord.player_name})"
            song = f"  {title} — {artist}" if title and state == "PLAYING" else ""
            print(f"{z.player_name:12} vol {z.volume:3}  {state.lower().replace('_playback', ''):8}{song}{grouped}")
    elif cmd == "favs":
        for f, service in core.favorites():
            print(f"{f.title:28} {service}")
    elif cmd == "search":
        for i, r in enumerate(core.apple_search(" ".join(rest)), 1):
            print(f"{i:2}. {r['title']} — {r['artist']}  ({r['album']})")
    elif cmd == "apple":
        z, words = core.split_room(rest)
        hits = core.apple_search(" ".join(words), limit=1)
        if not hits:
            raise SystemExit("Apple Music found nothing for that.")
        core.play_apple(z, hits[0]["url"])
        print(f"{z.player_name}: {hits[0]['title']} — {hits[0]['artist']}")
    elif cmd == "radio":
        z, words = core.split_room(rest)
        hits = core.apple_search(" ".join(words), limit=1)
        if not hits:
            raise SystemExit("Apple Music found nothing for that.")
        core.play_apple_station(z, hits[0]["url"], f"{hits[0]['title']} Station")
        print(f"{z.player_name}: {hits[0]['title']} — {hits[0]['artist']}, then similar music")
    elif cmd == "play":
        z, words = core.split_room(rest)
        if words:
            core.play_favorite(z, " ".join(words))
        else:
            z.group.coordinator.play()
        print(core.now_playing(z)[0].lower())
    elif cmd == "pause":
        core.split_room(rest)[0].group.coordinator.pause()
    elif cmd == "next":
        core.split_room(rest)[0].group.coordinator.next()
    elif cmd == "prev":
        core.split_room(rest)[0].group.coordinator.previous()
    elif cmd == "vol":
        z, words = core.split_room(rest)
        print(f"{z.player_name}: {_vol(z, words[0] if words else None)}")
    elif cmd == "join":
        # Both names may contain spaces: find the split that names two rooms.
        by_name = {z.player_name.lower(): z for z in core.rooms()}
        for i in range(1, len(rest)):
            a, b = " ".join(rest[:i]).lower(), " ".join(rest[i:]).lower()
            if a in by_name and b in by_name:
                by_name[a].join(by_name[b].group.coordinator)
                print(f"{by_name[a].player_name} now plays along with {by_name[b].player_name}.")
                break
        else:
            raise SystemExit("Usage: openplayer join ROOM OTHER-ROOM")
    elif cmd == "leave":
        core.split_room(rest)[0].unjoin()
    elif cmd == "laptop":
        if not rest or rest[0].lower() == "off":
            laptop.stop()
            print("Laptop sound is back on the laptop speakers.")
        else:
            z = core.split_room(rest)[0]
            laptop.start(z)
            print(f"Laptop sound now plays in {z.player_name} (about 1-2 s behind).")
    else:
        raise SystemExit(f"Unknown command '{cmd}'. Try: openplayer help")


if __name__ == "__main__":
    main()
