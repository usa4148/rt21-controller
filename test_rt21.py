"""Verification harness for rt21_controller.py.

Runs headless (QT_QPA_PLATFORM=offscreen) against the built-in simulator and
checks the protocol encoders, the frame decoder, and the live behaviour of the
link + window: connect, poll, slew, stop, drop-and-reconnect, clean shutdown.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import rt21_controller as app  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def wait_for(predicate, timeout: float = 8.0, tick: int = 25) -> bool:
    """Spin the Qt event loop until predicate() is true or we run out of time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(tick / 1000.0)
    QApplication.processEvents()
    return predicate()


# --------------------------------------------------------------------------- #
def test_protocol() -> None:
    print("\n--- protocol ---")
    proto = app.Protocol(unit=1)
    check("AI encoding", proto.read_heading() == "AI1;", proto.read_heading())
    check("R2 encoding", proto.read_status() == "R21;", proto.read_status())
    check("goto carries CR", proto.goto(45) == "AP1045\r;", repr(proto.goto(45)))
    check("goto zero-pads", proto.goto(7) == "AP1007\r;", repr(proto.goto(7)))
    check("stop clears buffer first", proto.stop() == [";", "ST1;"], str(proto.stop()))
    check("unit digit honoured", app.Protocol(2).goto(180) == "AP2180\r;")
    check("jog CCW/CW", (proto.jog_ccw(), proto.jog_cw()) == ("AA1;", "AB1;"))

    bare = app.Protocol.decode("045")
    check("bare reply is a heading", bare.kind == "heading" and bare.heading == 45.0)

    status = app.Protocol.decode(f"{app.SOH}180 1")
    check(
        "SOH reply carries motion",
        status.kind == "heading_status" and status.heading == 180.0 and status.moving is True,
    )
    stopped = app.Protocol.decode(f"{app.SOH}1802")
    check("stopped status parsed", stopped.moving is False and stopped.heading == 180.0)

    info = app.Protocol.decode(f"{app.SOH}RT-21 Version 3.9")
    check("version reply classified as info", info.kind == "info")

    check("360 wraps to 0", app.Protocol.decode("360").heading == 0.0)
    check("garbage is not a heading", app.Protocol.decode("??!").kind in ("unknown", "info"))


def test_deltas() -> None:
    print("\n--- geometry ---")
    check("0 -> 350 is 10 CCW", app._shortest_delta(0, 350) == -10.0)
    check("350 -> 10 is 20 CW", app._shortest_delta(350, 10) == 20.0)
    check("no move is zero", app._shortest_delta(123, 123) == 0.0)


def test_config(tmp_marker: str) -> None:
    print("\n--- configuration ---")
    cfg = app.Config()
    cfg.port = 99999          # out of range
    cfg.poll_interval = 0.01  # too fast
    cfg.presets = [{"name": "OK", "heading": 400}, {"bad": "row"}]
    cfg.sanitize()
    check("port clamped", cfg.port == 65535, str(cfg.port))
    check("poll interval clamped", cfg.poll_interval == 0.2, str(cfg.poll_interval))
    check("bad preset rows dropped", len(cfg.presets) == 1, str(cfg.presets))
    check("preset heading clamped", cfg.presets[0]["heading"] == 359)

    cfg.host = tmp_marker
    cfg.save()
    reloaded = app.Config.load()
    check("config round-trips to disk", reloaded.host == tmp_marker, reloaded.host)


def test_live() -> None:
    print("\n--- live link against the simulator ---")
    sim = app.Rt21Simulator(speed=40.0)
    sim.start()

    cfg = app.Config()
    cfg.host, cfg.port = "127.0.0.1", sim.port
    cfg.poll_interval = 0.25
    cfg.stale_timeout = 3.0
    cfg.auto_connect_on_start = False
    cfg.window_geometry = ""

    bridge = app.QtLogBridge()
    window = app.MainWindow(cfg, bridge)
    window.show()

    window.connect_link()
    check("connects", wait_for(lambda: bool(window.link and window.link.connected), 6.0))
    check("receives a heading", wait_for(lambda: window._heading is not None, 6.0),
          f"heading={window._heading}")
    check("controls enabled once connected", window.slew_btn.isEnabled())

    window.slew_to(120)
    check("reports rotation while moving", wait_for(lambda: window._moving is True, 4.0),
          f"moving={window._moving}")
    check("slews to target", wait_for(lambda: window._heading is not None
                                      and abs(window._heading - 120) <= 2, 12.0),
          f"heading={window._heading}")
    check("reports stopped on arrival", wait_for(lambda: window._moving is False, 4.0),
          f"moving={window._moving}")
    check("target needle cleared on arrival",
          wait_for(lambda: window._target is None, 3.0), f"target={window._target}")

    window.slew_to(300)
    wait_for(lambda: window._moving is True, 4.0)
    window.stop_rotation()
    time.sleep(0.6)
    QApplication.processEvents()
    frozen = window._heading
    time.sleep(1.0)
    QApplication.processEvents()
    check("STOP halts rotation", frozen is not None and abs((window._heading or 0) - frozen) < 3,
          f"{frozen} -> {window._heading}")

    # reject out-of-range input
    window.target_edit.setText("999")
    window.slew_to_entry()
    check("out-of-range target refused", window._target != 999)

    # drop the link underneath the app and confirm it comes back
    print("    (killing the simulator connection to test reconnect)")
    sim.shutdown()
    check("notices the link is gone",
          wait_for(lambda: window.link is not None and not window.link.connected, 12.0),
          f"state={window.link.state if window.link else '-'}")

    sim2 = None
    for _ in range(20):
        try:
            sim2 = app.Rt21Simulator(port=sim.port, speed=40.0)
            break
        except OSError:
            time.sleep(0.25)
    check("simulator restarted on the same port", sim2 is not None)
    assert sim2 is not None
    sim2.start()
    check("reconnects on its own",
          wait_for(lambda: bool(window.link and window.link.connected), 25.0),
          f"state={window.link.state if window.link else '-'}")

    window.close()
    check("thread stops cleanly", wait_for(lambda: window.link is None, 5.0))
    sim2.shutdown()


def main() -> int:
    app.setup_logging(verbose=False)
    qt_app = QApplication(sys.argv[:1])
    qt_app.setStyle("Fusion")

    import tempfile
    from pathlib import Path

    sandbox = Path(tempfile.mkdtemp(prefix="rt21-test-"))
    app.CONFIG_DIR = sandbox
    app.CONFIG_PATH = sandbox / "config.json"

    test_deltas()
    test_protocol()
    test_config("test-marker.local")
    test_live()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
