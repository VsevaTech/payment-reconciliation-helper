"""CSV ingestion: encoding detection, delimiter sniffing, string-typed DataFrame.

Everything is loaded as ``str`` on purpose: amounts must never pass through
float. Parsing to Decimal happens later, in :mod:`app.normalize`.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

import chardet
import pandas as pd

CANDIDATE_DELIMITERS = [",", ";", "\t", "|"]
MAX_SNIFF_BYTES = 64 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024


class CsvLoadError(ValueError):
    """Raised when a file cannot be interpreted as a tabular CSV."""


@dataclass
class LoadedCsv:
    df: pd.DataFrame
    encoding: str
    delimiter: str
    filename: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def columns(self) -> list[str]:
        return [str(c) for c in self.df.columns]

    @property
    def row_count(self) -> int:
        return int(len(self.df))

    def preview(self, n: int = 10) -> list[dict[str, str]]:
        return self.df.head(n).to_dict(orient="records")


def detect_encoding(raw: bytes) -> str:
    """Return a codec name that decodes ``raw``.

    Strategy (deterministic): BOM → utf-8-sig; strict utf-8; chardet guess
    (only if confident); cp1252 as the last resort (it decodes any byte).
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    guess = chardet.detect(raw[:MAX_SNIFF_BYTES])
    enc = (guess.get("encoding") or "").lower()
    conf = guess.get("confidence") or 0.0
    if enc and conf >= 0.7 and enc not in {"ascii"}:
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            pass
    return "cp1252"


def detect_delimiter(text: str) -> str:
    """Pick the delimiter by consistency of column counts over the first lines.

    ``csv.Sniffer`` is used first; if it fails or picks a character that does
    not produce a stable column count, the candidate with the most consistent
    non-trivial split wins.
    """
    sample = text[:MAX_SNIFF_BYTES]
    lines = [ln for ln in sample.splitlines() if ln.strip()][:50]
    if not lines:
        raise CsvLoadError("File is empty")

    def consistency(delim: str) -> tuple[int, int]:
        counts = []
        for row in csv.reader(lines, delimiter=delim):
            counts.append(len(row))
        if not counts:
            return (0, 0)
        first = counts[0]
        if first < 2:
            return (0, 0)
        stable = sum(1 for c in counts if c == first)
        return (stable, first)

    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters="".join(CANDIDATE_DELIMITERS))
        if sniffed.delimiter in CANDIDATE_DELIMITERS:
            stable, width = consistency(sniffed.delimiter)
            if width >= 2 and stable == len(lines):
                return sniffed.delimiter
    except csv.Error:
        pass

    best = max(CANDIDATE_DELIMITERS, key=lambda d: consistency(d))
    stable, width = consistency(best)
    if width < 2:
        raise CsvLoadError(
            "Could not detect a delimiter: every candidate yields a single column. "
            "Is this really a CSV file?"
        )
    return best


def _validate_structure(text: str, delimiter: str, filename: str) -> None:
    """Fail loudly on what pandas would silently 'repair'.

    pandas treats a row with *more* fields than the header as having an
    implicit index column, and renames duplicate headers to ``a.1``. Both hide
    real export problems, so they are rejected here with a row number.
    """
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None)
    if header is None:
        raise CsvLoadError(f"{filename or 'File'} is empty")
    names = [h.strip() for h in header]
    seen: set[str] = set()
    dupes = sorted({n for n in names if n and (n in seen or seen.add(n))})
    if dupes:
        raise CsvLoadError(f"{filename or 'File'} has duplicate header names: {dupes}")
    width = len(names)
    for line_no, row in enumerate(reader, start=2):
        if not row or (len(row) == 1 and row[0].strip() == ""):
            continue
        if len(row) != width:
            raise CsvLoadError(
                f"Malformed CSV ({filename or 'file'}): line {line_no} has {len(row)} fields, "
                f"header has {width}"
            )


def load_csv(raw: bytes, filename: str = "") -> LoadedCsv:
    """Parse ``raw`` bytes into a string-typed DataFrame.

    Raises :class:`CsvLoadError` with a human-readable message on failure.
    """
    if not raw or not raw.strip():
        raise CsvLoadError(f"{filename or 'File'} is empty")
    if len(raw) > MAX_FILE_BYTES:
        raise CsvLoadError(f"{filename or 'File'} exceeds {MAX_FILE_BYTES // 1024 // 1024} MB")
    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise CsvLoadError(f"{filename or 'File'} looks binary, not CSV")

    encoding = detect_encoding(raw)
    text = raw.decode(encoding, errors="strict")
    delimiter = detect_delimiter(text)
    _validate_structure(text, delimiter, filename)

    warnings: list[str] = []
    try:
        df = pd.read_csv(
            io.StringIO(text),
            sep=delimiter,
            dtype=str,
            keep_default_na=False,
            na_filter=False,
            engine="python",
            skip_blank_lines=True,
            on_bad_lines="error",
        )
    except pd.errors.ParserError as exc:
        raise CsvLoadError(f"Malformed CSV ({filename or 'file'}): {exc}") from exc
    except pd.errors.EmptyDataError as exc:
        raise CsvLoadError(f"{filename or 'File'} has no data rows") from exc

    if df.shape[1] < 2:
        raise CsvLoadError(
            f"{filename or 'File'} has a single column after parsing with '{delimiter}'. "
            "Expected at least reference and amount columns."
        )
    if len(df) == 0:
        raise CsvLoadError(f"{filename or 'File'} contains a header but no rows")

    df.columns = [str(c).strip() for c in df.columns]
    if any(c == "" or c.startswith("Unnamed:") for c in df.columns):
        warnings.append("Some header cells are empty; they were kept as positional names.")
    df.columns = [
        c if c and not c.startswith("Unnamed:") else f"column_{i + 1}"
        for i, c in enumerate(df.columns)
    ]
    return LoadedCsv(
        df=df, encoding=encoding, delimiter=delimiter, filename=filename, warnings=warnings
    )
