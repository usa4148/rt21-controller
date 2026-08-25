"""Second verification pass: stale-link watchdog, noisy stream tolerance, and a
rendered screenshot of the window so the GUI can be eyeballed."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import rt21_controller as app  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def wait_for(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.03)
    QApplication.processEvents()
    return predicate()


class MuteServer(threading.Thread):
    """Accepts a connection and then says nothing at all — the classic hang."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self._conns: list[socket.socket] = []

    def run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self._conns.append(conn)

    def close(self) -> None:
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


def test_stale_watchdog() -> None:
    print("\n--- stale-link watchdog ---")
    server = MuteServer()
    server.start()

    cfg = app.Config()
    cfg.host, cfg.port = "127.0.0.1", server.port
    cfg.poll_interval = 0.3
    cfg.stale_timeout = 2.0
    cfg.auto_connect_on_start = False

    link = app.RotatorLink(cfg)
    states: list[str] = []
    link.state_changed.connect(lambda state, _detail: states.append(state))
    link.start()

    check("connects to a silent controller",
          wait_for(lambda: link.state == app.LinkState.CONNECTED, 5.0), link.state)
    check("watchdog tears down a silent link",
          wait_for(lambda: link.state in (app.LinkState.ERROR, app.LinkState.RECONNECTING), 8.0),
          f"states={states[-3:]}")
    link.shutdown()
    check("shuts down after the watchdog fired", not link.isRunning())
    server.close()


def test_noisy_stream() -> None:
    print("\n--- frame decoding under noise ---")
    proto = app.Protocol(1)
    link = app.RotatorLink(app.Config())
    seen: list[float] = []
    link.heading_received.connect(seen.append)

    # split frames, junk between frames, an SOH status frame, a CR/LF terminator
    leftover = link._consume("04")
    leftover = link._consume(leftover + "5;\r\n")
    leftover = link._consume(leftover + "!!;" + app.SOH + "090 1;")
    leftover = link._consume(leftover + "12")
    QApplication.processEvents()
    check("reassembles a split frame", 45.0 in seen, str(seen))
    check("ignores junk between frames", seen.count(45.0) == 1, str(seen))
    check("accepts an SOH status frame", 90.0 in seen, str(seen))
    check("holds an incomplete frame back", leftover == "12", repr(leftover))
    check("buffer never grows unbounded", len(link._consume("x" * 9000)) <= 4096)
    assert proto.read_heading() == "AI1;"


def render_screenshot(path: str) -> None:
    print("\n--- rendering ---")
    sim = app.Rt21Simulator(speed=25.0)
    sim.start()

    cfg = app.Config()
    cfg.host, cfg.port = "127.0.0.1", sim.port
    cfg.poll_interval = 0.25
    cfg.auto_connect_on_start = False
    cfg.window_geometry = ""

    window = app.MainWindow(cfg, app.QtLogBridge())
    window.resize(980, 660)
    window.show()
    window.connect_link()
    wait_for(lambda: window._heading is not None, 6.0)
    window.slew_to(115)
    wait_for(lambda: window._moving is True, 4.0)
    time.sleep(1.2)
    QApplication.processEvents()

    pixmap = window.grab()
    ok = pixmap.save(path)
    check("window renders without error", ok and not pixmap.isNull(), path)

    window.cfg.dark_mode = False
    window._apply_theme()
    QApplication.processEvents()
    light = window.grab()
    light.save(path.replace(".png", "-light.png"))
    check("light theme renders", not light.isNull())

    window.close()
    sim.shutdown()


def main() -> int:
    app.setup_logging(False)
    qt_app = QApplication(sys.argv[:1])
    qt_app.setStyle("Fusion")

    import tempfile
    from pathlib import Path

    sandbox = Path(tempfile.mkdtemp(prefix="rt21-extra-"))
    app.CONFIG_DIR = sandbox
    app.CONFIG_PATH = sandbox / "config.json"

    test_noisy_stream()
    test_stale_watchdog()
    render_screenshot("/home/claude/gh/screenshot.png")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
