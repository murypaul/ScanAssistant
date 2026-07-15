# Changelog

## 1.4.0 — 2026-07-15

### New / Changed

**Interface**
- Reworked the capture screen's colors, spacing, and layout for use in a
  fully dark room: a warmer, less harsh background, and nothing ever
  drawn on top of the negative itself except the crop rectangle — the
  image name and confidence now live in a header of their own directly
  above the preview, with the export queue and progress moved to a
  status strip below it.
- The crop rectangle now carries a dark keyline on both sides of its
  color, so it stays visible against any negative — including an
  orange-based colour negative, where a plain colored line nearly
  disappeared into the film's own cast.
- The Session history and Export queue panels no longer show a bright
  border around the whole list — that border is now reserved for actual
  editable fields, so it stops competing for attention with the rows
  inside.
- Positive settings' exposure, contrast, shadows, and highlights are now
  draggable sliders with a value field and a right-click reset to
  default, instead of plain spin boxes; horizontal flip is now an
  animated switch. Any change — including mid-drag — is reflected in an
  open positive/master preview immediately instead of only on the next
  image.
- Pressing `V` to rotate the current image now rotates the plain
  negative view itself, crop rectangle included, instead of switching
  away to the master preview.
- Added `K`, a dedicated key that cycles the preview through negative →
  positive → master, independently of the existing `P`/`T` toggles.
- The Export queue, Session history, and Positive settings panels no
  longer follow you to the Home or Project screen — they're confined to
  the capture screen, remembering what was open so it comes back the
  same way next time you start capturing.

**Framing**
- Added optional rule-of-thirds guide lines while editing the crop (`G`
  in frame-edit mode) — a compositional aid for cropping, off by default
  and cleared automatically when you leave edit mode.

### Bug Fixes

**Preferences & Shortcuts**
- Every Preferences tab is now scrollable — the Shortcuts tab in
  particular no longer gets cut off with no way to reach the entries
  past the bottom of the window.
- "Reopen last project on startup" now actually reopens it — the
  setting existed and could be toggled, but had no effect.

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
