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


def test_leader_ranking_prefers_reliable_5ghz(tmp_path, monkeypatch):
    from openplayer import leader
    monkeypatch.setattr(netwatch, "DIR", tmp_path)
    monkeypatch.setattr(leader, "MIN_ROUNDS", 10)
    now = time.time()
    rows = []
    for i in range(50):
        rows.append({"t": now - 300 + i * 5, "k": "ping", "ms": {
            "Den·21": 400.0 if i % 5 == 0 else 9.0,    # often slow
            "Hall·22": 8.0,                            # solid, but on 2.4 GHz
            "Loft·23": 7.0,                            # solid, 5 GHz
            "router": 900.0 if i == 1 else 3.0}})      # a laptop-side hiccup: ignored
    rows.append({"t": now - 10, "k": "radio", "speakers": {
        "Den·21": {"mhz": 5805}, "Hall·22": {"mhz": 2437}, "Loft·23": {"mhz": 5805}}})
    (tmp_path / netwatch._file().name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    class Z:
        def __init__(self, name, ip):
            self.player_name, self.ip_address = name, ip
    zones = [Z("Den", "192.0.2.21"), Z("Hall", "192.0.2.22"), Z("Loft", "192.0.2.23")]
    monkeypatch.setattr(leader, "_legacy", lambda z: False)
    monkeypatch.setattr(leader, "_wired", lambda zs: {leader.key(z): False for z in zs})
    ordered, source, _ = leader.rank(zones)
    assert source == "history"
    assert [z.player_name for z in ordered] == ["Loft", "Den", "Hall"]
    # an old (legacy) speaker goes last even with the best numbers
    monkeypatch.setattr(leader, "_legacy", lambda z: z.player_name == "Loft")
    assert [z.player_name for z in leader.rank(zones)[0]] == ["Den", "Hall", "Loft"]
    # a speaker wired with Ethernet leads, whatever its numbers
    monkeypatch.setattr(leader, "_legacy", lambda z: False)
    monkeypatch.setattr(leader, "_wired", lambda zs: {leader.key(z): z.player_name == "Hall" for z in zs})
    assert leader.rank(zones)[0][0].player_name == "Hall"


def test_leader_ranking_radio_errors_break_ties(tmp_path, monkeypatch):
    from openplayer import leader
    monkeypatch.setattr(netwatch, "DIR", tmp_path)
    monkeypatch.setattr(leader, "MIN_ROUNDS", 10)
    monkeypatch.setattr(leader, "_legacy", lambda z: False)
    monkeypatch.setattr(leader, "_wired", lambda zs: {leader.key(z): False for z in zs})
    now = time.time()
    rows = [{"t": now - 300 + i * 5, "k": "ping", "ms": {"Den·21": 6.0, "Loft·23": 5.0, "router": 3.0}}
            for i in range(50)]
    rows += [{"t": now - 60 * i, "k": "radio", "speakers": {
        "Den·21": {"mhz": 2412, "mode": "INFRA (sonosnet)", "phy": 20000},
        "Loft·23": {"mhz": 2412, "mode": "INFRA (sonosnet)", "phy": 110000}}} for i in range(5)]
    (tmp_path / netwatch._file().name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    class Z:
        def __init__(self, name, ip):
            self.player_name, self.ip_address = name, ip
    zones = [Z("Den", "192.0.2.21"), Z("Loft", "192.0.2.23")]
    # both answer every check (and Loft a bit faster), but Loft is drowning in radio errors
    assert [z.player_name for z in leader.rank(zones)[0]] == ["Den", "Loft"]


def test_ask_any_skips_a_speaker_that_fails_mid_request(monkeypatch):
    import requests
    calls = []

    class Fake:
        def __init__(self, ip):
            self.ip = ip

        @property
        def visible_zones(self):
            calls.append(self.ip)
            if self.ip == "192.0.2.1":
                raise requests.ConnectionError("no route to host")
            return ["zone"]

    monkeypatch.setattr(core, "speaker_ips", lambda rescan=False: ["192.0.2.1", "192.0.2.2"])
    monkeypatch.setattr(core, "_port_open", lambda ip, timeout=0.6: True)
    monkeypatch.setattr(core.soco, "SoCo", Fake)
    assert core.ask_any(lambda sp: sp.visible_zones) == ["zone"]
    assert calls == ["192.0.2.1", "192.0.2.2"]


def test_monitor_keeps_a_speaker_that_stops_answering(monkeypatch):
    desc = {"192.0.2.5": "<roomName>Patio</roomName><modelNumber>S1</modelNumber>RINCON_AAAAAAAAAA0101400"}

    def fake_get(ip, path, timeout=3):
        if ip not in desc:
            raise OSError("no route to host")
        return desc[ip]
    monkeypatch.setattr(netwatch, "_get", fake_get)
    monkeypatch.setattr(core, "scan", lambda: ["192.0.2.5"])         # the Den is off the network now
    known = {"192.0.2.6": {"name": "Den·6", "model": "S2", "mac5": "BBBBBBBBBB"}}
    out = netwatch.speakers(known)
    assert out["192.0.2.5"]["name"] == "Patio·5"
    assert out["192.0.2.6"]["name"] == "Den·6"                      # still watched, will show as lost
