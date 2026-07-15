"""Full CSV contract."""

from __future__ import annotations

import csv
import io
import itertools
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from scanassistant.project.errors import InvalidCsvError
from scanassistant.utils.atomic import atomic_write_bytes, atomic_write_text

STATUS_COLUMN = "status"
SOURCE_FILE_COLUMN = "source_file"
NAME_ALIASES = ("filename", "name", "nom_fichier", "fichier", "nom")
MAX_NAME_LENGTH = 100  # default; operator-configurable (config.json:csv.max_name_length)
MAX_NAME_LENGTH_CEILING = 300  # hard ceiling: config.py validates against this
DELIMITER_CANDIDATES = (";", ",", "\t")

_INVALID_CHARS_PATTERN = re.compile(r"[^A-Za-z0-9._-]")
_TODO_STATUS_ALIASES = {"", "todo", "à faire", "a faire", "a_faire"}
_DONE_STATUS_ALIASES = {"done", "fait"}


@dataclass
class Inventory:
    """In-memory model of `inventory.csv`: columns, rows, cursor.

    `cursor` is the in-memory source of truth; its persisted copy is
    `state.json:csv_cursor` — kept in sync by the caller after any
    mutation (the link between the two is made by `core.session`).
    """

    fieldnames: list[str]
    rows: list[dict[str, str]]
    name_column: str
    delimiter: str
    cursor: int = 0

    # --- cursor --------------------------------------------------------

    def current_index(self) -> int | None:
        if 0 <= self.cursor < len(self.rows):
            return self.cursor
        return None

    def current_name(self) -> str | None:
        index = self.current_index()
        return self.rows[index][self.name_column] if index is not None else None

    def is_exhausted(self) -> bool:
        """CSV exhausted: no more `todo` row from the cursor onward."""
        return self.current_index() is None

    def advance_to_next_todo(self) -> None:
        """Advances the cursor to the next `todo` row."""
        for i in range(self.cursor + 1, len(self.rows)):
            if self.rows[i][STATUS_COLUMN] == "todo":
                self.cursor = i
                return
        self.cursor = len(self.rows)

    def go_to_previous_todo(self) -> bool:
        """Previous name (←). Returns `True` if the cursor moved."""
        for i in range(self.cursor - 1, -1, -1):
            if self.rows[i][STATUS_COLUMN] == "todo":
                self.cursor = i
                return True
        return False

    def go_to_next_todo(self) -> bool:
        """Next name (→). Returns `True` if the cursor moved."""
        for i in range(self.cursor + 1, len(self.rows)):
            if self.rows[i][STATUS_COLUMN] == "todo":
                self.cursor = i
                return True
        return False

    def go_to_name(self, name: str) -> None:
        """Go-to (G) / "Set cursor here": refuses a `done` row."""
        index = self._find_row_index(name)
        if index is None:
            raise ValueError(f"Unknown inventory name: {name!r}")
        if self.rows[index][STATUS_COLUMN] != "todo":
            raise ValueError(f"Cannot set cursor on a non-todo row: {name!r}")
        self.cursor = index

    # --- reads -----------------------------------------------------------

    def row(self, name: str) -> dict[str, str] | None:
        index = self._find_row_index(name)
        return self.rows[index] if index is not None else None

    def _find_row_index(self, name: str) -> int | None:
        for i, row in enumerate(self.rows):
            if row[self.name_column] == name:
                return i
        return None

    def _require_row_index(self, name: str) -> int:
        index = self._find_row_index(name)
        if index is None:
            raise ValueError(f"Unknown inventory name: {name!r}")
        return index

    # --- mutation ----------------------------------------------------------

    def set_source_file(self, name: str, source_file: str) -> None:
        """Ingestion: records the original camera file name, status unchanged."""
        self.rows[self._require_row_index(name)][SOURCE_FILE_COLUMN] = source_file

    def set_status(self, name: str, status: str) -> None:
        """Validation (`done`) or rejection (`todo`)."""
        if status not in ("todo", "done"):
            raise ValueError(f"Invalid status: {status!r}")
        self.rows[self._require_row_index(name)][STATUS_COLUMN] = status

    def add_free_name(self, name: str, source_file: str) -> None:
        """Free-form off-list name: new row, `done` right away.

        The cursor doesn't move: a free-form name never consumes an
        existing inventory row.
        """
        validate_name(name)
        if self._find_row_index(name) is not None:
            raise ValueError(f"Name already exists in the inventory: {name!r}")

        new_row = dict.fromkeys(self.fieldnames, "")
        new_row[self.name_column] = name
        new_row[STATUS_COLUMN] = "done"
        new_row[SOURCE_FILE_COLUMN] = source_file
        self.rows.append(new_row)

    # --- persistence -------------------------------------------------------

    def save(self, path: Path) -> None:
        """Rewrites `inventory.csv` in full, atomically, same dialect.

        `.bak` is only ever copied once, before the very first rewrite of
        an already-existing file (absent right after initial creation).
        """
        path = Path(path)
        if path.exists():
            bak_path = path.with_name(path.name + ".bak")
            if not bak_path.exists():
                atomic_write_bytes(bak_path, path.read_bytes())

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=self.fieldnames, delimiter=self.delimiter)
        writer.writeheader()
        writer.writerows(self.rows)
        atomic_write_text(path, buffer.getvalue())


@dataclass
class ImportedInventory:
    """Result of a CSV import."""

    inventory: Inventory
    warnings: list[str] = field(default_factory=list)
    rows_imported: int = 0
    rows_skipped_blank: int = 0
    character_fixes: list[tuple[str, str]] = field(default_factory=list)


def import_csv(
    source_path: Path,
    name_column: str = "filename",
    *,
    fix_invalid_characters: bool = True,
    has_header: bool = True,
    max_name_length: int = MAX_NAME_LENGTH,
) -> ImportedInventory:
    """Imports an external CSV (also used to reload `inventory.csv`).

    Raises `InvalidCsvError` (E-11) without writing anything to disk if the
    CSV is rejected: all validation happens in memory.

    `has_header`: whether the first physical line is a column-name row
    (default) or already real data. There is no content-based detection —
    a headerless, single-column CSV would otherwise silently lose its
    first name (swallowed as the "column name"), so the caller (CSV
    wizard step) must say which case this is.

    `max_name_length`: operator-configurable (config.json:csv.max_name_length,
    [10;MAX_NAME_LENGTH_CEILING]); `load_inventory()` below always passes the
    ceiling instead, so a reload never rejects a name that was valid at
    import time under a looser setting than whatever is configured now.
    """
    raw = Path(source_path).read_bytes()
    text, warnings = _decode_csv_bytes(raw)
    delimiter = _sniff_delimiter(text)

    # `csv.reader` (rather than `DictReader`): `DictReader` silently
    # swallows blank lines without exposing them, but they must be counted.
    raw_reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    first_row = next(raw_reader, [])
    if not first_row:
        message = "the CSV file has no header row" if has_header else "the CSV file is empty"
        raise InvalidCsvError([message])

    if has_header:
        original_fieldnames = first_row
        data_rows: Iterable[tuple[int, list[str]]] = enumerate(raw_reader, start=2)
    else:
        # No real header text to match aliases against: the first column is
        # named after the requested `name_column` directly, so it resolves
        # below without ambiguity; extra columns get positional names.
        original_fieldnames = [name_column] + [f"column_{i}" for i in range(2, len(first_row) + 1)]
        data_rows = itertools.chain([(1, first_row)], enumerate(raw_reader, start=2))

    resolved_name_column = _resolve_name_column(original_fieldnames, name_column)

    problems: list[str] = []
    character_fixes: list[tuple[str, str]] = []
    seen_names: dict[str, int] = {}
    blank_skipped = 0
    processed_rows: list[dict[str, str]] = []

    for line_number, raw_fields in data_rows:
        if not raw_fields:
            blank_skipped += 1
            continue
        raw_row = {
            field_name: (raw_fields[i] if i < len(raw_fields) else "")
            for i, field_name in enumerate(original_fieldnames)
        }
        if _is_blank_row(raw_row):
            blank_skipped += 1
            continue
        name = (raw_row.get(resolved_name_column) or "").strip()
        if not name:
            blank_skipped += 1
            continue
        if _has_extension(name):
            problems.append(f"line {line_number}: name must not include a file extension: {name!r}")
            continue
        if len(name) > max_name_length:
            problems.append(
                f"line {line_number}: name exceeds {max_name_length} characters: {name!r}"
            )
            continue
        if _INVALID_CHARS_PATTERN.search(name):
            fixed = _INVALID_CHARS_PATTERN.sub("_", name)
            if not fix_invalid_characters:
                problems.append(f"line {line_number}: invalid characters in name: {name!r}")
                continue
            character_fixes.append((name, fixed))
            name = fixed
        if name in seen_names:
            first_line = seen_names[name]
            problems.append(
                f"line {line_number}: duplicate name {name!r} (first seen at line {first_line})"
            )
            continue
        seen_names[name] = line_number

        raw_row[resolved_name_column] = name
        processed_rows.append(raw_row)

    if problems:
        raise InvalidCsvError(problems)

    fieldnames = list(original_fieldnames)
    if STATUS_COLUMN not in fieldnames:
        fieldnames.append(STATUS_COLUMN)
    if SOURCE_FILE_COLUMN not in fieldnames:
        fieldnames.append(SOURCE_FILE_COLUMN)

    final_rows: list[dict[str, str]] = []
    for row in processed_rows:
        normalized = {name: (row.get(name) or "") for name in fieldnames}
        status, status_unrecognized = _normalize_status(row.get(STATUS_COLUMN) or "")
        if status_unrecognized:
            warnings.append(
                f"unrecognized status {row.get(STATUS_COLUMN)!r} for "
                f"{row[resolved_name_column]!r}, defaulted to 'todo'"
            )
        normalized[STATUS_COLUMN] = status
        final_rows.append(normalized)

    cursor = next(
        (i for i, row in enumerate(final_rows) if row[STATUS_COLUMN] == "todo"), len(final_rows)
    )

    inventory = Inventory(
        fieldnames=fieldnames,
        rows=final_rows,
        name_column=resolved_name_column,
        delimiter=delimiter,
        cursor=cursor,
    )
    return ImportedInventory(
        inventory=inventory,
        warnings=warnings,
        rows_imported=len(final_rows),
        rows_skipped_blank=blank_skipped,
        character_fixes=character_fixes,
    )


def load_inventory(path: Path, name_column: str) -> Inventory:
    """Reloads `inventory.csv` for an already-created campaign (same rules as import).

    Always permissive on name length (`MAX_NAME_LENGTH_CEILING`, never the
    live `csv.max_name_length` setting): every row here was already valid
    when written, under whatever setting was active then.
    """
    return import_csv(path, name_column, max_name_length=MAX_NAME_LENGTH_CEILING).inventory


def export_inventory(inventory_path: Path, destination: Path) -> None:
    """Project ▸ CSV ▸ Export to…: copies the file as-is, no transformation."""
    shutil.copyfile(inventory_path, destination)


def has_been_modified_externally(path: Path, known_mtime: float) -> bool:
    """Detects a third-party modification of `inventory.csv`."""
    return Path(path).stat().st_mtime != known_mtime


# --- internal helpers -------------------------------------------------------


def validate_name(name: str, *, max_name_length: int = MAX_NAME_LENGTH) -> None:
    """Rules common to every image name: free-form name or conflict

    resolution (`<NAME>_BIS`/`<NAME>_OLD`). Raises `ValueError` on an
    invalid name; does not check uniqueness (depends on the calling
    context: internal CSV or on-disk file names).
    """
    if _has_extension(name):
        raise ValueError(f"Name must not include a file extension: {name!r}")
    if not name:
        raise ValueError("Name must not be empty")
    if len(name) > max_name_length:
        raise ValueError(f"Name exceeds {max_name_length} characters: {name!r}")
    if _INVALID_CHARS_PATTERN.search(name):
        raise ValueError(f"Invalid characters in name: {name!r}")


def _has_extension(name: str) -> bool:
    return bool(Path(name).suffix)


def _is_blank_row(row: dict[str, str]) -> bool:
    return all(value.strip() == "" for value in row.values())


def _normalize_status(value: str) -> tuple[str, bool]:
    normalized = value.strip().lower()
    if normalized in _TODO_STATUS_ALIASES:
        return "todo", False
    if normalized in _DONE_STATUS_ALIASES:
        return "done", False
    return "todo", True


def _resolve_name_column(fieldnames: list[str], requested: str) -> str:
    lower_map = {f.lower(): f for f in fieldnames}
    if requested.lower() in lower_map:
        return lower_map[requested.lower()]
    for alias in NAME_ALIASES:
        if alias in lower_map:
            return lower_map[alias]
    if len(fieldnames) == 1:
        return fieldnames[0]
    raise InvalidCsvError([f"no column matches {requested!r} or the known aliases {NAME_ALIASES}"])


def _sniff_delimiter(text: str) -> str:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="".join(DELIMITER_CANDIDATES))
        if dialect.delimiter in DELIMITER_CANDIDATES:
            return dialect.delimiter
    except csv.Error:
        pass
    return ";"


def _decode_csv_bytes(raw: bytes) -> tuple[str, list[str]]:
    warnings: list[str] = []
    try:
        return raw.decode("utf-8-sig"), warnings
    except UnicodeDecodeError:
        pass
    warnings.append("CSV is not valid UTF-8; decoded as cp1252 (fallback).")
    return raw.decode("cp1252", errors="replace"), warnings
