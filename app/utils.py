"""Pure helper functions: filename sanitizing, AO3 tag encoding, atomic writes."""
import io
import os
import re
import urllib.parse
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# AO3 encodes these characters in tag URLs with its own substitutions,
# applied before ordinary percent-encoding.
_TAG_SUBSTITUTIONS = [
    ("/", "*s*"),
    ("&", "*a*"),
    (".", "*d*"),
    ("#", "*h*"),
    ("?", "*q*"),
]


def sanitize_filename(name: str, fallback: str = "untitled", max_length: int = 150) -> str:
    name = _UNSAFE_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(". ")
    if len(name) > max_length:
        name = name[:max_length].rstrip(". ")
    return name or fallback


def encode_tag(tag: str) -> str:
    for char, replacement in _TAG_SUBSTITUTIONS:
        tag = tag.replace(char, replacement)
    return urllib.parse.quote(tag, safe="*")


def safe_child(base: Path, *parts: str) -> Path | None:
    """Resolve base/parts... and return it only if it stays inside base.

    Starlette decodes %2F inside a path parameter into a literal "/", so each
    segment must be checked explicitly — resolve() alone would treat it as an
    extra directory level.
    """
    for part in parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part or "\x00" in part:
            return None
    target = base.joinpath(*parts).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a .part file + os.replace so a crash never leaves a truncated file."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def add_epub_subject(data: bytes, subject: str) -> bytes:
    """Append a <dc:subject> to the EPUB's OPF metadata.

    Calibre (with its default "read metadata from file contents" setting) turns
    dc:subject entries into tags on import — this is what actually tags the book,
    since calibre's filename regex cannot set tags.
    """
    src = zipfile.ZipFile(io.BytesIO(data))
    container = src.read("META-INF/container.xml").decode("utf-8")
    match = re.search(r'full-path="([^"]+)"', container)
    if not match:
        raise ValueError("No OPF path in META-INF/container.xml")
    opf_name = match.group(1)
    opf = src.read(opf_name).decode("utf-8")

    element = f"<dc:subject>{escape(subject)}</dc:subject>"
    if "xmlns:dc" not in opf or "</metadata>" not in opf:
        raise ValueError("OPF lacks a dc-namespaced metadata block")
    if element not in opf:
        opf = opf.replace("</metadata>", f"  {element}\n</metadata>", 1)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        # infolist preserves order, keeping the uncompressed mimetype entry first.
        for item in src.infolist():
            content = opf.encode("utf-8") if item.filename == opf_name else src.read(item.filename)
            dst.writestr(item, content)
    return out.getvalue()
