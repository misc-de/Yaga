# Yaga Gallery

Yaga is a GTK4/libadwaita gallery app written in Python. It indexes configured media folders, keeps a thumbnail cache, and shows folders, photos, screenshots, and videos in a tiled overview.

## Features

- Top navigation for Photos, Pictures, Videos, Screenshots, and optional locations
- Recursive folder scanning with folder structure preserved
- SQLite media index for faster startup
- Thumbnail cache under `~/.cache/yaga/thumbnails`
- Image fullscreen viewer with previous/next navigation
- Built-in video playback with GTK media widgets
- Optional external video player command
- Sort modes: newest, oldest, name, folder
- Theme modes: system, light, dark
- Runtime language switching: system, English, German
- Delete, move, and share-by-email actions

## Run

Install the runtime dependencies for your distribution:

- Python 3.11+
- GTK 4
- libadwaita 1
- PyGObject

Then run:

```bash
python3 -m yaga
```

Optional tools improve video thumbnails and sharing:

- `ffmpegthumbnailer` or `ffmpeg`
- `xdg-email`

