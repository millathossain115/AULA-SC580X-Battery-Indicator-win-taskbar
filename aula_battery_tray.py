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
import winreg
import ctypes
from ctypes import wintypes
import hid
from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as item

# Win32 Constants
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
ERROR_ALREADY_EXISTS = 183

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
hid_dll = ctypes.windll.hid

# Enable High-DPI Awareness so Windows gives true pixel coordinates
try:
    user32.SetProcessDPIAware()
except Exception:
    pass

APP_NAME = "AULA SC580X Battery Tray"
MUTEX_NAME = r"Global\AULA_SC580X_Battery_Monitor_Mutex"
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "AULASc580xBatteryTray"

# Hardware IDs for AULA SC580X
WIRELESS_VID = 0x3554
WIRELESS_PID = 0xFA09
WIRED_VID = 0x088D
WIRED_PID = 0x062E


def ensure_single_instance():
    """Ensure only one instance runs at a time using a Win32 Named Mutex."""
    mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    last_error = kernel32.GetLastError()
    if last_error == ERROR_ALREADY_EXISTS:
        try:
            from win11toast import toast
            toast(
                "AULA SC580X Monitor",
                "Application is already running in your system tray! Check near the taskbar clock (or ^ overflow menu)."
            )
        except Exception:
            pass
        return None
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

    def generate_crisp_icon(self, percentage, connected, is_charging=False):
        """
        Generate pixel-perfect native resolution icon matching user's exact Windows tray DPI:
        - Pure solid 100% intense white (#FFFFFF) with zero color bleeding/fading
        - Maximized Segoe UI Bold typography filling the space above the gauge bar
        - Monochromatic gauge bar at the bottom
        - Crisp lightning bolt when charging
        """
        # Render on 2x retina buffer for clean integer downsampling
        buf_size = self.tray_size * 2
        img = Image.new('RGBA', (buf_size, buf_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 1. Monochromatic slim gauge bar at bottom
        bar_h = max(3, int(buf_size * 0.10))
        bar_y1 = buf_size - bar_h - 2
        bar_y2 = buf_size - 2
        draw.rounded_rectangle([1, bar_y1, buf_size - 2, bar_y2], radius=1, fill=(40, 44, 52, 230))

        if connected:
            fill_w = max(3, int((buf_size - 3) * (percentage / 100.0)))
            # Pure solid high-intensity white
            draw.rounded_rectangle([1, bar_y1, 1 + fill_w, bar_y2], radius=1, fill=(255, 255, 255, 255))
        else:
            draw.rounded_rectangle([1, bar_y1, 6, bar_y2], radius=1, fill=(180, 60, 60, 255))

        # 2. Number on top
        text = "?" if not connected else str(percentage)
        font_path = "C:/Windows/Fonts/segoeuib.ttf"

        if connected and is_charging:
            # Scaled number on left with sharp lightning bolt on right
            fsize = int(buf_size * 0.50) if len(text) == 3 else int(buf_size * 0.70)
            try:
                font = ImageFont.truetype(font_path, fsize)
            except Exception:
                font = ImageFont.load_default()

            x_num = int(buf_size * 0.35)
            y_num = (bar_y1) // 2

            # Pure 100% solid white
            draw.text((x_num, y_num), text, font=font, fill=(255, 255, 255, 255), anchor="mm")

            # Lightning bolt (sharp solid white)
            bx = int(buf_size * 0.80)
            by = 4
            bolt_h = int(buf_size * 0.58)
            bolt_pts = [
                (bx + 3,  by + 0),
                (bx - 4,  by + int(bolt_h * 0.44)),
                (bx + 0,  by + int(bolt_h * 0.44)),
                (bx - 4,  by + bolt_h),
                (bx + 6,  by + int(bolt_h * 0.36)),
                (bx + 1,  by + int(bolt_h * 0.36))
            ]
            draw.polygon(bolt_pts, fill=(255, 255, 255, 255))

        else:
            # Full width number centered with maximum scale
            fsize = int(buf_size * 0.58) if len(text) == 3 else int(buf_size * 0.78)
            try:
                font = ImageFont.truetype(font_path, fsize)
            except Exception:
                font = ImageFont.load_default()

            x = buf_size // 2
            y = (bar_y1) // 2

            fill_col = (180, 185, 195, 255) if not connected else (255, 255, 255, 255)
            draw.text((x, y), text, font=font, fill=fill_col, anchor="mm")

        # Downsample cleanly to user's exact native tray icon size
        return img.resize((self.tray_size, self.tray_size), Image.Resampling.LANCZOS)

    def send_notification(self, title, message):
        """Send native Windows toast notification"""
        try:
            from win11toast import toast
            toast(title, message)
        except Exception:
            pass

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
            item("Start with Windows", self.toggle_startup, checked=lambda item: self.check_startup_enabled()),
            pystray.Menu.SEPARATOR,
            item("Exit", self.exit_app)
        )

        self.icon = pystray.Icon("AULASc580xBattery", init_icon, self._get_status_text(), menu)

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
