# Robobloq Ambient Light Controller

[![Build & Release](../../actions/workflows/build.yml/badge.svg)](../../actions/workflows/build.yml)
![Windows 11 x64](https://img.shields.io/badge/Windows_11-x64-blue)

Robobloq 모니터 라이트 스트립용 Ambient Light 프로그램  
화면 가장자리 색상을 실시간으로 캡처해 65개 LED에 반영합니다.

---

## 📥 다운로드

**[Releases](../../releases/latest)** → `robobloq_ambient.exe`  
Python 설치 불필요. USB 연결 후 바로 실행.

---

## 🗺️ LED 레이아웃

```
       ◄──────── TOP 31개 (왼쪽→오른쪽) ────────►
       ┌────────────────────────────────────────┐
  ▲    │                                        │  ▲
LEFT   │           MONITOR SCREEN              │ RIGHT
17개   │  [48-64]                    [0-16]    │ 17개
(↑)   │  아래→위                    위→아래   │ (↓)
       └────────────────────────────────────────┘
```

---

## 🚀 사용법

```
robobloq_ambient.exe                   # 화면 ambient 모드
robobloq_ambient.exe --demo            # 무지개 데모 (장치 테스트용)
robobloq_ambient.exe --brightness 180  # 밝기 1-255 (기본 200)
robobloq_ambient.exe --smoothing 0.5   # 부드러움 0-1 (기본 0.3)
robobloq_ambient.exe --fps 60          # FPS (기본 30)
robobloq_ambient.exe --list-devices    # 연결 장치 목록
```

---

## 🔨 빌드 (GitHub Actions)

### 수동 빌드
1. **Actions** 탭 → **Build & Release** → **Run workflow**
2. 완료 후 **Artifacts** → `robobloq_ambient-windows-x64` 다운로드

### 자동 릴리즈
```bash
git tag v1.0.0
git push origin v1.0.0
```
→ 자동 빌드 후 Releases에 `.exe` 첨부

---

## ⚠️ SmartScreen 경고

**추가 정보 → 실행** 클릭 (이 저장소 소스만으로 빌드됨)
