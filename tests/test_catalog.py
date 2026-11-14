from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import TYPE_CHECKING, Any

import m3u8
import pytest

from isubrip.__main__ import parse_cli_args, read_batch_urls
from isubrip.catalog import (
    SubtitlesCatalog,
    audit_catalog,
    normalize_source_url,
    utc_now_iso,
)
from isubrip.commands.download import download
from isubrip.config import Config
from isubrip.data_structures import (
    CatalogSubtitlesRecord,
    Movie,
    ScrapedMediaResponse,
    SubtitleMediaGroup,
    SubtitlesData,
    SubtitlesFormatType,
)
from isubrip.scrapers.scraper import HLSScraper, ScraperError, ScraperFactory
from isubrip.subtitle_formats.webvtt import WebVTTSubtitles

if TYPE_CHECKING:
    from isubrip.data_structures import SubtitlesType

MOVIE_URL = "https://tv.apple.com/us/movie/test-movie/umc.cmc.abc123"
DSID = "9988776655"

CONTENT_EN = b"WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n"
CONTENT_FR = b"WEBVTT\n\n00:00.000 --> 00:01.000\nBonjour\n"
CONTENT_DE = b"WEBVTT\n\n00:00.000 --> 00:01.000\nHallo\n"
CONTENT_EN_FORCED = b"WEBVTT\n\n00:00.000 --> 00:01.000\n[forced]\n"


class CatalogPlaylistScraper(HLSScraper):
    id = "catalog-playlist-test"
    name = "Catalog Playlist Test"
    abbreviation = "CPT"
    url_regex = re.compile(".*")
    subtitles_class = WebVTTSubtitles

    async def get_data(self, url: str) -> ScrapedMediaResponse:
        raise NotImplementedError


def make_media(language: str, kind: str) -> m3u8.Media:
    return m3u8.Media(
        uri=f"https://example.com/{language}-{kind}.m3u8",
        type="SUBTITLES",
        group_id="subs",
        language=language,
        name=f"{language} subtitles",
        forced="YES" if kind == "forced" else None,
        characteristics="public.accessibility.transcribes-spoken-dialog" if kind == "cc" else None,
    )


def make_playlist_scraper(
    monkeypatch: pytest.MonkeyPatch,
    subtitles: dict[tuple[str, str], bytes],
    downloaded: list[tuple[str, str]],
) -> CatalogPlaylistScraper:
    scraper = object.__new__(CatalogPlaylistScraper)

    async def load_playlist(url: str, headers: dict | None = None) -> object:  # noqa: ARG001
        return object()

    def find_matching_subtitle_groups(
        main_playlist: object,  # noqa: ARG001
        language_filter: list[str] | None = None,
    ) -> list[SubtitleMediaGroup[m3u8.Media]]:
        groups = []

        for language, kind in subtitles:
            if language_filter and language.casefold() not in [item.casefold() for item in language_filter]:
                continue

            groups.append(SubtitleMediaGroup(candidates=(make_media(language=language, kind=kind),)))

        return groups

    async def download_subtitle_group(
        media_group: SubtitleMediaGroup[m3u8.Media],
        subrip_conversion: bool = False,  # noqa: ARG001
    ) -> SubtitlesData:
        media = media_group.primary
        special_type = CatalogPlaylistScraper.detect_subtitles_type(subtitles_media=media)
        kind = "forced" if special_type and special_type.value == "Forced" else (
            "cc" if special_type else "regular"
        )
        downloaded.append((media.language, kind))

        return SubtitlesData(
            language_code=media.language,
            language_name=media.name,
            subtitles_format=SubtitlesFormatType.WEBVTT,
            content=subtitles[(media.language, kind)],
            content_encoding="utf-8",
            special_type=special_type,
        )

    monkeypatch.setattr(scraper, "load_playlist", load_playlist)
    monkeypatch.setattr(scraper, "find_matching_subtitle_groups", find_matching_subtitle_groups)
    monkeypatch.setattr(scraper, "download_subtitle_group", download_subtitle_group)
    return scraper


def setup_download_fakes(
    monkeypatch: pytest.MonkeyPatch,
    subtitles: dict[tuple[str, str], bytes],
    downloaded: list[tuple[str, str]],
    failing_urls: tuple[str, ...] = (),
    movie: Movie | None = None,
) -> None:
    playlist_scraper = make_playlist_scraper(
        monkeypatch=monkeypatch,
        subtitles=subtitles,
        downloaded=downloaded,
    )
    movie = movie or Movie(
        id="movie-1",
        name="Test Movie",
        release_date=2020,
        playlist="https://example.com/master.m3u8",
    )

    class MetadataScraper:
        @staticmethod
        async def get_data(url: str) -> ScrapedMediaResponse[Movie]:
            if url in failing_urls:
                raise ScraperError("Scraping failed.")

            return ScrapedMediaResponse(
                media_data=[movie],
                metadata_scraper="metadata-test",
                playlist_scraper=CatalogPlaylistScraper.id,
                original_data={},
            )

    def fake_get_scraper_instance(
        scraper_id: str | None = None,  # noqa: ARG001
        url: str | None = None,
        **kwargs: Any,  # noqa: ARG001
    ) -> Any:
        return MetadataScraper() if url is not None else playlist_scraper

    monkeypatch.setattr(ScraperFactory, "get_scraper_instance", staticmethod(fake_get_scraper_instance))


def fetch_all(db_path: Path, table: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()

    finally:
        conn.close()


def make_catalog_record(
    source_url: str,
    language_code: str,
    subtitles_type: SubtitlesType | str,
    run_id: str = "test-run",
    itunes_id: str = "movie-1",
) -> CatalogSubtitlesRecord:
    return CatalogSubtitlesRecord(
        run_id=run_id,
        source_url=normalize_source_url(url=source_url),
        storefront="US",
        itunes_id=itunes_id,
        media_type="movie",
        title="Test Movie",
        release_year=2020,
        language_code=language_code,
        subtitles_type=subtitles_type,
        format="vtt",
        file_name=f"Test.Movie.2020.{language_code}.vtt",
        size_bytes=10,
        sha256="0" * 64,
        downloaded_at=utc_now_iso(),
    )


def test_first_run_creates_catalog_and_records_subtitles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"{MOVIE_URL}?dsid={DSID}"
    subtitles = {("en", "regular"): CONTENT_EN}
    setup_download_fakes(monkeypatch=monkeypatch, subtitles=subtitles, downloaded=[])
    db_path = tmp_path / "subtitles-catalog.db"

    assert asyncio.run(download(url, download_path=tmp_path, catalog_path=db_path)) is True
    assert db_path.is_file()

    rows = fetch_all(db_path=db_path, table="subtitles")
    assert len(rows) == 1
    row = rows[0]
    assert row["source_url"] == f"{MOVIE_URL}?dsid=REDACTED"
    assert DSID not in row["source_url"]
    assert row["storefront"] == "US"
    assert row["itunes_id"] == "movie-1"
    assert row["media_type"] == "movie"
    assert row["title"] == "Test Movie"
    assert row["release_year"] == 2020
    assert row["season_number"] is None
    assert row["episode_number"] is None
    assert row["language_code"] == "en"
    assert row["language_name"] == "en subtitles"
    assert row["subtitles_type"] == "regular"
    assert row["format"] == "vtt"
    assert row["size_bytes"] == len(CONTENT_EN)
    assert row["sha256"] == hashlib.sha256(CONTENT_EN).hexdigest()
    assert row["downloaded_at"]

    file_path = tmp_path / row["file_name"]
    assert not Path(row["file_name"]).is_absolute()
    assert file_path.is_file()
    assert file_path.read_bytes() == CONTENT_EN

    runs = fetch_all(db_path=db_path, table="runs")
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["added_count"] == 1
    assert runs[0]["updated_count"] == 0
    assert runs[0]["failed_count"] == 0
    assert runs[0]["run_id"] == row["run_id"]
    assert runs[0]["started_at"] and runs[0]["finished_at"]


def test_second_run_updates_record_instead_of_duplicating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtitles = {("en", "regular"): CONTENT_EN}
    setup_download_fakes(monkeypatch=monkeypatch, subtitles=subtitles, downloaded=[])
    db_path = tmp_path / "subtitles-catalog.db"

    assert asyncio.run(download(MOVIE_URL, download_path=tmp_path, catalog_path=db_path)) is True
    first_row = fetch_all(db_path=db_path, table="subtitles")[0]

    updated_content = b"WEBVTT\n\n00:00.000 --> 00:02.000\nHello again\n"
    subtitles[("en", "regular")] = updated_content

    assert asyncio.run(download(MOVIE_URL, download_path=tmp_path, catalog_path=db_path)) is True

    rows = fetch_all(db_path=db_path, table="subtitles")
    assert len(rows) == 1
    row = rows[0]
    assert row["sha256"] == hashlib.sha256(updated_content).hexdigest()
    assert row["size_bytes"] == len(updated_content)
    assert row["run_id"] != first_row["run_id"]
    assert row["downloaded_at"] >= first_row["downloaded_at"]

    runs = fetch_all(db_path=db_path, table="runs")
    assert len(runs) == 2
    second_run = next(run for run in runs if run["run_id"] == row["run_id"])
    assert second_run["status"] == "success"
    assert second_run["added_count"] == 0
    assert second_run["updated_count"] == 1

    file_path = tmp_path / row["file_name"]
    assert file_path.read_bytes() == updated_content


def test_regular_and_forced_subtitles_are_recorded_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtitles = {
        ("en", "regular"): CONTENT_EN,
        ("en", "forced"): CONTENT_EN_FORCED,
    }
    setup_download_fakes(monkeypatch=monkeypatch, subtitles=subtitles, downloaded=[])
    db_path = tmp_path / "subtitles-catalog.db"

    assert asyncio.run(download(MOVIE_URL, download_path=tmp_path, catalog_path=db_path)) is True

    rows = fetch_all(db_path=db_path, table="subtitles")
    assert len(rows) == 2
    kinds = {row["subtitles_type"] for row in rows}
    assert kinds == {"regular", "forced"}
    assert all(row["language_code"] == "en" for row in rows)

    file_names = {row["file_name"] for row in rows}
    assert len(file_names) == 2

    for row in rows:
        assert (tmp_path / row["file_name"]).is_file()


def test_only_missing_skips_cataloged_subtitles_and_fills_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtitles = {
        ("en", "regular"): CONTENT_EN,
        ("fr", "regular"): CONTENT_FR,
    }
    downloaded: list[tuple[str, str]] = []
    setup_download_fakes(monkeypatch=monkeypatch, subtitles=subtitles, downloaded=downloaded)
    db_path = tmp_path / "subtitles-catalog.db"

    assert asyncio.run(download(MOVIE_URL, download_path=tmp_path, catalog_path=db_path)) is True
    assert len(fetch_all(db_path=db_path, table="subtitles")) == 2

    subtitles[("de", "regular")] = CONTENT_DE
    downloaded.clear()

    assert asyncio.run(
        download(MOVIE_URL, download_path=tmp_path, catalog_path=db_path, only_missing=True),
    ) is True

    assert downloaded == [("de", "regular")]

    rows = fetch_all(db_path=db_path, table="subtitles")
    assert len(rows) == 3
    assert {row["language_code"] for row in rows} == {"en", "fr", "de"}

    de_row = next(row for row in rows if row["language_code"] == "de")
    assert (tmp_path / de_row["file_name"]).read_bytes() == CONTENT_DE

    runs = fetch_all(db_path=db_path, table="runs")
    assert len(runs) == 2
    second_run = next(run for run in runs if run["run_id"] == de_row["run_id"])
    assert second_run["status"] == "success"
    assert second_run["added_count"] == 1


def test_audit_returns_zero_when_fully_covered(tmp_path: Path) -> None:
    db_path = tmp_path / "subtitles-catalog.db"

    with SubtitlesCatalog(db_path=db_path) as catalog:
        catalog.upsert_subtitles(
            records=[
                make_catalog_record(source_url=MOVIE_URL, language_code="en", subtitles_type=kind)
                for kind in ("regular", "cc", "forced")
            ],
        )

    assert audit_catalog(catalog_path=db_path, urls=[MOVIE_URL], target_languages=["en"]) is True


def test_audit_returns_one_when_languages_are_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "subtitles-catalog.db"

    with SubtitlesCatalog(db_path=db_path) as catalog:
        catalog.upsert_subtitles(
            records=[
                make_catalog_record(source_url=MOVIE_URL, language_code="en", subtitles_type="regular"),
                make_catalog_record(source_url=MOVIE_URL, language_code="en", subtitles_type="cc"),
            ],
        )

    # 'forced' is missing for 'en', and 'fr' is missing entirely
    assert audit_catalog(catalog_path=db_path, urls=[MOVIE_URL], target_languages=["en", "fr"]) is False


def test_audit_treats_unknown_addresses_as_fully_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "subtitles-catalog.db"

    with SubtitlesCatalog(db_path=db_path) as catalog:
        catalog.upsert_subtitles(
            records=[
                make_catalog_record(source_url=MOVIE_URL, language_code="en", subtitles_type=kind)
                for kind in ("regular", "cc", "forced")
            ],
        )

    unknown_url = "https://tv.apple.com/us/movie/other-movie/umc.cmc.xyz789"
    assert audit_catalog(catalog_path=db_path, urls=[unknown_url], target_languages=["en"]) is False
    assert audit_catalog(catalog_path=db_path, urls=[MOVIE_URL], target_languages=["en"]) is True


def test_failed_address_does_not_block_others_and_run_is_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_url = "https://tv.apple.com/us/movie/broken/umc.cmc.bad000"
    subtitles = {("en", "regular"): CONTENT_EN}
    setup_download_fakes(
        monkeypatch=monkeypatch,
        subtitles=subtitles,
        downloaded=[],
        failing_urls=(failing_url,),
    )
    db_path = tmp_path / "subtitles-catalog.db"

    assert asyncio.run(
        download(MOVIE_URL, failing_url, download_path=tmp_path, catalog_path=db_path),
    ) is True

    rows = fetch_all(db_path=db_path, table="subtitles")
    assert len(rows) == 1
    assert rows[0]["source_url"] == MOVIE_URL

    runs = fetch_all(db_path=db_path, table="runs")
    assert len(runs) == 1
    assert runs[0]["status"] == "partial"
    assert runs[0]["added_count"] == 1
    assert runs[0]["failed_count"] == 1


def test_run_is_failed_when_nothing_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_url = "https://tv.apple.com/us/movie/broken/umc.cmc.bad000"
    setup_download_fakes(
        monkeypatch=monkeypatch,
        subtitles={},
        downloaded=[],
        failing_urls=(failing_url,),
    )
    db_path = tmp_path / "subtitles-catalog.db"

    assert asyncio.run(download(failing_url, download_path=tmp_path, catalog_path=db_path)) is False

    runs = fetch_all(db_path=db_path, table="runs")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["failed_count"] == 1
    assert fetch_all(db_path=db_path, table="subtitles") == []


def test_failed_batch_leaves_no_partial_records(tmp_path: Path) -> None:
    db_path = tmp_path / "subtitles-catalog.db"
    catalog = SubtitlesCatalog(db_path=db_path)
    record = make_catalog_record(source_url=MOVIE_URL, language_code="en", subtitles_type="regular")

    with pytest.raises(Exception):  # noqa: B017, PT011
        catalog.upsert_subtitles(records=[record, object()])  # type: ignore[list-item]

    # The entire batch was rolled back, and the database is still usable
    assert catalog.get_source_rows(source_url=record.source_url) == []
    assert catalog.upsert_subtitles(records=[record]) == (1, 0)
    assert len(catalog.get_source_rows(source_url=record.source_url)) == 1
    catalog.close()


def test_read_batch_urls_skips_blank_lines_comments_and_duplicates(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "\n"
        "# A comment\n"
        f"{MOVIE_URL}\n"
        "   \n"
        "https://tv.apple.com/us/movie/other/umc.cmc.xyz789\n"
        f"  {MOVIE_URL}  \n"
        "# Another comment\n",
        encoding="utf-8",
    )

    assert read_batch_urls(batch_file=batch_file) == [
        MOVIE_URL,
        "https://tv.apple.com/us/movie/other/umc.cmc.xyz789",
    ]


def test_parse_cli_args_batch_audit_and_languages() -> None:
    cli_args = parse_cli_args([
        "--batch", "urls.txt",
        "--audit",
        "--only-missing",
        "--catalog", "custom.db",
        "--languages", "en,fr", "de",
    ])

    assert cli_args.batch == Path("urls.txt")
    assert cli_args.audit is True
    assert cli_args.only_missing is True
    assert cli_args.catalog == Path("custom.db")
    assert cli_args.languages == ["en", "fr", "de"]
    assert cli_args.urls == []


def test_parse_cli_args_audit_requires_batch() -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_cli_args(["--audit", MOVIE_URL])

    assert exc_info.value.code == 2


def test_parse_cli_args_rejects_urls_combined_with_batch() -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_cli_args(["--batch", "urls.txt", MOVIE_URL])

    assert exc_info.value.code == 2


def test_config_downloads_category_accepts_catalog_path() -> None:
    assert Config.model_validate({}).downloads.catalog is None

    config = Config.model_validate({"downloads": {"catalog": "/tmp/custom-catalog.db"}})
    assert config.downloads.catalog == Path("/tmp/custom-catalog.db")
