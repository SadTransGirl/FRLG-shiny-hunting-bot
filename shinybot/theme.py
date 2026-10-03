"""Look of the window (CustomTkinter): soft rounded dark UI with an Eevee accent.

Same layout language as Sea-Salt FT: night-blue panels, rounded cards, a
sidebar of pages. Text uses "Pokemon Pixel Font" by SpyroSteak (CC BY-SA),
which has no $ & < > [ ] ^ _ { } ~ characters: keep those out of UI text.

    theme.set_app_id()     # before the window exists (taskbar icon)
    theme.init(root)       # fonts, ttk styles, window icon
    theme.F["body"]        # CTkFont for widgets
"""

from __future__ import annotations

import sys
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk

import customtkinter as ctk
from PIL import Image, ImageDraw

ASSETS = Path(__file__).parent / 'assets'
ICON_PNG = ASSETS / 'icon.png'
ICON_ICO = ASSETS / 'icon.ico'
PIXEL_FONT = ASSETS / 'fonts' / 'pokemon_pixel_font.ttf'
PIXEL_FAMILY = 'Pokemon Pixel Font'
APP_ID = 'FRLGShinyBot.Hunter'

# ---- palette: night-blue panels, Eevee gold accent ------------------------
BG = '#0e1324'
SIDEBAR = '#0a0e1b'
PANEL = '#161d33'
PANEL_ALT = '#1f2842'
PANEL_HOVER = '#27325a'
BORDER = '#2a3558'
TEXT = '#eef1f7'
TEXT_MUTED = '#8e97b5'
ACCENT = '#d9a75c'            # Eevee
ACCENT_HOVER = '#ebbe78'
ACCENT_DIM = '#3a3022'        # selected nav item
ACCENT_TEXT = '#1d1408'
CREAM = '#d4b48c'             # Eevee's collar
SUCCESS = '#5fd49a'
DANGER = '#ef6b7b'
WARNING = '#e8b23f'
RADIUS = 12

F: dict[str, ctk.CTkFont] = {}
FAMILY = {'pixel': 'Segoe UI', 'symbol': 'Segoe UI Symbol'}


def set_app_id() -> None:
    """Own taskbar identity on Windows, so the taskbar shows our icon (call
    before the first window is created)."""
    if sys.platform == 'win32':
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass


def _load_pixel_font() -> str | None:
    """Make the bundled TTF usable by Tk; returns its family name if available."""
    if sys.platform == 'win32' and PIXEL_FONT.exists():
        try:
            import ctypes

            ctypes.windll.gdi32.AddFontResourceExW(str(PIXEL_FONT), 0x10, 0)  # FR_PRIVATE
        except Exception:
            pass
    families = set(tkfont.families())
    return PIXEL_FAMILY if PIXEL_FAMILY in families else None


def init(root) -> None:
    ctk.set_appearance_mode('dark')
    pixel = _load_pixel_font()
    fallback = 'Segoe UI' if sys.platform == 'win32' else 'DejaVu Sans'
    FAMILY['pixel'] = pixel or fallback
    FAMILY['symbol'] = 'Segoe UI Symbol' if sys.platform == 'win32' else 'DejaVu Sans'
    # The pixel font is drawn on a grid: sizes in steps of 8 px stay crisp.
    size = (lambda px: px) if pixel else (lambda px: int(px * 0.7))
    F.update(
        body=ctk.CTkFont(FAMILY['pixel'], size(24)),
        small=ctk.CTkFont(FAMILY['pixel'], size(20)),
        button=ctk.CTkFont(FAMILY['pixel'], size(24)),
        nav=ctk.CTkFont(FAMILY['pixel'], size(24)),
        heading=ctk.CTkFont(FAMILY['pixel'], size(32)),
        title=ctk.CTkFont(FAMILY['pixel'], size(40)),
        big=ctk.CTkFont(FAMILY['pixel'], size(64)),
        symbol=ctk.CTkFont(FAMILY['symbol'], 16),
        mono=ctk.CTkFont('Consolas' if sys.platform == 'win32' else 'DejaVu Sans Mono', 12),
    )
    _ttk_styles(root)
    set_icon(root)


def _ttk_styles(root) -> None:
    """The sequence table is ttk (CustomTkinter has no table)."""
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL, rowheight=30,
                    font=(FAMILY['pixel'], -24))
    style.configure('Treeview.Heading', background=PANEL_ALT, foreground=TEXT_MUTED, relief='flat',
                    bordercolor=PANEL_ALT, lightcolor=PANEL_ALT, darkcolor=PANEL_ALT,
                    font=(FAMILY['pixel'], -24), padding=(8, 6))
    style.map('Treeview', background=[('selected', ACCENT_DIM)], foreground=[('selected', ACCENT)])
    style.map('Treeview.Heading', background=[('active', PANEL_HOVER)])
    style.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])


def set_icon(window) -> None:
    try:
        if ICON_ICO.exists() and sys.platform == 'win32':
            window.iconbitmap(str(ICON_ICO))  # also replaces CustomTkinter's default icon
    except Exception:
        pass
    try:
        if ICON_PNG.exists():
            from PIL import ImageTk

            window._icon_photo = ImageTk.PhotoImage(Image.open(ICON_PNG))
            window.iconphoto(True, window._icon_photo)
    except Exception:
        pass


# ---- icons, drawn at runtime so no extra image files are needed -------------

def _canvas(size: int) -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
    scale = 4  # draw big, shrink for smooth edges
    img = Image.new('RGBA', (size * scale, size * scale), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img), size * scale


def _pokeball(color: str, size: int) -> Image.Image:
    img, d, s = _canvas(size)
    w = s // 14
    d.ellipse((w, w, s - w, s - w), outline=color, width=w)
    d.rectangle((w, s // 2 - w // 2, s - w, s // 2 + w // 2), fill=color)
    r = s // 7
    d.ellipse((s // 2 - r, s // 2 - r, s // 2 + r, s // 2 + r), fill=SIDEBAR, outline=color, width=w)
    d.pieslice((w, w, s - w, s - w), 180, 360, fill=color)
    d.ellipse((s // 2 - r, s // 2 - r, s // 2 + r, s // 2 + r), fill=SIDEBAR, outline=color, width=w)
    return img.resize((size, size), Image.LANCZOS)


def _gamepad(color: str, size: int) -> Image.Image:
    img, d, s = _canvas(size)
    d.rounded_rectangle((s * 0.04, s * 0.26, s * 0.96, s * 0.74), radius=s * 0.22, fill=color)
    hole = SIDEBAR
    t = s * 0.06
    cx, cy = s * 0.3, s * 0.5
    d.rectangle((cx - t * 2.2, cy - t * 0.7, cx + t * 2.2, cy + t * 0.7), fill=hole)
    d.rectangle((cx - t * 0.7, cy - t * 2.2, cx + t * 0.7, cy + t * 2.2), fill=hole)
    for bx, by in ((0.68, 0.44), (0.8, 0.56)):
        d.ellipse((s * bx - t, s * by - t, s * bx + t, s * by + t), fill=hole)
    return img.resize((size, size), Image.LANCZOS)


def _gear(color: str, size: int) -> Image.Image:
    import math

    img, d, s = _canvas(size)
    c, outer, inner, teeth = s / 2, s * 0.46, s * 0.34, 8
    points = []
    for i in range(teeth * 2):
        for step in (0.0, 0.5):
            angle = (i + step) * math.pi / teeth
            r = outer if i % 2 == 0 else inner
            points.append((c + r * math.cos(angle), c + r * math.sin(angle)))
    d.polygon(points, fill=color)
    d.ellipse((c - inner * 0.95, c - inner * 0.95, c + inner * 0.95, c + inner * 0.95), fill=color)
    hole = s * 0.13
    d.ellipse((c - hole, c - hole, c + hole, c + hole), fill=SIDEBAR)
    return img.resize((size, size), Image.LANCZOS)


_DRAWERS = {'hunt': _pokeball, 'play': _gamepad, 'setup': _gear}


def icon(name: str, size: int = 30, color: str = TEXT_MUTED) -> ctk.CTkImage | None:
    if name == 'app':
        if not ICON_PNG.exists():
            return None
        img = Image.open(ICON_PNG).convert('RGBA')
    else:
        img = _DRAWERS[name](color, size * 2)
    return ctk.CTkImage(img, img, size=(size, size))
