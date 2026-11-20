"""Render an allowlisted status string as a PNG for the device screen.

Board 2's vendor UI (LYNAI_LcdDisplay + StandbyManager) receives chat text and drops it:
the 2026-11-19 serial capture shows the board logging ">> กำลังคิด..." while the screen kept
showing only the emoji and the standby clock. Its firmware does expose an MCP tool,
`self.screen.preview_image`, which takes a `url`, fetches it over plain HTTP and hands the
bytes to LVGL (PNG is decoded by lodepng). So the text is drawn here, server side, with a
font that has Thai glyphs, and the board is asked to display the picture.

Nothing but the allowlisted status strings is ever rendered (hard rule: the screen never
shows transcript or answer text).
"""
import logging
import os
from io import BytesIO

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

logger = logging.getLogger("ScreenImage")

# Board 2's panel is 240x240 (boot log: LcdDisplay, 240x240, ST7789-class).
SIZE = (240, 240)
BG = (0, 0, 0)
FG = (255, 255, 255)

# Fonts with Thai glyphs, in order of preference. Leelawadee UI ships with Windows and
# renders Thai well at small sizes; Tahoma is the fallback.
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\leelawui.ttf",
    r"C:\Windows\Fonts\leelawad.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    "/usr/share/fonts/truetype/tlwg/Loma.ttf",
)
_cache: dict[tuple[str, int, int, int], bytes] = {}


def _font(size: int):
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    logger.warning("No Thai-capable font found; falling back to the bitmap default font")
    return ImageFont.load_default()


def render_status(text: str, size: tuple[int, int] = SIZE, font_size: int = 44) -> bytes:
    """Returns PNG bytes with the text centred. Results are cached per text/size."""
    if Image is None:
        raise RuntimeError("Pillow is required for screen_image rendering (pip install pillow)")
    key = (text, size[0], size[1], font_size)
    if key in _cache:
        return _cache[key]

    img = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(img)
    font = _font(font_size)
    # Shrink until it fits the width, so a longer status never runs off the panel.
    while font_size > 14:
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= size[0] - 24:
            break
        font_size -= 4
        font = _font(font_size)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((size[0] - (box[2] - box[0])) / 2 - box[0],
               (size[1] - (box[3] - box[1])) / 2 - box[1]), text, font=font, fill=FG)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png = buf.getvalue()
    _cache[key] = png
    logger.info("Rendered status image %r: %d bytes", text, len(png))
    return png
