"""Tests that need no speakers or network."""
import json
import time

from openplayer import app, core, netwatch, theme


def test_clock_and_seconds_round_trip():
    assert app._secs("0:03:25") == 205
    assert app._secs("NOT_IMPLEMENTED") == 0
    assert app._secs(None) == 0
    assert app._clock(205) == "3:25"
    assert app._clock(3725) == "1:02:05"
    assert app._clock_hms(65) == "0:01:05"


def test_theme_blend_and_gradient():
    assert theme.blend("#000000", "#ffffff", 0.5) == "#808080"
    cols = theme.gradient({"a": "#000000", "b": "#ffffff"}, ["a", "b"], 3)
    assert cols == ["#000000", "#808080", "#ffffff"]


def test_apple_country(monkeypatch):
    monkeypatch.delenv("OPENPLAYER_COUNTRY", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    assert core.apple_country() == "GB"
    monkeypatch.setenv("OPENPLAYER_COUNTRY", "de")
    assert core.apple_country() == "DE"
    monkeypatch.delenv("OPENPLAYER_COUNTRY")
    monkeypatch.setenv("LANG", "C")
    assert core.apple_country() == "US"


def test_netreport_finds_trouble_and_marks(tmp_path, monkeypatch):
    monkeypatch.setattr(netwatch, "DIR", tmp_path)
    now = time.time()
    rows = []
    for i in range(20):
        slow = 8 <= i <= 9                      # two bad rounds in a row
        rows.append({"t": now - 100 + i * 5, "k": "ping",
                     "ms": {"Kitchen·10": 300.0 if slow else 12.0, "Patio·11": 9.0, "router": 3.0}})
    rows.append({"t": now - 55, "k": "mark", "note": "test"})
    rows.append({"t": now - 50, "k": "radio", "speakers": {
        "Kitchen·10": {"mode": "INFRA (station)", "mhz": 5805, "phy": 1200, "links": {"Patio·11": [15, 14, 3]}},
        "Patio·11": {"mode": "INFRA (station)", "mhz": 2437, "phy": 90, "links": {}}}})
    (tmp_path / netwatch._file().name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    out = netwatch.report(1)
    assert "Kitchen·10" in out and "Patio·11" in out
    assert "5 GHz ch 161" in out and "2.4 GHz ch 6" in out
    assert "1 speakers: Kitchen·10" in out                  # the trouble episode
    assert "test: Kitchen·10 (2×)" in out                   # the mark lines up with it
    assert "Kitchen·10 ↔ Patio·11: 14" in out                 # weak link
    assert "(laptop→router)" in out
