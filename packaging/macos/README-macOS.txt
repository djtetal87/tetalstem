TetalStem for macOS
==================

Install:

1. Open the TetalStem DMG.
2. Drag TetalStem.app to Applications.
3. Open TetalStem from Applications.

First launch:

- TetalStem is a thin native app. It downloads a pinned, checksummed TetalStem
  runtime pack on first launch.
- The runtime installs to:
  ~/Library/Application Support/TetalStem/runtime
- FFmpeg and ffprobe install to:
  ~/Library/Application Support/TetalStem/ffmpeg
- Demucs model weights download on first use and are cached under:
  ~/Library/Application Support/TetalStem/models

Uninstall:

1. Delete /Applications/TetalStem.app.
2. To remove runtime files, jobs, caches, models, and logs, delete:
   ~/Library/Application Support/TetalStem

Notes:

- Internet access is required for first-run setup.
- Public releases should be signed and notarized.
- Unsigned local builds are for development and internal testing only.
