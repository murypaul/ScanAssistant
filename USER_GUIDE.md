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

Opened outside capture mode. Tabs: **Summary**, **Folders**, **Capture**,
**Framing**, **Exports**, **Metadata**, **CSV** (a read-only table of the
inventory — search, filter by status, jump the cursor to a row), and
**Log** (today's events, filterable, with a shortcut to the log folder).

Every setting change is applied and saved immediately — there's no separate
save step.

## Capture mode

Full-screen-capable, one image at a time:

```
┌──────────────────────────────────────────────────────────────────┐
│ File  Project  Capture  Processing  Metadata  View  Help          │
├──────────────────────────────────────────────────────────────────┤
│                                                                    │
│                  [ current image preview ]                        │
│              detected crop overlaid, color-coded                  │
│                                                                    │
├──────────────────────────────────────────────────────────────────┤
│  NEG_00125            ● RELIABLE 0.94            127/842 · 15%    │
│  next: NEG_00126                              export queue: 2     │
├──────────────────────────────────────────────────────────────────┤
│  TIFF written (NEG_00124)                            ● CAPTURE    │
└──────────────────────────────────────────────────────────────────┘
```

The crop overlay is green (reliable), orange (needs a look), or red
(couldn't be determined — falls back to the full frame). Press `T` to
preview the crop actually applied instead of the raw frame with an
overlay; press `P` to preview the positive rendering instead.

## Keyboard shortcuts

Everything below works without touching the mouse; the mouse remains
available everywhere too. Shortcuts are letter-based (not tied to a
keyboard layout). These are the defaults — remap any of them from
**File ▸ Preferences ▸ Shortcuts** (click the current key, then press the
new one). The frame-editing arrow/+/− moves stay fixed — they're a spatial
gesture, not a pick-a-key shortcut — and so do Tab and Esc's roles as
navigation/cancel in the name conflict panel.

### Capture

| Key | Action |
| --- | --- |
| Enter | Accept the current image (same as the next one arriving) |
| R | Reject the current image |
| V | Rotate 90° (cycles 0°→90°→180°→270°) |
| ← / → | Previous / next name (moves the cursor among pending entries) |
| G | Go to an existing pending name (autocompletes as you type) |
| C | Recompute the frame (rerun automatic detection) |
| M | Edit the frame manually |
| P | Toggle positive preview |
| T | Toggle master (applied-crop) preview |
| Space | Pause / resume |
| F11 | Full screen |
| Esc | Stop capture (returns to preparation; exports keep processing) |

### Frame editing (after `M`)

| Key | Action |
| --- | --- |
| Arrows | Move the frame by 1 preview pixel (Shift: ×10) |
| + / − | Grow / shrink by 1% (Shift: 5%), centered |
| Ctrl+← / Ctrl+→ | Rotate ∓0.1° (Shift: ×10), clamped to ±45° |
| C | Rerun automatic detection |
| Enter | Confirm (marks the frame as manual, regenerates exports) |
| Esc | Cancel, back to the previous frame |

### Name conflict panel

| Key | Action |
| --- | --- |
| 1 / 2 / 3 | Pick the matching option |
| Tab / Shift+Tab | Move between options and fields |
| Enter | Confirm the selected option |
| Esc | Same as option 1 with an empty field |

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

## Framing & confidence

The crop is detected automatically on the embedded preview using classic
computer vision (no ratio or size assumed — it works on mixed formats).
Each detection gets a confidence score from five independent checks:
how well the rectangle fills the frame, how rectangular it is, its
plausible size, whether it touches the image border, and its solidity.

- **Reliable** (green) — used as-is.
- **Needs review** (orange) — worth a glance before moving on.
- **Impossible** (red) — falls back to the full, uncropped frame; fix it
  manually (`M`) if needed.

Manual edits and re-detections regenerate the exports for that image only;
already-finalized images are untouched (use completeness check +
regenerate to fix those instead).

## Positive preview

Three rendering modes for the JPEG reading positive, set per campaign:

- **simple** — plain linear min/max normalization.
- **auto** (default) — a deterministic exposure/gamma optimization, no
  machine learning involved, same output every time for the same input.
- **manual** — campaign-wide exposure, shadows, highlights, and contrast
  settings, adjustable live during capture with a preview (`P`).

The master TIFF and JPEG are never touched by this — only the reading
positive is affected.

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
- **Warning** — a banner above the status line, stays until the cause is
  gone, click for details. Doesn't stop anything (e.g. missing metadata
  tool, a leftover file that couldn't be cleaned up).
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
  closing waits for exports still in progress, and the maximum negative
  name length accepted when importing a new CSV.
- **Thresholds** — the disk-space warning/critical levels and the export
  queue's early-warning size.
- **Updates** — the opt-in startup check (see [Updating](README.md#updating)
  in the README) and a manual check button.
- **Shortcuts** — every remappable key (see above), with per-key and
  global reset.

**Export settings…**/**Import settings…**, at the bottom of that dialog,
save or load the whole file (including your shortcut remaps) as JSON — a
quick way to move your setup to another machine or keep a backup before
experimenting. Everything here also lives in a plain JSON file managed by
the OS (`platformdirs` — e.g. `~/.config/scanassistant/config.json` on
Linux), for anyone who prefers editing it directly.
