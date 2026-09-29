"""Send the laptop's sound (YouTube in the browser, anything) to a Sonos room.

Chain: apps -> PipeWire "Sonos" output (virtual cable, see
~/.config/pipewire/pipewire.conf.d/60-openplayer.conf) -> swyh-rs
(openplayer-stream.service) -> http://<laptop>:5901/stream/swyh.wav -> speaker.
"""
import json
import socket
import subprocess
import time

from . import core

SERVICE = "openplayer-stream.service"
SONOS_SINK = "sonos"
PORT = 5901
STATE = core.STATE_DIR / "laptop.json"


def _run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def _my_ip(towards):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((towards, core.SONOS_PORT))
        return s.getsockname()[0]


def stream_url(zone):
    return f"http://{_my_ip(zone.ip_address)}:{PORT}/stream/swyh.wav"


def _move_all_playback_to(sink):
    _run("pactl", "set-default-sink", sink)
    for line in _run("pactl", "list", "short", "sink-inputs").splitlines():
        _run("pactl", "move-sink-input", line.split()[0], sink)


def _wait_for_server(host, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection((host, PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise SystemExit("The laptop stream did not start (check: journalctl --user -u openplayer-stream).")


def is_our_stream(zone):
    coord = zone.group.coordinator
    uri = coord.get_current_track_info().get("uri", "")
    state = coord.get_current_transport_info()["current_transport_state"]
    return f":{PORT}/stream/swyh" in uri and state in ("PLAYING", "TRANSITIONING")


def receiving_leader(exclude):
    """The room already playing the laptop stream, if any."""
    for z in core.rooms():
        if z.group.coordinator == z and z != exclude and is_our_stream(z):
            return z
    return None


def start(zone, join_existing=True):
    previous = _run("pactl", "get-default-sink")
    if previous != SONOS_SINK:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"previous_sink": previous}))
    _run("systemctl", "--user", "start", SERVICE)
    _wait_for_server(_my_ip(zone.ip_address))
    _move_all_playback_to(SONOS_SINK)
    # Send at full level; turning it down here would throw away detail.
    # Change loudness on the speaker instead (openplayer vol ROOM ...).
    _run("pactl", "set-sink-volume", SONOS_SINK, "100%")
    _run("pactl", "set-sink-mute", SONOS_SINK, "0")
    leader = join_existing and receiving_leader(exclude=zone.group.coordinator)
    if leader:
        # Join the room that already has it, so both play in step.
        zone.join(leader)
        return
    coord = zone.group.coordinator
    coord.play_uri(stream_url(zone), title="This laptop (Open Player)")
    t0 = time.time()
    while time.time() - t0 < 10:
        if coord.get_current_transport_info()["current_transport_state"] == "PLAYING":
            return
        time.sleep(0.25)
    raise SystemExit(f"{zone.player_name} did not start playing the laptop stream.")


def stop():
    for z in core.rooms():
        if z.group.coordinator == z and is_our_stream(z):
            z.stop()
            for member in z.group.members:
                if member != z and member.is_visible:
                    member.unjoin()
    previous = json.loads(STATE.read_text())["previous_sink"] if STATE.exists() else None
    if previous:
        _move_all_playback_to(previous)
    _run("systemctl", "--user", "stop", SERVICE)
