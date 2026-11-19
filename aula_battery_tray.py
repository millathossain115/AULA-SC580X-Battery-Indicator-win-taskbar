"""
AULA SC580X Battery Monitor for Windows System Tray (Native Resolution High-Luminance Edition)
Pure Solid High-Intensity White Typography with Native 1:1 Tray Pixel Alignment,
Bottom Monochromatic Level Bar, Type-C Wired Charging Detection, Exact 1% Granularity Sync,
RF Sleep State Retention, and Single-Instance Mutex.

Hardware Support:
- AULA SC580X Wireless Receiver: VID 0x089D, PID 0x062F (Usage Page 0xFF02, 33-byte In/Out Report)
- AULA SC580X Type-C Wired Mode: VID 0x088D, PID 0x062E
- CompX PAW3311 Wireless Fallback: VID 0x3554, PID 0xFA09
"""

import os
import sys
import time
import json
import threading
import traceback
import winreg
import ctypes
from ctypes import wintypes
import contextlib
import tempfile
import psutil
import hid
from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as item

# Log all unhandled exceptions to crash.log for debugging
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    log_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(log_dir, "crash.log")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n--- CRASH AT {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)

sys.excepthook = handle_exception
threading.excepthook = lambda args: handle_exception(args.exc_type, args.exc_value, args.exc_traceback)

# Win32 Constants
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_ALREADY_EXISTS = 183

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
hid_dll = ctypes.windll.hid

class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ('Internal', ctypes.c_size_t),
        ('InternalHigh', ctypes.c_size_t),
        ('Offset', wintypes.DWORD),
        ('OffsetHigh', wintypes.DWORD),
        ('hEvent', wintypes.HANDLE)
    ]

class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('th32ModuleID', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('GlblcntUsage', wintypes.DWORD),
        ('ProccntUsage', wintypes.DWORD),
        ('modBaseAddr', ctypes.c_void_p),
        ('modBaseSize', wintypes.DWORD),
        ('hModule', wintypes.HMODULE),
        ('szModule', ctypes.c_char * 256),
        ('szExePath', ctypes.c_char * 260)
    ]

# Enable High-DPI Awareness so Windows reports physical tray pixel coordinates
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # Per-Monitor V2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

# Monkey-patch pystray._win32 to:
# 1. Save ICO with exact native dimensions sizes=[(image.width, image.height)]
#    avoiding Pillow's default downsampling.
# 2. Call LoadImage with exact pixel width/height (avoiding LR_DEFAULTSIZE stretch distortion).
try:
    import pystray._win32 as _pystray_win32

    @contextlib.contextmanager
    def _custom_serialized_image(image, format, extension=None):
        fd, path = tempfile.mkstemp('.%s' % (extension or format.lower()))
        try:
            with os.fdopen(fd, 'wb') as f:
                if str(format).upper() == 'ICO':
                    image.save(f, format='ICO', sizes=[(image.width, image.height)])
                else:
                    image.save(f, format=format)
            yield path
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    _pystray_win32.serialized_image = _custom_serialized_image

    def _custom_assert_icon_handle(self):
        if self._icon_handle:
            return
        with _custom_serialized_image(self.icon, 'ICO') as icon_path:
            self._icon_handle = _pystray_win32.win32.LoadImage(
                None,
                icon_path,
                _pystray_win32.win32.IMAGE_ICON,
                self.icon.width,
                self.icon.height,
                _pystray_win32.win32.LR_LOADFROMFILE
            )

    _pystray_win32.Icon._assert_icon_handle = _custom_assert_icon_handle
except Exception:
    pass

APP_NAME = "AULA SC580X Battery Tray"
MUTEX_NAME = r"Local\AULA_SC580X_Battery_Monitor_Mutex"
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "AULASc580xBatteryTray"

# Hardware IDs for AULA SC580X
AULA_WIRELESS_VID = 0x089D
AULA_WIRELESS_PID = 0x062F
AULA_WIRED_VID = 0x088D
AULA_WIRED_PID = 0x062E

# Secondary / CompX Fallback IDs
COMPX_WIRELESS_VID = 0x3554
COMPX_WIRELESS_PID = 0xFA09

# Persistent Cache Path
CACHE_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'AULA_SC580X_Battery')
CACHE_FILE = os.path.join(CACHE_DIR, 'cache.json')


def ensure_single_instance():
    """
    Ensure only one instance runs at a time.
    If an existing or zombie instance is already running, terminate it so this
    newly launched instance takes over cleanly.
    """
    try:
        curr_pid = os.getpid()
        parent_pid = os.getppid()
        killed_any = False
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pid = p.info['pid']
                if pid in (curr_pid, parent_pid):
                    continue
                pname = str(p.info['name']).lower()
                cmdline = ' '.join(p.info['cmdline'] or []).lower()
                if 'aula_sc580x_battery' in pname or 'aula_battery_tray.py' in cmdline:
                    p.kill()
                    killed_any = True
            except Exception:
                pass
        if killed_any:
            time.sleep(0.5)
    except Exception:
        pass

    mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return mutex_handle


class MouseBatteryMonitor:
    def __init__(self, mutex_handle=None):
        self.mutex_handle = mutex_handle
        self.battery_pct = self._load_cache() or 100
        self.is_connected = False
        self.is_charging = False
        self.low_battery_warned = False
        self.poll_interval = 4  # 4-second poll interval
        self.running = True
        self.show_gauge_bar = False
        self.icon = None
        self.blink_state = False
        self.ui_lock = threading.Lock()

        # Determine user's exact Windows system tray icon resolution
        self.tray_size = self._get_native_tray_size()

    @property
    def is_low_battery(self):
        """Returns True if battery is in low warning state (<= 20%) while discharging"""
        return (
            self.is_connected
            and not self.is_charging
            and self.battery_pct is not None
            and self.battery_pct <= 20
        )

    def _load_cache(self):
        """Load last known battery percentage from disk cache"""
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    pct = data.get('battery_pct')
                    if pct is not None and 0 <= pct <= 100:
                        return int(pct)
        except Exception:
            pass
        return None

    def _save_cache(self):
        """Save current battery percentage to disk cache"""
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'battery_pct': self.battery_pct,
                    'is_charging': self.is_charging,
                    'updated_at': time.time()
                }, f)
        except Exception:
            pass

    def _get_native_tray_size(self):
        try:
            sz = user32.GetSystemMetrics(49)  # SM_CXSMICON
            if 16 <= sz <= 64:
                return sz
        except Exception:
            pass
        return 20  # Default for 125% DPI displays

    def _read_cbmouse_memory(self):
        """
        If official CBMouse.exe is currently active, read real-time synchronized
        battery and charging state directly from its process memory.
        """
        try:
            for p in psutil.process_iter(['pid', 'name']):
                if p.info['name'] and p.info['name'].lower() == 'cbmouse.exe':
                    pid = p.info['pid']
                    hSnap = kernel32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
                    me = MODULEENTRY32()
                    me.dwSize = ctypes.sizeof(MODULEENTRY32)
                    base = None
                    if kernel32.Module32First(hSnap, ctypes.byref(me)):
                        while True:
                            if b'cbmouse' in me.szModule.lower():
                                base = me.modBaseAddr
                                break
                            if not kernel32.Module32Next(hSnap, ctypes.byref(me)):
                                break
                    kernel32.CloseHandle(hSnap)
                    if not base:
                        continue
                    delta = base - 0x400000
                    hProc = kernel32.OpenProcess(0x10, False, pid)
                    if not hProc:
                        continue
                    try:
                        buf4 = (ctypes.c_char * 4)()
                        read = ctypes.c_size_t()
                        # Read active device pointer at 0x5efcac + delta
                        if kernel32.ReadProcessMemory(hProc, ctypes.c_void_p(0x5efcac + delta), buf4, 4, ctypes.byref(read)):
                            import struct
                            pActive = struct.unpack('<I', buf4.raw)[0]
                            if pActive:
                                dev_buf = (ctypes.c_char * 0x2d8)()
                                if kernel32.ReadProcessMemory(hProc, ctypes.c_void_p(pActive), dev_buf, 0x2d8, ctypes.byref(read)):
                                    nPower = struct.unpack_from('<I', dev_buf.raw, 0x2cc)[0]
                                    nCharging = struct.unpack_from('<I', dev_buf.raw, 0x2d0)[0]
                                    bOnline = struct.unpack_from('<I', dev_buf.raw, 0x284)[0]
                                    if 0 <= nPower <= 100:
                                        return True, nPower, bool(nCharging)
                    finally:
                        kernel32.CloseHandle(hProc)
        except Exception:
            pass
        return False, None, False

    def _query_sc580x_hid(self):
        """
        Query AULA SC580X wireless dongle directly on Usage Page 0xFF02 via 33-byte HID reports.
        Returns: (is_connected: bool, new_pct: int | None, is_charging: bool)
        - new_pct is None if mouse is in RF Sleep (status == 2), preserving current percentage.
        - new_pct is an exact 1-100% integer when online (status == 0).
        """
        all_devs = hid.enumerate()

        # 1. Check if mouse is connected directly via Type-C wired cable
        is_wired = any(
            (d.get('vendor_id') == AULA_WIRED_VID and d.get('product_id') == AULA_WIRED_PID)
            for d in all_devs
        )

        # 2. Locate SC580X wireless receiver on Usage Page 0xFF02 (33-byte in/out report)
        dongle_path = None
        for d in all_devs:
            if d.get('vendor_id') == AULA_WIRELESS_VID and d.get('product_id') == AULA_WIRELESS_PID:
                if d.get('usage_page') == 0xFF02:
                    dongle_path = d['path']
                    break

        if not dongle_path and not is_wired:
            return False, None, False

        if dongle_path:
            path_str = dongle_path.decode('utf-8') if isinstance(dongle_path, bytes) else dongle_path
            hDev = kernel32.CreateFileW(
                path_str,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED,
                None
            )
            if hDev != -1:
                hW = kernel32.CreateEventW(None, True, False, None)
                ovW = OVERLAPPED()
                ovW.hEvent = hW

                hR = kernel32.CreateEventW(None, True, False, None)
                ovR = OVERLAPPED()
                ovR.hEvent = hR
                try:
                    # Construct 33-byte query output packet
                    # Command 0x30, Subcommand 1, Payload Length 0x1B (27)
                    pkt = bytearray(33)
                    pkt[0] = 0x00
                    pkt[1] = 0x30
                    pkt[2] = 0x00
                    pkt[3] = 0x01
                    pkt[4] = 0x1B

                    buf_out = (ctypes.c_ubyte * 33).from_buffer_copy(pkt)
                    written = wintypes.DWORD()
                    kernel32.WriteFile(hDev, buf_out, 33, ctypes.byref(written), ctypes.byref(ovW))
                    kernel32.WaitForSingleObject(hW, 200)

                    buf_in = (ctypes.c_ubyte * 33)()
                    read_bytes = wintypes.DWORD()
                    kernel32.ReadFile(hDev, buf_in, 33, ctypes.byref(read_bytes), ctypes.byref(ovR))
                    if kernel32.WaitForSingleObject(hR, 300) == 0:
                        kernel32.GetOverlappedResult(hDev, ctypes.byref(ovR), ctypes.byref(read_bytes), False)
                        resp = list(buf_in)
                        status = resp[2]
                        # resp[5:9] contains mouse model tag: 'M137' ([77, 49, 51, 55])
                        # resp[13] is charging flag (1 = charging, 0 = discharging)
                        # resp[14] is live exact battery percentage (0-100% at 1% precision)
                        if status == 0 and resp[5:9] == [77, 49, 51, 55]:
                            charging = bool(resp[13]) or is_wired
                            pct = resp[14]
                            if 0 <= pct <= 100:
                                return True, pct, charging
                        elif status == 2:
                            # Mouse is paired but in low-power RF sleep mode
                            return True, None, is_wired
                    else:
                        kernel32.CancelIo(hDev)
                finally:
                    kernel32.CloseHandle(hW)
                    kernel32.CloseHandle(hR)
                    kernel32.CloseHandle(hDev)

        if is_wired:
            return True, None, True

        return False, None, False

    def _query_compx_fallback(self):
        """Secondary fallback for CompX generic dongle (VID 0x3554, PID 0xFA09)"""
        try:
            for d in hid.enumerate(COMPX_WIRELESS_VID, COMPX_WIRELESS_PID):
                if d.get('usage_page') == 0xFF04:
                    path = d['path'].decode('utf-8') if isinstance(d['path'], bytes) else d['path']
                    hDev = kernel32.CreateFileW(
                        path,
                        GENERIC_READ | GENERIC_WRITE,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None,
                        OPEN_EXISTING,
                        0,
                        None
                    )
                    if hDev != -1:
                        buf = (ctypes.c_ubyte * 8)()
                        buf[0] = 6  # Report ID 6
                        success = hid_dll.HidD_GetFeature(hDev, ctypes.byref(buf), 8)
                        kernel32.CloseHandle(hDev)
                        if success:
                            data = list(buf)
                            if 0 <= data[3] <= 100:
                                charging = (data[1] == 1 or data[2] == 1)
                                return True, data[3], charging
                    break
        except Exception:
            pass
        return False, None, False

    def read_battery_hid(self):
        """
        Multi-tiered battery reader:
        Tier 1: Direct OEM hardware query on 0x089D:0x062F (Usage Page 0xFF02)
        Tier 2: CBMouse.exe memory sync (if official app is open)
        Tier 3: CompX fallback (0x3554:0xFA09)
        """
        try:
            # 1. Direct hardware query on primary AULA SC580X receiver
            connected, new_pct, charging = self._query_sc580x_hid()
            if connected:
                return connected, new_pct, charging

            # 2. Check CBMouse memory if running
            cb_ok, cb_pct, cb_charging = self._read_cbmouse_memory()
            if cb_ok:
                return True, cb_pct, cb_charging

            # 3. Secondary fallback
            fallback_ok, fb_pct, fb_charging = self._query_compx_fallback()
            if fallback_ok:
                return True, fb_pct, fb_charging

            return False, None, False

        except Exception:
            return False, None, False

    def _get_best_font(self, size):
        font_paths = [
            "C:/Windows/Fonts/seguisb.ttf",
            "seguisb.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "segoeui.ttf",
            "C:/Windows/Fonts/SegUIVar.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "arial.ttf"
        ]
        for fp in font_paths:
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def generate_crisp_icon(self, percentage, connected, is_charging=False, blink_off=False):
        """
        Generate pixel-perfect native resolution icon matching user's exact Windows tray DPI:
        - Pure Segoe UI Regular typography matching Windows Time & Date font weight (400)
        - Maximized full-tile height (13-14 physical pixels tall), completely un-squished
        - No cluttering bottom underline bar (clean, open typography matching taskbar clock)
        - Pure solid 100% white (#FFFFFF) numbers with pristine Lanczos subpixel sharpness
        - Active Windows blue (#38AFFF) typography when Type-C wired charging
        - Vibrant alert red (#FF2D2D) and blinking alert state on low battery (<= 20% or 10%)
        """
        scale = 4
        buf_size = self.tray_size * scale
        img = Image.new('RGBA', (buf_size, buf_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Check if mouse is in low battery state
        is_low = (connected and not is_charging and percentage is not None and percentage <= 20)

        # Optional bottom bar (disabled by default for pure taskbar-clock typography)
        bar_h = max(2 * scale, int(buf_size * 0.10)) if self.show_gauge_bar else 0
        bar_y1 = buf_size - bar_h if self.show_gauge_bar else buf_size
        bar_y2 = buf_size - 1

        if self.show_gauge_bar:
            draw.rectangle([0, bar_y1, buf_size - 1, bar_y2], fill=(45, 50, 58, 255))
            if connected:
                fill_pct = max(0.0, min(1.0, (percentage if percentage is not None else 100) / 100.0))
                fill_w = max(int(scale * 1.5), int((buf_size - 1) * fill_pct))
                bar_color = (255, 45, 45, 255) if is_low else (255, 255, 255, 255)
                draw.rectangle([0, bar_y1, fill_w, bar_y2], fill=bar_color)
            else:
                draw.rectangle([0, bar_y1, int(buf_size * 0.35), bar_y2], fill=(220, 65, 65, 255))

        avail_h = bar_y1 - (2 if self.show_gauge_bar else 0)
        avail_w = buf_size
        text = "?" if not connected else str(percentage)

        # Determine text color:
        # - Charging / Type-C Wired: Active vibrant Windows blue (#38AFFF)
        # - Disconnected: Muted / soft grey (#C8CDD7)
        # - Low Battery (<= 20%): Vibrant High-Contrast Alert Red (#FF2D2D)
        # - Normal Battery: 100% Solid pure white (#FFFFFF)
        if not connected:
            text_color = (200, 205, 215, 255)
        elif is_charging:
            text_color = (56, 175, 255, 255)  # Windows 11 Active Accent Blue (#38AFFF)
        elif is_low:
            text_color = (255, 45, 45, 255)   # Vibrant High-Contrast Alert Red (#FF2D2D)
        else:
            text_color = (255, 255, 255, 255)

        # Only draw typography when not in the "off" phase of a blink
        if not blink_off:
            f = self._get_best_font(56)
            bbox = f.getbbox(text)
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            g = Image.new('RGBA', (bw + 8, bh + 8), (0, 0, 0, 0))
            ImageDraw.Draw(g).text((4 - bbox[0], 4 - bbox[1]), text, font=f, fill=text_color)

            th = int(avail_h * 0.70)
            max_w = int(avail_w * 0.96) if len(text) == 3 else int(avail_w * 0.92)
            tw = min(max_w, int(th * (bw / bh)))
            stretched = g.resize((tw, th), Image.Resampling.LANCZOS)
            img.paste(stretched, ((avail_w - tw) // 2, (avail_h - th) // 2), stretched)

        # Pure Lanczos downsampling to native resolution for razor-sharp subpixel rendering
        final_icon = img.resize((self.tray_size, self.tray_size), Image.Resampling.LANCZOS)

        # Boost stroke core alpha so numbers achieve solid intensity
        # while keeping smooth subpixel anti-aliasing on the edges
        r, g_ch, b, a = final_icon.split()
        lut = []
        for i in range(256):
            if i < 35:
                lut.append(0)
            elif i < 110:
                lut.append(int((i - 35) * (180.0 / 75.0)))
            else:
                v = int(180 + (i - 110) * (75.0 / 145.0) * 1.5)
                lut.append(min(255, v))
        a_boosted = a.point(lut)
        return Image.merge('RGBA', (r, g_ch, b, a_boosted))

    def send_notification(self, title, message):
        """Send native Windows toast notification asynchronously"""
        def _show():
            try:
                from win11toast import toast
                toast(title, message)
            except Exception:
                pass
        threading.Thread(target=_show, daemon=True).start()

    def check_startup_enabled(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_READ)
            val, _ = winreg.QueryValueEx(key, STARTUP_REG_NAME)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return False

    def toggle_startup(self, icon, item):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_ALL_ACCESS)
            if self.check_startup_enabled():
                winreg.DeleteValue(key, STARTUP_REG_NAME)
            else:
                exe_path = sys.executable
                if getattr(sys, 'frozen', False):
                    cmd = f'"{sys.executable}"'
                else:
                    script_path = os.path.abspath(__file__)
                    cmd = f'"{exe_path}" "{script_path}"'
                winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)
        except Exception as e:
            print("Startup toggle error:", e)

    def _get_status_text(self):
        if not self.is_connected:
            return "AULA SC580X: Disconnected"
        if self.is_charging:
            return f"AULA SC580X: {self.battery_pct}% ⚡ (Charging / Wired)"
        if self.is_low_battery:
            return f"AULA SC580X: {self.battery_pct}% ⚠️ (Low Battery - Recharge Soon!)"
        return f"AULA SC580X: {self.battery_pct}%"

    def _update_ui(self):
        with self.ui_lock:
            if self.icon:
                try:
                    blink_off = self.blink_state if self.is_low_battery else False
                    self.icon.icon = self.generate_crisp_icon(
                        self.battery_pct,
                        self.is_connected,
                        self.is_charging,
                        blink_off=blink_off
                    )
                    self.icon.title = self._get_status_text()
                except Exception:
                    pass

    def _blink_loop(self):
        """Dedicated background thread for low battery red blinking alert"""
        while self.running:
            if self.is_low_battery:
                # Urgent 0.45s blink for critical <= 10%, steady 0.75s blink for <= 20%
                interval = 0.45 if (self.battery_pct is not None and self.battery_pct <= 10) else 0.75
                self.blink_state = not self.blink_state
                self._update_ui()
                time.sleep(interval)
            else:
                if self.blink_state:
                    self.blink_state = False
                    self._update_ui()
                time.sleep(0.5)

    def poll_loop(self):
        while self.running:
            success, new_pct, charging = self.read_battery_hid()
            if success:
                self.is_connected = True
                if new_pct is not None:
                    self.battery_pct = new_pct
                    self._save_cache()
                self.is_charging = charging

                if self.is_charging:
                    self.low_battery_warned = False
                elif self.battery_pct <= 20 and not self.low_battery_warned:
                    self.send_notification(
                        "AULA SC580X Low Battery",
                        f"Mouse battery is low ({self.battery_pct}%). Please connect the Type-C cable to recharge!"
                    )
                    self.low_battery_warned = True
                elif self.battery_pct > 30:
                    self.low_battery_warned = False
            else:
                self.is_connected = False
                self.is_charging = False

            self._update_ui()
            time.sleep(self.poll_interval)

    def manual_refresh(self, icon, item):
        success, new_pct, charging = self.read_battery_hid()
        if success:
            self.is_connected = True
            if new_pct is not None:
                self.battery_pct = new_pct
                self._save_cache()
            self.is_charging = charging
        else:
            self.is_connected = False
            self.is_charging = False
        self._update_ui()

    def toggle_gauge_bar(self, icon, item):
        self.show_gauge_bar = not self.show_gauge_bar
        self._update_ui()

    def exit_app(self, icon, item):
        self.running = False
        if self.mutex_handle:
            kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = None
        icon.stop()

    def run(self):
        # Initial read
        success, new_pct, charging = self.read_battery_hid()
        if success:
            self.is_connected = True
            if new_pct is not None:
                self.battery_pct = new_pct
                self._save_cache()
            self.is_charging = charging

        init_icon = self.generate_crisp_icon(self.battery_pct, self.is_connected, self.is_charging)

        # Context Menu
        menu = pystray.Menu(
            item(lambda text: self._get_status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Refresh Now", self.manual_refresh),
            item("Show Bottom Bar", self.toggle_gauge_bar, checked=lambda item: self.show_gauge_bar),
            item("Start with Windows", self.toggle_startup, checked=lambda item: self.check_startup_enabled()),
            pystray.Menu.SEPARATOR,
            item("Exit", self.exit_app)
        )

        self.icon = pystray.Icon("AULASc580xBattery", init_icon, self._get_status_text(), menu)

        # Notify user on startup so they know it is active and where to find it
        status_desc = f"{self.battery_pct}%" if self.is_connected else "Disconnected (?)"
        if self.is_charging:
            status_desc += " ⚡ (Charging)"
        self.send_notification(
            "AULA SC580X Monitor",
            f"Battery: {status_desc} • Active in System Tray\n(Exact 1% granularity synced)"
        )

        # Start background polling thread
        worker = threading.Thread(target=self.poll_loop, daemon=True)
        worker.start()

        # Start low-battery red blink worker thread
        blink_worker = threading.Thread(target=self._blink_loop, daemon=True)
        blink_worker.start()

        # Run system tray event loop
        self.icon.run()


if __name__ == "__main__":
    mutex = ensure_single_instance()
    if not mutex:
        sys.exit(0)

    app = MouseBatteryMonitor(mutex_handle=mutex)
    app.run()
