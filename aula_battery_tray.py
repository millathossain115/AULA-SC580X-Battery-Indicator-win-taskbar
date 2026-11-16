"""
AULA SC580X Battery Monitor for Windows System Tray (Native Resolution High-Luminance Edition)
Pure Solid High-Intensity White Typography with Native 1:1 Tray Pixel Alignment,
Bottom Monochromatic Level Bar, Type-C Wired Charging Detection, and Single-Instance Mutex.
Chipset: CompX (PixArt PAW3311), Wireless VID: 0x3554, PID: 0xFA09 | Wired VID: 0x088D, PID: 0x062E
"""

import os
import sys
import time
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
ERROR_ALREADY_EXISTS = 183

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
hid_dll = ctypes.windll.hid

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
WIRELESS_VID = 0x3554
WIRELESS_PID = 0xFA09
WIRED_VID = 0x088D
WIRED_PID = 0x062E


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
        self.battery_pct = 100
        self.is_connected = False
        self.is_charging = False
        self.low_battery_warned = False
        self.poll_interval = 4  # 4-second poll for instant cable reaction
        self.running = True
        self.show_gauge_bar = False
        self.icon = None

        # Determine user's exact Windows system tray icon resolution
        self.tray_size = self._get_native_tray_size()

    def _get_native_tray_size(self):
        try:
            sz = user32.GetSystemMetrics(49)  # SM_CXSMICON
            if 16 <= sz <= 64:
                return sz
        except Exception:
            pass
        return 20  # Default for 125% DPI displays

    def read_battery_hid(self):
        """Query the AULA mouse via HID Feature Report ID 6 and detect Type-C wired connection"""
        try:
            all_devices = hid.enumerate()

            # 1. Check if mouse is wired directly via Type-C cable
            is_wired = any(
                (d.get('vendor_id') == WIRED_VID and d.get('product_id') == WIRED_PID) or
                (d.get('vendor_id') == WIRELESS_VID and d.get('product_id') != WIRELESS_PID) or
                (d.get('vendor_id') in [WIRED_VID, WIRELESS_VID] and
                 'mouse' in str(d.get('product_string', '')).lower() and
                 'receiver' not in str(d.get('product_string', '')).lower())
                for d in all_devices
            )

            # 2. Query battery percentage via wireless receiver (Feature Report 6 on 0xFF04)
            pct = None
            found_wireless = False
            charging_from_report = False

            for d in all_devices:
                if d.get('vendor_id') == WIRELESS_VID and d.get('usage_page') == 0xff04:
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
                                pct = data[3]
                                found_wireless = True
                                if data[1] == 1 or data[2] == 1:
                                    charging_from_report = True
                    break

            is_connected = found_wireless or is_wired
            if not is_connected:
                return False, None, False

            # If wired cable is connected, percentage is 100% (or current read) and charging is TRUE
            if is_wired:
                if pct is None:
                    pct = self.battery_pct if self.battery_pct else 100
                return True, pct, True

            return True, pct, charging_from_report

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

    def generate_crisp_icon(self, percentage, connected, is_charging=False):
        """
        Generate pixel-perfect native resolution icon matching user's exact Windows tray DPI:
        - Pure Segoe UI Regular typography matching Windows Time & Date font weight (400)
        - Maximized full-tile height (13-14 physical pixels tall), completely un-squished
        - No cluttering bottom underline bar (clean, open typography matching taskbar clock)
        - Pure solid 100% white (#FFFFFF) numbers with pristine Lanczos subpixel sharpness
        - Sleek lightning bolt badge when Type-C wired charging
        """
        scale = 4
        buf_size = self.tray_size * scale
        img = Image.new('RGBA', (buf_size, buf_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Optional bottom bar (disabled by default for pure taskbar-clock typography)
        bar_h = max(2 * scale, int(buf_size * 0.10)) if self.show_gauge_bar else 0
        bar_y1 = buf_size - bar_h if self.show_gauge_bar else buf_size
        bar_y2 = buf_size - 1

        if self.show_gauge_bar:
            draw.rectangle([0, bar_y1, buf_size - 1, bar_y2], fill=(45, 50, 58, 255))
            if connected:
                fill_pct = max(0.0, min(1.0, (percentage if percentage is not None else 100) / 100.0))
                fill_w = max(int(scale * 1.5), int((buf_size - 1) * fill_pct))
                draw.rectangle([0, bar_y1, fill_w, bar_y2], fill=(255, 255, 255, 255))
            else:
                draw.rectangle([0, bar_y1, int(buf_size * 0.35), bar_y2], fill=(220, 65, 65, 255))

        avail_h = bar_y1 - (2 if self.show_gauge_bar else 0)
        avail_w = buf_size
        text = "?" if not connected else str(percentage)

        # Typography & Charging Badge
        if connected and is_charging:
            if len(text) == 3:  # 100% Charging
                f = self._get_best_font(56)
                bbox = f.getbbox(text)
                bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
                g = Image.new('RGBA', (bw + 8, bh + 8), (0, 0, 0, 0))
                ImageDraw.Draw(g).text((4 - bbox[0], 4 - bbox[1]), text, font=f, fill=(255, 255, 255, 255))

                th = int(avail_h * 0.70)
                tw = int(avail_w * 0.74)
                stretched = g.resize((tw, th), Image.Resampling.LANCZOS)
                img.paste(stretched, (0, (avail_h - th) // 2), stretched)

                # Sleek lightning bolt badge on right
                bx = int(avail_w * 0.84)
                by1 = int(avail_h * 0.16)
                bh_bolt = int(avail_h * 0.68)
                bolt = [
                    (bx + 3, by1),
                    (bx - 7, by1 + int(bh_bolt * 0.45)),
                    (bx + 0, by1 + int(bh_bolt * 0.45)),
                    (bx - 7, by1 + bh_bolt),
                    (bx + 11, by1 + int(bh_bolt * 0.38)),
                    (bx + 1, by1 + int(bh_bolt * 0.38)),
                ]
                draw.polygon(bolt, fill=(255, 255, 255, 255))

            else:  # 1 or 2 digits Charging (e.g. 85, 20)
                f = self._get_best_font(56)
                bbox = f.getbbox(text)
                bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
                g = Image.new('RGBA', (bw + 8, bh + 8), (0, 0, 0, 0))
                ImageDraw.Draw(g).text((4 - bbox[0], 4 - bbox[1]), text, font=f, fill=(255, 255, 255, 255))

                th = int(avail_h * 0.72)
                tw = min(int(avail_w * 0.62), int(th * (bw / bh)))
                stretched = g.resize((tw, th), Image.Resampling.LANCZOS)
                img.paste(stretched, (2, (avail_h - th) // 2), stretched)

                # Sleek lightning bolt on right
                bx = int(avail_w * 0.81)
                by1 = int(avail_h * 0.16)
                bh_bolt = int(avail_h * 0.68)
                bolt = [
                    (bx + 3, by1),
                    (bx - 7, by1 + int(bh_bolt * 0.45)),
                    (bx + 0, by1 + int(bh_bolt * 0.45)),
                    (bx - 7, by1 + bh_bolt),
                    (bx + 11, by1 + int(bh_bolt * 0.38)),
                    (bx + 1, by1 + int(bh_bolt * 0.38)),
                ]
                draw.polygon(bolt, fill=(255, 255, 255, 255))

        else:  # Normal or Disconnected
            if len(text) == 3:  # 100% normal
                f = self._get_best_font(56)
                bbox = f.getbbox(text)
                bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
                g = Image.new('RGBA', (bw + 8, bh + 8), (0, 0, 0, 0))
                ImageDraw.Draw(g).text((4 - bbox[0], 4 - bbox[1]), text, font=f, fill=(255, 255, 255, 255))

                tw = int(avail_w * 0.94)
                th = int(avail_h * 0.72)
                stretched = g.resize((tw, th), Image.Resampling.LANCZOS)
                img.paste(stretched, ((avail_w - tw) // 2, (avail_h - th) // 2), stretched)

            else:  # 1 or 2 digits or '?'
                f = self._get_best_font(56)
                col = (200, 205, 215, 255) if not connected else (255, 255, 255, 255)
                bbox = f.getbbox(text)
                bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
                g = Image.new('RGBA', (bw + 8, bh + 8), (0, 0, 0, 0))
                ImageDraw.Draw(g).text((4 - bbox[0], 4 - bbox[1]), text, font=f, fill=col)

                th = int(avail_h * 0.72)
                tw = min(int(avail_w * 0.92), int(th * (bw / bh)))
                stretched = g.resize((tw, th), Image.Resampling.LANCZOS)
                img.paste(stretched, ((avail_w - tw) // 2, (avail_h - th) // 2), stretched)

        # Pure Lanczos downsampling to native resolution for razor-sharp subpixel rendering
        final_icon = img.resize((self.tray_size, self.tray_size), Image.Resampling.LANCZOS)

        # Boost stroke core alpha so numbers achieve solid 100% white (#FFFFFF) intensity
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
        return f"AULA SC580X: {self.battery_pct}%"

    def _update_ui(self):
        if self.icon:
            self.icon.icon = self.generate_crisp_icon(self.battery_pct, self.is_connected, self.is_charging)
            self.icon.title = self._get_status_text()

    def poll_loop(self):
        while self.running:
            success, pct, charging = self.read_battery_hid()
            if success:
                self.is_connected = True
                self.battery_pct = pct
                self.is_charging = charging

                if self.is_charging:
                    self.low_battery_warned = False
                elif pct <= 20 and not self.low_battery_warned:
                    self.send_notification(
                        "AULA SC580X Low Battery",
                        f"Mouse battery is low ({pct}%). Please connect the Type-C cable to recharge!"
                    )
                    self.low_battery_warned = True
                elif pct > 30:
                    self.low_battery_warned = False
            else:
                self.is_connected = False
                self.is_charging = False

            self._update_ui()
            time.sleep(self.poll_interval)

    def manual_refresh(self, icon, item):
        success, pct, charging = self.read_battery_hid()
        if success:
            self.is_connected = True
            self.battery_pct = pct
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
        success, pct, charging = self.read_battery_hid()
        if success:
            self.is_connected = True
            self.battery_pct = pct
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
            f"Battery: {status_desc} • Active in System Tray\n(If hidden, click '^' near clock & drag icon to taskbar)"
        )

        # Start background polling thread
        worker = threading.Thread(target=self.poll_loop, daemon=True)
        worker.start()

        # Run system tray event loop
        self.icon.run()


if __name__ == "__main__":
    mutex = ensure_single_instance()
    if not mutex:
        sys.exit(0)

    app = MouseBatteryMonitor(mutex_handle=mutex)
    app.run()
