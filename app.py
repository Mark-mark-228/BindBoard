import webview
import json
import os
import sys
import re
import subprocess
import threading
import time
import ctypes
import ctypes.wintypes as wt
import shutil
import winreg
from datetime import datetime

# pywebview's WinForms backend calls SetProcessDPIAware() itself, but only once
# webview.start() actually creates its window — by then our own overlay window
# (created on a daemon thread at import time, see _overlay_worker below) may
# already have created a window first. DPI awareness is locked in by whichever
# window is created first, so calling it here, before any window in the process
# exists, makes the outcome deterministic instead of a startup race.
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

# ── Paths ────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

# Bug #11: use proper per-user location (WORK was created but not used before)
WORK = os.path.join(os.path.expanduser('~'), 'BindBoard')
os.makedirs(WORK, exist_ok=True)
BACKUP_DIR = os.path.join(WORK, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)
CONFIG = os.path.join(WORK, 'config.json')

# Migrate old config (sitting next to exe) on first run
_old_cfg = os.path.join(BASE, 'config.json')
if os.path.exists(_old_cfg) and not os.path.exists(CONFIG):
    try:
        shutil.copy2(_old_cfg, CONFIG)
    except Exception:
        pass

# ── WinAPI ───────────────────────────────────────────────────
user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32    = ctypes.windll.gdi32

gdi32.CreateSolidBrush.restype    = ctypes.c_void_p
gdi32.CreateFontW.restype         = ctypes.c_void_p
gdi32.SelectObject.restype        = ctypes.c_void_p
gdi32.CreateCompatibleDC.restype  = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.GetDIBits.restype           = ctypes.c_int
user32.BeginPaint.restype         = ctypes.c_void_p
user32.GetDC.restype              = ctypes.c_void_p
user32.DrawIconEx.restype         = ctypes.c_bool
user32.DestroyIcon.restype        = ctypes.c_bool

WH_KEYBOARD_LL  = 13
WM_KEYDOWN      = 0x0100
WM_KEYUP        = 0x0101
WM_INPUT        = 0x00FF
WM_DESTROY      = 0x0002
WM_QUIT         = 0x0012
HC_ACTION       = 0
PM_REMOVE       = 0x0001
RIDEV_INPUTSINK = 0x00000100
RID_INPUT       = 0x10000003
RI_KEY_BREAK    = 0x01
RI_KEY_E0       = 0x02          # extended-key flag in RAWKEYBOARD.Flags
WS_POPUP             = 0x80000000
WS_EX_LAYERED        = 0x00080000
WS_EX_TOPMOST        = 0x00000008
WS_EX_TOOLWINDOW     = 0x00000080
WS_EX_NOACTIVATE     = 0x08000000
LWA_ALPHA            = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DT_SINGLELINE        = 0x0020
DT_VCENTER           = 0x0004
DT_CENTER            = 0x0001
BT_TRANSPARENT       = 1

# Bug #1: hook flags for suppression
LLKHF_INJECTED  = 0x10
LLKHF_EXTENDED  = 0x01
LLKHF_UP        = 0x80

RIDI_DEVICENAME  = 0x20000007
RIDI_DEVICEINFO  = 0x2000000B
RIM_TYPEKEYBOARD = 1

HOOKPROC  = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wt.WPARAM, wt.LPARAM)
WNDPROC_T = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

user32.DefWindowProcW.restype  = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.PostThreadMessageW.restype = wt.BOOL
user32.CallNextHookEx.restype  = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t,
]

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wt.DWORD), ('scanCode', wt.DWORD),
                ('flags',  wt.DWORD), ('time',     wt.DWORD),
                ('dwExtraInfo', ctypes.c_ulonglong)]

class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [('hDevice', wt.HANDLE), ('dwType', wt.DWORD)]

class _KBD_INFO(ctypes.Structure):
    _fields_ = [('dwType', wt.DWORD), ('dwSubType', wt.DWORD),
                ('dwKeyboardMode', wt.DWORD), ('dwNumberOfFunctionKeys', wt.DWORD),
                ('dwNumberOfIndicators', wt.DWORD), ('dwNumberOfKeysTotal', wt.DWORD)]

class _RID_UNION(ctypes.Union):
    _fields_ = [('keyboard', _KBD_INFO)]

class RID_DEVICE_INFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('dwType', wt.DWORD), ('u', _RID_UNION)]

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wt.UINT), ('style', wt.UINT), ('lpfnWndProc', WNDPROC_T),
        ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
        ('hInstance', wt.HANDLE), ('hIcon', wt.HANDLE), ('hCursor', wt.HANDLE),
        ('hbrBackground', wt.HANDLE), ('lpszMenuName', wt.LPCWSTR),
        ('lpszClassName', wt.LPCWSTR), ('hIconSm', wt.HANDLE),
    ]

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ('usUsagePage', wt.USHORT), ('usUsage', wt.USHORT),
        ('dwFlags', wt.DWORD), ('hwndTarget', wt.HWND),
    ]

class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ('dwType', wt.DWORD), ('dwSize', wt.DWORD),
        ('hDevice', wt.HANDLE), ('wParam', wt.WPARAM),
    ]

class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ('MakeCode', wt.USHORT), ('Flags', wt.USHORT), ('Reserved', wt.USHORT),
        ('VKey', wt.USHORT), ('Message', wt.UINT), ('ExtraInformation', wt.ULONG),
    ]

class _RI_DATA(ctypes.Union):
    _fields_ = [('keyboard', RAWKEYBOARD)]

class RAWINPUT(ctypes.Structure):
    _fields_ = [('header', RAWINPUTHEADER), ('data', _RI_DATA)]

class RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

class SHFILEINFOW(ctypes.Structure):
    _fields_ = [('hIcon', ctypes.c_void_p), ('iIcon', ctypes.c_int),
                ('dwAttributes', wt.DWORD),
                ('szDisplayName', ctypes.c_wchar * 260),
                ('szTypeName',    ctypes.c_wchar * 80)]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', ctypes.c_long),
                ('biHeight', ctypes.c_long), ('biPlanes', ctypes.c_short),
                ('biBitCount', ctypes.c_short), ('biCompression', wt.DWORD),
                ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]

def _get_icon_b64(path, sz=20):
    try:
        import base64, io
        from PIL import Image
        shell32 = ctypes.windll.shell32
        shfi = SHFILEINFOW()
        if not shell32.SHGetFileInfoW(path, 0, ctypes.byref(shfi),
                                       ctypes.sizeof(shfi), 0x100 | 0x1):
            return None
        hIcon = shfi.hIcon
        if not hIcon:
            return None
        hdc    = user32.GetDC(None)
        hdcMem = gdi32.CreateCompatibleDC(hdc)
        hbm    = gdi32.CreateCompatibleBitmap(hdc, sz, sz)
        old_bmp = gdi32.SelectObject(hdcMem, hbm)
        br = gdi32.CreateSolidBrush(0x00202020)
        rc = RECT(0, 0, sz, sz)
        user32.FillRect(hdcMem, ctypes.byref(rc), br)
        gdi32.DeleteObject(br)
        user32.DrawIconEx(hdcMem, 0, 0, hIcon, sz, sz, 0, None, 3)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = sz; bmi.biHeight = -sz
        bmi.biPlanes = 1; bmi.biBitCount = 32; bmi.biCompression = 0
        buf = (ctypes.c_ubyte * (sz * sz * 4))()
        gdi32.GetDIBits(hdcMem, hbm, 0, sz, buf, ctypes.byref(bmi), 0)
        img = Image.frombuffer('RGBA', (sz, sz), bytes(buf), 'raw', 'BGRA', 0, 1)
        bio = io.BytesIO()
        img.save(bio, 'PNG')
        b64 = base64.b64encode(bio.getvalue()).decode()
        gdi32.SelectObject(hdcMem, old_bmp)
        gdi32.DeleteObject(hbm); gdi32.DeleteDC(hdcMem)
        user32.ReleaseDC(None, hdc); user32.DestroyIcon(hIcon)
        return f'data:image/png;base64,{b64}'
    except Exception:
        return None

# ── App filter ────────────────────────────────────────────────
def _get_foreground_exe():
    """Return lowercase exe name (no path) of the foreground window's process."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ''
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ''
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return ''
        buf = ctypes.create_unicode_buffer(260)
        sz  = wt.DWORD(260)
        kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz))
        kernel32.CloseHandle(hproc)
        return buf.value.split('\\')[-1].lower()
    except Exception:
        return ''

# ── Mode switch overlay — persistent window, PostMessageW direct call ────────
# NOTE: Win32 COLORREF is 0x00BBGGRR (blue in the high byte), not 0x00RRGGBB.
# The accent default below is the BGR encoding of the app's #7c8aff indigo accent —
# using the RGB hex directly here would render as orange instead.
_ov_hwnd  = [None]
_ov_data  = {'name': 'Mode', 'acc': 0xFF8A7C}
WM_OV_UPDATE = 0x8001  # private app message: update text + show

def _overlay_worker():
    # SetProcessDPIAware() (see top of file) makes GetSystemMetrics/CreateWindow
    # work in real physical pixels — Windows no longer bitmap-stretches windows
    # for us. Without scaling our own fixed sizes by the monitor's DPI, the
    # overlay would render tiny/undersized next to the rest of the (properly
    # DPI-scaled, via pywebview) UI on any display that isn't at 100% scaling.
    hdc0 = user32.GetDC(None)
    dpi  = gdi32.GetDeviceCaps(hdc0, 88) if hdc0 else 96  # LOGPIXELSX
    if hdc0:
        user32.ReleaseDC(None, hdc0)
    scale = (dpi or 96) / 96.0

    W, H     = round(320 * scale), round(56 * scale)
    BG_COL   = 0x151313  # BGR encoding of --s1 #131315
    TEXT_COL = 0xECECEC
    hmod     = kernel32.GetModuleHandleW(None)
    cls_name = 'BBOverlayPersist'
    WM_PAINT = 0x000F; WM_TIMER = 0x0113
    DT_END_ELLIPSIS = 0x8000

    def wnd_proc(hwnd, msg, wp, lp):
        if msg == WM_PAINT:
            ps_buf = ctypes.create_string_buffer(100)
            hdc = user32.BeginPaint(hwnd, ps_buf)
            if hdc:
                hbr = gdi32.CreateSolidBrush(BG_COL)
                user32.FillRect(hdc, ctypes.byref(RECT(0,0,W,H)), hbr)
                gdi32.DeleteObject(hbr)
                hbr = gdi32.CreateSolidBrush(_ov_data['acc'])
                user32.FillRect(hdc, ctypes.byref(RECT(0,0,round(4*scale),H)), hbr)
                gdi32.DeleteObject(hbr)
                hfont = gdi32.CreateFontW(-round(15*scale),0,0,0,700,0,0,0,1,0,0,0,0,'Segoe UI')
                old_font = gdi32.SelectObject(hdc, hfont)
                gdi32.SetBkMode(hdc, BT_TRANSPARENT)
                gdi32.SetTextColor(hdc, TEXT_COL)
                user32.DrawTextW(hdc, f'Mode: {_ov_data["name"]}', -1,
                                 ctypes.byref(RECT(round(16*scale),0,W-round(8*scale),H)),
                                 DT_SINGLELINE|DT_VCENTER|DT_END_ELLIPSIS)
                gdi32.SelectObject(hdc, old_font)
                gdi32.DeleteObject(hfont)
                user32.EndPaint(hwnd, ps_buf)
            return 0
        elif msg == WM_OV_UPDATE:
            user32.KillTimer(hwnd, 1)
            sw = user32.GetSystemMetrics(0); sh = user32.GetSystemMetrics(1)
            # SWP_SHOWWINDOW|SWP_NOACTIVATE = 0x0040|0x0010
            user32.SetWindowPos(hwnd, None, (sw-W)//2, int(sh*0.83), W, H, 0x0050)
            user32.InvalidateRect(hwnd, None, True)
            user32.UpdateWindow(hwnd)
            user32.SetTimer(hwnd, 1, 1200, None)
            return 0
        elif msg == WM_TIMER:
            user32.KillTimer(hwnd, 1)
            user32.ShowWindow(hwnd, 0)
            return 0
        elif msg == 0x0002:
            user32.PostQuitMessage(0); return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    cb_ref = WNDPROC_T(wnd_proc)
    user32.UnregisterClassW(cls_name, hmod)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = cb_ref; wc.hInstance = hmod; wc.lpszClassName = cls_name
    user32.RegisterClassExW(ctypes.byref(wc))
    ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    hwnd = user32.CreateWindowExW(ex, cls_name, '', WS_POPUP, 0, 0, W, H, None, None, hmod, None)
    if not hwnd:
        user32.UnregisterClassW(cls_name, hmod); return
    _ov_hwnd[0] = hwnd
    user32.SetLayeredWindowAttributes(hwnd, 0, 215, LWA_ALPHA)
    msg_s = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg_s), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg_s))
        user32.DispatchMessageW(ctypes.byref(msg_s))
    _ov_hwnd[0] = None
    user32.UnregisterClassW(cls_name, hmod)

threading.Thread(target=_overlay_worker, daemon=True).start()

def _show_mode_overlay(mode_name, color_hex='#7c8aff'):
    if not _ov_hwnd[0]:
        return
    try:
        ch = color_hex.lstrip('#')
        cr,cg,cb_v = int(ch[0:2],16), int(ch[2:4],16), int(ch[4:6],16)
        _ov_data['acc'] = cr | (cg << 8) | (cb_v << 16)
    except Exception:
        _ov_data['acc'] = 0xFF8A7C
    _ov_data['name'] = mode_name
    user32.PostMessageW(_ov_hwnd[0], WM_OV_UPDATE, 0, 0)

# ── Keyboard size estimation ──────────────────────────────────
def _estimate_size(key_count):
    if key_count <= 0 or key_count > 200:
        return '?'
    if key_count <= 48:   return '40%'
    elif key_count <= 64: return '60%'
    elif key_count <= 72: return '65%'
    elif key_count <= 85: return '75%'
    elif key_count <= 95: return 'TKL'
    else:                 return '100%'

def _get_raw_kb_data():
    num = wt.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(num), ctypes.sizeof(RAWINPUTDEVICELIST))
    if not num.value:
        return {}
    devs = (RAWINPUTDEVICELIST * num.value)()
    user32.GetRawInputDeviceList(devs, ctypes.byref(num), ctypes.sizeof(RAWINPUTDEVICELIST))
    counts = {}
    for d in devs:
        if d.dwType != RIM_TYPEKEYBOARD:
            continue
        nlen = wt.UINT(0)
        user32.GetRawInputDeviceInfoW(d.hDevice, RIDI_DEVICENAME, None, ctypes.byref(nlen))
        nbuf = ctypes.create_unicode_buffer(nlen.value)
        user32.GetRawInputDeviceInfoW(d.hDevice, RIDI_DEVICENAME, nbuf, ctypes.byref(nlen))
        name = nbuf.value.upper()
        info = RID_DEVICE_INFO()
        info.cbSize = ctypes.sizeof(RID_DEVICE_INFO)
        isz = wt.UINT(ctypes.sizeof(RID_DEVICE_INFO))
        user32.GetRawInputDeviceInfoW(d.hDevice, RIDI_DEVICEINFO, ctypes.byref(info), ctypes.byref(isz))
        kc = info.u.keyboard.dwNumberOfKeysTotal
        m = re.search(r'(VID_[0-9A-F]+&PID_[0-9A-F]+)', name)
        if m:
            counts.setdefault(m.group(1), []).append(kc)
    out = {}
    for vp, kclist in counts.items():
        real = [k for k in kclist if 0 < k < 264]
        kc = min(real) if real else (min(kclist) if kclist else 0)
        out[vp] = {'key_count': kc, 'real_kb': bool(real)}
    return out

def _get_vid_pid_handles():
    num = wt.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(num), ctypes.sizeof(RAWINPUTDEVICELIST))
    if not num.value:
        return {}
    devs = (RAWINPUTDEVICELIST * num.value)()
    user32.GetRawInputDeviceList(devs, ctypes.byref(num), ctypes.sizeof(RAWINPUTDEVICELIST))
    out = {}
    for d in devs:
        if d.dwType != RIM_TYPEKEYBOARD:
            continue
        nlen = wt.UINT(0)
        user32.GetRawInputDeviceInfoW(d.hDevice, RIDI_DEVICENAME, None, ctypes.byref(nlen))
        nbuf = ctypes.create_unicode_buffer(nlen.value)
        user32.GetRawInputDeviceInfoW(d.hDevice, RIDI_DEVICENAME, nbuf, ctypes.byref(nlen))
        m = re.search(r'(VID_[0-9A-F]+&PID_[0-9A-F]+)', nbuf.value.upper())
        if m:
            out.setdefault(m.group(1), set()).add(d.hDevice)
    return out

# ── Keyboard Listener ─────────────────────────────────────────
class KeyboardListener:
    def __init__(self):
        self._ri_thread     = None
        self._hook_thread   = None
        self._running       = False
        self._vid_pid       = ''
        self._dev_name      = ''
        self._config        = {}
        self._mode          = ''
        self._handles       = set()
        self._wndproc_cb    = None
        self._ri_hwnd       = None
        # Bug #1: suppression dict
        self._suppress      = {}          # (scan, ext, up) → monotonic timestamp
        self._suppress_lock = threading.Lock()
        self._hook_handle   = None
        self._hook_cb       = None

    def configure(self, config, vid_pid, dev_name=''):
        self._config   = config
        self._vid_pid  = vid_pid.upper()
        self._dev_name = dev_name
        if config.get('modes'):
            self._mode = config['modes'][0]['id']
        handles_map   = _get_vid_pid_handles()
        self._handles = handles_map.get(self._vid_pid, set())

    def start(self):
        # Bug #2: refuse to start if device handle is unknown
        if not self._handles:
            return {'ok': False, 'error': 'device_not_found'}
        if self._running:
            self.stop()
        self._running     = True
        self._ri_thread   = threading.Thread(target=self._run_ri,   daemon=True)
        self._hook_thread = threading.Thread(target=self._run_hook, daemon=True)
        self._ri_thread.start()
        self._hook_thread.start()
        return {'ok': True}

    def stop(self):
        # Bug #7: use join() instead of sleep()
        self._running = False
        if self._ri_hwnd:
            user32.PostMessageW(self._ri_hwnd, WM_DESTROY, 0, 0)
        if self._ri_thread:
            self._ri_thread.join(timeout=2)
            self._ri_thread = None
        if self._hook_thread:
            self._hook_thread.join(timeout=2)
            self._hook_thread = None

    @property
    def status(self):
        ri_alive = self._ri_thread is not None and self._ri_thread.is_alive()
        return {
            'running': self._running and ri_alive,
            'mode':    self._mode,
            'device':  self._dev_name or self._vid_pid,
            'vid_pid': self._vid_pid,
        }

    def _run_hook(self):
        """Bug #1: WH_KEYBOARD_LL hook that suppresses keys from the target device.
        Correlation window: a key that arrived via WM_INPUT (recorded in _suppress)
        within the last 25 ms is suppressed. Injected events always pass through."""
        WINDOW = 0.025

        def hook_proc(nCode, wParam, lParam):
            if nCode == HC_ACTION:
                ks = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not (ks.flags & LLKHF_INJECTED):
                    key_tup = (ks.scanCode,
                               bool(ks.flags & LLKHF_EXTENDED),
                               bool(ks.flags & LLKHF_UP))
                    now = time.monotonic()
                    with self._suppress_lock:
                        ts = self._suppress.get(key_tup)
                        if ts is not None and (now - ts) <= WINDOW:
                            del self._suppress[key_tup]
                            return 1  # suppress — do NOT call CallNextHookEx
            return user32.CallNextHookEx(self._hook_handle, nCode, wParam, lParam)

        self._hook_cb     = HOOKPROC(hook_proc)
        self._hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_cb, None, 0)

        msg = wt.MSG()
        while self._running:
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.001)

        if self._hook_handle:
            user32.UnhookWindowsHookEx(self._hook_handle)
            self._hook_handle = None
        self._hook_cb = None

    def _run_ri(self):
        cls_name = 'BBListener'
        hmod = kernel32.GetModuleHandleW(None)
        _pressed = set()

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                sz = wt.UINT(0)
                user32.GetRawInputData(lparam, RID_INPUT, None,
                                       ctypes.byref(sz), ctypes.sizeof(RAWINPUTHEADER))
                if sz.value > 0:
                    buf = ctypes.create_string_buffer(sz.value)
                    user32.GetRawInputData(lparam, RID_INPUT, buf,
                                           ctypes.byref(sz), ctypes.sizeof(RAWINPUTHEADER))
                    ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
                    if ri.header.dwType == RIM_TYPEKEYBOARD:
                        vk       = ri.data.keyboard.VKey
                        scan     = ri.data.keyboard.MakeCode
                        is_ext   = bool(ri.data.keyboard.Flags & RI_KEY_E0)
                        is_break = bool(ri.data.keyboard.Flags & RI_KEY_BREAK)
                        # Bug #2: no empty-handles fallback
                        from_dev = (ri.header.hDevice in self._handles)
                        if not from_dev and not is_break:
                            # Log unknown device key events for diagnostics
                            entry = {'vk': vk, 'mode': self._mode,
                                     'bind': None, 'ok': None, 'error': 'wrong_device'}
                            KeyboardListener._dbg_log = (
                                KeyboardListener._dbg_log + [entry])[-20:]
                        if from_dev:
                            # Bug #1: record for suppression hook
                            key_tup = (scan, is_ext, is_break)
                            now_m = time.monotonic()
                            with self._suppress_lock:
                                self._suppress[key_tup] = now_m
                                stale = [k for k, t in self._suppress.items()
                                         if now_m - t > 0.1]
                                for k in stale:
                                    del self._suppress[k]
                            if is_break:
                                _pressed.discard(vk)
                            elif vk not in _pressed:
                                _pressed.add(vk)
                                self._on_key(vk)
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_cb = WNDPROC_T(wnd_proc)
        user32.UnregisterClassW(cls_name, hmod)

        wc = WNDCLASSEXW()
        wc.cbSize        = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc   = self._wndproc_cb
        wc.hInstance     = hmod
        wc.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0, cls_name, cls_name, WS_POPUP,
            0, 0, 0, 0, None, None, hmod, None)
        self._ri_hwnd = hwnd

        if hwnd:
            rid = RAWINPUTDEVICE()
            rid.usUsagePage = 0x01
            rid.usUsage     = 0x06
            rid.dwFlags     = RIDEV_INPUTSINK
            rid.hwndTarget  = hwnd
            user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                           ctypes.sizeof(RAWINPUTDEVICE))

        msg = wt.MSG()
        while self._running:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._ri_hwnd == hwnd:
            self._ri_hwnd = None
        user32.UnregisterClassW(cls_name, hmod)

    def _exec(self, bind):
        btype = bind.get('type', '')
        v     = bind.get('value', '')

        # App filter: check foreground exe before executing
        af = self._config.get('app_filter', {})
        af_mode = af.get('mode', 'disabled')
        if af_mode in ('allow', 'deny'):
            fg = _get_foreground_exe()
            apps = [a.lower() for a in af.get('apps', [])]
            in_list = fg in apps
            if af_mode == 'allow' and not in_list:
                return True, None   # not in allow-list → skip
            if af_mode == 'deny'  and in_list:
                return True, None   # in deny-list → skip

        SP = r'C:\Program Files (x86)\Steam\steamapps\common\Soundpad\Soundpad.exe'
        try:
            if btype in ('url', 'lnk', 'exe'):
                os.startfile(v.strip('"\''))
            elif btype == 'steam':
                os.startfile(f'steam://rungameid/{v}')
            elif btype == 'sound':
                subprocess.Popen([SP, '-rc', f'DoPlaySound({v})'],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            elif btype == 'stop_sound':
                subprocess.Popen([SP, '-rc', 'DoStopSound()'],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            elif btype == 'mode_switch':
                target = bind.get('target', '')
                ids = [m['id'] for m in self._config.get('modes', [])]
                if target in ids:
                    self._mode = target
                    if self._config.get('overlay_enabled', False):
                        mode_obj = next(
                            (m for m in self._config.get('modes', []) if m['id'] == target),
                            None)
                        if mode_obj:
                            _show_mode_overlay(mode_obj['name'],
                                               mode_obj.get('color', '#7c8aff'))
            return True, None
        except Exception as e:
            return False, str(e)

    _dbg_log = []

    def _on_key(self, vk):
        mode_cfg = next((m for m in self._config.get('modes', [])
                         if m['id'] == self._mode), None)
        bind = mode_cfg.get('binds', {}).get(str(vk)) if mode_cfg else None
        mode_at_press = self._mode  # capture BEFORE exec so log shows what triggered it
        ok, err = None, None
        if bind:
            ok, err = self._exec(bind)
        entry = {
            'vk':    vk,
            'mode':  mode_at_press,
            'bind':  bind.get('type') if bind else None,
            'ok':    ok,
            'error': err,
        }
        KeyboardListener._dbg_log = (KeyboardListener._dbg_log + [entry])[-20:]

_listener = KeyboardListener()

# ── Keyboard Verifier ─────────────────────────────────────────
class KeyboardVerifier:
    """Bug #3: uses Raw Input (not global WH_KEYBOARD_LL) so only the target device triggers detection.
       Bug #12: _busy flag prevents concurrent start_verify calls from leaking hooks."""
    def __init__(self):
        self._thread     = None
        self._detected   = False
        self._start      = 0.0
        self._busy       = False          # Bug #12
        self._handles    = set()
        self._hwnd       = None
        self._wndproc_cb = None

    def start(self, vid_pid=''):
        # Bug #12: ignore concurrent calls
        if self._busy:
            return
        self._busy     = True
        self._detected = False
        self._start    = time.time()
        self._handles  = _get_vid_pid_handles().get(vid_pid.upper(), set()) if vid_pid else set()
        self._thread   = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._busy = False
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def result(self):
        elapsed = time.time() - self._start
        return {
            'detected': self._detected,
            'elapsed':  round(elapsed, 1),
            'timeout':  elapsed >= 10.0,
        }

    def _run(self):
        # Bug #3: use Raw Input instead of WH_KEYBOARD_LL
        cls_name = 'BBVerifier'
        hmod = kernel32.GetModuleHandleW(None)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                sz = wt.UINT(0)
                user32.GetRawInputData(lparam, RID_INPUT, None,
                                       ctypes.byref(sz), ctypes.sizeof(RAWINPUTHEADER))
                if sz.value > 0:
                    buf = ctypes.create_string_buffer(sz.value)
                    user32.GetRawInputData(lparam, RID_INPUT, buf,
                                           ctypes.byref(sz), ctypes.sizeof(RAWINPUTHEADER))
                    ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
                    if ri.header.dwType == RIM_TYPEKEYBOARD:
                        from_dev = (not self._handles or ri.header.hDevice in self._handles)
                        if from_dev:
                            self._detected = True
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_cb = WNDPROC_T(wnd_proc)
        user32.UnregisterClassW(cls_name, hmod)

        wc = WNDCLASSEXW()
        wc.cbSize        = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc   = self._wndproc_cb
        wc.hInstance     = hmod
        wc.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(0, cls_name, cls_name, WS_POPUP,
                                       0, 0, 0, 0, None, None, hmod, None)
        self._hwnd = hwnd

        if hwnd:
            rid = RAWINPUTDEVICE()
            rid.usUsagePage = 0x01
            rid.usUsage     = 0x06
            rid.dwFlags     = RIDEV_INPUTSINK
            rid.hwndTarget  = hwnd
            user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                           ctypes.sizeof(RAWINPUTDEVICE))

        deadline = time.time() + 12
        msg = wt.MSG()
        while self._busy and time.time() < deadline and not self._detected:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hwnd == hwnd:
            self._hwnd = None
        user32.UnregisterClassW(cls_name, hmod)
        self._busy = False

_verifier = KeyboardVerifier()

# ── Registry-based keyboard enumeration (no subprocess/PowerShell) ───────────

def _enum_keyboards_rawinput():
    """Enumerate keyboards via GetRawInputDeviceList — only currently connected devices."""
    RIM_TYPEKEYBOARD = 1
    RIDI_DEVICENAME  = 0x20000007

    class RAWINPUTDEVICELIST(ctypes.Structure):
        _fields_ = [('hDevice', wt.HANDLE), ('dwType', wt.DWORD)]

    user32 = ctypes.windll.user32
    count = wt.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))
    if count.value == 0:
        return []

    buf = (RAWINPUTDEVICELIST * count.value)()
    user32.GetRawInputDeviceList(buf, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))

    results = []
    seen_vidpid = set()
    for item in buf:
        if item.dwType != RIM_TYPEKEYBOARD:
            continue
        # get device path
        sz = wt.UINT(0)
        user32.GetRawInputDeviceInfoW(item.hDevice, RIDI_DEVICENAME, None, ctypes.byref(sz))
        name_buf = ctypes.create_unicode_buffer(sz.value)
        user32.GetRawInputDeviceInfoW(item.hDevice, RIDI_DEVICENAME, name_buf, ctypes.byref(sz))
        dev_path = name_buf.value  # e.g. \\?\HID#VID_5566&PID_0008#...

        m = re.search(r'(VID_[0-9A-F]+&PID_[0-9A-F]+)', dev_path.upper())
        if not m:
            continue
        vid_pid = m.group(1)
        if vid_pid in seen_vidpid:
            continue
        seen_vidpid.add(vid_pid)

        # try to get friendly name from registry
        friendly = _get_friendly_name(vid_pid) or 'HID Keyboard Device'
        results.append({'FriendlyName': friendly, 'InstanceId': dev_path})
    return results


def _get_friendly_name(vid_pid: str) -> str:
    """Look up FriendlyName for a VID_xxxx&PID_xxxx from registry."""
    hid_base = rf'SYSTEM\CurrentControlSet\Enum\HID\{vid_pid}'
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hid_base) as vp_root:
            jdx = 0
            while True:
                try:
                    instance = winreg.EnumKey(vp_root, jdx)
                    jdx += 1
                except OSError:
                    break
                with winreg.OpenKey(vp_root, instance) as inst_key:
                    try:
                        name, _ = winreg.QueryValueEx(inst_key, 'FriendlyName')
                        if name:
                            return name
                    except OSError:
                        pass
    except OSError:
        pass
    return ''

# ── Default config ────────────────────────────────────────────
DEFAULT_CONFIG = {
    "selected_keyboard": "",
    "selected_keyboard_name": "",
    "overlay_enabled": False,
    "app_filter": {"mode": "disabled", "apps": []},
    "modes": [
        {
            "id": "mode_main",
            "name": "Main",
            "color": "#7c8aff",
            "icon": "zap",
            "binds": {}
        }
    ]
}

_window = None

def _migrate_cfg(cfg):
    """Fix legacy configs where ESC was stored as VK 192 (tilde) instead of VK 27."""
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
    return cfg

# ── API ───────────────────────────────────────────────────────
class API:
    def load_config(self):
        # Bug #10: try/except + backup fallback
        def _try(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return None

        cfg = _try(CONFIG)
        if cfg is not None:
            return _migrate_cfg(cfg)

        # Try backups newest-first
        try:
            backups = sorted(
                [f for f in os.listdir(BACKUP_DIR)
                 if f.startswith('config.') and f.endswith('.json') and 'corrupted' not in f],
                reverse=True
            )
            for b in backups:
                cfg = _try(os.path.join(BACKUP_DIR, b))
                if cfg is not None:
                    return _migrate_cfg(cfg)
        except Exception:
            pass

        # Archive corrupted file, return default
        if os.path.exists(CONFIG):
            try:
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                shutil.copy2(CONFIG, os.path.join(BACKUP_DIR, f'config.corrupted-{ts}.json'))
            except Exception:
                pass

        return dict(DEFAULT_CONFIG)

    def save_config(self, config):
        # Bug #10: atomic write (tmp → replace) + rolling backup (keep 5)
        try:
            if os.path.exists(CONFIG):
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                shutil.copy2(CONFIG, os.path.join(BACKUP_DIR, f'config.{ts}.json'))
                # Rotate: keep newest 5
                try:
                    baks = sorted([
                        f for f in os.listdir(BACKUP_DIR)
                        if f.startswith('config.') and f.endswith('.json')
                        and 'corrupted' not in f
                    ])
                    for old in baks[:-5]:
                        try:
                            os.remove(os.path.join(BACKUP_DIR, old))
                        except Exception:
                            pass
                except Exception:
                    pass

            tmp = CONFIG + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG)

            if _listener._running:
                _listener._config = config
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Keyboard detection ────────────────────────────────────
    def detect_keyboards(self):
        try:
            kbs = _enum_keyboards_rawinput()
        except Exception as e:
            return [{'error': str(e)}]

        raw_data = {}
        try:
            raw_data = _get_raw_kb_data()
        except Exception:
            pass

        # Deduplicate by VID_PID — one physical keyboard creates many HID sub-nodes
        # (MI_00, MI_01, COL04…) all sharing the same VID_PID. Grouping by VID_PID
        # collapses them into one entry. True disambiguation of two identical-model
        # keyboards requires parent-USB-device lookup (future work).
        seen_vidpid = set()
        results     = []
        for kb in kbs:
            iid = kb.get('InstanceId', '').upper()
            if 'VHIDEV' in iid:
                continue
            m = re.search(r'(VID_[0-9A-F]+&PID_[0-9A-F]+)', iid)
            vid_pid = m.group(1) if m else iid[:30]
            if vid_pid in seen_vidpid:
                continue
            seen_vidpid.add(vid_pid)
            rd = raw_data.get(vid_pid, {})
            kc = rd.get('key_count', 0)
            if raw_data and not rd.get('real_kb', True):
                continue
            results.append({
                'name':        kb.get('FriendlyName', 'HID Keyboard Device'),
                'vid_pid':     vid_pid,
                'instance_id': kb.get('InstanceId', ''),
                'key_count':   kc,
                'size_label':  _estimate_size(kc),
            })
        return results

    # ── Verification ──────────────────────────────────────────
    def start_verify(self, vid_pid=''):
        # Bug #3: pass vid_pid so verifier uses Raw Input per-device filter
        _verifier.start(vid_pid)
        return {'ok': True}

    def poll_verify(self):
        return _verifier.result

    def stop_verify(self):
        _verifier.stop()
        return {'ok': True}

    # ── Listener control ──────────────────────────────────────
    def start_listener(self, vid_pid, dev_name=''):
        cfg = self.load_config()
        _listener.configure(cfg, vid_pid, dev_name)
        # Bug #2: start() now returns {'ok': bool, 'error': ...}
        return _listener.start()

    def stop_listener(self):
        _listener.stop()
        return {'ok': True}

    def update_listener_config(self, cfg):
        """Hot-reload config into the running listener without stopping it."""
        _listener._config = cfg
        return {'ok': True}

    def listener_status(self):
        return _listener.status

    def listener_log(self):
        return KeyboardListener._dbg_log

    # ── File dialog ───────────────────────────────────────────
    def browse_file(self):
        result = _window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=('All files (*.*)', 'Programs (*.exe)', 'Shortcuts (*.lnk)')
        )
        return result[0] if result else ''

    # ── Installed apps from Start Menu (Feature 1) ────────────
    def list_installed_apps(self):
        import glob as _glob
        import re as _re
        import base64 as _b64
        results = []
        seen = set()
        dirs = [
            os.path.expandvars(r'%APPDATA%\Microsoft\Windows\Start Menu\Programs'),
            r'C:\ProgramData\Microsoft\Windows\Start Menu\Programs',
        ]
        skip_words = ('uninstall', 'deinstall', 'remove', 'readme', 'license', 'help')
        for base in dirs:
            for lnk in _glob.glob(os.path.join(base, '**', '*.lnk'), recursive=True):
                name = os.path.splitext(os.path.basename(lnk))[0].strip()
                key = name.lower()
                if key in seen or any(s in key for s in skip_words):
                    continue
                seen.add(key)
                results.append({'name': name, 'exe': lnk, 'ftype': 'lnk',
                                'icon': _get_icon_b64(lnk)})

        # ── Steam games ───────────────────────────────────────────
        steam_path = ''
        try:
            import winreg as _wr
            k = _wr.OpenKey(_wr.HKEY_CURRENT_USER, r'Software\Valve\Steam')
            steam_path = _wr.QueryValueEx(k, 'SteamPath')[0].replace('/', '\\')
            _wr.CloseKey(k)
        except Exception:
            for _sp in [r'C:\Program Files (x86)\Steam', r'C:\Program Files\Steam']:
                if os.path.exists(_sp):
                    steam_path = _sp
                    break

        if steam_path:
            # Collect all steamapps directories (main + extra libraries)
            library_dirs = []
            vdf = os.path.join(steam_path, 'steamapps', 'libraryfolders.vdf')
            if os.path.exists(vdf):
                try:
                    with open(vdf, 'r', encoding='utf-8') as _f:
                        _vdf_text = _f.read()
                    for _p in _re.findall(r'"path"\s+"([^"]+)"', _vdf_text):
                        _sa = os.path.join(_p.replace('\\\\', '\\'), 'steamapps')
                        if os.path.exists(_sa):
                            library_dirs.append(_sa)
                except Exception:
                    pass
            _main_sa = os.path.join(steam_path, 'steamapps')
            if os.path.exists(_main_sa) and _main_sa not in library_dirs:
                library_dirs.insert(0, _main_sa)

            # Icon cache dir
            _icon_cache = os.path.join(steam_path, 'appcache', 'librarycache')

            seen_ids = set()
            for _sa_dir in library_dirs:
                for _acf in _glob.glob(os.path.join(_sa_dir, 'appmanifest_*.acf')):
                    try:
                        with open(_acf, 'r', encoding='utf-8') as _f:
                            _txt = _f.read()
                        _aid = _re.search(r'"appid"\s+"(\d+)"', _txt)
                        _aname = _re.search(r'"name"\s+"([^"]+)"', _txt)
                        if not (_aid and _aname):
                            continue
                        _id_str = _aid.group(1)
                        _game_name = _aname.group(1).strip()
                        if _id_str in seen_ids or not _game_name:
                            continue
                        seen_ids.add(_id_str)

                        # Try to get cached icon (icon.jpg from librarycache)
                        _ico_b64 = None
                        for _suf in (f'{_id_str}_icon.jpg', f'{_id_str}_icon.png'):
                            _ico_path = os.path.join(_icon_cache, _suf)
                            if os.path.exists(_ico_path):
                                try:
                                    from PIL import Image as _Img
                                    import io as _io
                                    _img = _Img.open(_ico_path).convert('RGBA').resize((20, 20))
                                    _buf = _io.BytesIO()
                                    _img.save(_buf, format='PNG')
                                    _ico_b64 = 'data:image/png;base64,' + _b64.b64encode(_buf.getvalue()).decode()
                                except Exception:
                                    pass
                                break

                        results.append({
                            'name': _game_name,
                            'exe': _id_str,
                            'ftype': 'steam',
                            'icon': _ico_b64,
                        })
                    except Exception:
                        continue

        return sorted(results, key=lambda x: x['name'].lower())

    # ── Running process list (app filter picker) ───────────────
    def list_processes(self):
        """Return unique running processes [{pid, name, exe}], sorted by name."""
        results = []
        seen    = set()
        try:
            Psapi  = ctypes.windll.Psapi
            buf    = (wt.DWORD * 4096)()
            needed = wt.DWORD(0)
            Psapi.EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(needed))
            count = needed.value // ctypes.sizeof(wt.DWORD)
            for i in range(count):
                pid = int(buf[i])
                if pid == 0:
                    continue
                hproc = kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not hproc:
                    continue
                try:
                    path_buf = ctypes.create_unicode_buffer(260)
                    sz = wt.DWORD(260)
                    if kernel32.QueryFullProcessImageNameW(
                            hproc, 0, path_buf, ctypes.byref(sz)):
                        path = path_buf.value
                        name = path.split('\\')[-1]
                        key  = name.lower()
                        if key not in seen:
                            seen.add(key)
                            results.append({'pid': pid, 'name': name, 'exe': path})
                finally:
                    kernel32.CloseHandle(hproc)
        except Exception as e:
            return [{'error': str(e)}]
        return sorted(results, key=lambda x: x['name'].lower())

    # ── Test action ───────────────────────────────────────────
    def test_action(self, atype, value):
        # Bug #4: each type handled explicitly; Bug #8: os.startfile()
        try:
            if atype in ('url', 'lnk', 'exe'):
                os.startfile(value.strip('"\''))
                return {"ok": True}
            elif atype == 'steam':
                os.startfile(f'steam://rungameid/{value}')
                return {"ok": True}
            elif atype in ('sound', 'stop_sound', 'mode_switch'):
                return {"ok": False, "error": "Testing this action type is not supported here"}
            else:
                return {"ok": False, "error": f"Unknown action type: {atype}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


api = API()
if getattr(sys, 'frozen', False):
    html = os.path.join(sys._MEIPASS, 'app.html').replace('\\', '/')
else:
    html = os.path.join(BASE, 'app.html').replace('\\', '/')

_window = webview.create_window(
    'BindBoard',
    url=f'file:///{html}',
    width=1200, height=820,
    min_size=(900, 640),
    js_api=api,
    text_select=False,
)

# ── Feature 3: minimize to tray instead of closing ────────────
_tray_icon  = None
_tray_failed = False  # if pystray/PIL never come up, closing must actually close

def _tray_lang():
    """Return 'ru' if Windows UI language is Russian, else 'en'."""
    try:
        import locale
        lang = locale.getdefaultlocale()[0] or ''
        if lang.lower().startswith('ru'):
            return 'ru'
    except Exception:
        pass
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Control Panel\International')
        val, _ = winreg.QueryValueEx(key, 'LocaleName')
        winreg.CloseKey(key)
        if str(val).lower().startswith('ru'):
            return 'ru'
    except Exception:
        pass
    return 'en'

_TRAY_STRINGS = {
    'en': {
        'open':   'Open BindBoard',
        'toggle': 'Pause / Resume listener',
        'exit':   'Exit',
    },
    'ru': {
        'open':   'Открыть BindBoard',
        'toggle': 'Пауза / Продолжить',
        'exit':   'Выход',
    },
}

def _start_tray():
    global _tray_icon, _tray_failed
    try:
        from PIL import Image
        import pystray

        ico_path = os.path.join(BASE, 'bindboard.ico')
        if not os.path.exists(ico_path) and getattr(sys, 'frozen', False):
            ico_path = os.path.join(sys._MEIPASS, 'bindboard.ico')
        img = Image.open(ico_path).resize((64, 64))

        S = _TRAY_STRINGS[_tray_lang()]

        def on_open(icon, item):
            _window.show()

        def on_toggle(icon, item):
            if _listener._running:
                _listener.stop()
            elif _listener._vid_pid:
                cfg = api.load_config()
                _listener.configure(cfg, _listener._vid_pid, _listener._dev_name)
                _listener.start()

        def on_exit(icon, item):
            icon.stop()
            _listener.stop()
            _window.destroy()
            os._exit(0)  # force-kill process and all WebView2 children

        menu = pystray.Menu(
            pystray.MenuItem(S['open'],   on_open, default=True),
            pystray.MenuItem(S['toggle'], on_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(S['exit'],   on_exit),
        )
        _tray_icon = pystray.Icon('BindBoard', img, 'BindBoard', menu)
        _tray_icon.run()
    except Exception:
        # pystray/PIL unavailable (missing dep, no tray area, icon file missing, ...).
        # Without this flag _on_closing() would keep hiding the window forever with
        # no tray to bring it back from and no way to quit except Task Manager.
        _tray_failed = True

threading.Thread(target=_start_tray, daemon=True).start()

def _on_closing():
    if _tray_failed:
        _listener.stop()
        return True    # no working tray to minimize to — let the close proceed
    _window.hide()
    return False   # cancel the close, keep running in the tray

_window.events.closing += _on_closing

webview.start(debug=False, gui='edgechromium')
