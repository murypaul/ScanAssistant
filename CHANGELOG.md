# Changelog

## 1.3.0 — 2026-07-15

### New / Changed

**Interface**
- The Export queue, Session history, and Positive settings side panels now
  remember whether they were open and where you left them — docked side,
  floating position, size — and reopen the same way the next time you
  launch the app.

## 1.2.0 — 2026-07-15

### New / Changed

**Preferences & Shortcuts**
- Added a Preferences screen (File ▸ Preferences…) covering everything
  previously only editable by hand-editing `config.json`: reopening the
  last project on startup, the exiftool path, whether closing waits for
  pending exports, disk-space and export-queue warning thresholds, the
  maximum negative name length, and the opt-in startup update check.
- Every keyboard shortcut can now be remapped from Preferences ▸
  Shortcuts — click the current key, then press the new one.
- Added Export settings… / Import settings… to move your whole setup
  (including remapped shortcuts) to another machine or back it up.
- Added a per-campaign list of extra file suffixes to ignore in the
  watched folder, for camera/card software that produces junk files the
  built-in list doesn't already know about.

**Robustness**
- Closing the app while exports are still processing no longer freezes
  the window: a "Finalizing…" panel shows progress, with a "Quit without
  waiting" option.

### Bug Fixes

**Interface**
- Fixed disk-space warning/critical thresholds set in Preferences having
  no effect on an actual capture session.
- Fixed Ctrl+F (Project ▸ CSV ▸ View) being documented as a shortcut but
  never actually triggering anything.

### Documentation
- Updated the README and user guides for the above, and corrected two
  stale descriptions: `V` rotates the image in a full 90° cycle rather
  than toggling portrait/landscape, and the `G` shortcut's claim about
  confirming a name outside the list with Ctrl+Enter (never actually
  implemented).
