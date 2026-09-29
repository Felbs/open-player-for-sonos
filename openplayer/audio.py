"""Listen to what the laptop is playing and turn it into spectrum bars.

We record the *output* the laptop is sending sound to (the "monitor" of the
default sink), never the microphone. When the output is the Sonos virtual
cable the speakers play ~1-2 s later, so the bars can be delayed to match.
"""
import collections
import subprocess
import threading
import time

import numpy as np

RATE = 48000
HOP = 1024           # samples per analysis step (~21 ms)
FFT = 4096
FMIN, FMAX = 35.0, 16000.0


def default_sink():
    return subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                          text=True).stdout.strip()


def default_source():
    return subprocess.run(["pactl", "get-default-source"], capture_output=True,
                          text=True).stdout.strip()


def _reading_mic():
    """True if our recorder got linked to a microphone (it must never be)."""
    links = subprocess.run(["pw-link", "-l"], capture_output=True, text=True).stdout
    current = None
    for line in links.splitlines():
        if not line.startswith(" "):
            current = line
        elif "<-" in line and current and current.startswith("openplayer-visualizer") and "alsa_input" in line:
            return True
    return False


class Analyzer:
    def __init__(self):
        self.sink = None
        self.mode = "output"      # "output" = laptop sound, "mic" = the room (opt-in), "off"
        self.delay = 0.0          # seconds to hold bars back (Sonos latency)
        self._proc = None
        self._frames = collections.deque()   # (time, spectrum)
        self._lock = threading.Lock()
        self._window = np.hanning(FFT).astype(np.float32)
        self._ring = np.zeros(FFT, dtype=np.float32)
        freqs = np.fft.rfftfreq(FFT, 1 / RATE)
        self._freqs = freqs
        self._edges_cache = {}
        self._level = 1e-3        # slow automatic gain
        self.stop_flag = False
        threading.Thread(target=self._run, daemon=True).start()

    def _start(self, target, mode):
        self._stop_proc()
        self.sink = target
        self._started_mode = mode
        extra = [] if mode == "mic" else ["-P", "stream.capture.sink=true"]
        self._proc = subprocess.Popen(
            ["pw-record", "--target", target, *extra,
             "-P", "node.name=openplayer-visualizer",
             "--rate", str(RATE), "--channels", "2", "--format", "s16", "--raw", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        if mode == "output" and _reading_mic():
            self._stop_proc()
            self.sink = "refused: microphone"

    def _stop_proc(self):
        if self._proc:
            self._proc.kill()
            self._proc.wait()
            self._proc = None

    def _run(self):
        last_check = 0
        while not self.stop_flag:
            if self.mode == "off":
                if self._proc:
                    self._stop_proc()
                    self.sink = None
                    self._started_mode = None
                time.sleep(0.2)
                continue
            if time.time() - last_check > 2 or self.mode != getattr(self, "_started_mode", None):
                last_check = time.time()
                mode = self.mode
                target = default_source() if mode == "mic" else default_sink()
                if target and (target != self.sink or mode != getattr(self, "_started_mode", None)):
                    self._start(target, mode)
            if not self._proc:
                time.sleep(0.5)
                continue
            data = self._proc.stdout.read(HOP * 4)
            if not data:
                self._proc = None
                self.sink = None
                continue
            pcm = np.frombuffer(data, dtype=np.int16).reshape(-1, 2).mean(axis=1) / 32768.0
            self._ring = np.roll(self._ring, -len(pcm))
            self._ring[-len(pcm):] = pcm
            mag = np.abs(np.fft.rfft(self._ring * self._window))
            with self._lock:
                self._frames.append((time.time(), mag))
                while len(self._frames) > 400:     # ~8 s
                    self._frames.popleft()
        self._stop_proc()

    def _edges(self, n):
        if n not in self._edges_cache:
            edges = np.geomspace(FMIN, FMAX, n + 1)
            idx = np.searchsorted(self._freqs, edges)
            idx = np.maximum.accumulate(np.maximum(idx, 1))
            for i in range(1, len(idx)):
                if idx[i] <= idx[i - 1]:
                    idx[i] = idx[i - 1] + 1
            self._edges_cache[n] = idx
        return self._edges_cache[n]

    def bands(self, n):
        """n values in 0..1, delayed by self.delay seconds."""
        target = time.time() - self.delay
        with self._lock:
            mag = None
            for t, m in reversed(self._frames):
                if t <= target:
                    mag = m
                    break
        if mag is None:
            return np.zeros(n)
        idx = self._edges(n)
        vals = np.maximum.reduceat(mag, idx[:-1])[:n] if idx[-1] <= len(mag) else np.zeros(n)
        # Gentle tilt so treble is visible next to bass.
        vals *= np.linspace(1.0, 3.0, n)
        peak = float(vals.max())
        self._level = max(peak, self._level * 0.995, 1e-3)
        db = 20 * np.log10(np.maximum(vals / self._level, 1e-6))
        return np.clip((db + 50) / 50, 0, 1)

    def close(self):
        self.stop_flag = True
