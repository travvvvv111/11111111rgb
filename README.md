# Robobloq Monitor Light Strip – Ambient Light Controller

> Real-time screen ambient lighting for the **Robobloq Monitor Light Strip**  
> (상단 31개 + 좌우 각 17개 = **65 LEDs**)

---

## ✨ Features

| Feature | Detail |
|---------|--------|
| 🌅 Ambient mode | Captures screen edges, maps colors to each LED zone |
| 🌈 Demo mode | Standalone rainbow animation (no screen required) |
| 🎨 Smooth transitions | Configurable exponential smoothing |
| ⚡ Low latency | ~30 FPS default, up to 120 FPS |
| 🔌 No OpenRGB required | Pure Python, talks directly to the HID device |
| 💡 Brightness control | Adjustable via CLI flag |

---

## 🗺️ LED Layout

```
         ◄──────── TOP 31 LEDs (left → right) ────────►
         ┌──────────────────────────────────────────────┐
    ▲    │                                              │    ▲
    │    │              MONITOR SCREEN                 │    │
  LEFT   │                                              │  RIGHT
  17     │                                              │  17
  LEDs   │         (bottom → top)   (top → bottom)     │  LEDs
  (↑)    │                                              │  (↓)
         └──────────────────────────────────────────────┘

Internal LED index order (0-based):
  [0..16]  = Right zone  (top → bottom)
  [17..47] = Top zone    (left → right)
  [48..64] = Left zone   (bottom → top)
```

---

## 🛠️ Requirements

```bash
pip install hid Pillow
```

| Package | Purpose |
|---------|---------|
| `hid` (`hidapi`) | USB HID communication with the device |
| `Pillow` | Screen capture for ambient mode |

### Linux – udev rule (one-time setup)

```bash
# Create udev rule so you don't need sudo
sudo tee /etc/udev/rules.d/99-robobloq.rules << 'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="fe07", MODE="0666", GROUP="plugdev"
EOF

sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then re-plug the device or log out and back in.

---

## 🚀 Usage

```bash
# Ambient mode (default) – mirrors your screen edges
python robobloq_ambient.py

# Custom brightness (1–255)
python robobloq_ambient.py --brightness 180

# Faster smoothing response
python robobloq_ambient.py --smoothing 0.6

# Higher frame rate
python robobloq_ambient.py --fps 60

# Demo rainbow (no screen needed, useful for testing)
python robobloq_ambient.py --demo

# All options
python robobloq_ambient.py --brightness 200 --smoothing 0.3 --fps 30
```

### CLI Options

```
--brightness  0-255    LED brightness level         (default: 200)
--smoothing   0.0-1.0  Color smoothing alpha        (default: 0.3)
                       0.0 = very smooth/laggy
                       1.0 = instant/sharp
--fps         1-120    Target frames per second      (default: 30)
--demo                 Force rainbow demo mode
```

---

## 🔬 How It Works

### Screen Capture & Mapping

```
Screen                              LED Strip
┌────────────────────┐
│  [TOP STRIP]       │  ──► sample 31 average colors ──► Top zone
│ L                R │
│ E                I │  ──► sample 17 colors (each side)
│ F                G │
│ T                H │
│  [BOT (not used)]  │
└────────────────────┘
```

Each edge strip is sampled from a **64-pixel-wide band** along the screen edge, then resized to the LED count using Lanczos resampling for accurate average colors.

### Packet Protocol (from OpenRGB source analysis)

```
SendSyncScreen (CMD 0x80) – multi-packet:
  Header: 53 43 [len_hi] [len_lo] [pkt_idx] [cmd]
  Body:   repeated 5-byte tuples: [start] [R] [G] [B] [end]
  Footer: [checksum_lo]

SendPacket (CMD 0x86/0x87 etc.) – single packet:
  Header: 52 42 [len] [pkt_idx]
  Body:   [command bytes]
  Footer: [checksum_lo]
  Padded to 64 bytes, sent as HID report with report ID 0x00
```

### Range Compression

The device's `SyncScreen` command accepts up to **34 color ranges** (not individual LEDs). The `merge_robobloq_ranges()` function uses a **greedy SSE-minimizing merge algorithm** (ported from `RobobloqRangeMerger.cpp`) to compress 65 LED values into ≤34 ranges with minimal color error.

---

## 📁 File Structure

```
robobloq-ambient/
├── robobloq_ambient.py   # Main script (self-contained)
└── README.md
```

---

## 🔧 Troubleshooting

| Problem | Solution |
|---------|----------|
| `No Robobloq device found` | Check USB cable; run with `sudo` on Linux; apply udev rule |
| `Pillow not found` | `pip install Pillow`; or use `--demo` mode |
| Laggy colors | Increase `--smoothing` (e.g. `0.7`) |
| Too jumpy/flickery | Decrease `--smoothing` (e.g. `0.15`) |
| High CPU usage | Decrease `--fps` (e.g. `15`) |
| Colors feel wrong | Try `--brightness 255` for max saturation |

---

## 📄 License

GPL-2.0-or-later (matching the OpenRGB project this is based on)

---

## 🙏 Credits

Protocol reverse-engineered from [OpenRGB](https://gitlab.com/CalcProgrammer1/OpenRGB) source code:
- `Controllers/RobobloqLightStripController/`
