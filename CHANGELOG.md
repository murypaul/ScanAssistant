# Changelog

## 1.10.0 — 2026-07-20

### New / Changed
**Capture**
- Session-wide white balance: pick a neutral point once from the preview
  (`W`, then click) and it now applies to every capture for the rest of
  the session — both the exported TIFF/JPEG files and the on-screen
  preview — instead of the camera's own per-shot white balance, which
  drifted depending on what was in front of the lens rather than the
  actual lighting.
- The correction side panel (recently finalized images, click one to
  reopen and fix a mistake) now stays populated across stopping and
  restarting capture mode within the same campaign, instead of forgetting
  everything the moment you step back to the project screen.

**Live view**
- The live badge, measured fps, and close button now float directly over
  the video feed instead of taking a row of their own above it, leaving
  more room for the actual preview.
- Measured fps now updates once a second instead of on every single
  frame, much less distracting to look at.

### Bug Fixes
**Framing**
- Nudging the crop frame with the arrow keys moved it in the wrong
  direction after rotating the image 90°/180°/270° — arrow keys now
  always match what's on screen, regardless of rotation.

**Interface**
- The export queue, session history, and positive settings side panels
  used to appear automatically the very first time capture mode was
  entered in a session, even with nothing to show yet — they now
  correctly stay hidden until the operator opens them.
- The "Capturing…" indicator during a remote-triggered shot could show up
  as a small floating window elsewhere on screen instead of over the live
  view.
- The live view toggle button's height no longer mismatches the other
  controls next to it.

## 1.9.0 — 2026-07-20

First extended real-world session with tethered capture against an
actual D750 (previously only unit-tested against a fake backend) —
turned up and fixed most of the feature's remaining rough edges.

### Fixed

**Camera — connection reliability**
- `connect()` only ever tried once; a USB device that wasn't quite ready
  yet (still settling after a replug, a just-released gvfs claim...)
  permanently failed until the operator noticed and clicked Capture ▸
  Release camera. It now retries with backoff, the same way
  `start_live_view()` already did.
- The gvfs USB-claim release (1.8.2) now runs automatically on every
  connect attempt, not just from the manual menu action.
- Tethered capture now connects on its own as soon as the app starts (if
  the camera's already on) and keeps retrying quietly in the background
  every 5 s until it succeeds — no need to open a campaign or click
  anything first.
- A failed `connect()` could leave the USB interface claimed by
  ScanAssistant's own process, permanently blocking every later attempt
  (including a fresh external one) until the app was restarted. Fixed by
  releasing the partial claim before re-raising.
- `connect()` is now a no-op once already connected, instead of silently
  leaking a second internal handle every time it was called again (each
  capture-session entry, the periodic reconnect poll).

**Camera — data safety**
- Captures were silently landing in the camera's internal RAM instead of
  the memory card, despite the "keep captures on the card" setting:
  `capturetarget` is a vendor-worded PTP enum ("Memory card"/"Internal
  RAM" on the D750), and the plain "card"/"sdram" strings this app was
  sending never matched any real choice, so the request was silently
  ignored every time.
- That fix's own first attempt was still wrong: matching against a
  hardcoded label (even the correct one) breaks the moment
  `libgphoto2`'s gettext catalog returns a translated label instead —
  confirmed on a French desktop, where the real choices are "Carte
  mémoire"/"Affichage entier"/"100 %" (non-breaking space), not the
  English text a quick check without `locale.setlocale()` turns up.
  `capturetarget` and the live view zoom below now both match by choice
  **position** instead, which is stable across languages.

**Camera — live view / remote trigger**
- Two `python-gphoto2` calls (`capture_preview`, `file_get`) were passed
  the wrong arguments, silently crashing the background camera thread
  the moment live view or a tethered download was attempted.
  Live view and remote-triggered downloads are confirmed working
  end-to-end against real hardware now (a full RAW downloaded and
  ingested normally).
- The live view panel never painted its own background/border (a
  missing Qt attribute), and its ●/⤡ icon buttons rendered as empty
  squares (a generic button style's padding left no room for a
  28px-wide icon button's label).
- Clicking the live view image to check focus in detail changed state
  but never actually resized the panel — nothing told the capture
  screen to re-apply the new size, so "expand" only visibly worked by
  coincidence, if some unrelated resize happened to fire around the
  same time.
- The live view opacity slider could be dragged to 0%, fading out its
  own controls along with the image — with nothing left visible to
  click, there was no way back short of hand-editing `config.json`.
  Floored at 15%.
- A stale "camera not detected" banner stayed up even after the camera
  successfully reconnected (existing warnings are deliberately never
  auto-dismissed elsewhere, but a connection error is actively wrong,
  not just stale, once the connection recovers).

### Added

**Camera**
- Clicking the live view image now swaps it to fill the capture screen,
  opaque, instead of a small translucent overlay — click again (or the
  ⤡ button) to go back. Only active while actually live, so a click near
  a static vignette can't be mistaken for one of the capture screen's
  own interactions.
- While expanded, the D750's own camera-side live view zoom engages
  automatically (the same feature its rear-screen zoom button drives) —
  real sensor-level crop for judging focus, not this app enlarging the
  same low-resolution preview pixels. It stays centered on the camera's
  current AF point; panning it isn't wired up yet.
- The live view panel can be hidden with its own × button and brought
  back from Capture ▸ Show/hide live view panel, View ▸ Show/hide live
  view panel, or the H key — hiding it also stops the feed itself
  rather than leaving it running behind an invisible widget.
- New Preferences ▸ Camera ▸ "Rotate live view 180°" for a camera
  mounted upside-down over the negative (a common copy-stand setup) —
  only affects the on-screen live view image, never the RAW file.

### Changed

- The Export queue, Session history, and Positive settings panels now
  default to visible on a fresh install (previously hidden until opened
  once) — an existing saved layout is never overridden by this.

## 1.8.2 — 2026-07-19

### Fixed

**Camera**
- First real-world test against the D750 turned up the actual leading
  cause of "camera not detected": Nemo/gvfs auto-mounts any PTP-mode
  camera the instant it's plugged in, and the resulting exclusive USB
  claim makes `libgphoto2` report the camera as absent even though it's
  right there. New Capture ▸ Release camera from file manager menu item
  releases that claim and retries connecting — enabled whenever tethered
  capture is on for the current session.

## 1.8.1 — 2026-07-19

### Fixed

**Camera**
- `pyproject.toml`'s `camera` extra referenced a nonexistent PyPI package
  (`python-gphoto2` — the actual distribution is `gphoto2`), so a config
  with tethered capture already enabled crashed on startup with
  `ModuleNotFoundError` no matter what you installed by hand.
- Turning "Enable tethered camera" on in Preferences now checks whether
  `gphoto2` is installed and, if not, offers to install it into
  ScanAssistant's own virtual environment right there — the setting is
  only actually turned on once that succeeds, so this class of crash
  can't happen again from the preferences flow.

## 1.8.0 — 2026-07-19

### New / Changed

**Capture**
- A remote-triggered tethered shot now downloads straight into the
  watched folder over the same connection used for live view, instead of
  relying on the camera being mounted as a drive — so live view and
  automatic arrival of each shot both work at once, with nothing to
  plug, unplug, or remount between shots. The negative still stays on
  the memory card either way, so nothing is lost if a download fails.
- Because of this, the watched folder for a campaign that uses tethered
  capture must now be a regular local folder rather than the camera's
  own memory-card mount point.

## 1.7.0 — 2026-07-19

### New / Changed

**Capture**
- Optional tethered capture for cameras whose own rear screen turns off
  while connected over USB (Nikon D750 first): a live view vignette over
  the preview, and a keyboard shortcut to fire the shutter remotely.
  Off by default — captures still arrive through the watched folder like
  any other file.
- The live view vignette can be expanded with mouse zoom and pan to check
  focus closely, and its opacity can be lowered to see the last accepted
  preview through it. Its frame rate is adjustable, with the actual
  achieved rate shown alongside the setting.

**Preferences & Shortcuts**
- A new Camera tab (File ▸ Preferences) turns tethered capture on or off.
- Pause/resume moved from Space to Tab; Space now fires a remote capture
  when tethered capture is on; a new L shortcut toggles live view. All
  three stay remappable like any other shortcut.

### Bug Fixes

**Capture**
- Switching to a different project while a capture session was still
  running no longer leaves the previous campaign's folder watching and
  export processing active in the background.
- A leftover file from an earlier crash no longer causes the watched
  folder to be wrongly reported as inaccessible.

**Framing**
- Rotated negatives (portrait shots the camera stores pre-rotated) could
  get an incorrectly positioned crop, both automatic and manual — fixed.

**Positive preview**
- Navigating away from an image in Positive crop review with an
  unconfirmed crop or exposure adjustment, then back, no longer discards
  it.

**Interface**
- Typing or pasting an out-of-range value directly into a numeric slider
  field no longer bypasses its minimum/maximum limits.

### Documentation

- The user guide and README now cover tethered capture setup, including
  the Nikon USB-mode prerequisite and a Linux Mint USB-conflict some
  desktops run into.

## 1.6.0 — 2026-07-16

### New / Changed

**Project & Campaign**
- The Folders tab now has an "Open watched folder" button next to the
  watched folder field, alongside the existing "Open campaign folder"
  button.

## 1.5.0 — 2026-07-16

### New / Changed

**Project & Campaign**
- The project screen now has a "Start capture" button at the bottom,
  always visible below the tabs — a faster, more discoverable way to
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
  A remaining gap in the same fix meant the settle delay could still be
  cut short to a fraction of a second while capture was running; this is
  now fixed too, and the delay itself has also been lengthened.

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
