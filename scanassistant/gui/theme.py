"""Single dark theme."""

from __future__ import annotations

from typing import cast

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# Warm neutral base (not cool blue-grey): the app runs in a fully dark room
# with the screen as the only light source, where a warm base is easier on
# the eyes over a long session than a blue-tinted one.
BACKGROUND = "#211f1d"
SURFACE = "#2a2724"
BORDER = "#3a352e"  # decorative dividers only (panes, menu separators) — no
# contrast requirement, not the boundary of an interactive control.
BORDER_STRONG = "#8f8168"  # QLineEdit/QComboBox/QSpinBox/QPushButton borders:
# checked at 3.5:1+ against SURFACE, BORDER alone measures under 2:1 there.
TEXT_PRIMARY = "#ece7df"
TEXT_SECONDARY = "#998d7d"  # checked at 4.5:1+ against BACKGROUND and SURFACE
ACCENT = "#5f9bd6"  # focus/selection/primary actions — deliberately not a
# status color: ACCENT_OK must mean "reliable framing", nothing else.
ACCENT_OK = "#7cc47f"
ACCENT_WARNING = "#e0a458"
ACCENT_CRITICAL = "#e2685c"
PREVIEW_BACKGROUND = "#171310"
ACCENT_WARNING_BG = "#3a2f1f"  # dark amber banner
ACCENT_CRITICAL_BG = "#3a231f"  # dark red banner

_MIN_TARGET_PX = 32

# ui.brightness: dims text/surfaces uniformly — never the preview
# (PREVIEW_BACKGROUND is never passed to _scaled) nor the semantic accent
# colors (RELIABLE/WARNING/CRITICAL must stay recognizable at any setting).
_BRIGHTNESS_FACTORS = {"normal": 1.0, "dimmed": 0.75, "minimal": 0.5}


def _scaled(hex_color: str, brightness: str) -> str:
    factor = _BRIGHTNESS_FACTORS[brightness]
    if factor >= 1.0:
        return hex_color
    color = QColor(hex_color)
    hue, saturation, lightness, alpha = cast("tuple[float, float, float, float]", color.getHslF())
    color.setHslF(hue, saturation, lightness * factor, alpha)
    return color.name()


def apply_theme(app: QApplication, brightness: str = "normal") -> None:
    """Applies the standard dark palette to the whole application."""
    app.setStyle("Fusion")
    app.setPalette(_build_palette(brightness))
    app.setStyleSheet(_build_qss(brightness))


def _build_palette(brightness: str) -> QPalette:
    palette = QPalette()
    background = QColor(_scaled(BACKGROUND, brightness))
    surface = QColor(_scaled(SURFACE, brightness))
    text = QColor(_scaled(TEXT_PRIMARY, brightness))
    disabled_text = QColor(_scaled(TEXT_SECONDARY, brightness))

    palette.setColor(QPalette.ColorRole.Window, background)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, surface)
    palette.setColor(QPalette.ColorRole.AlternateBase, background)
    palette.setColor(QPalette.ColorRole.ToolTipBase, surface)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, surface)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor(ACCENT_CRITICAL))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, background)

    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)
    return palette


def _build_qss(brightness: str) -> str:
    background = _scaled(BACKGROUND, brightness)
    surface = _scaled(SURFACE, brightness)
    border = _scaled(BORDER, brightness)
    border_strong = _scaled(BORDER_STRONG, brightness)
    text_primary = _scaled(TEXT_PRIMARY, brightness)
    text_secondary = _scaled(TEXT_SECONDARY, brightness)
    warning_bg = _scaled(ACCENT_WARNING_BG, brightness)
    critical_bg = _scaled(ACCENT_CRITICAL_BG, brightness)
    return f"""
    QWidget {{
        background-color: {background};
        color: {text_primary};
        font-size: 12pt;
    }}
    QMainWindow, QDialog {{
        background-color: {background};
    }}
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background-color: {surface};
        border: 1px solid {border_strong};
        border-radius: 3px;
        padding: 4px;
        selection-background-color: {ACCENT};
        selection-color: {background};
    }}
    QListView, QTreeView, QTableView {{
        background-color: {surface};
        /* Softer than an editable field's border: these are read-only
        display panels (history, export queue, CSV/log viewers), not
        controls the operator types into — the strong tan boundary made
        them read as a big editable box and drew the eye more than the
        rows inside it. */
        border: 1px solid {border};
        border-radius: 3px;
        padding: 4px;
        gridline-color: {border};
        alternate-background-color: {background};
        selection-background-color: {ACCENT};
        selection-color: {background};
    }}
    QHeaderView::section {{
        background-color: {surface};
        color: {text_secondary};
        border: none;
        border-bottom: 1px solid {border};
        padding: 4px;
    }}
    QPushButton {{
        background-color: {surface};
        border: 1px solid {border_strong};
        border-radius: 3px;
        padding: 6px 14px;
        min-height: {_MIN_TARGET_PX}px;
    }}
    QPushButton:hover {{
        border-color: {ACCENT};
    }}
    QPushButton:pressed {{
        background-color: {border};
    }}
    QPushButton:default {{
        border-color: {ACCENT};
    }}
    QTabWidget::pane {{
        border: 1px solid {border};
    }}
    QTabBar::tab {{
        background-color: {background};
        color: {text_secondary};
        padding: 8px 16px;
        min-height: {_MIN_TARGET_PX}px;
    }}
    QTabBar::tab:selected {{
        background-color: {surface};
        color: {text_primary};
        border-bottom: 2px solid {ACCENT};
    }}
    QMenuBar {{
        background-color: {background};
        border-bottom: 1px solid {border};
    }}
    QMenuBar::item:selected {{
        background-color: {surface};
    }}
    QMenu {{
        background-color: {surface};
        border: 1px solid {border};
    }}
    QMenu::item:selected {{
        background-color: {border};
    }}
    QLabel[role="secondary"] {{
        color: {text_secondary};
    }}
    QLabel[role="warning"] {{
        color: {ACCENT_WARNING};
    }}
    QLabel[role="critical"] {{
        color: {ACCENT_CRITICAL};
    }}
    QLabel[role="ok"] {{
        color: {ACCENT_OK};
    }}
    QFrame#previewArea {{
        background-color: {PREVIEW_BACKGROUND};
    }}
    QWidget[role="stage-header"] {{
        background-color: {background};
        border-bottom: 1px solid {border};
    }}
    QWidget[role="console"] {{
        background-color: {background};
        border-top: 1px solid {border};
    }}
    QPushButton[role="warning-banner"] {{
        background-color: {warning_bg};
        color: {text_primary};
        border: none;
        border-radius: 0px;
        text-align: left;
        padding: 8px 12px;
    }}
    QPushButton[role="warning-banner"]:hover {{
        background-color: {warning_bg};
        border: none;
    }}
    QWidget[role="critical-banner"] {{
        background-color: {critical_bg};
    }}
    """
