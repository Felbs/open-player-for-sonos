"""openplayer — a Sonos mixer + music visualizer in your Omarchy theme.

Talking to the speakers happens on a background thread (SonosLink) so the
screen never freezes; the UI only ever sends it requests and gets snapshots.
"""
import json
import logging
import queue
import threading
import time

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
import numpy as np
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Footer, Input, Label, OptionList, SelectionList, Static
from textual.widgets.selection_list import Selection
from soco.exceptions import SoCoUPnPException

from . import audio, choose, core, laptop, theme

SETTINGS = core.CONFIG_DIR / "settings.json"
LOG = core.STATE_DIR / "app.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOG, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openplayer")
BLOCKS = " ▁▂▃▄▅▆▇█"
USER_HOLD = 2.5      # s: ignore polled volumes right after the user moves a slider


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(s):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s))


def _secs(hms):
    """'0:03:25' -> 205; anything else -> 0."""
    try:
        h, m, sec = (int(x) for x in (hms or "").split(":"))
        return h * 3600 + m * 60 + sec
    except ValueError:
        return 0


def _clock_hms(secs):
    secs = int(max(0, secs))
    return f"{secs // 3600}:{secs // 60 % 60:02}:{secs % 60:02}"


def _clock(secs):
    secs = int(max(0, secs))
    return f"{secs // 3600}:{secs // 60 % 60:02}:{secs % 60:02}" if secs >= 3600 else f"{secs // 60}:{secs % 60:02}"


# ---------------------------------------------------------------- speakers

class SonosLink:
    """Owns all network traffic to the speakers."""

    def __init__(self, on_snapshot, on_error):
        self.on_snapshot, self.on_error = on_snapshot, on_error
        self.zones = {}
        self._pending = {}
        self._lock = threading.Lock()
        self._cmds = queue.Queue()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def set_volume(self, name, value):
        with self._lock:
            self._pending[name] = value

    def stop(self):
        self._stop.set()

    def do(self, fn):
        """Run fn(zones) on the network thread, then refresh."""
        self._cmds.put(fn)

    def _loop(self):
        last_poll = last_rooms = 0.0
        while not self._stop.is_set():
            try:
                if not self.zones or time.time() - last_rooms > 30:
                    self.zones = {z.player_name: z for z in core.rooms()}
                    last_rooms = time.time()
                with self._lock:
                    pending, self._pending = self._pending, {}
                for name, v in pending.items():
                    self.zones[name].volume = v
                try:
                    fn = self._cmds.get(timeout=0.05)
                    try:
                        fn(self.zones)
                    except BaseException as e:   # includes SystemExit from core/laptop
                        log.exception("command failed")
                        self.on_error(str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}")
                    last_poll = 0
                except queue.Empty:
                    pass
                if time.time() - last_poll > 2:
                    last_poll = time.time()
                    snap = self._snapshot()
                    if not self._stop.is_set():
                        self.on_snapshot(snap)
            except Exception as e:          # network hiccup: report, keep going
                if self._stop.is_set():
                    return
                log.exception("speaker link")
                self.on_error(f"{type(e).__name__}: {e}")
                time.sleep(1)

    def _snapshot(self):
        snap = {}
        for name, z in self.zones.items():
            coord = z.group.coordinator
            state = coord.get_current_transport_info()["current_transport_state"]
            track = coord.get_current_track_info()
            ours = (f":{laptop.PORT}/stream/swyh" in (track.get("uri") or "")
                    and state in ("PLAYING", "TRANSITIONING"))
            snap[name] = {
                "volume": z.volume, "mute": z.mute, "state": state,
                "title": "This laptop" if ours else track.get("title") or "",
                "artist": "" if ours else track.get("artist") or "",
                "with": "" if coord == z else coord.player_name,
                "laptop": ours,
                "album": "" if ours else track.get("album") or "",
                "position": _secs(track.get("position")),
                "duration": 0 if ours else _secs(track.get("duration")),
                "radio": (track.get("uri") or "").startswith(("x-sonosapi-radio", "x-sonosprog", "x-rincon-mp3radio")),
                "at": time.time(),
            }
        return snap


# ---------------------------------------------------------------- widgets

class Visualizer(Widget):
    """Spectrum bars, drawn line by line (cheap enough for an old laptop)."""
    FPS = 20

    def __init__(self, analyzer, **kw):
        super().__init__(**kw)
        self.analyzer = analyzer
        self.pal = theme.FALLBACK
        self._shown = self._peaks = None
        self._strips = []
        self._styles_key = None

    def on_mount(self):
        self.set_interval(1 / self.FPS, self._frame)

    def _styles(self, n):
        key = (n, tuple(sorted(self.pal.items())), self.rich_style)
        if key != self._styles_key:
            base = self.rich_style
            cols = theme.gradient(self.pal, ["blue", "cyan", "magenta", "red", "yellow"], n)
            self._bar_styles = [base + Style(color=c) for c in cols]
            self._dim = base + Style(color=self.pal.get("muted", "#444444"))
            self._blank = base
            self._styles_key = key
        return self._bar_styles

    def _frame(self):
        if not self.display or self.analyzer.mode == "off":
            return
        w, h = self.size.width, self.size.height
        if w < 2 or h < 1:
            return
        n = w // 2
        vals = self.analyzer.bands(n)
        if self._shown is None or len(self._shown) != n:
            self._shown = np.zeros(n)
            self._peaks = np.zeros(n)
        self._shown = np.maximum(vals, self._shown * 0.82)          # fast attack, soft fall
        self._peaks = np.maximum(self._shown, self._peaks - 0.012)
        if self._peaks.max() < 0.01:
            self._strips = []
            self.refresh()
            return
        styles = self._styles(n)
        heights = self._shown * h                 # in rows
        peak_rows = np.floor(self._peaks * h).astype(int)
        strips = []
        for row in range(h):
            level = h - row - 1                   # rows of bar needed below this one
            fill = np.clip(heights - level, 0, 1)
            segs, gap = [], 0
            for i in range(n):
                f = fill[i]
                if f >= 1:
                    ch, st = "█ ", styles[i]
                elif f > 0:
                    ch, st = BLOCKS[int(f * 8)] + " ", styles[i]
                elif peak_rows[i] == level and self._peaks[i] > 0.02:
                    ch, st = "▔ ", self._dim
                else:
                    gap += 2
                    continue
                if gap:
                    segs.append(Segment(" " * gap, self._blank))
                    gap = 0
                segs.append(Segment(ch, st))
            segs.append(Segment(" " * (gap + w - 2 * n), self._blank))
            strips.append(Strip(segs, w))
        self._strips = strips
        self.refresh()

    def render_line(self, y):
        w = self.size.width
        if self._strips and y < len(self._strips):
            return self._strips[y]
        if not self._strips:
            lines = ["The visualizer dances to the laptop's sound (Choose… → This laptop's sound).",
                     "Apple Music streams straight to the speakers, so the laptop can't hear it —",
                     "press v to let the visualizer listen to the room through the microphone instead."]
            i = y - (self.size.height // 2 - 2)
            if 0 <= i < len(lines):
                txt = lines[i][:w].center(w)
                self._styles(max(1, w // 2))
                return Strip([Segment(txt, self._dim)], w)
        return Strip.blank(w, self.rich_style)


class VolumeSlider(Widget):
    """Click, drag or scroll to set 0-100."""

    class Changed(Message):
        def __init__(self, slider, value):
            super().__init__()
            self.slider, self.value = slider, value

    def __init__(self, value=0, **kw):
        super().__init__(**kw)
        self.value = value
        self.muted = False
        self.pal = theme.FALLBACK

    def set_value(self, v, notify=True):
        v = max(0, min(100, int(round(v))))
        if v != self.value:
            self.value = v
            self.refresh()
            if notify:
                self.post_message(self.Changed(self, v))

    def _from_x(self, x):
        self.set_value(x / max(1, self.size.width - 1) * 100)

    def on_mouse_down(self, event):
        self.capture_mouse()
        self._from_x(event.x)

    def on_mouse_move(self, event):
        if self.app.mouse_captured is self:
            self._from_x(event.x)

    def on_mouse_up(self, event):
        self.release_mouse()

    def on_mouse_scroll_up(self, event):
        self.set_value(self.value + 2)
        event.stop()

    def on_mouse_scroll_down(self, event):
        self.set_value(self.value - 2)
        event.stop()

    def render(self):
        w = self.size.width
        filled = round(self.value / 100 * (w - 1))
        c = self.pal
        fill = c.get("muted") if self.muted else c.get("accent")
        t = Text("━" * filled, style=fill)
        t.append("●", style=c.get("bright_foreground", fill))
        t.append("─" * max(0, w - filled - 1), style=c.get("muted"))
        return t


class SeekBar(Widget):
    """Song progress; click to jump there."""

    class Seek(Message):
        def __init__(self, fraction):
            super().__init__()
            self.fraction = fraction

    def __init__(self, **kw):
        super().__init__(**kw)
        self.fraction = 0.0
        self.pal = theme.FALLBACK

    def on_click(self, event):
        self.post_message(self.Seek(event.x / max(1, self.size.width - 1)))

    def render(self):
        w = self.size.width
        done = round(max(0.0, min(1.0, self.fraction)) * w)
        t = Text("━" * done, style=self.pal.get("accent"))
        t.append("─" * (w - done), style=self.pal.get("muted"))
        return t


class NowPlaying(Vertical):
    def compose(self) -> ComposeResult:
        yield Label("", id="np-title")
        with Horizontal(id="np-controls"):
            yield Button("|◀", id="tp-prev", compact=True, tooltip="Previous song (or back to the start)")
            yield Button("◀◀ 15", id="tp-back", compact=True, tooltip="Back 15 seconds")
            yield Button("▶ play", id="tp-play", compact=True)
            yield Button("15 ▶▶", id="tp-fwd", compact=True, tooltip="Forward 15 seconds")
            yield Button("▶|", id="tp-next", compact=True, tooltip="Next song")
            yield Label("", id="np-pos")
            yield SeekBar(id="np-bar")
            yield Label("", id="np-dur")
            yield Button("", id="vizbtn", compact=True, tooltip="What the visualizer listens to (v)")


class RoomRow(Horizontal, can_focus=True):
    def __init__(self, name, master=False, **kw):
        super().__init__(**kw)
        self.room = name
        self.master = master

    def compose(self) -> ComposeResult:
        yield Label("", classes="icon")
        yield Label(self.room, classes="name")
        yield VolumeSlider(classes="slider")
        yield Label("", classes="num")
        if not self.master:
            yield Label("", classes="status")
            yield Button("resume", classes="pp", compact=True)
            yield Button("Choose…", classes="choose", compact=True)

    def show(self, info, colors):
        slider = self.query_one(VolumeSlider)
        slider.pal = colors
        slider.muted = info.get("mute", False)
        if not info.get("held"):
            slider.set_value(info["volume"], notify=False)
        slider.refresh()
        self.query_one(".num", Label).update(
            "mute" if slider.muted else f"{slider.value:3}")
        if self.master:
            self.query_one(".icon", Label).update("◆")
            return
        playing = info["state"] == "PLAYING"
        self.query_one(".icon", Label).update("▶" if playing else "‖" if "PAUSED" in info["state"] else "·")
        what = info["title"] + (f" — {info['artist']}" if info["artist"] else "")
        if "PAUSED" in info["state"]:
            what = what and f"paused: {what}"
        elif not playing:
            what = ""
        if info["with"]:
            what = f"↪ with {info['with']}"
        self.query_one(".status", Label).update(what)
        self.query_one(".pp", Button).label = "pause" if playing else "resume"
        self.set_class(playing, "playing")
        self.set_class(info["laptop"], "laptop")


class PlayMenu(ModalScreen):
    """Where (rooms) and what (laptop, favorites, Apple Music) in one place."""
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, clicked, rooms, preselected):
        super().__init__()
        self.clicked, self.rooms, self.pre = clicked, rooms, preselected
        self.fixed = [{"kind": "laptop", "label": "▸ This laptop's sound  [dim](YouTube, browser, any app)[/]"},
                      {"kind": "stop", "label": "■ Stop"}]
        self.favs, self.found = [], []

    def compose(self) -> ComposeResult:
        with Vertical(id="menu"):
            yield Label("1 · Play in  [dim](space ticks a room)[/]", classes="head")
            yield SelectionList(*[Selection(r, r, r in self.pre) for r in self.rooms], id="where")
            yield Label("2 · What to play", classes="head")
            yield Input(placeholder="Search Apple Music: song, artist or album — press Enter", id="search")
            yield Checkbox("∞ After a song, keep playing similar music (Apple Music station)",
                           self.app.settings.get("autoplay", True), id="autoplay")
            yield Label("", id="menumsg")
            yield OptionList(id="what")

    def on_mount(self):
        self._fill()
        self.query_one("#what").focus()
        self.run_worker(self._load_favs, thread=True)

    def _load_favs(self):
        try:
            favs = [{"kind": "favorite", "title": f.title,
                     "label": f"★ {f.title}  [dim]{svc}[/]"} for f, svc in core.favorites()]
        except Exception as e:
            favs = []
            self.app.call_from_thread(self._msg, f"Couldn't load favorites: {e}")
        self.app.call_from_thread(self._set_favs, favs)

    def _set_favs(self, favs):
        self.favs = favs
        self._fill()

    def _msg(self, text):
        self.query_one("#menumsg", Label).update(text)

    def _fill(self):
        ol = self.query_one("#what", OptionList)
        keep = ol.highlighted
        ol.clear_options()
        ol.add_options([o["label"] for o in self.options])
        if self.options:
            ol.highlighted = 0 if keep is None or self.found else min(keep, len(self.options) - 1)

    @property
    def options(self):
        return self.found + self.fixed + self.favs

    @on(Input.Submitted, "#search")
    def search(self, event):
        term = event.value.strip()
        if term:
            self._msg("Searching Apple Music…")
            self.run_worker(lambda: self._search(term), thread=True, exclusive=True)

    def _search(self, term):
        try:
            songs = core.apple_search(term, "song", limit=12)
            albums = core.apple_search(term, "album", limit=4)
        except Exception as e:
            self.app.call_from_thread(self._msg, f"Search failed: {e}")
            return
        found = ([{"kind": "apple", "url": r["url"], "title": r["title"],
                   "label": f"♪ {r['title']} — {r['artist']}  [dim]{r['album']}[/]"} for r in songs] +
                 [{"kind": "apple", "url": r["url"], "title": r["title"],
                   "label": f"◉ {r['title']} — {r['artist']}  [dim]album · {r['album']}[/]"} for r in albums])
        self.app.call_from_thread(self._show_found, found)

    def _show_found(self, found):
        self.found = found
        self._fill()
        self._msg("Nothing found on Apple Music." if not found else
                  f"{len(found)} Apple Music results at the top — ↑ ↓ then Enter")
        self.query_one("#what").focus()

    @on(OptionList.OptionSelected, "#what")
    def chosen(self, event):
        where = list(self.query_one("#where", SelectionList).selected)
        if not where:
            self._msg("Tick at least one room at the top first.")
            return
        where.sort(key=self.rooms.index)
        choice = dict(self.options[event.option_index])
        choice["station"] = self.query_one("#autoplay", Checkbox).value
        self.dismiss({"rooms": where, "choice": choice})

    @on(Checkbox.Changed, "#autoplay")
    def autoplay_changed(self, event):
        self.app.settings["autoplay"] = event.value
        save_settings(self.app.settings)


# ---------------------------------------------------------------- app

class OpenPlayer(App):
    TITLE = "Open Player"
    CSS = """
    Screen { background: $background; }
    #top { height: 1; padding: 0 2; color: $foreground-muted; }
    #viz { height: 1fr; min-height: 6; padding: 0 2; }
    NowPlaying { height: 4; padding: 0 2; border-top: solid $panel; }
    #np-title { height: 1; width: 1fr; }
    #np-controls { height: 1; margin-top: 1; }
    #np-controls Button { margin-right: 1; min-width: 5; }
    #tp-play { width: 9; }
    #np-pos, #np-dur { width: 8; content-align: center middle; color: $foreground-muted; }
    #np-bar { width: 1fr; }
    #vizbtn { margin-left: 2; width: 26; }
    #mixer { height: auto; max-height: 60%; padding: 0 1; border-top: solid $panel; }
    RoomRow { height: 1; padding: 0 1; }
    RoomRow:focus { background: $boost; }
    RoomRow:focus .name { color: $primary; text-style: bold; }
    RoomRow.laptop .name { color: $secondary; }
    RoomRow .icon { width: 2; color: $success; }
    RoomRow .name { width: 13; }
    RoomRow .slider { width: 36; }
    RoomRow .num { width: 5; content-align: right middle; }
    RoomRow .status { width: 1fr; color: $foreground-muted; padding: 0 1; }
    RoomRow.playing .status { color: $foreground; }
    RoomRow Button { margin: 0 0 0 1; width: 10; }
    RoomRow .choose { width: 11; }
    #master { margin-bottom: 1; }
    #msg { height: 1; padding: 0 2; color: $warning; }
    PlayMenu { align: center middle; }
    #menu { width: 90; height: 90%; background: $surface; border: round $primary; padding: 0 2; }
    #menu .head { color: $primary; text-style: bold; margin-top: 1; }
    #where { height: auto; max-height: 9; border: none; background: $surface; }
    #menumsg { color: $foreground-muted; height: 1; }
    #autoplay { border: none; background: $surface; padding: 0; }
    #what { height: 1fr; border: none; background: $surface; }
    """
    BINDINGS = [
        Binding("up", "move(-1)", "Room", show=False),
        Binding("down", "move(1)", "Room", show=False),
        Binding("left", "nudge(-2)", "Vol −", show=False),
        Binding("right", "nudge(2)", "Vol +", show=False),
        Binding("enter", "choose", "Choose music & rooms"),
        Binding("space", "playpause", "Pause/Resume"),
        Binding("m", "mute", "Mute"),
        Binding("a,f", "choose", "Choose", show=False),
        Binding("l", "laptop_here", "Laptop → room", show=False),
        Binding("o", "laptop_off", "Laptop off"),
        Binding("comma", "transport('prev')", "Prev"),
        Binding("full_stop", "transport('next')", "Next"),
        Binding("shift+left", "transport('back')", "−15s", show=False),
        Binding("shift+right", "transport('fwd')", "+15s", show=False),
        Binding("v", "viz_source", "Visualizer"),
        Binding("d", "dropout", "Mark dropout"),
        Binding("left_square_bracket", "delay(-0.1)", "Sync −", show=False),
        Binding("right_square_bracket", "delay(0.1)", "Sync +", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.analyzer = audio.Analyzer()
        self.analyzer.mode = self.settings.get("viz_mode", "output")
        self.pal = theme.palette()
        self.theme_stamp = theme.stamp()
        self.snap = {}
        self.touched = {}          # room -> time the user last moved its slider
        self.np_room = None        # room the now-playing bar shows
        self.link = None

    # layout
    def compose(self) -> ComposeResult:
        yield Static("", id="top")
        yield Visualizer(self.analyzer, id="viz")
        yield NowPlaying(id="np")
        with VerticalScroll(id="mixer"):
            yield RoomRow("All rooms", master=True, id="master")
        yield Static("", id="msg")
        yield Footer()

    def on_mount(self):
        self.apply_theme()
        self.link = SonosLink(
            lambda s: self.call_from_thread(self.got_snapshot, s),
            lambda e: self.call_from_thread(self.say, e))
        self.set_interval(1.0, self.tick)
        self.say("Finding speakers…")

    # theme
    def apply_theme(self):
        c = self.pal
        name = f"omarchy-{theme.name()}-{int(time.time())}"
        self.register_theme(Theme(
            name=name, primary=c["accent"], secondary=c.get("magenta"),
            accent=c.get("cyan"), foreground=c["foreground"],
            background=c["background"], surface=c.get("lighter_background", c["background"]),
            panel=c.get("selection", c["muted"]), success=c.get("green"),
            warning=c.get("yellow"), error=c.get("red"),
            dark=c.get("mode", "dark") != "light"))
        self.theme = name
        self.query_one(Visualizer).pal = c
        for s in [*self.query(VolumeSlider), *self.query(SeekBar)]:
            s.pal = c
            s.refresh()

    def tick(self):
        stamp = theme.stamp()
        if stamp != self.theme_stamp:
            self.theme_stamp = stamp
            self.pal = theme.palette()
            self.apply_theme()
        self.update_top()
        self.update_np()

    def update_top(self):
        sink = self.analyzer.sink or "…"
        sending = [n for n, i in self.snap.items() if i["laptop"]]
        if sink == "sonos":
            out = "laptop sound → " + (", ".join(sending) or "Sonos (no room yet)")
            self.analyzer.delay = self.settings.get("sonos_delay", 1.5)
            sync = f"   visual sync {self.analyzer.delay:+.1f}s  [ ]"
        else:
            out = "laptop sound → laptop speakers"
            self.analyzer.delay = 0.0
            sync = ""
        if sink.startswith("refused"):
            out = "visualizer off (would have listened to the microphone)"
        if self.analyzer.mode == "mic":
            self.analyzer.delay = 0.0
            sync = ""
            out += "   [b $warning]● mic on[/]"
        self.query_one("#top", Static).update(
            f"[b]Open Player  ·  [/b]{theme.name()}  ·  {out}{sync}")

    def say(self, msg):
        self.query_one("#msg", Static).update(msg)

    # data from the speakers
    async def got_snapshot(self, snap):
        if not self.snap:
            self.say("")
        self.snap = snap
        mixer = self.query_one("#mixer")
        existing = {r.room: r for r in self.query(RoomRow) if not r.master}
        new = [RoomRow(n, id="room-" + "".join(ch for ch in n if ch.isalnum()))
               for n in snap if n not in existing]
        if new:
            await mixer.mount_all(new)
            existing.update({r.room: r for r in new})
        now = time.time()
        for name, row in existing.items():
            if name in snap:
                info = dict(snap[name], held=now - self.touched.get(name, 0) < USER_HOLD)
                row.show(info, self.pal)
        vols = [i["volume"] for i in snap.values()]
        if vols:
            held = now - self.touched.get("All rooms", 0) < USER_HOLD
            self.query_one("#master", RoomRow).show(
                {"volume": sum(vols) / len(vols), "held": held}, self.pal)
        if not self.focused and existing:
            self.query(RoomRow).first().focus()
        self.update_np()

    # helpers
    def focused_row(self):
        f = self.focused
        while f is not None and not isinstance(f, RoomRow):
            f = f.parent
        return f

    def room_rows(self):
        return list(self.query(RoomRow))

    # slider moved (mouse or keys)
    @on(VolumeSlider.Changed)
    def slider_changed(self, event):
        row = event.slider.parent
        now = time.time()
        if row.master:
            vols = {n: i["volume"] for n, i in self.snap.items()}
            if not vols:
                return
            old = sum(vols.values()) / len(vols)
            delta = event.value - old
            self.touched["All rooms"] = now
            for r in self.room_rows():
                if r.master or r.room not in vols:
                    continue
                v = max(0, min(100, round(vols[r.room] + delta)))
                self.snap[r.room]["volume"] = v
                self.touched[r.room] = now
                r.query_one(VolumeSlider).set_value(v, notify=False)
                r.query_one(".num", Label).update(f"{v:3}")
                self.link.set_volume(r.room, v)
        else:
            self.touched[row.room] = now
            if row.room in self.snap:
                self.snap[row.room]["volume"] = event.value
            row.query_one(".num", Label).update(f"{event.value:3}")
            self.link.set_volume(row.room, event.value)
            info = self.snap.get(row.room, {})
            if info and info.get("state") != "PLAYING" and not info.get("with"):
                self.say(f"{row.room} isn't playing anything — press space to play, "
                         f"a for Apple Music, or l for laptop sound.")

    @on(Button.Pressed, ".pp")
    def pp_button(self, event):
        event.button.parent.focus()
        self.action_playpause()

    @on(Button.Pressed, ".choose")
    def choose_button(self, event):
        event.button.parent.focus()
        self.action_choose()

    # now-playing bar
    def np_target(self):
        """The room the transport buttons act on: the selected one, else one that's playing."""
        row = self.focused_row()
        if row and not row.master and row.room in self.snap:
            return row.room
        if self.np_room in self.snap:
            return self.np_room
        playing = [n for n, i in self.snap.items() if i["state"] == "PLAYING" and not i["with"]]
        return playing[0] if playing else next(iter(sorted(self.snap)), None)

    def on_descendant_focus(self, event):
        row = self.focused_row()
        if row and not row.master:
            self.np_room = row.room
            self.update_np()

    def update_np(self):
        vb = self.query_one("#vizbtn", Button)
        vb.label = {"mic": "visualizer: room mic ●", "output": "visualizer: laptop sound",
                    "off": "visualizer: off"}[self.analyzer.mode]
        self.query_one(Visualizer).display = self.analyzer.mode != "off"
        name = self.np_target()
        if not name:
            return
        info = self.snap[name]
        leader = info["with"] or name
        linfo = self.snap.get(leader, info)
        members = [n for n, i in self.snap.items() if n == leader or i["with"] == leader]
        where = leader + (f" + {len(members) - 1} more" if len(members) > 1 else "")
        playing = linfo["state"] == "PLAYING"
        what = linfo["title"] or "nothing playing"
        if linfo["artist"]:
            what += f" — {linfo['artist']}"
        if linfo["album"]:
            what += f"  [dim]· {linfo['album']}[/]"
        icon = "▶" if playing else "‖" if "PAUSED" in linfo["state"] else "·"
        self.query_one("#np-title", Label).update(f"[b]{icon} {where}  ·  [/b]{what}")
        self.query_one("#tp-play", Button).label = "‖ pause" if playing else "▶ play"
        pos, dur = linfo["position"], linfo["duration"]
        if playing:
            pos += time.time() - linfo["at"]
        pos = min(pos, dur) if dur else pos
        self.query_one("#np-pos", Label).update(_clock(pos) if (dur or pos) else "")
        self.query_one("#np-dur", Label).update(_clock(dur) if dur else ("live" if linfo["radio"] or linfo["laptop"] else ""))
        bar = self.query_one(SeekBar)
        bar.fraction = pos / dur if dur else 0.0
        bar.refresh()

    @on(Button.Pressed, "#tp-prev")
    def _b_prev(self):
        self.action_transport("prev")

    @on(Button.Pressed, "#tp-back")
    def _b_back(self):
        self.action_transport("back")

    @on(Button.Pressed, "#tp-play")
    def _b_play(self):
        self.action_playpause(self.np_target())

    @on(Button.Pressed, "#tp-fwd")
    def _b_fwd(self):
        self.action_transport("fwd")

    @on(Button.Pressed, "#tp-next")
    def _b_next(self):
        self.action_transport("next")

    @on(Button.Pressed, "#vizbtn")
    def _b_viz(self):
        self.action_viz_source()

    @on(SeekBar.Seek)
    def _seek_click(self, event):
        name = self.np_target()
        dur = self.snap.get(self.snap.get(name, {}).get("with") or name, {}).get("duration", 0) if name else 0
        if not dur:
            self.say("You can't jump around in a live stream or station song.")
            return
        self._transport(name, "to", event.fraction * dur)

    def action_transport(self, what):
        name = self.np_target()
        if name:
            self._transport(name, what)

    def _transport(self, name, what, to=None):
        def go(zones):
            coord = zones[name].group.coordinator
            pos = _secs(coord.get_current_track_info().get("position"))
            try:
                if what == "next":
                    coord.next()
                elif what == "prev":
                    coord.seek("0:00:00") if pos > 5 else coord.previous()
                elif what in ("back", "fwd"):
                    coord.seek(_clock_hms(pos + (15 if what == "fwd" else -15)))
                elif what == "to":
                    coord.seek(_clock_hms(to))
            except SoCoUPnPException as e:
                log.info("transport %s refused: %s", what, e)
                self.call_from_thread(self.say, {
                    "prev": "This station only lets you skip forward (▶|).",
                    "next": "There's no next song here.",
                }.get(what, "This one can't be rewound or fast-forwarded (a station or live stream)."))
        self.link.do(go)

    # keys
    def action_move(self, step):
        rows = self.room_rows()
        row = self.focused_row()
        i = rows.index(row) if row in rows else 0
        rows[max(0, min(len(rows) - 1, i + step))].focus()

    def action_nudge(self, step):
        row = self.focused_row()
        if row:
            s = row.query_one(VolumeSlider)
            s.set_value(s.value + step)

    def _room(self):
        row = self.focused_row()
        if not row or row.master:
            self.say("Pick a room first (↑ ↓).")
            return None
        return row.room

    def action_playpause(self, name=None):
        name = name or self._room()
        if not name:
            return
        playing = self.snap.get(name, {}).get("state") == "PLAYING"

        def go(zones):
            coord = zones[name].group.coordinator
            try:
                coord.pause() if playing else coord.play()
            except SoCoUPnPException as e:
                if e.error_code != "701":
                    raise
                # 701 = nothing to resume: offer the chooser instead.
                self.call_from_thread(self.say, f"{name} has nothing to resume — pick something to play.")
                self.call_from_thread(self.action_choose, name)
        self.link.do(go)

    def action_mute(self):
        name = self._room()
        if name:
            self.link.do(lambda zones: setattr(zones[name], "mute", not zones[name].mute))

    def action_laptop_here(self):
        name = self._room()
        if not name:
            return
        self.say(f"Sending laptop sound to {name}…")

        def go(zones):
            laptop.start(zones[name])
            self.call_from_thread(self.say, f"Laptop sound → {name}. Use this mixer for volume.")
        self.link.do(go)

    def action_laptop_off(self):
        self.say("Bringing sound back to the laptop…")

        def go(zones):
            laptop.stop()
            self.call_from_thread(self.say, "Laptop sound is back on the laptop speakers.")
        self.link.do(go)

    def action_viz_source(self):
        order = ["mic", "output", "off"]
        self.analyzer.mode = order[(order.index(self.analyzer.mode) + 1) % 3]
        self.settings["viz_mode"] = self.analyzer.mode
        save_settings(self.settings)
        self.say({"mic": "Visualizer listens to the room through the microphone (nothing is saved or sent).",
                  "output": "Visualizer follows the laptop's own sound. Microphone released.",
                  "off": "Visualizer off. Press v (or the visualizer button) to turn it back on.",
                  }[self.analyzer.mode])
        self.update_top()
        self.update_np()

    def action_dropout(self):
        from . import netwatch
        room = self.np_target() or ""
        netwatch.mark(f"(selected: {room})" if room else "")
        self.say(f"Dropout marked at {time.strftime('%H:%M:%S')} — thanks! The network monitor will line it up.")

    def action_delay(self, step):
        d = round(max(0.0, min(5.0, self.settings.get("sonos_delay", 1.5) + step)), 1)
        self.settings["sonos_delay"] = d
        save_settings(self.settings)
        self.update_top()

    def action_choose(self, name=None):
        name = name or self._room()
        if not name:
            return
        leader = self.snap.get(name, {}).get("with") or name
        pre = [n for n, i in self.snap.items() if n == leader or i.get("with") == leader]
        rooms = sorted(self.snap)

        def picked(result):
            if not result:
                return
            choice, where = result["choice"], result["rooms"]
            what = {"laptop": "laptop sound", "stop": "stop"}.get(choice["kind"], choice.get("title", ""))
            if choice.get("station") and "?i=" in choice.get("url", ""):
                what += " (+ similar music)"
            self.say(f"{what} → {', '.join(where)}…")

            def go(zones):
                lead = choose.play(zones, name, where, choice)
                led = f" (led by {lead.player_name}, the best connection)" if len(where) > 1 else ""
                self.call_from_thread(self.say, f"{'Stopped' if choice['kind'] == 'stop' else 'Playing ' + what} "
                                                f"in {', '.join(where)}{led if choice['kind'] != 'stop' else ''}.")
            self.link.do(go)
        self.push_screen(PlayMenu(name, rooms, pre), picked)

    def on_unmount(self):
        if self.link:
            self.link.stop()
        self.analyzer.close()


def main():
    OpenPlayer().run()
