from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from isubrip.data_structures import (
    CatalogRunRecord,
    CatalogSubtitlesKind,
    CatalogSubtitlesRecord,
    SubtitlesType,
)
from isubrip.logger import logger
from isubrip.utils import redact_url_query_param

if TYPE_CHECKING:
    from collections.abc import Iterable

DEFAULT_CATALOG_FILE_NAME = "subtitles-catalog.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
    added_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subtitles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    storefront TEXT,
    itunes_id TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL,
    title TEXT NOT NULL,
    release_year INTEGER,
    season_number INTEGER,
    episode_number INTEGER,
    language_code TEXT NOT NULL,
    language_name TEXT,
    subtitles_type TEXT NOT NULL CHECK (subtitles_type IN ('regular', 'cc', 'forced')),
    format TEXT NOT NULL,
    file_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    downloaded_at TEXT NOT NULL,
    UNIQUE (source_url, itunes_id, language_code, subtitles_type)
);
"""

_NATURAL_KEY_COLUMNS = ("source_url", "itunes_id", "language_code", "subtitles_type")

# Columns updated when an existing record (matching the natural key) is re-downloaded.
_UPDATE_COLUMNS = ("sha256", "size_bytes", "file_name", "downloaded_at", "run_id")

_INSERT_COLUMNS = (
    "run_id",
    "source_url",
    "storefront",
    "itunes_id",
    "media_type",
    "title",
    "release_year",
    "season_number",
    "episode_number",
    "language_code",
    "language_name",
    "subtitles_type",
    "format",
    "file_name",
    "size_bytes",
    "sha256",
    "downloaded_at",
)


def normalize_source_url(url: str) -> str:
    """
    Normalize a source URL for catalog storage, redacting sensitive query parameters.

    Args:
        url (str): Source URL to normalize.

    Returns:
        str: The normalized URL, with the DSID query parameter (if present) redacted.
    """
    return redact_url_query_param(url=url.strip(), key="dsid")


def extract_storefront(url: str) -> str | None:
    """
    Extract the storefront country code from an Apple TV / iTunes URL.

    Args:
        url (str): URL to extract the storefront from.

    Returns:
        str | None: The storefront country code (uppercase), or None if it couldn't be extracted.
    """
    match = re.search(r"https?://(?:tv|itunes)\.apple\.com/([a-zA-Z]{2})(?:/|$)", url)
    return match.group(1).upper() if match else None


def subtitles_kind(special_type: SubtitlesType | None) -> CatalogSubtitlesKind:
    """
    Convert a SubtitlesType to a catalog subtitles kind string.

    Args:
        special_type (SubtitlesType | None): The subtitles special type (None for regular subtitles).

    Returns:
        CatalogSubtitlesKind: "regular", "cc" or "forced".
    """
    if special_type is SubtitlesType.CC:
        return "cc"

    if special_type is SubtitlesType.FORCED:
        return "forced"

    return "regular"


def utc_now_iso() -> str:
    """
    Get the current UTC time as an ISO 8601 string.

    Returns:
        str: Current UTC time in ISO 8601 format.
    """
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_hexdigest(content: bytes) -> str:
    """
    Calculate the SHA-256 hex digest of the given content.

    Args:
        content (bytes): Content to hash.

    Returns:
        str: SHA-256 hex digest of the content.
    """
    return hashlib.sha256(content).hexdigest()


class SubtitlesCatalog:
    """
    A SQLite-based catalog for tracking downloaded subtitles.

    The catalog stores one row per downloaded subtitles file, keyed by the natural key
    (source URL, iTunes media ID, language code, subtitles type), and one row per download run.
    """
    def __init__(self, db_path: str | Path):
        """
        Initialize the catalog, creating the database file and tables if they don't exist.

        Args:
            db_path (str | Path): Path to the catalog's SQLite database file.
        """
        self.db_path = Path(db_path)

        if self.db_path.parent and not self.db_path.parent.is_dir():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        """Create the catalog tables if they don't already exist."""
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def __enter__(self) -> SubtitlesCatalog:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    @staticmethod
    def generate_run_id() -> str:
        """
        Generate a unique run ID.

        Returns:
            str: A unique run ID.
        """
        return uuid4().hex

    def finish_run(self, record: CatalogRunRecord) -> None:
        """
        Store a run record. Run records are only stored once the run has finished,
        with its final status, so a run is never left in an intermediate state.

        Args:
            record (CatalogRunRecord): The run record to store.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, started_at, finished_at, status, added_count, updated_count, failed_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.run_id,
                    record.started_at,
                    record.finished_at,
                    record.status,
                    record.added_count,
                    record.updated_count,
                    record.failed_count,
                ),
            )

    def upsert_subtitles(self, records: Iterable[CatalogSubtitlesRecord]) -> tuple[int, int]:
        """
        Insert or update subtitles records in a single transaction.
        Records matching an existing natural key (source URL, iTunes media ID, language code,
        subtitles type) are updated in place; new records are inserted.
        If any record fails, the entire batch is rolled back.

        Args:
            records (Iterable[CatalogSubtitlesRecord]): The records to insert or update.

        Returns:
            tuple[int, int]: A tuple of (added records count, updated records count).
        """
        added_count = 0
        updated_count = 0

        with self._conn:
            for record in records:
                existing_row = self._conn.execute(
                    f"SELECT id FROM subtitles WHERE {' AND '.join(f'{column} = ?' for column in _NATURAL_KEY_COLUMNS)}",
                    tuple(getattr(record, column) for column in _NATURAL_KEY_COLUMNS),
                ).fetchone()

                if existing_row is not None:
                    self._conn.execute(
                        f"UPDATE subtitles SET {', '.join(f'{column} = ?' for column in _UPDATE_COLUMNS)}"
                        " WHERE id = ?",
                        (*[getattr(record, column) for column in _UPDATE_COLUMNS], existing_row["id"]),
                    )
                    updated_count += 1

                else:
                    self._conn.execute(
                        f"INSERT INTO subtitles ({', '.join(_INSERT_COLUMNS)})"
                        f" VALUES ({', '.join('?' for _ in _INSERT_COLUMNS)})",
                        tuple(getattr(record, column) for column in _INSERT_COLUMNS),
                    )
                    added_count += 1

        return added_count, updated_count

    def get_subtitles_keys(self, source_url: str, itunes_id: str) -> set[tuple[str, str]]:
        """
        Get the set of (language code, subtitles type) pairs stored in the catalog
        for the given source URL and iTunes media ID.

        Args:
            source_url (str): Normalized source URL.
            itunes_id (str): iTunes media ID.

        Returns:
            set[tuple[str, str]]: A set of (casefolded language code, subtitles type) tuples.
        """
        rows = self._conn.execute(
            "SELECT language_code, subtitles_type FROM subtitles WHERE source_url = ? AND itunes_id = ?",
            (source_url, itunes_id),
        ).fetchall()

        return {(row["language_code"].casefold(), row["subtitles_type"]) for row in rows}

    def get_source_rows(self, source_url: str) -> list[sqlite3.Row]:
        """
        Get all subtitles records stored for the given source URL.

        Args:
            source_url (str): Normalized source URL.

        Returns:
            list[sqlite3.Row]: Matching subtitles records.
        """
        return self._conn.execute(
            "SELECT * FROM subtitles WHERE source_url = ? ORDER BY title, season_number, episode_number, "
            "language_code, subtitles_type",
            (source_url,),
        ).fetchall()

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        """
        Get a run record by its ID.

        Args:
            run_id (str): ID of the run.

        Returns:
            sqlite3.Row | None: The run record, or None if not found.
        """
        return self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
