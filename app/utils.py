"""Pure helper functions: filename sanitizing, AO3 tag encoding, atomic writes."""
import io
import os
import posixpath
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


_WORK_REF = re.compile(r"(?:archiveofourown\.org|ao3\.org)?/?works/(\d+)", re.IGNORECASE)


def parse_work_ref(query: str) -> str | None:
    """Extract an AO3 work ID from a pasted work/chapter URL, or a bare ID.

    Accepts anything containing `/works/<id>` — including chapter URLs like
    /works/123/chapters/456 — plus a bare numeric ID.
    """
    q = query.strip()
    match = _WORK_REF.search(q)
    if match:
        return match.group(1)
    if q.isdigit() and len(q) >= 5:
        return q
    return None


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


def _read_opf(data: bytes) -> tuple[str, str]:
    """Return (opf_path_in_zip, opf_text). Raises if the archive isn't a sane EPUB."""
    src = zipfile.ZipFile(io.BytesIO(data))
    container = src.read("META-INF/container.xml").decode("utf-8")
    match = re.search(r'full-path="([^"]+)"', container)
    if not match:
        raise ValueError("No OPF path in META-INF/container.xml")
    opf_name = match.group(1)
    return opf_name, src.read(opf_name).decode("utf-8")


def epub_has_cover(data: bytes) -> bool:
    """True if the EPUB already declares a cover image, so we never overwrite one.

    Covers can be declared three ways in the wild; any one counts:
    an EPUB2 `<meta name="cover">`, an EPUB3 manifest item with
    `properties="cover-image"`, or a manifest image item whose id/href says "cover".
    """
    try:
        _, opf = _read_opf(data)
    except (KeyError, ValueError, zipfile.BadZipFile, UnicodeDecodeError):
        return False
    if re.search(r'<meta\b[^>]*\bname=["\']cover["\']', opf, re.I):
        return True
    if re.search(r'properties=["\'][^"\']*cover-image', opf, re.I):
        return True
    for tag in re.findall(r"<item\b[^>]*>", opf):
        if "image/" in tag and re.search(r'(?:id|href)=["\'][^"\']*cover', tag, re.I):
            return True
    return False


_COVER_XHTML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<!DOCTYPE html>\n"
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title>\n'
    "<style>html,body{margin:0;padding:0;height:100%}"
    "img{display:block;height:100vh;max-width:100%;margin:0 auto}</style></head>\n"
    '<body><img src="ficfetch-cover.jpg" alt="Cover"/></body></html>\n'
)


def add_epub_cover(data: bytes, jpeg: bytes) -> bytes:
    """Embed `jpeg` as the EPUB's cover (image + EPUB2/3 pointers + a cover page).

    Mirrors add_epub_subject's raw-string OPF edit + full-archive rewrite. The
    cover files are placed in the OPF's own directory and referenced relatively,
    so it works whether the OPF sits at the root (AO3) or in a subfolder.
    """
    opf_name, opf = _read_opf(data)
    if "</manifest>" not in opf or "</metadata>" not in opf:
        raise ValueError("OPF lacks a manifest/metadata block")

    opf_dir = posixpath.dirname(opf_name)
    img_name = posixpath.join(opf_dir, "ficfetch-cover.jpg") if opf_dir else "ficfetch-cover.jpg"
    page_name = posixpath.join(opf_dir, "ficfetch-cover.xhtml") if opf_dir else "ficfetch-cover.xhtml"

    # A cover page in the spine makes the image the first page too; only add it
    # when there's a spine to insert into — the meta pointer alone already drives
    # the library thumbnail.
    add_page = re.search(r"<spine\b[^>]*>", opf) is not None

    manifest_add = (
        '<item id="ficfetch-cover-image" href="ficfetch-cover.jpg"'
        ' media-type="image/jpeg" properties="cover-image"/>'
    )
    if add_page:
        manifest_add += (
            '\n    <item id="ficfetch-cover-page" href="ficfetch-cover.xhtml"'
            ' media-type="application/xhtml+xml"/>'
        )
    opf = opf.replace("</manifest>", f"    {manifest_add}\n</manifest>", 1)
    opf = opf.replace(
        "</metadata>", '  <meta name="cover" content="ficfetch-cover-image"/>\n</metadata>', 1
    )
    if add_page:
        opf = re.sub(
            r"(<spine\b[^>]*>)", r'\1\n    <itemref idref="ficfetch-cover-page"/>', opf, count=1
        )

    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            content = opf.encode("utf-8") if item.filename == opf_name else src.read(item.filename)
            dst.writestr(item, content)
        img_info = zipfile.ZipInfo(img_name)
        img_info.compress_type = zipfile.ZIP_STORED  # JPEG is already compressed
        dst.writestr(img_info, jpeg)
        if add_page:
            page_info = zipfile.ZipInfo(page_name)
            page_info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(page_info, _COVER_XHTML.encode("utf-8"))
    return out.getvalue()
