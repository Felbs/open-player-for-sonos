"""Put rooms together and play something in them (what the Choose screen does)."""
from . import core, laptop


def arrange(zones, clicked, selected):
    """Group exactly the selected rooms; returns the group's leader.

    zones: {room name: SoCo}; clicked: the room whose Choose button was used.
    """
    sel = [zones[n] for n in selected]
    here = zones[clicked]
    coord = here.group.coordinator
    leader = coord if coord in sel else here if here in sel else sel[0]
    if leader.group.coordinator != leader:
        leader.unjoin()
    for m in list(leader.group.members):
        if m.is_visible and m != leader and m not in sel:
            m.unjoin()
    for z in sel:
        if z != leader and z.group.coordinator != leader:
            z.join(leader)
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
