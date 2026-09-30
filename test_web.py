#!/usr/bin/env python3
"""Tests for rt21_web.py — protocol, link durability, and the HTTP API.

Runs entirely against the built-in simulator; no hardware, no browser,
no third-party packages.

    python3 test_web.py
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import rt21_web as app
from rt21_web import (
    AppContext,
    Config,
    Handler,
    HamlibListener,
    Hub,
    LinkManager,
    LinkState,
    ListenerSet,
    MotionDirector,
    N1mmBridge,
    PstRotatorListener,
    Protocol,
    RotatorLink,
    Rt21Simulator,
    SOH,
)


class GheSimulator:
    """Emulates the GH Everywhere ezWebLynx HTTP-serial bridge in front of a
    simulated RT-21: SERIAL_STRING sends a command, data.htm returns the last
    buffered reply frame exactly as the real box formats it."""

    def __init__(self, speed: float = 400.0) -> None:
        import time as _time
        from http.server import BaseHTTPRequestHandler
        from urllib.parse import parse_qs, urlparse, unquote_plus

        sim = self
        self._speed = speed
        self._heading = 0.0
        self._target = 0.0
        self._last = _time.monotonic()
        self._buf = ""

        class GheHandler(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(parsed.query,
                          keep_blank_values=True).items()}
                sim._advance()
                if parsed.path == "/data.htm":
                    body = ("<END>\nserial_get:" + sim._buf +
                            "<END>\nid:RT-21<END>\n\n").encode("latin-1")
                elif parsed.path == "/blank.html":
                    if "SERIAL_STRING" in params:
                        sim._command(unquote_plus(params["SERIAL_STRING"]))
                    body = b"\x00"
                else:
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), GheHandler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever,
                         kwargs={"poll_interval": 0.2}, daemon=True).start()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _advance(self) -> None:
        now = time.monotonic()
        elapsed, self._last = now - self._last, now
        delta = app._shortest_delta(self._heading, self._target)
        if abs(delta) < 0.5:
            self._heading = self._target
            return
        step = min(abs(delta), self._speed * elapsed)
        self._heading = (self._heading + step * (1 if delta > 0 else -1)) % 360.0

    def _command(self, frame: str) -> None:
        frame = frame.strip("\r\n ;")
        moving = abs(app._shortest_delta(self._heading, self._target)) >= 0.5
        upper = frame.upper()
        if frame == "":
            self._target = self._heading
        elif upper.startswith("R2"):
            self._buf = f"{SOH}0{chr(1 if moving else 0)}> {self._heading:.1f};"
        elif upper.startswith("R1"):
            self._buf = f"{SOH}RT-21 4.13.2 (simulated);"
        elif upper.startswith("ST"):
            self._target = self._heading
        elif upper.startswith("AP"):
            digits = "".join(ch for ch in upper[3:] if ch.isdigit())
            if digits:
                self._target = float(int(digits) % 360)
        elif upper.startswith("AA"):
            self._target = (self._heading - 9) % 360
        elif upper.startswith("AB"):
            self._target = (self._heading + 9) % 360


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Protocol(unit=1)

    def test_encoding_matches_appendix_f(self) -> None:
        self.assertEqual(self.p.read_heading(), "AI1;")
        self.assertEqual(self.p.read_status(), "R21;")
        self.assertEqual(self.p.read_version(), "R11;")
        self.assertEqual(self.p.goto(45), "AP1045\r;")
        self.assertEqual(self.p.goto(359), "AP1359\r;")
        self.assertEqual(self.p.set_target(7), "AP1007;")
        self.assertEqual(self.p.move_to_target(), "AM1;")
        self.assertEqual(self.p.stop(), [";", "ST1;"])
        self.assertEqual(self.p.jog_ccw(), "AA1;")
        self.assertEqual(self.p.jog_cw(), "AB1;")

    def test_unit_digit_is_clamped_and_used(self) -> None:
        self.assertEqual(Protocol(unit=2).goto(100), "AP2100\r;")
        self.assertEqual(Protocol(unit=99).unit, 9)
        self.assertEqual(Protocol(unit=-3).unit, 0)

    def test_decode_bare_heading(self) -> None:
        r = Protocol.decode("245")
        self.assertEqual(r.kind, "heading")
        self.assertEqual(r.heading, 245.0)

    def test_decode_soh_status(self) -> None:
        r = Protocol.decode(f"{SOH}245 1")
        self.assertEqual(r.kind, "heading_status")
        self.assertEqual(r.heading, 245.0)
        self.assertTrue(r.moving)
        r = Protocol.decode(f"{SOH}003 2")
        self.assertFalse(r.moving)

    def test_decode_status_without_soh_is_not_status(self) -> None:
        self.assertNotEqual(Protocol.decode("245 1").kind, "heading_status")

    def test_decode_info(self) -> None:
        r = Protocol.decode(f"{SOH}RT-21 Version 4.13")
        self.assertEqual(r.kind, "info")
        self.assertIn("4.13", r.text)

    def test_decode_garbage(self) -> None:
        self.assertEqual(Protocol.decode("").kind, "unknown")
        self.assertEqual(Protocol.decode("999999").kind, "unknown")


class GheDecodeTests(unittest.TestCase):
    def test_idle_frame(self) -> None:
        r = Protocol.decode_ghe(f"{SOH}0\x00> 0.5;")
        self.assertEqual(r.kind, "heading_status")
        self.assertEqual(r.heading, 0.5)
        self.assertFalse(r.moving)

    def test_busy_frame(self) -> None:
        r = Protocol.decode_ghe(f"{SOH}0\x01> 123.4;")
        self.assertEqual(r.kind, "heading_status")
        self.assertEqual(r.heading, 123.4)
        self.assertTrue(r.moving)

    def test_printable_status_digit(self) -> None:
        r = Protocol.decode_ghe(f"{SOH}01> 45.0;")
        self.assertTrue(r.moving)

    def test_version_frame_falls_through(self) -> None:
        r = Protocol.decode_ghe(f"{SOH}RT-21 4.13.2;")
        self.assertEqual(r.kind, "info")
        self.assertIn("4.13.2", r.text)

    def test_garbage(self) -> None:
        self.assertEqual(Protocol.decode_ghe(f"{SOH}0\x00> garbage;").kind, "unknown")
        self.assertEqual(Protocol.decode_ghe("").kind, "unknown")


# --------------------------------------------------------------------------- #
class GheLinkTests(unittest.TestCase):
    """Auto-detection and full control loop against the GHE bridge simulator."""

    def setUp(self) -> None:
        self.sim = GheSimulator(speed=400.0)
        self.cfg = Config(host="127.0.0.1", port=self.sim.port,
                          transport="auto", poll_interval=1.0, stale_timeout=6.0)
        self.cfg.sanitize()
        self.hub = Hub()
        self.link = RotatorLink(self.cfg, self.hub)

    def tearDown(self) -> None:
        self.link.shutdown()
        self.sim.shutdown()

    def test_autodetect_poll_slew_stop(self) -> None:
        self.link.start()
        self.assertTrue(wait_for(lambda: self.link.connected, timeout=8.0),
                        "never connected via GHE")
        self.assertIn("GH Everywhere", self.hub.state["detail"])
        self.assertTrue(wait_for(lambda: "4.13.2" in self.hub.state["version"],
                                 timeout=8.0), "version never arrived")
        self.assertTrue(wait_for(lambda: self.hub.state["heading"] is not None,
                                 timeout=8.0), "no heading arrived")
        proto = self.link.protocol
        self.link.submit([proto.goto(200), proto.move_to_target()])
        self.assertTrue(wait_for(lambda: self.sim._target == 200.0, timeout=6.0),
                        "AP command never reached the simulator")
        self.assertTrue(
            wait_for(lambda: (self.hub.state["heading"] or 0) > 100, timeout=10.0),
            "rotator never moved toward 200",
        )
        self.link.submit(proto.stop())
        self.assertTrue(
            wait_for(lambda: self.sim._target == self.sim._heading, timeout=6.0),
            "stop never reached the simulator",
        )

    def test_mute_serial_side_goes_stale(self) -> None:
        """A bridge whose serial side is dead (unplugged USB) answers HTTP but
        never buffers a reply. The link must report the fault within the stale
        timeout instead of sitting at 'connected' with an empty compass."""
        self.sim._command = lambda frame: None
        self.link.start()
        self.assertTrue(wait_for(lambda: self.link.connected, timeout=8.0),
                        "never connected via GHE")
        self.assertTrue(
            wait_for(lambda: self.hub.state["link"] != "connected", timeout=15.0),
            "dead serial side never made the link leave 'connected'",
        )
        self.assertIsNone(self.hub.state["heading"])


# --------------------------------------------------------------------------- #
class ConfigTests(unittest.TestCase):
    def test_sanitize_clamps(self) -> None:
        cfg = Config(port=99999, unit=42, poll_interval=0.0, max_heading=100)
        cfg.sanitize()
        self.assertEqual(cfg.port, 65535)
        self.assertEqual(cfg.unit, 9)
        self.assertEqual(cfg.poll_interval, 0.2)
        self.assertEqual(cfg.max_heading, 359)

    def test_sanitize_presets(self) -> None:
        cfg = Config(presets=[{"name": "OK", "heading": 400},
                              {"junk": True}, "not-a-dict",
                              {"name": "", "heading": 10}])
        cfg.sanitize()
        self.assertEqual(cfg.presets, [{"name": "OK", "heading": 359}])

    def test_sanitize_bad_host(self) -> None:
        cfg = Config(host="  ")
        cfg.sanitize()
        self.assertTrue(cfg.host)


# --------------------------------------------------------------------------- #
class LinkTests(unittest.TestCase):
    """The worker thread against the simulator: connect, poll, slew, stop."""

    def setUp(self) -> None:
        self.sim = Rt21Simulator(speed=400.0)
        self.sim.start()
        self.cfg = Config(host="127.0.0.1", port=self.sim.port,
                          poll_interval=0.2, stale_timeout=3.0)
        self.cfg.sanitize()
        self.hub = Hub()
        self.link = RotatorLink(self.cfg, self.hub)

    def tearDown(self) -> None:
        self.link.shutdown()
        self.sim.shutdown()

    def test_connect_poll_slew_stop(self) -> None:
        self.link.start()
        self.assertTrue(wait_for(lambda: self.link.connected), "never connected")
        self.assertTrue(wait_for(lambda: self.hub.state["heading"] is not None),
                        "no heading arrived")
        self.link.submit(self.link.protocol.goto(200))
        self.assertTrue(
            wait_for(lambda: (self.hub.state["heading"] or 0) > 100, timeout=6.0),
            "rotator never moved toward 200",
        )
        self.link.submit(self.link.protocol.stop())
        time.sleep(0.6)
        stopped_at = self.hub.state["heading"]
        time.sleep(0.8)
        self.assertAlmostEqual(self.hub.state["heading"], stopped_at, delta=2.0)

    def test_reconnects_after_drop(self) -> None:
        self.link.start()
        self.assertTrue(wait_for(lambda: self.link.connected))
        # kill every client socket server-side; the link must heal itself
        for client in list(self.sim._clients):
            try:
                client.shutdown(2)
            except OSError:
                pass
        self.assertTrue(
            wait_for(lambda: not self.link.connected, timeout=5.0),
            "link never noticed the drop",
        )
        self.assertTrue(
            wait_for(lambda: self.link.connected, timeout=10.0),
            "link never reconnected",
        )

    def test_clean_shutdown(self) -> None:
        self.link.start()
        self.assertTrue(wait_for(lambda: self.link.connected))
        self.link.shutdown()
        self.assertFalse(self.link.is_alive())
        self.assertEqual(self.link.state, LinkState.DISCONNECTED)


# --------------------------------------------------------------------------- #
class HttpApiTests(unittest.TestCase):
    """The full stack: simulator <- link <- hub <- HTTP API."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sim = Rt21Simulator(speed=400.0)
        cls.sim.start()
        cls.cfg = Config(host="127.0.0.1", port=cls.sim.port, poll_interval=0.2)
        cls.cfg.sanitize()
        cls.cfg.save = lambda *a, **k: None  # type: ignore[assignment] # don't touch the real file
        cls.hub = Hub()
        cls.manager = LinkManager(cls.cfg, cls.hub)
        Handler.ctx = AppContext(cls.cfg, cls.hub, cls.manager)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever,
                         kwargs={"poll_interval": 0.2}, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.manager.disconnect()
        cls.sim.shutdown()

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read().decode())

    def _post(self, path: str, body: dict | None = None, origin: str | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, method="POST",
            data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_01_index_serves_html(self) -> None:
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            html = r.read().decode()
        self.assertIn("RT-21 Rotator", html)
        self.assertIn("EventSource", html)

    def test_02_connect_and_state(self) -> None:
        code, _ = self._post("/api/connect")
        self.assertEqual(code, 200)
        self.assertTrue(wait_for(lambda: self._get("/api/state")["link"] == "connected"))
        self.assertTrue(wait_for(lambda: self._get("/api/state")["heading"] is not None))

    def test_03_goto_and_stop(self) -> None:
        code, data = self._post("/api/goto", {"heading": 90})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(wait_for(lambda: (self._get("/api/state")["heading"] or 0) > 30,
                                 timeout=6.0))
        code, data = self._post("/api/stop")
        self.assertEqual(code, 200)

    def test_04_goto_rejects_bad_heading(self) -> None:
        for bad in (-1, 999, "abc", None):
            code, _ = self._post("/api/goto", {"heading": bad})
            self.assertEqual(code, 400, f"accepted {bad!r}")

    def test_05_jog_validation(self) -> None:
        code, _ = self._post("/api/jog", {"dir": "sideways"})
        self.assertEqual(code, 400)
        code, _ = self._post("/api/jog", {"dir": "cw"})
        self.assertEqual(code, 200)
        self._post("/api/stop")

    def test_06_cross_origin_rejected(self) -> None:
        code, _ = self._post("/api/stop", origin="http://evil.example")
        self.assertEqual(code, 403)
        code, _ = self._post("/api/stop", origin=self.base)
        self.assertEqual(code, 200)

    def test_07_config_roundtrip(self) -> None:
        code, data = self._post("/api/config", {"presets": [{"name": "Test", "heading": 123}],
                                                "max_heading": 450})
        self.assertEqual(code, 200)
        state = self._get("/api/state")
        self.assertEqual(state["config"]["presets"], [{"name": "Test", "heading": 123}])
        self.assertEqual(state["config"]["max_heading"], 450)

    def test_08_sse_hello(self) -> None:
        req = urllib.request.Request(self.base + "/events")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.headers["Content-Type"], "text/event-stream")
            line = r.readline().decode()
            self.assertEqual(line.strip(), "event: hello")
            data = r.readline().decode()
            self.assertTrue(data.startswith("data: "))
            snap = json.loads(data[6:])
            self.assertIn("config", snap)
            self.assertIn("link", snap)

    def test_09_disconnect(self) -> None:
        code, _ = self._post("/api/disconnect")
        self.assertEqual(code, 200)
        self.assertTrue(wait_for(lambda: self._get("/api/state")["link"] == "disconnected"))
        code, _ = self._post("/api/goto", {"heading": 10})
        self.assertEqual(code, 409)  # not connected


# --------------------------------------------------------------------------- #
class WatchdogTests(unittest.TestCase):
    def test_stale_link_forces_reconnect(self) -> None:
        """A server that accepts but never answers must trip the watchdog."""
        import socket as s

        silent = s.socket(s.AF_INET, s.SOCK_STREAM)
        silent.setsockopt(s.SOL_SOCKET, s.SO_REUSEADDR, 1)
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        port = silent.getsockname()[1]
        accepted = []
        stop_accepting = threading.Event()

        def accept_loop() -> None:
            while not stop_accepting.is_set():
                try:
                    conn, _ = silent.accept()
                    accepted.append(conn)
                except OSError:
                    return

        threading.Thread(target=accept_loop, daemon=True).start()

        cfg = Config(host="127.0.0.1", port=port, stale_timeout=2.0,
                     poll_interval=0.3, reconnect_max_delay=2.0)
        cfg.sanitize()
        hub = Hub()
        states: list[str] = []
        original_publish = hub.publish

        def spy(event: str, data: dict) -> None:
            if event == "state":
                states.append(data["state"])
            original_publish(event, data)

        hub.publish = spy  # type: ignore[assignment]
        link = RotatorLink(cfg, hub)
        link.start()
        try:
            self.assertTrue(
                wait_for(lambda: states.count(LinkState.CONNECTED) >= 2, timeout=15.0),
                f"watchdog never forced a reconnect; states: {states}",
            )
            self.assertIn(LinkState.ERROR, states)
        finally:
            link.shutdown()
            stop_accepting.set()
            silent.close()
            for conn in accepted:
                try:
                    conn.close()
                except OSError:
                    pass


# --------------------------------------------------------------------------- #
class N1mmBridgeTests(unittest.TestCase):
    """The N1MM UDP bridge against the simulator: turn, stop, heading report."""

    # What N1MM Logger+ actually broadcasts on port 12040 (Alt+J / stop).
    TURN = ("<N1MMRotor><rotor>teststack</rotor>"
            "<goazi>45.0</goazi><offset>0.0</offset>"
            "<bidirectional>0</bidirectional><freqband>14.0</freqband>"
            "</N1MMRotor>")
    STOP = ("<N1MMRotor><stop><rotor>teststack</rotor>"
            "<freqband>14.0</freqband></stop></N1MMRotor>")

    def setUp(self) -> None:
        self.sim = Rt21Simulator(speed=400.0)
        self.sim.start()
        self.cfg = Config(host="127.0.0.1", port=self.sim.port,
                          poll_interval=0.2, stale_timeout=3.0,
                          n1mm_enabled=True, n1mm_port=0,  # 0 = ephemeral port
                          n1mm_bind="127.0.0.1")
        self.cfg.transport = "tcp"
        self.hub = Hub()
        self.manager = LinkManager(self.cfg, self.hub)
        # A plain UDP socket plays the part of N1MM; the bridge's heading
        # reports are steered to this socket's own port instead of 13010.
        self.n1mm = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.n1mm.bind(("127.0.0.1", 0))
        self.n1mm.settimeout(6.0)
        self.bridge = N1mmBridge(self.cfg, self.hub, self.manager,
                                 feedback_port=self.n1mm.getsockname()[1])
        self.bridge.start()

    def tearDown(self) -> None:
        self.bridge.shutdown()
        self.manager.disconnect()
        self.sim.shutdown()
        self.n1mm.close()

    def _send(self, xml: str) -> None:
        self.n1mm.sendto(xml.encode(), ("127.0.0.1", self.bridge.port))

    def test_turn_stop_and_heading_report(self) -> None:
        self.manager.connect()
        self.assertTrue(wait_for(lambda: self.manager.link.connected),
                        "never connected")
        self._send(self.TURN)
        self.assertTrue(wait_for(lambda: self.hub.state["target"] == 45),
                        "goazi packet never became a target")
        self.assertTrue(
            wait_for(lambda: (self.hub.state["heading"] or 0) > 20, timeout=6.0),
            "rotator never moved toward N1MM's heading",
        )
        reply, _ = self.n1mm.recvfrom(256)
        name, _, tenths = reply.decode().partition(" @ ")
        self.assertEqual(name, "teststack")
        self.assertTrue(tenths.isdigit(), f"bad heading report: {reply!r}")
        self._send(self.STOP)
        self.assertTrue(wait_for(lambda: self.hub.state["target"] is None),
                        "stop packet never cleared the target")

    def test_offset_and_comma_decimals_apply(self) -> None:
        self.manager.connect()
        self.assertTrue(wait_for(lambda: self.manager.link.connected))
        packet = self.TURN.replace("45.0", "350,0").replace(">0.0</offset>",
                                                            ">20,0</offset>")
        self._send(packet)
        self.assertTrue(wait_for(lambda: self.hub.state["target"] == 10),
                        "350 + 20 offset should wrap to a 010 target")

    def test_garbage_and_disconnected_are_harmless(self) -> None:
        # No rotator connected: packets must be ignored, not crash the thread
        self._send("not xml at all \x00\xff")
        self._send(self.TURN)
        time.sleep(0.3)
        self.assertIsNone(self.hub.state["target"])
        self.assertTrue(self.bridge.is_alive())


# --------------------------------------------------------------------------- #
class N1mmParseTests(unittest.TestCase):
    """The pure N1MM packet parser, against real packet shapes."""

    def test_real_turn_packet(self) -> None:
        p = N1mmBridge.parse(N1mmBridgeTests.TURN)
        self.assertEqual(p["name"], "teststack")
        self.assertEqual(p["azimuth"], 45.0)
        self.assertFalse(p["stop"])
        self.assertFalse(p["bidirectional"])

    def test_real_stop_packet_carries_name(self) -> None:
        p = N1mmBridge.parse(N1mmBridgeTests.STOP)
        self.assertTrue(p["stop"])
        self.assertEqual(p["name"], "teststack")

    def test_legacy_rotorname_tag_still_works(self) -> None:
        p = N1mmBridge.parse("<N1MMRotor><rotor><rotorname>old</rotorname>"
                             "<goazi>10</goazi></rotor></N1MMRotor>")
        self.assertEqual(p["name"], "old")
        self.assertEqual(p["azimuth"], 10.0)

    def test_bidirectional_flag(self) -> None:
        p = N1mmBridge.parse(N1mmBridgeTests.TURN.replace(
            "<bidirectional>0", "<bidirectional>1"))
        self.assertTrue(p["bidirectional"])


# --------------------------------------------------------------------------- #
class FakeManager:
    """Stands in for LinkManager: always connected, no link thread."""

    def __init__(self, cfg: Config, hub: Hub) -> None:
        self.connected = True
        self.director = MotionDirector(cfg, hub, self)


class MotionDirectorTests(unittest.TestCase):
    """Latest-wins arbitration, without a link thread or a socket."""

    def setUp(self) -> None:
        self.cfg = Config(retarget_min_interval=0.0)
        self.hub = Hub()
        self.mgr = FakeManager(self.cfg, self.hub)
        self.d = self.mgr.director
        self.p = Protocol(1)

    def test_latest_goto_wins(self) -> None:
        self.d.goto(90, "hamlib")
        self.d.goto(180, "web")
        self.d.goto(270, "n1mm")
        self.assertEqual(self.d.take(self.p, False), ["AP1270\r;", "AM1;"])
        self.assertEqual(self.d.take(self.p, False), [])
        self.assertEqual(self.hub.state["target"], 270)
        self.assertEqual(self.hub.state["target_source"], "n1mm")

    def test_stop_replaces_pending_goto(self) -> None:
        self.d.goto(90, "hamlib")
        self.d.stop("pst")
        self.assertEqual(self.d.take(self.p, True), [";", "ST1;"])
        self.assertEqual(self.d.take(self.p, True), [], "the goto must never be sent")
        self.assertIsNone(self.hub.state["target"])

    def test_goto_after_stop_replaces_stop(self) -> None:
        self.d.stop("web")
        self.d.goto(45, "web")
        self.assertEqual(self.d.take(self.p, False), ["AP1045\r;", "AM1;"])

    def test_duplicate_of_active_target_is_dropped(self) -> None:
        self.d.goto(120, "hamlib")
        self.d.take(self.p, False)
        self.d.goto(120, "hamlib")                 # e.g. a tracker re-sending
        self.assertEqual(self.d.take(self.p, True), [])

    def test_min_interval_delays_but_keeps_latest(self) -> None:
        self.cfg.retarget_min_interval = 0.3
        self.d.goto(10, "web")
        self.assertTrue(self.d.take(self.p, False))
        self.d.goto(20, "web")
        self.assertEqual(self.d.take(self.p, True), [], "too soon after the last goto")
        time.sleep(0.35)
        self.assertEqual(self.d.take(self.p, True), ["AP1020\r;", "AM1;"])

    def test_stop_is_never_rate_limited(self) -> None:
        self.cfg.retarget_min_interval = 5.0
        self.d.goto(10, "web")
        self.d.take(self.p, False)
        self.d.stop("web")
        self.assertEqual(self.d.take(self.p, True), [";", "ST1;"])

    def test_stop_first_mode(self) -> None:
        self.cfg.retarget_mode = "stop_first"
        self.cfg.retarget_settle_ms = 200
        self.d.goto(10, "web")
        self.d.take(self.p, False)
        self.d.goto(200, "n1mm")
        self.assertEqual(self.d.take(self.p, True), [";", "ST1;"])
        self.assertEqual(self.d.take(self.p, True), [], "still settling")
        time.sleep(0.25)
        self.assertEqual(self.d.take(self.p, False), ["AP1200\r;", "AM1;"])

    def test_direct_mode_retargets_while_moving(self) -> None:
        self.d.goto(10, "web")
        self.d.take(self.p, False)
        self.d.goto(200, "n1mm")
        self.assertEqual(self.d.take(self.p, True), ["AP1200\r;", "AM1;"])

    def test_overlap_heading_goes_out_raw(self) -> None:
        self.d.goto(400, "web")
        self.assertEqual(self.d.take(self.p, False)[0], "AP1400\r;")
        self.assertEqual(self.hub.state["target"], 40)

    def test_disconnected_refuses(self) -> None:
        self.mgr.connected = False
        self.assertFalse(self.d.goto(10, "web"))
        self.assertFalse(self.d.stop("web"))
        self.assertEqual(self.d.take(self.p, False), [])

    def test_park(self) -> None:
        self.assertIsNone(self.d.park("hamlib"), "park is off until configured")
        self.cfg.park_heading = 180
        self.assertTrue(self.d.park("hamlib"))
        self.assertEqual(self.d.take(self.p, False)[0], "AP1180\r;")

    def test_jog(self) -> None:
        self.d.goto(10, "web")
        self.d.jog("cw", "hamlib")
        self.assertEqual(self.d.take(self.p, False), ["AB1;"])
        self.assertFalse(self.d.jog("up", "hamlib"))

    def test_discard_forgets_pending(self) -> None:
        self.d.goto(10, "web")
        self.d.discard()
        self.assertEqual(self.d.take(self.p, False), [])


# --------------------------------------------------------------------------- #
class RecordingDirector:
    """Records what a listener asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.ok = True
        self.park_result: "bool | None" = None

    def goto(self, heading, source):
        self.calls.append(("goto", heading, source)); return self.ok

    def stop(self, source):
        self.calls.append(("stop", source)); return self.ok

    def jog(self, direction, source):
        self.calls.append(("jog", direction, source)); return self.ok

    def park(self, source):
        self.calls.append(("park", source)); return self.park_result


class StubManager:
    def __init__(self) -> None:
        self.connected = True
        self.director = RecordingDirector()


class HamlibProtocolTests(unittest.TestCase):
    """rotctld command lines -> director calls and wire replies."""

    def setUp(self) -> None:
        self.cfg = Config(hamlib_bind="127.0.0.1", hamlib_port=0)
        self.hub = Hub()
        self.mgr = StubManager()
        self.h = HamlibListener(self.cfg, self.hub, self.mgr)
        self.calls = self.mgr.director.calls

    def tearDown(self) -> None:
        self.h.shutdown()

    def line(self, text: str) -> str:
        return self.h.handle_line(text)[0]

    def test_set_pos(self) -> None:
        self.assertEqual(self.line("P 180.5 0"), "RPRT 0\n")
        self.assertEqual(self.calls[-1], ("goto", 180, "hamlib"))
        self.assertEqual(self.line("\\set_pos 90 45"), "RPRT 0\n")
        self.assertEqual(self.calls[-1], ("goto", 90, "hamlib"))

    def test_set_pos_negative_and_360(self) -> None:
        self.line("P -90 0")
        self.assertEqual(self.calls[-1], ("goto", 270, "hamlib"))
        self.line("P 360 0")
        self.assertEqual(self.calls[-1], ("goto", 0, "hamlib"))

    def test_set_pos_bad_input(self) -> None:
        for bad in ("P", "P abc 0", "P 500 0", "P nan 0", "P -200 0"):
            self.assertEqual(self.line(bad), "RPRT -1\n", bad)
        self.assertEqual(self.calls, [])

    def test_set_pos_when_disconnected(self) -> None:
        self.mgr.director.ok = False
        self.assertEqual(self.line("P 10 0"), "RPRT -6\n")

    def test_get_pos(self) -> None:
        self.assertEqual(self.line("p"), "RPRT -6\n", "no heading yet")
        self.hub.publish("heading", {"deg": 123.4})
        self.assertEqual(self.line("p"), "123.40\n0.00\n")
        self.assertEqual(self.line("\\get_pos"), "123.40\n0.00\n")

    def test_extended_response(self) -> None:
        self.hub.publish("heading", {"deg": 45.0})
        self.assertEqual(self.line("+p"),
                         "get_pos:\nAzimuth: 45.00\nElevation: 0.00\nRPRT 0\n")
        self.assertEqual(self.line("+P 10 0"), "set_pos: 10 0\nRPRT 0\n")
        self.assertEqual(self.line(";p"), "get_pos:;Azimuth: 45.00;Elevation: 0.00;RPRT 0\n")

    def test_stop_park_move(self) -> None:
        self.assertEqual(self.line("S"), "RPRT 0\n")
        self.assertEqual(self.calls[-1], ("stop", "hamlib"))
        self.assertEqual(self.line("K"), "RPRT -11\n", "park not configured")
        self.mgr.director.park_result = True
        self.assertEqual(self.line("K"), "RPRT 0\n")
        self.assertEqual(self.line("M 8 50"), "RPRT 0\n")
        self.assertEqual(self.calls[-1], ("jog", "ccw", "hamlib"))
        self.assertEqual(self.line("M CW 50"), "RPRT 0\n")
        self.assertEqual(self.calls[-1], ("jog", "cw", "hamlib"))
        self.assertEqual(self.line("M 2 50"), "RPRT -4\n", "elevation moves")

    def test_dump_state_matches_netrotctl(self) -> None:
        lines = self.line("\\dump_state").splitlines()
        self.assertEqual(lines[0], "1")                 # protocol version
        self.assertIn("min_az=0.000000", lines)
        self.assertIn("max_az=360.000000", lines)
        self.assertIn("rot_type=Az", lines)
        self.assertEqual(lines[-1], "done")

    def test_info_quit_unknown_blank(self) -> None:
        self.assertIn(rt21_web_app_name(), self.line("_"))
        self.assertEqual(self.h.handle_line("q"), ("", True))
        self.assertEqual(self.line("Z"), "RPRT -4\n")
        self.assertEqual(self.line("\\nonsense"), "RPRT -4\n")
        self.assertEqual(self.line("   "), "")


def rt21_web_app_name() -> str:
    return app.APP_NAME


class PstParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(pst_bind="127.0.0.1", pst_port=0)
        self.hub = Hub()
        self.mgr = StubManager()
        self.pst = PstRotatorListener(self.cfg, self.hub, self.mgr)
        self.calls = self.mgr.director.calls

    def tearDown(self) -> None:
        self.pst.shutdown()

    def test_parse(self) -> None:
        self.assertEqual(PstRotatorListener.parse("<PST><AZIMUTH>85</AZIMUTH></PST>"),
                         [("AZIMUTH", "85")])
        self.assertEqual(PstRotatorListener.parse("<PST>AZ?</PST>"), [("QUERY", "AZ")])
        self.assertEqual(PstRotatorListener.parse("<PST>TGA?</PST>"), [("QUERY", "TGA")])
        self.assertEqual(PstRotatorListener.parse("garbage"), [])

    def test_azimuth_stop_park(self) -> None:
        self.pst.handle("<PST><AZIMUTH>85.4</AZIMUTH></PST>")
        self.assertEqual(self.calls[-1], ("goto", 85, "pst"))
        self.pst.handle("<PST><STOP>1</STOP></PST>")
        self.assertEqual(self.calls[-1], ("stop", "pst"))
        self.pst.handle("<PST><PARK>1</PARK></PST>")
        self.assertEqual(self.calls[-1], ("park", "pst"))

    def test_stop_wins_in_same_packet(self) -> None:
        self.pst.handle("<PST><AZIMUTH>85</AZIMUTH><STOP>1</STOP></PST>")
        self.assertEqual(self.calls, [("stop", "pst")])

    def test_bad_azimuth_ignored(self) -> None:
        self.pst.handle("<PST><AZIMUTH>abc</AZIMUTH></PST>")
        self.pst.handle("<PST><AZIMUTH>999</AZIMUTH></PST>")
        self.assertEqual(self.calls, [])

    def test_queries(self) -> None:
        self.assertEqual(self.pst.handle("<PST>AZ?</PST>"), [], "no heading yet")
        self.hub.publish("heading", {"deg": 84.6})
        self.assertEqual(self.pst.handle("<PST>AZ?</PST>"), ["AZ:85\r"])
        self.assertEqual(self.pst.handle("<PST>TGA?</PST>"), ["TGA:85\r"])
        self.hub.publish("target", {"deg": 200, "source": "web"})
        self.assertEqual(self.pst.handle("<PST>TGA?</PST>"), ["TGA:200\r"])


# --------------------------------------------------------------------------- #
class ListenerIntegrationTests(unittest.TestCase):
    """Real sockets, the real director and link, the simulator at a
    realistic speed so a move is still in progress when it is overridden."""

    def setUp(self) -> None:
        self.sim = Rt21Simulator(speed=60.0)
        self.sim.start()
        self.cfg = Config(host="127.0.0.1", port=self.sim.port,
                          poll_interval=0.2, stale_timeout=3.0,
                          n1mm_enabled=True, n1mm_bind="127.0.0.1", n1mm_port=0,
                          hamlib_enabled=True, hamlib_bind="127.0.0.1", hamlib_port=0,
                          pst_enabled=True, pst_bind="127.0.0.1", pst_port=0,
                          hamlib_max_clients=2)
        self.cfg.transport = "tcp"
        self.hub = Hub()
        self.manager = LinkManager(self.cfg, self.hub)
        self.listeners = ListenerSet(self.cfg, self.hub, self.manager)
        self.assertEqual(self.listeners.apply(), {})
        self.manager.connect()
        self.assertTrue(wait_for(lambda: self.manager.connected), "never connected")
        self.assertTrue(wait_for(lambda: self.hub.get("heading") is not None))
        self.sockets: list[socket.socket] = []

    def tearDown(self) -> None:
        for s in self.sockets:
            s.close()
        self.listeners.shutdown()
        self.manager.disconnect()
        self.sim.shutdown()

    def hamlib(self) -> socket.socket:
        port = self.listeners.get("hamlib").port
        s = socket.create_connection(("127.0.0.1", port), timeout=3.0)
        self.sockets.append(s)
        return s

    @staticmethod
    def ask(s: socket.socket, line: str, lines: int = 1) -> str:
        s.sendall(line.encode() + b"\n")
        data = b""
        while data.count(b"\n") < lines:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        return data.decode()

    def udp(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.settimeout(3.0)
        self.sockets.append(s)
        return s

    def test_override_hamlib_then_n1mm(self) -> None:
        c = self.hamlib()
        self.assertEqual(self.ask(c, "P 90 0"), "RPRT 0\n")
        self.assertTrue(wait_for(lambda: self.hub.get("moving") is True
                                 or (self.hub.get("heading") or 0) > 5))
        n1mm = self.udp()
        n1mm.sendto(N1mmBridgeTests.TURN.replace("45.0", "270.0").encode(),
                    ("127.0.0.1", self.listeners.get("n1mm").port))
        self.assertTrue(wait_for(lambda: self.hub.get("target") == 270))
        self.assertEqual(self.hub.get("target_source"), "n1mm")
        self.assertTrue(
            wait_for(lambda: abs(app._shortest_delta(self.hub.get("heading"), 270)) < 2,
                     timeout=10.0), f"ended at {self.hub.get('heading')}")

    def test_burst_is_coalesced(self) -> None:
        q = self.hub.subscribe()
        n1mm = self.udp()
        port = self.listeners.get("n1mm").port
        for i in range(20):
            n1mm.sendto(N1mmBridgeTests.TURN.replace("45.0", f"{100 + i}.0").encode(),
                        ("127.0.0.1", port))
            time.sleep(0.005)
        self.assertTrue(wait_for(lambda: self.hub.get("target") == 119))
        time.sleep(0.8)
        sent = []
        while not q.empty():
            event, data = q.get_nowait()
            if event == "traffic" and data["dir"] == "tx" and data["data"].startswith("AP"):
                sent.append(data["data"])
        self.hub.unsubscribe(q)
        self.assertLessEqual(len(sent), 3, sent)
        self.assertEqual(sent[-1], "AP1119<CR>;")

    def test_hamlib_session_and_client_cap(self) -> None:
        c = self.hamlib()
        dump = self.ask(c, "\\dump_state", lines=9)
        self.assertTrue(dump.endswith("done\n"), dump)
        pos = self.ask(c, "p", lines=2).split("\n")
        self.assertEqual(pos[1], "0.00")
        float(pos[0])
        self.assertEqual(self.ask(c, "S"), "RPRT 0\n")
        self.hamlib()
        self.assertTrue(wait_for(lambda: self.listeners.get("hamlib").client_count == 2))
        third = self.hamlib()
        self.assertEqual(third.recv(64), b"", "third client should be refused")
        self.assertEqual(self.hub.get("listeners")["hamlib"]["clients"], 2)

    def test_hamlib_overlong_line_disconnects(self) -> None:
        c = self.hamlib()
        c.sendall(b"P" * 5000)
        try:
            self.assertEqual(c.recv(64), b"")
        except ConnectionResetError:
            pass                            # closed with unread data -> RST
        self.assertTrue(self.listeners.get("hamlib").is_alive())

    def test_hamlib_client_drops_mid_line(self) -> None:
        c = self.hamlib()
        c.sendall(b"P 12")
        c.close()
        self.assertTrue(wait_for(lambda: self.listeners.get("hamlib").client_count == 0))
        self.assertEqual(self.ask(self.hamlib(), "S"), "RPRT 0\n")

    def test_pst_turn_and_reply_on_port_plus_one(self) -> None:
        pst = self.listeners.get("pst")
        reply_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            reply_sock.bind(("127.0.0.1", pst.port + 1))
        except OSError:
            reply_sock.close()
            self.skipTest("port + 1 is taken on this machine")
        reply_sock.settimeout(3.0)
        self.sockets.append(reply_sock)
        sender = self.udp()
        sender.sendto(b"<PST><AZIMUTH>30</AZIMUTH></PST>", ("127.0.0.1", pst.port))
        self.assertTrue(wait_for(lambda: self.hub.get("target") == 30))
        self.assertEqual(self.hub.get("target_source"), "pst")
        sender.sendto(b"<PST>AZ?</PST>", ("127.0.0.1", pst.port))
        data, _ = reply_sock.recvfrom(64)
        self.assertTrue(data.startswith(b"AZ:") and data.endswith(b"\r"), data)

    def test_listener_set_reconfigures_and_reports_port_in_use(self) -> None:
        self.cfg.pst_enabled = False
        self.listeners.apply()
        self.assertIsNone(self.listeners.get("pst"))
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.sockets.append(blocker)
        self.cfg.hamlib_port = blocker.getsockname()[1]
        errors = self.listeners.apply()
        self.assertIn("hamlib", errors)
        self.assertIsNone(self.listeners.get("hamlib"))

    def test_clean_shutdown_with_all_listeners(self) -> None:
        self.hamlib()
        threads = [self.listeners.get(k) for k in ("n1mm", "hamlib", "pst")]
        self.listeners.shutdown()
        for t in threads:
            self.assertFalse(t.is_alive(), t.name)


class ConfigListenerTests(unittest.TestCase):
    def test_new_keys_sanitize(self) -> None:
        cfg = Config(hamlib_bind="bogus", pst_bind="localhost", park_heading="abc",
                     retarget_mode="wild", hamlib_max_clients=999, pst_port=65535)
        cfg.sanitize()
        self.assertEqual(cfg.hamlib_bind, "0.0.0.0")
        self.assertEqual(cfg.pst_bind, "127.0.0.1")
        self.assertIsNone(cfg.park_heading)
        self.assertEqual(cfg.retarget_mode, "direct")
        self.assertEqual(cfg.hamlib_max_clients, 32)
        self.assertEqual(cfg.pst_port, 65534, "leaves room for the reply port")

    def test_defaults_bind_all_interfaces(self) -> None:
        cfg = Config()
        self.assertEqual((cfg.n1mm_bind, cfg.hamlib_bind, cfg.pst_bind),
                         ("0.0.0.0",) * 3)
        self.assertFalse(cfg.hamlib_enabled or cfg.pst_enabled)
        self.assertIsNone(cfg.park_heading)


if __name__ == "__main__":
    unittest.main(verbosity=2)
