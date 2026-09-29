"""
Generates the 2XKO arcade-stick art assets as high-resolution PNGs.

Run this once (already run for you in this project) to produce everything
under assets/. It is only needed again if you want to change the design.

Output (assets/):
  chassis.png                 -- static stick body, screws, button/lever sockets
  lever_neutral.png           -- ball-top lever, 9 total: neutral + 8 directions
  lever_N.png / _NE / _E / _SE / _S / _SW / _W / _NW
  arrow_<DIR>_off.png / arrow_<DIR>_on.png   -- 8 dirs x 2 states = 16 files
  button_<ID>_off.png / button_<ID>_on.png   -- 8 buttons x 2 states = 16 files

Everything is drawn at 4x supersampling then downsampled for clean
anti-aliased edges, so these are crisp even if you scale the window up.
"""

import math
import os
import subprocess
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.join(os.path.dirname(__file__), "assets")
os.makedirs(OUT_DIR, exist_ok=True)

SS = 4  # supersampling factor

FONT_PATH = None  # auto-detected at runtime, see find_font() below

# ---- Palette -----------------------------------------------------------
PLATE_FILL     = (26, 26, 29, 255)
PLATE_OUTLINE  = (118, 120, 128, 255)
DETAIL_STROKE  = (90, 92, 100, 255)
SOCKET_OUTLINE = (135, 137, 145, 255)

BUTTON_COLORS = {
    "L":  "#c9a6f5",
    "M":  "#8e3ff0",
    "H":  "#5b21b6",
    "S1": "#29b6f6",
    "S2": "#e53935",
    "T":  "#cddc39",
}
DIM_FACTOR = 0.30

ARROW_OFF = (150, 152, 158, 255)
ARROW_ON = (255, 255, 255, 255)
ARROW_OUTLINE = (10, 10, 12, 220)

# World-space (final resolution) layout -----------------------------------
CHASSIS_W, CHASSIS_H = 900, 560
LEVER_CENTER = (275, 275)
LEVER_BEZEL_OUTER = 66
LEVER_BEZEL_MID = 54

BUTTON_POS = {
    "L":  (560, 235), "M": (635, 235), "H": (710, 235),
    "S1": (505, 315), "S2": (580, 315), "T": (655, 315),
}
BUTTON_OUTER_R = 36
BUTTON_INNER_R = 27

ARROW_RING_RADIUS = 78
# Compass bearing in degrees: 0 = North (up), clockwise. With dx = sin(theta),
# dy = -cos(theta) in screen space (x right, y down), theta=0 -> (0,-1) up,
# theta=90 -> (1,0) right, theta=180 -> (0,1) down, theta=270 -> (-1,0) left.
DIR_ANGLES = {
    "N": 0, "NE": 45, "E": 90, "SE": 135,
    "S": 180, "SW": 225, "W": 270, "NW": 315,
}


def bearing_to_vector(deg):
    rad = math.radians(deg)
    return math.sin(rad), -math.cos(rad)


def luminance(rgb):
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def contrasting_text_color(bg_rgba):
    return (20, 20, 20, 255) if luminance(bg_rgba) > 150 else (245, 245, 245, 255)


def hex_to_rgba(h, a=255):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, a)


def darken_rgba(rgba, factor):
    r, g, b, a = rgba
    return (int(r * factor), int(g * factor), int(b * factor), a)


def canvas(w, h):
    img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def finalize(img, w, h, path):
    img = img.resize((w, h), Image.LANCZOS)
    img.save(path)
    return path


def s(v):
    """scale a coordinate/length into supersampled space"""
    return v * SS


def font(size):
    path = find_font()
    if path is None:
        try:
            return ImageFont.load_default(size=s(size))
        except TypeError:
            # older Pillow: load_default() has no size parameter
            return ImageFont.load_default()
    return ImageFont.truetype(path, s(size))


_FONT_CACHE = {}


def _fc_match_bold_font():
    """
    Ask fontconfig for a bold sans font -- near-universal on Linux desktop
    systems (Arch, SteamOS/Steam Deck included), so this catches cases
    the hardcoded path list below misses (distros vary in exactly where
    they install DejaVu/Liberation).
    """
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "sans-serif:bold"],
            capture_output=True, text=True, timeout=3,
        )
        path = result.stdout.strip()
        if path and os.path.isfile(path):
            return path
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return None


def find_font():
    """
    Locate a bold TrueType font that works cross-platform, since
    Pillow doesn't ship one. Tries common Windows/macOS/Linux names
    first (bare names resolve via each OS's own font directory), then
    asks fontconfig on Linux, then falls back to Pillow's built-in font
    with a warning.
    """
    if "path" in _FONT_CACHE:
        return _FONT_CACHE["path"]

    candidates = [
        FONT_PATH,  # manual override, if you set one above
        "arialbd.ttf",                     # Windows: Arial Bold
        "segoeuib.ttf",                    # Windows: Segoe UI Bold
        "seguisb.ttf",                     # Windows: Segoe UI Semibold
        "/Library/Fonts/Arial Bold.ttf",   # macOS
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",       # Debian/Ubuntu
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",  # Debian/Ubuntu
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",            # Arch (ttf-dejavu)
        "/usr/share/fonts/TTF/LiberationSans-Bold.ttf",        # Arch (ttf-liberation)
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",  # some distros
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            ImageFont.truetype(candidate, 10)
            _FONT_CACHE["path"] = candidate
            return candidate
        except OSError:
            continue

    fc_path = _fc_match_bold_font()
    if fc_path:
        try:
            ImageFont.truetype(fc_path, 10)
            _FONT_CACHE["path"] = fc_path
            return fc_path
        except OSError:
            pass

    print("Warning: no TrueType font found on this system -- button "
          "labels will use Pillow's built-in bitmap font (smaller/"
          "blurrier at this resolution). To fix: set FONT_PATH near the "
          "top of this file to the full path of any .ttf font you have "
          "(e.g. C:\\Windows\\Fonts\\arialbd.ttf).")
    _FONT_CACHE["path"] = None
    return None


# ---------------------------------------------------------------------------
# 1. CHASSIS
# ---------------------------------------------------------------------------
def build_chassis():
    img, d = canvas(CHASSIS_W, CHASSIS_H)

    d.rounded_rectangle(
        [s(30), s(30), s(870), s(530)], radius=s(46),
        fill=PLATE_FILL, outline=PLATE_OUTLINE, width=s(5),
    )

    # decorative seam near the top-left
    d.line(
        [(s(80), s(70)), (s(330), s(70)), (s(430), s(120))],
        fill=DETAIL_STROKE, width=s(3), joint="curve",
    )

    # screws
    for cx, cy in [(95, 68), (125, 68)]:
        d.ellipse([s(cx - 6), s(cy - 6), s(cx + 6), s(cy + 6)],
                   outline=PLATE_OUTLINE, width=s(3))

    # utility 3-dot cluster
    for cx in (215, 245, 275):
        d.ellipse([s(cx - 10), s(90 - 10), s(cx + 10), s(90 + 10)],
                   outline=PLATE_OUTLINE, width=s(3))

    # top-right accents
    for cx, cy in [(690, 90), (715, 90)]:
        d.ellipse([s(cx - 6), s(cy - 6), s(cx + 6), s(cy + 6)],
                   outline=PLATE_OUTLINE, width=s(3))
    jx, jy = 790, 120
    d.ellipse([s(jx - 26), s(jy - 26), s(jx + 26), s(jy + 26)],
               outline=PLATE_OUTLINE, width=s(3))
    d.ellipse([s(jx - 14), s(jy - 14), s(jx + 14), s(jy + 14)],
               outline=PLATE_OUTLINE, width=s(3))

    # NOTE: the lever bezel rings are intentionally NOT drawn here. They're
    # drawn live in input_display.py instead, from the same LEVER_CENTER
    # this file exports -- so the ring and the ball can never drift apart,
    # even without regenerating this PNG. See input_display.py's
    # _build_scene() for where they're drawn.

    # button sockets
    for bx, by in BUTTON_POS.values():
        d.ellipse([s(bx - BUTTON_OUTER_R), s(by - BUTTON_OUTER_R),
                   s(bx + BUTTON_OUTER_R), s(by + BUTTON_OUTER_R)],
                  outline=SOCKET_OUTLINE, width=s(3))
        d.ellipse([s(bx - BUTTON_INNER_R), s(by - BUTTON_INNER_R),
                   s(bx + BUTTON_INNER_R), s(by + BUTTON_INNER_R)],
                  outline=DETAIL_STROKE, width=s(2))

    return finalize(img, CHASSIS_W, CHASSIS_H, os.path.join(OUT_DIR, "chassis.png"))


# ---------------------------------------------------------------------------
# 2. LEVER (9 states: neutral + 8 directions)
# ---------------------------------------------------------------------------
def build_lever_states():
    sprite_w = sprite_h = 260
    center = (sprite_w // 2, sprite_h // 2)
    travel = 24
    ball_r = 24

    def draw_lever(offset_xy, path):
        img, d = canvas(sprite_w, sprite_h)
        cx, cy = center
        ox, oy = offset_xy
        bx, by = cx + ox, cy + oy

        if ox or oy:
            d.line([s(cx), s(cy), s(bx), s(by)],
                   fill=(85, 87, 92, 255), width=s(10))

        # ball shadow
        d.ellipse([s(bx - ball_r + 3), s(by - ball_r + 5),
                   s(bx + ball_r + 3), s(by + ball_r + 5)],
                  fill=(0, 0, 0, 90))
        # ball body
        d.ellipse([s(bx - ball_r), s(by - ball_r),
                   s(bx + ball_r), s(by + ball_r)],
                  fill=(207, 207, 212, 255), outline=(120, 122, 128, 255),
                  width=s(3))
        # glossy highlight
        d.ellipse([s(bx - ball_r * 0.5), s(by - ball_r * 0.7),
                   s(bx - ball_r * 0.05), s(by - ball_r * 0.25)],
                  fill=(255, 255, 255, 130))
        finalize(img, sprite_w, sprite_h, path)

    draw_lever((0, 0), os.path.join(OUT_DIR, "lever_neutral.png"))
    for name, deg in DIR_ANGLES.items():
        dx, dy = bearing_to_vector(deg)
        draw_lever((dx * travel, dy * travel),
                   os.path.join(OUT_DIR, f"lever_{name}.png"))


# ---------------------------------------------------------------------------
# 3. ARROW RING (8 directions x off/on) -- bare chevrons, no circle badge
# ---------------------------------------------------------------------------
def build_arrows():
    sprite_w = sprite_h = 40
    center = (sprite_w // 2, sprite_h // 2)
    shrink = 0.7  # ~30% smaller than the original chevron

    def triangle_points(angle_deg):
        # notched arrowhead/chevron template, tip pointing "up" (screen -y).
        # the inward notch at the back makes the pointing direction
        # unambiguous at any rotation, unlike a plain triangle.
        base = [(0, -18), (12, 7), (0, -2), (-12, 7)]
        template = [(px * shrink, py * shrink) for px, py in base]
        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        pts = []
        for px, py in template:
            rx = px * cos_a - py * sin_a
            ry = px * sin_a + py * cos_a
            pts.append((center[0] + rx, center[1] + ry))
        return pts

    def draw_arrow(name, deg, on):
        img, d = canvas(sprite_w, sprite_h)
        pts = [(s(x), s(y)) for x, y in triangle_points(deg)]
        fill = ARROW_ON if on else ARROW_OFF
        d.polygon(pts, fill=fill, outline=ARROW_OUTLINE,
                  width=s(2) if on else s(1))
        suffix = "on" if on else "off"
        finalize(img, sprite_w, sprite_h,
                 os.path.join(OUT_DIR, f"arrow_{name}_{suffix}.png"))

    for name, deg in DIR_ANGLES.items():
        draw_arrow(name, deg, on=False)
        draw_arrow(name, deg, on=True)


# ---------------------------------------------------------------------------
# 4. BUTTON BADGES (8 buttons x off/on)
# ---------------------------------------------------------------------------
def build_buttons():
    sprite_w = sprite_h = 100
    center = (sprite_w // 2, sprite_h // 2)
    r = 34
    f = font(30)

    for bid, hexcol in BUTTON_COLORS.items():
        bright = hex_to_rgba(hexcol)
        dim = darken_rgba(bright, DIM_FACTOR)
        for on, color, suffix in ((False, dim, "off"), (True, bright, "on")):
            img, d = canvas(sprite_w, sprite_h)
            cx, cy = center
            d.ellipse([s(cx - r), s(cy - r), s(cx + r), s(cy + r)],
                      fill=color,
                      outline=(255, 255, 255, 255) if on else (0, 0, 0, 0),
                      width=s(2))
            text_color = contrasting_text_color(color)
            bbox = d.textbbox((0, 0), bid, font=f)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            d.text((s(cx) - tw / 2 - bbox[0], s(cy) - th / 2 - bbox[1]),
                   bid, font=f, fill=text_color)
            finalize(img, sprite_w, sprite_h,
                     os.path.join(OUT_DIR, f"button_{bid}_{suffix}.png"))


if __name__ == "__main__":
    build_chassis()
    build_lever_states()
    build_arrows()
    build_buttons()
    print(f"Assets written to: {OUT_DIR}")
    print(len(os.listdir(OUT_DIR)), "files")
