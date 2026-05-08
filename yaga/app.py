from __future__ import annotations

import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import signal
import shlex
import subprocess
import sys
import threading
import shutil
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk

from . import APP_ID, APP_NAME
from .config import DEBUG_LOG_PATH, Settings
from .database import Database
from .gallery_grid import GalleryGrid
from .i18n import Translator
from .models import MediaItem
from .settings_window import SettingsWindow
from .scanner import MediaScanner
from .thumbnails import Thumbnailer
from .viewer import ViewerWindow

LOGGER = logging.getLogger(__name__)


def _configure_debug_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        return
    DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(DEBUG_LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)


def _enable_thread_dump_signal() -> None:
    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
        except RuntimeError:
            LOGGER.debug("Could not register SIGUSR1 thread dump handler", exc_info=True)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class GalleryApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        _configure_debug_logging()
        _enable_thread_dump_signal()
        GLib.set_application_name(APP_NAME)
        self.connect("activate", self.on_activate)

    def on_activate(self, _app: Adw.Application) -> None:
        icons_dir = Path(__file__).parent / "data" / "icons"
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icons_dir))
        window = GalleryWindow(self)
        window.present()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class GalleryWindow(Adw.ApplicationWindow):
    def __init__(self, app: GalleryApplication) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1120, 760)
        self.set_icon_name(APP_ID)

        self.settings = Settings.load()
        self.translator = Translator(self.settings.language)
        self.database = Database()
        self.thumbnailer = Thumbnailer()
        self.scanner = MediaScanner(self.database, self.thumbnailer)
        self.category = self._first_existing_category()
        self.current_folder: str | None = None
        self.current_items: list[MediaItem] = []
        self.category_buttons: dict[str, Gtk.ToggleButton] = {}
        self._selection_mode: bool = False
        self._selected_paths: set[str] = set()
        self._nc_spinner: Gtk.Spinner | None = None
        self._nc_broken_img: Gtk.Image | None = None
        self._nc_folder_sync_generation = 0
        self._date_group_modes: dict[str, str] = {}

        # Track last-rendered view so we can preserve scroll position on refresh
        self._last_render_key: tuple[str, str | None] | None = None

        # Dynamic tile-size CSS (updated via tick callback whenever the scroller resizes)
        self._tile_css = Gtk.CssProvider()
        self._grid_width = 0

        self._apply_theme()
        self._load_css()
        self._build_ui()
        Adw.StyleManager.get_default().connect("notify::dark", self._on_system_theme_changed)
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

        self.settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        self.settings_button.set_tooltip_text(self._("Settings"))
        self.settings_button.connect("clicked", self._open_settings)
        self.header.pack_start(self.settings_button)

        self.sort_button = Gtk.MenuButton(icon_name="view-sort-descending-symbolic")
        self.sort_button.set_tooltip_text(self._("Sort"))
        self.sort_button.set_popover(self._sort_popover())
        self.header.pack_end(self.sort_button)

        # ── Selection-mode header widgets (hidden until long-press activates) ──
        self._sel_cancel_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        self._sel_cancel_btn.set_tooltip_text(self._("Cancel selection"))
        self._sel_cancel_btn.set_visible(False)
        self._sel_cancel_btn.connect("clicked", lambda _: self._exit_selection_mode())
        self.header.pack_start(self._sel_cancel_btn)

        self._sel_title = Adw.WindowTitle(title="", subtitle="")
        self._sel_title.set_visible(False)

        self._sel_delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self._sel_delete_btn.set_tooltip_text(self._("Delete selected"))
        self._sel_delete_btn.add_css_class("destructive-action")
        self._sel_delete_btn.set_visible(False)
        self._sel_delete_btn.connect("clicked", lambda _: self._sel_delete_selected())
        self.header.pack_end(self._sel_delete_btn)

        self._sel_move_btn = Gtk.Button.new_from_icon_name("folder-move-symbolic")
        self._sel_move_btn.set_tooltip_text(self._("Move selected"))
        self._sel_move_btn.set_visible(False)
        self._sel_move_btn.connect("clicked", lambda _: self._sel_move_selected())
        self.header.pack_end(self._sel_move_btn)

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
        self.gallery_grid = GalleryGrid(self)
        self.gallery_grid.scroller.add_tick_callback(self._on_grid_tick)
        self.gallery_grid.scroller.connect("edge-overshot", self._on_scroll_edge_overshot)
        scroll_refresh = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_refresh.connect("scroll", self._on_pull_refresh_scroll)
        self.gallery_grid.scroller.add_controller(scroll_refresh)
        folder_swipe = Gtk.GestureSwipe()
        folder_swipe.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        folder_swipe.connect("swipe", self._on_folder_swipe)
        self.gallery_grid.add_controller(folder_swipe)
        self._apply_grid_settings()
        self._grid_width = 0  # force CSS update after rebuild

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_hexpand(True)
        content.set_vexpand(True)
        content.append(self.status)
        content.append(self.gallery_grid)
        self.toolbar.set_content(content)
        self._rebuild_categories()

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
            ("date", "Date", "view-calendar-symbolic"),
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
        _nc_icon_dir = Path(__file__).parent / "data" / "icons"
        _dark = Adw.StyleManager.get_default().get_dark()
        self._nc_spinner = None
        self._nc_broken_img = None
        for category, label, path in self.settings.categories():
            if not path:
                continue
            if category == "nextcloud":
                img = self._make_nc_icon(_nc_icon_dir, _dark)
            else:
                img = Gtk.Image.new_from_icon_name(_icons.get(category, "folder-symbolic"))
                img.set_pixel_size(22)
            lbl = Gtk.Label(label=self._(label))
            lbl.add_css_class("caption")
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            vbox.set_halign(Gtk.Align.CENTER)
            vbox.append(img)
            vbox.append(lbl)
            button = Gtk.ToggleButton()
            if category == "nextcloud":
                spinner = Gtk.Spinner()
                spinner.set_size_request(14, 14)
                spinner.set_halign(Gtk.Align.END)
                spinner.set_valign(Gtk.Align.START)
                spinner.set_visible(False)
                broken_img = Gtk.Image.new_from_icon_name("network-error-symbolic")
                broken_img.set_pixel_size(14)
                broken_img.add_css_class("error")
                broken_img.set_halign(Gtk.Align.END)
                broken_img.set_valign(Gtk.Align.START)
                broken_img.set_visible(False)
                overlay = Gtk.Overlay()
                overlay.set_child(vbox)
                overlay.add_overlay(spinner)
                overlay.add_overlay(broken_img)
                button.set_child(overlay)
                self._nc_spinner = spinner
                self._nc_broken_img = broken_img
            else:
                button.set_child(vbox)
            button.add_css_class("flat")
            button.set_hexpand(True)
            button.set_tooltip_text(str(Path(path).expanduser()))
            button.set_active(category == self.category)
            button.connect("toggled", self._on_category_toggled, category)
            self.nav_box.append(button)
            self.category_buttons[category] = button

    def _make_nc_icon(self, icon_dir: Path, dark: bool) -> Gtk.Image:
        name = "nc-icon-dark.png" if dark else "nc-icon-light.png"
        png = icon_dir / name
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(png), 22, 22, True)
            img = Gtk.Image.new_from_pixbuf(pixbuf)
            img.set_pixel_size(22)
            return img
        except Exception:
            img = Gtk.Image.new_from_icon_name("folder-remote-symbolic")
            img.set_pixel_size(22)
            return img

    def _on_system_theme_changed(self, _mgr, _param) -> None:
        self._rebuild_categories()

    def _apply_grid_settings(self) -> None:
        columns = min(max(int(self.settings.grid_columns), 2), 10)
        self.gallery_grid.set_columns(columns)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def refresh(self, scan: bool = False) -> None:
        if scan:
            self._render()
            self.refresh_button.set_sensitive(False)
            nc_folder = self.current_folder if self.category == "nextcloud" else None
            threading.Thread(target=self._scan_thread, args=(nc_folder,), daemon=True).start()
            return
        self.refresh_button.set_sensitive(True)
        self._render()

    def _scan_thread(self, nc_folder: str | None) -> None:
        try:
            nc_client = None
            if self.settings.nextcloud_url and self.settings.nextcloud_user:
                pwd = self.settings.load_app_password()
                if pwd:
                    from .nextcloud import NextcloudClient
                    nc_client = NextcloudClient(
                        self.settings.nextcloud_url,
                        self.settings.nextcloud_user,
                        pwd,
                    )
                    LOGGER.info("Nextcloud client created for %s", self.settings.nextcloud_url)
                else:
                    LOGGER.info("No Nextcloud app password available; skipping scan")
                    GLib.idle_add(self._set_nc_broken, True)

            # Phase 1: local categories only
            local_cats = [(c, l, p) for c, l, p in self.settings.categories() if c != "nextcloud"]
            self.scanner.scan(local_cats)

            # Phase 2: NC structure scan (no thumbnails)
            if nc_client is not None:
                GLib.idle_add(self._set_nc_syncing, True)
                GLib.idle_add(self._set_nc_broken, False)
                self.scanner.scan_nc_structure(nc_client, self.settings.nextcloud_photos_path)
                GLib.idle_add(self.refresh, False)  # show folder structure immediately

                # Phase 3: load thumbnails only for the active NC folder
                active_folder = nc_folder or "/"
                self.scanner.load_nc_folder_thumbs(
                    nc_client,
                    active_folder,
                    lambda path, thumb: GLib.idle_add(self._update_item_thumb, path, thumb),
                )
        except Exception as e:
            LOGGER.exception("Media scan failed: %s", e)
            GLib.idle_add(self._set_nc_broken, True)
        finally:
            nc_client = None
            GLib.idle_add(self._set_nc_syncing, False)
            GLib.idle_add(self.refresh, False)
            GLib.idle_add(lambda: self.refresh_button.set_sensitive(True))

    def _set_nc_syncing(self, active: bool) -> None:
        if self._nc_spinner is not None:
            self._nc_spinner.set_visible(active)
            if active:
                self._nc_spinner.start()
            else:
                self._nc_spinner.stop()

    def _set_nc_broken(self, active: bool) -> None:
        if self._nc_broken_img is not None:
            self._nc_broken_img.set_visible(active)

    def _update_item_thumb(self, path: str, thumb_path: str) -> None:
        updated = self.gallery_grid.update_item_thumb(path, thumb_path)
        item = self.database.get_media_by_path(path, "nextcloud")
        if item is not None:
            folder_path = self._visible_child_folder_for_item(item.folder)
            if folder_path is not None:
                updated = self.gallery_grid.update_folder_thumb(folder_path, thumb_path) or updated
        if updated:
            LOGGER.debug("Updated visible thumbnail for %s", path)

    def _render(self) -> None:
        # Preserve scroll position when refreshing the same view (e.g. after scan)
        render_key = (self.category, self.current_folder)
        vadj = self.gallery_grid.get_vadjustment()
        saved_pos = vadj.get_value() if render_key == self._last_render_key else 0.0
        self._last_render_key = render_key

        self.gallery_grid.clear()
        self.current_items = []

        sort_mode = self.settings.get_sort_mode(self.category, self.current_folder)
        self.back_button.set_visible(False)
        if sort_mode == "folder":
            self._render_folders()
        elif sort_mode == "date":
            self._render_date_groups()
        else:
            self.current_items = self.database.list_media(
                self.category, sort_mode, self.current_folder
            )
            for item in self.current_items:
                self.gallery_grid.append_media(item, item.path in self._selected_paths)
            self._set_status("")
            self.gallery_grid.set_empty(self._("No pictures found"), not self.current_items)
        self.gallery_grid.finish()

        if saved_pos > 0:
            def _restore() -> bool:
                vadj.set_value(saved_pos)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(_restore, priority=GLib.PRIORITY_HIGH_IDLE)

    def _render_folders(self) -> None:
        sort_mode = self.settings.get_sort_mode(self.category, self.current_folder)
        folders = self.database.child_folders(self.category, self.current_folder)
        for folder, count, thumbs in folders:
            self.gallery_grid.append_folder(folder, count, thumbs)
        direct_folder = self.current_folder or "/"
        self.current_items = self.database.list_media(
            self.category, sort_mode, direct_folder
        )
        for item in self.current_items:
            self.gallery_grid.append_media(item, item.path in self._selected_paths)
        total = len(folders) + len(self.current_items)
        self.gallery_grid.set_empty(self._("No pictures found"), total == 0)
        self._set_status("")

    def _render_date_groups(self) -> None:
        date_key = f"{self.category}\x00{self.current_folder or '/'}"
        granularity = self._date_group_modes.get(date_key, "day")
        self.current_items = self.database.list_media(
            self.category, "newest", self.current_folder
        )
        last_header = None
        for item in self.current_items:
            header = self._date_group_label(item.mtime, granularity)
            if header != last_header:
                self.gallery_grid.append_header(header)
                last_header = header
            self.gallery_grid.append_media(item, item.path in self._selected_paths)
        self._set_status("")
        self.gallery_grid.set_empty(self._("No pictures found"), not self.current_items)

    def _date_group_label(self, mtime: float, granularity: str) -> str:
        dt = datetime.fromtimestamp(mtime)
        if granularity == "week":
            year, week, _weekday = dt.isocalendar()
            return f"{year} · Week {week:02d}"
        if granularity == "month":
            return dt.strftime("%B %Y")
        if granularity == "year":
            return dt.strftime("%Y")
        return dt.strftime("%Y-%m-%d")

    def _visible_child_folder_for_item(self, item_folder: str) -> str | None:
        if item_folder in ("", "/"):
            return None
        parent = self.current_folder
        if parent in (None, "/"):
            return item_folder.split("/", 1)[0]
        parent_prefix = f"{parent}/"
        if not item_folder.startswith(parent_prefix):
            return None
        remainder = item_folder[len(parent_prefix):]
        if not remainder or "/" not in remainder and item_folder == parent:
            return None
        return f"{parent}/{remainder.split('/', 1)[0]}"

    # ------------------------------------------------------------------
    # Item actions
    # ------------------------------------------------------------------

    def _open_folder(self, _button, folder: str) -> None:
        self.current_folder = folder
        self._render()
        if self.category == "nextcloud":
            self._nc_folder_sync_generation += 1
            generation = self._nc_folder_sync_generation
            threading.Thread(
                target=lambda: self._nc_folder_sync_bg(folder, generation),
                daemon=True,
            ).start()

    def _nc_folder_sync_bg(self, folder: str, generation: int) -> None:
        LOGGER.info("Nextcloud thumbnail sync started for folder %r", folder)
        nc_client = None
        try:
            from .nextcloud import NextcloudClient
            pwd = self.settings.load_app_password()
            if not pwd:
                GLib.idle_add(self._set_nc_broken, True)
                return
            nc_client = NextcloudClient(self.settings.nextcloud_url, self.settings.nextcloud_user, pwd)
            GLib.idle_add(self._set_nc_syncing, True)
            GLib.idle_add(self._set_nc_broken, False)
            self.scanner.load_nc_folder_thumbs(
                nc_client,
                folder,
                lambda path, thumb: self._queue_nc_thumb_update(path, thumb, generation),
            )
        except Exception as e:
            LOGGER.exception("Nextcloud folder sync failed: %s", e)
            GLib.idle_add(self._set_nc_broken, True)
        finally:
            nc_client = None
            if generation == self._nc_folder_sync_generation:
                GLib.idle_add(self._set_nc_syncing, False)
            LOGGER.info("Nextcloud thumbnail sync finished for folder %r", folder)

    def _queue_nc_thumb_update(self, path: str, thumb: str, generation: int) -> None:
        if generation == self._nc_folder_sync_generation:
            GLib.idle_add(self._update_item_thumb, path, thumb)

    def _open_item(self, _button, item: MediaItem) -> None:
        if item.is_video and self.settings.external_video_player.strip():
            subprocess.Popen(shlex.split(self.settings.external_video_player) + [item.path])
            return
        items = self.current_items or self.database.list_media(
            item.category, self.settings.get_sort_mode(item.category, self.current_folder), self.current_folder
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

    # ------------------------------------------------------------------
    # Multi-select
    # ------------------------------------------------------------------

    def _enter_selection_mode(self) -> None:
        self._selection_mode = True
        self.back_button.set_visible(False)
        self.refresh_button.set_visible(False)
        self.settings_button.set_visible(False)
        self.sort_button.set_visible(False)
        self._sel_cancel_btn.set_visible(True)
        self._sel_delete_btn.set_visible(True)
        self._sel_move_btn.set_visible(True)
        self.header.set_title_widget(self._sel_title)
        self._update_sel_title()

    def _exit_selection_mode(self) -> None:
        self._selection_mode = False
        self._selected_paths.clear()
        self._sel_cancel_btn.set_visible(False)
        self._sel_delete_btn.set_visible(False)
        self._sel_move_btn.set_visible(False)
        # Restore normal header
        title = Adw.WindowTitle(title=APP_NAME, subtitle="")
        self.header.set_title_widget(title)
        self.back_button.set_visible(False)
        self.refresh_button.set_visible(True)
        self.settings_button.set_visible(True)
        self.sort_button.set_visible(True)
        self._render()

    def _toggle_selection(self, path: str) -> None:
        if path in self._selected_paths:
            self._selected_paths.discard(path)
        else:
            self._selected_paths.add(path)
        if not self._selected_paths:
            self._exit_selection_mode()
            return
        self._update_sel_title()
        self._render()

    def _update_sel_title(self) -> None:
        n = len(self._selected_paths)
        self._sel_title.set_title(f"{n} {self._('selected')}")
        self._sel_title.set_subtitle("")

    def _sel_delete_selected(self) -> None:
        paths = list(self._selected_paths)
        n = len(paths)
        if n == 0:
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=self._("Delete selection?"),
            body=self._("Photos will be moved to trash."),
        )
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("delete", self._("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_sel_delete_confirmed, paths)
        dialog.present()

    def _on_sel_delete_confirmed(self, _dialog, response: str, paths: list[str]) -> None:
        if response != "delete":
            return
        for path in paths:
            try:
                Gio.File.new_for_path(path).trash(None)
                self.database.delete_path(path, self.category)
            except GLib.Error:
                pass
        self._exit_selection_mode()
        self._set_status(self._("Deleted"))

    def _sel_move_selected(self) -> None:
        if not self._selected_paths:
            return
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._on_sel_move_response)
        chooser.show()

    def _on_sel_move_response(self, chooser: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            folder = chooser.get_file().get_path()
            errors = 0
            for path in list(self._selected_paths):
                try:
                    target = Path(folder) / Path(path).name
                    Path(path).rename(target)
                    self.database.delete_path(path, self.category)
                except OSError:
                    errors += 1
            self._exit_selection_mode()
            self.refresh(scan=True)
            self._set_status(self._("Moved") if not errors else self._("Could not complete action"))
        chooser.destroy()

    def _delete_item(self, item: MediaItem) -> None:
        try:
            Gio.File.new_for_path(item.path).trash(None)
            self.database.delete_path(item.path, item.category)
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
                self.database.delete_path(item.path, item.category)
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
        self._go_back_folder()

    def _go_back_folder(self) -> None:
        if not self.current_folder or "/" not in self.current_folder:
            self.current_folder = None
        else:
            self.current_folder = self.current_folder.rsplit("/", 1)[0]
        self._render()

    def _on_folder_swipe(self, _gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        if self._selection_mode or self.current_folder is None:
            return
        if abs(velocity_x) < 350 or abs(velocity_x) <= abs(velocity_y):
            return
        if velocity_x > 0:
            self._go_back_folder()

    def _set_sort_mode(self, _button: Gtk.Button, mode: str, popover: Gtk.Popover) -> None:
        sort_key = f"{self.category}\x00{self.current_folder}" if self.current_folder is not None else self.category
        if mode == "date":
            current_mode = self.settings.sort_modes.get(sort_key)
            order = ["day", "week", "month", "year"]
            date_key = f"{self.category}\x00{self.current_folder or '/'}"
            current_group = self._date_group_modes.get(date_key, "day")
            if current_mode == "date":
                current_group = order[(order.index(current_group) + 1) % len(order)]
            self._date_group_modes[date_key] = current_group
            self._set_status(self._date_group_label(time.time(), current_group).split(" · ")[0])
        self.settings.sort_modes[sort_key] = mode
        self.settings.save()
        popover.popdown()
        self._render()

    def _open_settings(self, _button: Gtk.Button) -> None:
        SettingsWindow(self).present()

    def apply_settings(self, settings: Settings) -> None:
        self._selection_mode = False
        self._selected_paths.clear()
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

    def _on_scroll_edge_overshot(self, _scroller, pos: Gtk.PositionType) -> None:
        if pos == Gtk.PositionType.TOP:
            self._trigger_pull_refresh()

    def _on_pull_refresh_scroll(self, _controller: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        adjustment = self.gallery_grid.get_vadjustment()
        if dy < -0.8 and adjustment.get_value() <= adjustment.get_lower() + 1:
            self._trigger_pull_refresh()
            return True
        return False

    def _trigger_pull_refresh(self) -> None:
        if not self.refresh_button.get_sensitive():
            return
        LOGGER.info("Pull refresh triggered")
        self.gallery_grid.pull_revealer.set_reveal_child(True)
        self.refresh(scan=True)
        GLib.timeout_add(1200, lambda: self.gallery_grid.pull_revealer.set_reveal_child(False) or False)

    def _update_tile_size(self, scroller_width: int) -> None:
        if scroller_width <= 0:
            return
        columns = min(max(int(self.settings.grid_columns), 2), 10)
        # Each cell has 1px padding on each side → 2px per cell
        cell_size = max(32, scroller_width // columns)
        self._tile_css.load_from_data(
            f".gallery-tile {{ min-height: {cell_size}px; }}".encode()
        )

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            .gallery-tile {
                padding: 0;
                margin: 1px;
                border-radius: 0;
                min-width: 0;
                min-height: 0;
            }
            .gallery-tile > * {
                margin: 0;
            }
            listview.gallery-grid > row {
                padding: 0;
            }
            listview.gallery-grid > row:hover,
            listview.gallery-grid > row:selected {
                background: transparent;
            }
            gridview.gallery-grid > child {
                padding: 1px;
            }
            .date-header {
                min-height: 30px;
                padding: 0 4px;
                background: rgba(0,0,0,0.45);
                color: white;
                font-weight: 700;
            }
            .view-switcher {
                border-top: 1px solid @borders;
                padding-top: 4px;
            }
            .sel-check {
                background: alpha(@window_bg_color, 0.75);
                border-radius: 999px;
                padding: 2px;
                margin: 5px;
                -gtk-icon-size: 18px;
            }
            .sel-check.checked {
                background: @accent_bg_color;
                color: @accent_fg_color;
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

# ---------------------------------------------------------------------------



def main() -> int:
    app = GalleryApplication()
    return app.run()
