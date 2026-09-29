"""The current Omarchy theme's colours, reloaded when the theme changes."""
import subprocess
import tomllib
from pathlib import Path

STATE = Path.home() / ".local" / "state" / "omarchy" / "current"
COLORS = STATE / "theme" / "colors.toml"
NAME = STATE / "theme.name"

# Used when not on Omarchy (Tokyo Night).
FALLBACK = {
    "background": "#1a1b26", "foreground": "#a9b1d6", "accent": "#7aa2f7",
    "muted": "#414868", "selection": "#292e42", "lighter_background": "#24283b",
    "red": "#f7768e", "yellow": "#e0af68", "green": "#9ece6a", "cyan": "#449dab",
    "blue": "#7aa2f7", "magenta": "#ad8ee6", "bright_foreground": "#c0caf5",
}


def stamp():
    """Changes whenever a new theme is applied."""
    try:
        return (NAME.stat().st_mtime, COLORS.stat().st_mtime)
    except OSError:
        return None


def name():
    try:
        return NAME.read_text().strip()
    except OSError:
        return "default"


def palette():
    colors = dict(FALLBACK)
    try:
        out = subprocess.run(["omarchy-theme-color", "--all"], capture_output=True,
                             text=True, timeout=3).stdout
        resolved = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
        if not resolved:
            resolved = tomllib.loads(COLORS.read_text())
        colors.update({k: v.strip() for k, v in resolved.items()
                       if isinstance(v, str) and v.strip().startswith("#")})
    except (OSError, subprocess.SubprocessError, tomllib.TOMLDecodeError):
        pass
    return colors


def blend(a, b, t):
    """Mix two #rrggbb colours; t=0 gives a, t=1 gives b."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def gradient(colors, stops, n):
    """n colours running smoothly through the given palette keys."""
    hexes = [colors[k] for k in stops]
    if n <= 1:
        return hexes[:1]
    out = []
    for i in range(n):
        pos = i / (n - 1) * (len(hexes) - 1)
        j = min(int(pos), len(hexes) - 2)
        out.append(blend(hexes[j], hexes[j + 1], pos - j))
    return out
