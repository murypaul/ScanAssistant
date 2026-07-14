"""Catalog of user-visible strings.

No user-visible string should be hardcoded anywhere else in the
application: everything goes through `t(key, **kwargs)`. The business
journal is not concerned: it stores codes and raw values.
"""

from __future__ import annotations

from scanassistant.i18n import en as _en

_CATALOGS: dict[str, dict[str, str]] = {"en": _en.STRINGS}
_DEFAULT_LANGUAGE = "en"
_current_language = _DEFAULT_LANGUAGE


def set_language(lang: str) -> None:
    """Changes the active catalog language (only `en` exists for now)."""
    if lang not in _CATALOGS:
        raise ValueError(f"Unknown language catalog: {lang!r}")
    global _current_language
    _current_language = lang


def get_language() -> str:
    return _current_language


def t(key: str, **kwargs: object) -> str:
    """Resolves `key` in the active catalog (falls back to English)."""
    catalog = _CATALOGS[_current_language]
    template = catalog.get(key) or _CATALOGS[_DEFAULT_LANGUAGE][key]
    return template.format(**kwargs)
