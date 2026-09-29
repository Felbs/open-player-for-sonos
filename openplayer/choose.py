"""Put rooms together and play something in them (what the Choose screen does)."""
from . import core, laptop, leader as leaders


def arrange(zones, clicked, selected):
    """Group exactly the selected rooms; returns the group's leader.

    zones: {room name: SoCo}; clicked: the room whose Choose button was used.
    With several rooms, the best-connected one leads (see leader.py), because
    the leader feeds every other speaker in the group.
    """
    sel = [zones[n] for n in selected]
    if len(sel) > 1:
        leader, _ = leaders.best(sel)
    else:
        leader = sel[0]
    # Decide everything from the grouping as it is now: Sonos takes a few
    # seconds to report changes, so re-reading it midway would be stale.
    led_by = {z: z.group.coordinator for z in zones.values()}
    leftovers = [z for z, c in led_by.items() if c == leader and z != leader and z not in sel]
    if led_by[leader] != leader:
        leader.unjoin()
    for z in sel:
        if z != leader and led_by[z] != leader:
            z.join(leader)
    for z in leftovers:
        z.unjoin()
    return leader


def play(zones, clicked, selected, choice):
    leader = arrange(zones, clicked, selected)
    kind = choice["kind"]
    if kind == "laptop":
        laptop.start(leader, join_existing=False)
    elif kind == "apple" and choice.get("station") and "?i=" in choice["url"]:
        core.play_apple_station(leader, choice["url"], f"{choice['title']} Station")
    elif kind == "apple":
        core.play_apple(leader, choice["url"])
    elif kind == "favorite":
        core.play_favorite(leader, choice["title"])
    elif kind == "stop":
        was_laptop = laptop.is_our_stream(leader)
        leader.stop()
        for m in list(leader.group.members):
            if m.is_visible and m != leader:
                m.unjoin()
        if was_laptop and not laptop.receiving_leader(exclude=None):
            laptop.stop()
    return leader
