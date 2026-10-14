"""Backend tests for BindBoard (stdlib unittest, Windows only).

Run from the project folder:
    python -m unittest discover -s tests -v

The RawInput* tests inject harmless keys (F23/F24, a Shift tap) with SendInput and need an
interactive desktop session. Set BINDBOARD_SKIP_INPUT_TESTS=1 to skip them.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_HOME = tempfile.mkdtemp(prefix='bindboard_test_home_')
os.environ['BINDBOARD_HOME'] = _HOME          # never touch the real config
sys.path.insert(0, ROOT)

import app  # noqa: E402

SKIP_INPUT = os.environ.get('BINDBOARD_SKIP_INPUT_TESTS') == '1'
TEST_VIDPID = 'VID_7E57&PID_0001'


# ── helpers ──────────────────────────────────────────────────
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD), ('time', wt.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('ki', _KEYBDINPUT), ('mi', _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [('type', wt.DWORD), ('u', _INPUTUNION)]


_SendInput = ctypes.WinDLL('user32').SendInput
_SendInput.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]


def send_vk(vk, up=False):
    i = _INPUT(type=1)
    i.u.ki = _KEYBDINPUT(vk, 0, 0x2 if up else 0, 0, 0)
    _SendInput(1, ctypes.byref(i), ctypes.sizeof(_INPUT))


def send_scan(scan, up=False, ext=False):
    i = _INPUT(type=1)
    i.u.ki = _KEYBDINPUT(0, scan, 0x8 | (0x2 if up else 0) | (0x1 if ext else 0), 0, 0)
    _SendInput(1, ctypes.byref(i), ctypes.sizeof(_INPUT))


def tap(vk, hold=0.02):
    send_vk(vk)
    time.sleep(hold)
    send_vk(vk, up=True)


def wait_for(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


VK_F23, VK_F24 = 0x86, 0x87


class FakeRunner:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))
        fn(*args)


class TempConfigDir:
    """Points app.CONFIG / app.BACKUP_DIR at a fresh directory for one test."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix='bb_cfg_')
        self.saved = (app.CONFIG, app.BACKUP_DIR, dict(app._cfg_state))
        app.CONFIG = os.path.join(self.dir, 'config.json')
        app.BACKUP_DIR = os.path.join(self.dir, 'backups')
        os.makedirs(app.BACKUP_DIR)
        app._cfg_state.update(last_text=None, last_backup=0.0)
        return self

    def __exit__(self, *exc):
        app.CONFIG, app.BACKUP_DIR, state = self.saved
        app._cfg_state.clear()
        app._cfg_state.update(state)
        shutil.rmtree(self.dir, ignore_errors=True)


def sample_config():
    return {
        'selected_keyboard': TEST_VIDPID,
        'modes': [
            {'id': 'a', 'name': 'A', 'color': '#7c8aff', 'binds': {
                str(VK_F23): {'type': 'url', 'value': 'https://example.com'},
                str(VK_F24): {'type': 'mode_switch', 'target': 'b'},
                '160': {'type': 'sound', 'value': '3'},
            }},
            {'id': 'b', 'name': 'B', 'color': '#b389f9', 'binds': {
                str(VK_F24): {'type': 'mode_switch', 'target': '__next__'},
            }},
        ],
        'app_filter': {'mode': 'disabled', 'apps': []},
    }


# ── config ───────────────────────────────────────────────────
class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_unchanged_save_is_noop(self):
        with TempConfigDir():
            cfg = sample_config()
            self.assertTrue(app.save_config(cfg)['ok'])
            self.assertEqual(app.load_config()['modes'][0]['id'], 'a')
            mtime = os.path.getmtime(app.CONFIG)
            time.sleep(0.02)
            r = app.save_config(cfg)
            self.assertTrue(r.get('unchanged'))
            self.assertEqual(os.path.getmtime(app.CONFIG), mtime)

    def test_rejects_non_object(self):
        with TempConfigDir():
            self.assertFalse(app.save_config(['nope'])['ok'])
            self.assertFalse(os.path.exists(app.CONFIG))

    def test_backup_once_per_interval_and_no_duplicates(self):
        with TempConfigDir():
            cfg = sample_config()
            app.save_config(cfg)                       # no previous file → nothing to back up
            self.assertEqual(app._regular_backups(), [])
            cfg['overlay_enabled'] = True
            app.save_config(cfg)                       # first overwrite of the session → backup
            self.assertEqual(len(app._regular_backups()), 1)
            for i in range(5):                         # rapid edits: no further backups
                cfg['n'] = i
                app.save_config(cfg)
            self.assertEqual(len(app._regular_backups()), 1)
            app._backup_current_config()               # same content as newest backup? no → new
            app._backup_current_config()               # identical → skipped
            self.assertLessEqual(len(app._regular_backups()), 2)

    def test_backup_rotation_keeps_newest(self):
        with TempConfigDir():
            for i in range(15):
                with open(os.path.join(app.BACKUP_DIR, f'config.20200101_0000{i:02d}.json'), 'w') as f:
                    f.write(json.dumps({'i': i}))
            app.save_config(sample_config())
            cfg = sample_config()
            cfg['x'] = 1
            app._cfg_state['last_backup'] = 0
            app.save_config(cfg)
            baks = app._regular_backups()
            self.assertEqual(len(baks), app.BACKUP_KEEP)
            self.assertNotIn('config.20200101_000000.json', baks)

    def test_corrupted_config_falls_back_to_backup(self):
        with TempConfigDir():
            with open(os.path.join(app.BACKUP_DIR, 'config.20250101_000000.json'), 'w', encoding='utf-8') as f:
                json.dump(sample_config(), f)
            with open(app.CONFIG, 'w') as f:
                f.write('{broken')
            self.assertEqual(app.load_config()['modes'][1]['id'], 'b')

    def test_corrupted_config_without_backup_gives_default_and_archives(self):
        with TempConfigDir():
            with open(app.CONFIG, 'w') as f:
                f.write('{broken')
            cfg = app.load_config()
            self.assertEqual(cfg['modes'][0]['id'], 'mode_main')
            self.assertTrue(any('corrupted' in f for f in os.listdir(app.BACKUP_DIR)))
            cfg['modes'][0]['binds']['1'] = {}
            self.assertEqual(app.DEFAULT_CONFIG['modes'][0]['binds'], {}, 'default config must not be mutated')

    def test_migrations(self):
        cfg = {'key_rows': [[[192, 'ESC'], [49, '1']]], 'detected_keys': [192, 49],
               'modes': [{'id': 'm', 'binds': {'192': {'type': 'url'}}}]}
        out = app._migrate_cfg(cfg)
        self.assertEqual(out['key_rows'][0][0], [27, 'ESC'])
        self.assertIn('27', out['modes'][0]['binds'])
        self.assertEqual(out['detected_keys'], [27, 49])
        self.assertTrue(app._migrate_cfg({})['modes'])


# ── app filter ───────────────────────────────────────────────
class FilterTests(unittest.TestCase):
    def test_semantics(self):
        f = app.compile_app_filter({'mode': 'deny', 'apps': ['osu!.exe', 'Discord.lnk', 'YouTube', '  ', '.lnk']})
        self.assertFalse(app.filter_allows(f, 'osu!.exe', ''))
        self.assertTrue(app.filter_allows(f, 'osu!lazer.exe', ''))          # .exe is an exact match
        self.assertFalse(app.filter_allows(f, 'discord.exe', ''))           # .lnk stem in exe
        self.assertFalse(app.filter_allows(f, 'chrome.exe', 'cats - youtube - google chrome'))
        self.assertTrue(app.filter_allows(f, 'notepad.exe', 'untitled'))
        allow = app.compile_app_filter({'mode': 'allow', 'apps': ['code.exe']})
        self.assertTrue(app.filter_allows(allow, 'code.exe', ''))
        self.assertFalse(app.filter_allows(allow, 'notepad.exe', ''))
        self.assertIsNone(app.compile_app_filter({'mode': 'disabled', 'apps': ['x.exe']}))
        self.assertTrue(app.filter_allows(None, 'anything.exe', 'x'))


# ── key normalisation ────────────────────────────────────────
class NormalizeTests(unittest.TestCase):
    def test_modifiers_numpad_and_fakes(self):
        n = app.normalize_vk
        self.assertEqual((n(16, 0x2A, False), n(16, 0x36, False)), (160, 161))
        self.assertEqual((n(17, 0x1D, False), n(17, 0x1D, True)), (162, 163))
        self.assertEqual((n(18, 0x38, False), n(18, 0x38, True)), (164, 165))
        self.assertIsNone(n(16, 0x2A, True))     # fake shift around extended keys
        self.assertIsNone(n(0xFF, 0x00, False))
        self.assertEqual(n(36, 0x47, False), 103)  # numpad 7 with NumLock off → still numpad 7
        self.assertEqual(n(103, 0x47, False), 103)
        self.assertEqual(n(36, 0x47, True), 36)    # real Home key (extended)
        self.assertEqual(n(46, 0x53, False), 110)  # numpad Del → decimal
        self.assertEqual(n(65, 0x1E, False), 65)


# ── listener logic (no real input) ───────────────────────────
class ListenerLogicTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.hub = mock.Mock()
        self.hub.alive.return_value = True
        self.hub.subscribe.return_value = True
        self.lst = app.KeyboardListener(self.hub, self.runner)
        self.lst.configure(sample_config(), TEST_VIDPID, 'Test KB')
        self.executed = []
        p = mock.patch.object(app, 'execute_action', side_effect=lambda b: (self.executed.append(b) or (True, None)))
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(app, '_device_connected', return_value=True)
        p2.start()
        self.addCleanup(p2.stop)

    def ev(self, vk, up=False, vid=TEST_VIDPID, scan=0, e0=False):
        return app.KeyEvent(1, vid, vk, scan, e0, up, time.perf_counter())

    def test_start_requires_device(self):
        with mock.patch.object(app, '_device_connected', return_value=False):
            self.assertEqual(self.lst.start()['error'], 'device_not_found')
        self.assertTrue(self.lst.start()['ok'])
        self.assertTrue(self.lst.status['running'])

    def test_action_and_mode_switch_and_cycle(self):
        changes = []
        self.lst.on_change = lambda: changes.append(self.lst.status['mode'])
        self.lst.start()
        self.lst._on_event(self.ev(VK_F23))
        self.assertEqual(self.executed[-1]['type'], 'url')
        self.lst._on_event(self.ev(VK_F24))
        self.assertEqual(self.lst.status['mode'], 'b')
        self.lst._on_event(self.ev(VK_F24, up=True))
        self.lst._on_event(self.ev(VK_F24))                     # '__next__' cycles back to a
        self.assertEqual(self.lst.status['mode'], 'a')
        self.assertEqual(changes[-2:], ['b', 'a'])

    def test_dangling_target_cycles_instead_of_trapping(self):
        cfg = sample_config()
        cfg['modes'][0]['binds'][str(VK_F24)]['target'] = '_1784532336213'   # mode that no longer exists
        self.lst.configure(cfg, TEST_VIDPID)
        self.lst.start()
        self.lst._on_event(self.ev(VK_F24))
        self.assertEqual(self.lst.status['mode'], 'b')

    def test_single_mode_switch_reports_error(self):
        cfg = {'modes': [{'id': 'only', 'binds': {'1': {'type': 'mode_switch', 'target': 'gone'}}}]}
        self.lst.configure(cfg, TEST_VIDPID)
        entry = self.lst.handle_press(1)
        self.assertFalse(entry['ok'])

    def test_autorepeat_and_release(self):
        self.lst.start()
        for _ in range(3):
            self.lst._on_event(self.ev(VK_F23))                 # held key repeats
        self.assertEqual(len(self.executed), 1)
        self.lst._on_event(self.ev(VK_F23, up=True))
        self.lst._on_event(self.ev(VK_F23))
        self.assertEqual(len(self.executed), 2)

    def test_modifier_bind_fires_from_raw_shift(self):
        self.lst.start()
        self.lst._on_event(self.ev(16, scan=0x2A))              # raw VK_SHIFT → layout code 160
        self.assertEqual(self.executed[-1]['type'], 'sound')

    def test_other_keyboards_ignored_and_logged_once(self):
        self.lst.start()
        for _ in range(4):
            self.lst._on_event(self.ev(VK_F23, vid='VID_1111&PID_2222'))
            self.lst._on_event(self.ev(VK_F23, up=True, vid='VID_1111&PID_2222'))
        self.assertEqual(self.executed, [])
        log = self.lst.log_entries()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]['count'], 4)
        self.assertIsNone(log[0]['vk'], 'keys typed on other keyboards must not be recorded')

    def test_app_filter_blocks(self):
        cfg = sample_config()
        cfg['app_filter'] = {'mode': 'deny', 'apps': ['blocked.exe']}
        self.lst.configure(cfg, TEST_VIDPID)
        self.lst.start()
        with mock.patch.object(app, '_get_foreground_info', return_value=('blocked.exe', 'x')):
            self.lst._on_event(self.ev(VK_F23))
        self.assertEqual(self.executed, [])
        self.assertEqual(self.lst.log_entries()[-1]['note'], 'filtered')

    def test_capture_pauses_binds(self):
        self.lst.start()
        app._capture(+1)
        try:
            self.lst._on_event(self.ev(VK_F23))
        finally:
            app._capture(-1)
        self.assertEqual(self.executed, [])

    def test_hot_reload_keeps_valid_mode(self):
        self.lst.start()
        self.lst._on_event(self.ev(VK_F24))
        self.assertEqual(self.lst.status['mode'], 'b')
        self.lst.set_config(sample_config())
        self.assertEqual(self.lst.status['mode'], 'b')
        cfg = sample_config()
        del cfg['modes'][1]
        self.lst.set_config(cfg)
        self.assertEqual(self.lst.status['mode'], 'a')

    def test_scanner_watchdog_resumes_binds(self):
        sc = app.KeyScanner(mock.Mock(subscribe=mock.Mock(return_value=True)))
        before = app._capture_count[0]
        sc.start('')
        self.assertEqual(app._capture_count[0], before + 1, 'binds pause while scanning')
        self.assertTrue(sc._check_idle(sc._token))
        sc._last_poll -= sc.IDLE_TIMEOUT + 1          # the page stopped polling
        self.assertFalse(sc._check_idle(sc._token))
        self.assertFalse(sc.active)
        self.assertEqual(app._capture_count[0], before, 'binds resume after an abandoned scan')

    def test_device_unplug_and_replug(self):
        self.lst.start()
        changes = []
        self.lst.on_change = lambda: changes.append(self.lst.status['connected'])
        with mock.patch.object(app, '_device_connected', return_value=False):
            self.lst._on_event(app.DeviceEvent('removed', 1, TEST_VIDPID))
        self.lst._on_event(app.DeviceEvent('arrived', 2, TEST_VIDPID))
        self.assertEqual(changes, [False, True])


# ── actions ──────────────────────────────────────────────────
class ExecuteActionTests(unittest.TestCase):
    def test_dispatch(self):
        with mock.patch('os.startfile', create=True) as sf, mock.patch.object(app.subprocess, 'Popen') as po:
            self.assertEqual(app.execute_action({'type': 'url', 'value': 'https://x.org'}), (True, None))
            sf.assert_called_with('https://x.org')
            app.execute_action({'type': 'app', 'value': '"C:\\a b\\c.lnk"'})
            sf.assert_called_with('C:\\a b\\c.lnk')
            app.execute_action({'type': 'app', 'value': 'shell:AppsFolder\\X_1!App'})
            self.assertEqual(po.call_args[0][0], ['explorer.exe', 'shell:AppsFolder\\X_1!App'])
            app.execute_action({'type': 'steam', 'value': '730'})
            sf.assert_called_with('steam://rungameid/730')
        self.assertEqual(app.execute_action({'type': 'lnk', 'value': ''}), (False, 'empty value'))
        self.assertFalse(app.execute_action({'type': 'nope'})[0])
        with mock.patch('os.startfile', create=True, side_effect=OSError('boom')):
            self.assertEqual(app.execute_action({'type': 'exe', 'value': 'x.exe'}), (False, 'boom'))

    def test_runner_executes_in_order_off_thread(self):
        r = app.ActionRunner()
        seen = []
        done = threading.Event()
        for i in range(5):
            r.submit(lambda i=i: seen.append((i, threading.current_thread().name)))
        r.submit(done.set)
        self.assertTrue(done.wait(2))
        self.assertEqual([s[0] for s in seen], list(range(5)))
        self.assertTrue(all(name == 'ActionRunner' for _, name in seen))


# ── Soundpad ─────────────────────────────────────────────────
_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.CreateNamedPipeW.restype = wt.HANDLE
_k32.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPVOID]
_k32.ConnectNamedPipe.argtypes = [wt.HANDLE, wt.LPVOID]
_k32.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.WriteFile.argtypes = [wt.HANDLE, wt.LPCVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.FlushFileBuffers.argtypes = [wt.HANDLE]
_k32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
_k32.CloseHandle.argtypes = [wt.HANDLE]


class FakeSoundpad(threading.Thread):
    """Minimal named-pipe server that answers like Soundpad."""

    def __init__(self, name, reply=b'R-200'):
        super().__init__(daemon=True)
        self.name_ = name
        self.reply = reply
        self.received = []
        self.ready = threading.Event()

    def run(self):
        h = _k32.CreateNamedPipeW(self.name_, 3, 4 | 2, 1, 4096, 4096, 0, None)  # DUPLEX, MESSAGE
        self.ready.set()
        _k32.ConnectNamedPipe(h, None)
        buf = ctypes.create_string_buffer(4096)
        n = wt.DWORD()
        _k32.ReadFile(h, buf, 4096, ctypes.byref(n), None)
        self.received.append(buf.raw[:n.value].decode())
        _k32.WriteFile(h, self.reply, len(self.reply), ctypes.byref(n), None)
        _k32.FlushFileBuffers(h)
        _k32.DisconnectNamedPipe(h)
        _k32.CloseHandle(h)


class SoundpadTests(unittest.TestCase):
    def make(self, reply=b'R-200'):
        sp = app.Soundpad()
        sp.PIPE = r'\\.\pipe\bindboard_test_sp_%d_%d' % (os.getpid(), id(sp))
        srv = FakeSoundpad(sp.PIPE, reply)
        srv.start()
        srv.ready.wait(2)
        return sp, srv

    def test_play_via_pipe(self):
        sp, srv = self.make()
        t = time.perf_counter()
        self.assertEqual(sp.play('#7'), (True, None))
        self.assertLess(time.perf_counter() - t, 0.5)
        srv.join(2)
        self.assertEqual(srv.received, ['DoPlaySound(7)'])

    def test_error_reply_falls_back_to_exe(self):
        sp, srv = self.make(b'R-404')
        with mock.patch.object(sp, 'find_exe', return_value=r'C:\fake\Soundpad.exe'), \
                mock.patch.object(app.subprocess, 'Popen') as po:
            self.assertEqual(sp.stop(), (True, None))
            self.assertEqual(po.call_args[0][0], [r'C:\fake\Soundpad.exe', '-rc', 'DoStopSound()'])
        srv.join(2)
        self.assertEqual(srv.received, ['DoStopSound()'])

    def test_not_running_falls_back_to_exe(self):
        sp = app.Soundpad()
        sp.PIPE = r'\\.\pipe\bindboard_test_missing_%d' % os.getpid()
        with mock.patch.object(sp, 'find_exe', return_value=r'C:\fake\Soundpad.exe'), \
                mock.patch.object(app.subprocess, 'Popen') as po:
            self.assertEqual(sp.play(2), (True, None))
            self.assertEqual(po.call_args[0][0], [r'C:\fake\Soundpad.exe', '-rc', 'DoPlaySound(2)'])
        with mock.patch.object(sp, 'find_exe', return_value=None):
            self.assertFalse(sp.play(2)[0])

    def test_rejects_bad_index(self):
        self.assertFalse(app.Soundpad().play('1); DoSomething(')[0])


# ── icons / installed apps ───────────────────────────────────
class IconTests(unittest.TestCase):
    def test_stock_icon_has_alpha(self):
        from PIL import Image
        u32 = ctypes.WinDLL('user32')
        u32.LoadIconW.restype = wt.HICON
        u32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPVOID]
        h = u32.LoadIconW(None, ctypes.c_void_p(32516))  # IDI_INFORMATION
        uri = app._hicon_to_png_b64(h, 32)
        self.assertTrue(uri and uri.startswith('data:image/png;base64,'))
        img = Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1])))
        self.assertEqual((img.mode, img.size), ('RGBA', (32, 32)))
        lo, hi = img.getchannel('A').getextrema()
        self.assertEqual((lo, hi), (0, 255), 'icon needs transparent and opaque pixels')

    def test_shortcut_icon_from_worker_thread(self):
        base = os.path.expandvars(r'%ProgramData%\Microsoft\Windows\Start Menu\Programs')
        lnks = [os.path.join(dp, f) for dp, _, fs in os.walk(base) for f in fs if f.endswith('.lnk')][:3]
        if not lnks:
            self.skipTest('no Start Menu shortcuts')
        out = []
        t = threading.Thread(target=lambda: out.extend(app._get_icon_b64(p) for p in lnks))
        t.start()
        t.join(30)
        self.assertTrue(any(out), 'icons must work on API worker threads (COM initialised)')

    def test_steam_icon_layouts(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, '10'))
            for name, size in (('a' * 40 + '.jpg', 900), ('b' * 40 + '.jpg', 400), ('header.jpg', 100)):
                with open(os.path.join(d, '10', name), 'wb') as f:
                    f.write(b'x' * size)
            self.assertTrue(app._steam_icon(d, '10').endswith('b' * 40 + '.jpg'))
            with open(os.path.join(d, '20_icon.jpg'), 'wb') as f:
                f.write(b'x')
            self.assertTrue(app._steam_icon(d, '20').endswith('20_icon.jpg'))
            self.assertIsNone(app._steam_icon(d, '30'))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_installed_apps_cache(self):
        first = app.list_installed_apps_cached(force=True)
        t = time.perf_counter()
        second = app.list_installed_apps_cached()
        self.assertIs(first, second)
        self.assertLess(time.perf_counter() - t, 0.01)
        for item in first:
            self.assertEqual(set(item), {'name', 'exe', 'ftype', 'icon'})


# ── mode overlay (rendered off-screen, never shown to the user) ──
class OverlayTests(unittest.TestCase):
    def test_overlay_paints(self):
        from PIL import Image
        if not app._ov_hwnd[0]:
            app._start_overlay()
            self.assertTrue(wait_for(lambda: app._ov_hwnd[0], 3))
        h = app._ov_hwnd[0]
        app._ov_data.update(name='Test', acc=0xF989B3, prefix='Mode')
        app.SetWindowPos(h, None, -30000, -30000, 320, 56, 0x0050)
        try:
            app.InvalidateRect(h, None, True)
            app.UpdateWindow(h)
            u32 = ctypes.WinDLL('user32')
            u32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]

            def grab():
                screen = app.GetDC(None)
                dc = app.CreateCompatibleDC(screen)
                bmi = app.BITMAPINFO()
                bmi.bmiHeader.biSize = ctypes.sizeof(app.BITMAPINFOHEADER)
                bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = 320, -56
                bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
                bits = wt.LPVOID()
                hbm = app.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
                old = app.SelectObject(dc, hbm)
                u32.PrintWindow(h, dc, 2)
                app.GdiFlush()
                im = Image.frombuffer('RGB', (320, 56), ctypes.string_at(bits.value, 320 * 56 * 4), 'raw', 'BGRX', 0, 1)
                app.SelectObject(dc, old)
                app.DeleteObject(hbm)
                app.DeleteDC(dc)
                app.ReleaseDC(None, screen)
                return im.copy()
            img = grab()
            for _ in range(40):              # DWM composes layered windows asynchronously
                if img.getpixel((1, 28)) != (0, 0, 0):
                    break
                time.sleep(0.025)
                img = grab()
        finally:
            app.ShowWindow(h, 0)
        self.assertEqual(img.getpixel((1, 28)), (179, 137, 249))    # accent bar
        self.assertEqual(img.getpixel((300, 5)), (19, 19, 21))      # background
        self.assertGreater(len(img.getcolors(100000)), 5, 'text was drawn')
        if os.path.exists(app.LOG_FILE):
            with open(app.LOG_FILE, encoding='utf-8') as f:
                self.assertNotIn('overlay', f.read())

    def test_overlay_prefix_follows_language(self):
        with mock.patch.object(app, '_ov_hwnd', [None]):
            app._show_mode_overlay('X', '#ffffff', 'ru')  # no window → returns early, prefix untouched
        self.assertEqual(app._resolve_lang('ru'), 'ru')
        self.assertEqual(app._resolve_lang('en'), 'en')
        self.assertIn(app._resolve_lang(''), ('ru', 'en'))


# ── API surface ──────────────────────────────────────────────
class ApiTests(unittest.TestCase):
    def test_every_js_call_exists(self):
        html = open(os.path.join(ROOT, 'app.html'), encoding='utf-8-sig').read()
        calls = set(re.findall(r'pywebview\??\.api\??\.([A-Za-z_]\w*)', html))
        missing = sorted(c for c in calls if not callable(getattr(app.API, c, None)))
        self.assertEqual(missing, [])
        popup_calls = set(re.findall(r'pywebview\.api\.([A-Za-z_]\w*)', app.POPUP_HTML))
        self.assertEqual(sorted(c for c in popup_calls if not callable(getattr(app.PopupAPI, c, None))), [])

    def test_api_exposes_no_attributes(self):
        # pywebview walks every public attribute of js_api; keep it to plain methods
        for obj in (app.API(), app.PopupAPI()):
            self.assertEqual([n for n in vars(obj) if not n.startswith('_')], [])

    def test_frontend_static_checks(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node not installed')
        r = subprocess.run([node, os.path.join(HERE, 'check_frontend.mjs'), os.path.join(ROOT, 'app.html'),
                            os.path.join(ROOT, 'app.py')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_misc_endpoints(self):
        api = app.API()
        self.assertEqual(api.app_info()['version'], app.APP_VERSION)
        self.assertFalse(api.test_action('mode_switch', '')['ok'])
        self.assertEqual(api.test_action('url', '')['error'], 'empty value')
        kbs = api.detect_keyboards()
        self.assertIsInstance(kbs, list)
        for kb in kbs:
            self.assertRegex(kb['vid_pid'], r'^VID_[0-9A-F]+&PID_[0-9A-F]+$')
        self.assertTrue(any(p['name'].lower() == 'python.exe' for p in api.list_processes()))

    def test_ui_bridge_coalesces_pushes(self):
        class W:
            def __init__(self):
                self.calls = []

            def evaluate_js(self, js):
                self.calls.append(js)
        main, popup = W(), W()
        with mock.patch.object(app, '_window', main), mock.patch.object(app, '_popup_win', popup):
            bridge = app.UiBridge()
            for _ in range(10):
                bridge.notify()
            self.assertTrue(wait_for(lambda: main.calls and popup.calls))
            time.sleep(0.15)
        self.assertEqual(len(main.calls), 1)
        self.assertIn('__bbOnStatus', main.calls[0])
        self.assertIn('"running"', main.calls[0])

    def test_instance_key_depends_on_config_dir(self):
        a = app._instance_key()
        with mock.patch.object(app, 'WORK', os.path.join(_HOME, 'other')):
            self.assertNotEqual(a, app._instance_key())


# ── real Raw Input (SendInput) ───────────────────────────────
@unittest.skipIf(SKIP_INPUT, 'BINDBOARD_SKIP_INPUT_TESTS=1')
class RawInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app._hub.injected_as = TEST_VIDPID   # SendInput keys count as the test keyboard

    @classmethod
    def tearDownClass(cls):
        app._hub.injected_as = ''

    def test_hub_delivers_events_fast(self):
        got = []
        sub = lambda ev: got.append((time.perf_counter(), ev)) if isinstance(ev, app.KeyEvent) and ev.vk == VK_F24 else None  # noqa: E731
        self.assertTrue(app._hub.subscribe(sub))
        try:
            lat = []
            for _ in range(30):
                got.clear()
                t0 = time.perf_counter()
                send_vk(VK_F24)
                self.assertTrue(wait_for(lambda: got, 1.0))
                lat.append((got[0][0] - t0) * 1000)
                send_vk(VK_F24, up=True)
                time.sleep(0.01)
            ev = got[0][1]
            self.assertEqual((ev.vid_pid, ev.up), (TEST_VIDPID, False))
            med = statistics.median(lat)
            print(f'\n    SendInput -> subscriber latency: median {med:.2f} ms, max {max(lat):.2f} ms')
            self.assertLess(med, 10)
        finally:
            app._hub.unsubscribe(sub)

    def test_listener_end_to_end_and_verifier_does_not_steal_input(self):
        executed = []
        lst = app.KeyboardListener(app._hub, app.ActionRunner())
        with mock.patch.object(app, '_device_connected', return_value=True), \
                mock.patch.object(app, 'execute_action',
                                  side_effect=lambda b: executed.append(threading.current_thread().name) or (True, None)):
            lst.configure(sample_config(), TEST_VIDPID)
            self.assertTrue(lst.start()['ok'])
            try:
                tap(VK_F23)
                self.assertTrue(wait_for(lambda: executed))
                self.assertEqual(executed, ['ActionRunner'])
                tap(VK_F24)
                self.assertTrue(wait_for(lambda: lst.status['mode'] == 'b'))
                tap(VK_F24)
                self.assertTrue(wait_for(lambda: lst.status['mode'] == 'a'))

                verifier = app.KeyboardVerifier(app._hub)     # used to register its own window
                verifier.start(TEST_VIDPID)
                tap(VK_F23)
                self.assertTrue(wait_for(lambda: verifier.result['detected']))
                time.sleep(0.1)
                self.assertEqual(len(executed), 1, 'binds stay quiet while verifying')
                verifier.stop()

                tap(VK_F23)                                   # listener must still receive input
                self.assertTrue(wait_for(lambda: len(executed) == 2))
            finally:
                lst.stop()

    def test_scanner_normalises_keys(self):
        sc = app.KeyScanner(app._hub)
        self.assertTrue(sc.start(TEST_VIDPID))
        try:
            tap(VK_F23)
            tap(VK_F24)
            send_scan(0x2A)              # left Shift by scan code → raw VK_SHIFT
            time.sleep(0.02)
            send_scan(0x2A, up=True)
            tap(VK_F23)                  # duplicate is ignored
            self.assertTrue(wait_for(lambda: len(sc.keys()) == 3))
        finally:
            keys = sc.stop()
        self.assertEqual(keys, [VK_F23, VK_F24, 160])


def tearDownModule():
    logging_handlers = list(app.log.handlers)
    for h in logging_handlers:
        h.close()
    shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
