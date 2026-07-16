# Changelog

## 1.5.0 — 2026-07-16

### New / Changed

**Project & Campaign**
- The project screen now has a large "Start capture" button at the top,
  always visible above the tabs — a faster, more discoverable way to
  begin than the keyboard shortcut or the menu.
- New "Positive crop review" screen (Project menu): lists the negatives
  whose reading positive the automatic crop couldn't confidently narrow
  down, shows the already-exported image with a draggable crop overlay,
  and lets the operator confirm or adjust the crop and the exposure for
  that one image — one keystroke to confirm and move to the next. Only
  the reading positive is regenerated; the archival master files are
  never touched from this screen.

**Framing**
- A severely underexposed negative that the automatic frame detector
  couldn't place at all (near-zero contrast against the light table) now
  gets a second, more thorough attempt instead of being left unframed.

**Exports & Metadata**
- The reading positive now automatically excludes the negative's
  unexposed border from its crop when it can confidently tell the two
  apart, instead of always showing the whole framed negative. Automatic
  exposure no longer gets thrown off by that same border either way,
  whether or not the crop above succeeds.

**Interface**
- The "processing queue is growing" warning banner can now be dismissed
  with a close button instead of staying on screen until the app closes.

### Bug Fixes

**Capture**
- Dragging the crop rectangle no longer re-exports the image after every
  small movement — only once the drag settles, matching how a keyboard
  adjustment already behaved. Several quick adjustments in a row now
  produce one export instead of flooding the queue with one per movement.

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
  image, at a reduced resolution while actually dragging so it doesn't
  stutter. The manual sliders now grey out visibly when Positive mode
  isn't Manual, matching the rest of the app's disabled controls.
- Pressing `V` to rotate the current image now rotates the plain
  negative view itself, crop rectangle included, instead of switching
  away to the master preview. `Shift+V` rotates the other way. Rotating
  several times in a row now re-exports once, after you settle on a
  value, instead of once per press.
- Added `K`, a dedicated key that cycles the preview through negative →
  positive → master, independently of the existing `P`/`T` toggles;
  `Shift+K` cycles the other way.
- The Export queue, Session history, and Positive settings panels no
  longer follow you to the Home or Project screen — they're confined to
  the capture screen, remembering what was open so it comes back the
  same way next time you start capturing.
- Positive/master preview now resets to the plain negative view on the
  next image, rather than carrying over from the previous one.
- Adjusting the crop no longer needs a dedicated edit mode: arrows move
  it, `+`/`-` resize it, `Ctrl+arrows` deskew it, and it can now also be
  dragged directly by its border, corner, or interior with the mouse —
  all available at any time on the negative view, switching back to it
  automatically if a positive or master preview was open. As with
  rotation, an edit only re-exports once you settle on a value (or
  immediately on mouse release), not on every intermediate nudge or
  drag step.
- `Left`/`Right` no longer move between pending names (only Session
  history and Go to name do that now) — they move the crop instead.
- Go to name is now `Ctrl+G` instead of plain `G`, which now toggles the
  rule-of-thirds guides at any time.

**Framing**
- Added optional rule-of-thirds guide lines for the crop (`G`) — a
  compositional aid for cropping, off by default.

### Bug Fixes

**Interface**
- The rule-of-thirds guides (`G`) are now a clearly visible dotted line
  instead of nearly disappearing against the negative.
- The Positive settings sliders' handles are no longer clipped at either
  end of the track.

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
