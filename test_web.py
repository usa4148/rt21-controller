#!/usr/bin/env python3
"""Tests for rt21_web.py — protocol, link durability, and the HTTP API.

Runs entirely against the built-in simulator; no hardware, no browser,
no third-party packages.

    python3 test_web.py
"""

from __future__ import annotations

import json
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
    Hub,
    LinkManager,
    LinkState,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
