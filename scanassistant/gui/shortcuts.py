"""Configurable keyboard shortcuts: the default map, validation, and matching.

A shortcut is stored as a plain `QKeySequence`-parseable string ("R",
"Ctrl+N", "F11"...). Contexts mirror the collision-priority stack already
used by `CaptureScreen.keyPressEvent` (text field > conflict > frame edit
> capture > global): the same physical key can be assigned in two
different contexts without that counting as a duplicate, since only one
context is ever active at a time.

Scope: only the single named-key actions (letters, function keys, arrows
used as plain navigation, Enter/Escape/Space, and the digit options in a
name conflict) are remappable here. Frame-edit move/resize/rotate (arrow
keys combined with Shift/Ctrl for magnitude) stay fixed — those are
spatial gestures, not a pick-a-letter shortcut, and remapping them would
add a lot of surface for very little real benefit.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QKeySequence

CAPTURE = "capture"
FRAME_EDIT = "frame_edit"
NAME_CONFLICT = "name_conflict"
GLOBAL = "global"

CONTEXTS = (CAPTURE, FRAME_EDIT, NAME_CONFLICT, GLOBAL)

DEFAULT_SHORTCUTS: dict[str, dict[str, str]] = {
    CAPTURE: {
        "finalize": "Return",
        "reject": "R",
        "rotate": "V",
        "recompute_frame": "C",
        "edit_frame": "M",
        "positive_preview": "P",
        "master_preview": "T",
        "cycle_preview": "K",
        "go_to_name": "G",
        "pause_resume": "Space",
        "stop_capture": "Escape",
        "navigate_previous": "Left",
        "navigate_next": "Right",
    },
    FRAME_EDIT: {
        "confirm": "Return",
        "cancel": "Escape",
        "recompute": "C",
        "toggle_guides": "G",
    },
    NAME_CONFLICT: {
        "option_1": "1",
        "option_2": "2",
        "option_3": "3",
    },
    GLOBAL: {
        "new_campaign": "Ctrl+N",
        "open_campaign": "Ctrl+O",
        "quit": "Ctrl+Q",
        "search_csv": "Ctrl+F",
        "start_capture": "F5",
        "shortcuts_help": "F1",
        "fullscreen": "F11",
    },
}

# 07 §1: only named letters, digits, function keys, arrows, Enter/Escape/Space —
# never a position-dependent punctuation key (AZERTY/QWERTY independence).
_ALLOWED_BASE_KEYS = (
    {Qt.Key(k) for k in range(Qt.Key.Key_A, Qt.Key.Key_Z + 1)}
    | {Qt.Key(k) for k in range(Qt.Key.Key_0, Qt.Key.Key_9 + 1)}
    | {Qt.Key(k) for k in range(Qt.Key.Key_F1, Qt.Key.Key_F12 + 1)}
    | {
        Qt.Key.Key_Left,
        Qt.Key.Key_Right,
        Qt.Key.Key_Up,
        Qt.Key.Key_Down,
        Qt.Key.Key_Return,
        Qt.Key.Key_Enter,
        Qt.Key.Key_Escape,
        Qt.Key.Key_Space,
    }
)

_FORBIDDEN = QKeySequence("Ctrl+S")

# Frame-edit reserves plain and Shift/Ctrl-modified arrows plus +/-/= for
# move/resize/rotate (analog gestures, not remappable, see the module
# docstring): a remapped action bound to one of these would silently never
# fire, since those keys are consumed first.
_FRAME_EDIT_RESERVED = {
    Qt.Key.Key_Left,
    Qt.Key.Key_Right,
    Qt.Key.Key_Up,
    Qt.Key.Key_Down,
    Qt.Key.Key_Plus,
    Qt.Key.Key_Equal,
    Qt.Key.Key_Minus,
}


def is_allowed_key(key_string: str, *, context: str | None = None) -> bool:
    """Rejects anything outside the allowed key class, and Ctrl+S specifically."""
    if not key_string:
        return False
    sequence = QKeySequence(key_string)
    if sequence.count() != 1 or sequence.isEmpty():
        return False
    if sequence == _FORBIDDEN:
        return False
    combination = sequence[0]
    key = Qt.Key(combination.key())
    if key not in _ALLOWED_BASE_KEYS:
        return False
    return not (context == FRAME_EDIT and key in _FRAME_EDIT_RESERVED)


# The main-keyboard Return and the numpad Enter are two distinct `Qt.Key`
# values that every physical keyboard layout still treats as "the same
# key" for this purpose — configuring one accepts both.
_RETURN_ENTER = {Qt.Key.Key_Return, Qt.Key.Key_Enter}


def matches(event: QKeyEvent, key_string: str) -> bool:
    """True if `event` is exactly the shortcut described by `key_string`."""
    if not key_string:
        return False
    configured = QKeySequence(key_string)
    if configured.isEmpty():
        return False
    combination = configured[0]
    configured_key = Qt.Key(combination.key())
    if configured_key in _RETURN_ENTER:
        return (
            Qt.Key(event.key()) in _RETURN_ENTER
            and event.modifiers() == combination.keyboardModifiers()
        )
    return QKeySequence(event.keyCombination()) == configured


def default_shortcuts() -> dict[str, dict[str, str]]:
    return {context: dict(actions) for context, actions in DEFAULT_SHORTCUTS.items()}


def conflicting_action(
    actions: dict[str, str], key_string: str, *, exclude_action: str | None = None
) -> str | None:
    """The other action already bound to `key_string` in this context, if any.

    Only within the same context: the same physical key legitimately
    appears in two different contexts (only one is ever active at a time).
    """
    target = QKeySequence(key_string)
    for action, existing in actions.items():
        if action == exclude_action:
            continue
        if QKeySequence(existing) == target:
            return action
    return None


def _drop_duplicate_overrides(context: str, actions: dict[str, str]) -> None:
    """Reverts an *overridden* action if its key collides with another one in

    the same context — defense in depth against a hand-edited or imported
    `config.json`; the Preferences editor should never let a duplicate
    through in the first place (`conflicting_action`). Untouched (still at
    their default) actions are never reverted or reassigned: defaults are
    pairwise collision-free within a context by construction, so claiming
    them first guarantees a revert never has to touch one of them.
    """
    defaults = DEFAULT_SHORTCUTS[context]
    overridden = [action for action, key in actions.items() if key != defaults[action]]
    claimed = [QKeySequence(key) for action, key in actions.items() if action not in overridden]
    for action in overridden:
        sequence = QKeySequence(actions[action])
        if any(sequence == other for other in claimed):
            actions[action] = defaults[action]
        claimed.append(QKeySequence(actions[action]))


def merge_with_defaults(configured: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Fills in any action missing from `configured` with its default.

    Keeps a `config.json` written by an older version usable after a new
    action is added, and tolerates a context/action typo in a hand-edited
    or imported file (falls back rather than crashing).
    """
    merged = default_shortcuts()
    for context, actions in configured.items():
        if context not in merged:
            continue
        for action, key_string in actions.items():
            if action in merged[context] and is_allowed_key(key_string, context=context):
                merged[context][action] = key_string
    for context, actions in merged.items():
        _drop_duplicate_overrides(context, actions)
    return merged
