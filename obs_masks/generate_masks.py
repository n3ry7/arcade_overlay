"""
Generates OBS Image Mask/Blend mask PNGs for the two joystick windows'
button clusters -- white = button region (visible), black = everything
else (hidden). Used to keep buttons on their true colors, excluded from
the OBS Color Key chroma-key filter applied for transparency.

See README.md's "Excluding buttons from the chroma key" section for how
these get wired up in OBS.

Run: python generate_masks.py   (re-run any time button positions/radii
change in either classic_procedural/generate_assets.py or
cabinet_photo/generate_assets.py)
"""

import importlib.util
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

FEATHER_PX = 3   # soft edge so the mask doesn't cut a hard ring into the AA'd button art
PADDING_PX = 6   # extra radius beyond the button's own visible edge


def _load(name, subfolder):
    path = ROOT / subfolder / "generate_assets.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_mask(w, h, button_pos, radius, out_path):
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    r = radius + PADDING_PX
    for cx, cy in button_pos.values():
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(FEATHER_PX))
    mask.save(out_path)
    print(f"wrote {out_path}  ({w}x{h}, {len(button_pos)} buttons, radius {r}px)")


if __name__ == "__main__":
    classic_ga = _load("classic_ga", "classic_procedural")
    _build_mask(
        classic_ga.CHASSIS_W, classic_ga.CHASSIS_H,
        classic_ga.BUTTON_POS, classic_ga.BUTTON_OUTER_R,
        HERE / "classic_buttons_mask.png",
    )

    cabinet_ga = _load("cabinet_ga", "cabinet_photo")
    # Cabinet's on-state sprite shifts down BUTTON_DROP_PX when pressed --
    # nudge the mask center down half that so it comfortably covers both
    # the resting and pressed positions.
    shifted_pos = {
        b: (x, y + cabinet_ga.BUTTON_DROP_PX / 2)
        for b, (x, y) in cabinet_ga.BUTTON_POS.items()
    }
    _build_mask(
        cabinet_ga.CHASSIS_W, cabinet_ga.CHASSIS_H,
        shifted_pos, cabinet_ga.BUTTON_SPRITE_RADIUS,
        HERE / "cabinet_buttons_mask.png",
    )
