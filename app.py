"""BindBoard — macro keyboard app for Windows (backend).

How it fits together
  RawInputHub       one hidden window + thread that owns this process's keyboard Raw Input
                    registration. Windows keeps only ONE raw-input target window per process
                    (a second RegisterRawInputDevices call silently steals all input from the
                    first), so the listener, the keyboard verifier and the key scanner all
                    subscribe here instead of registering windows of their own.
  KeyboardListener  turns key presses from the selected keyboard into actions. Mode switches
                    are applied inline (so the next key already sees the new mode); everything
                    else goes to ActionRunner, so a slow program launch never stalls input.
  ActionRunner      one worker thread that executes actions in order.
  API               methods the web UI (app.html) calls through pywebview.

There is deliberately no WH_KEYBOARD_LL hook: Windows calls low-level hooks *before* it
generates raw input, so a hook can never know which keyboard a key came from, and keys it
blocks never produce raw input at all. The old hook therefore never suppressed anything,
while adding latency to every keystroke in the system.
"""
import base64
import collections
import concurrent.futures
import copy
import ctypes
import ctypes.wintypes as wt
import glob
import hashlib
import io
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import winreg
from datetime import datetime

import webview

APP_VERSION = '1.0.7'

# pywebview's WinForms backend calls SetProcessDPIAware() itself, but only once
# webview.start() creates its window — our overlay window may be created first.
# DPI awareness is locked in by whichever window is created first, so set it now.
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

# ── Paths ────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
    RES = getattr(sys, '_MEIPASS', BASE)
else:
    BASE = RES = os.path.dirname(os.path.abspath(__file__))

# BINDBOARD_HOME lets tests run against a throw-away config directory.
WORK = os.environ.get('BINDBOARD_HOME') or os.path.join(os.path.expanduser('~'), 'BindBoard')
BACKUP_DIR = os.path.join(WORK, 'backups')
CONFIG = os.path.join(WORK, 'config.json')
LOG_FILE = os.path.join(WORK, 'bindboard.log')
os.makedirs(BACKUP_DIR, exist_ok=True)

log = logging.getLogger('bindboard')
if not log.handlers:
    try:
        _fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=256 * 1024, backupCount=1,
                                                   encoding='utf-8', delay=True)
        _fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(threadName)s: %(message)s'))
        log.addHandler(_fh)
    except Exception:
        log.addHandler(logging.NullHandler())
    log.setLevel(logging.INFO)
    log.propagate = False

# ── WinAPI ───────────────────────────────────────────────────
# Private WinDLL instances: argtypes/restype set here cannot clash with other libraries
# (pystray configures the shared ctypes.windll.user32 functions its own way). Every handle is
# declared pointer-sized — passing 64-bit HDC/HBRUSH values through ctypes' default C int
# overflowed and broke the overlay and the app icons before.
_user32 = ctypes.WinDLL('user32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
_shell32 = ctypes.WinDLL('shell32', use_last_error=True)
_ole32 = ctypes.WinDLL('ole32', use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wt.UINT), ('style', wt.UINT), ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
        ('hInstance', wt.HINSTANCE), ('hIcon', wt.HICON), ('hCursor', wt.HANDLE),
        ('hbrBackground', wt.HBRUSH), ('lpszMenuName', wt.LPCWSTR),
        ('lpszClassName', wt.LPCWSTR), ('hIconSm', wt.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [('hdc', wt.HDC), ('fErase', wt.BOOL), ('rcPaint', wt.RECT),
                ('fRestore', wt.BOOL), ('fIncUpdate', wt.BOOL), ('rgbReserved', ctypes.c_byte * 32)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [('usUsagePage', wt.USHORT), ('usUsage', wt.USHORT),
                ('dwFlags', wt.DWORD), ('hwndTarget', wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [('dwType', wt.DWORD), ('dwSize', wt.DWORD),
                ('hDevice', wt.HANDLE), ('wParam', wt.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [('MakeCode', wt.USHORT), ('Flags', wt.USHORT), ('Reserved', wt.USHORT),
                ('VKey', wt.USHORT), ('Message', wt.UINT), ('ExtraInformation', wt.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [('header', RAWINPUTHEADER), ('keyboard', RAWKEYBOARD)]


class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [('hDevice', wt.HANDLE), ('dwType', wt.DWORD)]


class _KBD_INFO(ctypes.Structure):
    _fields_ = [('dwType', wt.DWORD), ('dwSubType', wt.DWORD), ('dwKeyboardMode', wt.DWORD),
                ('dwNumberOfFunctionKeys', wt.DWORD), ('dwNumberOfIndicators', wt.DWORD),
                ('dwNumberOfKeysTotal', wt.DWORD)]


class RID_DEVICE_INFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('dwType', wt.DWORD), ('keyboard', _KBD_INFO)]


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [('hIcon', wt.HICON), ('iIcon', ctypes.c_int), ('dwAttributes', wt.DWORD),
                ('szDisplayName', ctypes.c_wchar * 260), ('szTypeName', ctypes.c_wchar * 80)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG),
                ('biPlanes', wt.WORD), ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', wt.LONG),
                ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD), ('biClrImportant', wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('rcMonitor', wt.RECT), ('rcWork', wt.RECT), ('dwFlags', wt.DWORD)]


GetModuleHandleW = _fn(_kernel32, 'GetModuleHandleW', wt.HMODULE, wt.LPCWSTR)
GetCurrentThreadId = _fn(_kernel32, 'GetCurrentThreadId', wt.DWORD)
OpenProcess = _fn(_kernel32, 'OpenProcess', wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
CloseHandle = _fn(_kernel32, 'CloseHandle', wt.BOOL, wt.HANDLE)
QueryFullProcessImageNameW = _fn(_kernel32, 'QueryFullProcessImageNameW', wt.BOOL,
                                 wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
K32EnumProcesses = _fn(_kernel32, 'K32EnumProcesses', wt.BOOL,
                       ctypes.POINTER(wt.DWORD), wt.DWORD, ctypes.POINTER(wt.DWORD))
CreateMutexW = _fn(_kernel32, 'CreateMutexW', wt.HANDLE, wt.LPVOID, wt.BOOL, wt.LPCWSTR)
GetUserDefaultUILanguage = _fn(_kernel32, 'GetUserDefaultUILanguage', wt.WORD)

RegisterClassExW = _fn(_user32, 'RegisterClassExW', wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
UnregisterClassW = _fn(_user32, 'UnregisterClassW', wt.BOOL, wt.LPCWSTR, wt.HINSTANCE)
CreateWindowExW = _fn(_user32, 'CreateWindowExW', wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
                      wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
DestroyWindow = _fn(_user32, 'DestroyWindow', wt.BOOL, wt.HWND)
DefWindowProcW = _fn(_user32, 'DefWindowProcW', LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
GetMessageW = _fn(_user32, 'GetMessageW', wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
TranslateMessage = _fn(_user32, 'TranslateMessage', wt.BOOL, ctypes.POINTER(wt.MSG))
DispatchMessageW = _fn(_user32, 'DispatchMessageW', LRESULT, ctypes.POINTER(wt.MSG))
PostMessageW = _fn(_user32, 'PostMessageW', wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
PostQuitMessage = _fn(_user32, 'PostQuitMessage', None, ctypes.c_int)
RegisterRawInputDevices = _fn(_user32, 'RegisterRawInputDevices', wt.BOOL,
                              ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT)
GetRawInputData = _fn(_user32, 'GetRawInputData', wt.UINT, wt.HANDLE, wt.UINT, wt.LPVOID,
                      ctypes.POINTER(wt.UINT), wt.UINT)
GetRawInputDeviceList = _fn(_user32, 'GetRawInputDeviceList', wt.UINT, wt.LPVOID,
                            ctypes.POINTER(wt.UINT), wt.UINT)
GetRawInputDeviceInfoW = _fn(_user32, 'GetRawInputDeviceInfoW', wt.UINT, wt.HANDLE, wt.UINT,
                             wt.LPVOID, ctypes.POINTER(wt.UINT))
GetForegroundWindow = _fn(_user32, 'GetForegroundWindow', wt.HWND)
GetWindowThreadProcessId = _fn(_user32, 'GetWindowThreadProcessId', wt.DWORD, wt.HWND,
                               ctypes.POINTER(wt.DWORD))
GetWindowTextLengthW = _fn(_user32, 'GetWindowTextLengthW', ctypes.c_int, wt.HWND)
GetWindowTextW = _fn(_user32, 'GetWindowTextW', ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
FindWindowW = _fn(_user32, 'FindWindowW', wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
GetDC = _fn(_user32, 'GetDC', wt.HDC, wt.HWND)
ReleaseDC = _fn(_user32, 'ReleaseDC', ctypes.c_int, wt.HWND, wt.HDC)
BeginPaint = _fn(_user32, 'BeginPaint', wt.HDC, wt.HWND, ctypes.POINTER(PAINTSTRUCT))
EndPaint = _fn(_user32, 'EndPaint', wt.BOOL, wt.HWND, ctypes.POINTER(PAINTSTRUCT))
FillRect = _fn(_user32, 'FillRect', ctypes.c_int, wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH)
DrawTextW = _fn(_user32, 'DrawTextW', ctypes.c_int, wt.HDC, wt.LPCWSTR, ctypes.c_int,
                ctypes.POINTER(wt.RECT), wt.UINT)
InvalidateRect = _fn(_user32, 'InvalidateRect', wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT), wt.BOOL)
UpdateWindow = _fn(_user32, 'UpdateWindow', wt.BOOL, wt.HWND)
SetTimer = _fn(_user32, 'SetTimer', ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID)
KillTimer = _fn(_user32, 'KillTimer', wt.BOOL, wt.HWND, ctypes.c_size_t)
ShowWindow = _fn(_user32, 'ShowWindow', wt.BOOL, wt.HWND, ctypes.c_int)
IsIconic = _fn(_user32, 'IsIconic', wt.BOOL, wt.HWND)
SetWindowPos = _fn(_user32, 'SetWindowPos', wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                   ctypes.c_int, ctypes.c_int, wt.UINT)
GetWindowRect = _fn(_user32, 'GetWindowRect', wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
GetSystemMetrics = _fn(_user32, 'GetSystemMetrics', ctypes.c_int, ctypes.c_int)
GetWindowLongW = _fn(_user32, 'GetWindowLongW', wt.LONG, wt.HWND, ctypes.c_int)
SetWindowLongW = _fn(_user32, 'SetWindowLongW', wt.LONG, wt.HWND, ctypes.c_int, wt.LONG)
SetLayeredWindowAttributes = _fn(_user32, 'SetLayeredWindowAttributes', wt.BOOL, wt.HWND,
                                 wt.COLORREF, ctypes.c_ubyte, wt.DWORD)
DrawIconEx = _fn(_user32, 'DrawIconEx', wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, wt.HICON,
                 ctypes.c_int, ctypes.c_int, wt.UINT, wt.HBRUSH, wt.UINT)
DestroyIcon = _fn(_user32, 'DestroyIcon', wt.BOOL, wt.HICON)
GetCursorPos = _fn(_user32, 'GetCursorPos', wt.BOOL, ctypes.POINTER(wt.POINT))
MonitorFromPoint = _fn(_user32, 'MonitorFromPoint', wt.HMONITOR, wt.POINT, wt.DWORD)
GetMonitorInfoW = _fn(_user32, 'GetMonitorInfoW', wt.BOOL, wt.HMONITOR, ctypes.POINTER(MONITORINFO))
SetForegroundWindow = _fn(_user32, 'SetForegroundWindow', wt.BOOL, wt.HWND)

GetDeviceCaps = _fn(_gdi32, 'GetDeviceCaps', ctypes.c_int, wt.HDC, ctypes.c_int)
CreateSolidBrush = _fn(_gdi32, 'CreateSolidBrush', wt.HBRUSH, wt.COLORREF)
CreateFontW = _fn(_gdi32, 'CreateFontW', wt.HFONT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                  ctypes.c_int, ctypes.c_int, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                  wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR)
SelectObject = _fn(_gdi32, 'SelectObject', wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
DeleteObject = _fn(_gdi32, 'DeleteObject', wt.BOOL, wt.HGDIOBJ)
SetBkMode = _fn(_gdi32, 'SetBkMode', ctypes.c_int, wt.HDC, ctypes.c_int)
SetTextColor = _fn(_gdi32, 'SetTextColor', wt.COLORREF, wt.HDC, wt.COLORREF)
CreateCompatibleDC = _fn(_gdi32, 'CreateCompatibleDC', wt.HDC, wt.HDC)
DeleteDC = _fn(_gdi32, 'DeleteDC', wt.BOOL, wt.HDC)
CreateDIBSection = _fn(_gdi32, 'CreateDIBSection', wt.HBITMAP, wt.HDC, ctypes.POINTER(BITMAPINFO),
                       wt.UINT, ctypes.POINTER(wt.LPVOID), wt.HANDLE, wt.DWORD)
GdiFlush = _fn(_gdi32, 'GdiFlush', wt.BOOL)

CreateFileW = _fn(_kernel32, 'CreateFileW', wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID,
                  wt.DWORD, wt.DWORD, wt.HANDLE)
_cfgmgr32 = ctypes.WinDLL('cfgmgr32')
CM_Locate_DevNodeW = _fn(_cfgmgr32, 'CM_Locate_DevNodeW', wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPCWSTR, wt.ULONG)
CM_Get_Parent = _fn(_cfgmgr32, 'CM_Get_Parent', wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD, wt.ULONG)
CM_Get_Device_IDW = _fn(_cfgmgr32, 'CM_Get_Device_IDW', wt.DWORD, wt.DWORD, wt.LPWSTR, wt.ULONG, wt.ULONG)
_hid = ctypes.WinDLL('hid')
HidD_GetProductString = _fn(_hid, 'HidD_GetProductString', wt.BOOLEAN, wt.HANDLE, wt.LPVOID, wt.ULONG)
HidD_GetManufacturerString = _fn(_hid, 'HidD_GetManufacturerString', wt.BOOLEAN, wt.HANDLE, wt.LPVOID, wt.ULONG)
SHGetFileInfoW = _fn(_shell32, 'SHGetFileInfoW', ctypes.c_size_t, wt.LPCWSTR, wt.DWORD,
                     ctypes.POINTER(SHFILEINFOW), wt.UINT, wt.UINT)
ImageList_GetIcon = _fn(ctypes.WinDLL('comctl32'), 'ImageList_GetIcon', wt.HICON, wt.HANDLE,
                        ctypes.c_int, wt.UINT)
CoInitializeEx = _fn(_ole32, 'CoInitializeEx', ctypes.c_long, wt.LPVOID, wt.DWORD)

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_INPUT_DEVICE_CHANGE = 0x00FE
WM_INPUT = 0x00FF
WM_APP = 0x8000
WM_OV_UPDATE = WM_APP + 1       # overlay: update text + show
WM_BB_ACTIVATE = WM_APP + 2     # a second BindBoard instance asks us to show the window
RIDEV_INPUTSINK = 0x00000100
RIDEV_DEVNOTIFY = 0x00002000
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RIDI_DEVICEINFO = 0x2000000B
RIM_TYPEKEYBOARD = 1
RI_KEY_BREAK = 0x01
RI_KEY_E0 = 0x02
GIDC_ARRIVAL = 1
GIDC_REMOVAL = 2
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
GWL_EXSTYLE = -20
LWA_ALPHA = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DT_SINGLELINE, DT_VCENTER, DT_END_ELLIPSIS = 0x0020, 0x0004, 0x8000
BK_TRANSPARENT = 1
ERROR_ALREADY_EXISTS = 183
_UINT_ERR = 0xFFFFFFFF

_VIDPID_RE = re.compile(r'(VID_[0-9A-F]+&PID_[0-9A-F]+)')
_HMOD = GetModuleHandleW(None)
_com_local = threading.local()


def _ensure_com():
    """Initialise COM (STA) once per thread — ShellExecute/SHGetFileInfo on .lnk need it."""
    if not getattr(_com_local, 'ok', False):
        try:
            CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        except Exception:
            pass
        _com_local.ok = True


def _register_window_class(name, wndproc_cb):
    UnregisterClassW(name, _HMOD)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = wndproc_cb
    wc.hInstance = _HMOD
    wc.lpszClassName = name
    return RegisterClassExW(ctypes.byref(wc))


def _instance_key():
    """Distinguishes app instances by config directory (lets tests run beside a real copy)."""
    return hashlib.md5(os.path.normcase(os.path.abspath(WORK)).encode('utf-8')).hexdigest()[:10]


# ── Icons (installed apps picker) ────────────────────────────
def _hicon_to_png_b64(hicon, size=32):
    """Render an HICON with correct transparency. The icon is drawn once on black and once on
    white; the difference between the two gives per-pixel alpha, which works for both modern
    alpha icons and old mask-based ones."""
    from PIL import Image, ImageChops
    n = size * size * 4
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    screen = GetDC(None)
    dc = CreateCompatibleDC(screen)
    bits = wt.LPVOID()
    hbm = CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    if not hbm or not bits.value:
        DeleteDC(dc)
        ReleaseDC(None, screen)
        return None
    old = SelectObject(dc, hbm)
    shots = []
    try:
        for bg in (0x00, 0xFF):
            ctypes.memset(bits.value, bg, n)
            DrawIconEx(dc, 0, 0, hicon, size, size, 0, None, 0x0003)  # DI_NORMAL
            GdiFlush()
            shots.append(ctypes.string_at(bits.value, n))
    finally:
        SelectObject(dc, old)
        DeleteObject(hbm)
        DeleteDC(dc)
        ReleaseDC(None, screen)
    black = Image.frombuffer('RGB', (size, size), shots[0], 'raw', 'BGRX', 0, 1)
    white = Image.frombuffer('RGB', (size, size), shots[1], 'raw', 'BGRX', 0, 1)
    r, g, b = ImageChops.subtract(white, black).split()
    alpha = ImageChops.invert(ImageChops.lighter(ImageChops.lighter(r, g), b))
    if not alpha.getbbox():
        return None  # nothing visible was drawn
    img = Image.merge('RGBa', (*black.split(), alpha)).convert('RGBA')
    bio = io.BytesIO()
    img.save(bio, 'PNG', optimize=True)
    return 'data:image/png;base64,' + base64.b64encode(bio.getvalue()).decode('ascii')


_imagelist_lock = threading.Lock()


def _get_icon_b64(path, size=32):
    """Shell icon of a file/shortcut as a PNG data URI, or None. Taken from the system image
    list, which stores the shortcut arrow as a separate overlay — so app icons come out clean."""
    try:
        _ensure_com()
        shfi = SHFILEINFOW()
        himl = SHGetFileInfoW(path, 0, ctypes.byref(shfi), ctypes.sizeof(shfi),
                              0x4000)  # SHGFI_SYSICONINDEX | SHGFI_LARGEICON
        if not himl:
            return None
        with _imagelist_lock:
            hicon = ImageList_GetIcon(himl, shfi.iIcon, 0x1)  # ILD_TRANSPARENT
        if not hicon:
            return None
        try:
            return _hicon_to_png_b64(hicon, size)
        finally:
            DestroyIcon(hicon)
    except Exception:
        log.exception('icon extraction failed for %s', path)
        return None


# ── App filter ────────────────────────────────────────────────
def _get_foreground_info():
    """Return (exe_lower, title_lower) of the foreground window."""
    try:
        hwnd = GetForegroundWindow()
        if not hwnd:
            return '', ''
        pid = wt.DWORD(0)
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ''
        if pid.value:
            hproc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if hproc:
                try:
                    buf = ctypes.create_unicode_buffer(32768)
                    sz = wt.DWORD(32768)
                    if QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                        exe = buf.value.rsplit('\\', 1)[-1].lower()
                finally:
                    CloseHandle(hproc)
        title = ''
        tlen = GetWindowTextLengthW(hwnd)
        if tlen > 0:
            tbuf = ctypes.create_unicode_buffer(tlen + 1)
            GetWindowTextW(hwnd, tbuf, tlen + 1)
            title = tbuf.value.lower()
        return exe, title
    except Exception:
        return '', ''


def _get_foreground_exe():
    return _get_foreground_info()[0]


def compile_app_filter(af):
    """Pre-process the app filter once per config change instead of on every key press.
    Patterns: 'x.exe' = exact process name; 'x.lnk' = shortcut stem found in exe name or
    window title; anything else = substring of exe name or window title."""
    if not isinstance(af, dict):
        return None
    mode = af.get('mode', 'disabled')
    if mode not in ('allow', 'deny'):
        return None
    exes, subs = set(), []
    for pat in af.get('apps') or []:
        p = str(pat).lower().strip()
        if not p:
            continue
        if p.endswith('.exe'):
            exes.add(p)
        elif p.endswith('.lnk'):
            if p[:-4]:
                subs.append(p[:-4])
        else:
            subs.append(p)
    return mode, frozenset(exes), tuple(subs)


def filter_allows(compiled, exe, title):
    if not compiled:
        return True
    mode, exes, subs = compiled
    hit = exe in exes or any(s in exe or s in title for s in subs)
    return not hit if mode == 'deny' else hit


# ── Key normalisation ─────────────────────────────────────────
# Raw Input reports generic VK_SHIFT/VK_CONTROL/VK_MENU (16/17/18) for both sides, while the
# layouts store left/right codes 160–165 — so binds on those six keys never fired. Numpad keys
# change their VK with NumLock (numpad 7 is VK_HOME when NumLock is off), so they are mapped
# by scan code to stay the same key whatever the NumLock state.
_NUMPAD_SCAN_VK = {0x47: 103, 0x48: 104, 0x49: 105, 0x4B: 100, 0x4C: 101, 0x4D: 102,
                   0x4F: 97, 0x50: 98, 0x51: 99, 0x52: 96, 0x53: 110}


def normalize_vk(vk, scan, e0):
    """Return the layout key code for a raw keyboard event, or None for fake/unmapped keys."""
    if vk in (0, 0xFF):
        return None
    if vk == 16:            # VK_SHIFT
        if e0:
            return None     # fake shift the keyboard sends around extended keys
        return 161 if scan == 0x36 else 160
    if vk == 17:            # VK_CONTROL
        return 163 if e0 else 162
    if vk == 18:            # VK_MENU
        return 165 if e0 else 164
    if not e0 and scan in _NUMPAD_SCAN_VK:
        return _NUMPAD_SCAN_VK[scan]
    return vk


# ── Raw Input devices ─────────────────────────────────────────
def _device_name(hdev):
    n = wt.UINT(0)
    if GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, None, ctypes.byref(n)) == _UINT_ERR or not n.value:
        return ''
    buf = ctypes.create_unicode_buffer(n.value + 1)
    if GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, buf, ctypes.byref(n)) == _UINT_ERR:
        return ''
    return buf.value


_VIDPID_BT_RE = re.compile(r'VID&[0-9A-F]{4}([0-9A-F]{4})_PID&([0-9A-F]{4})')


def _vid_pid_of_name(name):
    """'VID_xxxx&PID_yyyy' from a device path (USB form, or the '_VID&0002xxxx_PID&yyyy' form
    Bluetooth keyboards use), else ''."""
    up = name.upper()
    m = _VIDPID_RE.search(up)
    if m:
        return m.group(1)
    m = _VIDPID_BT_RE.search(up)
    return f'VID_{m.group(1)}&PID_{m.group(2)}' if m else ''


def _instance_id_of_path(path):
    """'\\\\?\\HID#VID_x&PID_y&MI_00#8&abc&0&0000#{guid}' → 'HID\\VID_x&PID_y&MI_00\\8&abc&0&0000'."""
    p = path[4:] if path.startswith('\\\\?\\') else path
    if '#{' in p:
        p = p[:p.rindex('#{')]
    return p.replace('#', '\\')


_phys_cache = {}


def _physical_id(path):
    """Device-manager ID of the physical device behind a raw-input interface: walk up from the
    HID node past interface (&MI_) nodes. One keyboard's interfaces share it, and two identical
    keyboards get different ones — so it tells two keyboards of the same model apart."""
    if path in _phys_cache:
        return _phys_cache[path]
    phys = ''
    try:
        node = wt.DWORD()
        if CM_Locate_DevNodeW(ctypes.byref(node), _instance_id_of_path(path), 0) == 0:
            buf = ctypes.create_unicode_buffer(512)
            for _ in range(8):
                if CM_Get_Device_IDW(node.value, buf, 512, 0) != 0:
                    break
                dev_id = buf.value.upper()
                if not dev_id.startswith('HID\\') and '&MI_' not in dev_id:
                    phys = dev_id
                    break
                parent = wt.DWORD()
                if CM_Get_Parent(ctypes.byref(parent), node.value, 0) != 0:
                    break
                node = parent
    except Exception:
        log.exception('physical device lookup failed for %s', path)
    _phys_cache[path] = phys
    return phys


_hid_names = {}


def _hid_strings(path):
    """(product, manufacturer) as the device reports them, e.g. ('Dell Smart Card Reader Keyboard',
    'Dell'). Opening with no access rights is allowed even for keyboards Windows owns."""
    if path in _hid_names:
        return _hid_names[path]
    product = manufacturer = ''
    h = CreateFileW(path, 0, 0x3, None, 3, 0, None)  # FILE_SHARE_READ|WRITE, OPEN_EXISTING
    if h and h != ctypes.c_void_p(-1).value:
        try:
            buf = ctypes.create_unicode_buffer(256)
            if HidD_GetProductString(h, buf, ctypes.sizeof(buf)):
                product = buf.value.strip()
            buf = ctypes.create_unicode_buffer(256)
            if HidD_GetManufacturerString(h, buf, ctypes.sizeof(buf)):
                manufacturer = buf.value.strip()
        finally:
            CloseHandle(h)
    _hid_names[path] = (product, manufacturer)
    return product, manufacturer


def _enum_raw_keyboards(dev_type=RIM_TYPEKEYBOARD):
    """One pass over connected keyboards (or mice with dev_type=0) →
    [{'handle', 'path', 'vid_pid', 'phys', 'keys'}]."""
    num = wt.UINT(0)
    size = ctypes.sizeof(RAWINPUTDEVICELIST)
    if GetRawInputDeviceList(None, ctypes.byref(num), size) == _UINT_ERR or not num.value:
        return []
    num.value += 4  # room for a device plugged in between the two calls
    devs = (RAWINPUTDEVICELIST * num.value)()
    got = GetRawInputDeviceList(devs, ctypes.byref(num), size)
    if got == _UINT_ERR:
        return []
    out = []
    for d in devs[:got]:
        if d.dwType != dev_type:
            continue
        path = _device_name(d.hDevice)
        if dev_type != RIM_TYPEKEYBOARD:
            vid_pid = _vid_pid_of_name(path)
            out.append({'handle': d.hDevice or 0, 'path': path, 'vid_pid': vid_pid,
                        'phys': _physical_id(path) if vid_pid else '', 'keys': 0})
            continue
        info = RID_DEVICE_INFO()
        info.cbSize = ctypes.sizeof(RID_DEVICE_INFO)
        isz = wt.UINT(ctypes.sizeof(RID_DEVICE_INFO))
        keys = 0
        if GetRawInputDeviceInfoW(d.hDevice, RIDI_DEVICEINFO, ctypes.byref(info), ctypes.byref(isz)) != _UINT_ERR:
            keys = info.keyboard.dwNumberOfKeysTotal
        vid_pid = _vid_pid_of_name(path)
        out.append({'handle': d.hDevice or 0, 'path': path, 'vid_pid': vid_pid,
                    'phys': _physical_id(path) if vid_pid else '', 'keys': keys})
    return out


def resolve_device(vid_pid, instance='', keyboards=None):
    """Which keyboard events to accept for a saved selection → (connected, phys_filter).
    phys_filter '' accepts every keyboard with this VID_PID (old configs, or a keyboard that
    moved to another USB port while it is the only one of its model)."""
    vid_pid = (vid_pid or '').upper()
    instance = (instance or '').upper()
    kbs = [k for k in (keyboards if keyboards is not None else _enum_raw_keyboards()) if k['vid_pid'] == vid_pid]
    if not vid_pid or not kbs:
        return False, instance
    if not instance:
        return True, ''
    physes = {k['phys'] for k in kbs}
    if instance in physes:
        return True, instance
    if len(physes) == 1:
        return True, ''
    return False, instance   # several identical keyboards, none on the remembered port


def _device_connected(vid_pid, instance=''):
    return resolve_device(vid_pid, instance)[0]


_MI_RE = re.compile(r'&MI_([0-9A-F]{2})')


def _interfaces(paths):
    return {m.group(1) for p in paths for m in [_MI_RE.search(p.upper())] if m}


def list_keyboards(devices=None, mice=None):
    """Connected keyboards, one entry per physical device, named the way the device names itself.
    Every device with a VID/PID is listed: the old rule (show only 1–263 reported keys) hid plain
    keyboards, which report 264 keys on their only interface. Several keyboards of one model get
    dup_index 1, 2, … so they can be told apart and selected separately.
    kind='mouse' marks gaming mice that also present a keyboard interface (for side buttons):
    their pointer sits on a USB interface of its own, while a keyboard's "mouse keys" collection
    shares the keyboard's interface. They stay selectable but are listed last."""
    devices = _enum_raw_keyboards() if devices is None else devices
    mice = _enum_raw_keyboards(0) if mice is None else mice
    mouse_ifaces = collections.defaultdict(set)
    for m in mice:
        if m.get('phys'):
            mouse_ifaces[m['phys']] |= _interfaces([m['path']])
    groups = collections.OrderedDict()
    for d in devices:
        vp = d['vid_pid']
        if not vp or 'VHIDEV' in d['path'].upper():
            continue
        groups.setdefault(d.get('phys') or vp, []).append(d)
    out = []
    for devs in groups.values():
        devs = sorted(devs, key=lambda d: d['path'].upper())
        vp = devs[0]['vid_pid']
        product = manufacturer = ''
        for d in devs:
            p, m = _hid_strings(d['path'])
            product, manufacturer = product or p, manufacturer or m
        counts = [d['keys'] for d in devs if 0 < d['keys'] < 264]
        kc = min(counts) if counts else 0
        phys = devs[0].get('phys') or ''
        pointer_only = mouse_ifaces.get(phys, set()) - _interfaces(d['path'] for d in devs)
        out.append({'name': product or _get_friendly_name(vp) or 'HID Keyboard Device',
                    'manufacturer': manufacturer, 'vid_pid': vp, 'instance': phys,
                    'instance_id': devs[0]['path'], 'key_count': kc, 'size_label': _estimate_size(kc),
                    'dup_index': 0, 'kind': 'mouse' if pointer_only else 'keyboard'})
    out.sort(key=lambda kb: kb['kind'] == 'mouse')  # stable: keyboards first, enumeration order kept
    by_model = collections.defaultdict(list)
    for kb in out:
        by_model[kb['vid_pid']].append(kb)
    for same in by_model.values():
        if len(same) > 1:
            for i, kb in enumerate(sorted(same, key=lambda k: k['instance']), 1):
                kb['dup_index'] = i
    return out


def _estimate_size(key_count):
    if key_count <= 0 or key_count > 200:
        return '?'
    if key_count <= 48:
        return '40%'
    if key_count <= 64:
        return '60%'
    if key_count <= 72:
        return '65%'
    if key_count <= 85:
        return '75%'
    if key_count <= 95:
        return 'TKL'
    return '100%'


_friendly_cache = {}


def _get_friendly_name(vid_pid):
    """FriendlyName for VID_xxxx&PID_xxxx from the registry (cached)."""
    if vid_pid in _friendly_cache:
        return _friendly_cache[vid_pid]
    name = ''
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf'SYSTEM\CurrentControlSet\Enum\HID\{vid_pid}') as root:
            i = 0
            while not name:
                try:
                    inst = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, inst) as k:
                        name = winreg.QueryValueEx(k, 'FriendlyName')[0] or ''
                except OSError:
                    pass
    except OSError:
        pass
    _friendly_cache[vid_pid] = name
    return name


class KeyEvent:
    __slots__ = ('hdev', 'vid_pid', 'vk', 'scan', 'e0', 'up', 't', 'phys')

    def __init__(self, hdev, vid_pid, vk, scan, e0, up, t, phys=''):
        self.hdev, self.vid_pid, self.vk, self.scan, self.e0, self.up, self.t = hdev, vid_pid, vk, scan, e0, up, t
        self.phys = phys

    @property
    def key(self):
        return normalize_vk(self.vk, self.scan, self.e0)

    def matches(self, vid_pid, phys_filter=''):
        return self.vid_pid == vid_pid and (not phys_filter or self.phys == phys_filter)


class DeviceEvent:
    __slots__ = ('kind', 'hdev', 'vid_pid')

    def __init__(self, kind, hdev, vid_pid):
        self.kind, self.hdev, self.vid_pid = kind, hdev, vid_pid


class RawInputHub:
    """Owns the process-wide keyboard Raw Input registration; fans events out to subscribers.

    Subscribers are callables taking a KeyEvent or DeviceEvent. They run on the hub thread and
    must return quickly. The thread starts on the first subscription and then just sleeps in
    GetMessage (no polling, no hook)."""

    CLASS_NAME = 'BBRawInputHub'

    def __init__(self):
        self._lock = threading.Lock()
        self._subs = ()
        self._thread = None
        self._hwnd = None
        self._ready = threading.Event()
        self._names = {}          # hDevice → (VID_PID, physical device id); '' when unknown
        self._buf = RAWINPUT()
        self._wndproc = WNDPROC(self._wnd_proc)
        # Tests only: treat SendInput-injected keys (hDevice NULL) as coming from this VID_PID.
        self.injected_as = os.environ.get('BINDBOARD_TEST_INJECTED_AS', '').upper()

    def subscribe(self, fn):
        with self._lock:
            if fn not in self._subs:
                self._subs = self._subs + (fn,)
        return self._ensure_running()

    def unsubscribe(self, fn):
        with self._lock:
            self._subs = tuple(s for s in self._subs if s != fn)

    def alive(self):
        return self._thread is not None and self._thread.is_alive() and self._hwnd is not None

    def _ensure_running(self):
        with self._lock:
            if self.alive():
                return True
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name='RawInputHub', daemon=True)
            self._thread.start()
        self._ready.wait(3)
        return self.alive()

    def device_of(self, hdev):
        """(VID_PID, physical device id) of a raw-input handle, cached per handle."""
        if not hdev:
            return self.injected_as, ''
        dev = self._names.get(hdev)
        if dev is None:
            path = _device_name(hdev)
            vp = _vid_pid_of_name(path)
            dev = (vp, _physical_id(path) if vp else '')
            self._names[hdev] = dev
        return dev

    def vid_pid_of(self, hdev):
        return self.device_of(hdev)[0]

    def _run(self):
        try:
            _register_window_class(self.CLASS_NAME, self._wndproc)
            hwnd = CreateWindowExW(0, self.CLASS_NAME, self.CLASS_NAME, WS_POPUP,
                                   0, 0, 0, 0, None, None, _HMOD, None)
            if not hwnd:
                log.error('raw input hub: CreateWindowEx failed (%s)', ctypes.get_last_error())
                return
            rid = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK | RIDEV_DEVNOTIFY, hwnd)
            if not RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)):
                log.error('raw input hub: RegisterRawInputDevices failed (%s)', ctypes.get_last_error())
                DestroyWindow(hwnd)
                return
            self._hwnd = hwnd
            self._ready.set()
            msg = wt.MSG()
            while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                TranslateMessage(ctypes.byref(msg))
                DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception('raw input hub crashed')
        finally:
            self._hwnd = None
            self._ready.set()

    def _dispatch(self, ev):
        for fn in self._subs:
            try:
                fn(ev)
            except Exception:
                log.exception('raw input subscriber failed')

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                ri = self._buf
                sz = wt.UINT(ctypes.sizeof(RAWINPUT))
                got = GetRawInputData(lparam, RID_INPUT, ctypes.byref(ri), ctypes.byref(sz),
                                      ctypes.sizeof(RAWINPUTHEADER))
                if got != _UINT_ERR and got and ri.header.dwType == RIM_TYPEKEYBOARD and self._subs:
                    kb = ri.keyboard
                    hdev = ri.header.hDevice or 0
                    vid_pid, phys = self.device_of(hdev)
                    self._dispatch(KeyEvent(hdev, vid_pid, kb.VKey, kb.MakeCode,
                                            bool(kb.Flags & RI_KEY_E0), bool(kb.Flags & RI_KEY_BREAK),
                                            time.perf_counter(), phys))
            except Exception:
                log.exception('WM_INPUT handling failed')
            return DefWindowProcW(hwnd, msg, wparam, lparam)  # lets Windows free the input
        if msg == WM_INPUT_DEVICE_CHANGE:
            hdev = ctypes.c_void_p(lparam).value or 0
            if wparam == GIDC_REMOVAL:
                vp = (self._names.pop(hdev, None) or ('', ''))[0]
                self._dispatch(DeviceEvent('removed', hdev, vp))
            else:
                self._names.pop(hdev, None)
                self._dispatch(DeviceEvent('arrived', hdev, self.vid_pid_of(hdev)))
            return 0
        if msg == WM_DESTROY:
            PostQuitMessage(0)
            return 0
        return DefWindowProcW(hwnd, msg, wparam, lparam)


# ── Actions ───────────────────────────────────────────────────
def _run_with_timeout(fn, timeout):
    box = {}

    def target():
        try:
            box['r'] = fn()
        except Exception as e:  # noqa: BLE001
            box['e'] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError('no response')
    if 'e' in box:
        raise box['e']
    return box.get('r')


class Soundpad:
    """Soundpad remote control. Uses Soundpad's named pipe (instant, no process start) and
    falls back to `Soundpad.exe -rc`, which also starts Soundpad when it isn't running."""

    PIPE = r'\\.\pipe\sp_remote_control'

    def __init__(self):
        self._exe = None
        self._lock = threading.Lock()

    def _pipe_request(self, cmd):
        with open(self.PIPE, 'r+b', buffering=0) as p:
            p.write(cmd.encode('utf-8'))
            return p.read(4096).decode('utf-8', 'replace').strip('\x00 \r\n')

    def command(self, cmd):
        with self._lock:
            try:
                resp = _run_with_timeout(lambda: self._pipe_request(cmd), 2.0)
                if not (resp.startswith('R-') and not resp.startswith('R-2')):
                    return True, None
                # Unexpected answer: retry the classic way (`Soundpad.exe -rc`) rather than lose the sound
                log.warning('soundpad pipe answered %r to %r — falling back to Soundpad.exe', resp, cmd)
            except FileNotFoundError:
                pass  # Soundpad is not running — the exe below starts it and forwards the command
            except TimeoutError:
                return False, 'Soundpad is not responding'
            except OSError as e:
                if getattr(e, 'winerror', None) != 231:  # anything but ERROR_PIPE_BUSY is logged
                    log.warning('soundpad pipe error: %r', e)
            exe = self.find_exe()
            if not exe:
                return False, 'Soundpad not found'
            subprocess.Popen([exe, '-rc', cmd], creationflags=subprocess.CREATE_NO_WINDOW)
            return True, None

    def play(self, index):
        s = str(index).strip().lstrip('#')
        if not s.isdigit():
            return False, f'invalid sound number: {index!r}'
        return self.command(f'DoPlaySound({int(s)})')

    def stop(self):
        return self.command('DoStopSound()')

    def find_exe(self):
        if self._exe and os.path.isfile(self._exe):
            return self._exe
        cands = []
        steam = _steam_root()
        if steam:
            for lib in _steam_library_dirs(steam):
                cands.append(os.path.join(lib, 'common', 'Soundpad', 'Soundpad.exe'))
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                        r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
                try:
                    with winreg.OpenKey(root, sub) as uk:
                        i = 0
                        while True:
                            try:
                                name = winreg.EnumKey(uk, i)
                            except OSError:
                                break
                            i += 1
                            if 'soundpad' not in name.lower():
                                continue
                            try:
                                with winreg.OpenKey(uk, name) as ak:
                                    cands.append(os.path.join(winreg.QueryValueEx(ak, 'InstallLocation')[0],
                                                              'Soundpad.exe'))
                            except OSError:
                                pass
                except OSError:
                    pass
        cands += [r'C:\Program Files (x86)\Steam\steamapps\common\Soundpad\Soundpad.exe',
                  r'C:\Program Files\Soundpad\Soundpad.exe']
        for c in cands:
            if c and os.path.isfile(c):
                self._exe = c
                return c
        return None


_soundpad = Soundpad()


def execute_action(bind):
    """Run one non-mode action. Returns (ok, error_message)."""
    btype = bind.get('type', '')
    v = str(bind.get('value', '') or '').strip()
    try:
        if btype in ('url', 'lnk', 'exe', 'app'):
            target = v.strip('"\'')
            if not target:
                return False, 'empty value'
            if target.lower().startswith('shell:appsfolder'):
                subprocess.Popen(['explorer.exe', target], creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                _ensure_com()
                os.startfile(target)  # exe, lnk, documents, steam://, https://, …
        elif btype == 'uwp':
            if not v:
                return False, 'empty value'
            subprocess.Popen(['explorer.exe', v], creationflags=subprocess.CREATE_NO_WINDOW)
        elif btype == 'steam':
            if not v:
                return False, 'empty value'
            _ensure_com()
            os.startfile(v if v.startswith('steam://') else f'steam://rungameid/{v}')
        elif btype == 'sound':
            return _soundpad.play(v)
        elif btype == 'stop_sound':
            return _soundpad.stop()
        else:
            return False, f'unknown action type: {btype}'
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)


class ActionRunner:
    """Single worker thread: actions run in order, off the input thread."""

    def __init__(self):
        self._q = queue.SimpleQueue()
        self._thread = None
        self._lock = threading.Lock()

    def submit(self, fn, *args):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name='ActionRunner', daemon=True)
                self._thread.start()
        self._q.put((fn, args))

    def _run(self):
        _ensure_com()
        while True:
            fn, args = self._q.get()
            try:
                fn(*args)
            except Exception:
                log.exception('action failed')


# ── Mode switch overlay (also the single-instance IPC window) ─
# NOTE: Win32 COLORREF is 0x00BBGGRR, so the indigo accent #7c8aff is 0xFF8A7C here.
_ov_hwnd = [None]
_ov_data = {'name': 'Mode', 'acc': 0xFF8A7C, 'prefix': 'Mode'}
_on_activate_request = []  # callbacks run when another instance asks us to show the window


def _overlay_class_name():
    return 'BBOverlay.' + _instance_key()


def _overlay_worker():
    hdc0 = GetDC(None)
    dpi = GetDeviceCaps(hdc0, 88) if hdc0 else 96  # LOGPIXELSX
    if hdc0:
        ReleaseDC(None, hdc0)
    scale = (dpi or 96) / 96.0
    W, H = round(320 * scale), round(56 * scale)
    BG_COL = 0x151313
    TEXT_COL = 0xECECEC
    cls_name = _overlay_class_name()

    def wnd_proc(hwnd, msg, wp, lp):
        try:
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                hdc = BeginPaint(hwnd, ctypes.byref(ps))
                if hdc:
                    hbr = CreateSolidBrush(BG_COL)
                    FillRect(hdc, ctypes.byref(wt.RECT(0, 0, W, H)), hbr)
                    DeleteObject(hbr)
                    hbr = CreateSolidBrush(_ov_data['acc'])
                    FillRect(hdc, ctypes.byref(wt.RECT(0, 0, round(4 * scale), H)), hbr)
                    DeleteObject(hbr)
                    hfont = CreateFontW(-round(15 * scale), 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 0, 0, 'Segoe UI')
                    old_font = SelectObject(hdc, hfont)
                    SetBkMode(hdc, BK_TRANSPARENT)
                    SetTextColor(hdc, TEXT_COL)
                    DrawTextW(hdc, f'{_ov_data["prefix"]}: {_ov_data["name"]}', -1,
                              ctypes.byref(wt.RECT(round(16 * scale), 0, W - round(8 * scale), H)),
                              DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS)
                    SelectObject(hdc, old_font)
                    DeleteObject(hfont)
                    EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == WM_OV_UPDATE:
                KillTimer(hwnd, 1)
                sw, sh = GetSystemMetrics(0), GetSystemMetrics(1)
                SetWindowPos(hwnd, None, (sw - W) // 2, int(sh * 0.83), W, H, 0x0050)  # SHOWWINDOW|NOACTIVATE
                InvalidateRect(hwnd, None, True)
                UpdateWindow(hwnd)
                SetTimer(hwnd, 1, 1200, None)
                return 0
            if msg == WM_TIMER:
                KillTimer(hwnd, 1)
                ShowWindow(hwnd, 0)
                return 0
            if msg == WM_BB_ACTIVATE:
                for cb in list(_on_activate_request):
                    threading.Thread(target=cb, daemon=True).start()
                return 0
            if msg == WM_DESTROY:
                PostQuitMessage(0)
                return 0
        except Exception:
            log.exception('overlay wndproc failed')
        return DefWindowProcW(hwnd, msg, wp, lp)

    cb_ref = WNDPROC(wnd_proc)
    _register_window_class(cls_name, cb_ref)
    ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    hwnd = CreateWindowExW(ex, cls_name, '', WS_POPUP, 0, 0, W, H, None, None, _HMOD, None)
    if not hwnd:
        UnregisterClassW(cls_name, _HMOD)
        return
    _ov_hwnd[0] = hwnd
    SetLayeredWindowAttributes(hwnd, 0, 215, LWA_ALPHA)
    msg = wt.MSG()
    while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        TranslateMessage(ctypes.byref(msg))
        DispatchMessageW(ctypes.byref(msg))
    _ov_hwnd[0] = None
    UnregisterClassW(cls_name, _HMOD)


def _start_overlay():
    threading.Thread(target=_overlay_worker, name='Overlay', daemon=True).start()


def _resolve_lang(lang):
    """'ru'/'en': the language chosen in the app, else the Windows display language."""
    lang = str(lang or '').lower()
    if lang in ('ru', 'en'):
        return lang
    try:
        if GetUserDefaultUILanguage() & 0x3FF == 0x19:  # LANG_RUSSIAN
            return 'ru'
    except Exception:
        pass
    return 'en'


def _show_mode_overlay(mode_name, color_hex='#7c8aff', lang=''):
    if not _ov_hwnd[0]:
        return
    _ov_data['prefix'] = 'Режим' if _resolve_lang(lang) == 'ru' else 'Mode'
    try:
        ch = (color_hex or '#7c8aff').lstrip('#')
        r, g, b = int(ch[0:2], 16), int(ch[2:4], 16), int(ch[4:6], 16)
        _ov_data['acc'] = r | (g << 8) | (b << 16)
    except Exception:
        _ov_data['acc'] = 0xFF8A7C
    _ov_data['name'] = mode_name
    PostMessageW(_ov_hwnd[0], WM_OV_UPDATE, 0, 0)


# ── Keyboard listener ─────────────────────────────────────────
_capture_lock = threading.Lock()
_capture_count = [0]   # >0 while the verifier/key scanner reads keys: binds stay quiet meanwhile


def _capture(delta):
    with _capture_lock:
        _capture_count[0] = max(0, _capture_count[0] + delta)


class KeyboardListener:
    NEXT_MODE = '__next__'

    def __init__(self, hub, runner):
        self._hub = hub
        self._runner = runner
        self._running = False
        self._vid_pid = ''
        self._instance = ''       # physical device id when several keyboards share the VID_PID
        self._phys_filter = ''
        self._dev_name = ''
        self._config = {}
        self._modes = {}
        self._mode_order = []
        self._mode = ''
        self._filter = None
        self._overlay = False
        self._pressed = set()
        self._connected = None
        self._log = collections.deque(maxlen=20)
        self.on_change = None   # called (no args) when running/mode/connection changes

    # configuration
    def set_config(self, config):
        config = config if isinstance(config, dict) else {}
        modes = [m for m in (config.get('modes') or []) if isinstance(m, dict) and m.get('id')]
        self._config = config
        self._modes = {m['id']: m for m in modes}
        self._mode_order = [m['id'] for m in modes]
        self._filter = compile_app_filter(config.get('app_filter'))
        self._overlay = bool(config.get('overlay_enabled'))
        if self._mode not in self._modes:
            self._mode = self._mode_order[0] if self._mode_order else ''

    def configure(self, config, vid_pid, dev_name='', instance=''):
        self._vid_pid = (vid_pid or '').upper()
        self._instance = (instance or '').upper()
        self._dev_name = dev_name or ''
        self._mode = ''
        self.set_config(config)

    # control
    def start(self):
        connected, self._phys_filter = resolve_device(self._vid_pid, self._instance)
        if not connected:
            return {'ok': False, 'error': 'device_not_found'}
        self._pressed = set()
        self._connected = True
        if not self._hub.subscribe(self._on_event):
            return {'ok': False, 'error': 'raw_input_unavailable'}
        self._running = True
        self._changed()
        return {'ok': True}

    def stop(self):
        self._hub.unsubscribe(self._on_event)
        was = self._running
        self._running = False
        self._pressed = set()
        if was:
            self._changed()

    @property
    def status(self):
        return {
            'running': self._running and self._hub.alive(),
            'mode': self._mode,
            'device': self._dev_name or self._vid_pid,
            'vid_pid': self._vid_pid,
            'instance': self._instance,
            'connected': self._connected,
        }

    def log_entries(self):
        return list(self._log)

    def _changed(self):
        cb = self.on_change
        if cb:
            try:
                cb()
            except Exception:
                log.exception('status callback failed')

    # input (runs on the hub thread — must stay fast)
    def _on_event(self, ev):
        if not self._running:
            return
        if isinstance(ev, DeviceEvent):
            if ev.vid_pid == self._vid_pid or not ev.vid_pid:
                connected, self._phys_filter = resolve_device(self._vid_pid, self._instance)
                if connected != self._connected:
                    self._connected = connected
                    self._pressed = set()
                    self._changed()
            return
        if not ev.matches(self._vid_pid, self._phys_filter):
            if not ev.up and ev.vid_pid:
                last = self._log[-1] if self._log else None
                if last and last.get('error') == 'wrong_device' and last.get('device') == ev.vid_pid:
                    last['count'] = last.get('count', 1) + 1
                else:
                    self._log.append({'vk': None, 'mode': self._mode, 'bind': None, 'ok': None,
                                      'error': 'wrong_device', 'device': ev.vid_pid, 'count': 1})
            return
        key = ev.key
        if key is None:
            return
        if ev.up:
            self._pressed.discard(key)
            return
        if key in self._pressed:
            return  # auto-repeat while the key is held
        self._pressed.add(key)
        self.handle_press(key, ev.t)

    def handle_press(self, key, t0=None):
        mode_id = self._mode
        mode = self._modes.get(mode_id) or {}
        bind = (mode.get('binds') or {}).get(str(key))
        entry = {'vk': key, 'mode': mode_id, 'bind': bind.get('type') if bind else None,
                 'ok': None, 'error': None}
        self._log.append(entry)
        if not bind:
            return entry
        if _capture_count[0] > 0:
            entry['note'] = 'capture'
            return entry
        if self._filter and not filter_allows(self._filter, *_get_foreground_info()):
            entry['note'] = 'filtered'
            return entry
        if bind.get('type') == 'mode_switch':
            target = self._resolve_target(bind.get('target', ''))
            if not target:
                entry['ok'], entry['error'] = False, 'no other mode to switch to'
                return entry
            self._mode = target
            entry['ok'] = True
            if self._overlay:
                m = self._modes.get(target, {})
                _show_mode_overlay(m.get('name', target), m.get('color', '#7c8aff'), self._config.get('lang', ''))
            self._changed()
            return entry
        self._runner.submit(self._run_action, bind, entry, t0 or time.perf_counter())
        return entry

    def _resolve_target(self, target):
        if target in self._modes and target != self.NEXT_MODE:
            return target
        # Empty / 'next' / dangling target (its mode was deleted or renamed): cycle to the next
        # mode instead of silently doing nothing and trapping the user in this mode.
        order = self._mode_order
        if len(order) < 2:
            return None
        i = order.index(self._mode) if self._mode in order else -1
        return order[(i + 1) % len(order)]

    @staticmethod
    def _run_action(bind, entry, t0):
        ok, err = execute_action(bind)
        entry['ok'], entry['error'] = ok, err
        entry['ms'] = round((time.perf_counter() - t0) * 1000, 1)


# ── Keyboard verifier / key scanner (share the hub) ───────────
class KeyboardVerifier:
    """Waits (≤10 s) for any key from the chosen keyboard."""

    def __init__(self, hub):
        self._hub = hub
        self._lock = threading.Lock()
        self._busy = False
        self._detected = False
        self._start = 0.0
        self._vid_pid = ''
        self._phys = ''
        self._timer = None

    def start(self, vid_pid='', instance=''):
        phys = resolve_device(vid_pid, instance)[1] if vid_pid else ''
        with self._lock:
            if self._busy:
                return
            self._busy = True
            self._detected = False
            self._start = time.time()
            self._vid_pid = (vid_pid or '').upper()
            self._phys = phys
            _capture(+1)
        self._hub.subscribe(self._on_event)
        self._timer = threading.Timer(12.0, self.stop)
        self._timer.daemon = True
        self._timer.start()

    def stop(self):
        with self._lock:
            if not self._busy:
                return
            self._busy = False
            _capture(-1)
        self._hub.unsubscribe(self._on_event)
        if self._timer:
            self._timer.cancel()

    def _on_event(self, ev):
        if isinstance(ev, KeyEvent) and not ev.up and (not self._vid_pid or ev.matches(self._vid_pid, self._phys)):
            self._detected = True

    @property
    def result(self):
        elapsed = time.time() - self._start
        return {'detected': self._detected, 'elapsed': round(elapsed, 1), 'timeout': elapsed >= 10.0}


class KeyScanner:
    """Collects distinct key codes pressed on the chosen keyboard (manual layout scan).
    Binds are paused while scanning, so a scan the page stopped polling (window closed,
    page reloaded) ends itself after IDLE_TIMEOUT seconds."""

    IDLE_TIMEOUT = 60

    def __init__(self, hub):
        self._hub = hub
        self._lock = threading.Lock()
        self._keys = []
        self._vid_pid = ''
        self._phys = ''
        self._active = False
        self._last_poll = 0.0
        self._token = 0

    def start(self, vid_pid='', instance=''):
        phys = resolve_device(vid_pid, instance)[1] if vid_pid else ''
        with self._lock:
            self._keys = []
            self._vid_pid = (vid_pid or '').upper()
            self._phys = phys
            self._last_poll = time.time()
            self._token += 1
            token = self._token
            if not self._active:
                self._active = True
                _capture(+1)
        threading.Thread(target=self._watchdog, args=(token,), name='KeyScanWatch', daemon=True).start()
        return self._hub.subscribe(self._on_event)

    def _watchdog(self, token):
        while self._check_idle(token):
            time.sleep(5)

    def _check_idle(self, token):
        """False once this scan is over (stopped, restarted, or idle too long → stopped now)."""
        with self._lock:
            if not self._active or token != self._token:
                return False
            idle = time.time() - self._last_poll
        if idle > self.IDLE_TIMEOUT:
            self.stop()
            return False
        return True

    def stop(self):
        with self._lock:
            if self._active:
                self._active = False
                _capture(-1)
        self._hub.unsubscribe(self._on_event)
        return self.keys()

    def keys(self):
        with self._lock:
            self._last_poll = time.time()
            return list(self._keys)

    @property
    def active(self):
        return self._active

    def _on_event(self, ev):
        if not isinstance(ev, KeyEvent) or ev.up:
            return
        if self._vid_pid and not ev.matches(self._vid_pid, self._phys):
            return
        k = ev.key
        if k is None:
            return
        with self._lock:
            if k not in self._keys:
                self._keys.append(k)


_hub = RawInputHub()
_runner = ActionRunner()
_listener = KeyboardListener(_hub, _runner)
_verifier = KeyboardVerifier(_hub)
_scanner = KeyScanner(_hub)

# ── Config ────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "selected_keyboard": "",
    "selected_keyboard_name": "",
    "selected_keyboard_instance": "",
    "overlay_enabled": False,
    "app_filter": {"mode": "disabled", "apps": []},
    "theme": "dark",
    "custom_theme": {},
    "modes": [
        {"id": "mode_main", "name": "Main", "color": "#7c8aff", "icon": "zap", "binds": {}}
    ],
}
BACKUP_KEEP = 10
BACKUP_INTERVAL = 600  # seconds between automatic backups while editing

_cfg_lock = threading.Lock()
_cfg_state = {'last_text': None, 'last_backup': 0.0}


def _migrate_cfg(cfg):
    """Fix legacy configs where ESC was stored as VK 192 (tilde) instead of VK 27,
    and make sure there is at least one mode."""
    rows = cfg.get('key_rows', [])
    if rows and rows[0] and rows[0][0] == [192, 'ESC']:
        rows[0][0] = [27, 'ESC']
        for mode in cfg.get('modes', []):
            binds = mode.get('binds', {})
            if '192' in binds:
                if '27' not in binds:
                    binds['27'] = binds.pop('192')
                else:
                    binds.pop('192')
        dk = cfg.get('detected_keys', [])
        if 192 in dk and 27 not in dk:
            dk[dk.index(192)] = 27
        elif 192 in dk:
            dk.remove(192)
    if not isinstance(cfg.get('modes'), list) or not cfg['modes']:
        cfg['modes'] = copy.deepcopy(DEFAULT_CONFIG['modes'])
    return cfg


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _regular_backups():
    try:
        return sorted(f for f in os.listdir(BACKUP_DIR)
                      if f.startswith('config.') and f.endswith('.json') and 'corrupted' not in f)
    except OSError:
        return []


def _backup_current_config():
    """Copy config.json into backups/ unless the newest backup already has the same content."""
    if not os.path.exists(CONFIG):
        return
    try:
        with open(CONFIG, 'rb') as f:
            cur = f.read()
        baks = _regular_backups()
        if baks:
            with open(os.path.join(BACKUP_DIR, baks[-1]), 'rb') as f:
                if f.read() == cur:
                    return
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        with open(os.path.join(BACKUP_DIR, f'config.{ts}.json'), 'wb') as f:
            f.write(cur)
        for old in _regular_backups()[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except OSError:
                pass
    except OSError:
        log.exception('config backup failed')


def load_config():
    cfg = _read_json(CONFIG)
    if cfg is not None:
        return _migrate_cfg(cfg)
    for b in reversed(_regular_backups()):
        cfg = _read_json(os.path.join(BACKUP_DIR, b))
        if cfg is not None:
            log.warning('config.json unreadable — restored from %s', b)
            return _migrate_cfg(cfg)
    if os.path.exists(CONFIG):
        try:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            shutil.copy2(CONFIG, os.path.join(BACKUP_DIR, f'config.corrupted-{ts}.json'))
        except OSError:
            pass
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config):
    """Atomic write; no-op when nothing changed. The previous file is backed up on the first
    save of each session and then at most every BACKUP_INTERVAL seconds, so one editing session
    can no longer rotate every older backup away."""
    if not isinstance(config, dict):
        return {'ok': False, 'error': 'config must be an object'}
    text = json.dumps(config, ensure_ascii=False, indent=2)
    with _cfg_lock:
        if text == _cfg_state['last_text'] and os.path.exists(CONFIG):
            return {'ok': True, 'unchanged': True}
        now = time.time()
        if os.path.exists(CONFIG) and now - _cfg_state['last_backup'] >= BACKUP_INTERVAL:
            _backup_current_config()
            _cfg_state['last_backup'] = now
        tmp = CONFIG + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(text)
            for attempt in range(5):
                try:
                    os.replace(tmp, CONFIG)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
        except Exception as e:  # noqa: BLE001
            log.exception('config save failed')
            return {'ok': False, 'error': str(e)}
        _cfg_state['last_text'] = text
    return {'ok': True}


# ── Installed apps (picker) ───────────────────────────────────
def _steam_root():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
        p = winreg.QueryValueEx(k, 'SteamPath')[0].replace('/', '\\')
        winreg.CloseKey(k)
        if os.path.isdir(p):
            return p
    except OSError:
        pass
    for p in (r'C:\Program Files (x86)\Steam', r'C:\Program Files\Steam'):
        if os.path.isdir(p):
            return p
    return ''


def _steam_library_dirs(steam_path):
    dirs = []
    main = os.path.join(steam_path, 'steamapps')
    if os.path.isdir(main):
        dirs.append(main)
    try:
        with open(os.path.join(main, 'libraryfolders.vdf'), 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        known = {os.path.normcase(d) for d in dirs}
        for p in re.findall(r'"path"\s+"([^"]+)"', text):
            sa = os.path.join(p.replace('\\\\', '\\'), 'steamapps')
            if os.path.isdir(sa) and os.path.normcase(sa) not in known:
                dirs.append(sa)
                known.add(os.path.normcase(sa))
    except OSError:
        pass
    return dirs


_HEX40_JPG = re.compile(r'^[0-9a-f]{40}\.jpg$')


def _steam_icon(cache_dir, appid):
    """Steam's small game icon. Old clients: <id>_icon.jpg; current clients: <id>/<sha1>.jpg."""
    try:
        for name in (f'{appid}_icon.jpg', f'{appid}_icon.png'):
            p = os.path.join(cache_dir, name)
            if os.path.isfile(p):
                return p
        d = os.path.join(cache_dir, appid)
        if os.path.isdir(d):
            files = [os.path.join(d, f) for f in os.listdir(d) if _HEX40_JPG.match(f)]
            files = [f for f in files if os.path.isfile(f)]
            if files:
                return min(files, key=os.path.getsize)
    except OSError:
        pass
    return None


def _file_data_uri(path):
    try:
        with open(path, 'rb') as f:
            data = f.read()
        mime = 'image/png' if path.lower().endswith('.png') else 'image/jpeg'
        return f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')
    except OSError:
        return None


_icon_cache = {}


def _icon_for(path):
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        key = (path, 0)
    if key not in _icon_cache:
        _icon_cache[key] = _get_icon_b64(path)
    return _icon_cache[key]


def _scan_installed_apps():
    results, seen = [], set()
    dirs = [os.path.expandvars(r'%APPDATA%\Microsoft\Windows\Start Menu\Programs'),
            os.path.expandvars(r'%ProgramData%\Microsoft\Windows\Start Menu\Programs')]
    skip_words = ('uninstall', 'deinstall', 'remove', 'readme', 'license', 'help')
    lnks = []
    for base in dirs:
        for lnk in glob.glob(os.path.join(base, '**', '*.lnk'), recursive=True):
            name = os.path.splitext(os.path.basename(lnk))[0].strip()
            key = name.lower()
            if key in seen or any(s in key for s in skip_words):
                continue
            seen.add(key)
            lnks.append((name, lnk))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='icons') as pool:
        icons = list(pool.map(lambda item: _icon_for(item[1]), lnks))
    for (name, lnk), ico in zip(lnks, icons):
        results.append({'name': name, 'exe': lnk, 'ftype': 'lnk', 'icon': ico})

    steam = _steam_root()
    if steam:
        cache = os.path.join(steam, 'appcache', 'librarycache')
        seen_ids = set()
        for sa in _steam_library_dirs(steam):
            for acf in glob.glob(os.path.join(sa, 'appmanifest_*.acf')):
                try:
                    with open(acf, 'r', encoding='utf-8', errors='replace') as f:
                        txt = f.read()
                    aid = re.search(r'"appid"\s+"(\d+)"', txt)
                    aname = re.search(r'"name"\s+"([^"]+)"', txt)
                    if not (aid and aname):
                        continue
                    appid, gname = aid.group(1), aname.group(1).strip()
                    if appid in seen_ids or not gname:
                        continue
                    seen_ids.add(appid)
                    ico_path = _steam_icon(cache, appid)
                    results.append({'name': gname, 'exe': appid, 'ftype': 'steam',
                                    'icon': _file_data_uri(ico_path) if ico_path else None})
                except OSError:
                    continue

    try:
        base = (r'Software\Classes\Local Settings\Software\Microsoft'
                r'\Windows\CurrentVersion\AppModel\Repository\Packages')
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root:
            i = 0
            while True:
                try:
                    pkg = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, pkg) as pk:
                        try:
                            disp = winreg.QueryValueEx(pk, 'DisplayName')[0]
                        except OSError:
                            continue
                        if not disp or disp.startswith('ms-resource:') or disp.startswith('@{'):
                            continue
                        parts = pkg.split('_')
                        if len(parts) < 2:
                            continue
                        app_id = 'App'
                        try:
                            with winreg.OpenKey(pk, 'Applications') as ak:
                                app_id = winreg.EnumKey(ak, 0)
                        except OSError:
                            pass
                    key = disp.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({'name': disp, 'exe': f'shell:AppsFolder\\{parts[0]}_{parts[-1]}!{app_id}',
                                    'ftype': 'uwp', 'icon': None})
                except OSError:
                    continue
    except OSError:
        pass
    return sorted(results, key=lambda x: x['name'].lower())


_apps_lock = threading.Lock()
_apps_cache = {'data': None, 't': 0.0}
APPS_CACHE_TTL = 600


def list_installed_apps_cached(force=False):
    with _apps_lock:  # a concurrent caller waits for the running scan instead of starting another
        if not force and _apps_cache['data'] is not None and time.time() - _apps_cache['t'] < APPS_CACHE_TTL:
            return _apps_cache['data']
        data = _scan_installed_apps()
        _apps_cache['data'], _apps_cache['t'] = data, time.time()
        return data


# ── Windows ───────────────────────────────────────────────────
_window = None
_popup_win = None
_hwnds = {}


def _hwnd_of(window):
    """Native HWND of a pywebview window (pywebview 6 has no native_handle attribute)."""
    if window is None:
        return 0
    h = _hwnds.get(id(window))
    if h:
        return h
    try:
        from System import Action
        form = window.native
        if form is None:
            return 0
        box = {}

        def get():
            box['h'] = int(form.Handle.ToInt64())
        if form.InvokeRequired:
            form.Invoke(Action(get))
        else:
            get()
        h = box.get('h', 0)
        if h:
            _hwnds[id(window)] = h
        return h
    except Exception:
        return 0


class UiBridge:
    """Pushes listener status to the page when it changes, instead of the page polling.
    Bursts are coalesced; evaluate_js runs on this thread so input threads never block."""

    def __init__(self):
        self._ev = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def notify(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name='UiBridge', daemon=True)
                self._thread.start()
        self._ev.set()

    def _run(self):
        while True:
            self._ev.wait()
            time.sleep(0.03)
            self._ev.clear()
            st = json.dumps(_listener.status)
            for w, js in ((_window, f'window.__bbOnStatus && window.__bbOnStatus({st})'),
                          (_popup_win, f'window.updateUI && updateUI({st})')):
                if w is None:
                    continue
                try:
                    w.evaluate_js(js)
                except Exception:
                    pass


_ui = UiBridge()
_listener.on_change = _ui.notify


def _set_webview_visible(window, visible):
    """Tell WebView2 whether its page is visible. pywebview's hide() only hides the form, so a
    page sitting in the tray kept running timers and repainting (document.visibilityState
    stayed 'visible'). Setting CoreWebView2Controller.IsVisible lets Chromium throttle it."""
    try:
        from System import Action
        from System.Reflection import BindingFlags
        form = window.native
        wv = form.browser.webview
        fld = wv.GetType().GetField('_coreWebView2Controller', BindingFlags.NonPublic | BindingFlags.Instance)
        ctrl = fld.GetValue(wv) if fld is not None else None
        if ctrl is None:
            return False

        def apply():
            ctrl.IsVisible = bool(visible)
        if form.InvokeRequired:
            form.Invoke(Action(apply))
        else:
            apply()
        return True
    except Exception:
        return False


def _show_main():
    if _window is None:
        return
    try:
        _set_webview_visible(_window, True)
        _window.show()
        hwnd = _hwnd_of(_window)
        if hwnd:
            if IsIconic(hwnd):
                ShowWindow(hwnd, 9)  # SW_RESTORE (keeps a maximised window maximised otherwise)
            SetForegroundWindow(hwnd)
    except Exception:
        log.exception('show window failed')


def _hide_main():
    if _window is None:
        return
    _window.hide()
    _set_webview_visible(_window, False)
    if _scanner.active:
        # Finish a running key scan (keeping the keys found so far): binds are paused while
        # it runs. evaluate_js waits for the page, so it must not run on this (UI) thread.
        threading.Thread(target=lambda: _window.evaluate_js(
            "typeof scanInterval !== 'undefined' && scanInterval && stopScan()"), daemon=True).start()


# ── API (called from app.html) ───────────────────────────────
class API:
    def app_info(self):
        return {'version': APP_VERSION}

    def load_config(self):
        return load_config()

    def save_config(self, config):
        r = save_config(config)
        if r.get('ok'):
            _listener.set_config(config)
            _sync_popup_language(config.get('lang'))
        return r

    def save_theme(self, name, custom_colors):
        cfg = load_config()
        cfg['theme'] = name or 'dark'
        cfg['custom_theme'] = custom_colors or {}
        return self.save_config(cfg)

    # keyboards
    def detect_keyboards(self):
        try:
            return list_keyboards()
        except Exception as e:  # noqa: BLE001
            log.exception('keyboard detection failed')
            return [{'error': str(e)}]

    def start_verify(self, vid_pid='', instance=''):
        _verifier.start(vid_pid, instance)
        return {'ok': True}

    def poll_verify(self):
        return _verifier.result

    def stop_verify(self):
        _verifier.stop()
        return {'ok': True}

    # manual key scan
    def start_detection(self, vid_pid='', instance=''):
        return {'ok': bool(_scanner.start(vid_pid, instance))}

    def get_detected_keys(self):
        return _scanner.keys()

    def stop_detection(self):
        return _scanner.stop()

    # listener
    def start_listener(self, vid_pid, dev_name='', instance=''):
        _listener.configure(load_config(), vid_pid, dev_name, instance)
        return _listener.start()

    def stop_listener(self):
        _listener.stop()
        return {'ok': True}

    def update_listener_config(self, cfg):
        _listener.set_config(cfg)
        return {'ok': True}

    def listener_status(self):
        return _listener.status

    def listener_log(self):
        return _listener.log_entries()

    def get_active_window_info(self):
        exe, title = _get_foreground_info()
        return {'exe': exe, 'title': title}

    def set_transparency(self, alpha):
        """Window-level alpha (10–255) on our own window. Looking the window up by its title
        could hit any other window called 'BindBoard' — e.g. an Explorer folder."""
        try:
            alpha = max(10, min(255, int(alpha)))
            hwnd = _hwnd_of(_window)
            if not hwnd:
                return {'ok': False, 'error': 'window not found'}
            style = GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not style & WS_EX_LAYERED:
                SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
            SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
            return {'ok': True}
        except Exception as e:  # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def browse_file(self):
        dialog = getattr(getattr(webview, 'FileDialog', None), 'OPEN', 10)
        result = _window.create_file_dialog(
            dialog, allow_multiple=False,
            file_types=('All files (*.*)', 'Programs (*.exe)', 'Shortcuts (*.lnk)'))
        return result[0] if result else ''

    def list_installed_apps(self, force=False):
        return list_installed_apps_cached(bool(force))

    def list_processes(self):
        """Unique running processes [{pid, name, exe}], sorted by name."""
        results, seen = [], set()
        try:
            buf = (wt.DWORD * 8192)()
            needed = wt.DWORD(0)
            if not K32EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(needed)):
                return []
            for pid in buf[:needed.value // ctypes.sizeof(wt.DWORD)]:
                if not pid:
                    continue
                hproc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not hproc:
                    continue
                try:
                    pbuf = ctypes.create_unicode_buffer(32768)
                    sz = wt.DWORD(32768)
                    if QueryFullProcessImageNameW(hproc, 0, pbuf, ctypes.byref(sz)):
                        path = pbuf.value
                        name = path.rsplit('\\', 1)[-1]
                        if name.lower() not in seen:
                            seen.add(name.lower())
                            results.append({'pid': int(pid), 'name': name, 'exe': path})
                finally:
                    CloseHandle(hproc)
        except Exception as e:  # noqa: BLE001
            return [{'error': str(e)}]
        return sorted(results, key=lambda x: x['name'].lower())

    def test_action(self, atype, value):
        if atype == 'mode_switch':
            return {'ok': False, 'error': 'Testing this action type is not supported here'}
        ok, err = execute_action({'type': atype, 'value': value})
        return {'ok': True} if ok else {'ok': False, 'error': err}


# ── Tray popup ────────────────────────────────────────────────
POPUP_W, POPUP_H = 200, 158

POPUP_STRINGS = {
    'ru': {'on': 'Включить бинды', 'off': 'Выключить бинды', 'open': 'Открыть BindBoard', 'exit': 'Выйти'},
    'en': {'on': 'Enable binds', 'off': 'Disable binds', 'open': 'Open BindBoard', 'exit': 'Exit'},
}

POPUP_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
body{
  font-family:'Segoe UI',system-ui,sans-serif;
  background:#131315;color:#ececec;
  display:flex;flex-direction:column;
  border:1px solid #2c2c30;border-radius:10px;overflow:hidden;
  user-select:none;cursor:default;
}
.hdr{
  padding:9px 12px 8px;display:flex;align-items:center;gap:7px;
  border-bottom:1px solid #1e1e21;flex-shrink:0;
}
.logo{font-size:12px;font-weight:800;letter-spacing:.06em}
.logo b{color:#7c8aff}.logo span{color:#ececec}
.dot{
  width:6px;height:6px;border-radius:50%;margin-left:auto;flex-shrink:0;
  background:#2c2c30;transition:background .25s,box-shadow .25s;
}
.dot.on{background:#4ade80;box-shadow:0 0 7px rgba(74,222,128,.55)}
.items{padding:4px;flex:1}
.row{
  display:flex;align-items:center;gap:8px;
  padding:7px 9px;border-radius:6px;font-size:12px;
  cursor:pointer;transition:background .12s;
}
.row:hover{background:#1b1b1e}.row:active{background:#242428}
svg.ic{width:13px;height:13px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
.row.toggle,.row.toggle .ic{color:#4ade80}
.row.toggle.active,.row.toggle.active .ic{color:#f66a6a}
.row.open .ic{color:#7c8aff}.row.open{color:#ececec}
.row.exit,.row.exit .ic{color:#f66a6a}
.sep{height:1px;background:#2c2c30;margin:3px 5px}
.ver{padding:4px 12px 7px;color:#3a3a3f;font-size:9px;flex-shrink:0;letter-spacing:.02em}
</style></head>
<body>
<div class="hdr">
  <div class="logo"><b>BIND</b><span>BOARD</span></div>
  <div id="dot" class="dot"></div>
</div>
<div class="items">
  <div class="row toggle" id="row-toggle" onclick="doToggle()">
    <svg class="ic" viewBox="0 0 24 24" id="svg-toggle">
      <polygon points="5 3 19 12 5 21 5 3"/>
    </svg>
    <span id="txt-toggle"></span>
  </div>
  <div class="row open" onclick="doOpen()">
    <svg class="ic" viewBox="0 0 24 24">
      <rect x="3" y="3" width="7" height="7" rx="1"/>
      <rect x="14" y="3" width="7" height="7" rx="1"/>
      <rect x="14" y="14" width="7" height="7" rx="1"/>
      <rect x="3" y="14" width="7" height="7" rx="1"/>
    </svg>
    <span id="txt-open"></span>
  </div>
  <div class="sep"></div>
  <div class="row exit" onclick="doExit()">
    <svg class="ic" viewBox="0 0 24 24">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
      <polyline points="16 17 21 12 16 7"/>
      <line x1="21" y1="12" x2="9" y2="12"/>
    </svg>
    <span id="txt-exit"></span>
  </div>
</div>
<div class="ver">v__VERSION__</div>
<script>
var S = __STRINGS__;
var _running = false;
function updateUI(st){
  _running = !!(st && typeof st === 'object' ? st.running : st);
  document.getElementById('dot').className='dot'+(_running?' on':'');
  document.getElementById('txt-toggle').textContent=_running?S.off:S.on;
  document.getElementById('row-toggle').className='row toggle'+(_running?' active':'');
  document.getElementById('svg-toggle').innerHTML=_running
    ?'<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>'
    :'<polygon points="5 3 19 12 5 21 5 3"/>';
}
function setStrings(s){
  S = s;
  document.getElementById('txt-open').textContent=S.open;
  document.getElementById('txt-exit').textContent=S.exit;
  updateUI(_running);
}
setStrings(S);
async function doToggle(){if(window.pywebview)await pywebview.api.toggle();}
function doOpen(){if(window.pywebview)pywebview.api.open_main();}
function doExit(){if(window.pywebview)pywebview.api.quit();}
window.addEventListener('pywebviewready',async function(){
  try{var r=await pywebview.api.status();updateUI(r);}catch(e){}
});
window.addEventListener('blur',function(){
  setTimeout(function(){if(!document.hasFocus()&&window.pywebview)pywebview.api.close();},200);
});
</script>
</body></html>"""


def _ui_lang():
    return _resolve_lang(load_config().get('lang'))


_popup_lang = [None]


def _sync_popup_language(lang):
    """Re-label the tray popup when the UI language changes (it is built once at startup)."""
    lang = _resolve_lang(lang)
    if _popup_win is None or lang == _popup_lang[0]:
        return
    _popup_lang[0] = lang
    js = 'window.setStrings && setStrings(%s)' % json.dumps(POPUP_STRINGS[lang], ensure_ascii=False)
    threading.Thread(target=lambda: _popup_win.evaluate_js(js), daemon=True).start()


def _popup_html():
    _popup_lang[0] = _ui_lang()
    return (POPUP_HTML.replace('__VERSION__', APP_VERSION)
            .replace('__STRINGS__', json.dumps(POPUP_STRINGS[_popup_lang[0]], ensure_ascii=False)))


def _quit_app():
    """Let the page save anything still pending, then exit (WebView2 children included)."""
    try:
        if _window is not None:
            done = threading.Event()
            _window.evaluate_js('Promise.resolve(window.__bbFlush ? window.__bbFlush() : true)',
                                callback=lambda *_: done.set())
            done.wait(1.5)
    except Exception:
        pass
    try:
        _listener.stop()
    except Exception:
        pass
    try:
        if _tray_icon is not None:
            _tray_icon.stop()
    except Exception:
        pass
    os._exit(0)


class PopupAPI:
    def close(self):
        try:
            _popup_win.hide()
        except Exception:
            pass

    def toggle(self):
        try:
            if _listener._running:
                _listener.stop()
            else:
                cfg = load_config()
                vid_pid = _listener._vid_pid or cfg.get('selected_keyboard', '')
                dev_name = _listener._dev_name or cfg.get('selected_keyboard_name', '')
                instance = _listener._instance if _listener._vid_pid else cfg.get('selected_keyboard_instance', '')
                if vid_pid:
                    _listener.configure(cfg, vid_pid, dev_name, instance)
                    _listener.start()
        except Exception:
            log.exception('tray toggle failed')
        self.close()

    def open_main(self):
        _show_main()
        self.close()

    def quit(self):
        _quit_app()

    def status(self):
        return _listener.status


_tray_icon = None
_tray_failed = False


def _show_tray_popup():
    """Show the popup next to the cursor (i.e. the tray icon), inside the monitor's work area."""
    if _popup_win is None:
        return
    try:
        hwnd = _hwnd_of(_popup_win)
        if hwnd:
            pt = wt.POINT()
            GetCursorPos(ctypes.byref(pt))
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            GetMonitorInfoW(MonitorFromPoint(pt, 2), ctypes.byref(mi))  # MONITOR_DEFAULTTONEAREST
            rc = wt.RECT()
            GetWindowRect(hwnd, ctypes.byref(rc))
            w, h = (rc.right - rc.left) or POPUP_W, (rc.bottom - rc.top) or POPUP_H
            work = mi.rcWork
            x = min(max(pt.x - w // 2, work.left + 8), work.right - w - 8)
            y = min(max(pt.y - h - 8, work.top + 8), work.bottom - h - 8)
            SetWindowPos(hwnd, None, x, y, 0, 0, 0x0001 | 0x0004)  # NOSIZE|NOZORDER
    except Exception:
        log.exception('popup positioning failed')
    _popup_win.show()
    _ui.notify()


def _setup_popup_style():
    """No taskbar button + Win11 rounded corners for the popup."""
    try:
        hwnd = _hwnd_of(_popup_win)
        if not hwnd:
            return
        style = GetWindowLongW(hwnd, GWL_EXSTYLE)
        SetWindowLongW(hwnd, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        dwm = ctypes.WinDLL('dwmapi')
        dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, wt.LPVOID, wt.DWORD]
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def _start_tray():
    global _tray_icon, _tray_failed
    try:
        from PIL import Image
        import pystray
        import pystray._win32 as _pyw32

        # Right-click shows our own popup instead of the native context menu.
        _orig_on_notify = _pyw32.Icon._on_notify

        def _patched_on_notify(self_icon, wparam, lparam):
            if lparam == 0x0205:  # WM_RBUTTONUP
                threading.Thread(target=_show_tray_popup, daemon=True).start()
                return
            return _orig_on_notify(self_icon, wparam, lparam)
        _pyw32.Icon._on_notify = _patched_on_notify

        ico_path = os.path.join(RES, 'bindboard.ico')
        if not os.path.exists(ico_path):
            ico_path = os.path.join(BASE, 'bindboard.ico')
        img = Image.open(ico_path).resize((64, 64))
        menu = pystray.Menu(
            pystray.MenuItem('Open BindBoard', lambda icon, item: _show_tray_popup(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Exit', lambda icon, item: _quit_app()),
        )
        _tray_icon = pystray.Icon('BindBoard', img, 'BindBoard', menu)
        _tray_icon.run()
    except Exception:
        log.exception('tray icon failed')
        _tray_failed = True


def _on_closing():
    if _tray_failed:
        _listener.stop()
        return True    # no working tray to minimise to — let the close proceed
    _hide_main()
    return False       # cancel the close, keep running in the tray


def _apply_saved_transparency():
    try:
        alpha = int((load_config().get('transparency') or {}).get('window', 255))
        if alpha < 255:
            API().set_transparency(alpha)
    except Exception:
        pass


# ── Single instance ───────────────────────────────────────────
_mutex = None


def _acquire_single_instance():
    """True if no other BindBoard uses this config dir. A second copy would start a second
    listener, and every bound key would fire twice."""
    global _mutex
    _mutex = CreateMutexW(None, False, 'Local\\BindBoard.' + _instance_key())
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _activate_existing_instance():
    hwnd = FindWindowW(_overlay_class_name(), None)
    if hwnd:
        PostMessageW(hwnd, WM_BB_ACTIVATE, 0, 0)


def _migrate_old_config():
    old = os.path.join(BASE, 'config.json')
    if os.path.exists(old) and not os.path.exists(CONFIG):
        try:
            shutil.copy2(old, CONFIG)
        except OSError:
            pass


def _warm_caches():
    try:
        list_installed_apps_cached()
    except Exception:
        log.exception('app list warm-up failed')


SELFTEST_JS = r"""(async () => {
  const r = {bridge: !!(window.pywebview && window.pywebview.api)};
  r.version = (await pywebview.api.app_info()).version;
  const cfg = await pywebview.api.load_config();
  r.modes = (cfg.modes || []).length;
  r.save = (await pywebview.api.save_config(cfg)).ok;
  r.keyboards = (await pywebview.api.detect_keyboards()).length;
  r.status = await pywebview.api.listener_status();
  const apps = await pywebview.api.list_installed_apps();
  r.apps = apps.length;
  r.app_icons = apps.filter(a => a.icon).length;
  r.rendered = document.querySelectorAll('.nav-item').length;
  r.errors = window.__bbErrors || [];
  return r;
})()"""


def _run_selftest():
    """--selftest: load the UI hidden, exercise the JS↔Python bridge, write selftest.json, exit.
    Exit code 0 = everything worked. Handy for checking a fresh PyInstaller build."""
    out = {'ok': False}
    try:
        if not _window.events.loaded.wait(30):
            raise TimeoutError('page did not load')
        time.sleep(1.0)  # let init() finish
        done = threading.Event()
        _window.evaluate_js(SELFTEST_JS, callback=lambda r: (out.update(result=r), done.set()))
        if not done.wait(20):
            raise TimeoutError('no answer from page')
        r = out.get('result') or {}
        out['ok'] = bool(r.get('bridge') and r.get('version') == APP_VERSION and r.get('save')
                         and r.get('rendered') and not r.get('errors'))
    except Exception as e:  # noqa: BLE001
        out['error'] = repr(e)
    out['frozen'] = bool(getattr(sys, 'frozen', False))
    with open(os.path.join(WORK, 'selftest.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os._exit(0 if out['ok'] else 1)


def main():
    global _window, _popup_win
    selftest = '--selftest' in sys.argv
    start_hidden = selftest or '--minimized' in sys.argv or '--tray' in sys.argv
    if not _acquire_single_instance():
        if selftest:
            os._exit(2)  # another instance owns this config dir
        _activate_existing_instance()
        return
    _migrate_old_config()
    _start_overlay()
    _on_activate_request.append(_show_main)

    html = os.path.join(RES, 'app.html').replace('\\', '/')
    _window = webview.create_window(
        'BindBoard', url=f'file:///{html}', width=1200, height=820,
        min_size=(900, 640), js_api=API(), text_select=False, hidden=start_hidden)
    _popup_win = webview.create_window(
        '', html=_popup_html(), width=POPUP_W, height=POPUP_H,
        x=GetSystemMetrics(0) - POPUP_W - 12, y=GetSystemMetrics(1) - POPUP_H - 54,
        frameless=True, hidden=True, on_top=True, js_api=PopupAPI(),
        min_size=(POPUP_W, POPUP_H), text_select=False, background_color='#131315')
    _popup_win.events.loaded += _setup_popup_style
    _window.events.closing += _on_closing
    _window.events.loaded += _apply_saved_transparency
    _window.events.minimized += lambda: _set_webview_visible(_window, False)
    _window.events.restored += lambda: _set_webview_visible(_window, True)
    if start_hidden:  # --minimized: start in the tray; the page stays throttled until shown
        _window.events.loaded += lambda: _set_webview_visible(_window, False)
    if selftest:
        threading.Thread(target=_run_selftest, name='SelfTest', daemon=True).start()

    threading.Thread(target=_start_tray, name='Tray', daemon=True).start()
    # Build the "Apps" picker list in the background so its first opening is instant.
    warm = threading.Timer(5.0, _warm_caches)
    warm.daemon = True
    warm.start()

    webview.start(debug=False, gui='edgechromium', private_mode=False,
                  storage_path=os.path.join(WORK, 'webview-data'))


if __name__ == '__main__':
    main()
