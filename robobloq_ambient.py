#!/usr/bin/env python3
"""
Robobloq Monitor Light Strip — Ambient Light Controller
=========================================================
Windows 11 64-bit  |  Python 3.11+  |  USB HID

LED layout (65 total):
  Right : 17 LEDs  index  0-16   (top → bottom)
  Top   : 31 LEDs  index 17-47   (left → right)
  Left  : 17 LEDs  index 48-64   (bottom → top)

Usage:
  python robobloq_ambient.py [options]
  robobloq_ambient.exe [options]          (PyInstaller build)

Options:
  --brightness  1-255    LED brightness         (default 200)
  --smoothing   0.0-1.0  Color smoothing alpha  (default 0.3)
  --fps         1-120    Target frame rate       (default 30)
  --demo                 Force rainbow demo mode
  --list-devices         List all Robobloq HID devices and exit
"""

import sys
import os
import time
import math
import signal
import threading
import argparse
import textwrap

# ── Optional Pillow ────────────────────────────────────────────────────────────
try:
    from PIL import ImageGrab, Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ── HID ───────────────────────────────────────────────────────────────────────
try:
    import hid
except ImportError:
    print("[ERROR] 'hid' package not found.  Run:  pip install hid")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  Device constants  (from OpenRGB RobobloqLightStripController.h / .cpp)
# ══════════════════════════════════════════════════════════════════════════════
ROBOBLOQ_VID         = 0x1A86
ROBOBLOQ_PID         = 0xFE07
ROBOBLOQ_USAGE_PAGE  = 0xFF00
ROBOBLOQ_USAGE       = 0x01

# HID commands
CMD_SET_SYNC_SCREEN  = 0x80   # multi-packet color frame
CMD_READ_DEVICE_INFO = 0x82
CMD_SET_EFFECT       = 0x85
CMD_SET_COLOR        = 0x86   # single solid color
CMD_SET_BRIGHTNESS   = 0x87
CMD_SET_DYNAMIC_SPEED= 0x8A
CMD_SET_OPEN_URL     = 0x93   # disable driver-download nag
CMD_TURN_OFF         = 0x97

# LED layout
LED_RIGHT  = 17
LED_TOP    = 31
LED_LEFT   = 17
LED_TOTAL  = LED_RIGHT + LED_TOP + LED_LEFT   # 65

# SyncScreen compression: max tuples per frame
TUPLE_COUNT = 34

# Width (px) sampled from each screen edge for ambient capture
CAPTURE_THICKNESS = 80


# ══════════════════════════════════════════════════════════════════════════════
#  Range merger  (port of RobobloqRangeMerger.cpp — greedy SSE minimiser)
# ══════════════════════════════════════════════════════════════════════════════
def merge_robobloq_ranges(colors: list[tuple], tuple_count: int) -> bytes:
    """
    Compress a list of (r, g, b) tuples into ≤ tuple_count LED ranges.
    Returns bytes: each range = 5 bytes  [start_1idx, R, G, B, end_1idx].
    """
    if not colors or tuple_count == 0:
        return b''

    # Initialize: one range per LED (1-based index)
    # Fields: [start, end, n, sum_r, sum_g, sum_b, term]
    # term = (sum_r² + sum_g² + sum_b²) / n  — cached for merge cost calc
    ranges: list[list] = []
    for i, (r, g, b) in enumerate(colors):
        sr, sg, sb = float(r), float(g), float(b)
        ranges.append([i + 1, i + 1, 1, sr, sg, sb, sr*sr + sg*sg + sb*sb])

    # Greedy merge: find adjacent pair whose merge increases SSE least
    while len(ranges) > tuple_count:
        best_delta = float('inf')
        best_idx   = -1
        best_term  = 0.0

        for i in range(len(ranges) - 1):
            a = ranges[i]
            b = ranges[i + 1]
            sr = a[3] + b[3]
            sg = a[4] + b[4]
            sb = a[5] + b[5]
            n  = a[2] + b[2]
            t  = (sr*sr + sg*sg + sb*sb) / n
            d  = a[6] + b[6] - t
            if d < best_delta:
                best_delta, best_idx, best_term = d, i, t

        if best_idx < 0:
            break

        a = ranges[best_idx]
        b = ranges[best_idx + 1]
        a[1]  = b[1]
        a[2] += b[2]
        a[3] += b[3]
        a[4] += b[4]
        a[5] += b[5]
        a[6]  = best_term
        ranges.pop(best_idx + 1)

    out = bytearray()
    for (start, end, n, sr, sg, sb, _) in ranges:
        out += bytes([
            start,
            min(255, max(0, round(sr / n))),
            min(255, max(0, round(sg / n))),
            min(255, max(0, round(sb / n))),
            end,
        ])
    return bytes(out)


# ══════════════════════════════════════════════════════════════════════════════
#  HID device wrapper
# ══════════════════════════════════════════════════════════════════════════════
class RobobloqDevice:
    """Thread-safe wrapper around the Robobloq HID device."""

    def __init__(self, dev: hid.device):
        self._dev       = dev
        self._pkt_idx   = 0x02
        self._lock      = threading.Lock()

        self.led_count      = 0
        self.physical_size  = 0
        self.firmware       = ""
        self.uuid           = ""

        self._read_device_info()
        self._init_device()

    # ── Public API ────────────────────────────────────────────────────
    def set_brightness(self, brightness: int) -> None:
        b = max(1, min(255, int(brightness)))
        self._send_packet([CMD_SET_BRIGHTNESS, b])

    def set_frame(self, colors: list[tuple]) -> None:
        """Push a full color frame.  len(colors) should == led_count."""
        n = self.led_count if self.led_count > 0 else LED_TOTAL
        if len(colors) < n:
            colors = colors + [(0, 0, 0)] * (n - len(colors))
        elif len(colors) > n:
            colors = colors[:n]

        payload = merge_robobloq_ranges(colors, TUPLE_COUNT)
        self._sync_screen(payload)

    def turn_off(self) -> None:
        self._send_packet([CMD_TURN_OFF])
        # Force all LEDs black
        n = self.led_count if self.led_count > 0 else LED_TOTAL
        payload = [CMD_SET_COLOR, 0x01, 0, 0, 0, n, n + 1, 0, 0, 0, 0xFE]
        self._send_packet(payload, flush=False)

    def close(self) -> None:
        self._dev.close()

    # ── Internal ──────────────────────────────────────────────────────
    def _init_device(self) -> None:
        # Disable the "download our driver" URL nag (permanent on device)
        self._send_packet([CMD_SET_OPEN_URL, 0x00])
        self._send_packet([CMD_SET_BRIGHTNESS, 0xF9])
        self._send_packet([CMD_SET_DYNAMIC_SPEED, 0x32])

    def _sync_screen(self, payload: bytes) -> None:
        assert len(payload) % 5 == 0, "SyncScreen payload must be multiple of 5"
        self._send_multi(CMD_SET_SYNC_SCREEN, payload)

    def _send_multi(self, cmd: int, payload: bytes) -> None:
        """Send a multi-packet (SC header) command — used for SyncScreen."""
        with self._lock:
            total_len = len(payload) + 7
            data = bytearray([
                0x53, 0x43,
                (total_len >> 8) & 0xFF,
                total_len & 0xFF,
                self._pkt_idx,
                cmd,
            ]) + bytearray(payload)

            data.append(sum(data) & 0xFF)

            # Pad to multiple of 64
            r = len(data) % 64
            if r:
                data += b'\x00' * (64 - r)

            for i in range(0, len(data), 64):
                self._write(bytes(data[i:i + 64]))
                time.sleep(0.001)

            self._bump_idx()

    def _send_packet(self, cmd: list[int], flush: bool = True) -> None:
        """Send a single-packet (RB header) command."""
        with self._lock:
            length = len(cmd) + 5
            pkt = bytearray([0x52, 0x42, length, self._pkt_idx] + cmd)
            pkt.append(sum(pkt) & 0xFF)
            pkt += b'\x00' * (64 - len(pkt))

            self._write(bytes(pkt))
            self._bump_idx()

            if flush:
                while self._dev.read(64, timeout_ms=0):
                    pass

    def _send_packet_reply(self, cmd: list[int]) -> bytes:
        """Send a packet and wait for a matching reply."""
        exp = self._pkt_idx
        self._send_packet(cmd, flush=False)

        for _attempt in range(3):
            for _read in range(10):
                time.sleep(0.01)
                data = self._dev.read(64, timeout_ms=1000)
                if data and len(data) >= 4 and data[3] == exp:
                    return bytes(data)
            # No-op to un-stick device
            self._send_packet([CMD_SET_OPEN_URL, 0x00], flush=False)
        return b''

    def _read_device_info(self) -> None:
        """
        Request 0x82 — device replies with size/led_count/uuid/fw.
        Packet structure (from OpenRGB comments):
          [4]  = packet ID echo
          [8]  = physical size in inches
          [11] = LED count
          [12..19] = UUID bytes
          [21,22,23] = fw major, minor, patch
        """
        data = self._send_packet_reply([CMD_READ_DEVICE_INFO])
        if len(data) < 24:
            print(f"[WARN] Could not read device info — assuming {LED_TOTAL} LEDs")
            self.led_count = LED_TOTAL
            return

        self.physical_size = data[8]
        self.led_count     = data[11]
        self.uuid          = data[12:20].hex()
        self.firmware      = f"{data[21]}.{data[22]}.{data[23]}"

        print(f"[INFO] Robobloq {self.physical_size}\"  "
              f"LEDs={self.led_count}  FW={self.firmware}  UUID={self.uuid}")

    def _write(self, data: bytes) -> None:
        """Write a 64-byte chunk as a HID report (prepend report ID 0x00)."""
        self._dev.write(bytes([0x00]) + data[:64])

    def _bump_idx(self) -> None:
        self._pkt_idx = (self._pkt_idx + 1) & 0xFF


# ══════════════════════════════════════════════════════════════════════════════
#  Screen capture
# ══════════════════════════════════════════════════════════════════════════════
def _sample_strip(img: "Image.Image", count: int) -> list[tuple]:
    """Resize a strip image to (count × 1) and return pixel list."""
    small = img.resize((count, 1), Image.LANCZOS)
    return [small.getpixel((x, 0))[:3] for x in range(count)]


def capture_ambient(n_right: int, n_top: int, n_left: int) -> list[tuple]:
    """
    Grab the screen and sample edges.

    Return order (matches LED index layout):
      right[0..n_right-1]  top→bottom
      top  [0..n_top-1]    left→right
      left [0..n_left-1]   bottom→top
    """
    screen = ImageGrab.grab()
    W, H   = screen.size
    T      = CAPTURE_THICKNESS

    # Right edge: vertical strip, top→bottom
    right_img = screen.crop((W - T, 0, W, H))
    # Rotate so width = height → sample horizontally
    right_rotated = right_img.rotate(90, expand=True)
    right = _sample_strip(right_rotated, n_right)

    # Top edge: horizontal strip, left→right
    top_img = screen.crop((0, 0, W, T))
    top = _sample_strip(top_img, n_top)

    # Left edge: vertical strip → bottom→top
    left_img = screen.crop((0, 0, T, H))
    left_rotated = left_img.rotate(-90, expand=True)
    left = list(reversed(_sample_strip(left_rotated, n_left)))

    return right + top + left


# ══════════════════════════════════════════════════════════════════════════════
#  Demo mode: rainbow
# ══════════════════════════════════════════════════════════════════════════════
def rainbow_colors(n: int, phase: float) -> list[tuple]:
    """Pure hue rainbow across n LEDs, phase in [0, 1)."""
    out = []
    for i in range(n):
        h = ((i / n) + phase) % 1.0 * 6.0
        x = 1.0 - abs(h % 2 - 1)
        if   h < 1: r, g, b = 1.0, x,   0.0
        elif h < 2: r, g, b = x,   1.0, 0.0
        elif h < 3: r, g, b = 0.0, 1.0, x
        elif h < 4: r, g, b = 0.0, x,   1.0
        elif h < 5: r, g, b = x,   0.0, 1.0
        else:       r, g, b = 1.0, 0.0, x
        out.append((int(r * 255), int(g * 255), int(b * 255)))
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Exponential smoothing
# ══════════════════════════════════════════════════════════════════════════════
class Smoother:
    def __init__(self, n: int, alpha: float):
        self._buf   = [(0, 0, 0)] * n
        self._alpha = alpha

    def update(self, new: list[tuple]) -> list[tuple]:
        a  = self._alpha
        ia = 1.0 - a
        out = []
        for i, (r, g, b) in enumerate(new):
            pr, pg, pb = self._buf[i]
            nr = int(pr * ia + r * a)
            ng = int(pg * ia + g * a)
            nb = int(pb * ia + b * a)
            self._buf[i] = (nr, ng, nb)
            out.append((nr, ng, nb))
        return out


# ══════════════════════════════════════════════════════════════════════════════
#  Device discovery helpers
# ══════════════════════════════════════════════════════════════════════════════
def _enum_robobloq() -> list[dict]:
    return [
        d for d in hid.enumerate(ROBOBLOQ_VID, ROBOBLOQ_PID)
        if d.get("usage_page") == ROBOBLOQ_USAGE_PAGE
        and d.get("usage")      == ROBOBLOQ_USAGE
    ]


def open_device() -> tuple["hid.device", bool]:
    """
    Return (hid.device, found).
    Tries usage-page-filtered first, falls back to any matching VID/PID.
    """
    candidates = _enum_robobloq()
    if not candidates:
        # Fallback: any matching VID/PID
        candidates = list(hid.enumerate(ROBOBLOQ_VID, ROBOBLOQ_PID))

    for info in candidates:
        try:
            dev = hid.device()
            dev.open_path(info["path"])
            dev.set_nonblocking(True)
            return dev, True
        except Exception:
            continue

    return None, False


# ══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ══════════════════════════════════════════════════════════════════════════════
BANNER = r"""
  ╔══════════════════════════════════════════════════╗
  ║   Robobloq  Ambient  Light  Controller           ║
  ║   Top: 31  │  Left: 17  │  Right: 17  = 65 LEDs ║
  ╚══════════════════════════════════════════════════╝
"""


def run(args: argparse.Namespace) -> None:
    print(BANNER)

    # ── Find & open device ────────────────────────────────────────────
    raw, found = open_device()
    if not found:
        print("[ERROR] No Robobloq device found.")
        print("        VID=0x1A86  PID=0xFE07")
        print()
        print("  • Check the USB cable is plugged in.")
        print("  • On Windows, WinUSB/HID driver must be installed")
        print("    (the device should show as 'USB Input Device' in Device Manager).")
        sys.exit(1)

    device = RobobloqDevice(raw)
    device.set_brightness(args.brightness)

    n_total = device.led_count if device.led_count > 0 else LED_TOTAL
    n_right = LED_RIGHT
    n_top   = LED_TOP
    n_left  = LED_LEFT

    use_demo  = args.demo or not PIL_AVAILABLE
    smoother  = Smoother(n_total, alpha=args.smoothing)
    frame_dur = 1.0 / args.fps
    phase     = 0.0

    if use_demo and not args.demo:
        print("[WARN] Pillow not installed — running in rainbow demo mode.")
        print("       Install with:  pip install Pillow")

    print(f"[INFO] Mode       : {'rainbow demo' if use_demo else 'ambient (screen capture)'}")
    print(f"[INFO] Brightness : {args.brightness}/255")
    print(f"[INFO] Smoothing  : {args.smoothing}")
    print(f"[INFO] Target FPS : {args.fps}")
    print()
    print("Press  Ctrl+C  to stop.\n")

    # ── Graceful shutdown ─────────────────────────────────────────────
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    # ── Main loop ─────────────────────────────────────────────────────
    try:
        while running:
            t0 = time.monotonic()

            # Get raw colors
            if use_demo:
                raw_colors = rainbow_colors(n_total, phase)
                phase = (phase + 0.008) % 1.0
            else:
                try:
                    raw_colors = capture_ambient(n_right, n_top, n_left)
                except Exception as exc:
                    print(f"[WARN] Screen capture error: {exc}")
                    raw_colors = [(0, 0, 0)] * n_total

            # Smooth & push
            colors = smoother.update(raw_colors)
            device.set_frame(colors)

            # Sleep remainder of frame budget
            elapsed = time.monotonic() - t0
            wait    = frame_dur - elapsed
            if wait > 0:
                time.sleep(wait)

    finally:
        print("\n[INFO] Turning off LEDs…")
        try:
            device.turn_off()
        except Exception:
            pass
        raw.close()
        print("[INFO] Goodbye.")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="robobloq_ambient",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            Robobloq Monitor Light Strip — Ambient Light Controller
            --------------------------------------------------------
            Captures screen edges and maps colors to 65 RGB LEDs.

            LED layout
              Right : 17 LEDs  (top → bottom)
              Top   : 31 LEDs  (left → right)
              Left  : 17 LEDs  (bottom → top)
        """),
    )
    parser.add_argument(
        "--brightness", type=int, default=200, metavar="N",
        help="LED brightness 1-255  (default: 200)",
    )
    parser.add_argument(
        "--smoothing", type=float, default=0.3, metavar="F",
        help="Color smoothing 0.0-1.0  (0=smooth, 1=instant)  (default: 0.3)",
    )
    parser.add_argument(
        "--fps", type=int, default=30, metavar="N",
        help="Target frames per second 1-120  (default: 30)",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Force rainbow demo mode (no screen capture)",
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="List detected Robobloq HID devices and exit",
    )

    args = parser.parse_args()

    if args.list_devices:
        devices = list(hid.enumerate(ROBOBLOQ_VID, ROBOBLOQ_PID))
        if not devices:
            print("No Robobloq devices found (VID=0x1A86, PID=0xFE07).")
        else:
            print(f"Found {len(devices)} device(s):")
            for d in devices:
                print(f"  path={d['path']}  usage_page=0x{d['usage_page']:04X}"
                      f"  usage=0x{d['usage']:02X}")
        sys.exit(0)

    # Clamp values
    args.brightness = max(1,    min(255,  args.brightness))
    args.smoothing  = max(0.01, min(1.0,  args.smoothing))
    args.fps        = max(1,    min(120,  args.fps))

    run(args)


if __name__ == "__main__":
    main()
