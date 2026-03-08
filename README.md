# Robobloq Ambient Light Controller

[![Build](https://github.com/YOUR_NAME/robobloq-ambient/actions/workflows/build.yml/badge.svg)](https://github.com/YOUR_NAME/robobloq-ambient/actions/workflows/build.yml)
![Platform](https://img.shields.io/badge/platform-Windows%2011%2064--bit-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-GPL--2.0--or--later-green)

> Real-time ambient screen lighting for the **Robobloq Monitor Light Strip**  
> (상단 **31개** + 좌우 각 **17개** = **65 LEDs**)

---

## 📥 Download (Windows 11 64-bit)

→ **[Releases](../../releases/latest)** → download `robobloq_ambient.exe`  
No Python, no install. Just plug in your device and run.

---

## 🗺️ LED Layout

```
       ◄────────────── TOP  31 LEDs  (left → right) ──────────────►
       ┌────────────────────────────────────────────────────────────┐
  ▲    │                                                            │   ▲
  │    │                   MONITOR SCREEN                          │   │
LEFT   │                                                            │ RIGHT
17     │   index 48–64                           index 0–16        │   17
LEDs   │   (bottom → top)                        (top → bottom)    │  LEDs
  ▼    │                                                            │   ▼
       └────────────────────────────────────────────────────────────┘

Internal flat index:
  [  0 – 16 ]  Right  (top → bottom)
  [ 17 – 47 ]  Top    (left → right)
  [ 48 – 64 ]  Left   (bottom → top)
```

---

## 🚀 Usage

### Executable (recommended)

```
robobloq_ambient.exe                      # ambient mode (mirrors screen)
robobloq_ambient.exe --demo               # rainbow demo, no screen needed
robobloq_ambient.exe --brightness 180     # brightness 1-255  (default 200)
robobloq_ambient.exe --smoothing 0.5      # 0.0=smooth / 1.0=instant  (default 0.3)
robobloq_ambient.exe --fps 60             # target FPS  (default 30)
robobloq_ambient.exe --list-devices       # show detected HID devices
```

### Python (development)

```bash
pip install -r requirements.txt
python robobloq_ambient.py --demo
```

---

## 🛠️ Build from Source

### Local

```bash
pip install pyinstaller -r requirements.txt
pyinstaller --onefile --name robobloq_ambient robobloq_ambient.py
# → dist/robobloq_ambient.exe
```

### GitHub Actions (automatic)

Push a version tag to trigger a release build:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The workflow (`.github/workflows/build.yml`) will:
1. Set up Python 3.11 64-bit on `windows-latest`
2. Install dependencies + PyInstaller
3. Build `robobloq_ambient.exe` (self-contained, ~15 MB)
4. Create a GitHub Release with the `.exe` attached

You can also trigger a build manually from **Actions → Build & Release → Run workflow**.

---

## 🔌 Device Setup (Windows)

1. Plug in the Robobloq Monitor Light Strip via USB.
2. Windows should install it automatically as a **USB Input Device** (HID).  
   Check Device Manager → Human Interface Devices.
3. Run `robobloq_ambient.exe`. No driver installation required.

### SmartScreen warning

First run may show _"Windows protected your PC"_. Click **More info → Run anyway**.  
The `.exe` is built directly from this repo's source code.

---

## ⚙️ Options

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--brightness` | int 1-255 | `200` | Overall LED brightness |
| `--smoothing` | float 0-1 | `0.3` | Color response speed (higher = faster) |
| `--fps` | int 1-120 | `30` | Frame rate target |
| `--demo` | flag | off | Rainbow animation, skips screen capture |
| `--list-devices` | flag | — | Print HID device list and exit |

---

## 🔬 How It Works

### Ambient capture

```
Screen edges (80px thick)       Resize → LED count      Push to device
┌──────[TOP]────────┐
│L               R  │   → Pillow LANCZOS resize → 31 top colors
│E               I  │     17 right, 17 left colors
│F               G  │
│T               H  │
└───────────────────┘

Capture order → flat LED array:
  right[0..16] + top[0..30] + left[0..16]
```

### Packet protocol (reversed from OpenRGB source)

```
SyncScreen  (multi-packet, header SC 53 43)
  53 43  [len_hi] [len_lo]  [pkt_idx]  80  <payload>  [csum]
  payload = repeated 5-byte tuples: [start_1idx R G B end_1idx]
  max 34 tuples per frame

Single command  (header RB 52 42)
  52 42  [len]  [pkt_idx]  [cmd]  [args…]  [csum]
  padded to 64 bytes, sent as HID report ID 0x00
```

### Range compression

65 LED values → compressed to ≤ 34 color ranges using a **greedy SSE-minimising merge** algorithm (ported from `RobobloqRangeMerger.cpp` in the OpenRGB project).

---

## 📁 Repository Structure

```
robobloq-ambient/
├── .github/
│   └── workflows/
│       └── build.yml          # CI/CD: build + release
├── assets/
│   └── icon.ico               # (optional) exe icon
├── robobloq_ambient.py        # main script
├── requirements.txt
└── README.md
```

---

## 🐛 Troubleshooting

| Problem | Fix |
|---------|-----|
| `No Robobloq device found` | Check USB; run `--list-devices`; try different USB port |
| Colors lag behind screen | Increase `--smoothing` (e.g. `0.7`) |
| Colors are too jumpy | Decrease `--smoothing` (e.g. `0.15`) |
| High CPU / slow | Decrease `--fps` (e.g. `15`) |
| SmartScreen blocks .exe | Click **More info → Run anyway** |
| Pillow not found (Python) | `pip install Pillow` |

---

## 📄 License

GPL-2.0-or-later — matching the [OpenRGB](https://gitlab.com/CalcProgrammer1/OpenRGB) project this protocol analysis is based on.
