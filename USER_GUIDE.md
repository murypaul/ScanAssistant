# User guide

*[Version française disponible ici](USER_GUIDE.fr.md).*

This guide covers the full workflow: creating a campaign, running a
capture session, and everything the interface can do. For installation,
see [`README.md`](README.md).

## Overview

A **campaign** is one digitization run: a folder tree, a settings file, and
an inventory (a CSV list of names to assign, in order). Once a campaign is
open, **capture mode** does the rest of the work:

1. You drop a negative on the light table and shoot. The camera (or its
   transfer tool) writes the RAW file into a watched folder.
2. The app detects the new file, waits until it stops growing (the copy is
   finished), then moves and renames it against the next inventory entry.
3. A preview appears with the detected crop and a confidence indicator.
4. TIFF, JPEG master, and JPEG positive exports are generated in the
   background, with metadata attached.
5. You load the next object. The previous one is automatically accepted
   once the next one arrives — reject it first (`R`) if something was
   wrong.

Nothing is ever deleted: a rejected RAW is moved to `REJECTED/`, a replaced
file goes to `BACKUP/`, and the watched folder is only ever cleared of
files that have been safely ingested.

## Creating a campaign

From the home screen, **New campaign** opens a short wizard:

1. **Identity** — name, description, operator, institution, medium.
2. **Folders** — where the campaign lives, and which folder to watch for
   incoming files (files dropped there get moved into the campaign once
   verified).
3. **CSV** — pick the inventory file; the app detects its dialect and lets
   you confirm which column holds the names, with a preview and a
   validation report before anything is imported.
4. **Capture & framing** — default orientation, output size mode, margins,
   whether automatic framing is on.
5. **Exports** — settings for TIFF, JPEG master, and JPEG positive.
6. **Metadata** — IPTC fields written to every export (creator, institution,
   copyright, collection, keywords).
7. **Summary** — review, then create. Everything here stays editable later
   from the project screen.

## The project screen

Opened outside capture mode. A **Start capture** button sits below the
tabs at all times — faster to reach than the menu or the shortcut.
Tabs: **Summary**, **Folders**, **Capture**, **Framing**, **Exports**,
**Metadata**, **CSV** (a read-only table of the inventory — search,
filter by status, jump the cursor to a row), and **Log** (today's events,
filterable, with a shortcut to the log folder).

Every setting change is applied and saved immediately — there's no separate
save step.

The **Summary** tab has a **Reset campaign…** button (confirmation
required) to start the campaign over: every row goes back to todo, the
cursor returns to the start, and the TIFF/JPEG exports produced so far are
deleted (they're always regenerable from the RAW). Captured negatives are
never deleted — they're moved into a dated folder under `BACKUP/` instead,
so the RAW folder becomes empty and ready for new captures under the same
names without losing anything. Campaign settings themselves aren't touched.

## Capture mode

Full-screen-capable, one image at a time:

```
┌──────────────────────────────────────────────────────────────────┐
│ File  Project  Capture  Processing  Metadata  View  Help          │
├──────────────────────────────────────────────────────────────────┤
│  NEG_00125            ● RELIABLE 0.94                              │
├──────────────────────────────────────────────────────────────────┤
│                                                                    │
│                  [ current image preview ]                        │
│              detected crop overlaid, color-coded                  │
│                                                                    │
├──────────────────────────────────────────────────────────────────┤
│  next: NEG_00126   export queue: 2   127/842 · 15%                │
│  TIFF written (NEG_00124)                            ● CAPTURE    │
└──────────────────────────────────────────────────────────────────┘
```

Nothing is ever drawn on top of the image itself except the crop overlay
— name, confidence, and progress live in bars above and below the
preview. The crop overlay is green (reliable), orange (needs a look), or
red (couldn't be determined — falls back to the full frame); it carries
a dark keyline on both sides of its color so it stays visible whatever
the negative's own cast happens to be. Press `T` to preview the crop
actually applied instead of the raw frame with an overlay.

A small, translucent luminance histogram sits in a corner of the preview
— a quick read on exposure at a glance. It follows the plain negative
view only, not the master preview toggle above. There is no positive
preview during capture: judging tone belongs to the dedicated
[Positive calibration](#positive-calibration) screen, after capture — see
below. Capture itself is judged on the raw negative and its histogram,
which is enough to catch an exposure problem at a glance.

## Keyboard shortcuts

Everything below works without touching the mouse; the mouse remains
available everywhere too. Shortcuts are letter-based (not tied to a
keyboard layout). These are the defaults — remap any of them from
**File ▸ Preferences ▸ Shortcuts** (click the current key, then press the
new one). The crop's arrow/+/− moves and Ctrl+arrow rotation stay fixed —
they're a spatial gesture, not a pick-a-key shortcut — and so does Esc's
role as cancel in the name conflict panel. Tab is remappable in Capture
(below) but always keeps its usual field/option navigation role in the
name conflict panel, since only one of the two contexts is ever active at
a time.

### Capture

| Key | Action |
| --- | --- |
| Enter | Accept the current image (same as the next one arriving) |
| R | Reject the current image |
| V | Rotate 90° (cycles 0°→90°→180°→270°) — Shift+V rotates the other way |
| Ctrl+G | Go to an existing pending name (autocompletes as you type) |
| C | Recompute the frame (rerun automatic detection) |
| T | Toggle master (applied-crop) preview |
| K | Cycle preview (negative → master), same toggle as T — Shift+K cycles the other way |
| Tab | Pause / resume |
| Space | Trigger a remote capture (tethered camera only, see [Tethered capture](#tethered-capture-live-view--remote-trigger) below) |
| L | Toggle live view on/off (tethered camera only) |
| W | Pick white balance from a neutral point in the preview (click after pressing W) — applies to the rest of the session, both the preview and the exports; Esc cancels |
| F11 | Full screen |
| Esc | Stop capture (returns to preparation; exports keep processing) |

### Adjusting the crop

No mode to enter — always available on the plain negative view (switches
you back to it automatically if the master preview was open):

| Input | Action |
| --- | --- |
| Arrows | Move the frame by 1 preview pixel (Shift: ×10) |
| + / − | Grow / shrink by 1% (Shift: 5%), centered |
| Ctrl+← / Ctrl+→ | Rotate ∓0.1° (Shift: ×10), clamped to ±45° |
| G | Toggle rule-of-thirds guide lines |
| Drag a border or corner (mouse) | Resize from that side/corner |
| Drag the interior (mouse) | Move the whole frame |

Every edit settles into a single export after you pause for a moment (or
immediately, on mouse release) — no need to confirm or cancel anything. If
an edit goes wrong, drag it back or press `C` to fall back to the
automatic detection.

### White balance

The camera's own automatic white balance drifts from shot to shot
depending on what's in the frame — but the light table underneath stays
the same for the whole session, so a fixed balance chosen once reads more
consistently than a fresh guess on every negative.

Press `W` on the first capture of the session (a shot of the bare light
table works well) — the cursor becomes a crosshair — then click a neutral
point in the preview. That balance applies from then on to both the
on-screen preview and every export, for the rest of the time the campaign
stays open (stopping and restarting capture mode doesn't reset it). `W`
again or `Esc` cancels the pick without changing anything.

### Name conflict panel

| Key | Action |
| --- | --- |
| 1 / 2 / 3 | Pick the matching option |
| Tab / Shift+Tab | Move between options and fields |
| Enter | Confirm the selected option |
| Esc | Same as option 1 with an empty field |

### Positive calibration screen

Mouse Ctrl+click/Shift+click also multi-selects thumbnails in the grid.

| Key | Action |
| --- | --- |
| Enter | Confirm and next — applies the current image's settings, advances in the active filter |
| Up / Down | Previous / next image in the filtered list |
| Page Up / Page Down | Jump further in the same list |
| Ctrl+A | Select every image in the active filter |
| Ctrl+Enter | Apply to selection — copies the current image's tone settings to the selection (Dmin excluded by default) |
| Ctrl+Z / Ctrl+Y | Undo / redo the last confirmed change or propagation |
| Esc | Close the screen, back to the project |

### Everywhere else

| Key | Action |
| --- | --- |
| Ctrl+N | New campaign |
| Ctrl+O | Open a campaign |
| Ctrl+Q | Quit |
| F5 | Start capture |
| Ctrl+F | Search in the CSV viewer |
| F1 | This shortcut list |
| F11 | Full screen |
| Alt+letter | Open the matching menu |

## Tethered capture (live view & remote trigger)

Optional, off by default (enable it in **File ▸ Preferences ▸ Camera** —
takes effect the next time the app starts). Nikon's own firmware turns
off the camera's rear screen while it's connected over USB, so there's no
way to see what you're framing on the camera itself — this feature
displays a live view feed on screen instead, and lets you fire the
shutter from the keyboard. It does not replace loading film and framing
by hand, and it doesn't remotely control exposure, ISO, aperture, or
autofocus — only the live feed and the shutter.

The **Nikon D750** is the first supported body (USB/PTP). Before
connecting:

- On the camera, set **Setup menu ▸ USB** to **PTP/MTP** (not *Mass
  Storage*) — otherwise the OS mounts it as a regular USB drive instead of
  a camera.
- On Linux Mint (and other Cinnamon/GNOME desktops), the OS's own
  `gvfs`/`gvfsd-gphoto2` service tries to claim the camera first and can
  block the app with a "USB connection in use" message — close it (or
  disable `gvfsd-gphoto2` and `gvfs-gphoto2-volume-monitor`'s autostart)
  before opening capture mode.
- The camera can only hold one USB connection at a time, so the
  campaign's watched folder can't be the camera's own memory-card mount
  point while tethered capture is on — point it at a regular local
  folder instead (the captured file gets downloaded there automatically).

Once enabled and the camera is connected:

- **`L`** toggles the live view vignette on/off, in the corner of the
  preview area. It never starts on its own — turning it on keeps the
  camera's mirror raised, so switch it off again once you're done
  checking the frame.
- **`Space`** fires the shutter remotely and downloads the resulting RAW
  straight into the watched folder over the same connection — nothing to
  plug, unplug, or remount between shots. From there it's picked up
  exactly like a manually transferred file: naming, preview, and exports
  all work the same way. The negative also stays on the memory card, so
  a dropped connection or a failed download doesn't lose the shot.
- Drag the opacity slider on the vignette down to see the last accepted
  preview through it; click the vignette to expand it and use the mouse
  wheel/drag to zoom and pan for a close look at focus, click again (or
  the shrink icon) to go back to the small vignette.
- Drag the collapsed vignette itself to reposition it anywhere over the
  preview (a plain click still expands it) — it stays put across restarts.
- The fps setting is a ceiling, not a guarantee — USB 2.0 live view
  typically tops out around 10-20 fps regardless of what's selected; the
  vignette shows the fps actually being achieved next to the setting.

## Framing & confidence

The crop is detected automatically on the embedded preview using classic
computer vision (no ratio or size assumed — it works on mixed formats).
Each detection gets a confidence score from five independent checks:
how well the rectangle fills the frame, how rectangular it is, its
plausible size, whether it touches the image border, and its solidity.

- **Reliable** (green) — used as-is.
- **Needs review** (orange) — worth a glance before moving on.
- **Impossible** (red) — a severely underexposed negative (near-zero
  contrast against the light table) gets a second, more thorough attempt
  automatically before giving up; if that also fails, falls back to the
  full, uncropped frame — fix it manually if needed (see *Adjusting the
  crop* above).

Manual edits and re-detections regenerate the exports for that image only;
already-finalized images are untouched (use completeness check +
regenerate to fix those instead).

## Positive calibration

There's no positive preview during capture — judging tone happens entirely
after capture, in **Project ▸ Positive calibration** (also reachable
outside capture), a dedicated screen that takes over the main window, same
as capture mode rather than a separate floating window. Capture itself is
judged on the raw negative and its histogram.

Two interchangeable positive engines, set per campaign:

- **Legacy** — the original tone-curve pipeline, three rendering modes
  (**simple**: plain linear min/max normalization; **auto**, the default:
  a deterministic exposure/gamma optimization, no machine learning, same
  output every time for the same input; **manual**: campaign-wide
  exposure/shadows/highlights/contrast).
- **Density-domain** (`print_engine`) — reconstructs the darkroom print
  process itself instead of stretching an already-inverted curve: a
  per-channel film base (Dmin, sampled from the negative's own unexposed
  border — this is what absorbs a yellowed or stained base into a neutral
  result), a fixed film response curve, and a paper response (exposure,
  contrast, black point, highlight soft-clip). More physically grounded,
  at a real cost: a dedicated RAW decode and a much heavier render (several
  seconds to tens of seconds per image, depending on your machine) — during
  capture it runs on a separate worker pool sized by **Preferences ▸
  Processing ▸ Positive finalize workers**, so it never slows down the
  TIFF/JPEG master export.

The reading positive also automatically excludes the negative's unexposed
border from its crop whenever it can confidently tell the two apart — the
master TIFF and JPEG keep the full framed negative regardless, border
included, for archival fidelity. Automatic exposure is never thrown off by
that border either way, whether or not the extra crop above succeeds. An
image the engine isn't confident about — crop or, for the density-domain
engine, tone — is left flagged for review rather than silently accepted.

### The calibration screen

A grid of thumbnails (reusing the already-exported JPEG files, no extra
decode) on the left, filterable by checkable category — flagged for
review by default, but also already applied confidently or already
confirmed manually, if you want to double-check either — with multi-select
(click, Ctrl/Shift-click, or `Ctrl+A` for everything in the current
filter). The right-hand panel depends on the campaign's engine:

- **Legacy engine**: the same exposure/shadows/highlights/contrast
  controls as capture-time manual mode, plus a draggable crop rectangle on
  the preview, live-updated as you adjust.
- **Density-domain engine**: a draggable crop rectangle too, on the full
  (uncropped) preview, and four tone groups — film base (Dmin), scan
  exposure, film model (fixed, shown for reference only — not adjustable
  here, it's a single response curve applied to every image regardless of
  film stock, not a per-image or per-campaign choice), and paper model
  (contrast, with black point/soft-clip under an **Advanced** toggle that
  stays open or closed as you move between images). Each group starts on
  **Auto** — its own switch (labeled *Auto*, on by default) takes it to
  **Manual** when switched off, without disturbing the others. Because a
  real render is expensive, the preview only updates once a change is
  committed (released a slider or the crop handle, toggled a switch,
  pressed Enter in a field), not continuously while dragging — the screen
  locks with a status message for that one render, but the window itself
  stays responsive throughout rather than freezing. The first RAW decode
  of a given image is the only genuinely slow step; revisiting an image
  already opened earlier in this screen session, or committing a further
  change to it, reuses that decode and only re-runs the much cheaper tone
  math.

`Enter` (or **Confirm & next**) applies the current image's settings and
moves to the next one in the filtered list. For the density-domain
engine, **Apply to selection** (button, or `Ctrl+Enter`) copies the
current image's tone settings to every other selected image at once —
useful for a whole roll shot under the same conditions. Dmin is excluded
from that propagation by default (tick **Include Dmin** to override): it's
a physical measurement specific to that one negative's own border, not an
aesthetic choice to copy blindly across a whole selection. The crop is
never propagated either way — it's specific to each negative's own
physical framing, not something to copy across images. A confirmation
shows how many images are affected before anything is regenerated.

`Ctrl+Z` / `Ctrl+Y` undo and redo the last confirmed change or
propagation — never an in-progress drag — for the current screen session
(the stack doesn't persist once you leave the screen). `Esc` returns to
the project screen. A confirmed choice sticks even if the image is
reprocessed later (after a crash, a retry).

The master TIFF and JPEG are never touched by any of this — only the
reading positive is affected.

## Name conflicts

If the name the app is about to assign already exists on disk, capture
pauses on that file (later shots keep queuing up behind it) and a panel
appears:

- **Rename current image** — give the incoming file a different name
  (defaults to `<NAME>_BIS`); the original inventory row stays pending.
- **Replace existing** — move every existing file under that name (RAW and
  exports) into `BACKUP/`, then ingest the new one under the original name.
- **Rename existing file** — rename the existing files instead (defaults to
  `<NAME>_OLD`) and ingest the new one under the original name.

Nothing is ever silently overwritten.

## Alerts, errors, and recovery

Three levels, always non-blocking:

- **Info** — status line, disappears after 5 seconds. Routine events only;
  nothing that needs acknowledging.
- **Warning** — a banner above the status line, click for details or the
  × to dismiss it. Doesn't stop anything (e.g. missing metadata tool, a
  leftover file that couldn't be cleaned up, the export queue growing).
- **Critical** — a red banner and the pipeline pauses (e.g. disk nearly
  full, a folder became inaccessible). Detections keep queuing; nothing is
  lost. Resolve the cause, then click **Resume processing**.

If the app was closed forcefully (crash, power loss), reopening the
campaign shows a short recovery report: the image that was in progress
gets finalized, orphaned temporary files are cleaned up, and any export
that didn't finish is queued again — automatically, nothing to redo by
hand.

Closing normally while exports are still processing shows a small
"Finalizing: N export(s) pending" panel instead of freezing the window —
wait it out, or click **Quit without waiting** to close immediately (those
exports pick up automatically next time the campaign opens, same as after
a crash). Whether to wait at all is set in File ▸ Preferences ▸
Processing.

## Statistics & completeness

**Project ▸ Statistics** (also available outside capture, once a campaign
has entries) shows totals, done/pending/rejected/error counts, and a
**completeness check**: for every entry marked done, it confirms the
renamed RAW and every expected export actually exist on disk, lists what's
missing, and can regenerate the selection in one action.

## Configuration

Campaign settings live in `campaign.json` inside the campaign folder and
are edited from the project screen — no manual editing needed.

Machine-wide preferences live in **File ▸ Preferences** (disabled during
capture, like most menus), applied and saved as soon as you change them:

- **General** — reopen the last project on startup.
- **Processing** — the exiftool path (with a browse/test button), whether
  closing waits for exports still in progress, the maximum negative name
  length accepted when importing a new CSV, and the number of worker
  threads dedicated to the density-domain positive engine during capture
  (see [Positive calibration](#positive-calibration)).
- **Thresholds** — the disk-space warning/critical levels and the export
  queue's early-warning size.
- **Updates** — the opt-in startup check (see [Updating](README.md#updating)
  in the README) and a manual check button.
- **Camera** — enables tethered capture (see
  [Tethered capture](#tethered-capture-live-view--remote-trigger) below).
  Off by default; takes effect the next time the app is started.
- **Shortcuts** — every remappable key (see above), with per-key and
  global reset.

**Export settings…**/**Import settings…**, at the bottom of that dialog,
save or load the whole file (including your shortcut remaps) as JSON — a
quick way to move your setup to another machine or keep a backup before
experimenting. Everything here also lives in a plain JSON file managed by
the OS (`platformdirs` — e.g. `~/.config/scanassistant/config.json` on
Linux), for anyone who prefers editing it directly.
