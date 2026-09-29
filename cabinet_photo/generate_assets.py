"""
Generates the 2XKO cabinet-photo input display assets.

Starts from a real photo of the cabinet (cabinet_source.jpg) and:
  - cuts the WHOLE joystick (ball + neck, as one rigid piece) out of the
    photo and simply rotates it around its base for E/W and diagonals,
    with a small shift for N/S.
  - for buttons (L, M, H, S1, S2, T), simply shifts the cropped button
    down a few pixels and brightens it when pressed.
  - writes chassis.png as RGBA where ONLY two screen-shaped regions are
    transparent (top minor screen + central main screen). Everything
    else -- cabinet body, background, panel -- stays fully opaque.

Screen detection logic
----------------------
For each SCREEN_REGIONS box:
  1. Grayscale, slight blur.
  2. Threshold at a FRACTION of the box's peak brightness.
  3. Morphological CLOSE (small kernel) to bridge tiny gaps.
  4. Erode by SCREEN_ERODE_KERNEL: kills thin bright features (logos,
     indicator LEDs, glints) while leaving the solid screen fill intact.
  5. Connected-components on the eroded mask. Score each blob by
        area * squareness^3,   squareness = min(w,h)/max(w,h)
     and keep the highest-scoring blob.
  6. Dilate the winner back by SCREEN_DILATE_BACK_PX (NOT the full
     erosion amount), intersect with the closed threshold mask, then
     erode by SCREEN_FINAL_ERODE_PX to pull the mask in from the soft
     glow/antialiasing around the physical screen edge.
  7. Feather by SCREEN_FEATHER_PX.

The combination of (a) a small CLOSE kernel so the interior content
doesn't merge with the surrounding white frame, (b) a small
DILATE_BACK so we don't grow back to the original (possibly
frame-touching) blob size, and (c) a nonzero FINAL_ERODE to pull in
from the glow, is what keeps the screen's white frame from being
punched out along its top edge.

Manual override (fallback)
--------------------------
If auto-detection misfires, set SCREEN_MANUAL_RECTANGLES[i] or
SCREEN_MANUAL_POLYGONS[i] for that region. Read coordinates off
assets/chassis_grid.png.

Diagnostics written on every build:
  assets/chassis_grid.png              -- coordinate reference grid
  assets/chassis_preview.png           -- detected screens shaded red
  assets/debug_region_<i>.png          -- raw crop for region i
  assets/debug_region_<i>_mask.png     -- threshold+close+erode result
  assets/debug_region_<i>_contours.png -- all candidates drawn
                                          (green = kept, yellow = winner)
"""

import os
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageFont
import numpy as np

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

HERE = os.path.dirname(__file__)
SOURCE_PATH = os.path.join(HERE, "cabinet_source.jpg")
OUT_DIR = os.path.join(HERE, "assets")
os.makedirs(OUT_DIR, exist_ok=True)

SS = 4  # supersampling factor for mask edges

# =============================================================================
# Measured positions on cabinet_source.jpg (768x1376).
# =============================================================================

CHASSIS_W, CHASSIS_H = 768, 1376

# --- Joystick -------------------------------------------------------------
LEVER_SPRITE_BOX = (135, 870, 355, 1090)   # x0,y0,x1,y1 -> 220x220
LEVER_BULB_CENTER = (229, 945)
LEVER_BULB_RADIUS = (47, 39)               # rx, ry
LEVER_CAPSULE_TOP_Y = 955
LEVER_CAPSULE_TOP_HALFWIDTH = 32
LEVER_CAPSULE_BOTTOM_Y = 1042
LEVER_CAPSULE_BOTTOM_HALFWIDTH = 22
LEVER_PIVOT = (229, 1042)

LEVER_RIGHT_EXTRA = 14
LEVER_TILT_DEG = 20
LEVER_NUDGE_PX = 11

DIR_ANGLES = {
    "N": 0, "NE": 45, "E": 90, "SE": 135,
    "S": 180, "SW": 225, "W": 270, "NW": 315,
}

# --- Buttons --------------------------------------------------------------
BUTTON_POS = {
    "L":  (418, 1032), "M":  (488, 1023), "H":  (560, 1027),
    "S1": (420, 1083), "S2": (492, 1078), "T":  (564, 1078),
}
BUTTON_SPRITE_RADIUS = 24
BUTTON_DROP_PX = 6
BUTTON_BRIGHTEN = 1.6
BUTTON_SATURATE = 1.45
BUTTON_CONTRAST = 1.12

# Real photographed button colors to override entirely (hue+saturation),
# keeping only that button's own per-pixel lightness so it still looks like
# a physically lit button rather than a flat sticker. Left empty -- OBS now
# excludes the button regions from the chroma key via a mask (see
# obs_masks/), so buttons keep their true colors instead of being recolored.
BUTTON_COLOR_OVERRIDE = {}

# --- Screen punch-outs ----------------------------------------------------
SCREEN_REGIONS = [
    (275, 220, 495, 425),    # top square white screen
    (70,  420, 705, 840),    # main white screen
]

# === Manual override (only if auto-detect still fails) =====================
SCREEN_MANUAL_RECTANGLES = {
    # 0: (x0, y0, x1, y1),
    # 1: (x0, y0, x1, y1),
}
SCREEN_MANUAL_POLYGONS = {
    # 0: [(x0, y0), (x1, y1), (x2, y2), (x3, y3)],
}

# --- Auto-detect tuning ---------------------------------------------------
SCREEN_THRESHOLD_MODE = "peak_fraction"
SCREEN_PEAK_FRACTION = 0.65
SCREEN_MANUAL_THRESHOLD = 128

SCREEN_BLUR_KERNEL = 3
# Was 7. 7 bridges the interior content to the surrounding white frame,
# which is what made the top-frame edge get punched out. 3 only fills
# really tiny holes and keeps the frame separate.
SCREEN_CLOSE_KERNEL = 3

# Kills bright features thinner than ~(kernel-1)/2 px.
SCREEN_ERODE_KERNEL = 5

# NEW: how much to dilate the winning blob back after erosion. The full
# kernel_e amount (~erosion radius) would return the blob to its pre-
# erosion size, which is exactly the size that touched the frame. A
# smaller value (2) leaves the mask comfortably inside the screen.
# Set to 0 to use the eroded blob as-is (most conservative).
SCREEN_DILATE_BACK_PX = 2

# Final inward pull so the feather is centered on the true edge, not
# on the soft glow/antialiasing around it.
SCREEN_FINAL_ERODE_PX = 2

SCREEN_MIN_AREA_FRAC = 0.02
SCREEN_MIN_FILL = 0.55
SCREEN_MIN_SQUARENESS = 0.50

SCREEN_FEATHER_PX = 1


def s(v):
    return v * SS


# ---------------------------------------------------------------------------
# 1. Lever silhouette mask.
# ---------------------------------------------------------------------------
def _build_lever_mask():
    x0, y0, x1, y1 = LEVER_SPRITE_BOX
    w, h = x1 - x0, y1 - y0

    def local(pt):
        return (pt[0] - x0, pt[1] - y0)

    mask = Image.new("L", (w * SS, h * SS), 0)
    d = ImageDraw.Draw(mask)

    bcx, bcy = local(LEVER_BULB_CENTER)
    brx, bry = LEVER_BULB_RADIUS
    RX = LEVER_RIGHT_EXTRA

    d.ellipse(
        [s(bcx - brx), s(bcy - bry), s(bcx + brx + RX), s(bcy + bry)],
        fill=255,
    )

    top_y = local((0, LEVER_CAPSULE_TOP_Y))[1]
    top_hw = LEVER_CAPSULE_TOP_HALFWIDTH
    bot_y = local((0, LEVER_CAPSULE_BOTTOM_Y))[1]
    bot_hw = LEVER_CAPSULE_BOTTOM_HALFWIDTH
    d.polygon([
        (s(bcx - top_hw), s(top_y)),
        (s(bcx + top_hw + RX), s(top_y)),
        (s(bcx + bot_hw + RX), s(bot_y)),
        (s(bcx - bot_hw), s(bot_y)),
    ], fill=255)

    d.ellipse(
        [s(bcx - bot_hw), s(bot_y - bot_hw),
         s(bcx + bot_hw + RX), s(bot_y + bot_hw)],
        fill=255,
    )

    mask = mask.filter(ImageFilter.GaussianBlur(radius=SS * 1.4))
    mask = mask.resize((w, h), Image.LANCZOS)
    return mask


# ---------------------------------------------------------------------------
# 2. Manual masks.
# ---------------------------------------------------------------------------
def _rect_mask(shape, rect):
    x0, y0, x1, y1 = rect
    mask = np.zeros(shape, dtype=np.uint8)
    H, W = shape
    xa, xb = max(0, int(round(x0))), min(W, int(round(x1)))
    ya, yb = max(0, int(round(y0))), min(H, int(round(y1)))
    if xb > xa and yb > ya:
        mask[ya:yb, xa:xb] = 255
    return mask


def _polygon_mask(shape, polygon):
    mask = np.zeros(shape, dtype=np.uint8)
    pts = np.array(polygon, dtype=np.int32).reshape(-1, 1, 2)
    cv2.fillPoly(mask, [pts], 255)
    return mask


# ---------------------------------------------------------------------------
# 3. Auto-detect.
# ---------------------------------------------------------------------------
def _detect_screen_mask_cv2(bgr, gray, box, region_idx):
    if not CV2_AVAILABLE:
        return None, {"peak": 0, "candidates": [], "winner": None}

    x0, y0, x1, y1 = box
    crop_bgr = bgr[y0:y1, x0:x1]
    crop_gray = gray[y0:y1, x0:x1]

    info = {"peak": int(crop_gray.max()) if crop_gray.size else 0,
            "threshold": None, "candidates": [], "winner": None}

    Image.fromarray(cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)).save(
        os.path.join(OUT_DIR, f"debug_region_{region_idx}.png"))

    if crop_gray.size == 0:
        return None, info

    k = max(1, SCREEN_BLUR_KERNEL | 1)
    blur = cv2.GaussianBlur(crop_gray, (k, k), 0)

    if SCREEN_THRESHOLD_MODE == "peak_fraction":
        thr_value = int(info["peak"] * SCREEN_PEAK_FRACTION)
    elif SCREEN_THRESHOLD_MODE == "otsu":
        thr_value, _ = cv2.threshold(
            blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        thr_value = int(thr_value)
    else:
        thr_value = SCREEN_MANUAL_THRESHOLD
    info["threshold"] = thr_value

    _, th = cv2.threshold(blur, thr_value, 255, cv2.THRESH_BINARY)

    # Small close kernel -- enough to fill pinholes in the screen, not
    # enough to bridge the interior content to the surrounding frame.
    ck = max(1, SCREEN_CLOSE_KERNEL | 1)
    kernel_c = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ck, ck))
    closed = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel_c)

    # Erode to kill thin bright features.
    ek = max(1, SCREEN_ERODE_KERNEL | 1)
    kernel_e = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ek, ek))
    eroded = cv2.erode(closed, kernel_e)

    Image.fromarray(eroded).save(
        os.path.join(OUT_DIR, f"debug_region_{region_idx}_mask.png"))

    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        eroded, connectivity=8)

    box_area = float(crop_gray.shape[0] * crop_gray.shape[1])
    min_area = SCREEN_MIN_AREA_FRAC * box_area

    best_score = 0.0
    best_label = -1

    vis = crop_bgr.copy()
    cnts_all, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cnts_all, -1, (80, 80, 80), 1)

    for i in range(1, n_labels):
        x, y, w, h, area = stats[i]
        info_c = {"area": int(area), "w": int(w), "h": int(h)}

        if area < min_area:
            info_c["reason"] = f"area {area} < {min_area:.0f}"
            info["candidates"].append(info_c)
            continue

        bbox_area = float(w * h) if w and h else 1.0
        fill = area / bbox_area
        info_c["fill"] = round(fill, 3)
        if fill < SCREEN_MIN_FILL:
            info_c["reason"] = f"fill {fill:.2f} < {SCREEN_MIN_FILL}"
            info["candidates"].append(info_c)
            continue

        aspect = min(w, h) / max(w, h) if max(w, h) else 0.0
        info_c["aspect"] = round(aspect, 3)
        if aspect < SCREEN_MIN_SQUARENESS:
            info_c["reason"] = f"aspect {aspect:.2f} < {SCREEN_MIN_SQUARENESS}"
            info["candidates"].append(info_c)
            continue

        score = area * (aspect ** 3)
        info_c["score"] = round(score, 1)
        info["candidates"].append(info_c)

        color = (0, 200, 0)
        comp_mask = (labels == i).astype(np.uint8) * 255
        cnts, _ = cv2.findContours(comp_mask, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(vis, cnts, -1, color, 2)

        if score > best_score:
            best_score = score
            best_label = i

    if best_label < 0:
        Image.fromarray(cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)).save(
            os.path.join(OUT_DIR, f"debug_region_{region_idx}_contours.png"))
        return None, info

    win_mask = (labels == best_label).astype(np.uint8) * 255

    # Dilate back by a SMALL amount (not the full erosion amount). The
    # full amount would return to the pre-erosion size, which is what
    # touched the frame. A small dilate keeps us comfortably inside.
    if SCREEN_DILATE_BACK_PX > 0:
        dk = max(1, (SCREEN_DILATE_BACK_PX * 2 + 1) | 1)
        kernel_d = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dk, dk))
        recovered = cv2.dilate(win_mask, kernel_d)
        final = cv2.bitwise_and(recovered, closed)
    else:
        final = win_mask

    # Final inward pull so the feather is centered on the true edge.
    if SCREEN_FINAL_ERODE_PX > 0:
        fk = max(1, (SCREEN_FINAL_ERODE_PX * 2 + 1) | 1)
        kernel_f = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (fk, fk))
        final = cv2.erode(final, kernel_f)

    info["winner"] = next(
        (c for c in info["candidates"]
         if c.get("score") is not None and c.get("area") == int(
             stats[best_label][4])),
        None,
    )

    cnts, _ = cv2.findContours(final, cv2.RETR_EXTERNAL,
                               cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cnts, -1, (0, 255, 255), 3)
    Image.fromarray(cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)).save(
        os.path.join(OUT_DIR, f"debug_region_{region_idx}_contours.png"))

    full_mask = np.zeros(gray.shape, dtype=np.uint8)
    full_mask[y0:y1, x0:x1] = final
    return full_mask, info


# ---------------------------------------------------------------------------
# 4. Coordinate grid helper.
# ---------------------------------------------------------------------------
def _write_grid_image(source, step=50):
    W, H = source.size
    img = source.copy().convert("RGB")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 11)
    except Exception:
        font = ImageFont.load_default()

    line_color = (0, 255, 255)
    text_color = (0, 0, 0)
    text_bg = (255, 255, 0)

    for x in range(0, W, step):
        d.line([(x, 0), (x, H)], fill=line_color, width=1)
        label = str(x)
        tb = d.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        d.rectangle([x + 1, 1, x + 1 + tw + 2, 1 + th + 2], fill=text_bg)
        d.text((x + 2, 1), label, fill=text_color, font=font)

    for y in range(0, H, step):
        d.line([(0, y), (W, y)], fill=line_color, width=1)
        label = str(y)
        tb = d.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        d.rectangle([1, y + 1, 1 + tw + 2, y + 1 + th + 2], fill=text_bg)
        d.text((2, y + 1), label, fill=text_color, font=font)

    path = os.path.join(OUT_DIR, "chassis_grid.png")
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# 5. Chassis build.
# ---------------------------------------------------------------------------
def build_chassis(source):
    W, H = source.size
    rgb = np.array(source.convert("RGB"))
    if CV2_AVAILABLE:
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    else:
        bgr = None
        gray = np.array(source.convert("L"))

    alpha = np.full((H, W), 255, dtype=np.uint8)

    for i, box in enumerate(SCREEN_REGIONS):
        mask = None

        if i in SCREEN_MANUAL_POLYGONS and SCREEN_MANUAL_POLYGONS[i]:
            if not CV2_AVAILABLE:
                print(f"  ! region {i}: polygon override needs OpenCV")
            else:
                mask = _polygon_mask(gray.shape, SCREEN_MANUAL_POLYGONS[i])
                print(f"  region {i}: MANUAL POLYGON "
                      f"({len(SCREEN_MANUAL_POLYGONS[i])} vertices)")

        elif i in SCREEN_MANUAL_RECTANGLES and SCREEN_MANUAL_RECTANGLES[i]:
            mask = _rect_mask(gray.shape, SCREEN_MANUAL_RECTANGLES[i])
            print(f"  region {i}: MANUAL RECTANGLE "
                  f"{SCREEN_MANUAL_RECTANGLES[i]}")

        else:
            mask, info = _detect_screen_mask_cv2(bgr, gray, box, i)
            if mask is None:
                print(f"  ! region {i} {box}: no surviving blob "
                      f"(peak={info['peak']}, threshold={info['threshold']}, "
                      f"{len(info['candidates'])} candidate(s)).")
                for ci, c in enumerate(info["candidates"][:6]):
                    print(f"      candidate {ci}: {c.get('reason', '?')}")
                print(f"      -> See debug_region_{i}_contours.png.")
                continue
            w = info["winner"] or {}
            print(f"  region {i} {box}: auto-detected "
                  f"(area={w.get('area')}, fill={w.get('fill')}, "
                  f"aspect={w.get('aspect')}, score={w.get('score')}, "
                  f"threshold={info['threshold']})")

        m_img = Image.fromarray(mask).filter(
            ImageFilter.GaussianBlur(SCREEN_FEATHER_PX)
        )
        m = np.array(m_img)
        alpha = np.minimum(alpha, 255 - m)

        pct = 100.0 * (m > 128).sum() / (W * H)
        print(f"      -> {pct:.2f}% of full image punched out")

    # Lever hole.
    local_mask = _build_lever_mask()
    full_mask = Image.new("L", (W, H), 0)
    full_mask.paste(local_mask, LEVER_SPRITE_BOX[:2])
    lever_hard = np.array(full_mask) > 30
    alpha[lever_hard] = 0

    rgba = np.dstack([rgb, alpha])
    Image.fromarray(rgba, mode="RGBA").save(
        os.path.join(OUT_DIR, "chassis.png"))

    # Preview with red tint.
    preview = rgb.copy().astype(np.float32)
    red = np.array([255, 0, 0], dtype=np.float32)
    for i, box in enumerate(SCREEN_REGIONS):
        if i in SCREEN_MANUAL_POLYGONS and SCREEN_MANUAL_POLYGONS[i]:
            m_ = _polygon_mask(gray.shape, SCREEN_MANUAL_POLYGONS[i])
        elif i in SCREEN_MANUAL_RECTANGLES and SCREEN_MANUAL_RECTANGLES[i]:
            m_ = _rect_mask(gray.shape, SCREEN_MANUAL_RECTANGLES[i])
        elif CV2_AVAILABLE:
            m_, _ = _detect_screen_mask_cv2(bgr, gray, box, i)
        else:
            m_ = None
        if m_ is None:
            continue
        where = m_ > 0
        preview[where] = 0.45 * preview[where] + 0.55 * red

    preview_path = os.path.join(OUT_DIR, "chassis_preview.png")
    Image.fromarray(preview.astype(np.uint8)).save(preview_path)
    print(f"  wrote {preview_path}          (verify the red regions)")
    grid_path = _write_grid_image(source)
    print(f"  wrote {grid_path}  (use this to read off screen coords)")


# ---------------------------------------------------------------------------
# 6. Lever sprite + 9 states.
# ---------------------------------------------------------------------------
def build_lever_states(source):
    mask = _build_lever_mask()
    sprite = source.crop(LEVER_SPRITE_BOX).convert("RGBA")
    sprite.putalpha(mask)

    x0, y0 = LEVER_SPRITE_BOX[:2]
    pivot_local = (LEVER_PIVOT[0] - x0, LEVER_PIVOT[1] - y0)

    sprite.save(os.path.join(OUT_DIR, "lever_neutral.png"))

    T, N = LEVER_TILT_DEG, LEVER_NUDGE_PX
    states = {
        "N":  (0,      0, -N),
        "S":  (0,      0, +N),
        "E":  (-T,     0,  0),
        "W":  (+T,     0,  0),
        "NE": (-T*0.8, 0, -N*0.8),
        "SE": (-T*0.8, 0, +N*0.8),
        "NW": (+T*0.8, 0, -N*0.8),
        "SW": (+T*0.8, 0, +N*0.8),
    }
    for name, (angle, dx, dy) in states.items():
        rotated = sprite.rotate(
            angle, resample=Image.BICUBIC, center=pivot_local, expand=False,
        )
        if dx or dy:
            shifted = Image.new("RGBA", rotated.size, (0, 0, 0, 0))
            shifted.paste(rotated, (int(dx), int(dy)), rotated)
            rotated = shifted
        rotated.save(os.path.join(OUT_DIR, f"lever_{name}.png"))


# ---------------------------------------------------------------------------
# 7. Button press sprites.
# ---------------------------------------------------------------------------
def _recolor_hue(rgb_img, target_hex):
    """Replace rgb_img's hue+saturation with target_hex's, keeping its own
    per-pixel lightness (V) so shading/highlights from the source photo
    survive the recolor."""
    from PIL import ImageColor
    target_h, target_s, _ = Image.new(
        "RGB", (1, 1), ImageColor.getrgb(target_hex)).convert("HSV").getpixel((0, 0))
    _, _, v = rgb_img.convert("HSV").split()
    h = Image.new("L", rgb_img.size, target_h)
    s = Image.new("L", rgb_img.size, target_s)
    return Image.merge("HSV", (h, s, v)).convert("RGB")


def build_buttons(source):
    R = BUTTON_SPRITE_RADIUS
    for bid, (cx, cy) in BUTTON_POS.items():
        box = (cx - R, cy - R, cx + R, cy + R)
        crop = source.crop(box).convert("RGBA")

        mask = Image.new("L", (2 * R * SS, 2 * R * SS), 0)
        d = ImageDraw.Draw(mask)
        d.ellipse([SS * 2, SS * 2, 2 * R * SS - SS * 2, 2 * R * SS - SS * 2],
                  fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(SS * 1.2))
        mask = mask.resize((2 * R, 2 * R), Image.LANCZOS)
        crop.putalpha(mask)

        bright = crop.convert("RGB")
        bright = ImageEnhance.Color(bright).enhance(BUTTON_SATURATE)
        bright = ImageEnhance.Brightness(bright).enhance(BUTTON_BRIGHTEN)
        bright = ImageEnhance.Contrast(bright).enhance(BUTTON_CONTRAST)
        if bid in BUTTON_COLOR_OVERRIDE:
            bright = _recolor_hue(bright, BUTTON_COLOR_OVERRIDE[bid])
        bright = bright.convert("RGBA")
        bright.putalpha(mask)

        shifted = Image.new("RGBA", bright.size, (0, 0, 0, 0))
        shifted.paste(bright, (0, BUTTON_DROP_PX), bright)

        shifted.save(os.path.join(OUT_DIR, f"button_{bid}_on.png"))


if __name__ == "__main__":
    if not os.path.isfile(SOURCE_PATH):
        raise SystemExit(f"Missing {SOURCE_PATH}.")
    src = Image.open(SOURCE_PATH).convert("RGB")
    assert src.size == (CHASSIS_W, CHASSIS_H), (
        f"cabinet_source.jpg is {src.size}, expected "
        f"({CHASSIS_W},{CHASSIS_H})."
    )
    build_chassis(src)
    build_lever_states(src)
    build_buttons(src)
    n = len(os.listdir(OUT_DIR))
    print(f"Assets written to: {OUT_DIR} ({n} files)")
