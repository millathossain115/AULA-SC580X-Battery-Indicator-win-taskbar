"""
AULA SC580X Battery Monitor for Windows System Tray (Windows Native Monochrome Edition)
High-Visibility Monochromatic Segoe UI / Arial Black Typography with Bottom Gauge Bar
and Type-C Wired Charging Detection.
Single Instance protection via Win32 Named Mutex.
Chipset: CompX (PixArt PAW3311), VID: 0x3554, PID: 0xFA09
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
hid_dll = ctypes.windll.hid

APP_NAME = "AULA SC580X Battery Tray"
MUTEX_NAME = r"Global\AULA_SC580X_Battery_Monitor_Mutex"
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "AULASc580xBatteryTray"


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
        self.poll_interval = 60  # seconds
        self.running = True
        self.icon = None

    def _get_fitted_font(self, text, max_w, max_h):
        font_candidates = [
            "C:/Windows/Fonts/ariblk.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "arialbd.ttf"
        ]
        for sz in range(115, 35, -2):
            for fp in font_candidates:
                try:
                    font = ImageFont.truetype(fp, sz)
                    bbox = font.getbbox(text)
                    w = bbox[2] - bbox[0]
                    h = bbox[3] - bbox[1]
                    if w <= max_w and h <= max_h:
                        return font, sz, bbox
                except Exception:
                    continue
        return ImageFont.load_default(), 40, (0, 0, 10, 10)

    def read_battery_hid(self):
        """Query the AULA mouse via HID Feature Report ID 6 on vendor usage page 0xFF04"""
        try:
            devices = hid.enumerate(0x3554)
            is_wired = any(
                d.get('vendor_id') == 0x3554 and d.get('product_id') != 0xfa09
                for d in devices
            )

            for d in devices:
                up = d.get('usage_page', 0)
                if up == 0xff04 or (up >= 0xff00 and d.get('usage') == 2):
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
                            pct = data[3]
                            if 0 <= pct <= 100:
                                # Mode byte: 1 = wired/charging, 2 = 2.4G wireless
                                charging = is_wired or (data[1] == 1) or (data[2] == 1)
                                return True, pct, charging
        except Exception:
            pass
        return False, None, False

    def generate_crisp_icon(self, percentage, connected, is_charging=False):
        """
        Render high-contrast monochromatic icon:
        - Large, high-visibility bold numbers on top (maximizing available tile space)
        - Sleek monochromatic battery gauge bar across the bottom
        - Distinct lightning bolt charging indicator when wired
        """
        size = 128
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 1. Monochromatic gauge bar at the bottom
        bar_x1, bar_y1, bar_x2, bar_y2 = 4, 104, 124, 124
        bar_w = bar_x2 - bar_x1

        # Track slot background (dark slate with border)
        draw.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=6, fill=(45, 48, 55, 230), outline=(80, 85, 95, 255), width=1)

        if connected:
            fill_w = max(10, int(bar_w * (percentage / 100.0)))
            # Monochromatic bright silver/white gauge bar fill
            draw.rounded_rectangle([bar_x1, bar_y1, bar_x1 + fill_w, bar_y2], radius=6, fill=(245, 248, 255, 255))
        else:
            # Subtle disconnected indicator in bar
            draw.rounded_rectangle([bar_x1, bar_y1, bar_x1 + 14, bar_y2], radius=6, fill=(160, 60, 60, 255))

        # 2. Number / Symbol on top
        text = "?" if not connected else str(percentage)

        if connected and is_charging:
            # Scaled number on left with sharp lightning bolt on right
            max_w = 88
            font, sz, bbox = self._get_fitted_font(text, max_w, 92)
            x_num = 44
            y_num = 50

            # Outline
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    if dx * dx + dy * dy <= 9:
                        draw.text((x_num + dx, y_num + dy), text, font=font, fill=(0, 0, 0, 255), anchor="mm")
            draw.text((x_num, y_num), text, font=font, fill=(255, 255, 255, 255), anchor="mm")

            # Modern lightning bolt
            bx = 108
            by = 8
            bolt_pts = [
                (bx + 9,  by + 0),
                (bx - 12, by + 40),
                (bx - 1,  by + 40),
                (bx - 14, by + 82),
                (bx + 16, by + 32),
                (bx + 2,  by + 32)
            ]
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    if dx * dx + dy * dy <= 9:
                        draw.polygon([(px + dx, py + dy) for px, py in bolt_pts], fill=(0, 0, 0, 255))
            draw.polygon(bolt_pts, fill=(255, 255, 255, 255))

        else:
            # Full width number centered
            max_w = 122
            font, sz, bbox = self._get_fitted_font(text, max_w, 92)
            x, y = size // 2, 50

            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    if dx * dx + dy * dy <= 9:
                        draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 255), anchor="mm")
            fill_col = (180, 185, 195, 255) if not connected else (255, 255, 255, 255)
            draw.text((x, y), text, font=font, fill=fill_col, anchor="mm")

        return img.resize((64, 64), Image.Resampling.LANCZOS)

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
            return f"AULA SC580X: {self.battery_pct}% ⚡ (Charging)"
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
