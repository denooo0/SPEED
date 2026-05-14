"""Tests for the COTArchive multi-year history loader."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from typing import Dict

import pandas as pd
import pytest

from src.data_layer.historical.cot_archive import (
    COTArchive,
    parse_cot_archive_csv,
    parse_cot_archive_zip,
)


HEADER = (
    "Market_and_Exchange_Names,Report_Date_as_YYYY-MM-DD,"
    "Prod_Merc_Positions_Long_All,Prod_Merc_Positions_Short_All,"
    "Swap_Positions_Long_All,Swap__Positions_Short_All,"
    "M_Money_Positions_Long_All,M_Money_Positions_Short_All,"
    "NonRept_Positions_Long_All,NonRept_Positions_Short_All"
)


def _row(date: str, market: str, vals=(10, 20, 30, 40, 50, 60, 70, 80)) -> str:
    return f"{market},{date}," + ",".join(str(v) for v in vals)


def _build_csv_bytes(rows) -> bytes:
    body = "\n".join([HEADER, *rows]) + "\n"
    return body.encode("utf-8")


def _build_zip_bytes(rows, filename: str = "f_year.txt") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, _build_csv_bytes(rows))
    return buf.getvalue()


def test_parse_cot_archive_csv_extracts_gold_only():
    rows = [
        _row("2024-01-02", "SILVER - COMMODITY EXCHANGE INC.", (1, 1, 1, 1, 1, 1, 1, 1)),
        _row("2024-01-02", "GOLD - COMMODITY EXCHANGE INC.", (10, 20, 5, 8, 100, 50, 7, 3)),
        _row("2024-01-09", "GOLD - COMMODITY EXCHANGE INC.", (15, 25, 6, 9, 110, 60, 8, 4)),
    ]
    df = parse_cot_archive_csv(_build_csv_bytes(rows))
    assert len(df) == 2
    first = df.iloc[0]
    # commercial = PM + SD
    assert first["commercial_long"] == 15  # 10 + 5
    assert first["commercial_short"] == 28  # 20 + 8
    assert first["commercial_net"] == -13
    assert first["managed_money_long"] == 100
    assert first["managed_money_short"] == 50
    assert first["managed_money_net"] == 50
    assert first["small_spec_long"] == 7
    assert first["small_spec_short"] == 3
    assert first["small_spec_net"] == 4


def test_parse_cot_archive_zip_roundtrip():
    payload = _build_zip_bytes(
        [_row("2024-03-05", "GOLD - COMMODITY EXCHANGE INC.", (1, 2, 3, 4, 5, 6, 7, 8))]
    )
    df = parse_cot_archive_zip(payload)
    assert len(df) == 1
    assert df.iloc[0]["commercial_long"] == 4  # 1 + 3


def test_parse_handles_thousands_commas_in_quoted_field():
    # CFTC ships quoted fields with comma thousands separators.
    header_quoted = (
        '"Market_and_Exchange_Names","Report_Date_as_YYYY-MM-DD",'
        '"Prod_Merc_Positions_Long_All","Prod_Merc_Positions_Short_All",'
        '"Swap_Positions_Long_All","Swap__Positions_Short_All",'
        '"M_Money_Positions_Long_All","M_Money_Positions_Short_All",'
        '"NonRept_Positions_Long_All","NonRept_Positions_Short_All"'
    )
    row = (
        '"GOLD - COMMODITY EXCHANGE INC.","2024-04-02",'
        '"1000","2000","3000","4000","5000","6000","7000","8000"'
    )
    raw = (header_quoted + "\n" + row + "\n").encode("utf-8")
    df = parse_cot_archive_csv(raw)
    assert len(df) == 1
    assert df.iloc[0]["commercial_long"] == 4000  # 1000 + 3000


def test_archive_fetcher_concatenates_years_and_filters_window():
    payloads: Dict[int, bytes] = {
        2022: _build_zip_bytes(
            [
                _row("2022-12-27", "GOLD - COMMODITY EXCHANGE INC.", (1, 2, 3, 4, 5, 6, 7, 8)),
                _row("2022-06-07", "GOLD - COMMODITY EXCHANGE INC.", (2, 3, 4, 5, 6, 7, 8, 9)),
            ]
        ),
        2023: _build_zip_bytes(
            [
                _row("2023-01-03", "GOLD - COMMODITY EXCHANGE INC.", (3, 4, 5, 6, 7, 8, 9, 10)),
                _row("2023-12-26", "GOLD - COMMODITY EXCHANGE INC.", (4, 5, 6, 7, 8, 9, 10, 11)),
            ]
        ),
    }

    def fake_fetch(year: int) -> bytes:
        return payloads[year]

    archive = COTArchive(fetcher=fake_fetch)
    df = archive.fetch_history(
        datetime(2022, 12, 1, tzinfo=timezone.utc),
        datetime(2023, 6, 1, tzinfo=timezone.utc),
    )
    # Expect 2 rows in window: 2022-12-27 and 2023-01-03.
    assert len(df) == 2
    assert df["date"].tolist() == [
        pd.Timestamp("2022-12-27", tz="UTC"),
        pd.Timestamp("2023-01-03", tz="UTC"),
    ]
    assert df["date"].is_monotonic_increasing


def test_archive_skips_failing_years():
    def fake_fetch(year: int) -> bytes:
        if year == 2021:
            raise RuntimeError("404 archive missing")
        return _build_zip_bytes(
            [_row(f"{year}-06-07", "GOLD - COMMODITY EXCHANGE INC.", (1, 1, 1, 1, 1, 1, 1, 1))]
        )

    archive = COTArchive(fetcher=fake_fetch)
    df = archive.fetch_history(
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        datetime(2022, 12, 31, tzinfo=timezone.utc),
    )
    # 2020 + 2022 should survive; 2021 dropped.
    assert len(df) == 2
    years = sorted({d.year for d in df["date"]})
    assert years == [2020, 2022]


def test_inverted_window_raises():
    archive = COTArchive(fetcher=lambda _y: b"")
    with pytest.raises(ValueError):
        archive.fetch_history(
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2022, 1, 1, tzinfo=timezone.utc),
        )


def test_parse_no_gold_returns_empty():
    rows = [_row("2024-01-02", "SILVER - COMMODITY EXCHANGE INC.")]
    df = parse_cot_archive_csv(_build_csv_bytes(rows))
    assert df.empty


def test_parse_missing_columns_raises():
    raw = b"Foo,Bar\n1,2\n"
    with pytest.raises(ValueError):
        parse_cot_archive_csv(raw)
