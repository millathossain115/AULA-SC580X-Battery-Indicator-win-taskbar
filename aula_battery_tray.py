"""
AULA SC580X Battery Monitor for Windows System Tray (Windows Native Edition)
Clean, Sharp, Natural-Weight Segoe UI Typography with Bottom Gauge Bar,
Type-C Wired Charging Detection, and Single-Instance Protection.
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
hid_dll = ctypes.windll.hid

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

    def _get_fitted_font(self, text, max_w, max_h):
        """Fit clean, natural-weight Segoe UI typography to maximize size without chunkiness"""
        font_candidates = [
            "C:/Windows/Fonts/segoeui.ttf",
            "segoeui.ttf",
            "arial.ttf"
        ]
        for sz in range(130, 35, -1):
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
        Render clean, high-visibility monochromatic icon:
        - Large, clean-weight Segoe UI numbers on top (sharp, natural proportions)
        - Sleek monochromatic battery gauge bar across the bottom
        - Crisp lightning bolt charging indicator when wired
        """
        size = 128
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 1. Monochromatic gauge bar at the bottom
        bar_x1, bar_y1, bar_x2, bar_y2 = 4, 112, 124, 124
        bar_w = bar_x2 - bar_x1

        # Track background
        draw.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=4, fill=(45, 48, 55, 230), outline=(75, 80, 90, 255), width=1)

        if connected:
            fill_w = max(8, int(bar_w * (percentage / 100.0)))
            # Clean silver/white gauge fill
            draw.rounded_rectangle([bar_x1, bar_y1, bar_x1 + fill_w, bar_y2], radius=4, fill=(245, 248, 255, 255))
        else:
            # Disconnected indicator in bar
            draw.rounded_rectangle([bar_x1, bar_y1, bar_x1 + 14, bar_y2], radius=4, fill=(160, 60, 60, 255))

        # 2. Number on top
        text = "?" if not connected else str(percentage)

        if connected and is_charging:
            # Scaled number on left with sharp lightning bolt on right
            max_w = 88
            font, sz, b = self._get_fitted_font(text, max_w, 100)
            x_num = 44
            y_num = 52

            # Clean 1px outline for razor-sharp edge contrast
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx != 0 or dy != 0:
                        draw.text((x_num + dx, y_num + dy), text, font=font, fill=(0, 0, 0, 230), anchor="mm")
            draw.text((x_num, y_num), text, font=font, fill=(255, 255, 255, 255), anchor="mm")

            # Sleek lightning bolt
            bx = 106
            by = 14
            bolt_pts = [
                (bx + 8,  by + 0),
                (bx - 10, by + 36),
                (bx - 1,  by + 36),
                (bx - 12, by + 74),
                (bx + 14, by + 28),
                (bx + 2,  by + 28)
            ]
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx != 0 or dy != 0:
                        draw.polygon([(px + dx, py + dy) for px, py in bolt_pts], fill=(0, 0, 0, 230))
            draw.polygon(bolt_pts, fill=(255, 255, 255, 255))

        else:
            # Clean large number centered
            max_w = 124
            font, sz, b = self._get_fitted_font(text, max_w, 102)
            x, y = size // 2, 52

            # Clean 1px outline
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx != 0 or dy != 0:
                        draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 230), anchor="mm")
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
