#!/usr/bin/env python3
"""
RT-21 Rotator Controller — a durable, cross-platform desktop client for the
Green Heron Engineering RT-21 rotator controller over its network (TCP) port.

Design goals
------------
* Correctness  : commands and replies follow RT-21 Manual Appendix F exactly.
* Durability   : all socket I/O lives in one worker thread with an outbound
                 queue, automatic reconnect with backoff, and a stale-data
                 watchdog. No unhandled exception can take the window down.
* Portability  : pure PyQt6 + stdlib. Config and logs go to the correct
                 per-platform locations on macOS, Windows and Linux.

Protocol reference (RT-21 Manual, Appendix F — "n" is the rotator/unit digit):

    ;                   stop immediately (also clears the controller's buffer)
    STn;                stop immediately
    AIn;                read heading      -> "xxx;"            (no SOH prefix)
    BIn;                read heading .1   -> "xxx.y;"          (SOH prefixed)
    APnxxx;             set target for the next AMn;
    APnxxx<CR>;         slew to xxx immediately (note the carriage return)
    AMn;                slew to the last APn target
    AAn; / ABn;         jog CCW / CW for 1.5 s, re-triggerable
    R1n;                read model + firmware -> <SOH>"RT-21 Version X.Y";
    R2n;                read heading + status -> <SOH>"xxx s;"  s=1 running, 2 stopped

    Every reply that carries a value EXCEPT AIn; is prefixed with SOH (0x01),
    which is what lets this client interleave AIn; and R2n; polls safely.

Usage
-----
    python3 rt21_controller.py                 # normal run
    python3 rt21_controller.py --host 192.168.7.203 --port 6555
    python3 rt21_controller.py --demo          # run against a built-in simulator
    python3 rt21_controller.py --reset-config  # start from factory settings
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import select
import socket
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import (
    QObject,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "RT-21 Controller"
APP_SLUG = "rt21-controller"
APP_VERSION = "2.0.0"
ORG_NAME = "GreenHeron"

SOH = "\x01"
LOG = logging.getLogger(APP_SLUG)


# --------------------------------------------------------------------------- #
# Paths, configuration, logging
# --------------------------------------------------------------------------- #
def _base_dir(kind: str) -> Path:
    """Return a per-user config/log directory that is correct on every OS.

    Qt's QStandardPaths is authoritative, but this must also work before a
    QApplication exists (and if Qt ever returns nothing), so there is a plain
    stdlib fallback for each platform.
    """
    try:
        from PyQt6.QtCore import QStandardPaths

        loc = (
            QStandardPaths.StandardLocation.AppConfigLocation
            if kind == "config"
            else QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        path = QStandardPaths.writableLocation(loc)
        if path:
            return Path(path)
    except Exception:  # pragma: no cover - defensive
        pass

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


@dataclass
class Config:
    """Everything that survives a restart. Written atomically as JSON."""

    host: str = "192.168.7.203"
    port: int = 6555
    unit: int = 1
    poll_interval: float = 1.0          # seconds between AIn; polls
    stale_timeout: float = 6.0          # no heading for this long -> force reconnect
    connect_timeout: float = 5.0
    auto_reconnect: bool = True
    reconnect_max_delay: float = 15.0
    max_heading: int = 359              # 359, or up to 449 for overlap rotators
    confirm_large_moves: bool = False
    large_move_threshold: int = 180
    auto_connect_on_start: bool = True
    compass_image: str = ""             # optional rose image; empty = drawn rose
    dark_mode: bool = True
    show_console: bool = True
    show_raw_traffic: bool = False
    presets: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_PRESETS))
    window_geometry: str = ""           # base64 from QMainWindow.saveGeometry()

    # -- persistence -------------------------------------------------------- #
    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            if CONFIG_PATH.exists():
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                known = {f for f in cls().__dataclass_fields__}  # type: ignore[attr-defined]
                for key, value in raw.items():
                    if key in known:
                        setattr(cfg, key, value)
                LOG.info("Loaded configuration from %s", CONFIG_PATH)
        except Exception:
            LOG.exception("Configuration unreadable; falling back to defaults")
        cfg.sanitize()
        return cfg

    def sanitize(self) -> None:
        """Clamp anything a hand-edited config file could get wrong."""
        self.port = _clamp(int(self.port or 6555), 1, 65535)
        self.unit = _clamp(int(self.unit or 1), 0, 9)
        self.poll_interval = _clampf(float(self.poll_interval), 0.2, 10.0)
        self.stale_timeout = _clampf(float(self.stale_timeout), 2.0, 120.0)
        self.connect_timeout = _clampf(float(self.connect_timeout), 1.0, 60.0)
        self.reconnect_max_delay = _clampf(float(self.reconnect_max_delay), 1.0, 300.0)
        self.max_heading = _clamp(int(self.max_heading), 359, 719)
        self.large_move_threshold = _clamp(int(self.large_move_threshold), 1, 360)
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
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, CONFIG_PATH)
            LOG.debug("Configuration saved to %s", CONFIG_PATH)
        except Exception:
            LOG.exception("Could not save configuration to %s", CONFIG_PATH)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _clampf(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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


class QtLogBridge(logging.Handler, QObject):
    """Feeds Python log records into the in-app console pane."""

    record_emitted = pyqtSignal(int, str)

    def __init__(self) -> None:
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self.setLevel(logging.DEBUG)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.record_emitted.emit(record.levelno, record.getMessage())
        except Exception:  # pragma: no cover - the log must never crash the app
            pass


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
        immediate move from merely loading a target; the old client omitted it.
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


# --------------------------------------------------------------------------- #
# Transport — one worker thread owns the socket
# --------------------------------------------------------------------------- #
class LinkState:
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class RotatorLink(QThread):
    """Owns the TCP connection to the RT-21 for the lifetime of the app.

    Nothing outside this thread ever touches the socket. The GUI submits
    command strings through a queue; headings and state come back as signals.
    Losing the link is a normal event, not an error: the thread reconnects on
    an exponential backoff and keeps polling.
    """

    state_changed = pyqtSignal(str, str)       # LinkState, human-readable detail
    heading_received = pyqtSignal(float)
    motion_changed = pyqtSignal(bool)
    traffic = pyqtSignal(str, str)             # "tx"/"rx", payload
    info_received = pyqtSignal(str)

    def __init__(self, cfg: Config, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._proto = Protocol(cfg.unit)
        self._outbox: "queue.Queue[str]" = queue.Queue(maxsize=200)
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._state = LinkState.DISCONNECTED
        self._last_heading_at = 0.0
        self._last_heading: Optional[float] = None
        self._moving = False
        self._poll_count = 0

    # -- public API (safe to call from the GUI thread) ---------------------- #
    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state == LinkState.CONNECTED

    @property
    def protocol(self) -> Protocol:
        return self._proto

    def submit(self, command: str | list[str]) -> None:
        """Queue one or more commands for the worker thread to transmit."""
        commands = [command] if isinstance(command, str) else list(command)
        for item in commands:
            try:
                self._outbox.put_nowait(item)
            except queue.Full:
                LOG.warning("Command queue full; dropped %r", item)

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Ask the worker to exit and wait briefly for it."""
        self._quit.set()
        self._wake.set()
        self._close_socket()
        if not self.wait(timeout_ms):
            LOG.warning("Link thread did not exit within %d ms; terminating", timeout_ms)
            self.terminate()
            self.wait(500)

    # -- worker ------------------------------------------------------------- #
    def run(self) -> None:  # noqa: C901 - a connection loop is inherently branchy
        delay = 1.0
        while not self._quit.is_set():
            if self._connect_once():
                delay = 1.0
                self._pump()                      # blocks until the link drops
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

        self._last_heading_at = time.monotonic()
        self._poll_count = 0
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

            # transmit anything the GUI queued
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
                self._poll_count += 1
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
            self.traffic.emit("rx", reply.raw.replace(SOH, "<SOH>") + ";")
            if reply.kind in ("heading", "heading_status") and reply.heading is not None:
                self._last_heading_at = time.monotonic()
                self._last_heading = reply.heading
                self.heading_received.emit(reply.heading)
                if reply.moving is not None and reply.moving != self._moving:
                    self._moving = reply.moving
                    self.motion_changed.emit(reply.moving)
            elif reply.kind == "info":
                LOG.info("Controller: %s", reply.text)
                self.info_received.emit(reply.text)
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
        self.traffic.emit("tx", command.replace("\r", "<CR>"))
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
        self.state_changed.emit(state, detail)


# --------------------------------------------------------------------------- #
# Compass
# --------------------------------------------------------------------------- #
class CompassWidget(QWidget):
    """A vector compass rose with a live needle and a ghosted target needle.

    Drawn entirely with QPainter so it is crisp at any size and on any HiDPI
    display, with an optional user-supplied rose image underneath.
    """

    def __init__(self, dark: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._heading: Optional[float] = None
        self._target: Optional[int] = None
        self._moving = False
        self._dark = dark
        self._backdrop = QPixmap()

    # -- state -------------------------------------------------------------- #
    def set_heading(self, heading: Optional[float]) -> None:
        self._heading = heading
        self.update()

    def set_target(self, target: Optional[int]) -> None:
        self._target = target
        self.update()

    def set_moving(self, moving: bool) -> None:
        self._moving = moving
        self.update()

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    def load_backdrop(self, path: str) -> bool:
        """Load an optional rose image. Explicit path only — never scan a folder."""
        self._backdrop = QPixmap()
        if not path:
            self.update()
            return True
        ok = self._backdrop.load(path)
        if not ok:
            LOG.warning("Could not load compass image: %s", path)
            self._backdrop = QPixmap()
        self.update()
        return ok

    # -- painting ----------------------------------------------------------- #
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

            size = min(self.width(), self.height())
            radius = size / 2 - 14
            cx, cy = self.width() / 2, self.height() / 2
            painter.translate(cx, cy)

            ink = QColor("#e9edf2") if self._dark else QColor("#1d2430")
            faint = QColor(ink)
            faint.setAlpha(70)
            face = QColor("#161b22") if self._dark else QColor("#ffffff")
            rim = QColor("#2c3542") if self._dark else QColor("#c9d2dd")

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(face)
            painter.drawEllipse(int(-radius), int(-radius), int(radius * 2), int(radius * 2))

            if not self._backdrop.isNull():
                side = int(radius * 2)
                scaled = self._backdrop.scaled(
                    side,
                    side,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                painter.save()
                clip = QPainterPath()
                clip.addEllipse(-radius, -radius, radius * 2, radius * 2)
                painter.setClipPath(clip)
                painter.setOpacity(0.85)
                painter.drawPixmap(-scaled.width() // 2, -scaled.height() // 2, scaled)
                painter.restore()

            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(rim, 2))
            painter.drawEllipse(int(-radius), int(-radius), int(radius * 2), int(radius * 2))

            self._draw_ticks(painter, radius, ink, faint)
            if self._target is not None:
                self._draw_target(painter, radius)
            if self._heading is not None:
                self._draw_needle(painter, radius)
            self._draw_hub(painter, ink)
            self._draw_readout(painter, radius, ink)
        finally:
            painter.end()

    def _draw_ticks(self, painter: QPainter, radius: float, ink: QColor, faint: QColor) -> None:
        label_font = QFont(self.font())
        label_font.setPointSizeF(max(7.0, radius * 0.068))
        cardinal_font = QFont(label_font)
        cardinal_font.setBold(True)
        cardinal_font.setPointSizeF(max(9.0, radius * 0.105))

        for degree in range(0, 360, 5):
            painter.save()
            painter.rotate(degree)
            if degree % 30 == 0:
                painter.setPen(QPen(ink, 2))
                painter.drawLine(0, int(-radius + 2), 0, int(-radius + 13))
            elif degree % 10 == 0:
                painter.setPen(QPen(faint, 1.5))
                painter.drawLine(0, int(-radius + 2), 0, int(-radius + 9))
            else:
                painter.setPen(QPen(faint, 1))
                painter.drawLine(0, int(-radius + 2), 0, int(-radius + 5))
            painter.restore()

        cardinals = {0: "N", 90: "E", 180: "S", 270: "W"}
        for degree in range(0, 360, 30):
            text = cardinals.get(degree, str(degree))
            is_cardinal = degree in cardinals
            painter.setFont(cardinal_font if is_cardinal else label_font)
            painter.setPen(QPen(ink if is_cardinal else faint))
            metrics = QFontMetrics(painter.font())
            # Sit the label inside the tick ring, offset by half its own size so
            # long labels ("330") never spill over the rim.
            rect_w = metrics.horizontalAdvance(text)
            angle = math.radians(degree)
            ring = radius - 18 - max(rect_w, metrics.capHeight()) / 2 - 4
            x = ring * math.sin(angle)
            y = -ring * math.cos(angle)
            painter.drawText(
                int(x - rect_w / 2),
                int(y + metrics.capHeight() / 2),
                text,
            )

    def _draw_target(self, painter: QPainter, radius: float) -> None:
        painter.save()
        painter.rotate(float(self._target or 0))
        pen = QPen(QColor("#f2a63b"), 2.5, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(0, 0, 0, int(-radius + 16))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f2a63b"))
        painter.drawEllipse(-4, int(-radius + 10), 8, 8)
        painter.restore()

    def _draw_needle(self, painter: QPainter, radius: float) -> None:
        painter.save()
        painter.rotate(float(self._heading or 0.0))
        colour = QColor("#e5484d") if not self._moving else QColor("#3fb950")
        tip = radius - 18

        path = QPainterPath()
        path.moveTo(0, -tip)
        path.lineTo(7, 10)
        path.lineTo(0, 3)
        path.lineTo(-7, 10)
        path.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawPath(path)

        tail = QColor("#8b949e")
        painter.setBrush(tail)
        path = QPainterPath()
        path.moveTo(0, tip * 0.28)
        path.lineTo(5, -6)
        path.lineTo(0, -1)
        path.lineTo(-5, -6)
        path.closeSubpath()
        painter.drawPath(path)
        painter.restore()

    def _draw_hub(self, painter: QPainter, ink: QColor) -> None:
        painter.setPen(QPen(ink, 1.5))
        painter.setBrush(QColor("#0d1117") if self._dark else QColor("#3a4553"))
        painter.drawEllipse(-6, -6, 12, 12)

    def _draw_readout(self, painter: QPainter, radius: float, ink: QColor) -> None:
        text = "---°" if self._heading is None else f"{self._heading:.0f}°"
        font = QFont(self.font())
        font.setPointSizeF(max(11.0, radius * 0.16))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(ink))
        metrics = QFontMetrics(font)
        width = metrics.horizontalAdvance(text)
        painter.drawText(int(-width / 2), int(radius * 0.62), text)


# --------------------------------------------------------------------------- #
# Small widgets
# --------------------------------------------------------------------------- #
class StatusPill(QLabel):
    """Colour-coded connection state, always visible in the header."""

    COLOURS = {
        LinkState.CONNECTED: ("#0f2f1c", "#3fb950"),
        LinkState.CONNECTING: ("#332a10", "#d29922"),
        LinkState.RECONNECTING: ("#332a10", "#d29922"),
        LinkState.ERROR: ("#3a1518", "#f85149"),
        LinkState.DISCONNECTED: ("#21262d", "#8b949e"),
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_state(LinkState.DISCONNECTED, "Disconnected")

    def set_state(self, state: str, detail: str) -> None:
        background, foreground = self.COLOURS.get(state, self.COLOURS[LinkState.DISCONNECTED])
        self.setText(f"  ●  {detail}  ")
        self.setStyleSheet(
            f"background:{background}; color:{foreground}; border-radius:11px;"
            f"padding:4px 10px; font-weight:600;"
        )


class ConsolePane(QWidget):
    """Filterable log view. Bounded so a long session cannot eat memory."""

    LEVELS = [("Debug", logging.DEBUG), ("Info", logging.INFO), ("Warnings", logging.WARNING)]

    def __init__(self, show_raw: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._min_level = logging.INFO

        self.level_box = QComboBox()
        for label, level in self.LEVELS:
            self.level_box.addItem(label, level)
        self.level_box.setCurrentIndex(1)
        self.level_box.currentIndexChanged.connect(self._level_changed)

        self.raw_box = QCheckBox("Show wire traffic")
        self.raw_box.setChecked(show_raw)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(lambda: self.view.clear())

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(2000)
        self.view.setFont(_mono_font())

        header = QHBoxLayout()
        header.addWidget(QLabel("Console"))
        header.addStretch(1)
        header.addWidget(self.raw_box)
        header.addWidget(self.level_box)
        header.addWidget(clear_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.view)

    def _level_changed(self) -> None:
        self._min_level = self.level_box.currentData()

    def append_log(self, level: int, message: str) -> None:
        if level < self._min_level:
            return
        tag = {logging.DEBUG: "DBG", logging.INFO: "INF", logging.WARNING: "WRN"}.get(
            level, "ERR" if level >= logging.ERROR else "INF"
        )
        self.view.appendPlainText(f"{time.strftime('%H:%M:%S')} {tag}  {message}")

    def append_traffic(self, direction: str, payload: str) -> None:
        if not self.raw_box.isChecked():
            return
        arrow = "→" if direction == "tx" else "←"
        self.view.appendPlainText(f"{time.strftime('%H:%M:%S')} {arrow}    {payload}")


def _mono_font() -> QFont:
    font = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font


# --------------------------------------------------------------------------- #
# Dialogs
# --------------------------------------------------------------------------- #
class PreferencesDialog(QDialog):
    def __init__(self, cfg: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(420)
        self._cfg = cfg

        self.host = QLineEdit(cfg.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(cfg.port)
        self.unit = QSpinBox()
        self.unit.setRange(0, 9)
        self.unit.setValue(cfg.unit)
        self.unit.setToolTip("Rotator/unit digit used in every command (AI1; vs AI2;)")

        self.poll = QDoubleSpinBox()
        self.poll.setRange(0.2, 10.0)
        self.poll.setSingleStep(0.1)
        self.poll.setSuffix(" s")
        self.poll.setValue(cfg.poll_interval)

        self.stale = QDoubleSpinBox()
        self.stale.setRange(2.0, 120.0)
        self.stale.setSuffix(" s")
        self.stale.setValue(cfg.stale_timeout)

        self.max_heading = QSpinBox()
        self.max_heading.setRange(359, 719)
        self.max_heading.setValue(cfg.max_heading)
        self.max_heading.setToolTip("Raise above 359 only for overlap rotators")

        self.auto_reconnect = QCheckBox("Reconnect automatically")
        self.auto_reconnect.setChecked(cfg.auto_reconnect)
        self.auto_connect = QCheckBox("Connect on launch")
        self.auto_connect.setChecked(cfg.auto_connect_on_start)
        self.confirm = QCheckBox("Confirm moves larger than")
        self.confirm.setChecked(cfg.confirm_large_moves)
        self.threshold = QSpinBox()
        self.threshold.setRange(1, 360)
        self.threshold.setSuffix("°")
        self.threshold.setValue(cfg.large_move_threshold)

        self.image = QLineEdit(cfg.compass_image)
        self.image.setPlaceholderText("optional compass rose image")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.image.clear())
        image_row = QHBoxLayout()
        image_row.addWidget(self.image, 1)
        image_row.addWidget(browse)
        image_row.addWidget(clear)

        confirm_row = QHBoxLayout()
        confirm_row.addWidget(self.confirm)
        confirm_row.addWidget(self.threshold)
        confirm_row.addStretch(1)

        form = QFormLayout()
        form.addRow("Controller host", self.host)
        form.addRow("Port", self.port)
        form.addRow("Rotator unit", self.unit)
        form.addRow("Poll interval", self.poll)
        form.addRow("Stale timeout", self.stale)
        form.addRow("Maximum heading", self.max_heading)
        form.addRow("", self.auto_reconnect)
        form.addRow("", self.auto_connect)
        form.addRow("", confirm_row)
        form.addRow("Compass image", image_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        hint = QLabel(f"Settings file: {CONFIG_PATH}\nLog file: {LOG_PATH}")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b949e; font-size:11px;")
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose compass image", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if path:
            self.image.setText(path)

    def apply_to(self, cfg: Config) -> None:
        cfg.host = self.host.text().strip() or cfg.host
        cfg.port = self.port.value()
        cfg.unit = self.unit.value()
        cfg.poll_interval = self.poll.value()
        cfg.stale_timeout = self.stale.value()
        cfg.max_heading = self.max_heading.value()
        cfg.auto_reconnect = self.auto_reconnect.isChecked()
        cfg.auto_connect_on_start = self.auto_connect.isChecked()
        cfg.confirm_large_moves = self.confirm.isChecked()
        cfg.large_move_threshold = self.threshold.value()
        cfg.compass_image = self.image.text().strip()
        cfg.sanitize()


class PresetsDialog(QDialog):
    def __init__(self, presets: list[dict[str, Any]], max_heading: int,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit beam headings")
        self.setMinimumSize(360, 340)
        self._max = max_heading

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Label", "Heading"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for preset in presets:
            self._add_row(preset["name"], preset["heading"])

        add = QPushButton("Add")
        add.clicked.connect(lambda: self._add_row("New", 0))
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_row)
        restore = QPushButton("Restore defaults")
        restore.clicked.connect(self._restore)

        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        row.addWidget(restore)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _add_row(self, name: str, heading: int) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(name)))
        self.table.setItem(row, 1, QTableWidgetItem(str(heading)))

    def _remove_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _restore(self) -> None:
        self.table.setRowCount(0)
        for preset in DEFAULT_PRESETS:
            self._add_row(preset["name"], preset["heading"])

    def presets(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            heading_item = self.table.item(row, 1)
            if not name_item or not heading_item:
                continue
            name = name_item.text().strip()[:12]
            try:
                heading = _clamp(int(float(heading_item.text().strip())), 0, self._max)
            except ValueError:
                continue
            if name:
                result.append({"name": name, "heading": heading})
        return result


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, log_bridge: QtLogBridge) -> None:
        super().__init__()
        self.cfg = cfg
        self.link: Optional[RotatorLink] = None
        self._heading: Optional[float] = None
        self._target: Optional[int] = None
        self._moving = False
        self._last_update = 0.0

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setMinimumSize(QSize(820, 560))
        self._build_ui()
        self._build_menu()
        self._build_shortcuts()
        self._apply_theme()

        log_bridge.record_emitted.connect(self.console.append_log)

        if cfg.window_geometry:
            try:
                from PyQt6.QtCore import QByteArray

                self.restoreGeometry(QByteArray.fromBase64(cfg.window_geometry.encode()))
            except Exception:
                LOG.debug("Stored window geometry was unusable")

        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh_freshness)
        self._ui_timer.start(1000)

        if cfg.auto_connect_on_start:
            QTimer.singleShot(300, self.connect_link)

    # -- construction ------------------------------------------------------- #
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(14, 12, 14, 10)
        outer.setSpacing(10)

        # header ------------------------------------------------------------ #
        self.host_edit = QLineEdit(self.cfg.host)
        self.host_edit.setFixedWidth(150)
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(self.cfg.port)
        self.port_edit.setFixedWidth(85)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.pill = StatusPill()

        header = QHBoxLayout()
        header.addWidget(QLabel("Controller"))
        header.addWidget(self.host_edit)
        header.addWidget(QLabel("Port"))
        header.addWidget(self.port_edit)
        header.addWidget(self.connect_btn)
        header.addStretch(1)
        header.addWidget(self.pill)
        outer.addLayout(header)

        # body -------------------------------------------------------------- #
        body = QHBoxLayout()
        body.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(10)

        self.heading_lbl = QLabel("---°")
        heading_font = QFont(self.font())
        heading_font.setPointSize(46)
        heading_font.setBold(True)
        self.heading_lbl.setFont(heading_font)
        self.heading_lbl.setObjectName("heading")

        self.sub_lbl = QLabel("waiting for controller")
        self.sub_lbl.setObjectName("subtle")

        readout = QVBoxLayout()
        readout.setSpacing(0)
        readout.addWidget(self.heading_lbl)
        readout.addWidget(self.sub_lbl)
        left.addLayout(readout)

        # target row
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText(f"0–{self.cfg.max_heading}")
        self.target_edit.setFixedWidth(90)
        self.target_edit.returnPressed.connect(self.slew_to_entry)
        self.slew_btn = QPushButton("Slew")
        self.slew_btn.setObjectName("primary")
        self.slew_btn.clicked.connect(self.slew_to_entry)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target"))
        target_row.addWidget(self.target_edit)
        target_row.addWidget(self.slew_btn)
        target_row.addStretch(1)
        left.addLayout(target_row)

        # jog + stop
        self.ccw_btn = QPushButton("◀  CCW")
        self.ccw_btn.setToolTip("Jog counter-clockwise for 1.5 s (AAn;)")
        self.ccw_btn.clicked.connect(lambda: self._send(self.protocol.jog_ccw()))
        self.cw_btn = QPushButton("CW  ▶")
        self.cw_btn.setToolTip("Jog clockwise for 1.5 s (ABn;)")
        self.cw_btn.clicked.connect(lambda: self._send(self.protocol.jog_cw()))
        jog_row = QHBoxLayout()
        jog_row.addWidget(self.ccw_btn)
        jog_row.addWidget(self.cw_btn)
        left.addLayout(jog_row)

        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setMinimumHeight(46)
        self.stop_btn.setToolTip("Stop rotation immediately  (Esc)")
        self.stop_btn.clicked.connect(self.stop_rotation)
        left.addWidget(self.stop_btn)

        # presets
        self.presets_box = QGroupBox("Beam headings")
        self.presets_grid = QGridLayout(self.presets_box)
        self.presets_grid.setSpacing(6)
        left.addWidget(self.presets_box)
        self._rebuild_presets()

        left.addStretch(1)

        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setFixedWidth(290)
        body.addWidget(left_widget)

        self.compass = CompassWidget(dark=self.cfg.dark_mode)
        self.compass.load_backdrop(self.cfg.compass_image)
        body.addWidget(self.compass, 1)

        body_widget = QWidget()
        body_widget.setLayout(body)

        # console ------------------------------------------------------------ #
        self.console = ConsolePane(self.cfg.show_raw_traffic)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(body_widget)
        self.splitter.addWidget(self.console)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([420, 150])
        self.console.setVisible(self.cfg.show_console)
        outer.addWidget(self.splitter, 1)

        self.statusBar().showMessage("Ready")
        self._set_controls_enabled(False)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        prefs = QAction("Preferences…", self)
        prefs.setShortcut(QKeySequence.StandardKey.Preferences)
        prefs.triggered.connect(self.open_preferences)
        file_menu.addAction(prefs)

        presets = QAction("Edit beam headings…", self)
        presets.triggered.connect(self.open_presets)
        file_menu.addAction(presets)

        file_menu.addSeparator()
        open_logs = QAction("Reveal log file…", self)
        open_logs.triggered.connect(self._reveal_logs)
        file_menu.addAction(open_logs)

        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")
        self.console_action = QAction("Show console", self, checkable=True)
        self.console_action.setChecked(self.cfg.show_console)
        self.console_action.triggered.connect(self._toggle_console)
        view_menu.addAction(self.console_action)

        self.dark_action = QAction("Dark mode", self, checkable=True)
        self.dark_action.setChecked(self.cfg.dark_mode)
        self.dark_action.triggered.connect(self._toggle_dark)
        view_menu.addAction(self.dark_action)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("About", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.stop_rotation)
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.toggle_connection)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=lambda: self.target_edit.setFocus())

    # -- theming ------------------------------------------------------------ #
    def _apply_theme(self) -> None:
        dark = self.cfg.dark_mode
        palette = {
            "bg": "#0d1117" if dark else "#f4f6f9",
            "panel": "#161b22" if dark else "#ffffff",
            "ink": "#e9edf2" if dark else "#1d2430",
            "subtle": "#8b949e" if dark else "#5b6675",
            "line": "#2c3542" if dark else "#d4dbe4",
            "accent": "#2f81f7",
        }
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{ background:{palette['bg']}; color:{palette['ink']}; }}
            QGroupBox {{
                border:1px solid {palette['line']}; border-radius:8px;
                margin-top:12px; padding:10px 8px 8px 8px; font-weight:600;
            }}
            QGroupBox::title {{ subcontrol-origin: margin; left:10px; padding:0 4px;
                                color:{palette['subtle']}; }}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTableWidget {{
                background:{palette['panel']}; border:1px solid {palette['line']};
                border-radius:6px; padding:5px 7px; color:{palette['ink']};
            }}
            QLineEdit:focus, QSpinBox:focus {{ border-color:{palette['accent']}; }}
            QPushButton {{
                background:{palette['panel']}; border:1px solid {palette['line']};
                border-radius:6px; padding:6px 12px; color:{palette['ink']};
            }}
            QPushButton:hover {{ border-color:{palette['accent']}; }}
            QPushButton:disabled {{ color:{palette['subtle']}; border-color:{palette['line']}; }}
            QPushButton#primary {{ background:{palette['accent']}; border:none; color:#ffffff;
                                   font-weight:600; }}
            QPushButton#primary:disabled {{ background:{palette['line']}; color:{palette['subtle']}; }}
            QPushButton#stop {{ background:#b62324; border:none; color:#ffffff;
                                font-size:16px; font-weight:700; letter-spacing:1px; }}
            QPushButton#stop:disabled {{ background:{palette['line']}; color:{palette['subtle']}; }}
            QPushButton#preset {{ padding:7px 4px; font-weight:600; }}
            QLabel#heading {{ color:{palette['ink']}; }}
            QLabel#subtle {{ color:{palette['subtle']}; font-size:12px; }}
            QStatusBar {{ color:{palette['subtle']}; }}
            QSplitter::handle {{ background:{palette['line']}; height:1px; }}
            """
        )
        self.compass.set_dark(dark)

    # -- presets ------------------------------------------------------------ #
    def _rebuild_presets(self) -> None:
        while self.presets_grid.count():
            item = self.presets_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._preset_buttons: list[QPushButton] = []
        for index, preset in enumerate(self.cfg.presets):
            button = QPushButton(f"{preset['name']}\n{preset['heading']}°")
            button.setObjectName("preset")
            button.setToolTip(f"Slew to {preset['heading']}°")
            button.clicked.connect(lambda _, deg=preset["heading"]: self.slew_to(int(deg)))
            self.presets_grid.addWidget(button, index // 3, index % 3)
            self._preset_buttons.append(button)
        for button in self._preset_buttons:
            button.setEnabled(bool(self.link and self.link.connected))

    # -- link management ---------------------------------------------------- #
    @property
    def protocol(self) -> Protocol:
        return self.link.protocol if self.link else Protocol(self.cfg.unit)

    def toggle_connection(self) -> None:
        if self.link and self.link.isRunning():
            self.disconnect_link()
        else:
            self.connect_link()

    def connect_link(self) -> None:
        self.cfg.host = self.host_edit.text().strip() or self.cfg.host
        self.cfg.port = self.port_edit.value()
        self.cfg.save()

        self.disconnect_link()
        self.link = RotatorLink(self.cfg, self)
        self.link.state_changed.connect(self._on_state)
        self.link.heading_received.connect(self._on_heading)
        self.link.motion_changed.connect(self._on_motion)
        self.link.traffic.connect(self.console.append_traffic)
        self.link.info_received.connect(lambda text: self.statusBar().showMessage(text, 8000))
        self.link.start()
        self.connect_btn.setText("Disconnect")

    def disconnect_link(self) -> None:
        if self.link:
            LOG.info("Closing link")
            self.link.shutdown()
            self.link.deleteLater()
            self.link = None
        self.connect_btn.setText("Connect")
        self._set_controls_enabled(False)
        self.pill.set_state(LinkState.DISCONNECTED, "Disconnected")
        self._heading = None
        self.compass.set_heading(None)
        self.heading_lbl.setText("---°")
        self.sub_lbl.setText("disconnected")

    # -- commands ----------------------------------------------------------- #
    def _send(self, command: str | list[str]) -> None:
        if not (self.link and self.link.connected):
            self.statusBar().showMessage("Not connected", 3000)
            return
        self.link.submit(command)

    def slew_to_entry(self) -> None:
        text = self.target_edit.text().strip()
        try:
            degrees = int(round(float(text)))
        except ValueError:
            self.statusBar().showMessage(
                f"Enter a heading between 0 and {self.cfg.max_heading}", 4000
            )
            return
        self.slew_to(degrees)

    def slew_to(self, degrees: int) -> None:
        if not 0 <= degrees <= self.cfg.max_heading:
            self.statusBar().showMessage(
                f"{degrees}° is outside 0–{self.cfg.max_heading}", 4000
            )
            return
        if self.cfg.confirm_large_moves and self._heading is not None:
            delta = abs(_shortest_delta(self._heading, degrees))
            if delta >= self.cfg.large_move_threshold:
                answer = QMessageBox.question(
                    self,
                    "Confirm move",
                    f"Slew {delta:.0f}° to {degrees}°?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
        self._target = degrees
        self.compass.set_target(degrees)
        self.target_edit.setText(str(degrees))
        LOG.info("Slew to %d°", degrees)
        self._send(self.protocol.goto(degrees))

    def stop_rotation(self) -> None:
        LOG.warning("STOP")
        self._target = None
        self.compass.set_target(None)
        self._send(self.protocol.stop())

    # -- link signals ------------------------------------------------------- #
    def _on_state(self, state: str, detail: str) -> None:
        self.pill.set_state(state, detail)
        connected = state == LinkState.CONNECTED
        self._set_controls_enabled(connected)
        if state in (LinkState.ERROR, LinkState.RECONNECTING):
            self.sub_lbl.setText(detail.lower())
        self.statusBar().showMessage(detail, 6000)

    def _on_heading(self, heading: float) -> None:
        self._heading = heading
        self._last_update = time.monotonic()
        self.heading_lbl.setText(f"{heading:.0f}°")
        self.compass.set_heading(heading)
        self._update_subtitle()

    def _on_motion(self, moving: bool) -> None:
        self._moving = moving
        self.compass.set_moving(moving)
        if not moving and self._target is not None and self._heading is not None:
            if abs(_shortest_delta(self._heading, self._target)) <= 2:
                self._target = None
                self.compass.set_target(None)
        self._update_subtitle()

    def _update_subtitle(self) -> None:
        parts: list[str] = []
        if self._moving:
            parts.append("rotating")
        if self._target is not None and self._heading is not None:
            delta = _shortest_delta(self._heading, self._target)
            direction = "CW" if delta > 0 else "CCW"
            parts.append(f"{abs(delta):.0f}° {direction} to {self._target}°")
        elif not self._moving:
            parts.append("stopped")
        self.sub_lbl.setText(" · ".join(parts) if parts else "—")

    def _refresh_freshness(self) -> None:
        if not (self.link and self.link.connected) or self._last_update == 0.0:
            return
        age = time.monotonic() - self._last_update
        self.statusBar().showMessage(
            f"Last reading {age:.0f}s ago · polling every {self.cfg.poll_interval:g}s"
        )

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.slew_btn, self.stop_btn, self.ccw_btn, self.cw_btn):
            widget.setEnabled(enabled)
        for button in getattr(self, "_preset_buttons", []):
            button.setEnabled(enabled)

    # -- menu actions ------------------------------------------------------- #
    def open_preferences(self) -> None:
        dialog = PreferencesDialog(self.cfg, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            was_connected = bool(self.link and self.link.isRunning())
            dialog.apply_to(self.cfg)
            self.cfg.save()
            self.host_edit.setText(self.cfg.host)
            self.port_edit.setValue(self.cfg.port)
            self.target_edit.setPlaceholderText(f"0–{self.cfg.max_heading}")
            self.compass.load_backdrop(self.cfg.compass_image)
            self._rebuild_presets()
            if was_connected:
                LOG.info("Reconnecting to apply new settings")
                self.connect_link()

    def open_presets(self) -> None:
        dialog = PresetsDialog(self.cfg.presets, self.cfg.max_heading, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.cfg.presets = dialog.presets()
            self.cfg.sanitize()
            self.cfg.save()
            self._rebuild_presets()

    def _toggle_console(self, checked: bool) -> None:
        self.cfg.show_console = checked
        self.console.setVisible(checked)
        self.cfg.save()

    def _toggle_dark(self, checked: bool) -> None:
        self.cfg.dark_mode = checked
        self._apply_theme()
        self.cfg.save()

    def _reveal_logs(self) -> None:
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_PATH.parent)))

    def _about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br><br>"
            "Network client for the Green Heron Engineering RT-21 rotator "
            "controller, speaking the Appendix F protocol over TCP.<br><br>"
            f"<small>Settings: {CONFIG_PATH}<br>Logs: {LOG_PATH}</small>",
        )

    # -- shutdown ----------------------------------------------------------- #
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        try:
            from PyQt6.QtCore import QByteArray

            geometry: QByteArray = self.saveGeometry()
            self.cfg.window_geometry = bytes(geometry.toBase64()).decode()
            self.cfg.show_raw_traffic = self.console.raw_box.isChecked()
            self.cfg.save()
        except Exception:
            LOG.exception("Could not persist window state")
        self._ui_timer.stop()
        self.disconnect_link()
        event.accept()


def _shortest_delta(current: float, target: float) -> float:
    """Signed shortest angular distance, positive clockwise."""
    delta = (target - current + 540.0) % 360.0 - 180.0
    return delta


# --------------------------------------------------------------------------- #
# Built-in simulator (--demo) — lets the GUI be exercised with no hardware
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
        if frame == "" :
            self._target = self._heading
            return ""
        upper = frame.upper()
        if upper.startswith("AI"):
            return f"{int(self._heading) % 360:03d};"
        if upper.startswith("R2"):
            return f"{SOH}{int(self._heading) % 360:03d} {'1' if moving else '2'};"
        if upper.startswith("R1"):
            return f'{SOH}RT-21 Version 3.9 (simulated);'
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
# Entry point
# --------------------------------------------------------------------------- #
def install_excepthook(parent_getter: Callable[[], Optional[QWidget]]) -> None:
    """Log and surface unexpected exceptions instead of dying silently."""

    def hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        LOG.error("Unhandled exception:\n%s", text)
        try:
            QMessageBox.critical(
                parent_getter(),
                "Unexpected error",
                f"{exc_type.__name__}: {exc_value}\n\nDetails were written to:\n{LOG_PATH}",
            )
        except Exception:
            pass

    sys.excepthook = hook


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--host", help="controller hostname or IP (overrides saved config)")
    parser.add_argument("--port", type=int, help="controller TCP port (default 6555)")
    parser.add_argument("--unit", type=int, help="rotator/unit digit used in commands")
    parser.add_argument("--demo", action="store_true", help="run against a built-in simulator")
    parser.add_argument("--reset-config", action="store_true", help="ignore the saved config")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    setup_logging(args.verbose)
    LOG.info("%s %s starting on %s (Python %s)", APP_NAME, APP_VERSION,
             sys.platform, sys.version.split()[0])

    bridge = QtLogBridge()
    LOG.addHandler(bridge)

    if args.reset_config and CONFIG_PATH.exists():
        try:
            CONFIG_PATH.unlink()
            LOG.info("Removed %s at user request", CONFIG_PATH)
        except OSError:
            LOG.exception("Could not remove the config file")

    cfg = Config.load()

    simulator: Optional[Rt21Simulator] = None
    if args.demo:
        simulator = Rt21Simulator()
        simulator.start()
        cfg.host, cfg.port = "127.0.0.1", simulator.port
        cfg.auto_connect_on_start = True
        LOG.info("Simulator listening on 127.0.0.1:%d", simulator.port)

    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.unit is not None:
        cfg.unit = args.unit
    cfg.sanitize()

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")               # identical look on macOS, Windows, Linux

    window = MainWindow(cfg, bridge)
    install_excepthook(lambda: window)
    window.show()

    try:
        return app.exec()
    finally:
        if simulator is not None:
            simulator.shutdown()
        LOG.info("%s exiting", APP_NAME)


if __name__ == "__main__":
    raise SystemExit(main())
