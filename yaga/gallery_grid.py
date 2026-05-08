from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from .models import MediaItem
from .nextcloud import is_nc_path

if TYPE_CHECKING:
    from .app import GalleryWindow


class MediaRow(GObject.Object):
    """One cell in the gallery grid: either a folder or a media item."""

    __gtype_name__ = "YagaMediaRow"

    def __init__(self) -> None:
        super().__init__()
        self.media_item: MediaItem | None = None
        self.folder_path: str | None = None
        self.folder_count: int = 0
        self.folder_thumbs: list[str] = []
        self.selected: bool = False

    @classmethod
    def from_media(cls, item: MediaItem, selected: bool = False) -> "MediaRow":
        row = cls()
        row.media_item = item
        row.selected = selected
        return row

    @classmethod
    def from_folder(cls, folder: str, count: int, thumbs: list[str]) -> "MediaRow":
        row = cls()
        row.folder_path = folder
        row.folder_count = count
        row.folder_thumbs = thumbs
        return row

    @property
    def is_folder(self) -> bool:
        return self.folder_path is not None


class GalleryGrid(Gtk.Overlay):
    def __init__(self, owner: "GalleryWindow") -> None:
        super().__init__()
        self.owner = owner
        self.set_hexpand(True)
        self.set_vexpand(True)

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

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_hexpand(True)
        self.scroller.set_vexpand(True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.grid_view)

        self.empty_label = Gtk.Label()
        self.empty_label.set_halign(Gtk.Align.CENTER)
        self.empty_label.set_valign(Gtk.Align.CENTER)
        self.empty_label.add_css_class("dim-label")
        self.empty_label.add_css_class("title-3")
        self.empty_label.set_visible(False)

        self.set_child(self.scroller)
        self.add_overlay(self.empty_label)

    def set_columns(self, columns: int) -> None:
        columns = min(max(int(columns), 2), 10)
        self.grid_view.set_min_columns(columns)
        self.grid_view.set_max_columns(columns)

    def clear(self) -> None:
        self.item_store.remove_all()
        self.empty_label.set_visible(False)

    def append_folder(self, folder: str, count: int, thumbs: list[str]) -> None:
        self.item_store.append(MediaRow.from_folder(folder, count, thumbs))

    def append_media(self, item: MediaItem, selected: bool = False) -> None:
        self.item_store.append(MediaRow.from_media(item, selected))

    def set_empty(self, text: str, visible: bool) -> None:
        self.empty_label.set_label(text)
        self.empty_label.set_visible(visible)

    def get_vadjustment(self) -> Gtk.Adjustment:
        return self.scroller.get_vadjustment()

    def update_item_thumb(self, path: str, thumb_path: str) -> bool:
        n = self.item_store.get_n_items()
        for pos in range(n):
            row = self.item_store.get_item(pos)
            if not row.is_folder and row.media_item and row.media_item.path == path:
                updated = dataclasses.replace(row.media_item, thumb_path=thumb_path)
                self.item_store.splice(pos, 1, [MediaRow.from_media(updated, row.selected)])
                return True
        return False

    def _on_item_setup(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        single_pic = Gtk.Picture()
        single_pic.set_content_fit(Gtk.ContentFit.COVER)
        single_pic.set_hexpand(True)
        single_pic.set_vexpand(True)

        pic_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        pic_row1.set_vexpand(True)
        pic_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        pic_row2.set_vexpand(True)
        preview_pics: list[Gtk.Picture] = []
        for i in range(4):
            picture = Gtk.Picture()
            picture.set_content_fit(Gtk.ContentFit.COVER)
            picture.set_hexpand(True)
            picture.set_vexpand(True)
            preview_pics.append(picture)
            (pic_row1 if i < 2 else pic_row2).append(picture)

        pic_grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pic_grid.set_hexpand(True)
        pic_grid.set_vexpand(True)
        pic_grid.append(pic_row1)
        pic_grid.append(pic_row2)
        pic_grid.set_visible(False)

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

        check = Gtk.Image.new_from_icon_name("checkbox-symbolic")
        check.set_halign(Gtk.Align.END)
        check.set_valign(Gtk.Align.START)
        check.add_css_class("sel-check")
        check.set_visible(False)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(single_pic)
        overlay.add_overlay(pic_grid)
        overlay.add_overlay(badge)
        overlay.add_overlay(folder_label)
        overlay.add_overlay(check)

        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("gallery-tile")
        button.set_hexpand(True)
        button.set_vexpand(True)
        button.set_child(overlay)

        button._single_pic = single_pic        # type: ignore[attr-defined]
        button._preview_pics = preview_pics    # type: ignore[attr-defined]
        button._pic_grid = pic_grid            # type: ignore[attr-defined]
        button._badge = badge                  # type: ignore[attr-defined]
        button._folder_label = folder_label    # type: ignore[attr-defined]
        button._check = check                  # type: ignore[attr-defined]

        button.connect("clicked", self._on_tile_clicked, list_item)

        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", self._on_tile_right_click, list_item)
        button.add_controller(gesture)

        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", self._on_tile_long_press, list_item)
        button.add_controller(long_press)

        swipe = Gtk.GestureSwipe()
        swipe.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        swipe.connect("swipe", self._on_tile_swipe)
        button.add_controller(swipe)

        list_item.set_child(button)

    def _on_item_bind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        row: MediaRow = list_item.get_item()
        button = list_item.get_child()
        single_pic: Gtk.Picture = button._single_pic
        preview_pics: list[Gtk.Picture] = button._preview_pics
        pic_grid: Gtk.Widget = button._pic_grid
        badge: Gtk.Image = button._badge
        folder_label: Gtk.Label = button._folder_label
        check: Gtk.Image = button._check
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())

        if row.is_folder:
            valid_thumbs = [t for t in row.folder_thumbs if t and Path(t).exists()]
            if len(valid_thumbs) >= 2:
                single_pic.set_visible(False)
                pic_grid.set_visible(True)
                for i, picture in enumerate(preview_pics):
                    if i < len(valid_thumbs):
                        picture.set_filename(valid_thumbs[i])
                        picture.set_visible(True)
                    else:
                        picture.set_paintable(None)
                        picture.set_visible(False)
            else:
                single_pic.set_visible(True)
                pic_grid.set_visible(False)
                if valid_thumbs:
                    single_pic.set_filename(valid_thumbs[0])
                else:
                    single_pic.set_paintable(
                        icon_theme.lookup_icon(
                            "folder-pictures-symbolic", None, 96, 1,
                            Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE,
                        )
                    )
            label = row.folder_path.rsplit("/", 1)[-1] if row.folder_path != "/" else "/"
            folder_label.set_label(label)
            folder_label.set_visible(True)
            badge.set_visible(False)
            check.set_visible(False)
            return

        single_pic.set_visible(True)
        pic_grid.set_visible(False)
        item = row.media_item
        assert item is not None
        if item.thumb_path and Path(item.thumb_path).exists():
            single_pic.set_filename(item.thumb_path)
        elif is_nc_path(item.path):
            single_pic.set_paintable(
                icon_theme.lookup_icon(
                    "image-x-generic-symbolic", None, 96, 1,
                    Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE,
                )
            )
        else:
            single_pic.set_filename(item.path)
        badge.set_visible(item.is_video and not self.owner._selection_mode)
        folder_label.set_visible(False)
        if self.owner._selection_mode:
            check.set_visible(True)
            if row.selected:
                check.set_from_icon_name("checkbox-checked-symbolic")
                check.add_css_class("checked")
            else:
                check.set_from_icon_name("checkbox-symbolic")
                check.remove_css_class("checked")
        else:
            check.set_visible(False)

    def _on_item_unbind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        button = list_item.get_child()
        button._single_pic.set_paintable(None)
        for picture in button._preview_pics:
            picture.set_paintable(None)

    def _on_tile_clicked(self, _button: Gtk.Button, list_item: Gtk.ListItem) -> None:
        row: MediaRow | None = list_item.get_item()
        if row is None:
            return
        if self.owner._selection_mode:
            if not row.is_folder and row.media_item is not None:
                self.owner._toggle_selection(row.media_item.path)
            return
        if row.is_folder:
            self.owner._open_folder(None, row.folder_path)
        else:
            self.owner._open_item(None, row.media_item)

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
        self.owner._show_context_menu(gesture, 1, x, y, row.media_item, list_item.get_child())

    def _on_tile_long_press(self, _gesture, _x, _y, list_item: Gtk.ListItem) -> None:
        row: MediaRow | None = list_item.get_item()
        if row is None or row.is_folder or row.media_item is None:
            return
        if not self.owner._selection_mode:
            self.owner._enter_selection_mode()
        self.owner._toggle_selection(row.media_item.path)

    def _on_tile_swipe(self, gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        self.owner._on_folder_swipe(gesture, velocity_x, velocity_y)
