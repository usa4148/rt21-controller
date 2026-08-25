#!/usr/bin/env python3
"""
RT-21 Rotator Controller — web edition.

A durable, cross-platform client for the Green Heron Engineering RT-21
rotator controller over its network (TCP) port. The user interface is a web
page served locally by this script and rendered by whatever browser the
machine already has — nothing to install, no GUI toolkit to break.

    python3 rt21_web.py                # start, open the UI in the browser
    python3 rt21_web.py --demo         # run against a built-in RT-21 simulator
    python3 rt21_web.py --host 192.168.7.203 --port 6555
    python3 rt21_web.py --listen 0.0.0.0   # allow phones/tablets on the LAN
    python3 rt21_web.py --no-browser   # don't auto-open a browser tab

Design goals
------------
* Correctness  : commands and replies follow RT-21 Manual Appendix F exactly.
* Durability   : all socket I/O lives in one worker thread with an outbound
                 queue, automatic reconnect with backoff, and a stale-data
                 watchdog. The HTTP layer is stateless; refresh the page any
                 time and it re-syncs.
* Portability  : Python 3.9+ standard library only. Runs identically on
                 macOS, Windows and Linux, headless or not.

Protocol reference (RT-21 Manual, Appendix F — "n" is the rotator/unit digit):

    ;                   stop immediately (also clears the controller's buffer)
    STn;                stop immediately
    AIn;                read heading      -> "xxx;"            (no SOH prefix)
    APnxxx;             set target for the next AMn;
    APnxxx<CR>;         slew to xxx immediately (note the carriage return)
    AMn;                slew to the last APn target
    AAn; / ABn;         jog CCW / CW for 1.5 s, re-triggerable
    R1n;                read model + firmware -> <SOH>"RT-21 Version X.Y";
    R2n;                read heading + status -> <SOH>"xxx s;"  s=1 running, 2 stopped

    Every reply that carries a value EXCEPT AIn; is prefixed with SOH (0x01),
    which is what lets this client interleave AIn; and R2n; polls safely.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import queue
import re
import select
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

APP_NAME = "RT-21 Controller"
APP_SLUG = "rt21-controller"
APP_VERSION = "3.0.0"
ORG_NAME = "GreenHeron"

SOH = "\x01"
LOG = logging.getLogger(APP_SLUG)


# --------------------------------------------------------------------------- #
# Paths, configuration, logging
# --------------------------------------------------------------------------- #
def _base_dir(kind: str) -> Path:
    """Per-user config/log directory, correct on every OS, stdlib only."""
    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / ("Preferences" if kind == "config" else "Application Support")
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA" if kind == "config" else "LOCALAPPDATA", home))
    else:
        env = "XDG_CONFIG_HOME" if kind == "config" else "XDG_DATA_HOME"
        default = ".config" if kind == "config" else ".local/share"
        root = Path(os.environ.get(env, str(home / default)))
    return root / ORG_NAME / APP_NAME


CONFIG_DIR = _base_dir("config")
DATA_DIR = _base_dir("data")
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = DATA_DIR / "logs" / "rt21.log"

DEFAULT_PRESETS: list[dict[str, Any]] = [
    {"name": "EU", "heading": 30},
    {"name": "JA", "heading": 315},
    {"name": "VK/ZL", "heading": 240},
    {"name": "SA", "heading": 130},
    {"name": "AF", "heading": 75},
    {"name": "Carib", "heading": 105},
    {"name": "North", "heading": 0},
    {"name": "West", "heading": 270},
]


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _clampf(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class Config:
    """Everything that survives a restart. Written atomically as JSON.

    The file is shared with the older Qt client; keys this edition does not
    use are preserved on save rather than deleted.
    """

    host: str = "192.168.7.203"
    port: int = 6555
    unit: int = 1
    poll_interval: float = 1.0          # seconds between AIn; polls
    stale_timeout: float = 6.0          # no heading for this long -> force reconnect
    connect_timeout: float = 5.0
    auto_reconnect: bool = True
    reconnect_max_delay: float = 15.0
    transport: str = "auto"             # auto | tcp | ghe (GH Everywhere HTTP bridge)
    max_heading: int = 359              # 359, or up to 449 for overlap rotators
    auto_connect_on_start: bool = True
    dark_mode: bool = True
    show_raw_traffic: bool = False
    presets: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_PRESETS))
    http_port: int = 8721               # where this script's own web UI listens

    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            if CONFIG_PATH.exists():
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                known = set(cls().__dataclass_fields__) - {"_extra"}
                for key, value in raw.items():
                    if key in known:
                        setattr(cfg, key, value)
                    else:
                        cfg._extra[key] = value
                LOG.info("Loaded configuration from %s", CONFIG_PATH)
        except Exception:
            LOG.exception("Configuration unreadable; falling back to defaults")
        cfg.sanitize()
        return cfg

    def sanitize(self) -> None:
        """Clamp anything a hand-edited config file could get wrong."""
        self.host = str(self.host or "").strip() or "192.168.7.203"
        self.port = _clamp(int(self.port or 6555), 1, 65535)
        self.unit = _clamp(int(self.unit or 1), 0, 9)
        self.poll_interval = _clampf(float(self.poll_interval), 0.2, 10.0)
        self.stale_timeout = _clampf(float(self.stale_timeout), 2.0, 120.0)
        self.connect_timeout = _clampf(float(self.connect_timeout), 1.0, 60.0)
        self.reconnect_max_delay = _clampf(float(self.reconnect_max_delay), 1.0, 300.0)
        self.max_heading = _clamp(int(self.max_heading), 359, 719)
        self.http_port = _clamp(int(self.http_port or 8721), 1, 65535)
        if self.transport not in ("auto", "tcp", "ghe"):
            self.transport = "auto"
        if not isinstance(self.presets, list):
            self.presets = list(DEFAULT_PRESETS)
        clean: list[dict[str, Any]] = []
        for item in self.presets[:24]:
            try:
                name = str(item["name"])[:12].strip()
                heading = _clamp(int(item["heading"]), 0, self.max_heading)
                if name:
                    clean.append({"name": name, "heading": heading})
            except Exception:
                continue
        self.presets = clean

    def save(self) -> None:
        """Atomic write: temp file in the same directory, then replace."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = asdict(self)
            extra = data.pop("_extra", {})
            merged = {**extra, **data}
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
            os.replace(tmp, CONFIG_PATH)
            LOG.debug("Configuration saved to %s", CONFIG_PATH)
        except Exception:
            LOG.exception("Could not save configuration to %s", CONFIG_PATH)


def setup_logging(verbose: bool = False) -> None:
    """Rotating file log plus a console log. Never fatal if the disk says no."""
    LOG.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(fmt)
    LOG.addHandler(stream)

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        rotating.setLevel(logging.DEBUG)
        rotating.setFormatter(fmt)
        LOG.addHandler(rotating)
        LOG.info("Log file: %s", LOG_PATH)
    except Exception:
        LOG.warning("File logging unavailable (%s); continuing with console only", LOG_PATH)


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Reply:
    """One decoded frame from the controller."""

    kind: str            # heading | heading_status | info | unknown
    raw: str
    heading: Optional[float] = None
    moving: Optional[bool] = None
    text: str = ""


class Protocol:
    """Encoders and a decoder for the RT-21 command set (Appendix F).

    Every method is pure and side-effect free, which makes the wire format
    unit-testable without a radio or a socket.
    """

    #: reply to R2n; -> "xxx s" where s is 1 (running) or 2 (stopped)
    _STATUS_RE = re.compile(r"^(\d{1,3}(?:\.\d)?)\s*([12])$")
    _HEADING_RE = re.compile(r"^(\d{1,3}(?:\.\d)?)$")

    def __init__(self, unit: int = 1) -> None:
        self.unit = _clamp(int(unit), 0, 9)

    # -- outbound ----------------------------------------------------------- #
    def read_heading(self) -> str:
        """AIn; — the one reply with no SOH prefix, so it is unambiguous."""
        return f"AI{self.unit};"

    def read_status(self) -> str:
        """R2n; — heading plus running/stopped, SOH prefixed."""
        return f"R2{self.unit};"

    def read_version(self) -> str:
        return f"R1{self.unit};"

    def goto(self, degrees: int) -> str:
        """APnxxx<CR>; — slew immediately.

        The carriage return before the semicolon is what distinguishes an
        immediate move from merely loading a target.
        """
        return f"AP{self.unit}{int(degrees) % 1000:03d}\r;"

    def set_target(self, degrees: int) -> str:
        return f"AP{self.unit}{int(degrees) % 1000:03d};"

    def move_to_target(self) -> str:
        return f"AM{self.unit};"

    def stop(self) -> list[str]:
        """A bare ';' both clears the controller's input buffer and stops."""
        return [";", f"ST{self.unit};"]

    def jog_ccw(self) -> str:
        return f"AA{self.unit};"

    def jog_cw(self) -> str:
        return f"AB{self.unit};"

    # -- inbound ------------------------------------------------------------ #
    @classmethod
    def decode(cls, frame: str) -> Reply:
        raw = frame
        had_soh = SOH in frame
        body = frame.replace(SOH, "").replace("\r", "").replace("\n", "").strip()
        if not body:
            return Reply(kind="unknown", raw=raw)

        match = cls._STATUS_RE.match(body)
        if had_soh and match:
            return Reply(
                kind="heading_status",
                raw=raw,
                heading=float(match.group(1)) % 360.0,
                moving=match.group(2) == "1",
            )

        match = cls._HEADING_RE.match(body)
        if match:
            return Reply(kind="heading", raw=raw, heading=float(match.group(1)) % 360.0)

        if any(ch.isalpha() for ch in body):
            return Reply(kind="info", raw=raw, text=body)

        return Reply(kind="unknown", raw=raw, text=body)

    #: GH Everywhere status codes (byte 2 of the bridged R2n; reply)
    GHE_STATUS = {
        0: "Idle", 1: "Busy (in motion)", 2: "No Motion Error", 3: "Unknown",
        4: "Pot out-of-range", 5: "Counter Range Error", 6: "User Initiated Action",
    }

    @classmethod
    def decode_ghe(cls, value: str) -> Reply:
        """Decode one buffered frame from the GH Everywhere HTTP-serial bridge.

        The bridged R2n; reply is not the plain "xxx s" form; observed on
        firmware 4.13.2 it is  <SOH> '0' <status-byte> '>' ' 245.5' ';'
        — heading after the '>', status as a raw byte (see GHE_STATUS).
        Anything without a '>' falls through to the normal decoder.
        """
        raw = value
        body = value.rstrip().rstrip(";")
        if ">" in body:
            try:
                heading = float(body.split(">", 1)[1])
            except ValueError:
                return Reply(kind="unknown", raw=raw)
            moving: Optional[bool] = None
            if len(body) >= 3 and body.startswith(SOH):
                status = ord(body[2])
                if status not in cls.GHE_STATUS and body[2].isdigit():
                    status = int(body[2])
                moving = status == 1
            return Reply(kind="heading_status", raw=raw,
                         heading=heading % 360.0, moving=moving)
        return cls.decode(body)


def _shortest_delta(current: float, target: float) -> float:
    delta = (target - current) % 360.0
    if delta > 180.0:
        delta -= 360.0
    return delta


# --------------------------------------------------------------------------- #
# Event hub — one place that owns "what is true right now"
# --------------------------------------------------------------------------- #
class Hub:
    """Holds the live state snapshot and fans events out to SSE subscribers.

    Publishers (the link thread, the HTTP handlers) call publish(); every
    browser tab holds a subscriber queue that its /events stream drains.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list["queue.Queue[tuple[str, dict]]"] = []
        self.state: dict[str, Any] = {
            "link": "disconnected",
            "detail": "Disconnected",
            "heading": None,
            "moving": False,
            "target": None,
            "version": "",
        }
        self.console: deque[dict[str, str]] = deque(maxlen=400)

    def subscribe(self) -> "queue.Queue[tuple[str, dict]]":
        q: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[tuple[str, dict]]") -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            if event == "state":
                self.state["link"] = data.get("state", self.state["link"])
                self.state["detail"] = data.get("detail", self.state["detail"])
            elif event == "heading":
                self.state["heading"] = data.get("deg")
            elif event == "motion":
                self.state["moving"] = bool(data.get("moving"))
            elif event == "target":
                self.state["target"] = data.get("deg")
            elif event == "info":
                self.state["version"] = data.get("text", self.state["version"])
            if event in ("traffic", "info", "log"):
                entry = {"kind": event, **{k: str(v) for k, v in data.items()}}
                self.console.append(entry)
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass  # a stalled tab loses events, never blocks the link

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self.state, "console": list(self.console)}


class HubLogHandler(logging.Handler):
    """Mirrors log records into the browser console pane."""

    def __init__(self, hub: Hub) -> None:
        super().__init__(level=logging.INFO)
        self._hub = hub

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self._hub.publish("log", {"level": record.levelname, "text": record.getMessage()})
        except Exception:  # pragma: no cover - the log must never crash the app
            pass


# --------------------------------------------------------------------------- #
# Transport — one worker thread owns the socket
# --------------------------------------------------------------------------- #
class LinkState:
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class RotatorLink(threading.Thread):
    """Owns the TCP connection to the RT-21 for the lifetime of the app.

    Nothing outside this thread ever touches the socket. Callers submit
    command strings through a queue; headings and state go out through the
    hub. Losing the link is a normal event, not an error: the thread
    reconnects on an exponential backoff and keeps polling.
    """

    def __init__(self, cfg: Config, hub: Hub) -> None:
        super().__init__(daemon=True, name="rt21-link")
        self._cfg = cfg
        self._hub = hub
        self._proto = Protocol(cfg.unit)
        self._outbox: "queue.Queue[str]" = queue.Queue(maxsize=200)
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._state = LinkState.DISCONNECTED
        self._last_heading_at = 0.0
        self._moving = False
        self._mode = "tcp"                    # transport of the current session
        self._detected: Optional[str] = None  # remembered auto-detection result
        self._ghe_base = ""

    # -- public API (safe to call from HTTP handler threads) ---------------- #
    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state == LinkState.CONNECTED

    @property
    def protocol(self) -> Protocol:
        return self._proto

    def submit(self, command: "str | list[str]") -> None:
        """Queue one or more commands for the worker thread to transmit."""
        commands = [command] if isinstance(command, str) else list(command)
        for item in commands:
            try:
                self._outbox.put_nowait(item)
            except queue.Full:
                LOG.warning("Command queue full; dropped %r", item)
        self._wake.set()

    def shutdown(self, timeout: float = 3.0) -> None:
        """Ask the worker to exit and wait briefly for it."""
        self._quit.set()
        self._wake.set()
        self._close_socket()
        self.join(timeout)
        if self.is_alive():
            LOG.warning("Link thread did not exit within %.1fs", timeout)

    # -- worker ------------------------------------------------------------- #
    def run(self) -> None:  # noqa: C901 - a connection loop is inherently branchy
        delay = 1.0
        while not self._quit.is_set():
            if self._connect_once():
                delay = 1.0
                if self._mode == "ghe":
                    self._pump_ghe()              # blocks until the link drops
                else:
                    self._pump()
            if self._quit.is_set():
                break
            if not self._cfg.auto_reconnect:
                self._set_state(LinkState.DISCONNECTED, "Disconnected")
                break
            self._set_state(LinkState.RECONNECTING, f"Reconnecting in {delay:.0f}s…")
            self._wake.clear()
            self._wake.wait(delay)
            delay = min(delay * 2, self._cfg.reconnect_max_delay)
        self._close_socket()
        self._set_state(LinkState.DISCONNECTED, "Disconnected")
        LOG.info("Link thread exited")

    def _connect_once(self) -> bool:
        host, port = self._cfg.host.strip(), int(self._cfg.port)
        transport = self._detected or self._cfg.transport

        if transport in ("auto", "ghe"):
            if self._ghe_probe(host, port):
                self._mode = self._detected = "ghe"
                return True
            if transport == "ghe":
                return False

        self._set_state(LinkState.CONNECTING, f"Connecting to {host}:{port}…")
        try:
            sock = socket.create_connection((host, port), timeout=self._cfg.connect_timeout)
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
            with self._sock_lock:
                self._sock = sock
        except OSError as exc:
            self._set_state(LinkState.ERROR, f"Connect failed: {exc.strerror or exc}")
            LOG.warning("Connect to %s:%s failed: %s", host, port, exc)
            return False

        self._mode = self._detected = "tcp"
        self._last_heading_at = time.monotonic()
        self._drain_outbox()
        self._set_state(LinkState.CONNECTED, f"Connected to {host}:{port}")
        LOG.info("Connected to %s:%s", host, port)
        self._send(self._proto.read_version())
        self._send(self._proto.read_heading())
        return True

    def _pump(self) -> None:
        """Read frames, transmit queued commands, poll, and watch for staleness."""
        buffer = ""
        next_poll = time.monotonic()
        while not self._quit.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                readable, _, errored = select.select([sock], [], [sock], 0.2)
            except (OSError, ValueError):
                LOG.info("Socket went away while selecting")
                break
            if errored:
                LOG.warning("Socket reported an error condition")
                break

            if readable:
                try:
                    chunk = sock.recv(4096)
                except (TimeoutError, socket.timeout):
                    chunk = b""
                except OSError as exc:
                    LOG.warning("Read error: %s", exc)
                    break
                if not chunk:
                    LOG.info("Remote end closed the connection")
                    self._set_state(LinkState.ERROR, "Connection closed by controller")
                    break
                buffer = self._consume(buffer + chunk.decode("ascii", errors="replace"))

            # transmit anything the UI queued
            while True:
                try:
                    command = self._outbox.get_nowait()
                except queue.Empty:
                    break
                if not self._send(command):
                    return

            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + self._cfg.poll_interval
                # AIn; is the guaranteed heading source (no SOH, every firmware).
                # R2n; adds running/stopped and is SOH-prefixed, so the two
                # replies can never be confused with one another. If the
                # controller ignores R2n; the UI simply shows no motion state.
                if not self._send(self._proto.read_heading()):
                    return
                if not self._send(self._proto.read_status()):
                    return

            if now - self._last_heading_at > self._cfg.stale_timeout:
                LOG.warning(
                    "No heading for %.1fs — link is stale, forcing a reconnect",
                    now - self._last_heading_at,
                )
                self._set_state(LinkState.ERROR, "No response from controller")
                break

        self._close_socket()

    # -- GH Everywhere (ezWebLynx HTTP-serial bridge) transport -------------- #
    #
    # The GHE box serves HTTP on the configured port. Commands go out as
    #   GET /blank.html?SERIAL_STRING=<cmd>
    # and the last serial reply frame (delimited SOH..';' by the init call) is
    # read back from
    #   GET /data.htm   ->  "<END>\nserial_get:<frame><END>\nid:RT-21<END>\n\n"
    # This is exactly what Green Heron's own web page does.

    def _http_get(self, url: str, timeout: float) -> bytes:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()

    def _ghe_probe(self, host: str, port: int) -> bool:
        base = f"http://{host}:{port}/"
        self._set_state(LinkState.CONNECTING, f"Probing {host}:{port}…")
        try:
            body = self._http_get(
                base + "data.htm?SERIAL_START=0x01&SERIAL_END=0x3B",
                timeout=min(3.0, self._cfg.connect_timeout),
            )
        except Exception as exc:
            LOG.debug("GHE probe of %s:%s failed: %s", host, port, exc)
            return False
        if b"serial_get" not in body:
            return False
        self._ghe_base = base
        self._last_heading_at = time.monotonic()
        self._drain_outbox()
        name = ""
        for part in body.decode("latin-1").split("<END>"):
            part = part.strip()
            if part.startswith("id:"):
                name = part[3:].strip()
        detail = f"Connected to {host}:{port} (GH Everywhere{' · ' + name if name else ''})"
        self._set_state(LinkState.CONNECTED, detail)
        LOG.info("%s", detail)
        return True

    def _pump_ghe(self) -> None:
        """Poll the GHE bridge: send queued commands, read the reply buffer."""
        next_poll = 0.0
        version_pending = True
        while not self._quit.is_set():
            while True:
                try:
                    command = self._outbox.get_nowait()
                except queue.Empty:
                    break
                if not self._ghe_send(command):
                    return

            now = time.monotonic()
            if now >= next_poll:
                # the bridge needs ~0.75 s to collect the serial reply, so the
                # effective poll floor here is one second
                next_poll = now + max(self._cfg.poll_interval, 1.0)
                query = self._proto.read_version() if version_pending \
                    else self._proto.read_status()
                if not self._ghe_send(query):
                    return
                if self._quit.wait(0.75):
                    return
                frame = self._ghe_read()
                if frame is None:
                    return
                if frame:
                    reply = Protocol.decode_ghe(frame)
                    printable = reply.raw.replace(SOH, "<SOH>").replace("\x00", "<NUL>")
                    self._hub.publish("traffic", {"dir": "rx", "data": printable + ";"})
                    if reply.kind == "heading_status" and reply.heading is not None:
                        self._last_heading_at = time.monotonic()
                        self._hub.publish("heading", {"deg": reply.heading})
                        if reply.moving is not None and reply.moving != self._moving:
                            self._moving = reply.moving
                            self._hub.publish("motion", {"moving": reply.moving})
                    elif reply.kind == "info":
                        version_pending = False
                        LOG.info("Controller: %s", reply.text)
                        self._hub.publish("info", {"text": reply.text})

            if not version_pending and \
                    now - self._last_heading_at > max(self._cfg.stale_timeout, 5.0):
                LOG.warning("No heading from the GHE bridge — forcing a reconnect")
                self._set_state(LinkState.ERROR, "No response from controller")
                return

            if self._quit.wait(0.15):
                return

    def _ghe_send(self, command: str) -> bool:
        url = self._ghe_base + "blank.html?" + urllib.parse.urlencode(
            {"SERIAL_STRING": command})
        try:
            self._http_get(url, timeout=min(4.0, self._cfg.connect_timeout))
        except Exception as exc:
            LOG.warning("GHE send failed (%s): %s", command.strip(), exc)
            self._set_state(LinkState.ERROR, f"Send failed: {exc}")
            return False
        self._hub.publish("traffic", {"dir": "tx", "data": command.replace("\r", "<CR>")})
        return True

    def _ghe_read(self) -> Optional[str]:
        """Return the buffered serial frame, '' if none, None on link failure."""
        try:
            body = self._http_get(self._ghe_base + "data.htm",
                                  timeout=min(4.0, self._cfg.connect_timeout))
        except Exception as exc:
            LOG.warning("GHE read failed: %s", exc)
            self._set_state(LinkState.ERROR, f"Read failed: {exc}")
            return None
        for part in body.decode("latin-1").split("<END>"):
            part = part.lstrip("\n")
            if part.startswith("serial_get:"):
                return part[len("serial_get:"):]
        return ""

    def _consume(self, buffer: str) -> str:
        """Split the buffer on ';' and dispatch each complete frame.

        Returns whatever partial frame is left over. A controller that streams
        bytes without ever sending a terminator cannot grow this buffer without
        bound — anything past 4 kB of unterminated data is discarded.
        """
        if len(buffer) > 4096:
            LOG.warning("Discarding %d bytes of unterminated input", len(buffer) - 1024)
            buffer = buffer[-1024:]
        while ";" in buffer:
            frame, buffer = buffer.split(";", 1)
            frame = frame.strip("\r\n ")
            if not frame:
                continue
            reply = Protocol.decode(frame)
            self._hub.publish("traffic", {"dir": "rx", "data": reply.raw.replace(SOH, "<SOH>") + ";"})
            if reply.kind in ("heading", "heading_status") and reply.heading is not None:
                self._last_heading_at = time.monotonic()
                self._hub.publish("heading", {"deg": reply.heading})
                if reply.moving is not None and reply.moving != self._moving:
                    self._moving = reply.moving
                    self._hub.publish("motion", {"moving": reply.moving})
            elif reply.kind == "info":
                LOG.info("Controller: %s", reply.text)
                self._hub.publish("info", {"text": reply.text})
        return buffer

    def _send(self, command: str) -> bool:
        with self._sock_lock:
            sock = self._sock
            if sock is None:
                return False
            try:
                sock.sendall(command.encode("ascii", errors="ignore"))
            except OSError as exc:
                LOG.warning("Send failed (%s): %s", command.strip(), exc)
                self._set_state(LinkState.ERROR, f"Send failed: {exc.strerror or exc}")
                return False
        self._hub.publish("traffic", {"dir": "tx", "data": command.replace("\r", "<CR>")})
        return True

    def _drain_outbox(self) -> None:
        while True:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                return

    def _close_socket(self) -> None:
        with self._sock_lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _set_state(self, state: str, detail: str) -> None:
        self._state = state
        self._hub.publish("state", {"state": state, "detail": detail})


class LinkManager:
    """Starts and stops RotatorLink threads on behalf of the HTTP handlers."""

    def __init__(self, cfg: Config, hub: Hub) -> None:
        self._cfg = cfg
        self._hub = hub
        self._link: Optional[RotatorLink] = None
        self._lock = threading.Lock()

    @property
    def link(self) -> Optional[RotatorLink]:
        return self._link

    def connect(self) -> None:
        with self._lock:
            if self._link is not None and self._link.is_alive():
                return
            self._link = RotatorLink(self._cfg, self._hub)
            self._link.start()

    def disconnect(self) -> None:
        with self._lock:
            link, self._link = self._link, None
        if link is not None:
            link.shutdown()

    def submit(self, command: "str | list[str]") -> bool:
        link = self._link
        if link is None or not link.connected:
            return False
        link.submit(command)
        return True

    def protocol(self) -> Protocol:
        link = self._link
        return link.protocol if link is not None else Protocol(self._cfg.unit)


# --------------------------------------------------------------------------- #
# Built-in simulator (--demo) — lets the UI be exercised with no hardware
# --------------------------------------------------------------------------- #
class Rt21Simulator(threading.Thread):
    """A minimal fake RT-21 that speaks enough of Appendix F to drive the UI."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, speed: float = 6.0) -> None:
        super().__init__(daemon=True, name="rt21-sim")
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._speed = speed
        self._heading = 0.0
        self._target = 0.0
        self._stop = threading.Event()
        self._clients: list[socket.socket] = []

    def shutdown(self) -> None:
        self._stop.set()
        for client in list(self._clients):
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
        self._clients.clear()
        try:
            self._server.close()
        except OSError:
            pass

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            self._clients.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(0.2)
        buffer = ""
        last = time.monotonic()
        with conn:
            while not self._stop.is_set():
                now = time.monotonic()
                self._advance(now - last)
                last = now
                try:
                    data = conn.recv(1024)
                    if not data:
                        return
                    buffer += data.decode("ascii", errors="ignore")
                except (TimeoutError, socket.timeout):
                    continue
                except OSError:
                    return
                while ";" in buffer:
                    frame, buffer = buffer.split(";", 1)
                    reply = self._handle(frame.strip("\r\n "))
                    if reply:
                        try:
                            conn.sendall(reply.encode("ascii"))
                        except OSError:
                            return

    def _advance(self, elapsed: float) -> None:
        delta = _shortest_delta(self._heading, self._target)
        if abs(delta) < 0.5:
            self._heading = self._target
            return
        step = min(abs(delta), self._speed * elapsed)
        self._heading = (self._heading + step * (1 if delta > 0 else -1)) % 360.0

    def _handle(self, frame: str) -> str:
        moving = abs(_shortest_delta(self._heading, self._target)) >= 0.5
        if frame == "":
            self._target = self._heading
            return ""
        upper = frame.upper()
        if upper.startswith("AI"):
            return f"{int(self._heading) % 360:03d};"
        if upper.startswith("R2"):
            return f"{SOH}{int(self._heading) % 360:03d} {'1' if moving else '2'};"
        if upper.startswith("R1"):
            return f"{SOH}RT-21 Version 3.9 (simulated);"
        if upper.startswith("ST"):
            self._target = self._heading
            return ""
        if upper.startswith("AP"):
            digits = re.sub(r"\D", "", upper[3:])
            if digits:
                self._target = float(int(digits) % 360)
            return ""
        if upper.startswith("AA"):
            self._target = (self._heading - 9) % 360
            return ""
        if upper.startswith("AB"):
            self._target = (self._heading + 9) % 360
            return ""
        return ""


# --------------------------------------------------------------------------- #
# HTTP server + API
# --------------------------------------------------------------------------- #
class AppContext:
    """What the HTTP handlers need: config, hub, link manager."""

    def __init__(self, cfg: Config, hub: Hub, manager: LinkManager) -> None:
        self.cfg = cfg
        self.hub = hub
        self.manager = manager


class Handler(BaseHTTPRequestHandler):
    server_version = f"rt21-web/{APP_VERSION}"
    protocol_version = "HTTP/1.1"
    ctx: AppContext  # set on the server class before serving

    # -- plumbing ----------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:  # quiet the default spam
        LOG.debug("http: " + fmt, *args)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _same_origin(self) -> bool:
        """Reject cross-site POSTs. Browsers always send Origin on those."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    # -- GET ---------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._json(200, self._state_payload())
            elif path == "/events":
                self._serve_events()
            else:
                self._json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _state_payload(self) -> dict[str, Any]:
        cfg = self.ctx.cfg
        return {
            **self.ctx.hub.snapshot(),
            "config": {
                "host": cfg.host,
                "port": cfg.port,
                "unit": cfg.unit,
                "max_heading": cfg.max_heading,
                "presets": cfg.presets,
                "dark_mode": cfg.dark_mode,
                "show_raw_traffic": cfg.show_raw_traffic,
                "poll_interval": cfg.poll_interval,
                "transport": cfg.transport,
            },
            "app": {"name": APP_NAME, "version": APP_VERSION},
        }

    def _serve_events(self) -> None:
        """Server-Sent Events: the push channel to the browser."""
        q = self.ctx.hub.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self._sse("hello", self._state_payload())
            while True:
                try:
                    event, data = q.get(timeout=15.0)
                    self._sse(event, data)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.ctx.hub.unsubscribe(q)

    def _sse(self, event: str, data: dict[str, Any]) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    # -- POST --------------------------------------------------------------- #
    def do_POST(self) -> None:  # noqa: N802
        if not self._same_origin():
            self._json(403, {"error": "cross-origin request rejected"})
            return
        path = urlparse(self.path).path
        body = self._read_body()
        ctx = self.ctx
        try:
            if path == "/api/connect":
                ctx.manager.connect()
                self._json(200, {"ok": True})
            elif path == "/api/disconnect":
                ctx.manager.disconnect()
                self._json(200, {"ok": True})
            elif path == "/api/goto":
                self._handle_goto(body)
            elif path == "/api/stop":
                ok = ctx.manager.submit(ctx.manager.protocol().stop())
                ctx.hub.publish("target", {"deg": None})
                self._json(200 if ok else 409, {"ok": ok})
            elif path == "/api/jog":
                proto = ctx.manager.protocol()
                direction = str(body.get("dir", ""))
                if direction not in ("cw", "ccw"):
                    self._json(400, {"error": "dir must be cw or ccw"})
                    return
                cmd = proto.jog_cw() if direction == "cw" else proto.jog_ccw()
                ok = ctx.manager.submit(cmd)
                self._json(200 if ok else 409, {"ok": ok})
            elif path == "/api/config":
                self._handle_config(body)
            else:
                self._json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_goto(self, body: dict[str, Any]) -> None:
        ctx = self.ctx
        try:
            heading = int(body.get("heading"))
        except (TypeError, ValueError):
            self._json(400, {"error": "heading must be a number"})
            return
        if not 0 <= heading <= ctx.cfg.max_heading:
            self._json(400, {"error": f"heading must be 0–{ctx.cfg.max_heading}"})
            return
        # AP1xxx<CR>; slews on its own; AM1; after it is harmless and covers
        # firmware that treats the AP form as target-only (Green Heron's own
        # web client sends the same pair).
        proto = ctx.manager.protocol()
        ok = ctx.manager.submit([proto.goto(heading), proto.move_to_target()])
        if ok:
            ctx.hub.publish("target", {"deg": heading % 360})
        self._json(200 if ok else 409, {"ok": ok, "heading": heading})

    def _handle_config(self, body: dict[str, Any]) -> None:
        """Apply a settings change. Connection fields take effect on reconnect."""
        cfg = self.ctx.cfg
        allowed = {
            "host": str, "port": int, "unit": int, "max_heading": int,
            "presets": list, "dark_mode": bool, "show_raw_traffic": bool,
            "poll_interval": float, "auto_reconnect": bool, "transport": str,
        }
        changed = []
        for key, cast in allowed.items():
            if key in body:
                try:
                    setattr(cfg, key, cast(body[key]))
                    changed.append(key)
                except (TypeError, ValueError):
                    self._json(400, {"error": f"bad value for {key}"})
                    return
        cfg.sanitize()
        cfg.save()
        if "unit" in changed:
            link = self.ctx.manager.link
            if link is not None:
                link.protocol.unit = cfg.unit
        self.ctx.hub.publish("config", self._state_payload()["config"])
        LOG.info("Configuration updated (%s)", ", ".join(changed) or "no changes")
        self._json(200, {"ok": True, "changed": changed})


# --------------------------------------------------------------------------- #
# The page. One HTML document, no external assets, dark and light themes.
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>RT-21 Rotator</title>
<style>
  :root {
    --bg: #10141b; --panel: #1a2029; --panel2: #222a36; --line: #2e3947;
    --text: #e8edf4; --dim: #8b98a9; --accent: #4da3ff;
    --needle: #ff5252; --needle-moving: #35d07f; --target: #ffb340;
    --stop: #d33; --stop-press: #f44; --ok: #35d07f; --warn: #ffb340; --err: #ff5252;
  }
  html[data-theme="light"] {
    --bg: #eef1f5; --panel: #ffffff; --panel2: #f2f5f9; --line: #d5dce5;
    --text: #1a222c; --dim: #5c6a7a; --accent: #1668c7;
    --needle: #d32f2f; --needle-moving: #1d9e63; --target: #c07f00;
  }
  * { box-sizing: border-box; margin: 0; -webkit-tap-highlight-color: transparent; }
  body {
    background: var(--bg); color: var(--text);
    font: 15px/1.45 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    min-height: 100vh; padding: 10px; touch-action: manipulation;
  }
  .wrap { max-width: 1150px; margin: 0 auto; display: flex; flex-direction: column; gap: 10px; }

  header {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px;
  }
  header h1 { font-size: 17px; font-weight: 650; margin-right: 4px; white-space: nowrap; }
  .pill {
    padding: 3px 12px; border-radius: 99px; font-size: 13px; font-weight: 600;
    background: var(--panel2); border: 1px solid var(--line); color: var(--dim); white-space: nowrap;
  }
  .pill.connected    { color: #fff; background: var(--ok); border-color: transparent; }
  .pill.connecting, .pill.reconnecting { color: #222; background: var(--warn); border-color: transparent; }
  .pill.error        { color: #fff; background: var(--err); border-color: transparent; }
  #statusDetail { color: var(--dim); font-size: 13px; flex: 1; min-width: 120px;
                  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  header .spacer { flex: 1; }

  button {
    font: inherit; color: var(--text); background: var(--panel2);
    border: 1px solid var(--line); border-radius: 10px; padding: 8px 14px;
    cursor: pointer; user-select: none; -webkit-user-select: none;
  }
  button:hover { border-color: var(--accent); }
  button:active { transform: translateY(1px); }
  button.primary { background: var(--accent); border-color: transparent; color: #fff; font-weight: 600; }
  button:disabled { opacity: .45; cursor: default; pointer-events: none; }

  .main { display: grid; grid-template-columns: minmax(300px, 1fr) 330px; gap: 10px; }
  @media (max-width: 860px) { .main { grid-template-columns: 1fr; } }

  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; }

  /* -- compass -- */
  .compass-card { display: flex; flex-direction: column; align-items: center; gap: 8px; }
  #rose { width: 100%; max-width: 560px; aspect-ratio: 1; touch-action: none; cursor: crosshair; }
  .readout { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; justify-content: center; }
  #headingBig { font-size: 54px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1; }
  #headingSub { color: var(--dim); font-size: 15px; }
  #headingSub.moving { color: var(--needle-moving); font-weight: 600; }

  /* -- controls -- */
  .controls { display: flex; flex-direction: column; gap: 12px; }
  .row { display: flex; gap: 8px; align-items: center; }
  #target {
    flex: 1; min-width: 0; font: inherit; font-size: 20px; text-align: center;
    font-variant-numeric: tabular-nums;
    color: var(--text); background: var(--panel2); border: 1px solid var(--line);
    border-radius: 10px; padding: 9px 10px;
  }
  #target:focus { outline: 2px solid var(--accent); border-color: transparent; }
  #goBtn { font-size: 18px; padding: 9px 26px; }
  .jogs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .jogs button { font-size: 16px; padding: 12px; }
  #stopBtn {
    background: var(--stop); border-color: transparent; color: #fff;
    font-size: 24px; font-weight: 800; letter-spacing: 2px; padding: 18px; border-radius: 12px;
  }
  #stopBtn:active { background: var(--stop-press); }
  .presets { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
  .presets button { padding: 10px 4px; display: flex; flex-direction: column; gap: 1px; }
  .presets .pname { font-weight: 650; font-size: 14px; }
  .presets .pdeg { color: var(--dim); font-size: 12px; font-variant-numeric: tabular-nums; }
  .section-label { color: var(--dim); font-size: 12px; text-transform: uppercase;
                   letter-spacing: .8px; font-weight: 650; }

  /* -- console -- */
  .console-card { display: none; flex-direction: column; gap: 8px; }
  .console-card.visible { display: flex; }
  .console-head { display: flex; align-items: center; gap: 12px; }
  .console-head label { color: var(--dim); font-size: 13px; display: flex; gap: 5px; align-items: center; }
  #console {
    height: 180px; overflow-y: auto; background: var(--panel2);
    border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px;
    font: 12px/1.55 ui-monospace, "SF Mono", Menlo, Consolas, monospace; white-space: pre-wrap;
  }
  #console .tx   { color: var(--accent); }
  #console .rx   { color: var(--ok); }
  #console .log  { color: var(--dim); }
  #console .warn { color: var(--warn); }
  #console .err  { color: var(--err); }
  #console .info { color: var(--text); }

  /* -- modal -- */
  dialog {
    background: var(--panel); color: var(--text); border: 1px solid var(--line);
    border-radius: 14px; padding: 20px; width: min(480px, 92vw);
  }
  dialog::backdrop { background: rgba(0,0,0,.55); }
  dialog h2 { font-size: 17px; margin-bottom: 14px; }
  dialog .grid { display: grid; grid-template-columns: auto 1fr; gap: 10px 12px; align-items: center; }
  dialog label { color: var(--dim); font-size: 14px; white-space: nowrap; }
  dialog input[type=text], dialog input[type=number], dialog select {
    font: inherit; color: var(--text); background: var(--panel2);
    border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px; width: 100%;
  }
  dialog textarea {
    grid-column: 1 / -1; width: 100%; height: 130px; resize: vertical;
    font: 13px/1.5 ui-monospace, Menlo, Consolas, monospace;
    color: var(--text); background: var(--panel2);
    border: 1px solid var(--line); border-radius: 8px; padding: 8px;
  }
  dialog .hint { grid-column: 1 / -1; color: var(--dim); font-size: 12.5px; }
  dialog .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
  .footrow { display: flex; gap: 14px; align-items: center; color: var(--dim); font-size: 12.5px;
             flex-wrap: wrap; padding: 0 4px; }
  .footrow a { color: var(--dim); cursor: pointer; text-decoration: underline; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>RT-21 Rotator</h1>
    <span class="pill" id="statusPill">offline</span>
    <span id="statusDetail">Connecting to controller…</span>
    <span class="spacer"></span>
    <button id="connBtn">Connect</button>
    <button id="settingsBtn" title="Settings">⚙︎</button>
    <button id="themeBtn" title="Toggle theme">◐</button>
  </header>

  <div class="main">
    <div class="card compass-card">
      <canvas id="rose"></canvas>
      <div class="readout">
        <span id="headingBig">---°</span>
        <span id="headingSub">no data</span>
      </div>
    </div>

    <div class="card controls">
      <div class="section-label">Target</div>
      <div class="row">
        <input id="target" type="number" min="0" max="359" placeholder="°" inputmode="numeric">
        <button id="goBtn" class="primary">Go</button>
      </div>
      <div class="jogs">
        <button id="jogCCW" title="Jog counter-clockwise (AAn;)">⟲ CCW</button>
        <button id="jogCW"  title="Jog clockwise (ABn;)">CW ⟳</button>
      </div>
      <button id="stopBtn" title="Stop (Esc)">STOP</button>
      <div class="section-label">Beam headings</div>
      <div class="presets" id="presets"></div>
    </div>
  </div>

  <div class="card console-card" id="consoleCard">
    <div class="console-head">
      <span class="section-label">Console</span>
      <label><input type="checkbox" id="rawChk"> wire traffic</label>
      <span class="spacer" style="flex:1"></span>
      <button id="clearBtn" style="padding:4px 10px;font-size:12px">Clear</button>
    </div>
    <div id="console"></div>
  </div>

  <div class="footrow">
    <span id="verInfo"></span>
    <span id="connInfo"></span>
    <a id="consoleToggle">console</a>
    <span style="flex:1"></span>
    <span>Esc stop · Enter go · drag the rose to point</span>
  </div>
</div>

<dialog id="settingsDlg">
  <h2>Settings</h2>
  <div class="grid">
    <label>Controller host</label><input type="text" id="setHost">
    <label>Port</label><input type="number" id="setPort" min="1" max="65535">
    <label>Transport</label><select id="setTransport">
      <option value="auto">Auto-detect</option>
      <option value="tcp">Raw TCP</option>
      <option value="ghe">GH Everywhere (HTTP)</option>
    </select>
    <label>Unit digit</label><input type="number" id="setUnit" min="0" max="9">
    <label>Max heading</label><input type="number" id="setMax" min="359" max="719">
    <label>Poll interval (s)</label><input type="number" id="setPoll" min="0.2" max="10" step="0.1">
    <textarea id="setPresets" spellcheck="false"></textarea>
    <div class="hint">Beam headings, one per line: <b>name, degrees</b> (e.g. <code>EU, 30</code>)</div>
  </div>
  <div class="actions">
    <button id="setCancel">Cancel</button>
    <button id="setSave" class="primary">Save</button>
  </div>
</dialog>

<script>
"use strict";
const $ = id => document.getElementById(id);

/* ------------------------------------------------------------------ state */
const S = {
  link: "disconnected", detail: "", heading: null, moving: false,
  target: null, dragTarget: null, cfg: null, raw: false,
};

/* ------------------------------------------------------------- API helper */
async function api(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      if (r.status === 409) toast("Not connected");
      else if (e.error) toast(e.error);
    }
    return r.ok;
  } catch { toast("UI server unreachable"); return false; }
}
function toast(msg) { addLine("warn", "⚠ " + msg); }

/* ------------------------------------------------------------------- SSE */
let es = null;
function connectEvents() {
  es = new EventSource("/events");
  es.addEventListener("hello", e => { applySnapshot(JSON.parse(e.data)); });
  es.addEventListener("state", e => {
    const d = JSON.parse(e.data);
    S.link = d.state; S.detail = d.detail; renderStatus();
  });
  es.addEventListener("heading", e => {
    S.heading = JSON.parse(e.data).deg; renderHeading(); draw();
  });
  es.addEventListener("motion", e => {
    S.moving = JSON.parse(e.data).moving; renderHeading(); draw();
  });
  es.addEventListener("target", e => {
    S.target = JSON.parse(e.data).deg; renderHeading(); draw();
  });
  es.addEventListener("info", e => {
    const t = JSON.parse(e.data).text;
    addLine("info", t); $("verInfo").textContent = t;
  });
  es.addEventListener("traffic", e => {
    if (!S.raw) return;
    const d = JSON.parse(e.data);
    addLine(d.dir, (d.dir === "tx" ? "→ " : "← ") + d.data);
  });
  es.addEventListener("log", e => {
    const d = JSON.parse(e.data);
    addLine(d.level === "WARNING" ? "warn" : d.level === "ERROR" ? "err" : "log", d.text);
  });
  es.addEventListener("config", e => { S.cfg = JSON.parse(e.data); renderPresets(); draw(); });
  es.onerror = () => {
    S.link = "ui-lost"; S.detail = "Lost contact with the controller app";
    renderStatus();     /* EventSource reconnects on its own */
  };
}

function applySnapshot(snap) {
  S.link = snap.link; S.detail = snap.detail; S.heading = snap.heading;
  S.moving = snap.moving; S.target = snap.target; S.cfg = snap.config;
  S.raw = !!snap.config.show_raw_traffic;
  $("rawChk").checked = S.raw;
  document.documentElement.dataset.theme = snap.config.dark_mode ? "dark" : "light";
  $("target").max = snap.config.max_heading;
  $("connInfo").textContent = snap.config.host + ":" + snap.config.port +
                              " · unit " + snap.config.unit;
  if (snap.version) $("verInfo").textContent = snap.version;
  $("console").innerHTML = "";
  for (const line of snap.console || []) {
    if (line.kind === "traffic") {
      if (S.raw) addLine(line.dir, (line.dir === "tx" ? "→ " : "← ") + line.data);
    } else if (line.kind === "log") {
      addLine(line.level === "WARNING" ? "warn" : line.level === "ERROR" ? "err" : "log", line.text);
    } else addLine("info", line.text);
  }
  renderStatus(); renderHeading(); renderPresets(); draw();
}

/* -------------------------------------------------------------- rendering */
function renderStatus() {
  const pill = $("statusPill");
  pill.textContent = {connected: "connected", connecting: "connecting…",
    reconnecting: "reconnecting…", disconnected: "offline", error: "error",
    "ui-lost": "app lost"}[S.link] || S.link;
  pill.className = "pill " + (S.link === "ui-lost" ? "error" : S.link);
  $("statusDetail").textContent = S.detail;
  $("connBtn").textContent =
    (S.link === "connected" || S.link === "connecting" || S.link === "reconnecting")
      ? "Disconnect" : "Connect";
}

function renderHeading() {
  $("headingBig").textContent = S.heading == null ? "---°"
      : String(Math.round(S.heading)).padStart(3, "0") + "°";
  const sub = $("headingSub");
  if (S.heading == null) { sub.textContent = "no data"; sub.className = ""; return; }
  if (S.moving && S.target != null) {
    let d = (S.target - S.heading) % 360; if (d > 180) d -= 360; if (d < -180) d += 360;
    sub.textContent = "rotating · " + Math.abs(Math.round(d)) + "° " +
                      (d >= 0 ? "CW" : "CCW") + " to " + Math.round(S.target) + "°";
    sub.className = "moving";
  } else if (S.moving) { sub.textContent = "rotating"; sub.className = "moving"; }
  else { sub.textContent = "stopped"; sub.className = ""; }
}

function renderPresets() {
  const box = $("presets"); box.innerHTML = "";
  for (const p of (S.cfg ? S.cfg.presets : [])) {
    const b = document.createElement("button");
    b.innerHTML = "<span class='pname'></span><span class='pdeg'></span>";
    b.querySelector(".pname").textContent = p.name;
    b.querySelector(".pdeg").textContent = p.heading + "°";
    b.onclick = () => slew(p.heading);
    box.appendChild(b);
  }
}

function addLine(cls, text) {
  const con = $("console");
  const div = document.createElement("div");
  div.className = cls; div.textContent = text;
  con.appendChild(div);
  while (con.childNodes.length > 500) con.removeChild(con.firstChild);
  con.scrollTop = con.scrollHeight;
}

/* ---------------------------------------------------------------- compass */
const canvas = $("rose"), ctx2d = canvas.getContext("2d");

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function draw() {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== rect.width * dpr) {
    canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  }
  const g = ctx2d, w = rect.width;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, w);
  const cx = w / 2, cy = w / 2, R = w / 2 - 14;
  const line = cssVar("--line"), dim = cssVar("--dim"), text = cssVar("--text");

  g.lineWidth = 2; g.strokeStyle = line;
  g.beginPath(); g.arc(cx, cy, R, 0, Math.PI * 2); g.stroke();
  g.beginPath(); g.arc(cx, cy, R * 0.62, 0, Math.PI * 2);
  g.strokeStyle = line; g.globalAlpha = 0.5; g.stroke(); g.globalAlpha = 1;

  /* ticks + labels */
  for (let d = 0; d < 360; d += 5) {
    const major = d % 30 === 0, mid = d % 10 === 0;
    const a = (d - 90) * Math.PI / 180;
    const r1 = R, r2 = R - (major ? 14 : mid ? 9 : 5);
    g.strokeStyle = major ? dim : line; g.lineWidth = major ? 2 : 1;
    g.beginPath();
    g.moveTo(cx + r1 * Math.cos(a), cy + r1 * Math.sin(a));
    g.lineTo(cx + r2 * Math.cos(a), cy + r2 * Math.sin(a));
    g.stroke();
    if (major) {
      const cardinal = {0: "N", 90: "E", 180: "S", 270: "W"}[d];
      const rl = R - 28;
      g.fillStyle = cardinal ? text : dim;
      g.font = (cardinal ? "700 " + Math.max(15, w * 0.038) : "500 " + Math.max(11, w * 0.024)) +
               "px -apple-system, sans-serif";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(cardinal || String(d), cx + rl * Math.cos(a), cy + rl * Math.sin(a));
    }
  }

  /* target needle (dashed amber) — drag preview wins over the committed one */
  const tgt = S.dragTarget != null ? S.dragTarget : S.target;
  if (tgt != null) {
    const a = (tgt - 90) * Math.PI / 180;
    g.save();
    g.strokeStyle = cssVar("--target"); g.lineWidth = 3; g.setLineDash([7, 6]);
    g.beginPath(); g.moveTo(cx, cy);
    g.lineTo(cx + (R - 20) * Math.cos(a), cy + (R - 20) * Math.sin(a));
    g.stroke();
    g.restore();
    g.fillStyle = cssVar("--target");
    g.beginPath();
    g.arc(cx + (R - 20) * Math.cos(a), cy + (R - 20) * Math.sin(a), 5, 0, Math.PI * 2);
    g.fill();
  }

  /* heading needle */
  if (S.heading != null) {
    const a = (S.heading - 90) * Math.PI / 180;
    const color = S.moving ? cssVar("--needle-moving") : cssVar("--needle");
    const tipX = cx + (R - 24) * Math.cos(a), tipY = cy + (R - 24) * Math.sin(a);
    const backX = cx - 22 * Math.cos(a), backY = cy - 22 * Math.sin(a);
    const side = 9, pa = a + Math.PI / 2;
    g.fillStyle = color;
    g.beginPath();
    g.moveTo(tipX, tipY);
    g.lineTo(cx + side * Math.cos(pa), cy + side * Math.sin(pa));
    g.lineTo(backX, backY);
    g.lineTo(cx - side * Math.cos(pa), cy - side * Math.sin(pa));
    g.closePath(); g.fill();
  }

  /* hub */
  g.fillStyle = cssVar("--panel2"); g.strokeStyle = line; g.lineWidth = 2;
  g.beginPath(); g.arc(cx, cy, 7, 0, Math.PI * 2); g.fill(); g.stroke();

  if (S.dragTarget != null) {
    g.fillStyle = cssVar("--target");
    g.font = "700 " + Math.max(18, w * 0.05) + "px -apple-system, sans-serif";
    g.textAlign = "center";
    g.fillText(String(Math.round(S.dragTarget)).padStart(3, "0") + "°", cx, cy + R * 0.4);
  }
}

/* drag on the rose: preview while dragging, slew on release, Esc cancels */
let dragging = false;
function pointDeg(ev) {
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left - rect.width / 2;
  const y = ev.clientY - rect.top - rect.height / 2;
  return ((Math.atan2(y, x) * 180 / Math.PI) + 90 + 360) % 360;
}
canvas.addEventListener("pointerdown", ev => {
  dragging = true; canvas.setPointerCapture(ev.pointerId);
  S.dragTarget = Math.round(pointDeg(ev)); draw();
});
canvas.addEventListener("pointermove", ev => {
  if (!dragging) return;
  S.dragTarget = Math.round(pointDeg(ev)); draw();
});
canvas.addEventListener("pointerup", ev => {
  if (!dragging) return;
  dragging = false;
  const deg = Math.round(pointDeg(ev));
  S.dragTarget = null;
  slew(deg);
});
canvas.addEventListener("pointercancel", () => { dragging = false; S.dragTarget = null; draw(); });

/* ---------------------------------------------------------------- actions */
function slew(deg) {
  deg = Math.round(deg);
  const max = S.cfg ? S.cfg.max_heading : 359;
  if (!(deg >= 0 && deg <= max)) { toast("Heading must be 0–" + max); return; }
  $("target").value = deg;
  api("/api/goto", {heading: deg});
}
function stopNow() { S.dragTarget = null; dragging = false; api("/api/stop"); draw(); }

$("goBtn").onclick = () => { const v = parseInt($("target").value, 10);
                             if (!isNaN(v)) slew(v); };
$("target").addEventListener("keydown", e => { if (e.key === "Enter") $("goBtn").click(); });
$("stopBtn").onclick = stopNow;
$("jogCW").onclick  = () => api("/api/jog", {dir: "cw"});
$("jogCCW").onclick = () => api("/api/jog", {dir: "ccw"});
$("connBtn").onclick = () => {
  const on = S.link === "connected" || S.link === "connecting" || S.link === "reconnecting";
  api(on ? "/api/disconnect" : "/api/connect");
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if ($("settingsDlg").open) return;
    stopNow();
  } else if (e.key === "Enter" && document.activeElement === document.body) {
    $("goBtn").click();
  }
});

/* console visibility + raw toggle */
$("consoleToggle").onclick = () => $("consoleCard").classList.toggle("visible");
$("clearBtn").onclick = () => { $("console").innerHTML = ""; };
$("rawChk").onchange = e => {
  S.raw = e.target.checked;
  api("/api/config", {show_raw_traffic: S.raw});
};
$("themeBtn").onclick = () => {
  const dark = document.documentElement.dataset.theme !== "light";
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  api("/api/config", {dark_mode: !dark});
  draw();
};

/* ---------------------------------------------------------------- settings */
$("settingsBtn").onclick = () => {
  if (!S.cfg) return;
  $("setHost").value = S.cfg.host; $("setPort").value = S.cfg.port;
  $("setTransport").value = S.cfg.transport || "auto";
  $("setUnit").value = S.cfg.unit; $("setMax").value = S.cfg.max_heading;
  $("setPoll").value = S.cfg.poll_interval;
  $("setPresets").value = S.cfg.presets.map(p => p.name + ", " + p.heading).join("\n");
  $("settingsDlg").showModal();
};
$("setCancel").onclick = () => $("settingsDlg").close();
$("setSave").onclick = async () => {
  const presets = [];
  for (const raw of $("setPresets").value.split("\n")) {
    const m = raw.match(/^\s*(.+?)\s*,\s*(\d+)\s*$/);
    if (m) presets.push({name: m[1].slice(0, 12), heading: parseInt(m[2], 10)});
  }
  const ok = await api("/api/config", {
    host: $("setHost").value.trim(), port: parseInt($("setPort").value, 10) || 6555,
    transport: $("setTransport").value,
    unit: parseInt($("setUnit").value, 10) || 1,
    max_heading: parseInt($("setMax").value, 10) || 359,
    poll_interval: parseFloat($("setPoll").value) || 1.0,
    presets,
  });
  if (ok) $("settingsDlg").close();
};

window.addEventListener("resize", draw);
connectEvents();
draw();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: "Optional[list[str]]" = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} {APP_VERSION} (web edition)")
    ap.add_argument("--host", help="RT-21 controller host (overrides saved setting)")
    ap.add_argument("--port", type=int, help="RT-21 controller TCP port")
    ap.add_argument("--unit", type=int, help="rotator/unit digit (0-9)")
    ap.add_argument("--demo", action="store_true", help="run against a built-in simulator")
    ap.add_argument("--listen", default="127.0.0.1",
                    help="address the web UI binds to (default 127.0.0.1; "
                         "use 0.0.0.0 to reach it from other devices)")
    ap.add_argument("--http-port", type=int, help="web UI port (default from config, 8721)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    ap.add_argument("--reset-config", action="store_true", help="start from factory settings")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return ap.parse_args(argv)


def main(argv: "Optional[list[str]]" = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    if args.reset_config and CONFIG_PATH.exists():
        try:
            CONFIG_PATH.unlink()
            LOG.info("Removed %s", CONFIG_PATH)
        except OSError:
            LOG.warning("Could not remove %s", CONFIG_PATH)

    cfg = Config.load()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.unit is not None:
        cfg.unit = args.unit
    if args.http_port:
        cfg.http_port = args.http_port
    cfg.sanitize()

    sim: Optional[Rt21Simulator] = None
    if args.demo:
        sim = Rt21Simulator()
        sim.start()
        cfg.host, cfg.port = "127.0.0.1", sim.port
        cfg.transport = "tcp"       # the simulator is raw TCP; skip the probe
        # Demo settings changes stay in memory: never write the simulator's
        # address (or anything else) over the user's real config file.
        cfg.save = lambda: None  # type: ignore[method-assign]
        LOG.info("Demo mode: simulator listening on 127.0.0.1:%d", sim.port)

    hub = Hub()
    LOG.addHandler(HubLogHandler(hub))
    manager = LinkManager(cfg, hub)

    Handler.ctx = AppContext(cfg, hub, manager)
    try:
        httpd = ThreadingHTTPServer((args.listen, cfg.http_port), Handler)
    except OSError as exc:
        LOG.error("Cannot bind web UI to %s:%d: %s", args.listen, cfg.http_port, exc)
        return 1
    httpd.daemon_threads = True

    display_host = "127.0.0.1" if args.listen in ("127.0.0.1", "localhost") else args.listen
    url = f"http://{display_host}:{cfg.http_port}/"
    LOG.info("Web UI on %s", url)
    if args.listen not in ("127.0.0.1", "localhost"):
        LOG.info("Listening on all interfaces — anyone on this network can turn the rotator")

    if cfg.auto_connect_on_start:
        manager.connect()

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        LOG.info("Interrupted — shutting down")
    finally:
        httpd.server_close()
        manager.disconnect()
        if sim is not None:
            sim.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
