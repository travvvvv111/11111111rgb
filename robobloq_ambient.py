#!/usr/bin/env python3
"""
Robobloq Monitor Light Strip - Ambient Light Controller
========================================================
Captures screen edges and maps colors to your Robobloq light strip.

Layout:
  - Top:   31 LEDs  (left → right)
  - Right: 17 LEDs  (top  → bottom)
  - Left:  17 LEDs  (bottom → top)
  Total:   65 LEDs

LED Index Order (matching OpenRGB source):
  Right[0..16] → Top[0..30] → Left[0..16]
  = indices 0–16 (Right), 17–47 (Top), 48–64 (Left)

Usage:
  python robobloq_ambient.py [--brightness 200] [--smoothing 0.3] [--fps 30]
"""

import hid
import time
import math
import argparse
import threading
import signal
import sys
from typing import Optional

try:
    from PIL import ImageGrab, Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("[WARN] Pillow not found. Install with: pip install Pillow")
    print("[WARN] Running in demo (rainbow) mode.")

# ─────────────────────────────────────────────
#  Device constants (from OpenRGB source)
# ─────────────────────────────────────────────
ROBOBLOQ_VID          = 0x1A86
ROBOBLOQ_PID          = 0xFE07
ROBOBLOQ_USAGE_PAGE   = 0xFF00
ROBOBLOQ_USAGE        = 0x01

# Commands
CMD_SET_SYNC_SCREEN   = 0x80
CMD_READ_DEVICE_INFO  = 0x82
CMD_SET_EFFECT        = 0x85
CMD_SET_COLOR         = 0x86
CMD_SET_BRIGHTNESS    = 0x87
CMD_SET_DYNAMIC_SPEED = 0x8A
CMD_SET_OPEN_URL      = 0x93
CMD_TURN_OFF          = 0x97

# LED layout for 65-LED Robobloq
TOP_LED_COUNT         = 31
SIDE_LED_COUNT        = 17
TOTAL_LED_COUNT       = TOP_LED_COUNT + SIDE_LED_COUNT * 2  # 65

# How many (start,r,g,b,end) tuples to send per SyncScreen frame
TUPLE_COUNT           = 34

# ─────────────────────────────────────────────
#  RGBColor helpers
# ─────────────────────────────────────────────
def rgb(r: int, g: int, b: int) -> tuple:
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def blend(c1: tuple, c2: tuple, t: float) -> tuple:
    """Linear interpolate between two RGB colors. t=0 → c1, t=1 → c2."""
    return rgb(
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


# ─────────────────────────────────────────────
#  Range merger (port of RobobloqRangeMerger.cpp)
# ─────────────────────────────────────────────
def merge_robobloq_ranges(colors: list, tuple_count: int) -> bytes:
    """
    Compress a list of (r,g,b) tuples into at most `tuple_count` ranges.
    Returns bytes: repeated (start, r, g, b, end) — 5 bytes per range.
    Matches the algorithm in RobobloqRangeMerger.cpp exactly.
    """
    if tuple_count == 0 or not colors:
        return b''

    # Build initial ranges (one per LED, 1-indexed)
    ranges = []
    for i, (r, g, b) in enumerate(colors):
        sr, sg, sb = float(r), float(g), float(b)
        term = sr*sr + sg*sg + sb*sb
        ranges.append([i + 1, i + 1, 1, sr, sg, sb, term])
        # fields: start, end, n, sum_r, sum_g, sum_b, term

    # Greedy merge
    while len(ranges) > tuple_count:
        best_delta = float('inf')
        best_idx   = -1
        best_term  = 0.0

        for i in range(len(ranges) - 1):
            r1 = ranges[i]
            r2 = ranges[i + 1]
            sr = r1[3] + r2[3]
            sg = r1[4] + r2[4]
            sb = r1[5] + r2[5]
            n  = r1[2] + r2[2]
            term_merged = (sr*sr + sg*sg + sb*sb) / n
            delta = r1[6] + r2[6] - term_merged
            if delta < best_delta:
                best_delta = delta
                best_idx   = i
                best_term  = term_merged

        if best_idx == -1:
            break

        l = ranges[best_idx]
        r = ranges[best_idx + 1]
        l[1]  = r[1]         # extend end
        l[2] += r[2]         # sum n
        l[3] += r[3]         # sum_r
        l[4] += r[4]         # sum_g
        l[5] += r[5]         # sum_b
        l[6]  = best_term    # new term
        ranges.pop(best_idx + 1)

    out = bytearray()
    for (start, end, n, sr, sg, sb, _) in ranges:
        avg_r = min(255, max(0, round(sr / n)))
        avg_g = min(255, max(0, round(sg / n)))
        avg_b = min(255, max(0, round(sb / n)))
        out += bytes([start, avg_r, avg_g, avg_b, end])

    return bytes(out)


# ─────────────────────────────────────────────
#  HID Device wrapper
# ─────────────────────────────────────────────
class RobobloqDevice:
    def __init__(self, dev: hid.device):
        self._dev          = dev
        self._pkt_index    = 0x02
        self._led_count    = 0
        self._physical_size = 0
        self._uuid         = ""
        self._fw_version   = ""
        self._lock         = threading.Lock()

        self._request_device_info()
        self._initialize()

    # ── Public ─────────────────────────────────
    @property
    def led_count(self) -> int:
        return self._led_count

    @property
    def firmware_version(self) -> str:
        return self._fw_version

    @property
    def uuid(self) -> str:
        return self._uuid

    def set_brightness(self, brightness: int):
        b = max(1, min(255, brightness))
        self._send_packet([CMD_SET_BRIGHTNESS, b])

    def set_colors(self, colors: list):
        """
        Send a full frame. colors = list of (r,g,b), length must equal led_count.
        Falls back gracefully if led_count unknown.
        """
        n = self._led_count if self._led_count > 0 else len(colors)
        if len(colors) != n:
            # Truncate or pad
            if len(colors) < n:
                colors = colors + [(0, 0, 0)] * (n - len(colors))
            else:
                colors = colors[:n]

        color_bytes = merge_robobloq_ranges(colors, TUPLE_COUNT)
        self._send_sync_screen(color_bytes)

    def turn_off(self):
        self._send_packet([CMD_TURN_OFF])
        self._set_color_solid(0, 0, 0)

    # ── Internal ────────────────────────────────
    def _initialize(self):
        self._send_packet([CMD_SET_OPEN_URL, 0x00])
        self._send_packet([CMD_SET_BRIGHTNESS, 0xF9])
        self._send_packet([CMD_SET_DYNAMIC_SPEED, 0x32])

    def _set_color_solid(self, r: int, g: int, b: int):
        n = self._led_count
        payload = [CMD_SET_COLOR, 0x01, r, g, b, n, n + 1, 0, 0, 0, 0xFE]
        self._send_packet(payload, flush=False)

    def _send_sync_screen(self, color_bytes: bytes):
        assert len(color_bytes) % 5 == 0
        self._send_multi_packet(CMD_SET_SYNC_SCREEN, color_bytes)

    def _send_multi_packet(self, command: int, payload: bytes):
        with self._lock:
            length = len(payload) + 7
            header = bytes([0x53, 0x43,
                            (length >> 8) & 0xFF, length & 0xFF,
                            self._pkt_index, command])
            data = bytearray(header + payload)

            csum = sum(data) & 0xFF
            data.append(csum)

            # Pad to multiple of 64
            remainder = len(data) % 64
            if remainder:
                data += b'\x00' * (64 - remainder)

            for i in range(0, len(data), 64):
                self._write_report(bytes(data[i:i+64]))
                time.sleep(0.001)

            self._inc_packet_index()

    def _send_packet(self, command: list, flush: bool = True):
        with self._lock:
            length = len(command) + 5
            packet = bytearray([0x52, 0x42, length, self._pkt_index] + command)
            csum = sum(packet) & 0xFF
            packet.append(csum)
            packet += b'\x00' * (64 - len(packet))

            self._write_report(bytes(packet))
            self._inc_packet_index()

            if flush:
                buf = bytearray(64)
                while True:
                    res = self._dev.read(64, timeout_ms=0)
                    if not res:
                        break

    def _send_packet_with_reply(self, command: list) -> bytes:
        expected_id = self._pkt_index
        self._send_packet(command, flush=False)

        for _ in range(3):
            for _ in range(10):
                time.sleep(0.01)
                data = self._dev.read(64, timeout_ms=1000)
                if data and len(data) >= 4 and data[3] == expected_id:
                    return bytes(data)
            # no-op to unstick
            self._send_packet([CMD_SET_OPEN_URL, 0x00], flush=False)

        return b''

    def _request_device_info(self):
        data = self._send_packet_with_reply([CMD_READ_DEVICE_INFO])
        if len(data) < 24:
            print("[WARN] Could not read device info – using defaults (65 LEDs)")
            self._led_count = TOTAL_LED_COUNT
            return

        self._physical_size = data[8]
        self._led_count     = data[11]

        self._uuid        = data[12:20].hex()
        self._fw_version  = f"{data[21]}.{data[22]}.{data[23]}"
        print(f"[INFO] Robobloq detected: {self._physical_size}\" | "
              f"LEDs: {self._led_count} | FW: {self._fw_version} | UUID: {self._uuid}")

    def _write_report(self, data: bytes):
        report = bytes([0x00]) + data[:64]
        self._dev.write(report)

    def _inc_packet_index(self):
        self._pkt_index = (self._pkt_index + 1) & 0xFF


# ─────────────────────────────────────────────
#  Screen capture & color mapping
# ─────────────────────────────────────────────
CAPTURE_STRIP_THICKNESS = 64   # px sampled from each edge

def capture_edge_colors(led_count_top: int,
                        led_count_right: int,
                        led_count_left: int) -> list:
    """
    Capture the screen edges and return a flat list of (r,g,b) for all LEDs.
    Order: right[0..N-1]  (top→bottom)
           top[0..M-1]    (left→right)
           left[0..N-1]   (bottom→top)
    """
    screen = ImageGrab.grab()
    W, H   = screen.size

    T = CAPTURE_STRIP_THICKNESS

    def sample_strip(strip_img: Image.Image, count: int) -> list:
        """Resize strip to (count, 1) and read pixel colors."""
        strip_small = strip_img.resize((count, 1), Image.LANCZOS)
        return [strip_small.getpixel((x, 0))[:3] for x in range(count)]

    # Right edge: full height strip on the right
    right_strip = screen.crop((W - T, 0, W, H)).rotate(90, expand=True)
    right_colors = sample_strip(right_strip, led_count_right)  # top→bottom

    # Top edge: full width strip on the top
    top_strip = screen.crop((0, 0, W, T))
    top_colors = sample_strip(top_strip, led_count_top)  # left→right

    # Left edge: full height strip on the left, reversed (bottom→top)
    left_strip = screen.crop((0, 0, T, H)).rotate(-90, expand=True)
    left_colors = sample_strip(left_strip, led_count_left)  # top→bottom → reverse
    left_colors = list(reversed(left_colors))  # bottom→top

    return right_colors + top_colors + left_colors


# ─────────────────────────────────────────────
#  Demo rainbow mode (no screen capture)
# ─────────────────────────────────────────────
def rainbow_frame(n: int, phase: float) -> list:
    """Generate a rainbow across n LEDs with given phase (0.0–1.0)."""
    colors = []
    for i in range(n):
        hue = (i / n + phase) % 1.0
        h = hue * 6.0
        x = 1 - abs(h % 2 - 1)
        if   h < 1: r, g, b = 1, x, 0
        elif h < 2: r, g, b = x, 1, 0
        elif h < 3: r, g, b = 0, 1, x
        elif h < 4: r, g, b = 0, x, 1
        elif h < 5: r, g, b = x, 0, 1
        else:       r, g, b = 1, 0, x
        colors.append((int(r * 255), int(g * 255), int(b * 255)))
    return colors


# ─────────────────────────────────────────────
#  Smoothing buffer
# ─────────────────────────────────────────────
class ColorSmoother:
    def __init__(self, n: int, alpha: float = 0.3):
        self._buf   = [(0, 0, 0)] * n
        self._alpha = alpha  # higher = faster response

    def update(self, new_colors: list) -> list:
        a = self._alpha
        result = []
        for i, (r, g, b) in enumerate(new_colors):
            pr, pg, pb = self._buf[i]
            sr = int(pr + (r - pr) * a)
            sg = int(pg + (g - pg) * a)
            sb = int(pb + (b - pb) * a)
            self._buf[i] = (sr, sg, sb)
            result.append((sr, sg, sb))
        return result


# ─────────────────────────────────────────────
#  Main ambient loop
# ─────────────────────────────────────────────
def find_robobloq_device() -> Optional[hid.device]:
    """Find and open the Robobloq HID device."""
    for info in hid.enumerate(ROBOBLOQ_VID, ROBOBLOQ_PID):
        if (info.get('usage_page') == ROBOBLOQ_USAGE_PAGE and
                info.get('usage') == ROBOBLOQ_USAGE):
            dev = hid.device()
            dev.open_path(info['path'])
            dev.set_nonblocking(True)
            return dev

    # Fallback: try any matching VID/PID
    for info in hid.enumerate(ROBOBLOQ_VID, ROBOBLOQ_PID):
        try:
            dev = hid.device()
            dev.open_path(info['path'])
            dev.set_nonblocking(True)
            print(f"[WARN] Opened device without usage page filter (path: {info['path']})")
            return dev
        except Exception:
            continue

    return None


def run(args):
    print("=" * 56)
    print("  Robobloq Ambient Light Controller")
    print("  Top: 31 LEDs | Left/Right: 17 LEDs each | Total: 65")
    print("=" * 56)

    # ── Find device ────────────────────────────
    raw_dev = find_robobloq_device()
    if raw_dev is None:
        print("[ERROR] No Robobloq device found (VID=0x1A86, PID=0xFE07).")
        print("        Check USB connection and run with appropriate permissions.")
        print("        On Linux: sudo usermod -aG plugdev $USER  (then re-login)")
        print("        Or run with sudo.")
        sys.exit(1)

    device = RobobloqDevice(raw_dev)
    device.set_brightness(args.brightness)

    n_total = device.led_count if device.led_count > 0 else TOTAL_LED_COUNT
    n_right = SIDE_LED_COUNT
    n_top   = TOP_LED_COUNT
    n_left  = SIDE_LED_COUNT

    smoother = ColorSmoother(n_total, alpha=args.smoothing)

    print(f"[INFO] Brightness: {args.brightness}/255  "
          f"Smoothing: {args.smoothing}  FPS: {args.fps}")
    print(f"[INFO] Mode: {'ambient (screen capture)' if PIL_AVAILABLE else 'demo (rainbow)'}")
    print("[INFO] Press Ctrl+C to stop.\n")

    # ── Signal handler ─────────────────────────
    running = True
    def handle_exit(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT,  handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    frame_time = 1.0 / args.fps
    phase      = 0.0

    try:
        while running:
            t0 = time.monotonic()

            if PIL_AVAILABLE:
                try:
                    raw_colors = capture_edge_colors(n_top, n_right, n_left)
                except Exception as e:
                    print(f"[WARN] Screen capture failed: {e}")
                    raw_colors = [(0, 0, 0)] * n_total
            else:
                raw_colors = rainbow_frame(n_total, phase)
                phase = (phase + 0.01) % 1.0

            colors = smoother.update(raw_colors)
            device.set_colors(colors)

            elapsed = time.monotonic() - t0
            sleep   = frame_time - elapsed
            if sleep > 0:
                time.sleep(sleep)

    finally:
        print("\n[INFO] Shutting down – turning off lights…")
        device.turn_off()
        raw_dev.close()
        print("[INFO] Done.")


# ─────────────────────────────────────────────
#  CLI entry point
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Robobloq Monitor Light Strip – Ambient Light Controller",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--brightness", type=int, default=200,
                        metavar="0-255",
                        help="LED brightness (1–255)")
    parser.add_argument("--smoothing", type=float, default=0.3,
                        metavar="0.0-1.0",
                        help="Color smoothing factor (0=max smooth, 1=instant)")
    parser.add_argument("--fps", type=int, default=30,
                        help="Target frames per second")
    parser.add_argument("--demo", action="store_true",
                        help="Force rainbow demo mode (no screen capture)")
    args = parser.parse_args()

    # Validate
    args.brightness = max(1, min(255, args.brightness))
    args.smoothing  = max(0.01, min(1.0, args.smoothing))
    args.fps        = max(1, min(120, args.fps))

    if args.demo:
        global PIL_AVAILABLE
        PIL_AVAILABLE = False
        print("[INFO] Demo mode forced.")

    run(args)


if __name__ == "__main__":
    main()
