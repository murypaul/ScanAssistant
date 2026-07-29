# Changelog

## 1.20.0 — 2026-07-29

### New / Changed
**Positive calibration**
- Left/Right now move the content-frame crop as well (same 1 px, 10 px
  with Shift convention as Up/Down), instead of browsing between images —
  the operator only ever moves between images by click, Page Up/Page
  Down or Enter here, so all four arrows are now dedicated to the crop.

## 1.19.1 — 2026-07-28

### Bug Fixes
**Positive calibration**
- Picking Dmin from the image could leave the screen permanently stuck
  showing "Rendering…", with nothing clickable, until the app was
  restarted — two full renders could end up running at once (the group's
  Auto-to-Manual switch already renders on its own for a first pick, and
  a second one was queued right behind it), and two at once could hang
  indefinitely. Committing a setting no longer starts a new render while
  one is still in flight, whatever triggered it.

## 1.19.0 — 2026-07-28

### New / Changed
**Positive calibration**
- Up/Down now move the content-frame crop (1 px, 10 px with Shift), the
  same convention as the support-frame nudge in Capture, instead of
  browsing between images — the same keys did different things in the
  two screens, which read as inconsistent. Space and Page Up/Page Down
  now cover browsing.

**Project & Campaign**
- New "Export queue" tab on the project screen, next to CSV and Log —
  the same pending-tasks list already shown in the capture screen's own
  dock, now also reachable outside of an active capture session (e.g.
  while a Statistics "Regenerate selection" is still draining in the
  background).

## 1.18.1 — 2026-07-28

### Bug Fixes
**Positive calibration**
- Space, pressed twice in quick succession before the first image had
  actually appeared, could silently skip a second one — now ignored
  until the previous move's image is genuinely on screen.

## 1.18.0 — 2026-07-28

### New / Changed
**Capture**
- Renaming the current image onto a name already used on disk (redoing a
  bad shot under its original name) now offers the same duplicate-name
  choices — replace, back up the existing files under another name, or
  use a different name instead — rather than a dead end.

**Positive calibration**
- Space now moves to the next image, same as Down — the same "keep moving
  with one hand" habit as Capture's own Space, even though the two keys
  trigger unrelated actions in each screen.
- V/Shift+V rotates the image 90° clockwise/counter-clockwise, for an
  orientation missed during capture and only caught here while judging
  tone. Re-exports the TIFF/JPEG master along with the positive.

**Installation & Updates**
- If an update is blocked by uncommitted local changes to installed
  files, you're now offered to discard them and update anyway — naming
  exactly which files would be lost — instead of being stuck on a bare
  error with no way forward.

### Bug Fixes
**Capture**
- The automatic frame detection's margin could occasionally push the crop
  rectangle past the image's own edges on its own, with no action from
  the operator. Manual dragging past the edge on purpose is unaffected.
- Correcting an earlier capture from the "Session history" panel, then
  leaving and reopening the capture screen (or restarting mid-campaign),
  could show it out of order — ahead of a later capture that was never
  touched.
- A capture made right before leaving the capture screen, before its
  preview had time to load, still exported correctly but could silently
  disappear from "Session history".
- Resolving a name conflict by typing a name and pressing Enter could
  accidentally finalize whatever image was currently under review, as an
  unrelated side effect.

## 1.17.0 — 2026-07-28

### New / Changed
**Capture**
- Adjusting a crop or rotation several times before moving on to the next
  image no longer produces one full export per adjustment — the archival
  TIFF, JPEG master, and reading positive are now produced once, from
  whatever you've settled on, exactly when you leave the image (accept or
  reject it). No change to what gets exported or when a crop/rotation edit
  is saved, only to when the heavier files actually get written.
- Reopening an already-processed image from the capture screen's "Session
  history" panel now returns keyboard focus to the preview immediately —
  V/Shift+V and every other capture shortcut used to stay unresponsive
  until clicking back into the preview by hand.
- Reopening an already-reviewed image for correction now also restores the
  positive's content crop as it was last seen, instead of resetting it.

**Project & Campaign**
- The Statistics screen's "Regenerate selection" now shows real progress
  (how many images are left, and when it's actually done) instead of
  refreshing the list instantly against work that hadn't run yet — the
  list used to look like nothing had happened at all. Added a permanent
  explanation of what the completeness check and regeneration actually do.

### Bug Fixes
**Capture**
- A still-pending export queued in the background could silently lose its
  crop/rotation and be marked done without ever producing a file, if the
  campaign was closed and reopened (or the app restarted) before it
  finished draining — not just after a crash. It's now rebuilt from the
  campaign's own history before running; a truly unrecoverable case (the
  source RAW gone, or never framed) now shows up as an error instead of
  vanishing silently.

## 1.16.0 — 2026-07-27

### New / Changed
**Positive calibration**
- There's no separate "Confirm" step anymore: an image reviewed on the
  calibration screen counts as done as soon as it's left — moving to
  another image, or closing the screen — whether or not anything was
  actually changed. Enter still applies the current image's settings and
  jumps ahead right away, for moving fast through a batch of real edits.
- Leaving an image untouched is now instant: nothing is re-rendered, only
  its reviewed status is recorded, so browsing through an already-good
  campaign to spot-check it costs nothing per image.
- An image confirmed after fixing its framing no longer keeps showing as
  still needing review just because the automatic tone estimate stayed
  uncertain — a confirmed crop is trusted regardless.

### Bug Fixes
**Positive calibration**
- Ctrl+Left/Right (rotate the content-frame crop) could be silently
  swallowed by the thumbnail grid instead of rotating the crop, whenever
  the grid itself had keyboard focus — the normal state right after
  clicking a thumbnail.
- Reopening a campaign later, outside of an active capture session, to
  review its positives still ran every export synchronously in the
  background, freezing "Confirm and next" for the length of the render —
  the same freeze already fixed for an active capture session previously,
  now fixed for this path too. Reopening the same campaign again in one
  sitting no longer risks acting on stale project data, and closing the
  app right after confirming something no longer risks a silent freeze
  while the export finishes.

## 1.15.0 — 2026-07-25

### New / Changed
**Positive calibration**
- The content-frame crop can now be rotated (deskewed) with Ctrl+Left/Right,
  same behavior and shortcut as the support-frame crop already has in
  Capture — useful when a negative was laid down slightly askew on the
  light table, independently of any support-frame correction already made
  during capture.
- Dragging a tonal slider (exposure, contrast, paper black/soft-clip) now
  shows a real-time preview while dragging, instead of only updating once
  the slider is released.

### Bug Fixes
**Capture**
- The remote shutter trigger could fail silently on a well-known transient
  "camera busy" condition — the shutter (and sometimes the mirror) would
  fire with nothing shown on screen at all, and no automatic retry. Now
  retries automatically, the same way live view startup already did.
- Live view could drop out after a real capture more often than expected;
  widened the tolerance for the brief interruption a real exposure causes.
- Repeated, identical camera-connection error messages while the camera
  stays disconnected no longer flood the technical log.
- The app could become impossible to close if the "exports still
  finishing" panel shown while quitting was dismissed by its own close
  button instead of the "Quit without waiting" button.

**Positive calibration**
- Confirming an image, or moving to the next one, no longer waits for the
  actual export to finish rendering in the background — matches how
  Capture already handles its own exports, and removes the pause that
  used to follow every single confirmation.
- A confirmed image could stay listed as still needing review even though
  it had already been processed.
- Fixed a rare race where two background renders of the same positive
  image could finish out of order, letting an older tonal/crop adjustment
  silently overwrite a more recent one on disk.

**Exports & Metadata**
- A stuck metadata-writing step could freeze all further exports
  indefinitely with nothing shown on screen — now gives up after 30
  seconds and continues, same as any other metadata failure already does.
- Fixed a rare crash from two parts of the app saving campaign state at
  the same moment.

### Performance
- Reduced CPU contention between the background workers finalizing
  positive images concurrently.
- The positive calibration screen now prepares two upcoming images in
  advance instead of just one.
- Browsing between images in the positive calibration screen no longer
  re-reads the whole campaign log on every single navigation — this was
  the main reason the screen kept getting slower as a campaign grew
  larger.
- The on-screen histogram is now computed on a lighter sample instead of
  scanning every pixel of the full preview on every render.

### Documentation
- Keyboard shortcuts guide updated with the new content-frame rotation
  shortcut in the positive calibration screen.

## 1.14.0 — 2026-07-24

### New / Changed
**Positive rendering**
- Removed the older positive rendering engine entirely: the darkroom-print
  engine introduced in 1.13.0 is now the only one. There's no longer a
  per-campaign engine choice, and the simple/auto/manual mode selector is
  gone from the project screen — the calibration screen's own per-image
  Auto/Manual controls already covered that need, and covered it correctly
  (the campaign-wide selector didn't, see Bug Fixes below).
- Added a way to set the film base (Dmin) by clicking directly on the
  negative instead of deciding three RGB sliders by eye on the rendered
  positive — picks up the actual color at the clicked point.

### Bug Fixes
**Positive rendering**
- Opening the positive calibration screen, or moving between images there,
  still took a couple of seconds even on an already-processed campaign: the
  content-frame detection ran a second time from scratch instead of reusing
  the one already computed during capture. A "Redetect frame" button covers
  the rare case where a fresh detection is actually wanted.
- Switching a calibration setting back to Auto after adjusting it manually
  left the slider showing the old manual value instead of the automatic
  estimate — the calculation itself already ignored that stale value, only
  the display was wrong.
- The simple/auto/manual positive mode was a single setting for the whole
  campaign rather than per image: changing it on the project screen could
  silently affect every negative not yet regenerated, including ones
  already reviewed. Resolved by removing the setting along with the rest of
  the older engine (see above) — the calibration screen's per-image
  Auto/Manual controls don't have this problem.

## 1.13.0 — 2026-07-24

### New / Changed
**Capture**
- Added a "Simple" campaign mode: watch a folder, stabilize, assign the
  next inventory name, move the RAW — nothing else. No crop detection,
  no TIFF/JPEG/positive exports. Meant for campaigns that only need the
  RAWs filed under the right names. Renaming the current image on the
  fly is its primary way to correct a name, on a dedicated key.
- The current image can now be renamed from the Capture menu in normal
  campaigns too, moving its RAW and any already-produced exports
  together.
- If a capture arrives after every inventory row has already been used,
  a name field now opens right on the capture screen so you can name it
  on the spot, instead of a blocking error telling you to edit the CSV.

**Positive rendering**
- Added a new positive rendering engine, selectable per campaign:
  reconstructs the darkroom print process (film response, then paper
  response, sampling the film base directly off the negative's own
  border) instead of stretching an already-inverted image — meant to be
  more physically faithful, especially on unevenly aged or stained film.
- Added a dedicated positive calibration screen (Project ▸ Positive
  calibration) for reviewing and adjusting positives after capture: film
  base, exposure, contrast, and paper black/soft-clip, each starting
  automatic with a manual override, undo/redo, and "apply to selection"
  to copy a look across several negatives at once (the film base is
  deliberately excluded from that propagation — it's a physical
  measurement specific to each negative, not an aesthetic choice).
- The positive's crop can now be dragged and adjusted on this screen for
  the new engine too, not only the previous one.
- Moving between images on this screen is now instant — it no longer
  waits for the previous image's render, and a slow render can no longer
  land on the wrong image if you've already moved on by the time it
  finishes.
- Opening the calibration screen for an already-processed campaign no
  longer re-decodes every negative from scratch: captured images are
  ready to review immediately, since capture itself already did that
  work in the background.

**Framing**
- Support-frame detection reworked: the detected frame no longer
  systematically excludes the negative's own unexposed border, and is
  reliable on far more images than before.

### Bug Fixes
**Positive rendering**
- Two confirmations submitted close together for the same image (e.g. a
  quick edit followed immediately by another) could occasionally leave
  the older one's render as the final result instead of the latest one.
- Paper black/soft-clip are now applied to each color channel before the
  monochrome conversion, instead of after — matching how a real paper's
  response actually works.

### Documentation
- User guide: documented the simple capture mode, the positive engine
  choice, the new positive calibration screen, and renaming an image on
  the fly (English and French).

## 1.12.0 — 2026-07-22

### New / Changed
**Positive review**
- The positive crop review screen can now also show images the automatic
  detector already cropped confidently, or already confirmed manually —
  not just the ones flagged for review — via two new checkboxes, so a
  crop can be double-checked or corrected even when it wasn't flagged.
  Reopening one of those starts from its actual crop, not a generic
  centered guess.
- A manual crop or exposure choice confirmed on this screen now survives
  a later reprocessing of the same image (after a crash, a retry)
  instead of silently reverting to the automatic result.
- The preview now shows the actual positive (inverted, exposure-corrected)
  instead of the raw negative, updated live as exposure settings change.
- A small luminance histogram, matching the one already in capture mode,
  now sits in a corner of this preview too.

### Bug Fixes
**Positive review**
- Confirming an image whose new status was still shown by the checked
  filters (e.g. "Applied" and "Already confirmed manually" both checked)
  could get stuck re-confirming the very same image over and over
  instead of moving on to the next one.
- Re-rendering the preview on every settings change or image change could
  take several seconds on a full-resolution negative, making it look
  frozen — now near-instant.

## 1.11.0 — 2026-07-21

### New / Changed
**Project & Campaign**
- New "Reset campaign…" button (Project ▸ Summary, confirmation required)
  to start a campaign over: every row goes back to todo, the cursor
  returns to the start, and TIFF/JPEG exports produced so far are
  deleted. Captured negatives are never deleted — they're archived into a
  dated backup folder instead, so the campaign becomes ready to restart
  under the same names without losing anything.
- The positive crop review screen is now part of the main window, the
  same way capture mode is, instead of a separate popup window — Esc
  returns to the project screen.

**Interface**
- A small, translucent luminance histogram now sits in a corner of the
  capture preview for a quick read on exposure.
- The live view vignette can now be dragged anywhere over the preview
  (a plain click still expands it as before) and stays where it's put
  across restarts, instead of always sitting in the bottom-right corner.

### Bug Fixes
**Capture**
- Session white balance could compute a badly wrong correction — a
  strong pink cast instead of a neutral result — because it measured the
  correction on already color-processed pixels instead of the camera's
  raw sensor data. It's now measured correctly and matches the camera's
  own calibrated white balance.
- The crop frame actually applied to exports (not just the on-screen
  overlay) could land at roughly half its intended position and size
  once a white balance had been set for the session, squeezing the
  negative into a corner of the exported image. Fixed.
- A remote-triggered capture could silently disconnect the camera and
  cut live view instead of completing normally — a brief, expected pause
  in the live feed right after the shutter fires was being mistaken for
  the camera having been unplugged.
- A remote-triggered download that received no file is now recorded in
  the technical log instead of leaving no trace at all.

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
