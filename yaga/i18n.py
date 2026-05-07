import locale
from dataclasses import dataclass


TRANSLATIONS = {
    "en": {
        "Photos": "Photos",
        "Pictures": "Pictures",
        "Videos": "Videos",
        "Screenshots": "Screenshots",
        "Locations": "Locations",
        "Settings": "Settings",
        "Refresh": "Refresh",
        "Sort": "Sort",
        "Newest first": "Newest first",
        "Oldest first": "Oldest first",
        "Name": "Name",
        "Folder": "Folder",
        "Open": "Open",
        "Delete": "Delete",
        "Move": "Move",
        "Share": "Share",
        "Open externally": "Open externally",
        "Close": "Close",
        "Back": "Back",
        "Empty": "Empty",
        "Choose folder": "Choose folder",
        "Media folders": "Media folders",
        "Photos folder": "Photos folder",
        "Pictures folder": "Pictures folder",
        "Videos folder": "Videos folder",
        "Screenshots folder": "Screenshots folder",
        "Optional locations": "Optional locations",
        "Add location": "Add location",
        "Appearance": "Appearance",
        "Grid": "Grid",
        "Photos per row": "Photos per row",
        "Theme": "Theme",
        "System": "System",
        "Light": "Light",
        "Dark": "Dark",
        "Language": "Language",
        "Use system language": "Use system language",
        "English": "English",
        "German": "German",
        "Thumbnails": "Thumbnails",
        "Clear thumbnail cache": "Clear thumbnail cache",
        "Video": "Video",
        "External player command": "External player command",
        "Leave empty to use built-in playback": "Leave empty to use built-in playback",
        "Folder structure": "Folder structure",
        "All media": "All media",
        "No configured folder exists yet.": "No configured folder exists yet.",
        "Deleted": "Deleted",
        "Delete media?": "Delete media?",
        "Delete this item from the gallery?": "Delete this item from the gallery?",
        "Cancel": "Cancel",
        "Moved": "Moved",
        "Could not complete action": "Could not complete action",
    },
    "de": {
        "Photos": "Fotos",
        "Pictures": "Bilder",
        "Videos": "Videos",
        "Screenshots": "Screenshots",
        "Locations": "Orte",
        "Settings": "Einstellungen",
        "Refresh": "Aktualisieren",
        "Sort": "Sortierung",
        "Newest first": "Neueste zuerst",
        "Oldest first": "Älteste zuerst",
        "Name": "Name",
        "Folder": "Ordner",
        "Open": "Öffnen",
        "Delete": "Löschen",
        "Move": "Verschieben",
        "Share": "Teilen",
        "Open externally": "Extern öffnen",
        "Close": "Schließen",
        "Back": "Zurück",
        "Empty": "Leer",
        "Choose folder": "Ordner wählen",
        "Media folders": "Medienordner",
        "Photos folder": "Fotoordner",
        "Pictures folder": "Bilderordner",
        "Videos folder": "Videoordner",
        "Screenshots folder": "Screenshotordner",
        "Optional locations": "Optionale Orte",
        "Add location": "Ort hinzufügen",
        "Appearance": "Darstellung",
        "Grid": "Raster",
        "Photos per row": "Fotos pro Zeile",
        "Theme": "Theme",
        "System": "System",
        "Light": "Hell",
        "Dark": "Dunkel",
        "Language": "Sprache",
        "Use system language": "Systemsprache verwenden",
        "English": "Englisch",
        "German": "Deutsch",
        "Thumbnails": "Vorschaubilder",
        "Clear thumbnail cache": "Thumbnail-Cache löschen",
        "Video": "Video",
        "External player command": "Externer Player-Befehl",
        "Leave empty to use built-in playback": "Leer lassen für integrierte Wiedergabe",
        "Folder structure": "Ordnerstruktur",
        "All media": "Alle Medien",
        "No configured folder exists yet.": "Noch kein konfigurierter Ordner existiert.",
        "Deleted": "Gelöscht",
        "Delete media?": "Medium löschen?",
        "Delete this item from the gallery?": "Dieses Element aus der Galerie löschen?",
        "Cancel": "Abbrechen",
        "Moved": "Verschoben",
        "Could not complete action": "Aktion konnte nicht ausgeführt werden",
    },
}


def system_language() -> str:
    lang, _encoding = locale.getlocale()
    if lang and lang.lower().startswith("de"):
        return "de"
    return "en"


@dataclass
class Translator:
    language: str = "system"

    @property
    def active_language(self) -> str:
        if self.language == "system":
            return system_language()
        return self.language if self.language in TRANSLATIONS else "en"

    def gettext(self, text: str) -> str:
        return TRANSLATIONS.get(self.active_language, TRANSLATIONS["en"]).get(text, text)
