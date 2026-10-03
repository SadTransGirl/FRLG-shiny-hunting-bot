"""See-through panels over a wallpaper.

Tk has no real widget transparency, so every "glass" widget is a canvas that
paints the slice of the page wallpaper behind it, tinted by the cards it sits
in, and draws its text straight onto that. Ordinary (opaque) widgets placed in
glass get their corner colour matched to what is behind them.

    page = GlassPage(parent, wallpaper)          # fills a content area
    card = GlassFrame(page, tint=theme.PANEL, alpha=0.72, radius=12)
    GlassLabel(card, 'Hello', font='heading').pack(...)
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from . import theme

# Wallpapers scaled to a page size, shared by all pages of that size.
_wall_cache: dict[tuple[int, int, int], Image.Image] = {}


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip('#')
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _hex(rgb) -> str:
    return '#%02x%02x%02x' % tuple(int(v) for v in rgb[:3])


def _scaling(widget) -> float:
    try:
        return ctk.ScalingTracker.get_window_scaling(widget.winfo_toplevel())
    except Exception:
        return 1.0


class _Glass:
    """Shared behaviour: painting the backdrop and scaling geometry like CustomTkinter."""

    page: GlassPage
    tint: str | None = None
    alpha: float = 0.0
    radius: int = 0
    border: str | None = None

    def _init_glass(self) -> None:
        self._s = _scaling(self)
        self.image: Image.Image | None = None
        self._photo = None
        self._image_item = self.create_image(0, 0, anchor='nw')
        self.tag_lower(self._image_item)
        self.bind('<Configure>', lambda _e: self.page.schedule(self), add='+')

    def S(self, value):
        """Scale a pixel value (or tuple) for the display, as CustomTkinter does."""
        if isinstance(value, (tuple, list)):
            return tuple(int(round(v * self._s)) for v in value)
        return int(round(value * self._s))

    # CustomTkinter widgets scale their own padding; do the same for glass widgets.
    def pack(self, **kw):
        for key in ('padx', 'pady', 'ipadx', 'ipady'):
            if key in kw:
                kw[key] = self.S(kw[key])
        return tk.Canvas.pack(self, **kw)

    def grid(self, **kw):
        for key in ('padx', 'pady', 'ipadx', 'ipady'):
            if key in kw:
                kw[key] = self.S(kw[key])
        return tk.Canvas.grid(self, **kw)

    def paint(self) -> None:
        width, height = self.winfo_width(), self.winfo_height()
        if width < 2 or height < 2:
            return
        img = self.page.backdrop(self, width, height)
        if self.tint and self.alpha > 0:
            img = _rounded_tint(img, self.tint, self.alpha, self.S(self.radius), self.border)
        self.image = img
        self._photo = ImageTk.PhotoImage(img)
        self.itemconfigure(self._image_item, image=self._photo)
        self.tag_lower(self._image_item)
        self._match_children()

    def _match_children(self) -> None:
        """Give opaque CustomTkinter children corners the colour behind them."""
        for child in self.winfo_children():
            if isinstance(child, _Glass) or not isinstance(child, ctk.CTkBaseClass):
                continue
            x, y = child.winfo_x(), child.winfo_y()
            w, h = max(1, child.winfo_width()), max(1, child.winfo_height())
            region = self.image.crop((x, y, x + w, y + h)).resize((1, 1), Image.BOX)
            color = _hex(region.getpixel((0, 0)))
            try:
                if child.cget('bg_color') != color:
                    child.configure(bg_color=color)
            except (ValueError, tk.TclError):
                pass


_corner_cache: dict[tuple[int, bool], Image.Image] = {}


def _corner(radius: int, outline: bool) -> Image.Image:
    """Smooth top-left corner of a rounded rectangle mask (or of its 1px outline)."""
    key = (radius, outline)
    if key not in _corner_cache:
        factor = 4
        size = radius * factor
        big = Image.new('L', (size * 2, size * 2), 0)
        draw = ImageDraw.Draw(big)
        if outline:
            draw.ellipse((0, 0, size * 2 - 1, size * 2 - 1), outline=110, width=factor)
        else:
            draw.ellipse((0, 0, size * 2 - 1, size * 2 - 1), fill=255)
        _corner_cache[key] = big.crop((0, 0, size, size)).resize((radius, radius), Image.LANCZOS)
    return _corner_cache[key]


def _rounded_mask(size: tuple[int, int], radius: int, outline: bool = False) -> Image.Image:
    w, h = size
    radius = max(1, min(radius, w // 2, h // 2))
    mask = Image.new('L', size, 0 if outline else 255)
    if outline:
        draw = ImageDraw.Draw(mask)
        draw.line((radius, 0, w - radius, 0), fill=110)
        draw.line((radius, h - 1, w - radius, h - 1), fill=110)
        draw.line((0, radius, 0, h - radius), fill=110)
        draw.line((w - 1, radius, w - 1, h - radius), fill=110)
    corner = _corner(radius, outline)
    for flip, (x, y) in ((None, (0, 0)), (Image.FLIP_LEFT_RIGHT, (w - radius, 0)),
                         (Image.FLIP_TOP_BOTTOM, (0, h - radius)), ('both', (w - radius, h - radius))):
        tile = corner
        if flip == 'both':
            tile = corner.transpose(Image.FLIP_LEFT_RIGHT).transpose(Image.FLIP_TOP_BOTTOM)
        elif flip is not None:
            tile = corner.transpose(flip)
        mask.paste(tile, (x, y))
    return mask


def _rounded_tint(img: Image.Image, tint: str, alpha: float, radius: int,
                  border: str | None) -> Image.Image:
    tinted = Image.blend(img, Image.new('RGB', img.size, _rgb(tint)), alpha)
    out = Image.composite(tinted, img, _rounded_mask(img.size, radius))
    if border:
        out = Image.composite(Image.new('RGB', img.size, _rgb(border)), out,
                              _rounded_mask(img.size, radius, outline=True))
    return out


class GlassPage(_Glass, tk.Canvas):
    """Fills its area with the wallpaper; glass widgets inside paint from it."""

    def __init__(self, master, wallpaper: Image.Image | None, shade: float = 0.3) -> None:
        tk.Canvas.__init__(self, master, highlightthickness=0, bd=0, bg=theme.BG)
        self.page = self
        self.wallpaper = wallpaper
        self.shade = shade
        self.members: list[_Glass] = []
        self._dirty: set = set()
        self._job = None
        self._init_glass()

    def register(self, widget: _Glass) -> None:
        self.members.append(widget)

    def schedule(self, widget=None) -> None:
        """Repaint `widget` (and the glass inside it) soon; None or the page = everything."""
        self._dirty.add(widget or self)
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(40, self.redraw)

    def set_wallpaper(self, wallpaper: Image.Image | None) -> None:
        self.wallpaper = wallpaper
        _wall_cache.clear()
        self.schedule()

    def _scaled_wall(self, width: int, height: int) -> Image.Image:
        key = (width, height, id(self.wallpaper))
        if key not in _wall_cache:
            if self.wallpaper is None:
                wall = Image.new('RGB', (width, height), _rgb(theme.BG))
            else:
                src = self.wallpaper
                scale = max(width / src.width, height / src.height)
                size = (max(width, int(src.width * scale + 1)), max(height, int(src.height * scale + 1)))
                big = src.resize(size, Image.LANCZOS)
                left, top = (big.width - width) // 2, (big.height - height) // 2
                wall = big.crop((left, top, left + width, top + height))
                wall = Image.blend(wall, Image.new('RGB', wall.size, _rgb(theme.BG)), self.shade)
            _wall_cache[key] = wall
        return _wall_cache[key]

    def backdrop(self, widget, width: int, height: int) -> Image.Image:
        """What is behind `widget`: the wallpaper tinted by every glass card around it."""
        if widget is self:
            return self._scaled_wall(width, height).copy()
        wall = self._scaled_wall(max(1, self.winfo_width()), max(1, self.winfo_height()))
        x = widget.winfo_rootx() - self.winfo_rootx()
        y = widget.winfo_rooty() - self.winfo_rooty()
        img = Image.new('RGB', (width, height), _rgb(theme.BG))
        img.paste(wall.crop((x, y, x + width, y + height)), (0, 0))
        chain = []
        parent = widget.master
        while parent is not None and parent is not self:
            if isinstance(parent, _Glass) and parent.tint and parent.alpha > 0:
                chain.append(parent)
            parent = parent.master
        for card in reversed(chain):  # outermost first
            img = Image.blend(img, Image.new('RGB', img.size, _rgb(card.tint)), card.alpha)
        return img

    def redraw(self) -> None:
        self._job = None
        dirty, self._dirty = self._dirty, set()
        if self in dirty:
            self.paint()
            targets = self.members
        else:
            targets = [w for w in self.members if any(_inside(w, d) for d in dirty)]
        for widget in list(targets):  # creation order: containers before their contents
            if widget.winfo_exists():
                widget.paint()


def _inside(widget, ancestor) -> bool:
    while widget is not None:
        if widget is ancestor:
            return True
        widget = widget.master
    return False


class GlassFrame(_Glass, tk.Canvas):
    """A container; with a tint it is a see-through rounded card."""

    def __init__(self, master, tint: str | None = None, alpha: float = 0.0, radius: int = 0,
                 border: str | None = None, **kw) -> None:
        tk.Canvas.__init__(self, master, highlightthickness=0, bd=0, bg=theme.PANEL, **kw)
        self.page = master.page
        self.tint, self.alpha, self.radius, self.border = tint, alpha, radius, border
        self._init_glass()
        self.page.register(self)


class GlassLabel(_Glass, tk.Canvas):
    """Text drawn over the backdrop (with a soft shadow so it reads on any picture)."""

    def __init__(self, master, text: str = '', font: str = 'body', color: str = theme.TEXT,
                 wraplength: int | None = None, width: int | None = None, shadow: bool = True) -> None:
        tk.Canvas.__init__(self, master, highlightthickness=0, bd=0, bg=theme.PANEL)
        self.page = master.page
        self._init_glass()
        self.page.register(self)
        self.font = tkfont.Font(family=theme.FAMILY['pixel'] if font != 'mono' else theme.F['mono'].cget('family'),
                                size=-self.S(theme.SIZES[font]))
        self.wraplength = self.S(wraplength) if wraplength else None
        self.fixed_width = self.S(width) if width else None
        self.shadow = shadow
        offset = max(1, self.S(1))
        self._shadow_item = self.create_text(offset, offset, anchor='nw', font=self.font,
                                             fill='#04060c', width=self.wraplength or 0)
        self._text_item = self.create_text(0, 0, anchor='nw', font=self.font, width=self.wraplength or 0)
        self.text = ''
        self.color = color
        self.set(text, color)

    def set(self, text: str | None = None, color: str | None = None) -> None:
        if text is not None:
            self.text = text
        if color is not None:
            self.color = color
        self.itemconfigure(self._text_item, text=self.text, fill=self.color)
        self.itemconfigure(self._shadow_item, text=self.text if self.shadow else '')
        bbox = self.bbox(self._text_item) or (0, 0, 1, 1)
        width = self.fixed_width or (bbox[2] - bbox[0] + self.S(2))
        height = max(bbox[3] - bbox[1], self.font.metrics('linespace')) + self.S(2)
        if int(self.cget('width')) != width or int(self.cget('height')) != height:
            self.configure(width=width, height=height)
