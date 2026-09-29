"""
2XKO Input Display -- Dual Edition (Qt / Wayland-friendly)
----------------------------------------------------------
Same app as before, but the window layer is Qt instead of Tk. Qt supports
real per-pixel window transparency on Wayland natively (Tk does not), so
this is the version that works on Hyprland + OBS with no Color Key filter.

Run:   python input_display.py
       python input_display.py --only classic
       python input_display.py --calibrate
       python input_display.py --send <ip>   /   --listen

Install:  pip install pygame pillow PyQt6
  (Arch:  sudo pacman -S python-pyqt6 qt6-wayland   also works)
  On Wayland/Hyprland you also want qt6-wayland so the app runs as a
  native Wayland client (check the "Qt platform:" line it prints).

OBS on Hyprland:
  - Add a "Screen Capture (PipeWire)" source, pick "Window" mode, choose
    the 2XKO window. (OBS sometimes labels it "Window Capture (PipeWire)".)
  - No Color Key filter needed. No Chroma Key.
  - If the alpha doesn't come through, your OBS/portal is old -- update
    OBS to 30+, xdg-desktop-portal-hyprland to latest, and try again.
"""

import atexit
import importlib.util
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False

try:
    from PyQt6.QtCore import Qt, QTimer, QPoint, QRect
    from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor
    from PyQt6.QtWidgets import QApplication, QWidget
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


# =============================================================================
# USER CONFIG
# =============================================================================

WINDOW_TITLE_CLASSIC = "2XKO Input Display - Classic"
WINDOW_TITLE_CABINET = "2XKO Input Display - Cabinet"

ALWAYS_ON_TOP = True
POLL_MS = 16

CLASSIC_POSITION = (80, 80)      # ignored on Wayland (compositor decides)
CABINET_POSITION = (1020, 80)

NETWORK_PORT = 5721

BUTTON_IDS = ["L", "M", "H", "S1", "S2", "T"]

ACTION_BUTTON_MAP = {b: None for b in BUTTON_IDS}
DIRECTION_BUTTON_MAP = {"left": None, "down": None, "right": None, "up": None}
HAT_INDEX = 0
AXIS_THRESHOLD = 0.5
AXIS_INVERT_Y = True
AXIS_BUTTON_THRESHOLD = 0.1

MAP_FILE = Path(__file__).resolve().parent / "button_map.txt"


# ---------------------------------------------------------------------------
# Mapping file
# ---------------------------------------------------------------------------

def _parse_map_file(path):
    mapping = {}
    if not path.exists():
        return mapping
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if val:
            mapping[key] = val
    return mapping


def _parse_input_spec(raw):
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("axis:"):
        parts = raw.split(":")
        idx = int(parts[1])
        threshold = float(parts[2]) if len(parts) > 2 else AXIS_BUTTON_THRESHOLD
        return ("axis", idx, threshold)
    return ("button", int(raw))


def _spec_pressed(joystick, spec):
    if spec is None or joystick is None:
        return False
    if spec[0] == "button":
        idx = spec[1]
        if idx >= joystick.get_numbuttons():
            return False
        return bool(joystick.get_button(idx))
    if spec[0] == "axis":
        idx, threshold = spec[1], spec[2]
        if idx >= joystick.get_numaxes():
            return False
        return joystick.get_axis(idx) > threshold
    return False


_loaded_map = _parse_map_file(MAP_FILE)
for _bid in ACTION_BUTTON_MAP:
    if _bid in _loaded_map:
        ACTION_BUTTON_MAP[_bid] = _parse_input_spec(_loaded_map[_bid])
for _d in DIRECTION_BUTTON_MAP:
    if _d in _loaded_map:
        DIRECTION_BUTTON_MAP[_d] = _parse_input_spec(_loaded_map[_d])
if "hat_index" in _loaded_map:
    HAT_INDEX = int(_loaded_map["hat_index"])
if "axis_threshold" in _loaded_map:
    AXIS_THRESHOLD = float(_loaded_map["axis_threshold"])
if "axis_invert_y" in _loaded_map:
    AXIS_INVERT_Y = _loaded_map["axis_invert_y"].lower() in ("1", "true", "yes", "y")

if _loaded_map:
    print(f"Loaded button mapping from {MAP_FILE.name}")
else:
    print(f"No {MAP_FILE.name} -- keyboard fallback only until you run "
          f"`python input_display.py --calibrate`.")


# Qt key codes -> our identifiers
QT_KEY_TO_ID = {
    Qt.Key.Key_Left: "left", Qt.Key.Key_Down: "down",
    Qt.Key.Key_Right: "right", Qt.Key.Key_Up: "up",
    Qt.Key.Key_U: "L", Qt.Key.Key_I: "M", Qt.Key.Key_O: "H",
    Qt.Key.Key_J: "S1", Qt.Key.Key_K: "S2", Qt.Key.Key_L: "T",
}


# ---------------------------------------------------------------------------
# Load both asset modules by path (they're both named generate_assets.py).
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent


def _load_asset_module(name, subfolder):
    path = HERE / subfolder / "generate_assets.py"
    if not path.exists():
        raise SystemExit(
            f"Missing {path} -- keep the {subfolder}/ folder next to this script."
        )
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


classic_ga = _load_asset_module("classic_generate_assets", "classic_procedural")
cabinet_ga = _load_asset_module("cabinet_generate_assets", "cabinet_photo")

CLASSIC_ASSETS_DIR = HERE / "classic_procedural" / "assets"
CABINET_ASSETS_DIR = HERE / "cabinet_photo" / "assets"


def _ensure_classic_assets():
    if CLASSIC_ASSETS_DIR.exists() and any(CLASSIC_ASSETS_DIR.glob("*.png")):
        return
    print("Classic: building assets (one-time)...")
    CLASSIC_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    classic_ga.build_chassis()
    classic_ga.build_lever_states()
    classic_ga.build_arrows()
    classic_ga.build_buttons()


def _ensure_cabinet_assets():
    if CABINET_ASSETS_DIR.exists() and any(CABINET_ASSETS_DIR.glob("*.png")):
        return
    source_path = Path(cabinet_ga.SOURCE_PATH)
    if not source_path.is_file():
        raise SystemExit(f"Missing {source_path}.")
    print("Cabinet: building assets (one-time)...")
    CABINET_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    src = Image.open(source_path).convert("RGB")
    if src.size != (cabinet_ga.CHASSIS_W, cabinet_ga.CHASSIS_H):
        raise SystemExit(f"cabinet_source.jpg is {src.size}, expected "
                         f"({cabinet_ga.CHASSIS_W},{cabinet_ga.CHASSIS_H}).")
    cabinet_ga.build_chassis(src)
    cabinet_ga.build_lever_states(src)
    cabinet_ga.build_buttons(src)


ONLY_MODE = None
if "--only" in sys.argv:
    _i = sys.argv.index("--only")
    _v = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else None
    if _v in ("classic", "cabinet"):
        ONLY_MODE = _v
    else:
        print("Usage: --only classic | --only cabinet (showing both)")


# ---------------------------------------------------------------------------
# Calibration wizard
# ---------------------------------------------------------------------------

def run_calibration():
    if not PYGAME_AVAILABLE:
        print("pygame is not installed. Run: pip install pygame pillow PyQt6")
        return
    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        print("No joystick detected.")
        return
    joy = pygame.joystick.Joystick(0)
    joy.init()
    print(f"Calibrating: {joy.get_name()}")
    print(f"({joy.get_numbuttons()} buttons, {joy.get_numhats()} hat(s), "
          f"{joy.get_numaxes()} axes)\n")

    def wait_for_new_input(timeout=12):
        prev_buttons = [joy.get_button(i) for i in range(joy.get_numbuttons())]
        baseline_axes = [joy.get_axis(i) for i in range(joy.get_numaxes())]
        start = time.time()
        while time.time() - start < timeout:
            pygame.event.pump()
            for i in range(joy.get_numbuttons()):
                cur = joy.get_button(i)
                if cur and not prev_buttons[i]:
                    return str(i)
                prev_buttons[i] = cur
            for i in range(joy.get_numaxes()):
                cur = joy.get_axis(i)
                if abs(cur - baseline_axes[i]) > 0.5:
                    return f"axis:{i}"
            time.sleep(0.01)
        return None

    mapping = {}
    print("Press the button for each label below (or wait ~12s to skip).\n")
    for name in BUTTON_IDS:
        print(f"  {name:3} -> ", end="", flush=True)
        try:
            result = wait_for_new_input()
        except KeyboardInterrupt:
            print("stopped early"); break
        if result is None:
            print("(skipped)")
        else:
            print(result if result.startswith("axis:") else f"button {result}")
            mapping[name] = result

    if joy.get_numhats() > 0:
        print("\nHat detected -- directions auto-mapped.")
    else:
        print("\nNo hat -- mapping directions as buttons:\n")
        for name in ["left", "down", "right", "up"]:
            print(f"  {name:5} -> ", end="", flush=True)
            try:
                result = wait_for_new_input()
            except KeyboardInterrupt:
                print("stopped early"); break
            if result is None:
                print("(skipped)")
            else:
                print(result if result.startswith("axis:") else f"button {result}")
                mapping[name] = result

    lines = [
        "# 2XKO Input Display -- button mapping",
        "# Generated by: python input_display.py --calibrate",
        "", "# Action buttons",
    ]
    for name in BUTTON_IDS:
        lines.append(f"{name}={mapping.get(name, '')}")
    lines += ["", "# Directions (blank = read hat automatically)"]
    for name in ["left", "down", "right", "up"]:
        lines.append(f"{name}={mapping.get(name, '')}")
    lines += ["", "# Advanced", "hat_index=0",
              "axis_threshold=0.5", "axis_invert_y=true", ""]
    MAP_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSaved mapping to {MAP_FILE}")


# ---------------------------------------------------------------------------
# Input state helpers
# ---------------------------------------------------------------------------

def flags_to_lever_state(left, down, right, up):
    if up and down: up = down = False
    if left and right: left = right = False
    if up and left: return "NW"
    if up and right: return "NE"
    if down and left: return "SW"
    if down and right: return "SE"
    if up: return "N"
    if down: return "S"
    if left: return "W"
    if right: return "E"
    return None


def compute_direction_flags(joystick, keys_down):
    flags = {"left": False, "down": False, "right": False, "up": False}
    for d in flags:
        if d in keys_down:
            flags[d] = True
    if not joystick:
        return flags
    if any(v is not None for v in DIRECTION_BUTTON_MAP.values()):
        for d, spec in DIRECTION_BUTTON_MAP.items():
            if _spec_pressed(joystick, spec):
                flags[d] = True
        return flags
    if joystick.get_numhats() > 0:
        hx, hy = joystick.get_hat(HAT_INDEX)
        flags["left"] |= hx < 0; flags["right"] |= hx > 0
        flags["up"] |= hy > 0; flags["down"] |= hy < 0
        return flags
    if joystick.get_numaxes() >= 2:
        ax = joystick.get_axis(0)
        ay = joystick.get_axis(1)
        if AXIS_INVERT_Y:
            ay = -ay
        flags["left"] |= ax < -AXIS_THRESHOLD
        flags["right"] |= ax > AXIS_THRESHOLD
        flags["up"] |= ay > AXIS_THRESHOLD
        flags["down"] |= ay < -AXIS_THRESHOLD
    return flags


def compute_button_states(joystick, keys_down):
    states = {}
    for bid in BUTTON_IDS:
        pressed = bid in keys_down
        if joystick:
            pressed = pressed or _spec_pressed(joystick, ACTION_BUTTON_MAP.get(bid))
        states[bid] = pressed
    return states


def _init_local_joystick():
    if not PYGAME_AVAILABLE:
        print("pygame not installed -- keyboard only.")
        return None
    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        print("No joystick found -- keyboard only.")
        return None
    joy = pygame.joystick.Joystick(0)
    joy.init()
    print(f"Using joystick: {joy.get_name()}")
    return joy


def run_sender(target_ip):
    joystick = _init_local_joystick()
    if joystick is None:
        print("Sender needs a real controller.")
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (target_ip, NETWORK_PORT)
    print(f"Sending to {target_ip}:{NETWORK_PORT}. Ctrl+C to stop.")
    last_payload, last_send, heartbeat = None, 0.0, 0.25
    try:
        while True:
            pygame.event.pump()
            flags = compute_direction_flags(joystick, set())
            lever = flags_to_lever_state(flags["left"], flags["down"],
                                          flags["right"], flags["up"])
            buttons = compute_button_states(joystick, set())
            payload = json.dumps({"lever": lever, "buttons": buttons}).encode()
            now = time.time()
            if payload != last_payload or now - last_send > heartbeat:
                sock.sendto(payload, target)
                last_payload, last_send = payload, now
            time.sleep(1 / 120)
    except KeyboardInterrupt:
        print("\nSender stopped.")


# =============================================================================
# Qt scene widgets.
#
# Each scene is a QWidget with WA_TranslucentBackground. On Wayland that
# gets us a genuine ARGB surface (the compositor honours per-pixel alpha),
# on Windows Qt uses a layered window (also fine), on X11 it uses an ARGB
# visual (also fine). One code path, everywhere.
#
# We cache everything as QPixmaps and repaint only on state change.
# =============================================================================

def _q_pen(rgba, width):
    return QPen(QColor(int(rgba[0]), int(rgba[1]), int(rgba[2])), width)


class _BaseScene(QWidget):
    def __init__(self, title, w, h):
        super().__init__()
        self.setWindowTitle(title)
        self.setFixedSize(w, h)
        # Real per-pixel transparency -- this is the whole point.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if ALWAYS_ON_TOP:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.key_callback = None  # set by DualApp

        # State caches. "_lever_state" is always a valid key into px_lever
        # (never None, never "__init__") -- set_state() enforces that.
        self._lever_state = "neutral"
        self._btn_state = {b: None for b in BUTTON_IDS}

    def keyPressEvent(self, ev):
        if self.key_callback:
            self.key_callback(ev, True)
        else:
            super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        if self.key_callback:
            self.key_callback(ev, False)
        else:
            super().keyReleaseEvent(ev)


class ClassicScene(_BaseScene):
    def __init__(self):
        super().__init__(WINDOW_TITLE_CLASSIC,
                         classic_ga.CHASSIS_W, classic_ga.CHASSIS_H)

        base = CLASSIC_ASSETS_DIR
        self.px_chassis = QPixmap(str(base / "chassis.png"))

        self.px_lever = {"neutral": QPixmap(str(base / "lever_neutral.png"))}
        for n in classic_ga.DIR_ANGLES:
            self.px_lever[n] = QPixmap(str(base / f"lever_{n}.png"))

        self.px_arrow = {}
        for n in classic_ga.DIR_ANGLES:
            self.px_arrow[(n, False)] = QPixmap(str(base / f"arrow_{n}_off.png"))
            self.px_arrow[(n, True)] = QPixmap(str(base / f"arrow_{n}_on.png"))

        self.px_button = {}
        for b in BUTTON_IDS:
            self.px_button[(b, False)] = QPixmap(str(base / f"button_{b}_off.png"))
            self.px_button[(b, True)] = QPixmap(str(base / f"button_{b}_on.png"))

        # Pre-render bezel rings (the only thing Tk was drawing itself).
        self.px_bezel = self._render_bezel()

        self._dir_on = {n: False for n in classic_ga.DIR_ANGLES}

    def _render_bezel(self):
        px = QPixmap(self.width(), self.height())
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(Qt.BrushStyle.NoBrush)
        lx, ly = classic_ga.LEVER_CENTER
        p.setPen(_q_pen(classic_ga.PLATE_OUTLINE, 4))
        R = int(classic_ga.LEVER_BEZEL_OUTER)
        p.drawEllipse(QPoint(int(lx), int(ly)), R, R)
        p.setPen(_q_pen(classic_ga.DETAIL_STROKE, 3))
        R = int(classic_ga.LEVER_BEZEL_MID)
        p.drawEllipse(QPoint(int(lx), int(ly)), R, R)
        p.end()
        return px

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        p.drawPixmap(0, 0, self.px_chassis)
        p.drawPixmap(0, 0, self.px_bezel)

        lx, ly = classic_ga.LEVER_CENTER
        lev = self.px_lever.get(self._lever_state, self.px_lever["neutral"])
        p.drawPixmap(int(lx - lev.width() / 2), int(ly - lev.height() / 2), lev)

        for name, deg in classic_ga.DIR_ANGLES.items():
            dx, dy = classic_ga.bearing_to_vector(deg)
            x = int(lx + dx * classic_ga.ARROW_RING_RADIUS)
            y = int(ly + dy * classic_ga.ARROW_RING_RADIUS)
            arrow = self.px_arrow[(name, bool(self._dir_on[name]))]
            p.drawPixmap(x - arrow.width() // 2,
                         y - arrow.height() // 2, arrow)

        for bid, (bx, by) in classic_ga.BUTTON_POS.items():
            btn = self.px_button[(bid, bool(self._btn_state[bid]))]
            p.drawPixmap(int(bx - btn.width() / 2),
                         int(by - btn.height() / 2), btn)

    def set_state(self, lever_state, btn_states):
        # None means neutral -- normalize so paintEvent never has to
        # special-case it and the dict lookup can't KeyError.
        lever_state = lever_state or "neutral"

        changed = False
        if lever_state != self._lever_state:
            self._lever_state = lever_state
            changed = True

        active = set()
        if lever_state != "neutral":
            active.add(lever_state)
            if lever_state in ("NE", "NW", "SE", "SW"):
                active.add(lever_state[0])
                active.add(lever_state[1])
        for n in classic_ga.DIR_ANGLES:
            on = n in active
            if self._dir_on[n] != on:
                self._dir_on[n] = on
                changed = True

        for b, on in btn_states.items():
            if self._btn_state.get(b) != on:
                self._btn_state[b] = on
                changed = True

        if changed:
            self.update()


class CabinetScene(_BaseScene):
    def __init__(self):
        super().__init__(WINDOW_TITLE_CABINET,
                         cabinet_ga.CHASSIS_W, cabinet_ga.CHASSIS_H)

        base = CABINET_ASSETS_DIR
        self.px_chassis = QPixmap(str(base / "chassis.png"))

        self.px_lever = {"neutral": QPixmap(str(base / "lever_neutral.png"))}
        for n in cabinet_ga.DIR_ANGLES:
            self.px_lever[n] = QPixmap(str(base / f"lever_{n}.png"))

        self.px_button_on = {}
        for b in BUTTON_IDS:
            self.px_button_on[b] = QPixmap(str(base / f"button_{b}_on.png"))

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # Paint the OBS chroma-key color directly into the screen cutout
        # regions before the chassis art. chassis.png already has real
        # alpha=0 holes there, but a small transparent island fully
        # enclosed by opaque art doesn't survive the Qt/Wayland/Hyprland
        # capture pipeline as a clean flat color (unlike Classic's large
        # open transparent area) -- it comes out darkened/blurred instead.
        # Drawing solid color here sidesteps that: chassis.png's opaque
        # pixels still cover it everywhere except the actual hole shape.
        key_color = QColor(f"#{BACKDROP_KEY_COLOR}")
        for region in cabinet_ga.SCREEN_REGIONS:
            x0, y0, x1, y1 = region
            p.fillRect(QRect(x0, y0, x1 - x0, y1 - y0), key_color)

        p.drawPixmap(0, 0, self.px_chassis)

        lx, ly = cabinet_ga.LEVER_SPRITE_BOX[:2]
        lev = self.px_lever.get(self._lever_state, self.px_lever["neutral"])
        p.drawPixmap(int(lx), int(ly), lev)

        R = cabinet_ga.BUTTON_SPRITE_RADIUS
        for bid, (cx, cy) in cabinet_ga.BUTTON_POS.items():
            if self._btn_state.get(bid):
                p.drawPixmap(int(cx - R), int(cy - R),
                             self.px_button_on[bid])
            # unpressed == chassis already shows it; nothing to draw

    def set_state(self, lever_state, btn_states):
        lever_state = lever_state or "neutral"

        changed = False
        if lever_state != self._lever_state:
            self._lever_state = lever_state
            changed = True
        for b, on in btn_states.items():
            if self._btn_state.get(b) != on:
                self._btn_state[b] = on
                changed = True
        if changed:
            self.update()


# =============================================================================
# noctalia pause/resume
#
# noctalia (if present) paints its wallpaper/bar/widgets on every output it
# sees, including the headless canvases these windows get pinned to (see the
# windowrule in hyprland.conf) -- which defeats real per-pixel transparency
# for OBS. noctalia has no per-monitor exclusion setting for this, so instead
# we just pause the whole thing while this app's windows are up, and bring it
# back on exit (however that happens: window close, Ctrl+C, or a crash).
# No-ops entirely on machines that don't have noctalia installed.
# =============================================================================

_HAS_NOCTALIA = shutil.which("noctalia") is not None
_noctalia_paused = False


def _pause_noctalia():
    global _noctalia_paused
    if not _HAS_NOCTALIA:
        return
    subprocess.run(["pkill", "-x", "noctalia"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _noctalia_paused = True
    print("Paused noctalia (will resume on exit).")


def _resume_noctalia():
    global _noctalia_paused
    if not _HAS_NOCTALIA or not _noctalia_paused:
        return
    _noctalia_paused = False  # set first so atexit + signal handler can't double-fire
    already = subprocess.run(["pgrep", "-x", "noctalia"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if already.returncode == 0:
        return
    subprocess.Popen(["noctalia"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, start_new_session=True)
    print("Resumed noctalia.")


def _resume_noctalia_and_exit(signum, _frame):
    _resume_noctalia()
    sys.exit(0)


# =============================================================================
# OBS transparency backdrop
#
# Hyprland's Monitor/Output-mode screencopy (used for the headless-canvas OBS
# capture -- see windowrule in hyprland.conf and the monitor lines in
# monitors.conf) captures the FINAL COMPOSITED frame, which resolves alpha
# blending into opaque RGB. So even with no wallpaper painting the headless
# canvases, "empty" areas still come through opaque, not transparent -- real
# per-pixel alpha only survives Window-mode capture, which is the mode that
# freezes under Flatpak OBS's older bundled PipeWire. Workaround: paint the
# headless canvases a solid, artwork-safe color and key it out in OBS with a
# Color Key filter (pure green checked clean against every generated asset
# pixel -- nothing in the art comes within ~140 color-distance units of it).
# Real monitors keep the normal wallpaper. No-ops without swaybg/hyprctl, or
# if the headless canvases haven't been created yet (see autostart.conf).
# =============================================================================

_HAS_SWAYBG = shutil.which("swaybg") is not None and shutil.which("hyprctl") is not None
# Picked by scanning every generated asset's significant (non-antialiasing)
# colors and maximizing hue distance from all of them -- particularly the
# "T" button's olive/yellow-green (~66deg), which pure #00FF00 (120deg) sat
# too close to once Color Key's Similarity/Smoothness were pushed up to
# clean up blur/render fringing. #00FF33 (~132deg) has the widest margin.
BACKDROP_KEY_COLOR = "00FF33"  # match this in OBS's Color Key filter
_BACKGROUND_LINK = str(Path.home() / ".config/omarchy/current/background")


def _setup_backdrop():
    if not _HAS_SWAYBG:
        return
    result = subprocess.run(["hyprctl", "monitors", "-j"],
                             capture_output=True, text=True)
    try:
        monitors = json.loads(result.stdout)
    except (ValueError, TypeError):
        return
    real = [m["name"] for m in monitors if not m["name"].startswith("HEADLESS-")]
    headless = [m["name"] for m in monitors if m["name"].startswith("HEADLESS-")]
    if not headless:
        return  # headless canvases not set up -- nothing to do

    subprocess.run(["pkill", "-x", "swaybg"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd = ["swaybg"]
    for mon in real:
        cmd += ["-o", mon, "-i", _BACKGROUND_LINK, "-m", "fill"]
    for mon in headless:
        cmd += ["-o", mon, "-c", BACKDROP_KEY_COLOR, "-m", "solid_color"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      start_new_session=True)
    print(f"Headless canvases set to solid #{BACKDROP_KEY_COLOR} -- "
          f"add an OBS Color Key filter targeting that color for transparency.")


# =============================================================================
# Main app
# =============================================================================

class DualApp:
    def __init__(self):
        if not QT_AVAILABLE:
            raise SystemExit(
                "PyQt6 is required for the Wayland-friendly window layer.\n"
                "Run:  pip install PyQt6\n"
                "Arch: sudo pacman -S python-pyqt6 qt6-wayland"
            )
        if not PIL_AVAILABLE:
            raise SystemExit("Pillow required: pip install pillow")

        _pause_noctalia()
        atexit.register(_resume_noctalia)
        signal.signal(signal.SIGINT, _resume_noctalia_and_exit)
        signal.signal(signal.SIGTERM, _resume_noctalia_and_exit)
        _setup_backdrop()

        # Ignore any inherited Qt style override from the desktop session --
        # the app is frameless and fully self-drawn, so the style is
        # irrelevant and a bad override (e.g. "kvantum" when kvantum-qt6
        # isn't installed) just prints warnings on startup.
        os.environ.pop("QT_STYLE_OVERRIDE", None)

        show_classic = ONLY_MODE in (None, "classic")
        show_cabinet = ONLY_MODE in (None, "cabinet")

        if show_classic:
            _ensure_classic_assets()
        if show_cabinet:
            _ensure_cabinet_assets()

        self.app = QApplication(sys.argv)
        self.app.setApplicationName("2XKO Input Display")

        self.classic = None
        self.cabinet = None

        if show_classic:
            self.classic = ClassicScene()
            # move() is a no-op on Wayland; harmless elsewhere.
            self.classic.move(*CLASSIC_POSITION)
            self.classic.key_callback = self._on_key
            self.classic.show()
        if show_cabinet:
            self.cabinet = CabinetScene()
            self.cabinet.move(*CABINET_POSITION)
            self.cabinet.key_callback = self._on_key
            self.cabinet.show()

        # Environment info
        plat = self.app.platformName()  # "wayland", "xcb", "windows", ...
        print(f"Qt platform: {plat}")
        if plat.startswith("wayland"):
            print("  -> Real per-pixel transparency enabled by the "
                  "compositor. In OBS use 'Screen Capture (PipeWire)' -> "
                  "Window mode, pick the 2XKO window, no filter.")
        elif plat.startswith("xcb"):
            print("  -> X11/XWayland session. Transparency uses an ARGB "
                  "visual -- works with OBS's PipeWire window capture on "
                  "Hyprland, or with Window Capture (XComposite) on X11.")
        elif plat.startswith("windows"):
            print("  -> Windows: layered window transparency, no filter "
                  "in OBS.")

        # Keyboard state
        self.keys_down = set()

        # Network vs local
        self.network_mode = "--listen" in sys.argv
        self.joystick = None
        if self.network_mode:
            self._lock = threading.Lock()
            self._net_state = {"lever": None, "buttons": {}}
            self._net_time = None
            self._start_listener()
            print(f"Listening on UDP {NETWORK_PORT} ...")
        else:
            self.joystick = _init_local_joystick()

        # Poll loop
        self.timer = QTimer()
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_MS)

    def _on_key(self, ev, pressed):
        ident = QT_KEY_TO_ID.get(ev.key())
        if ident is None:
            return
        if pressed:
            self.keys_down.add(ident)
        else:
            self.keys_down.discard(ident)

    def _start_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", NETWORK_PORT))
        sock.settimeout(1.0)

        def listen():
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    state = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                with self._lock:
                    self._net_state = state
                    self._net_time = time.time()

        threading.Thread(target=listen, daemon=True).start()

    def _net_snapshot(self):
        with self._lock:
            state = dict(self._net_state)
            last = self._net_time
        if last is None or time.time() - last > 1.5:
            return None, {}
        return state.get("lever"), state.get("buttons", {})

    def _poll(self):
        if self.network_mode:
            net_lever, net_btns = self._net_snapshot()
            kb = compute_direction_flags(None, self.keys_down)
            kb_lever = flags_to_lever_state(kb["left"], kb["down"],
                                             kb["right"], kb["up"])
            lever = kb_lever or net_lever
            btns = {b: (b in self.keys_down) or net_btns.get(b, False)
                    for b in BUTTON_IDS}
        else:
            if self.joystick:
                pygame.event.pump()
            flags = compute_direction_flags(self.joystick, self.keys_down)
            lever = flags_to_lever_state(flags["left"], flags["down"],
                                          flags["right"], flags["up"])
            btns = compute_button_states(self.joystick, self.keys_down)

        if self.classic is not None:
            self.classic.set_state(lever, btns)
        if self.cabinet is not None:
            self.cabinet.set_state(lever, btns)

    def run(self):
        sys.exit(self.app.exec())


# =============================================================================

if __name__ == "__main__":
    if "--calibrate" in sys.argv:
        run_calibration()
    elif "--send" in sys.argv:
        i = sys.argv.index("--send")
        target = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if not target:
            print("Usage: python input_display.py --send <receiver-ip>")
        else:
            run_sender(target)
    else:
        DualApp().run()
