---
title: "RT-21 Controller — Rotator Protocol Listeners"
subtitle: "Implementation plan — implemented 2026-09-29 on feat/rotator-protocols"
author: "Dan / Claude"
date: "2026-09-28"
---

# 1. What the code looks like today

Planned against `~/Documents/HAM/GreenHeron` (origin `usa4148/rt21-controller`),
checked out on `feat/n1mm-rotator-target` — one commit ahead of `main`, pushed,
not merged.

| Aspect | Current state (`rt21_web.py`, v3.0) |
|---|---|
| Link to RT-21 | TCP, not serial: raw TCP bridge **or** GH Everywhere HTTP bridge (auto-detected) |
| Threads | Already threaded: `RotatorLink` owns the link; bounded FIFO outbox; `ThreadingHTTPServer`; `N1mmBridge` UDP thread |
| GUI / headless | Browser UI served by the app; headless via `--no-browser`. Legacy PyQt client untouched |
| State | `Hub` (lock-protected snapshot + SSE fan-out to every tab) |
| Config | JSON, shared with the 2.0 client, sanitized on load |
| Dependencies | Standard library only; runs on 3.9+ (Mac has 3.13 and 3.14) |
| Tests | `test_web.py`, 33 tests against a built-in simulator |

**Conclusion:** the threading foundation already exists. The work is a motion
arbiter, two new listeners, and fixes to the N1MM bridge — not a rewrite.
No Python version bump is needed.

# 2. Problems found in the current code

1. **N1MM rotor name is never captured.** N1MM sends `<rotor>name</rotor>`
   (and nests `<rotor>` inside `<stop>`). The bridge looks for `<rotorname>`,
   and the test fixture uses that same wrong shape, so tests pass. Result:
   heading reports go out as `rotor @ …`, which N1MM can't match to its
   configured rotor.
2. **No "latest command wins".** Every goto is appended to a 200-deep FIFO.
   A burst of N1MM packets is replayed one by one — slow over GHE, where each
   command is an HTTP GET.
3. **Stop waits in line.** A stop lands *behind* queued gotos.
4. **`<bidirectional>` is ignored.**
5. **Command source is not tracked** — no way to see who is steering.

# 3. Design

## 3.1 Motion director (latest-wins arbitration)

A new `MotionDirector` sits between every command source and `RotatorLink`.

- API: `goto(heading, source)`, `stop(source)`, `jog(dir, source)`, `park(source)`.
- One lock-protected **pending-motion slot**, not a queue. A new goto
  overwrites any pending goto; stop replaces anything pending and is sent
  first.
- The link thread takes the slot on each loop pass (every 0.15–0.2 s), so
  bursts collapse into one command on the wire.
- Duplicate target to the same heading → dropped. Minimum retarget interval
  (default 0.3 s) guards the controller.
- Mid-move retarget, configurable: `direct` (send the new `APnxxx<CR>;AMn;`,
  the default) or `stop_first` (`;ST1;`, settle, then move). Confirmed on
  real hardware during testing.
- Hub state gains `target_source`; each move is logged, e.g.
  `target 245° from n1mm (was 090° from hamlib)`.
- The web UI, N1MM, Hamlib and PstRotator all go through the director.
  Nothing else calls `link.submit()` for motion.

## 3.2 Listener framework

- Shared base: bind in the constructor (a port in use fails loudly at
  startup — existing pattern), a stop `Event`, socket timeouts, and
  `shutdown()` joins the thread.
- Listeners answer position queries from `Hub` state, never from the link,
  so they cannot compete for the GHE box's one-reply buffer.
- Each listener has `enabled`, `bind`, `port` in config and a CLI flag.

## 3.3 Hamlib `rotctld` (TCP)

| Item | Plan |
|---|---|
| Default | `0.0.0.0:4533`, off by default |
| Clients | Accept loop plus one thread per client, cap `hamlib_max_clients` (4), idle timeout, 1 kB line cap |
| Commands | `P`/`\set_pos az el`, `p`/`\get_pos`, `S`/`\stop`, `K`/`\park`, `M`/`\move dir speed` (8 = CCW, 16 = CW mapped to jog), `_`/`\get_info`, `\dump_state`, `q` |
| Replies | `RPRT 0` / `RPRT -1` (bad argument) / `RPRT -4` (not implemented); `+` extended response mode |
| Elevation | Accepted and ignored; `p` returns `el 0.000000` |

`\dump_state` output is checked against a real `rotctl -m 2` session, since
clients call it on connect.

## 3.4 PstRotator (UDP)

| Item | Plan |
|---|---|
| Default | `0.0.0.0:12000`, off by default |
| Commands | `<PST><AZIMUTH>n</AZIMUTH></PST>`, `<STOP>1</STOP>`, `<PARK>1</PARK>`; multiple tags per packet |
| Queries | `<PST>AZ?</PST>` → `AZ:xxx\r`; `<PST>TGA?</PST>` → `TGA:xxx\r`; sent to sender IP, port + 1 (12001) |
| Ignored | `TRACK`, `ON`, `QRA`, `ANT`, `STF`, `STR` — logged at debug |

## 3.5 N1MM+ bridge fixes

- Parse `<rotor>`, including the name nested inside `<stop>`; fix the test
  fixture to match real N1MM packets.
- `<bidirectional>1` → turn to az or az + 180, whichever is nearer.
- Route through the director (coalescing and stop priority).
- New `n1mm_bind` setting. Default stays all interfaces, because N1MM runs on
  another machine.

## 3.6 Park

New `park_heading` setting (unset by default). Hamlib `K` and PstRotator
`PARK` return an error or are ignored until it's set.

## 3.7 Configuration, CLI, UI

- New JSON keys (all sanitized): `hamlib_enabled/bind/port/max_clients`,
  `pst_enabled/bind/port`, `n1mm_bind`, `park_heading`, `retarget_mode`,
  `retarget_settle_ms`, `retarget_min_interval`.
- CLI: `--hamlib`, `--pst`, plus existing `--n1mm`.
- UI: target readout shows the source (`→ 245° · N1MM`); the footer lists
  active listeners and connected Hamlib clients; settings toggles for each
  listener.

## 3.8 Code layout

Stays in the single `rt21_web.py` file (the README's "one file, stdlib only"
promise), in clearly marked sections. It grows by about 600 lines. Legacy
`rt21_controller.py` and `rt21_network_controller.py` are untouched.

# 4. Build order

1. `MotionDirector` + source tagging; move web UI and N1MM onto it.
2. N1MM fixes (rotor name, bidirectional, bind).
3. Hamlib `rotctld` listener.
4. PstRotator listener.
5. Config, CLI, UI, README.
6. Tests for each step, added with that step.

Each step is one commit on a new branch. Nothing is pushed until you've
tested.

# 5. Testing

## Automated (`test_web.py`, simulator, no hardware)

- Pure parser tests for Hamlib lines, PST packets, and N1MM packets (real
  shapes).
- **Override:** Hamlib `P 90` mid-move, then N1MM 270° → rotator ends at 270°,
  source `n1mm`.
- **Coalescing:** 20 N1MM packets in 100 ms → at most 2 `AP` commands on the
  wire.
- **Stop priority:** stop sent while a goto is pending → goto never sent.
- **Hamlib:** `P`/`p`/`S`/`K`/`M`, `+` mode, bad input, client cap, client
  drops mid-line.
- **PstRotator:** `AZIMUTH`, `STOP`, `AZ?` reply arrives on port + 1.
- **Clean shutdown** with every listener running.
- Run on both `python3.13` and `python3.14`.

## Manual on the Mac

- `brew install hamlib`; `rotctl -m 2 -r 127.0.0.1:4533` → `P 180 0`, `p`,
  `S`, `K`.
- `nc -ul 12001` in one terminal;
  `echo '<PST><AZIMUTH>85</AZIMUTH></PST>' | nc -u -w1 127.0.0.1 12000` and
  `<PST>AZ?</PST>` in another.
- Optional: GPredict against rotctld.

## Live hardware (RT-21 via GHE, 10.3.0.62:8080)

- N1MM on the Windows box: Alt+J, stop, and confirm N1MM shows the heading
  under the correct rotor name.
- Retarget mid-move: confirm `direct` works on firmware 4.13.2, otherwise
  switch the default to `stop_first`.
- Two sources at once (N1MM + rotctl) → latest wins, UI shows the source.

# 6. Decisions (2026-09-29)

1. **Working copy:** `~/Documents/HAM/GreenHeron` is the only copy on this
   Mac; `~/Documents/dev` does not exist.
2. **Branch:** `feat/rotator-protocols`, off `feat/n1mm-rotator-target`.
3. **Bind defaults:** all three listeners bind `0.0.0.0` (all off by default).
4. **Park:** disabled until `park_heading` is set.

# 7. As built — differences from the plan

- **Python 3.13+ bug fixed (pre-existing).** `Rt21Simulator` and `N1mmBridge`
  defined a `_handle` method, which Python 3.13's `threading.Thread` hides
  with its own `_handle` attribute. `--demo` and the N1MM bridge both failed on
  3.13/3.14. The methods were renamed (and the simulator's `_stop` event).
- **Listeners start/stop live** from the settings dialog (`ListenerSet`); no
  restart needed. A taken port is reported, not fatal.
- **SIGTERM** now shuts down cleanly, for launchd/systemd headless use.
- **Hamlib:** no idle timeout (loggers stay connected for hours); TCP
  keepalive instead. `K` with no park heading returns `RPRT -11`
  (feature not available); not connected returns `RPRT -6`.
  `\dump_state` follows Hamlib 4.5+ (`key=value` lines ending in `done`),
  which `rotctl -m 2` (Hamlib 4.5.5) accepts.
- **Version** bumped to 3.1.0.

# 8. Status

- 74 automated tests pass on Python 3.9, 3.10, 3.11, 3.12, 3.13 and 3.14.
- Checked with real Hamlib 4.5.5 `rotctl -m 2` and PstRotator-format UDP
  against `--demo`.
- **Still to do on real hardware** (section 5, live): N1MM Alt+J and stop
  from the Windows box, mid-move retarget on firmware 4.13.2 (switch to
  `stop_first` if `direct` misbehaves), two sources at once.
