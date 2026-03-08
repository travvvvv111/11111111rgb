#!/usr/bin/env python3
"""
Robobloq Monitor Light Strip — Ambient Light Controller
Windows 11 64-bit | Python 3.11 | USB HID

필요 패키지:
    pip install hidapi Pillow

LED 구성 (총 65개):
    Right  17개  index  0-16   위 → 아래
    Top    31개  index 17-47   왼쪽 → 오른쪽
    Left   17개  index 48-64   아래 → 위
"""

import sys
import os
import time
import signal
import threading
import argparse
import textwrap

# ── Pillow ────────────────────────────────────────────────────────────────────
try:
    from PIL import ImageGrab, Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ── hidapi ────────────────────────────────────────────────────────────────────
# 반드시 "hidapi" 패키지 사용  (pip install hidapi)
# "hid" 패키지는 Windows에서 DLL 미포함으로 동작 안 함
try:
    import hid
except ImportError:
    print("[ERROR] hidapi 패키지가 없습니다.")
    print("        설치: pip install hidapi")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  장치 상수 (OpenRGB RobobloqLightStripController.h 기준)
# ══════════════════════════════════════════════════════════════════════════════
VID  = 0x1A86
PID  = 0xFE07
UP   = 0xFF00   # Usage Page
USG  = 0x01     # Usage

CMD_SYNC_SCREEN   = 0x80
CMD_DEVICE_INFO   = 0x82
CMD_SET_COLOR     = 0x86
CMD_BRIGHTNESS    = 0x87
CMD_DYNAMIC_SPEED = 0x8A
CMD_OPEN_URL      = 0x93
CMD_TURN_OFF      = 0x97

N_RIGHT  = 17
N_TOP    = 31
N_LEFT   = 17
N_TOTAL  = N_RIGHT + N_TOP + N_LEFT   # 65

MAX_TUPLES = 34   # SyncScreen 프레임당 최대 색상 범위 수
EDGE_PX    = 80   # 화면 가장자리에서 샘플링할 픽셀 두께


# ══════════════════════════════════════════════════════════════════════════════
#  색상 범위 압축 (RobobloqRangeMerger.cpp 포팅 — 탐욕적 SSE 최소화)
# ══════════════════════════════════════════════════════════════════════════════
def compress_colors(colors: list, max_t: int) -> bytes:
    """
    N개의 (r,g,b) → 최대 max_t개 범위로 압축
    출력: 5바이트씩  [start_1idx, R, G, B, end_1idx]
    """
    if not colors or max_t == 0:
        return b''

    # 범위 구조: [start, end, n, sum_r, sum_g, sum_b, term]
    # term = (sr²+sg²+sb²)/n  (병합 비용 캐시)
    segs = []
    for i, (r, g, b) in enumerate(colors):
        sr, sg, sb = float(r), float(g), float(b)
        segs.append([i+1, i+1, 1, sr, sg, sb, sr*sr+sg*sg+sb*sb])

    while len(segs) > max_t:
        bd, bi, bt = float('inf'), -1, 0.0
        for i in range(len(segs)-1):
            a = segs[i]; b = segs[i+1]
            sr=a[3]+b[3]; sg=a[4]+b[4]; sb=a[5]+b[5]; n=a[2]+b[2]
            t = (sr*sr+sg*sg+sb*sb)/n
            d = a[6]+b[6]-t
            if d < bd:
                bd, bi, bt = d, i, t
        if bi < 0:
            break
        a=segs[bi]; b=segs[bi+1]
        a[1]=b[1]; a[2]+=b[2]; a[3]+=b[3]; a[4]+=b[4]; a[5]+=b[5]; a[6]=bt
        segs.pop(bi+1)

    out = bytearray()
    for (s, e, n, sr, sg, sb, _) in segs:
        out += bytes([s,
                      min(255, max(0, round(sr/n))),
                      min(255, max(0, round(sg/n))),
                      min(255, max(0, round(sb/n))),
                      e])
    return bytes(out)


# ══════════════════════════════════════════════════════════════════════════════
#  HID 장치 클래스
# ══════════════════════════════════════════════════════════════════════════════
class RobobloqDevice:
    def __init__(self, dev):
        self._dev  = dev
        self._idx  = 0x02
        self._lock = threading.Lock()
        self.n_leds    = 0
        self.size_inch = 0
        self.firmware  = "?"
        self.uuid      = "?"
        self._read_info()
        self._init_device()

    # ── 공개 메서드 ───────────────────────────────────────────────────────────
    def set_brightness(self, v: int):
        self._single([CMD_BRIGHTNESS, max(1, min(255, v))])

    def push_frame(self, colors: list):
        n = self.n_leds or N_TOTAL
        if len(colors) < n:
            colors += [(0,0,0)] * (n - len(colors))
        self._multi(CMD_SYNC_SCREEN, compress_colors(colors[:n], MAX_TUPLES))

    def turn_off(self):
        self._single([CMD_TURN_OFF])
        n = self.n_leds or N_TOTAL
        self._single([CMD_SET_COLOR, 1, 0,0,0, n, n+1, 0,0,0, 0xFE], flush=False)

    def close(self):
        try: self._dev.close()
        except: pass

    # ── 내부 메서드 ───────────────────────────────────────────────────────────
    def _init_device(self):
        self._single([CMD_OPEN_URL, 0x00])
        self._single([CMD_BRIGHTNESS, 0xF9])
        self._single([CMD_DYNAMIC_SPEED, 0x32])

    def _read_info(self):
        data = self._single_reply([CMD_DEVICE_INFO])
        if len(data) < 24:
            print(f"[WARN] 장치 정보 읽기 실패 — {N_TOTAL}개 LED로 가정")
            self.n_leds = N_TOTAL
            return
        self.size_inch = data[8]
        self.n_leds    = data[11]
        self.uuid      = data[12:20].hex()
        self.firmware  = f"{data[21]}.{data[22]}.{data[23]}"
        print(f"[INFO] Robobloq {self.size_inch}\"  "
              f"LED={self.n_leds}  FW={self.firmware}  UUID={self.uuid}")

    # SC 헤더 멀티패킷 (SyncScreen)
    def _multi(self, cmd: int, payload: bytes):
        assert len(payload) % 5 == 0
        with self._lock:
            total = len(payload) + 7
            data  = bytearray([0x53,0x43,(total>>8)&0xFF,total&0xFF,
                                self._idx, cmd]) + bytearray(payload)
            data.append(sum(data) & 0xFF)
            rem = len(data) % 64
            if rem: data += b'\x00'*(64-rem)
            for i in range(0, len(data), 64):
                self._write(data[i:i+64])
                time.sleep(0.001)
            self._bump()

    # RB 헤더 단일 패킷
    def _single(self, cmd: list, flush: bool = True):
        with self._lock:
            pkt = bytearray([0x52,0x42, len(cmd)+5, self._idx] + cmd)
            pkt.append(sum(pkt) & 0xFF)
            pkt += b'\x00'*(64-len(pkt))
            self._write(pkt)
            self._bump()
            if flush:
                while self._dev.read(64, timeout_ms=0):
                    pass

    def _single_reply(self, cmd: list) -> bytes:
        exp = self._idx
        self._single(cmd, flush=False)
        for _ in range(3):
            for _ in range(10):
                time.sleep(0.01)
                d = self._dev.read(64, timeout_ms=1000)
                if d and len(d) >= 4 and d[3] == exp:
                    return bytes(d)
            self._single([CMD_OPEN_URL, 0x00], flush=False)
        return b''

    def _write(self, data):
        self._dev.write(bytes([0x00]) + bytes(data)[:64])

    def _bump(self):
        self._idx = (self._idx + 1) & 0xFF


# ══════════════════════════════════════════════════════════════════════════════
#  화면 캡처
# ══════════════════════════════════════════════════════════════════════════════
def _resize_strip(img, count: int) -> list:
    s = img.resize((count, 1), Image.LANCZOS)
    return [s.getpixel((x, 0))[:3] for x in range(count)]

def capture_screen() -> list:
    """
    화면 가장자리를 샘플링해 LED 색상 리스트 반환
    순서: right(위→아래) + top(왼→오른) + left(아래→위)
    """
    scr  = ImageGrab.grab()
    W, H = scr.size
    T    = EDGE_PX

    right = _resize_strip(scr.crop((W-T, 0, W, H)).rotate(90, expand=True),  N_RIGHT)
    top   = _resize_strip(scr.crop((0, 0, W, T)),                             N_TOP)
    left  = list(reversed(
            _resize_strip(scr.crop((0, 0, T, H)).rotate(-90, expand=True),    N_LEFT)))
    return right + top + left


# ══════════════════════════════════════════════════════════════════════════════
#  무지개 데모
# ══════════════════════════════════════════════════════════════════════════════
def make_rainbow(n: int, phase: float) -> list:
    out = []
    for i in range(n):
        h = ((i/n) + phase) % 1.0 * 6.0
        x = 1.0 - abs(h % 2 - 1)
        if   h<1: r,g,b=1.,x,0.
        elif h<2: r,g,b=x,1.,0.
        elif h<3: r,g,b=0.,1.,x
        elif h<4: r,g,b=0.,x,1.
        elif h<5: r,g,b=x,0.,1.
        else:     r,g,b=1.,0.,x
        out.append((int(r*255), int(g*255), int(b*255)))
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  지수 평활 (색상 부드럽게)
# ══════════════════════════════════════════════════════════════════════════════
class Smoother:
    def __init__(self, n: int, alpha: float):
        self._buf   = [(0,0,0)]*n
        self._alpha = alpha

    def smooth(self, new: list) -> list:
        a=self._alpha; ia=1-a
        out=[]
        for i,(r,g,b) in enumerate(new):
            pr,pg,pb=self._buf[i]
            c=(int(pr*ia+r*a), int(pg*ia+g*a), int(pb*ia+b*a))
            self._buf[i]=c; out.append(c)
        return out


# ══════════════════════════════════════════════════════════════════════════════
#  장치 탐색
# ══════════════════════════════════════════════════════════════════════════════
def open_device():
    """Usage Page 필터링 → fallback VID/PID만으로 시도"""
    all_devs = list(hid.enumerate(VID, PID))

    # 1차: usage page 정확히 일치
    filtered = [d for d in all_devs
                if d.get('usage_page') == UP and d.get('usage') == USG]

    candidates = filtered if filtered else all_devs

    for info in candidates:
        try:
            d = hid.device()
            d.open_path(info['path'])
            d.set_nonblocking(True)
            return d
        except Exception:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  메인 루프
# ══════════════════════════════════════════════════════════════════════════════
def run(args):
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║  Robobloq Ambient Light Controller           ║")
    print("  ║  Right:17  Top:31  Left:17  →  Total:65 LEDs║")
    print("  ╚══════════════════════════════════════════════╝\n")

    raw = open_device()
    if raw is None:
        print("[ERROR] 장치를 찾을 수 없습니다.")
        print(f"        VID=0x{VID:04X}  PID=0x{PID:04X}")
        print("\n  • USB 케이블 확인")
        print("  • 장치 관리자 → 휴먼 인터페이스 장치 확인")
        print("  • --list-devices 옵션으로 연결 장치 목록 확인")
        sys.exit(1)

    dev      = RobobloqDevice(raw)
    dev.set_brightness(args.brightness)

    n        = dev.n_leds or N_TOTAL
    smoother = Smoother(n, args.smoothing)
    demo     = args.demo or not PIL_AVAILABLE
    interval = 1.0 / args.fps
    phase    = 0.0

    if not PIL_AVAILABLE and not args.demo:
        print("[WARN] Pillow 미설치 → 무지개 데모 모드로 실행")
        print("       화면 캡처 사용하려면: pip install Pillow\n")

    print(f"[INFO] 모드      : {'무지개 데모' if demo else '화면 ambient'}")
    print(f"[INFO] 밝기      : {args.brightness}/255")
    print(f"[INFO] 부드러움  : {args.smoothing}  (0=부드럽, 1=즉각)")
    print(f"[INFO] FPS 목표  : {args.fps}")
    print("\nCtrl+C 로 종료\n")

    running = True
    def stop(s, f): nonlocal running; running = False
    signal.signal(signal.SIGINT,  stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        while running:
            t0 = time.monotonic()

            if demo:
                colors = make_rainbow(n, phase)
                phase  = (phase + 0.008) % 1.0
            else:
                try:
                    colors = capture_screen()
                except Exception as e:
                    print(f"[WARN] 화면 캡처 실패: {e}")
                    colors = [(0,0,0)] * n

            dev.push_frame(smoother.smooth(colors))

            wait = interval - (time.monotonic() - t0)
            if wait > 0:
                time.sleep(wait)

    finally:
        print("\n[INFO] LED 끄는 중…")
        try: dev.turn_off()
        except: pass
        raw.close()
        print("[INFO] 종료 완료")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════
def main():
    p = argparse.ArgumentParser(
        prog="robobloq_ambient",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            Robobloq 모니터 라이트 스트립 Ambient Light 컨트롤러
            화면 가장자리 색상 → 65개 LED 실시간 반영
        """))
    p.add_argument("--brightness", type=int,   default=200, metavar="1-255",
                   help="LED 밝기 (기본값 200)")
    p.add_argument("--smoothing",  type=float, default=0.3, metavar="0-1",
                   help="색상 부드러움 (기본값 0.3)")
    p.add_argument("--fps",        type=int,   default=30,  metavar="1-120",
                   help="목표 FPS (기본값 30)")
    p.add_argument("--demo",       action="store_true",
                   help="무지개 데모 모드 강제 실행")
    p.add_argument("--list-devices", action="store_true",
                   help="연결된 HID 장치 목록 출력 후 종료")

    args = p.parse_args()

    if args.list_devices:
        devs = list(hid.enumerate(VID, PID))
        if not devs:
            print(f"Robobloq 장치 없음  (VID=0x{VID:04X} PID=0x{PID:04X})")
        else:
            print(f"{len(devs)}개 장치 발견:")
            for d in devs:
                print(f"  path={d['path']}"
                      f"  usage_page=0x{d.get('usage_page',0):04X}"
                      f"  usage=0x{d.get('usage',0):02X}")
        sys.exit(0)

    args.brightness = max(1,   min(255, args.brightness))
    args.smoothing  = max(0.01,min(1.0, args.smoothing))
    args.fps        = max(1,   min(120, args.fps))
    run(args)


if __name__ == "__main__":
    main()
