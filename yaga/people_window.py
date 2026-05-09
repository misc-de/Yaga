"""People view — named persons + auto-discovered clusters.

The window is a thin shell around FaceRepository. Heavy work (indexing,
clustering) runs in a worker thread; UI updates marshal back through
GLib.idle_add.
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk, Pango

from .database import Database
from .faces import FaceRepository, capabilities, is_available
from .faces.clusterer import FaceClusterer
from .faces.indexer import FaceIndexer

LOGGER = logging.getLogger(__name__)

AVATAR_SIZE = 96
TEXTURE_PIXELS = 192


class PeopleWindow(Adw.ApplicationWindow):
    def __init__(self, parent) -> None:
        super().__init__(
            application=parent.get_application(),
            transient_for=parent,
            title=parent._("People"),
        )
        self.set_default_size(820, 640)
        self.parent_window = parent
        self.database: Database = parent.database
        self.repo = FaceRepository(self.database)
        self._busy = False

        self._build_ui()
        self._refresh()

    # ── i18n shim ──────────────────────────────────────────────────────────

    def _(self, text: str) -> str:
        return self.parent_window._(text)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        self.header = Adw.HeaderBar()
        toolbar.add_top_bar(self.header)

        self.title_widget = Adw.WindowTitle(title=self._("People"), subtitle="")
        self.header.set_title_widget(self.title_widget)

        self.scan_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.scan_button.set_tooltip_text(self._("Scan for faces"))
        self.scan_button.connect("clicked", self._on_scan_clicked)
        self.header.pack_end(self.scan_button)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        self.header.pack_end(self.spinner)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        toolbar.set_content(scroller)

        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.content_box.set_margin_top(24)
        self.content_box.set_margin_bottom(24)
        self.content_box.set_margin_start(24)
        self.content_box.set_margin_end(24)
        scroller.set_child(self.content_box)

    # ── Refresh / state ────────────────────────────────────────────────────

    def _clear_content(self) -> None:
        child = self.content_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.content_box.remove(child)
            child = nxt

    def _refresh(self) -> None:
        self._clear_content()

        if not is_available():
            self._render_unavailable()
            self.scan_button.set_sensitive(False)
            return
        self.scan_button.set_sensitive(not self._busy)

        persons = self.repo.list_persons()
        clusters = self.repo.list_unnamed_clusters()

        if not persons and not clusters:
            self._render_empty()
            return

        if persons:
            self.content_box.append(self._section_header(self._("People")))
            self.content_box.append(self._person_grid(persons))

        if clusters:
            heading = self._("Suggested groups") if persons else self._("New groups")
            self.content_box.append(self._section_header(heading))
            self.content_box.append(self._cluster_grid(clusters))

    # ── Empty / unavailable states ─────────────────────────────────────────

    def _render_unavailable(self) -> None:
        caps = capabilities()
        missing = ", ".join(name for name, ok in caps.items() if not ok) or "—"
        status = Adw.StatusPage(
            title=self._("Face recognition unavailable"),
            description=(
                self._("Install with: pip install 'yaga-gallery[faces]'")
                + "\n\n"
                + self._("Missing: ")
                + missing
            ),
            icon_name="avatar-default-symbolic",
        )
        status.set_vexpand(True)
        self.content_box.append(status)

    def _render_empty(self) -> None:
        status = Adw.StatusPage(
            title=self._("No faces yet"),
            description=self._("Run a scan to find people in your photos."),
            icon_name="avatar-default-symbolic",
        )
        status.set_vexpand(True)
        button = Gtk.Button(label=self._("Scan now"))
        button.set_halign(Gtk.Align.CENTER)
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        button.connect("clicked", self._on_scan_clicked)
        status.set_child(button)
        self.content_box.append(status)

    # ── Section helpers ────────────────────────────────────────────────────

    @staticmethod
    def _section_header(text: str) -> Gtk.Widget:
        label = Gtk.Label(label=text)
        label.add_css_class("title-2")
        label.set_halign(Gtk.Align.START)
        return label

    @staticmethod
    def _flow() -> Gtk.FlowBox:
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_max_children_per_line(8)
        flow.set_min_children_per_line(2)
        flow.set_homogeneous(True)
        flow.set_column_spacing(16)
        flow.set_row_spacing(16)
        return flow

    @staticmethod
    def _avatar(text: str, thumb_path: str | None) -> Adw.Avatar:
        avatar = Adw.Avatar(size=AVATAR_SIZE, text=text or "?", show_initials=bool(text))
        if thumb_path:
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    thumb_path, TEXTURE_PIXELS, TEXTURE_PIXELS, True,
                )
                avatar.set_custom_image(Gdk.Texture.new_for_pixbuf(pix))
            except GLib.Error as e:
                LOGGER.debug("Avatar thumb load failed (%s): %s", thumb_path, e)
        return avatar

    # ── Person tiles ───────────────────────────────────────────────────────

    def _person_grid(self, persons) -> Gtk.Widget:
        flow = self._flow()
        for person in persons:
            flow.append(self._person_tile(person))
        return flow

    def _person_tile(self, person) -> Gtk.Widget:
        button = Gtk.Button()
        button.add_css_class("flat")
        thumb = self.repo.cover_thumb_for_person(person.id)
        avatar = self._avatar(person.name, thumb)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_halign(Gtk.Align.CENTER)
        box.append(avatar)

        name = Gtk.Label(label=person.name)
        name.add_css_class("heading")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(name)

        count_label = Gtk.Label(label=self._("{n} photos").format(n=person.face_count))
        count_label.add_css_class("dim-label")
        count_label.add_css_class("caption")
        box.append(count_label)

        button.set_child(box)
        button.connect("clicked", lambda _b, p=person: self._on_person_clicked(p))

        # Right-click menu: rename / delete
        gesture = Gtk.GestureClick()
        gesture.set_button(Gdk.BUTTON_SECONDARY)
        gesture.connect("released", lambda *_a, p=person, btn=button: self._show_person_menu(btn, p))
        button.add_controller(gesture)
        return button

    # ── Cluster tiles ──────────────────────────────────────────────────────

    def _cluster_grid(self, clusters) -> Gtk.Widget:
        flow = self._flow()
        for cluster in clusters:
            flow.append(self._cluster_tile(cluster))
        return flow

    def _cluster_tile(self, cluster: dict) -> Gtk.Widget:
        button = Gtk.Button()
        button.add_css_class("flat")
        avatar = self._avatar("", cluster["cover_thumb"])

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_halign(Gtk.Align.CENTER)
        box.append(avatar)

        n_label = Gtk.Label(label=self._("{n} faces").format(n=cluster["count"]))
        n_label.add_css_class("heading")
        box.append(n_label)

        hint = Gtk.Label(label=self._("Click to name"))
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)

        button.set_child(box)
        button.connect("clicked", lambda _b, c=cluster: self._on_cluster_name(c))
        return button

    # ── Actions ────────────────────────────────────────────────────────────

    def _on_person_clicked(self, person) -> None:
        # The gallery filter for "show only this person's media" lands in a
        # follow-up commit. For now, just close — opening a person tile is
        # already the discoverable hook.
        LOGGER.debug("Person tile clicked: id=%s name=%s", person.id, person.name)
        self.close()

    def _on_cluster_name(self, cluster: dict) -> None:
        dialog = Adw.AlertDialog(
            heading=self._("Name this person"),
            body=self._("This group has {n} faces.").format(n=cluster["count"]),
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text(self._("Name"))
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("save", self._("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(_d, response: str) -> None:
            if response != "save":
                return
            name = entry.get_text().strip()
            if not name:
                return
            try:
                pid = self.repo.create_person(name)
                self.repo.assign_cluster_to_person(cluster["cluster_id"], pid)
                self.database.commit()
            except Exception:
                LOGGER.exception("Naming cluster failed")
                return
            self._refresh()

        dialog.connect("response", on_response)
        dialog.present(self)

    def _show_person_menu(self, button: Gtk.Widget, person) -> None:
        popover = Gtk.Popover()
        popover.set_parent(button)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        rename_btn = Gtk.Button(label=self._("Rename"))
        rename_btn.add_css_class("flat")
        rename_btn.connect("clicked", lambda _b: (popover.popdown(), self._rename_person(person)))
        box.append(rename_btn)

        delete_btn = Gtk.Button(label=self._("Delete"))
        delete_btn.add_css_class("flat")
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect("clicked", lambda _b: (popover.popdown(), self._delete_person(person)))
        box.append(delete_btn)

        popover.set_child(box)
        popover.popup()

    def _rename_person(self, person) -> None:
        dialog = Adw.AlertDialog(
            heading=self._("Rename person"),
            body="",
        )
        entry = Gtk.Entry()
        entry.set_text(person.name)
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("save", self._("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(_d, response: str) -> None:
            if response != "save":
                return
            new_name = entry.get_text().strip()
            if not new_name or new_name == person.name:
                return
            self.repo.rename_person(person.id, new_name)
            self.database.commit()
            self._refresh()

        dialog.connect("response", on_response)
        dialog.present(self)

    def _delete_person(self, person) -> None:
        dialog = Adw.AlertDialog(
            heading=self._("Delete person?"),
            body=self._("Faces will return to the suggested groups.")
        )
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("delete", self._("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d, response: str) -> None:
            if response != "delete":
                return
            self.repo.delete_person(person.id)
            self.database.commit()
            self._refresh()

        dialog.connect("response", on_response)
        dialog.present(self)

    # ── Background scan ────────────────────────────────────────────────────

    def _on_scan_clicked(self, _btn) -> None:
        if self._busy or not is_available():
            return
        self._set_busy(True, self._("Scanning faces…"))

        def worker() -> None:
            err: str | None = None
            stats: dict | None = None
            try:
                indexer = FaceIndexer(self.database)
                processed = indexer.index_pending()
                clusterer = FaceClusterer(self.database)
                stats = clusterer.recluster()
                LOGGER.info("Scan summary: processed=%s, %s", processed, stats)
            except Exception as exc:
                LOGGER.exception("Face scan failed")
                err = str(exc)
            GLib.idle_add(self._on_scan_done, err)

        threading.Thread(target=worker, name="face-scan", daemon=True).start()

    def _on_scan_done(self, err: str | None) -> bool:
        self._set_busy(False, "")
        if err:
            self._show_error(err)
        self._refresh()
        return False

    def _set_busy(self, busy: bool, label: str) -> None:
        self._busy = busy
        self.scan_button.set_sensitive(not busy)
        self.spinner.set_visible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.title_widget.set_subtitle(label)

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading=self._("Scan failed"), body=message)
        dialog.add_response("close", self._("Close"))
        dialog.present(self)
