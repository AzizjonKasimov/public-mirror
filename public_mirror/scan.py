"""Look for things that must not be published.

Text files are searched for the repo's deny terms and for built-in detectors: secrets,
phone numbers, email addresses, and Korean registration numbers. Word, Excel, and PowerPoint
files are searched through their text, and PDFs too when `pypdf` is installed. Photos are
checked for a stored GPS location. Everything else cannot be read and is listed as unscanned.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

ERROR, WARNING = "error", "warning"

DETECTORS: dict[str, tuple[re.Pattern[str], str]] = {
    "private-key": (re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"), "a private key"),
    "token": (re.compile(
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"           # AWS access key ID
        r"|\bgh[pousr]_[A-Za-z0-9]{36,}"            # GitHub tokens
        r"|\bgithub_pat_[A-Za-z0-9_]{40,}"
        r"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"  # Anthropic and OpenAI keys
        r"|\bAIza[0-9A-Za-z_-]{35}"                 # Google API key
        r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"          # Slack token
        r"|\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b"        # Telegram bot token
    ), "an API key or access token"),
    "id-number": (re.compile(r"\b\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-[1-8]\d{6}\b"),
                  "a Korean resident or alien registration number"),
    "phone": (re.compile(
        r"(?<![\w+])(?:\+\d{1,3}[ .-]?(?:\(\d{1,4}\)|\d{1,4})(?:[ .-]?\d{2,4}){2,4}"  # +82 10 1234 5678
        r"|01[016789][ .-]?\d{3,4}[ .-]?\d{4})(?!\w)"                               # 010-1234-5678
    ), "a phone number"),
    "email": (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
              "an email address"),
}

# Matches that are never private: example and no-reply addresses, and git SSH remotes.
DEFAULT_ALLOW = [re.compile(p, re.IGNORECASE) for p in (r"@example\.(?:com|org|net)$", r"noreply", r"^git@")]

DOCUMENT_SUFFIXES = {".docx", ".xlsx", ".pptx"}
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}


@dataclass(frozen=True)
class Finding:
    level: str  # ERROR blocks publishing; WARNING does not
    path: str
    line: int | None
    rule: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}  [{self.rule}] {self.message}" if where else f"[{self.rule}] {self.message}"


def mask(value: str) -> str:
    """Enough of a value to recognize it, without printing a private value in full."""
    value = " ".join(value.split())
    return f'"{value[:2]}..." ({len(value)} characters)'


def term_regex(term: str) -> re.Pattern[str]:
    """Match a deny or replace term, ignoring case and treating any whitespace run as one space.

    A term that starts or ends with an ASCII letter or digit matches only at a word edge there,
    so "Storm" does not match "brainstorm". Other scripts match anywhere, because Korean, for one,
    attaches particles directly to words.
    """
    pattern = r"\s+".join(map(re.escape, term.split()))
    if term[:1].isspace():
        pattern = r"\s+" + pattern
    elif _word_char(term[:1]):
        pattern = r"(?<![A-Za-z0-9])" + pattern
    if term[-1:].isspace():
        pattern += r"\s+"
    elif _word_char(term[-1:]):
        pattern += r"(?![A-Za-z0-9])"
    return re.compile(pattern, re.IGNORECASE)


def _word_char(char: str) -> bool:
    return char.isascii() and char.isalnum()


def is_binary(path: str, data: bytes) -> bool:
    # Documents and photos go to their own readers even when they hold no NUL byte (git's own test).
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in DOCUMENT_SUFFIXES | PHOTO_SUFFIXES | {".pdf"} or b"\0" in data[:8000]


class Scanner:
    def __init__(self, deny: list[str], allow: list[re.Pattern[str]], skip: set[str]):
        self.deny = [(term, term_regex(term)) for term in deny]
        self.allow = allow + DEFAULT_ALLOW
        self.detectors = {name: value for name, value in DETECTORS.items() if name not in skip}

    def scan_text(self, path: str, text: str, line_map: list[int] | None = None,
                  numbered: bool = True) -> list[Finding]:
        """Findings for one text; `line_map` turns published line numbers into source line numbers."""
        hits: dict[tuple[str, str], list] = {}  # (rule, what) -> [first line, count]

        def add(rule: str, what: str, offset: int) -> None:
            line = None
            if numbered:
                line = text.count("\n", 0, offset) + 1
                if line_map:
                    line = line_map[min(line, len(line_map)) - 1]
            hit = hits.setdefault((rule, what), [line, 0])
            hit[1] += 1

        for index, (term, regex) in enumerate(self.deny, 1):
            for match in regex.finditer(text):
                add("deny", f"contains deny term #{index}, {mask(term)}", match.start())
        for rule, (regex, label) in self.detectors.items():
            for match in regex.finditer(text):
                value = match.group(0)
                if not any(allowed.search(value) for allowed in self.allow):
                    add(rule, f"looks like {label}: {mask(value)}", match.start())
        return [Finding(ERROR, path, line, rule, what + (f" ({count} times)" if count > 1 else ""))
                for (rule, what), (line, count) in hits.items()]

    def scan_binary(self, path: str, data: bytes) -> tuple[list[Finding], bool]:
        """Findings for a binary file, and whether its contents could be scanned at all."""
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in PHOTO_SUFFIXES:
            if has_gps(suffix, data):
                return [Finding(ERROR, path, None, "gps",
                                "the photo stores a GPS location; remove its location data first")], False
            return [], False
        text = document_text(suffix, data)
        if not text or not text.strip():
            return [], False
        return self.scan_text(path, text, numbered=False), True


def document_text(suffix: str, data: bytes) -> str | None:
    """The text of a Word, Excel, PowerPoint, or PDF file, or None when it cannot be read."""
    if suffix in DOCUMENT_SUFFIXES:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                parts = [archive.read(name).decode("utf-8", "replace") for name in archive.namelist()
                         if name.endswith((".xml", ".rels"))]
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return None
        xml = "\n".join(parts)
        # Word splits text into runs; join runs directly and end each paragraph, row, or cell with a newline.
        xml = re.sub(r"</w:p>|</a:p>|<w:br\s*/>|<w:tab\s*/>|</si>|</c>|</row>", "\n", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml))
    if suffix == ".pdf":
        try:
            import pypdf
        except ImportError:
            return None
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:  # pypdf raises many kinds of errors on unusual files
            return None
    return None


def has_gps(suffix: str, data: bytes) -> bool:
    if suffix == ".png":
        return any(_tiff_has_gps(chunk) for chunk in _png_chunks(data, b"eXIf"))
    return any(_tiff_has_gps(segment[6:]) for segment in _jpeg_exif_segments(data))


def _jpeg_exif_segments(data: bytes):
    if not data.startswith(b"\xff\xd8"):
        return
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:  # padding
            pos += 1
            continue
        if marker in (0xD9, 0xDA):  # end of image, or start of the image data: no metadata after this
            return
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        segment = data[pos + 4:pos + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\0\0"):
            yield segment
        pos += 2 + length


def _png_chunks(data: bytes, kind: bytes):
    pos = 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        if data[pos + 4:pos + 8] == kind:
            yield data[pos + 8:pos + 8 + length]
        pos += 12 + length


def _tiff_has_gps(tiff: bytes) -> bool:
    """True when the EXIF block has a GPS section with a latitude or longitude in it."""
    order = {b"II": "little", b"MM": "big"}.get(tiff[:2])
    if not order or len(tiff) < 8:
        return False

    def entries(offset: int):
        if offset + 2 > len(tiff):
            return
        for k in range(int.from_bytes(tiff[offset:offset + 2], order)):
            start = offset + 2 + 12 * k
            if start + 12 > len(tiff):
                return
            yield int.from_bytes(tiff[start:start + 2], order), int.from_bytes(tiff[start + 8:start + 12], order)

    for tag, value in entries(int.from_bytes(tiff[4:8], order)):
        if tag == 0x8825:  # pointer to the GPS section
            return any(gps_tag in (2, 4) for gps_tag, _ in entries(value))  # GPSLatitude, GPSLongitude
    return False
