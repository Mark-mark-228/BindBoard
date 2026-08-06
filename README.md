# BindBoard

**BindBoard** is a macro keyboard app for Windows. Assign actions to keys on a second keyboard — your main keyboard keeps working as normal.  
Built on Windows Raw Input API — no drivers, no third-party software required.

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Screenshots

| Keyboard setup | Bindings |
|---|---|
| ![Keyboard](screenshots/BindBoard3.png) | ![Bindings](screenshots/BindBoard1.png) |

| Launch | Theme |
|---|---|
| ![Launch](screenshots/BindBoard5.png) | ![Theme](screenshots/BindBoard.png) |

---

## Features

- 🎹 **Per-device key interception** — keys from the second keyboard trigger actions; your main keyboard is untouched
- 🗂 **Modes** — create multiple binding profiles and switch between them with a single key press
- 🖥 **Mode overlay** — a brief on-screen hint appears whenever you switch modes
- 🚀 **Action types** — open a URL, launch a program or shortcut, start a Steam game, play a sound via Soundpad, or switch mode
- 🔍 **App & game picker** — browse installed programs and Steam games by name, no manual path or ID entry needed
- 🌐 **English & Russian UI** — language is detected automatically from your system locale
- 🔔 **System tray** — closing the window minimizes to tray; the listener keeps running in the background

---

## Installation

### Ready-to-run EXE (recommended)

1. Go to [Releases](../../releases)
2. Download `BindBoard.zip`
3. Extract to any folder
4. Run `BindBoard.exe`

> Requires Windows 10/11 with [WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) (already included in Windows 11)

### From source

**Requirements:** Python 3.10+, Windows 10/11

```bash
git clone https://github.com/Mark-mark-228/BindBoard.git
cd BindBoard
pip install -r requirements.txt
python app.py
```

---

## Quick start

1. Launch **BindBoard**
2. Go to **Keyboard** → click **Refresh** → select your second keyboard → click **Apply layout**
3. Go to **Bindings** → click any key → choose an action on the right → click **Save**
4. Go to **Launch** → click **Start**

Your keys are live!

---

## Modes

Modes let you have multiple sets of bindings on the same physical keys — for example one profile for music control and another for gaming.

- Click **+ Mode** to create a new profile
- Assign any key the **"Switch mode"** action to jump between profiles
- A brief overlay appears on screen whenever the mode changes

---

## Building the EXE

```powershell
pip install -r requirements.txt
pyinstaller BindBoard.spec --noconfirm
```

The output folder will be at `dist\BindBoard\`.

---

## Project structure

```
BindBoard/
├── app.py           — backend: Raw Input, bindings engine, tray, API
├── app.html         — frontend: single-page app (HTML / CSS / JS)
├── bindboard.ico    — app icon
├── BindBoard.spec   — PyInstaller build config
├── requirements.txt — Python dependencies
├── screenshots/     — README screenshots
└── assets/          — UI icons and resources
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `pywebview` | App window (Chromium WebView2) |
| `pystray` | System tray icon |
| `Pillow` | Icon processing |
| `pyinstaller` | EXE build (dev only) |

---

## License

MIT — free to use, modify, and distribute.
