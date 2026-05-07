from pathlib import Path
import threading

import yaga.config as config
import yaga.thumbnails as thumbnails
from yaga.database import Database
from yaga.i18n import Translator
from yaga.models import media_type_for
from yaga.scanner import MediaScanner
from yaga.config import Settings
from yaga.thumbnails import Thumbnailer


class FakeThumbnailer:
    def ensure_thumbnail(self, path: Path, media_type: str) -> str:
        return f"thumb://{media_type}/{path.name}"


def test_media_type_detection() -> None:
    assert media_type_for(Path("photo.jpg")) == "image"
    assert media_type_for(Path("movie.webm")) == "video"
    assert media_type_for(Path("notes.txt")) is None


def test_translator_falls_back_to_english() -> None:
    assert Translator("de").gettext("Settings") == "Einstellungen"
    assert Translator("unknown").gettext("Settings") == "Settings"


def test_database_preserves_folder_hierarchy(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    root = tmp_path / "photos"
    nested = root / "Trips" / "Berlin"
    nested.mkdir(parents=True)
    direct = root / "Trips" / "cover.jpg"
    deep = nested / "street.jpg"
    direct.write_bytes(b"direct")
    deep.write_bytes(b"deep")

    db.upsert_media(path=direct, category="photos", media_type="image", folder="Trips", thumb_path=None)
    db.upsert_media(path=deep, category="photos", media_type="image", folder="Trips/Berlin", thumb_path=None)
    db.commit()

    assert db.child_folders("photos", None)[0][0] == "Trips"
    assert db.child_folders("photos", "Trips")[0][0] == "Trips/Berlin"
    assert [item.name for item in db.list_media("photos", "folder", "Trips")] == ["cover.jpg"]


def test_database_sort_modes(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    newer = tmp_path / "b-newer.jpg"
    older = tmp_path / "a-older.jpg"
    newer.write_bytes(b"newer")
    older.write_bytes(b"older")

    db.upsert_media(path=older, category="photos", media_type="image", folder="/", thumb_path=None)
    db.upsert_media(path=newer, category="photos", media_type="image", folder="/", thumb_path=None)
    db.conn.execute("UPDATE media SET mtime = 100 WHERE path = ?", (str(older),))
    db.conn.execute("UPDATE media SET mtime = 200 WHERE path = ?", (str(newer),))
    db.commit()

    assert [item.name for item in db.list_media("photos", "newest")] == ["b-newer.jpg", "a-older.jpg"]
    assert [item.name for item in db.list_media("photos", "oldest")] == ["a-older.jpg", "b-newer.jpg"]
    assert [item.name for item in db.list_media("photos", "name")] == ["a-older.jpg", "b-newer.jpg"]


def test_scanner_indexes_media_recursively_and_ignores_non_media(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    root = tmp_path / "Pictures"
    nested = root / "Camera" / "May"
    nested.mkdir(parents=True)
    (root / "cover.jpg").write_bytes(b"image")
    (nested / "clip.mp4").write_bytes(b"video")
    (nested / "notes.txt").write_bytes(b"text")

    scanner = MediaScanner(db, FakeThumbnailer())
    scanner.scan([("pictures", "Pictures", str(root))])

    all_items = db.list_media("pictures", "name")
    assert [item.name for item in all_items] == ["clip.mp4", "cover.jpg"]
    assert all_items[0].folder == "Camera/May"
    assert all_items[0].media_type == "video"
    assert all_items[0].thumb_path == "thumb://video/clip.mp4"
    assert all_items[1].folder == "/"


def test_scanner_can_use_database_from_background_thread(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    root = tmp_path / "Pictures"
    root.mkdir()
    (root / "background.jpg").write_bytes(b"image")
    scanner = MediaScanner(db, FakeThumbnailer())
    error: list[BaseException] = []

    def run_scan() -> None:
        try:
            scanner.scan([("pictures", "Pictures", str(root))])
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=run_scan)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert error == []
    assert [item.name for item in db.list_media("pictures")] == ["background.jpg"]


def test_scanner_prunes_removed_files_only_for_scanned_existing_categories(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    photos_root = tmp_path / "Photos"
    missing_root = tmp_path / "Missing"
    photos_root.mkdir()
    stale_photo = photos_root / "stale.jpg"
    mounted_later = tmp_path / "sd-card.jpg"
    stale_photo.write_bytes(b"stale")
    mounted_later.write_bytes(b"mounted")

    db.upsert_media(path=stale_photo, category="photos", media_type="image", folder="/", thumb_path=None)
    db.upsert_media(path=mounted_later, category="location:0", media_type="image", folder="/", thumb_path=None)
    db.commit()
    stale_photo.unlink()

    scanner = MediaScanner(db, FakeThumbnailer())
    scanner.scan([("photos", "Photos", str(photos_root)), ("location:0", "SD", str(missing_root))])

    assert db.list_media("photos") == []
    assert [item.name for item in db.list_media("location:0")] == ["sd-card.jpg"]


def test_settings_save_and_load_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    settings = Settings(
        photos_dir="/photos",
        pictures_dir="/pictures",
        videos_dir="/videos",
        screenshots_dir="/screenshots",
        extra_locations=["/run/media/card", "/home/user/Nextcloud"],
        sort_mode="folder",
        theme="dark",
        language="de",
        external_video_player="vlc",
        grid_columns=6,
    )

    settings.save()
    loaded = Settings.load()

    assert loaded == settings
    assert loaded.categories()[-2:] == [
        ("location:0", "card", "/run/media/card"),
        ("location:1", "Nextcloud", "/home/user/Nextcloud"),
    ]
    assert loaded.grid_columns == 6


def test_settings_load_ignores_unknown_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    config.CONFIG_DIR.mkdir(parents=True)
    (config.CONFIG_DIR / "settings.json").write_text('{"theme": "light", "future": true}', encoding="utf-8")

    loaded = Settings.load()

    assert loaded.theme == "light"
    assert not hasattr(loaded, "future")


def test_thumbnailer_uses_stable_hash_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(thumbnails, "THUMB_DIR", tmp_path / "thumbs")
    thumbnailer = Thumbnailer()
    image = tmp_path / "photo.jpg"

    first = thumbnailer.thumb_path_for(image)
    second = thumbnailer.thumb_path_for(image)

    assert first == second
    assert first.parent == tmp_path / "thumbs"
    assert first.suffix == ".jpg"


def test_thumbnailer_clear_recreates_cache_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(thumbnails, "THUMB_DIR", tmp_path / "thumbs")
    thumbnailer = Thumbnailer()
    old_file = thumbnails.THUMB_DIR / "old.jpg"
    old_file.write_bytes(b"old")

    thumbnailer.clear()

    assert thumbnails.THUMB_DIR.exists()
    assert not old_file.exists()


def test_gallery_media_area_is_configured_to_expand() -> None:
    source = Path("yaga/app.py").read_text(encoding="utf-8")

    assert "self.flow.set_vexpand(True)" in source
    assert "scroller.set_vexpand(True)" in source
    assert "content.set_vexpand(True)" in source


def test_gallery_grid_defaults_to_four_compact_media_columns() -> None:
    settings = Settings()
    source = Path("yaga/app.py").read_text(encoding="utf-8")

    assert settings.grid_columns == 4
    assert "self.flow.set_column_spacing(2)" in source
    assert "self.flow.set_row_spacing(2)" in source
    assert "self.flow.set_halign(Gtk.Align.START)" in source
    assert "self.flow.set_valign(Gtk.Align.START)" in source
    assert "self.flow.set_margin_start(0)" in source
    assert "self.flow.set_margin_end(0)" in source
    assert "self.flow.set_min_children_per_line(columns)" in source
    assert "self.flow.set_max_children_per_line(columns)" in source
    assert "Photos per row" in source


def test_gallery_tiles_are_sized_from_available_width() -> None:
    source = Path("yaga/app.py").read_text(encoding="utf-8")

    assert "def _calculate_tile_size" in source
    assert "(columns - 1) * 2" in source
    assert "tile.set_size_request(self.tile_size, self.tile_size)" in source
    assert "scroller.add_tick_callback(self._on_grid_tick)" in source


def test_viewer_has_header_actions_but_no_navigation_buttons() -> None:
    source = Path("yaga/app.py").read_text(encoding="utf-8")
    viewer_source = source.split("class ViewerWindow", 1)[1]

    assert "Adw.HeaderBar()" in viewer_source
    assert "window-close-symbolic" in viewer_source
    assert "user-trash-symbolic" in viewer_source
    assert "go-previous-symbolic" not in viewer_source
    assert "go-next-symbolic" not in viewer_source
    assert "Gtk.GestureSwipe()" in viewer_source
    assert "Gtk.GestureDrag()" in viewer_source
    assert "Gtk.GestureZoom()" in viewer_source
    assert "Gtk.GestureClick()" in viewer_source
    assert "Gtk.PropagationPhase.CAPTURE" in viewer_source
    assert 'swipe.connect("swipe", self._on_swipe)' in viewer_source
    assert 'swipe.connect("end", self._on_swipe)' not in viewer_source
    assert 'drag.connect("drag-end", self._on_drag_end)' in viewer_source
    assert 'zoom.connect("scale-changed", self._on_zoom_scale_changed)' in viewer_source
    assert 'click.connect("pressed", self._on_viewer_pressed)' in viewer_source
    assert "def _navigate_from_horizontal_motion" in viewer_source


def test_viewer_supports_pinch_zoom_and_double_tap_reset() -> None:
    source = Path("yaga/app.py").read_text(encoding="utf-8")
    viewer_source = source.split("class ViewerWindow", 1)[1]

    assert "self.zoom_scale = 1.0" in viewer_source
    assert "self.zoom_start_scale = 1.0" in viewer_source
    assert "self.zoom_view.set_size_request(int(width * self.zoom_scale), int(height * self.zoom_scale))" in viewer_source
    assert "self.zoom_view.set_size_request(-1, -1)" in viewer_source
    assert "self._set_zoom(self.zoom_start_scale * scale_delta)" in viewer_source
    assert "self._keep_zoom_focus(old_scale, self.zoom_scale, focus_x, focus_y)" in viewer_source
    assert "gesture.get_bounding_box_center()" in viewer_source
    assert "if n_press == 2:" in viewer_source
    assert "self._reset_zoom()" in viewer_source
    assert "if self.zoom_scale > 1.05:" in viewer_source


def test_viewer_delete_uses_confirmation_and_cleans_index_and_thumbnail() -> None:
    source = Path("yaga/app.py").read_text(encoding="utf-8")
    viewer_source = source.split("class ViewerWindow", 1)[1]

    assert "Adw.AlertDialog" in viewer_source
    assert "Adw.ResponseAppearance.DESTRUCTIVE" in viewer_source
    assert "Gio.File.new_for_path(item.path).trash(None)" in viewer_source
    assert "Path(item.thumb_path).unlink(missing_ok=True)" in viewer_source
    assert "self.parent_window.database.delete_path(item.path)" in viewer_source
    assert "self.parent_window.refresh(scan=False)" in viewer_source
