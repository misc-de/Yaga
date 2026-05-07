from __future__ import annotations

import io
import math
import shlex
import subprocess
import threading
import shutil
from datetime import datetime
from pathlib import Path

import gi
import cairo

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango, PangoCairo

try:
    from PIL import Image as PILImage, ImageEnhance, ImageOps, ImageDraw
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

from . import APP_ID, APP_NAME, VERSION
from .config import Settings
from .database import Database
from .i18n import Translator
from .models import MediaItem
from .scanner import MediaScanner
from .thumbnails import Thumbnailer


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _fmt_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_date(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d  %H:%M")


def _image_dimensions(path: str) -> str | None:
    fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(path)
    if fmt is not None:
        return f"{w} × {h}"
    return None


# ---------------------------------------------------------------------------
# Sticker generators (drawn with Pillow at runtime, no external assets needed)
# ---------------------------------------------------------------------------

def _make_star(size: int = 96) -> "PILImage.Image":
    img = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    r_out = size / 2 - 3
    r_in = r_out * 0.38
    pts = []
    for i in range(10):
        a = math.pi / 5 * i - math.pi / 2
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.polygon(pts, fill=(255, 215, 0, 255))
    return img


def _make_heart(size: int = 96) -> "PILImage.Image":
    img = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2 + 2
    sc = size / 36
    pts = [(cx + 16 * math.sin(math.radians(i)) ** 3 * sc,
            cy - (13 * math.cos(math.radians(i))
                  - 5 * math.cos(math.radians(2 * i))
                  - 2 * math.cos(math.radians(3 * i))
                  - math.cos(math.radians(4 * i))) * sc)
           for i in range(360)]
    draw.polygon(pts, fill=(255, 60, 80, 255))
    return img


def _make_sparkle(size: int = 96) -> "PILImage.Image":
    img = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    r_out = size / 2 - 3
    r_in = r_out * 0.12
    pts = []
    for i in range(8):
        a = math.pi / 4 * i - math.pi / 2
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.polygon(pts, fill=(255, 255, 255, 230))
    return img


def _pil_to_texture(img: "PILImage.Image") -> Gdk.Texture:
    rgba = img.convert("RGBA")
    w, h = rgba.size
    raw = rgba.tobytes()
    gbytes = GLib.Bytes.new(raw)
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, gbytes, w * 4)


# ---------------------------------------------------------------------------
# Emoji sticker rendering via Pango+Cairo
# ---------------------------------------------------------------------------

def _emoji_to_pil(char: str, size: int = 96) -> "PILImage.Image":
    """Render an emoji character to a square PIL RGBA image using PangoCairo."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    layout = PangoCairo.create_layout(cr)
    layout.set_text(char, -1)
    PangoCairo.context_set_resolution(layout.get_context(), 96)
    desc = Pango.FontDescription.from_string("Noto Color Emoji")
    desc.set_absolute_size(int(size * 0.88) * Pango.SCALE)
    layout.set_font_description(desc)
    PangoCairo.update_layout(cr, layout)
    _, ink = layout.get_pixel_extents()
    w = ink.width or size
    h = ink.height or size
    cr.move_to(max(0.0, (size - w) / 2.0 - ink.x),
               max(0.0, (size - h) / 2.0 - ink.y))
    PangoCairo.show_layout(cr, layout)
    data = bytes(surface.get_data())
    return PILImage.frombytes("RGBA", (size, size), data, "raw", "BGRA")


_STICKER_GROUPS: list[tuple[str, list[str]]] = [
    ("Smileys",         ["😀", "😂", "🥰", "😎", "🤩", "🥳", "😇", "🙈"]),
    ("Herzen & Sterne", ["❤", "🧡", "💛", "💚", "💙", "💜", "⭐", "🌟"]),
    ("Symbole",         ["🔥", "💫", "✨", "🎉", "🎊", "🌈", "🏆", "💬"]),
]

# Cache rendered emoji at a fixed master size; rescale on demand.
_EMOJI_PIL_CACHE: "dict[str, PILImage.Image]" = {}


def _get_emoji_pil(char: str, px: int) -> "PILImage.Image":
    _MASTER = 256
    if char not in _EMOJI_PIL_CACHE:
        _EMOJI_PIL_CACHE[char] = _emoji_to_pil(char, _MASTER)
    base = _EMOJI_PIL_CACHE[char]
    if px == _MASTER:
        return base
    return base.resize((px, px), PILImage.LANCZOS)


# ---------------------------------------------------------------------------
# Frame themes
# ---------------------------------------------------------------------------

_FRAME_THEMES: list[tuple[str, str, tuple, tuple]] = [
    ("christmas",  "Weihnachten", (220, 30,  30),  (0,   130, 60)),
    ("silvester",  "Silvester",   (20,  20,  20),  (255, 215, 0)),
    ("ostern",     "Ostern",      (255, 180, 230), (120, 200, 80)),
    ("hochzeit",   "Hochzeit",    (255, 255, 255), (200, 170, 130)),
    ("geburtstag", "Geburtstag",  (255, 100, 180), (255, 220, 0)),
    ("fruehling",  "Frühling",    (200, 240, 150), (255, 200, 220)),
    ("sommer",     "Sommer",      (255, 220, 0),   (255, 140, 0)),
    ("winter",     "Winter",      (200, 230, 255), (150, 200, 255)),
]


def _frame_pil(iw: int, ih: int, theme: str) -> "PILImage.Image | None":
    """Transparent RGBA border overlay; paste over image to add a frame."""
    theme_data = {t[0]: (t[2], t[3]) for t in _FRAME_THEMES}
    if theme not in theme_data:
        return None
    c1, c2 = theme_data[theme]
    frame = PILImage.new("RGBA", (iw, ih), (0, 0, 0, 0))
    draw = ImageDraw.Draw(frame)
    bw = max(10, min(iw, ih) // 18)
    for rect in [
        [0, 0, iw - 1, bw - 1],
        [0, ih - bw, iw - 1, ih - 1],
        [0, 0, bw - 1, ih - 1],
        [iw - bw, 0, iw - 1, ih - 1],
    ]:
        draw.rectangle(rect, fill=c1 + (230,))
    acc = max(2, bw // 3)
    for rect in [
        [bw, bw, iw - bw - 1, bw + acc - 1],
        [bw, ih - bw - acc, iw - bw - 1, ih - bw - 1],
        [bw, bw, bw + acc - 1, ih - bw - 1],
        [iw - bw - acc, bw, iw - bw - 1, ih - bw - 1],
    ]:
        draw.rectangle(rect, fill=c2 + (210,))
    return frame


def _make_text_pil(text: str, font_size: int, color: tuple) -> "PILImage.Image":
    """Render text to a transparent RGBA PIL image via PangoCairo (with outline)."""
    msurf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    mcr = cairo.Context(msurf)
    layout = PangoCairo.create_layout(mcr)
    layout.set_text(text, -1)
    PangoCairo.context_set_resolution(layout.get_context(), 96)
    desc = Pango.FontDescription.from_string("Sans Bold")
    desc.set_absolute_size(font_size * Pango.SCALE)
    layout.set_font_description(desc)
    PangoCairo.update_layout(mcr, layout)
    _, ink = layout.get_pixel_extents()
    w = max(1, ink.width) + 12
    h = max(1, ink.height) + 12
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    ox, oy = -ink.x + 6, -ink.y + 6
    layout2 = PangoCairo.create_layout(cr)
    layout2.set_text(text, -1)
    PangoCairo.context_set_resolution(layout2.get_context(), 96)
    layout2.set_font_description(desc)
    PangoCairo.update_layout(cr, layout2)
    cr.move_to(ox, oy)
    cr.set_source_rgba(0, 0, 0, 0.75)
    PangoCairo.layout_path(cr, layout2)
    cr.set_line_width(4.0)
    cr.stroke()
    cr.move_to(ox, oy)
    cr.set_source_rgba(color[0] / 255, color[1] / 255, color[2] / 255, 1.0)
    PangoCairo.show_layout(cr, layout2)
    data = bytes(surface.get_data())
    return PILImage.frombytes("RGBA", (w, h), data, "raw", "BGRA")


# ---------------------------------------------------------------------------
# Filter presets (PIL, each takes/returns RGB Image)
# ---------------------------------------------------------------------------

def _filter_bw(img: "PILImage.Image") -> "PILImage.Image":
    return ImageOps.grayscale(img).convert("RGB")


def _filter_sepia(img: "PILImage.Image") -> "PILImage.Image":
    return img.convert("RGB", matrix=(
        0.393, 0.769, 0.189, 0,
        0.349, 0.686, 0.168, 0,
        0.272, 0.534, 0.131, 0,
    ))


def _filter_warm(img: "PILImage.Image") -> "PILImage.Image":
    r, g, b = img.split()
    r = r.point(lambda x: min(255, int(x * 1.18)))
    g = g.point(lambda x: min(255, int(x * 1.04)))
    b = b.point(lambda x: max(0,   int(x * 0.82)))
    return PILImage.merge("RGB", (r, g, b))


def _filter_cool(img: "PILImage.Image") -> "PILImage.Image":
    r, g, b = img.split()
    r = r.point(lambda x: max(0,   int(x * 0.82)))
    g = g.point(lambda x: min(255, int(x * 1.02)))
    b = b.point(lambda x: min(255, int(x * 1.20)))
    return PILImage.merge("RGB", (r, g, b))


def _filter_fade(img: "PILImage.Image") -> "PILImage.Image":
    result = ImageEnhance.Contrast(img).enhance(0.68)
    result = ImageEnhance.Brightness(result).enhance(1.06)
    return result.point(lambda x: int(x * 0.83 + 28))


def _filter_dramatic(img: "PILImage.Image") -> "PILImage.Image":
    result = ImageEnhance.Contrast(img).enhance(1.85)
    return ImageEnhance.Color(result).enhance(0.55)


def _filter_vintage(img: "PILImage.Image") -> "PILImage.Image":
    result = ImageEnhance.Color(img).enhance(0.65)
    result = ImageEnhance.Contrast(result).enhance(0.88)
    r, g, b = result.split()
    r = r.point(lambda x: min(255, int(x * 1.12 + 8)))
    g = g.point(lambda x: min(255, int(x * 1.04)))
    b = b.point(lambda x: max(0,   int(x * 0.80)))
    return PILImage.merge("RGB", (r, g, b))


def _filter_invert(img: "PILImage.Image") -> "PILImage.Image":
    return ImageOps.invert(img)


_FILTER_DEFS: list[tuple[str, str, object]] = [
    ("none",      "Original",    None),
    ("bw",        "S/W",         _filter_bw),
    ("sepia",     "Sepia",       _filter_sepia),
    ("warm",      "Warm",        _filter_warm),
    ("cool",      "Kalt",        _filter_cool),
    ("fade",      "Verblasst",   _filter_fade),
    ("dramatic",  "Dramatisch",  _filter_dramatic),
    ("vintage",   "Vintage",     _filter_vintage),
    ("invert",    "Invertieren", _filter_invert),
]


# ---------------------------------------------------------------------------
# GObject wrapper for the virtualized grid model
# ---------------------------------------------------------------------------

class MediaRow(GObject.Object):
    """One cell in the gallery grid – either a folder or a media item."""

    __gtype_name__ = "YagaMediaRow"

    def __init__(self) -> None:
        super().__init__()
        self.media_item: MediaItem | None = None
        self.folder_path: str | None = None
        self.folder_count: int = 0
        self.folder_thumb: str | None = None

    @classmethod
    def from_media(cls, item: MediaItem) -> "MediaRow":
        row = cls()
        row.media_item = item
        return row

    @classmethod
    def from_folder(cls, folder: str, count: int, thumb: str | None) -> "MediaRow":
        row = cls()
        row.folder_path = folder
        row.folder_count = count
        row.folder_thumb = thumb
        return row

    @property
    def is_folder(self) -> bool:
        return self.folder_path is not None


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class GalleryApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name(APP_NAME)
        self.connect("activate", self.on_activate)

    def on_activate(self, _app: Adw.Application) -> None:
        window = GalleryWindow(self)
        window.present()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class GalleryWindow(Adw.ApplicationWindow):
    def __init__(self, app: GalleryApplication) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1120, 760)

        self.settings = Settings.load()
        self.translator = Translator(self.settings.language)
        self.database = Database()
        self.thumbnailer = Thumbnailer()
        self.scanner = MediaScanner(self.database, self.thumbnailer)
        self.category = self._first_existing_category()
        self.current_folder: str | None = None
        self.current_items: list[MediaItem] = []
        self.category_buttons: dict[str, Gtk.ToggleButton] = {}

        # Track last-rendered view so we can preserve scroll position on refresh
        self._last_render_key: tuple[str, str | None] | None = None

        # Dynamic tile-size CSS (updated via tick callback whenever the scroller resizes)
        self._tile_css = Gtk.CssProvider()
        self._grid_width = 0

        self._apply_theme()
        self._load_css()
        self._build_ui()
        self.refresh(scan=True)

    def _(self, text: str) -> str:
        return self.translator.gettext(text)

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)
        self.status.set_visible(bool(text))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.toolbar = Adw.ToolbarView()
        self.set_content(self.toolbar)

        self.header = Adw.HeaderBar()
        self.toolbar.add_top_bar(self.header)

        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.back_button.set_tooltip_text(self._("Back"))
        self.back_button.connect("clicked", self._on_back)
        self.header.pack_start(self.back_button)

        title = Adw.WindowTitle(title=APP_NAME, subtitle="")
        self.header.set_title_widget(title)

        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_button.set_tooltip_text(self._("Refresh"))
        self.refresh_button.connect("clicked", lambda _b: self.refresh(scan=True))
        self.header.pack_start(self.refresh_button)

        settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        settings_button.set_tooltip_text(self._("Settings"))
        settings_button.connect("clicked", self._open_settings)
        self.header.pack_start(settings_button)

        self.sort_button = Gtk.MenuButton(icon_name="view-sort-descending-symbolic")
        self.sort_button.set_tooltip_text(self._("Sort"))
        self.sort_button.set_popover(self._sort_popover())
        self.header.pack_end(self.sort_button)

        # Category nav bar (styled like a bottom switcher bar)
        self.nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.nav_box.set_hexpand(True)
        self.nav_box.add_css_class("view-switcher")
        self.toolbar.add_top_bar(self.nav_box)

        # Status label (hidden when empty)
        self.status = Gtk.Label(xalign=0)
        self.status.set_hexpand(True)
        self.status.set_vexpand(False)
        self.status.set_margin_start(16)
        self.status.set_margin_end(16)
        self.status.set_margin_top(6)
        self.status.set_margin_bottom(4)
        self.status.add_css_class("dim-label")
        self.status.set_visible(False)

        # Virtualized grid (GridView only renders visible tiles)
        self.item_store = Gio.ListStore(item_type=MediaRow)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_item_setup)
        factory.connect("bind", self._on_item_bind)
        factory.connect("unbind", self._on_item_unbind)

        self.grid_view = Gtk.GridView()
        self.grid_view.set_model(Gtk.NoSelection(model=self.item_store))
        self.grid_view.set_factory(factory)
        self.grid_view.add_css_class("gallery-grid")
        self.grid_view.set_hexpand(True)
        self.grid_view.set_vexpand(True)
        self._apply_grid_settings()

        scroller = Gtk.ScrolledWindow()
        self.media_scroller = scroller
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.grid_view)
        self._grid_width = 0  # force CSS update after rebuild
        scroller.add_tick_callback(self._on_grid_tick)

        self.empty_label = Gtk.Label(label=self._("No pictures found"))
        self.empty_label.set_halign(Gtk.Align.CENTER)
        self.empty_label.set_valign(Gtk.Align.CENTER)
        self.empty_label.add_css_class("dim-label")
        self.empty_label.add_css_class("title-3")
        self.empty_label.set_visible(False)

        grid_overlay = Gtk.Overlay()
        grid_overlay.set_hexpand(True)
        grid_overlay.set_vexpand(True)
        grid_overlay.set_child(scroller)
        grid_overlay.add_overlay(self.empty_label)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_hexpand(True)
        content.set_vexpand(True)
        content.append(self.status)
        content.append(grid_overlay)
        self.toolbar.set_content(content)
        self._rebuild_categories()

    # ------------------------------------------------------------------
    # GridView factory
    # ------------------------------------------------------------------

    def _on_item_setup(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        """Create the reusable widget tree for one grid cell."""
        picture = Gtk.Picture()
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_hexpand(True)
        picture.set_vexpand(True)

        badge = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        badge.add_css_class("osd")
        badge.set_halign(Gtk.Align.CENTER)
        badge.set_valign(Gtk.Align.CENTER)
        badge.set_visible(False)

        folder_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=20)
        folder_label.add_css_class("osd")
        folder_label.set_halign(Gtk.Align.FILL)
        folder_label.set_valign(Gtk.Align.END)
        folder_label.set_visible(False)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(picture)
        overlay.add_overlay(badge)
        overlay.add_overlay(folder_label)

        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("gallery-tile")
        button.set_hexpand(True)
        button.set_vexpand(True)
        button.set_child(overlay)

        # Store sub-widget refs directly on button for fast access in bind
        button._picture = picture          # type: ignore[attr-defined]
        button._badge = badge              # type: ignore[attr-defined]
        button._folder_label = folder_label  # type: ignore[attr-defined]

        # Connect signals once in setup; handlers read the live item via list_item
        button.connect("clicked", self._on_tile_clicked, list_item)

        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", self._on_tile_right_click, list_item)
        button.add_controller(gesture)

        list_item.set_child(button)

    def _on_item_bind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        """Populate the reused widget with data from the current row."""
        row: MediaRow = list_item.get_item()
        button = list_item.get_child()
        picture: Gtk.Picture = button._picture
        badge: Gtk.Image = button._badge
        folder_label: Gtk.Label = button._folder_label

        if row.is_folder:
            if row.folder_thumb and Path(row.folder_thumb).exists():
                picture.set_filename(row.folder_thumb)
            else:
                picture.set_paintable(
                    Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).lookup_icon(
                        "folder-pictures-symbolic", None, 96, 1,
                        Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE,
                    )
                )
            label = row.folder_path.rsplit("/", 1)[-1] if row.folder_path != "/" else "/"
            folder_label.set_label(f"{label} · {row.folder_count}")
            folder_label.set_visible(True)
            badge.set_visible(False)
        else:
            item = row.media_item
            assert item is not None
            if item.thumb_path and Path(item.thumb_path).exists():
                picture.set_filename(item.thumb_path)
            else:
                picture.set_filename(item.path)
            badge.set_visible(item.is_video)
            folder_label.set_visible(False)

    def _on_item_unbind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        """Release image data when the cell scrolls out of view."""
        button = list_item.get_child()
        button._picture.set_paintable(None)

    def _on_tile_clicked(self, _button: Gtk.Button, list_item: Gtk.ListItem) -> None:
        row: MediaRow | None = list_item.get_item()
        if row is None:
            return
        if row.is_folder:
            self._open_folder(None, row.folder_path)
        else:
            self._open_item(None, row.media_item)

    def _on_tile_right_click(
        self,
        gesture: Gtk.GestureClick,
        _n: int,
        x: float,
        y: float,
        list_item: Gtk.ListItem,
    ) -> None:
        row: MediaRow | None = list_item.get_item()
        if row is None or row.is_folder:
            return
        parent = list_item.get_child()
        self._show_context_menu(gesture, 1, x, y, row.media_item, parent)

    # ------------------------------------------------------------------
    # Sort popover
    # ------------------------------------------------------------------

    def _sort_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        for mode, label, icon in [
            ("newest", "Newest first", "view-sort-descending-symbolic"),
            ("oldest", "Oldest first", "view-sort-ascending-symbolic"),
            ("name", "Name", "format-text-bold-symbolic"),
            ("folder", "Folder", "folder-symbolic"),
        ]:
            button = Gtk.Button()
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Image.new_from_icon_name(icon))
            row.append(Gtk.Label(label=self._(label), xalign=0))
            button.set_child(row)
            button.connect("clicked", self._set_sort_mode, mode, popover)
            box.append(button)
        popover.set_child(box)
        return popover

    # ------------------------------------------------------------------
    # Category navigation
    # ------------------------------------------------------------------

    def _rebuild_categories(self) -> None:
        child = self.nav_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.nav_box.remove(child)
            child = next_child
        self.category_buttons.clear()

        _icons = {
            "photos": "camera-photo-symbolic",
            "pictures": "image-x-generic-symbolic",
            "videos": "video-display-symbolic",
            "screenshots": "applets-screenshooter-symbolic",
        }
        for category, label, path in self.settings.categories():
            if not path:
                continue
            img = Gtk.Image.new_from_icon_name(_icons.get(category, "folder-symbolic"))
            img.set_pixel_size(22)
            lbl = Gtk.Label(label=self._(label))
            lbl.add_css_class("caption")
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            vbox.set_halign(Gtk.Align.CENTER)
            vbox.append(img)
            vbox.append(lbl)
            button = Gtk.ToggleButton()
            button.set_child(vbox)
            button.add_css_class("flat")
            button.set_hexpand(True)
            button.set_tooltip_text(str(Path(path).expanduser()))
            button.set_active(category == self.category)
            button.connect("toggled", self._on_category_toggled, category)
            self.nav_box.append(button)
            self.category_buttons[category] = button

    def _apply_grid_settings(self) -> None:
        columns = min(max(int(self.settings.grid_columns), 2), 10)
        self.grid_view.set_min_columns(columns)
        self.grid_view.set_max_columns(columns)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def refresh(self, scan: bool = False) -> None:
        if scan:
            self._set_status(self._("Refresh"))
            self.refresh_button.set_sensitive(False)
            threading.Thread(target=self._scan_thread, daemon=True).start()
            return
        self.refresh_button.set_sensitive(True)
        self._render()

    def _scan_thread(self) -> None:
        self.scanner.scan(self.settings.categories())
        GLib.idle_add(self.refresh, False)

    def _render(self) -> None:
        # Preserve scroll position when refreshing the same view (e.g. after scan)
        render_key = (self.category, self.current_folder)
        vadj = self.media_scroller.get_vadjustment()
        saved_pos = vadj.get_value() if render_key == self._last_render_key else 0.0
        self._last_render_key = render_key

        self.item_store.remove_all()
        self.current_items = []
        self.empty_label.set_visible(False)

        self.back_button.set_visible(
            self.settings.sort_mode == "folder" and self.current_folder is not None
        )
        if self.settings.sort_mode == "folder":
            self._render_folders()
        else:
            self.current_items = self.database.list_media(
                self.category, self.settings.sort_mode, self.current_folder
            )
            for item in self.current_items:
                self.item_store.append(MediaRow.from_media(item))
            self._set_status("")
            self.empty_label.set_visible(not bool(self.current_items))

        if saved_pos > 0:
            def _restore() -> bool:
                vadj.set_value(saved_pos)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(_restore)

    def _render_folders(self) -> None:
        folders = self.database.child_folders(self.category, self.current_folder)
        for folder, count, thumb in folders:
            self.item_store.append(MediaRow.from_folder(folder, count, thumb))
        direct_folder = self.current_folder or "/"
        self.current_items = self.database.list_media(
            self.category, self.settings.sort_mode, direct_folder
        )
        for item in self.current_items:
            self.item_store.append(MediaRow.from_media(item))
        total = len(folders) + len(self.current_items)
        self.empty_label.set_visible(total == 0)
        if self.current_folder:
            self._set_status(self.current_folder)
        else:
            self._set_status("")

    # ------------------------------------------------------------------
    # Item actions
    # ------------------------------------------------------------------

    def _open_folder(self, _button, folder: str) -> None:
        self.current_folder = folder
        self._render()

    def _open_item(self, _button, item: MediaItem) -> None:
        if item.is_video and self.settings.external_video_player.strip():
            subprocess.Popen(shlex.split(self.settings.external_video_player) + [item.path])
            return
        items = self.current_items or self.database.list_media(
            item.category, self.settings.sort_mode, self.current_folder
        )
        ViewerWindow(self, items, items.index(item), self.settings.external_video_player).present()

    def _show_context_menu(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        item: MediaItem,
        parent: Gtk.Widget,
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(parent)
        popover.set_pointing_to(Gdk.Rectangle(int(x), int(y), 1, 1))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for label, icon, callback in [
            ("Delete", "user-trash-symbolic", self._delete_item),
            ("Move", "folder-move-symbolic", self._move_item),
            ("Share", "mail-send-symbolic", self._share_item),
            ("Open externally", "document-open-symbolic", self._open_externally),
        ]:
            button = Gtk.Button()
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Image.new_from_icon_name(icon))
            row.append(Gtk.Label(label=self._(label), xalign=0))
            button.set_child(row)
            button.connect("clicked", lambda _b, cb=callback, it=item, p=popover: (p.popdown(), cb(it)))
            box.append(button)
        popover.set_child(box)
        popover.popup()

    def _delete_item(self, item: MediaItem) -> None:
        try:
            Gio.File.new_for_path(item.path).trash(None)
            self.database.delete_path(item.path)
            self._render()
            self._set_status(self._("Deleted"))
        except GLib.Error:
            self._set_status(self._("Could not complete action"))

    def _move_item(self, item: MediaItem) -> None:
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._move_item_response, item)
        chooser.show()

    def _move_item_response(self, chooser: Gtk.FileChooserNative, response: int, item: MediaItem) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            folder = chooser.get_file().get_path()
            target = Path(folder) / item.name
            try:
                Path(item.path).rename(target)
                self.database.delete_path(item.path)
                self.refresh(scan=True)
                self._set_status(self._("Moved"))
            except OSError:
                self._set_status(self._("Could not complete action"))
        chooser.destroy()

    def _share_item(self, item: MediaItem) -> None:
        if shutil.which("xdg-email"):
            subprocess.Popen(["xdg-email", "--attach", item.path])
            return
        self._set_status(self._("Could not complete action"))

    def _open_externally(self, item: MediaItem) -> None:
        subprocess.Popen(["xdg-open", item.path])

    # ------------------------------------------------------------------
    # Navigation handlers
    # ------------------------------------------------------------------

    def _on_category_toggled(self, button: Gtk.ToggleButton, category: str) -> None:
        if not button.get_active():
            if category == self.category and self.current_folder is not None:
                button.handler_block_by_func(self._on_category_toggled)
                button.set_active(True)
                button.handler_unblock_by_func(self._on_category_toggled)
                self.current_folder = None
                self._render()
            return
        self.category = category
        self.current_folder = None
        for other_category, other_button in self.category_buttons.items():
            if other_category != category:
                other_button.set_active(False)
        self.settings.last_category = category
        self.settings.save()
        self._render()

    def _on_back(self, _button: Gtk.Button) -> None:
        if not self.current_folder or "/" not in self.current_folder:
            self.current_folder = None
        else:
            self.current_folder = self.current_folder.rsplit("/", 1)[0]
        self._render()

    def _set_sort_mode(self, _button: Gtk.Button, mode: str, popover: Gtk.Popover) -> None:
        self.settings.sort_mode = mode
        self.settings.save()
        self.current_folder = None
        popover.popdown()
        self._render()

    def _open_settings(self, _button: Gtk.Button) -> None:
        SettingsWindow(self).present()

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.save()
        self.translator.language = settings.language
        self._apply_theme()
        self._build_ui()
        self.refresh(scan=True)

    # ------------------------------------------------------------------
    # CSS / theme
    # ------------------------------------------------------------------

    def _on_grid_tick(self, widget: Gtk.Widget, _clock) -> bool:
        width = widget.get_width()
        if width != self._grid_width:
            self._grid_width = width
            self._update_tile_size(width)
        return GLib.SOURCE_CONTINUE

    def _update_tile_size(self, scroller_width: int) -> None:
        if scroller_width <= 0:
            return
        columns = min(max(int(self.settings.grid_columns), 2), 10)
        # Each cell has 1px padding on each side → 2px per cell
        cell_size = max(32, scroller_width // columns)
        self._tile_css.load_from_data(
            f"gridview.gallery-grid > child {{ min-height: {cell_size}px; }}".encode()
        )

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            .gallery-tile {
                padding: 0;
                margin: 0;
                border-radius: 0;
                min-width: 0;
                min-height: 0;
            }
            .gallery-tile > * {
                margin: 0;
            }
            gridview.gallery-grid > child {
                padding: 1px;
            }
            .view-switcher {
                border-top: 1px solid @borders;
                padding-top: 4px;
            }
            """
        )
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        Gtk.StyleContext.add_provider_for_display(
            display, self._tile_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _apply_theme(self) -> None:
        style = Adw.StyleManager.get_default()
        style.set_color_scheme(
            {
                "system": Adw.ColorScheme.DEFAULT,
                "light": Adw.ColorScheme.FORCE_LIGHT,
                "dark": Adw.ColorScheme.FORCE_DARK,
            }.get(self.settings.theme, Adw.ColorScheme.DEFAULT)
        )

    def _first_existing_category(self) -> str:
        known = {cat for cat, _label, _path in self.settings.categories()}
        if self.settings.last_category in known:
            return self.settings.last_category
        return self.settings.categories()[0][0]


# ---------------------------------------------------------------------------
# In-app image editor
# ---------------------------------------------------------------------------

class EditorView(Gtk.Box):
    """In-app image editor with collapsible bottom-nav panels."""

    def __init__(self, item: MediaItem) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._item = item
        self._original = PILImage.open(item.path)
        if self._original.mode not in ("RGB", "RGBA"):
            self._original = self._original.convert("RGB")
        self._working = self._original.copy()

        self._filter_mode = "none"
        self._brightness = 1.0
        self._contrast = 1.0
        self._red = 1.0
        self._green = 1.0
        self._blue = 1.0

        self._crop_mode = False
        self._crop_start: tuple[float, float] | None = None
        self._crop_current: tuple[float, float] | None = None
        self._pending_crop: tuple[int, int, int, int] | None = None
        self._crop_rect_disp: tuple[float, float, float, float] | None = None
        self._crop_active_handle: str | None = None
        self._crop_handle_orig: tuple[float, float, float, float] | None = None

        # sticker
        self._sticker_source: "str | PILImage.Image | None" = None
        self._sticker_rel = (0.5, 0.5)          # center as fraction of image
        self._sticker_size_frac = 0.25           # sticker width as fraction of image width
        self._sticker_zoom_start = 0.25
        self._sticker_del_rect: tuple[float, float, float, float] | None = None
        self._drag_sticker = False
        self._drag_sx = 0.0
        self._drag_sy = 0.0

        # frame overlay
        self._frame_theme: str | None = None

        # text sticker
        self._text_color: tuple = (255, 255, 255)

        self._active_panel: str | None = None
        self._update_id: int | None = None
        self._nav_handler_ids: dict[str, int] = {}

        self._build_ui()
        self._schedule_update()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Preview overlay ──
        self._preview = Gtk.Picture()
        self._preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._preview.set_hexpand(True)
        self._preview.set_vexpand(True)

        self._draw_area = Gtk.DrawingArea()
        self._draw_area.set_hexpand(True)
        self._draw_area.set_vexpand(True)
        self._draw_area.set_draw_func(self._draw_overlay)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._draw_area.add_controller(drag)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_overlay_click)
        self._draw_area.add_controller(click)

        zoom_g = Gtk.GestureZoom()
        zoom_g.connect("begin", self._on_sticker_zoom_begin)
        zoom_g.connect("scale-changed", self._on_sticker_zoom_scale)
        self._draw_area.add_controller(zoom_g)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(self._preview)
        overlay.add_overlay(self._draw_area)
        self.append(overlay)

        # ── Panel revealer (slides up above nav bar) ──
        self._panel_stack = Gtk.Stack()
        self._panel_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._panel_stack.set_vhomogeneous(False)
        self._panel_stack.add_named(self._build_panel_filter(), "filter")
        self._panel_stack.add_named(self._build_panel_brightness(), "brightness")
        self._panel_stack.add_named(self._build_panel_colors(), "colors")
        self._panel_stack.add_named(self._build_panel_sticker(), "sticker")
        self._panel_stack.add_named(self._build_panel_crop(), "crop")

        self._panel_revealer = Gtk.Revealer()
        self._panel_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._panel_revealer.set_transition_duration(180)
        self._panel_revealer.set_reveal_child(False)
        self._panel_revealer.set_child(self._panel_stack)
        self.append(self._panel_revealer)

        # ── Bottom nav bar ──
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav_box.set_hexpand(True)
        nav_box.add_css_class("toolbar")

        self._nav_btns: dict[str, Gtk.ToggleButton] = {}
        for key, icon, label in [
            ("filter",     "image-filter-symbolic",       "Filter"),
            ("brightness", "display-brightness-symbolic", "Helligkeit"),
            ("colors",     "preferences-color-symbolic",  "Farben"),
            ("sticker",    "face-smile-symbolic",         "Sticker"),
            ("crop",       "crop-symbolic",               "Zuschneiden"),
        ]:
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.set_margin_top(6)
            inner.set_margin_bottom(6)
            inner.append(Gtk.Image.new_from_icon_name(icon))
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("caption")
            inner.append(lbl)
            btn = Gtk.ToggleButton()
            btn.add_css_class("flat")
            btn.set_child(inner)
            btn.set_hexpand(True)
            hid = btn.connect("toggled", self._on_nav_toggled, key)
            self._nav_handler_ids[key] = hid
            nav_box.append(btn)
            self._nav_btns[key] = btn

        self.append(nav_box)

    # ── Panel builders ──

    def _build_panel_filter(self) -> Gtk.Widget:
        # Build a square 72-px center-crop thumbnail from the working image once
        iw, ih = self._working.size
        side = min(iw, ih)
        base = self._working.crop(
            ((iw - side) // 2, (ih - side) // 2,
             (iw + side) // 2, (ih + side) // 2)
        ).resize((72, 72), PILImage.BILINEAR).convert("RGB")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroll.set_hexpand(True)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row.set_margin_start(8)
        row.set_margin_end(8)
        row.set_margin_top(6)
        row.set_margin_bottom(6)

        self._filter_btns: dict[str, Gtk.ToggleButton] = {}
        for mode, label, fn in _FILTER_DEFS:
            preview = fn(base) if fn else base.copy()
            texture = _pil_to_texture(preview)

            pic = Gtk.Picture()
            pic.set_paintable(texture)
            pic.set_size_request(64, 64)
            pic.set_content_fit(Gtk.ContentFit.FILL)
            pic.set_hexpand(False)

            lbl = Gtk.Label(label=label)
            lbl.add_css_class("caption")
            lbl.set_max_width_chars(8)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)

            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.set_margin_top(4)
            inner.set_margin_bottom(4)
            inner.set_margin_start(2)
            inner.set_margin_end(2)
            inner.append(pic)
            inner.append(lbl)

            btn = Gtk.ToggleButton()
            btn.add_css_class("flat")
            btn.set_active(mode == self._filter_mode)
            btn.set_child(inner)
            btn.connect("toggled", self._on_filter_toggled, mode)
            row.append(btn)
            self._filter_btns[mode] = btn

        scroll.set_child(row)
        return scroll

    def _build_panel_brightness(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        for attr, label in [("_brightness", "Helligkeit"), ("_contrast", "Kontrast")]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            l = Gtk.Label(label=label, xalign=0)
            l.set_size_request(90, -1)
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 2.0, 0.05)
            sc.set_value(getattr(self, attr))
            sc.set_hexpand(True)
            sc.set_draw_value(False)
            sc.connect("value-changed", self._on_slider, attr)
            row.append(l)
            row.append(sc)
            box.append(row)
        return box

    def _build_panel_colors(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        for attr, label in [("_red", "Rot"), ("_green", "Grün"), ("_blue", "Blau")]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            l = Gtk.Label(label=label, xalign=0)
            l.set_size_request(60, -1)
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 2.0, 0.05)
            sc.set_value(getattr(self, attr))
            sc.set_hexpand(True)
            sc.set_draw_value(False)
            sc.connect("value-changed", self._on_slider, attr)
            row.append(l)
            row.append(sc)
            box.append(row)
        return box

    def _build_panel_sticker(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # ── Sub-stack: one page per category ──
        self._sticker_sub_stack = Gtk.Stack()
        self._sticker_sub_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._sticker_sub_stack.set_vhomogeneous(False)

        # Emoji pages (smileys / emotions / symbole)
        for key, emojis in [
            ("smileys",  _STICKER_GROUPS[0][1]),
            ("emotions", _STICKER_GROUPS[1][1]),
            ("symbole",  _STICKER_GROUPS[2][1]),
        ]:
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            scroll.set_hexpand(True)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            row.set_margin_start(6); row.set_margin_end(6)
            row.set_margin_top(5);   row.set_margin_bottom(5)
            for emoji in emojis:
                try:
                    tex = _pil_to_texture(_emoji_to_pil(emoji, 56))
                    pic = Gtk.Picture()
                    pic.set_paintable(tex)
                    pic.set_size_request(52, 52)
                    pic.set_content_fit(Gtk.ContentFit.FILL)
                    pic.set_margin_top(3);    pic.set_margin_bottom(3)
                    pic.set_margin_start(2);  pic.set_margin_end(2)
                    btn = Gtk.Button()
                    btn.add_css_class("flat")
                    btn.set_child(pic)
                except Exception:
                    btn = Gtk.Button(label=emoji)
                    btn.add_css_class("flat")
                    btn.set_size_request(48, 48)
                btn.connect("clicked", self._on_sticker_clicked, emoji)
                row.append(btn)
            scroll.set_child(row)
            self._sticker_sub_stack.add_named(scroll, key)

        # Rahmen page (frame theme thumbnails as toggle buttons)
        rahmen_scroll = Gtk.ScrolledWindow()
        rahmen_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        rahmen_scroll.set_hexpand(True)
        rahmen_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        rahmen_row.set_margin_start(6); rahmen_row.set_margin_end(6)
        rahmen_row.set_margin_top(5);   rahmen_row.set_margin_bottom(5)
        self._frame_btns: dict[str, Gtk.ToggleButton] = {}
        self._frame_hids: dict[str, int] = {}
        iw, ih = self._working.size
        side = min(iw, ih)
        thumb_base = self._working.crop(
            ((iw - side) // 2, (ih - side) // 2,
             (iw + side) // 2, (ih + side) // 2)
        ).resize((64, 64), PILImage.BILINEAR).convert("RGB")
        for theme_key, theme_label, _c1, _c2 in _FRAME_THEMES:
            preview = thumb_base.copy().convert("RGBA")
            f = _frame_pil(64, 64, theme_key)
            if f:
                preview = PILImage.alpha_composite(preview, f)
            tex = _pil_to_texture(preview)
            pic = Gtk.Picture()
            pic.set_paintable(tex)
            pic.set_size_request(60, 60)
            pic.set_content_fit(Gtk.ContentFit.FILL)
            lbl = Gtk.Label(label=theme_label)
            lbl.add_css_class("caption")
            lbl.set_max_width_chars(9)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.set_margin_top(3); inner.set_margin_bottom(3)
            inner.set_margin_start(2); inner.set_margin_end(2)
            inner.append(pic)
            inner.append(lbl)
            fbtn = Gtk.ToggleButton()
            fbtn.add_css_class("flat")
            fbtn.set_child(inner)
            hid = fbtn.connect("toggled", self._on_frame_toggled, theme_key)
            self._frame_btns[theme_key] = fbtn
            self._frame_hids[theme_key] = hid
            rahmen_row.append(fbtn)
        rahmen_scroll.set_child(rahmen_row)
        self._sticker_sub_stack.add_named(rahmen_scroll, "rahmen")

        # Text page (entry + color row + add button)
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        text_box.set_margin_start(10); text_box.set_margin_end(10)
        text_box.set_margin_top(8);    text_box.set_margin_bottom(8)
        self._text_entry = Gtk.Entry()
        self._text_entry.set_placeholder_text("Text eingeben…")
        self._text_entry.set_hexpand(True)
        color_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._text_color_btns: dict[str, tuple] = {}
        self._text_color_hids: dict[str, int] = {}
        for cname, cval in [("Weiß", (255, 255, 255)), ("Schwarz", (0, 0, 0)),
                             ("Gelb", (255, 220, 0)),  ("Rot", (220, 30, 30))]:
            cb = Gtk.ToggleButton(label=cname)
            cb.add_css_class("flat")
            cb.set_active(cname == "Weiß")
            chid = cb.connect("toggled", self._on_text_color_toggled, cname, cval)
            self._text_color_btns[cname] = (cb, cval)
            self._text_color_hids[cname] = chid
            color_box.append(cb)
        add_btn = Gtk.Button(label="Hinzufügen")
        add_btn.add_css_class("flat")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_text_add)
        text_box.append(self._text_entry)
        text_box.append(color_box)
        text_box.append(add_btn)
        self._sticker_sub_stack.add_named(text_box, "text")

        self._sticker_sub_revealer = Gtk.Revealer()
        self._sticker_sub_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._sticker_sub_revealer.set_transition_duration(150)
        self._sticker_sub_revealer.set_reveal_child(False)
        self._sticker_sub_revealer.set_child(self._sticker_sub_stack)
        outer.append(self._sticker_sub_revealer)

        # ── Category nav (5 buttons) ──
        cat_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        cat_box.set_hexpand(True)
        cat_box.set_margin_top(4)
        cat_box.set_margin_bottom(4)

        self._sticker_cat_btns: dict[str, Gtk.ToggleButton] = {}
        self._sticker_cat_hids: dict[str, int] = {}
        for key, label in [("smileys", "Smileys"), ("emotions", "Emotions"),
                            ("symbole", "Symbole"), ("rahmen", "Rahmen"), ("text", "Text")]:
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("flat")
            btn.set_hexpand(True)
            hid = btn.connect("toggled", self._on_sticker_cat_toggled, key)
            self._sticker_cat_hids[key] = hid
            cat_box.append(btn)
            self._sticker_cat_btns[key] = btn

        outer.append(cat_box)
        return outer

    def _on_sticker_cat_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active():
            for k, b in self._sticker_cat_btns.items():
                if k != key and b.get_active():
                    b.handler_block(self._sticker_cat_hids[k])
                    b.set_active(False)
                    b.handler_unblock(self._sticker_cat_hids[k])
            self._sticker_sub_stack.set_visible_child_name(key)
            self._sticker_sub_revealer.set_reveal_child(True)
        else:
            self._sticker_sub_revealer.set_reveal_child(False)

    def _on_frame_toggled(self, btn: Gtk.ToggleButton, theme: str) -> None:
        if btn.get_active():
            for k, b in self._frame_btns.items():
                if k != theme and b.get_active():
                    b.handler_block(self._frame_hids[k])
                    b.set_active(False)
                    b.handler_unblock(self._frame_hids[k])
            self._frame_theme = theme
        else:
            self._frame_theme = None
        self._schedule_update()

    def _on_text_color_toggled(self, btn: Gtk.ToggleButton, cname: str, cval: tuple) -> None:
        if btn.get_active():
            self._text_color = cval
            for name, (b, _) in self._text_color_btns.items():
                if name != cname and b.get_active():
                    b.handler_block(self._text_color_hids[name])
                    b.set_active(False)
                    b.handler_unblock(self._text_color_hids[name])

    def _on_text_add(self, _btn: Gtk.Button) -> None:
        text = self._text_entry.get_text().strip()
        if not text:
            return
        try:
            pil = _make_text_pil(text, 60, self._text_color)
            self._set_sticker(pil)
        except Exception:
            pass

    def _build_panel_crop(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        self._crop_btn = Gtk.ToggleButton(label="✂  Zuschneiden")
        self._crop_btn.add_css_class("flat")
        self._crop_btn.connect("toggled", self._on_crop_toggled)
        self._crop_apply_btn = Gtk.Button(label="Übernehmen")
        self._crop_apply_btn.add_css_class("flat")
        self._crop_apply_btn.set_sensitive(False)
        self._crop_apply_btn.connect("clicked", self._apply_crop)
        self._crop_reset_btn = Gtk.Button(label="Zurücksetzen")
        self._crop_reset_btn.add_css_class("flat")
        self._crop_reset_btn.connect("clicked", self._reset_working)
        box.append(self._crop_btn)
        box.append(self._crop_apply_btn)
        box.append(self._crop_reset_btn)
        return box

    # ------------------------------------------------------------------
    # Nav toggle
    # ------------------------------------------------------------------

    def _on_nav_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active():
            for k, b in self._nav_btns.items():
                if k != key and b.get_active():
                    b.handler_block(self._nav_handler_ids[k])
                    b.set_active(False)
                    b.handler_unblock(self._nav_handler_ids[k])
            if key != "crop":
                self._deactivate_crop()
            self._active_panel = key
            self._panel_stack.set_visible_child_name(key)
            self._panel_revealer.set_reveal_child(True)
        else:
            self._active_panel = None
            self._panel_revealer.set_reveal_child(False)
            if key == "crop":
                self._deactivate_crop()

    def _deactivate_crop(self) -> None:
        self._crop_mode = False
        if hasattr(self, "_crop_btn"):
            self._crop_btn.handler_block_by_func(self._on_crop_toggled)
            self._crop_btn.set_active(False)
            self._crop_btn.handler_unblock_by_func(self._on_crop_toggled)
        self._crop_start = self._crop_current = None
        self._crop_rect_disp = None
        self._crop_active_handle = None
        self._draw_area.queue_draw()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_filter_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if not btn.get_active():
            return
        self._filter_mode = mode
        for m, b in self._filter_btns.items():
            if m != mode:
                b.set_active(False)
        self._schedule_update()

    def _on_crop_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._crop_mode = btn.get_active()
        if not self._crop_mode:
            self._crop_start = self._crop_current = None
            self._draw_area.queue_draw()

    def _apply_crop(self, _btn: Gtk.Button) -> None:
        if self._pending_crop:
            self._working = self._working.crop(self._pending_crop)
            self._pending_crop = None
            self._crop_start = self._crop_current = None
            self._crop_rect_disp = None
            self._crop_active_handle = None
            self._crop_btn.set_active(False)
            self._crop_apply_btn.set_sensitive(False)
            self._schedule_update()

    def _reset_working(self, _btn: Gtk.Button) -> None:
        self._working = self._original.copy()
        self._pending_crop = None
        self._crop_start = self._crop_current = None
        self._crop_rect_disp = None
        self._crop_active_handle = None
        self._crop_btn.set_active(False)
        self._crop_apply_btn.set_sensitive(False)
        self._schedule_update()

    def _on_sticker_clicked(self, _btn: Gtk.Button, source) -> None:
        self._set_sticker(source)

    def _set_sticker(self, source: "str | PILImage.Image | None") -> None:
        self._sticker_source = source
        self._sticker_rel = (0.5, 0.5)
        self._sticker_size_frac = 0.15
        self._sticker_del_rect = None
        self._schedule_update()

    def _on_slider(self, sc: Gtk.Scale, attr: str) -> None:
        setattr(self, attr, sc.get_value())
        self._schedule_update()

    # ── Sticker zoom (pinch to resize) ──

    def _on_sticker_zoom_begin(self, _g: Gtk.GestureZoom, _seq) -> None:
        self._sticker_zoom_start = self._sticker_size_frac

    def _on_sticker_zoom_scale(self, _g: Gtk.GestureZoom, scale_delta: float) -> None:
        if self._sticker_source is None:
            return
        self._sticker_size_frac = max(0.04, min(0.9, self._sticker_zoom_start * scale_delta))
        self._schedule_update()

    # ── Sticker delete (click on X badge) ──

    def _on_overlay_click(self, _g: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        if self._sticker_del_rect is None:
            return
        x1, y1, x2, y2 = self._sticker_del_rect
        if x1 <= x <= x2 and y1 <= y <= y2:
            self._set_sticker(None)

    # ------------------------------------------------------------------
    # Drag (crop rect + sticker move)
    # ------------------------------------------------------------------

    def _hit_crop_handle(self, x: float, y: float) -> str | None:
        if not self._crop_rect_disp:
            return None
        x1, y1, x2, y2 = self._crop_rect_disp
        for name, (cx, cy) in [("tl", (x1, y1)), ("tr", (x2, y1)),
                                ("bl", (x1, y2)), ("br", (x2, y2))]:
            if (x - cx) ** 2 + (y - cy) ** 2 <= 22 ** 2:
                return name
        return None

    def _on_drag_begin(self, _g: Gtk.GestureDrag, x: float, y: float) -> None:
        self._drag_sticker = False
        if self._crop_mode:
            handle = self._hit_crop_handle(x, y)
            if handle:
                self._crop_active_handle = handle
                self._crop_handle_orig = self._crop_rect_disp
                self._crop_start = None
            else:
                self._crop_active_handle = None
                self._crop_handle_orig = None
                self._crop_rect_disp = None
                self._pending_crop = None
                self._crop_apply_btn.set_sensitive(False)
                self._crop_start = (x, y)
                self._crop_current = (x, y)
        elif self._sticker_source is not None:
            self._drag_sticker = True
            self._drag_sx, self._drag_sy = x, y

    def _on_drag_update(self, _g: Gtk.GestureDrag, ox: float, oy: float) -> None:
        if self._crop_mode:
            if self._crop_active_handle and self._crop_handle_orig:
                x1, y1, x2, y2 = self._crop_handle_orig
                h = self._crop_active_handle
                if h == "tl":   x1 += ox; y1 += oy
                elif h == "tr": x2 += ox; y1 += oy
                elif h == "bl": x1 += ox; y2 += oy
                elif h == "br": x2 += ox; y2 += oy
                if abs(x2 - x1) > 16 and abs(y2 - y1) > 16:
                    self._crop_rect_disp = (min(x1, x2), min(y1, y2),
                                            max(x1, x2), max(y1, y2))
                self._draw_area.queue_draw()
            elif self._crop_start:
                sx, sy = self._crop_start
                self._crop_current = (sx + ox, sy + oy)
                self._draw_area.queue_draw()
        elif self._drag_sticker:
            self._move_sticker(self._drag_sx + ox, self._drag_sy + oy)

    def _on_drag_end(self, _g: Gtk.GestureDrag, ox: float, oy: float) -> None:
        if self._crop_mode:
            if self._crop_active_handle and self._crop_rect_disp:
                rect = self._display_to_image(*self._crop_rect_disp)
                if rect and (rect[2] - rect[0]) > 8 and (rect[3] - rect[1]) > 8:
                    self._pending_crop = rect
                    self._crop_apply_btn.set_sensitive(True)
                self._crop_active_handle = None
                self._crop_handle_orig = None
            elif self._crop_start:
                sx, sy = self._crop_start
                fx, fy = sx + ox, sy + oy
                x1, y1 = min(sx, fx), min(sy, fy)
                x2, y2 = max(sx, fx), max(sy, fy)
                if (x2 - x1) > 8 and (y2 - y1) > 8:
                    self._crop_rect_disp = (x1, y1, x2, y2)
                    rect = self._display_to_image(x1, y1, x2, y2)
                    if rect:
                        self._pending_crop = rect
                        self._crop_apply_btn.set_sensitive(True)
                self._crop_start = self._crop_current = None
            self._draw_area.queue_draw()
        self._drag_sticker = False

    def _move_sticker(self, px: float, py: float) -> None:
        dw = self._draw_area.get_width()
        dh = self._draw_area.get_height()
        iw, ih = self._working.size
        if dw <= 0 or dh <= 0:
            return
        scale = min(dw / iw, dh / ih)
        ox = (dw - iw * scale) / 2
        oy = (dh - ih * scale) / 2
        self._sticker_rel = (
            max(0.0, min(1.0, (px - ox) / (iw * scale))),
            max(0.0, min(1.0, (py - oy) / (ih * scale))),
        )
        self._schedule_update()

    def _display_to_image(
        self, x1: float, y1: float, x2: float, y2: float
    ) -> tuple[int, int, int, int] | None:
        dw = self._draw_area.get_width()
        dh = self._draw_area.get_height()
        iw, ih = self._working.size
        if dw <= 0 or dh <= 0:
            return None
        scale = min(dw / iw, dh / ih)
        ox = (dw - iw * scale) / 2
        oy = (dh - ih * scale) / 2

        def clip(v: float, lo: int, hi: int) -> int:
            return max(lo, min(hi, int(v)))

        return (
            clip((min(x1, x2) - ox) / scale, 0, iw),
            clip((min(y1, y2) - oy) / scale, 0, ih),
            clip((max(x1, x2) - ox) / scale, 0, iw),
            clip((max(y1, y2) - oy) / scale, 0, ih),
        )

    # ------------------------------------------------------------------
    # Overlay drawing (cairo)
    # ------------------------------------------------------------------

    def _draw_crop_rect(self, cr: cairo.Context, width: int, height: int,
                        rx: float, ry: float, rw: float, rh: float,
                        handles: bool) -> None:
        cr.set_source_rgba(0, 0, 0, 0.50)
        cr.rectangle(0, 0, width, height)
        cr.rectangle(rx, ry, rw, rh)
        cr.set_fill_rule(cairo.FillRule.EVEN_ODD)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.92)
        cr.set_line_width(1.5)
        cr.set_dash([])
        cr.rectangle(rx, ry, rw, rh)
        cr.stroke()
        if handles:
            for cx, cy in [(rx, ry), (rx + rw, ry), (rx, ry + rh), (rx + rw, ry + rh)]:
                cr.set_source_rgba(1, 1, 1, 0.96)
                cr.arc(cx, cy, 9.0, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(0.15, 0.15, 0.15, 0.70)
                cr.arc(cx, cy, 9.0, 0, 2 * math.pi)
                cr.set_line_width(1.5)
                cr.stroke()

    def _draw_overlay(self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int) -> None:
        if self._crop_mode:
            if self._crop_start and self._crop_current:
                x1, y1 = self._crop_start
                x2, y2 = self._crop_current
                self._draw_crop_rect(cr, width, height, min(x1, x2), min(y1, y2),
                                     abs(x2 - x1), abs(y2 - y1), handles=False)
            elif self._crop_rect_disp:
                x1, y1, x2, y2 = self._crop_rect_disp
                self._draw_crop_rect(cr, width, height, x1, y1, x2 - x1, y2 - y1, handles=True)

        if self._sticker_source is not None and not self._crop_mode:
            iw, ih = self._working.size
            scale = min(width / iw, height / ih)
            ox = (width - iw * scale) / 2
            oy = (height - ih * scale) / 2
            scx = ox + self._sticker_rel[0] * iw * scale
            scy = oy + self._sticker_rel[1] * ih * scale
            half = self._sticker_size_frac * iw * scale / 2
            # Delete button: red circle with × at top-right corner of sticker
            R = 12.0
            del_cx = scx + half
            del_cy = scy - half
            self._sticker_del_rect = (del_cx - R, del_cy - R, del_cx + R, del_cy + R)
            cr.set_source_rgba(0.88, 0.18, 0.18, 0.90)
            cr.arc(del_cx, del_cy, R, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.set_line_width(2.0)
            d = 5.0
            cr.move_to(del_cx - d, del_cy - d)
            cr.line_to(del_cx + d, del_cy + d)
            cr.move_to(del_cx + d, del_cy - d)
            cr.line_to(del_cx - d, del_cy + d)
            cr.stroke()

    # ------------------------------------------------------------------
    # Image processing
    # ------------------------------------------------------------------

    def _apply_edits(self, img: "PILImage.Image") -> "PILImage.Image":
        result = img.convert("RGB")
        fn = next((f for k, _l, f in _FILTER_DEFS if k == self._filter_mode and f), None)
        if fn:
            result = fn(result)
        result = ImageEnhance.Brightness(result).enhance(self._brightness)
        result = ImageEnhance.Contrast(result).enhance(self._contrast)
        if not (self._red == self._green == self._blue == 1.0):
            r, g, b = result.split()
            r = r.point(lambda x: min(255, int(x * self._red)))
            g = g.point(lambda x: min(255, int(x * self._green)))
            b = b.point(lambda x: min(255, int(x * self._blue)))
            result = PILImage.merge("RGB", (r, g, b))
        if self._sticker_source is not None:
            iw, ih = result.size
            px = max(32, int(self._sticker_size_frac * iw))
            if isinstance(self._sticker_source, str):
                si = _get_emoji_pil(self._sticker_source, px)
            else:
                si = self._sticker_source.resize((px, int(px * self._sticker_source.height / max(1, self._sticker_source.width))), PILImage.LANCZOS)
            sx = int(self._sticker_rel[0] * iw - si.width / 2)
            sy = int(self._sticker_rel[1] * ih - si.height / 2)
            canvas = PILImage.new("RGBA", (iw, ih), (0, 0, 0, 0))
            canvas.paste(si, (sx, sy))
            result = PILImage.alpha_composite(result.convert("RGBA"), canvas).convert("RGB")
        if self._frame_theme is not None:
            iw, ih = result.size
            frame = _frame_pil(iw, ih, self._frame_theme)
            if frame:
                result = PILImage.alpha_composite(result.convert("RGBA"), frame).convert("RGB")
        return result

    def _schedule_update(self) -> None:
        if self._update_id is not None:
            GLib.source_remove(self._update_id)
        self._update_id = GLib.timeout_add(90, self._do_update)

    def _do_update(self) -> bool:
        self._update_id = None
        pw = self._preview.get_width() or 800
        ph = self._preview.get_height() or 600
        # Downscale working copy first so _apply_edits operates on a small image
        thumb = self._working.copy()
        thumb.thumbnail((pw * 2, ph * 2), PILImage.BILINEAR)
        img = self._apply_edits(thumb)
        self._preview.set_paintable(_pil_to_texture(img))
        self._draw_area.queue_draw()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_as_new(self) -> str:
        result = self._apply_edits(self._working)
        orig = Path(self._item.path)
        ext = orig.suffix.lower()
        if ext in (".heic", ".heif", ".avif"):
            ext = ".jpg"
        i = 1
        while True:
            dest = orig.parent / f"{orig.stem}_edit_{i}{ext}"
            if not dest.exists():
                break
            i += 1
        if ext in (".jpg", ".jpeg"):
            result.convert("RGB").save(str(dest), "JPEG", quality=95)
        elif ext == ".png":
            result.save(str(dest), "PNG")
        else:
            dest = orig.parent / f"{orig.stem}_edit_{i}.jpg"
            result.convert("RGB").save(str(dest), "JPEG", quality=95)
        return str(dest)


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------

class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, parent: GalleryWindow) -> None:
        super().__init__(transient_for=parent, modal=True, title=parent._("Settings"))
        self.parent_window = parent
        self.settings = Settings(**parent.settings.__dict__)
        self._build()

    def _(self, text: str) -> str:
        return self.parent_window._(text)

    def _build(self) -> None:
        media = Adw.PreferencesPage(title=self._("Media folders"), icon_name="folder-pictures-symbolic")
        self.add(media)
        group = Adw.PreferencesGroup(title=self._("Media folders"))
        media.add(group)

        for attr, title in [
            ("photos_dir", "Photos folder"),
            ("pictures_dir", "Pictures folder"),
            ("videos_dir", "Videos folder"),
            ("screenshots_dir", "Screenshots folder"),
        ]:
            group.add(self._folder_row(attr, title))

        extra = Adw.PreferencesGroup(title=self._("Optional locations"))
        media.add(extra)
        add = Gtk.Button(label=self._("Add location"), icon_name="list-add-symbolic")
        add.connect("clicked", self._add_location)
        extra.add(add)
        for path in self.settings.extra_locations:
            row = Adw.ActionRow(title=Path(path).name or path, subtitle=path)
            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            remove.connect("clicked", self._remove_location, path)
            row.add_suffix(remove)
            extra.add(row)
        self.extra_group = extra

        app = Adw.PreferencesPage(title=self._("Appearance"), icon_name="preferences-desktop-appearance-symbolic")
        self.add(app)
        theme_group = Adw.PreferencesGroup(title=self._("Appearance"))
        app.add(theme_group)
        theme_group.add(self._combo_row("theme", "Theme", [("system", "System"), ("light", "Light"), ("dark", "Dark")]))
        theme_group.add(self._combo_row("language", "Language", [("system", "Use system language"), ("en", "English"), ("de", "German")]))

        grid_group = Adw.PreferencesGroup(title=self._("Grid"))
        app.add(grid_group)
        columns = Adw.SpinRow.new_with_range(2, 10, 1)
        columns.set_title(self._("Photos per row"))
        columns.set_value(self.settings.grid_columns)
        columns.connect("notify::value", self._columns_changed)
        grid_group.add(columns)

        thumb_group = Adw.PreferencesGroup(title=self._("Thumbnails"))
        app.add(thumb_group)
        clear = Gtk.Button(label=self._("Clear thumbnail cache"), icon_name="edit-clear-symbolic")
        clear.connect("clicked", self._clear_thumbnails)
        thumb_group.add(clear)

        video_group = Adw.PreferencesGroup(title=self._("Video"))
        app.add(video_group)
        command = Adw.EntryRow(title=self._("External player command"))
        command.set_text(self.settings.external_video_player)
        command.set_show_apply_button(True)
        command.connect("apply", self._entry_apply, "external_video_player")
        video_group.add(command)

        hint = Adw.ActionRow(title=self._("Leave empty to use built-in playback"))
        video_group.add(hint)

        save_group = Adw.PreferencesGroup()
        app.add(save_group)
        save = Gtk.Button(label=self._("Settings"), icon_name="document-save-symbolic")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        save_group.add(save)

    def _folder_row(self, attr: str, title: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=self._(title), subtitle=getattr(self.settings, attr))
        choose = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        choose.set_tooltip_text(self._("Choose folder"))
        choose.connect("clicked", self._choose_folder, attr, row)
        row.add_suffix(choose)
        return row

    def _combo_row(self, attr: str, title: str, values: list[tuple[str, str]]) -> Adw.ComboRow:
        store = Gtk.StringList()
        active = 0
        current = getattr(self.settings, attr)
        for i, (value, label) in enumerate(values):
            store.append(self._(label))
            if value == current:
                active = i
        row = Adw.ComboRow(title=self._(title), model=store, selected=active)
        row.values = [value for value, _label in values]
        row.connect("notify::selected", self._combo_changed, attr)
        return row

    def _combo_changed(self, row: Adw.ComboRow, _param, attr: str) -> None:
        setattr(self.settings, attr, row.values[row.get_selected()])
        self.parent_window.apply_settings(self.settings)

    def _entry_apply(self, row: Adw.EntryRow, attr: str) -> None:
        setattr(self.settings, attr, row.get_text())
        self.parent_window.apply_settings(self.settings)

    def _columns_changed(self, row: Adw.SpinRow, _param) -> None:
        self.settings.grid_columns = int(row.get_value())
        self.parent_window.apply_settings(self.settings)

    def _choose_folder(self, _button: Gtk.Button, attr: str, row: Adw.ActionRow) -> None:
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._folder_response, attr, row)
        chooser.show()

    def _folder_response(self, chooser: Gtk.FileChooserNative, response: int, attr: str, row: Adw.ActionRow) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            setattr(self.settings, attr, path)
            row.set_subtitle(path)
            self.parent_window.apply_settings(self.settings)
        chooser.destroy()

    def _add_location(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._add_location_response)
        chooser.show()

    def _add_location_response(self, chooser: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            if path and path not in self.settings.extra_locations:
                self.settings.extra_locations.append(path)
                self.parent_window.apply_settings(self.settings)
                self.close()
                SettingsWindow(self.parent_window).present()
        chooser.destroy()

    def _remove_location(self, _button: Gtk.Button, path: str) -> None:
        self.settings.extra_locations = [item for item in self.settings.extra_locations if item != path]
        self.parent_window.apply_settings(self.settings)
        self.close()
        SettingsWindow(self.parent_window).present()

    def _clear_thumbnails(self, _button: Gtk.Button) -> None:
        self.parent_window.thumbnailer.clear()
        self.parent_window.refresh(scan=True)

    def _save(self, _button: Gtk.Button) -> None:
        self.parent_window.apply_settings(self.settings)
        self.close()


# ---------------------------------------------------------------------------
# Viewer window
# ---------------------------------------------------------------------------

class ViewerWindow(Adw.ApplicationWindow):
    def __init__(self, parent: GalleryWindow, items: list[MediaItem], index: int, external_player: str = "") -> None:
        super().__init__(application=parent.get_application(), transient_for=parent, title=items[index].name)
        self.set_default_size(1000, 720)
        self.parent_window = parent
        self.items = items
        self.index = index
        self.external_player = external_player
        self.last_gesture_nav_at = 0
        self.zoom_scale = 1.0
        self.zoom_start_scale = 1.0
        self.zoom_view: Gtk.Picture | None = None
        self.zoom_scroller: Gtk.ScrolledWindow | None = None
        self.toolbar = Adw.ToolbarView()
        self.set_content(self.toolbar)

        header = Adw.HeaderBar()
        self.header = header

        self.delete_button = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self.delete_button.set_tooltip_text(parent._("Delete"))
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._confirm_delete_current)
        header.pack_start(self.delete_button)

        self.info_button = Gtk.Button.new_from_icon_name("help-about-symbolic")
        self.info_button.set_tooltip_text(parent._("Info"))
        self.info_button.connect("clicked", self._show_info)
        header.pack_start(self.info_button)

        self.edit_button = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        self.edit_button.set_tooltip_text(parent._("Edit"))
        self.edit_button.connect("clicked", self._enter_edit_mode)
        header.pack_end(self.edit_button)

        self.cancel_edit_button = Gtk.Button.new_with_label(parent._("Cancel"))
        self.cancel_edit_button.connect("clicked", self._exit_edit_mode)
        self.cancel_edit_button.set_visible(False)
        header.pack_start(self.cancel_edit_button)

        self.save_edit_button = Gtk.Button.new_with_label(parent._("Save"))
        self.save_edit_button.add_css_class("suggested-action")
        self.save_edit_button.connect("clicked", self._save_edit)
        self.save_edit_button.set_visible(False)
        header.pack_end(self.save_edit_button)

        self.fullscreen_btn = Gtk.Button.new_from_icon_name("view-restore-symbolic")
        self.fullscreen_btn.set_tooltip_text(parent._("Exit fullscreen"))
        self.fullscreen_btn.connect("clicked", self._toggle_fullscreen)
        header.pack_end(self.fullscreen_btn)

        self._editor: EditorView | None = None
        self.toolbar.add_top_bar(header)

        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.toolbar.set_content(self.stack)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        swipe = Gtk.GestureSwipe()
        swipe.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        swipe.connect("swipe", self._on_swipe)
        self.stack.add_controller(swipe)
        drag = Gtk.GestureDrag()
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-end", self._on_drag_end)
        self.stack.add_controller(drag)
        zoom = Gtk.GestureZoom()
        zoom.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        zoom.connect("begin", self._on_zoom_begin)
        zoom.connect("scale-changed", self._on_zoom_scale_changed)
        self.stack.add_controller(zoom)
        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_viewer_pressed)
        self.stack.add_controller(click)
        self.fullscreen()
        self.show_item()

    def show_item(self) -> None:
        child = self.stack.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.stack.remove(child)
            child = next_child
        item = self.items[self.index]
        self.set_title(item.name)
        self._reset_zoom()
        self.edit_button.set_visible(not item.is_video and _PIL_OK)
        self.fullscreen_btn.set_visible(item.is_video)
        if item.is_video:
            self.zoom_view = None
            self.zoom_scroller = None
            video = Gtk.Video.new_for_file(Gio.File.new_for_path(item.path))
            video.set_autoplay(True)
            self.stack.add_child(video)
        else:
            picture = Gtk.Picture.new_for_filename(item.path)
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            picture.set_hexpand(True)
            picture.set_vexpand(True)
            scroller = Gtk.ScrolledWindow()
            scroller.set_hexpand(True)
            scroller.set_vexpand(True)
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroller.set_child(picture)
            self.zoom_view = picture
            self.zoom_scroller = scroller
            self.stack.add_child(scroller)

    def previous(self) -> None:
        if self.items:
            self.index = (self.index - 1) % len(self.items)
            self.show_item()

    def next(self) -> None:
        if self.items:
            self.index = (self.index + 1) % len(self.items)
            self.show_item()

    def _on_key(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType) -> bool:
        if self._editor is not None:
            if keyval == Gdk.KEY_Escape:
                self._exit_edit_mode()
                return True
            return False
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up):
            self.previous()
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_space):
            self.next()
            return True
        if keyval == Gdk.KEY_F11:
            self._toggle_fullscreen()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.props.fullscreened:
                self._toggle_fullscreen()
            else:
                self.close()
            return True
        return False

    def _on_swipe(self, _gesture: Gtk.GestureSwipe, velocity_x: float, _velocity_y: float) -> None:
        if self._editor is not None:
            return
        self._navigate_from_horizontal_motion(velocity_x, 0)

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if self._editor is not None:
            return
        self._navigate_from_horizontal_motion(offset_x, offset_y)

    def _navigate_from_horizontal_motion(self, x: float, y: float) -> None:
        if self.zoom_scale > 1.05:
            return
        if abs(x) < 60 or abs(x) <= abs(y):
            return
        now = GLib.get_monotonic_time()
        if now - self.last_gesture_nav_at < 300_000:
            return
        self.last_gesture_nav_at = now
        if x > 0:
            self.previous()
        else:
            self.next()

    def _on_zoom_begin(self, gesture: Gtk.GestureZoom, _sequence) -> None:
        self.zoom_start_scale = self.zoom_scale
        self._zoom_anchor: tuple[float, float, float, float] | None = None
        if self.zoom_scroller:
            ok, bx, by = gesture.get_bounding_box_center()
            if ok:
                hadj = self.zoom_scroller.get_hadjustment()
                vadj = self.zoom_scroller.get_vadjustment()
                s = max(self.zoom_scale, 0.01)
                self._zoom_anchor = (bx, by, (hadj.get_value() + bx) / s, (vadj.get_value() + by) / s)

    def _on_zoom_scale_changed(self, _gesture: Gtk.GestureZoom, scale_delta: float) -> None:
        self._set_zoom(self.zoom_start_scale * scale_delta)
        anchor = getattr(self, "_zoom_anchor", None)
        if self.zoom_scroller and anchor and self.zoom_scale > 1.01:
            vp_x, vp_y, cx, cy = anchor
            scale = self.zoom_scale
            scroller = self.zoom_scroller

            def _apply() -> bool:
                self._set_adjustment_for_focus(scroller.get_hadjustment(), cx, scale, vp_x)
                self._set_adjustment_for_focus(scroller.get_vadjustment(), cy, scale, vp_y)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_apply)

    def _on_viewer_pressed(self, _gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float) -> None:
        if n_press == 2:
            self._reset_zoom()

    def _set_zoom(self, scale: float) -> None:
        self.zoom_scale = min(max(scale, 1.0), 6.0)
        self._apply_zoom()

    def _reset_zoom(self) -> None:
        self.zoom_scale = 1.0
        self.zoom_start_scale = 1.0
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        if not self.zoom_view or not self.zoom_scroller:
            return
        if self.zoom_scale <= 1.01:
            self.zoom_view.set_size_request(-1, -1)
            return
        width = max(self.zoom_scroller.get_width(), self.get_width(), 1)
        height = max(self.zoom_scroller.get_height(), self.get_height(), 1)
        self.zoom_view.set_size_request(int(width * self.zoom_scale), int(height * self.zoom_scale))

    def _set_adjustment_for_focus(self, adjustment: Gtk.Adjustment, content_pos: float, scale: float, focus_pos: float) -> None:
        target = content_pos * scale - focus_pos
        lower = adjustment.get_lower()
        upper = max(lower, adjustment.get_upper() - adjustment.get_page_size())
        adjustment.set_value(min(max(target, lower), upper))

    def _enter_edit_mode(self, _button=None) -> None:
        if not _PIL_OK:
            return
        item = self.items[self.index]
        if item.is_video:
            return
        self.header.set_show_end_title_buttons(False)
        self.header.set_show_start_title_buttons(False)
        self.delete_button.set_visible(False)
        self.info_button.set_visible(False)
        self.edit_button.set_visible(False)
        self.cancel_edit_button.set_visible(True)
        self.save_edit_button.set_visible(True)
        child = self.stack.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.stack.remove(child)
            child = nxt
        self._editor = EditorView(item)
        self.stack.add_child(self._editor)

    def _exit_edit_mode(self, _button=None) -> None:
        self._editor = None
        self.header.set_show_end_title_buttons(True)
        self.header.set_show_start_title_buttons(True)
        self.cancel_edit_button.set_visible(False)
        self.save_edit_button.set_visible(False)
        self.delete_button.set_visible(True)
        self.info_button.set_visible(True)
        self.show_item()

    def _save_edit(self, _button: Gtk.Button) -> None:
        if self._editor is None:
            return
        try:
            self._editor.save_as_new()
        except Exception:
            self.parent_window._set_status(self.parent_window._("Could not save edited image"))
            return
        self.parent_window.refresh(scan=True)
        self._exit_edit_mode()

    def _toggle_fullscreen(self, _btn=None) -> None:
        if self.props.fullscreened:
            self.unfullscreen()
            self.fullscreen_btn.set_icon_name("view-fullscreen-symbolic")
            self.fullscreen_btn.set_tooltip_text(self.parent_window._("Fullscreen"))
        else:
            self.fullscreen()
            self.fullscreen_btn.set_icon_name("view-restore-symbolic")
            self.fullscreen_btn.set_tooltip_text(self.parent_window._("Exit fullscreen"))

    def _confirm_delete_current(self, _button: Gtk.Button) -> None:
        dialog = Adw.AlertDialog(
            heading=self.parent_window._("Delete media?"),
            body=self.parent_window._("Delete this item from the gallery?"),
        )
        dialog.add_response("cancel", self.parent_window._("Cancel"))
        dialog.add_response("delete", self.parent_window._("Delete"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.choose(self, None, self._delete_dialog_finished, None)

    def _delete_dialog_finished(self, dialog: Adw.AlertDialog, result: Gio.AsyncResult, _data) -> None:
        if dialog.choose_finish(result) == "delete":
            self._delete_current_item()

    def _delete_current_item(self) -> None:
        if not self.items:
            self.close()
            return
        item = self.items[self.index]
        try:
            Gio.File.new_for_path(item.path).trash(None)
        except GLib.Error:
            self.parent_window._set_status(self.parent_window._("Could not complete action"))
            return
        if item.thumb_path:
            try:
                Path(item.thumb_path).unlink(missing_ok=True)
            except OSError:
                pass
        self.parent_window.database.delete_path(item.path)
        self.items.pop(self.index)
        self.parent_window.refresh(scan=False)
        if not self.items:
            self.close()
            return
        self.index = min(self.index, len(self.items) - 1)
        self.show_item()

    def _show_info(self, _button: Gtk.Button) -> None:
        item = self.items[self.index]
        _ = self.parent_window._

        rows: list[tuple[str, str]] = [
            (_("Name"), item.name),
            (_("Folder"), item.folder),
            (_("Size"), _fmt_size(item.size)),
            (_("Modified"), _fmt_date(item.mtime)),
        ]
        if not item.is_video:
            dims = _image_dimensions(item.path)
            if dims:
                rows.append((_("Dimensions"), dims))

        grid = Gtk.Grid()
        grid.set_column_spacing(20)
        grid.set_row_spacing(8)
        grid.set_margin_top(14)
        grid.set_margin_bottom(14)
        grid.set_margin_start(16)
        grid.set_margin_end(16)
        for i, (key, value) in enumerate(rows):
            key_lbl = Gtk.Label(label=key, xalign=1.0)
            key_lbl.add_css_class("dim-label")
            val_lbl = Gtk.Label(label=value, xalign=0.0)
            val_lbl.set_selectable(True)
            val_lbl.set_wrap(True)
            val_lbl.set_max_width_chars(32)
            grid.attach(key_lbl, 0, i, 1, 1)
            grid.attach(val_lbl, 1, i, 1, 1)

        popover = Gtk.Popover()
        popover.set_parent(self.info_button)
        popover.set_child(grid)
        popover.popup()


def main() -> int:
    app = GalleryApplication()
    return app.run()
