"""File scanning and metadata extraction utilities for PDF/MD corpus linking."""
import mimetypes
import os
import re
from pathlib import Path
from typing import Optional

# Supported file extensions
SUPPORTED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".epub": "application/epub+zip",
    ".djvu": "image/vnd.djvu",
}


def get_content_type(file_path: str) -> str:
    """Determine MIME type from file extension."""
    ext = Path(file_path).suffix.lower()
    if ext in SUPPORTED_EXTENSIONS:
        return SUPPORTED_EXTENSIONS[ext]
    guess, _ = mimetypes.guess_type(file_path)
    return guess or "application/octet-stream"


def scan_directory(
    directory: str,
    extensions: Optional[list[str]] = None,
    recursive: bool = True,
    max_files: int = 5000,
) -> list[dict]:
    """Scan a directory for source files, returning metadata for each.

    Args:
        directory: Root directory to scan
        extensions: File extensions to include (default: all supported)
        recursive: Whether to scan subdirectories
        max_files: Maximum number of files to return

    Returns:
        List of dicts with keys: path, filename, extension, size_bytes, content_type
    """
    if extensions is None:
        extensions = list(SUPPORTED_EXTENSIONS.keys())
    else:
        extensions = [e if e.startswith(".") else f".{e}" for e in extensions]

    results = []
    root = Path(directory)

    if not root.exists():
        return []

    pattern_func = root.rglob if recursive else root.glob
    for path in pattern_func("*"):
        if len(results) >= max_files:
            break
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in extensions:
            continue

        results.append({
            "path": str(path),
            "filename": path.name,
            "stem": path.stem,
            "extension": ext,
            "size_bytes": path.stat().st_size,
            "content_type": get_content_type(str(path)),
        })

    return results


def extract_doi_from_pdf(file_path: str, max_bytes: int = 20000) -> Optional[str]:
    """Attempt to extract a DOI from the first pages of a PDF.

    Uses regex matching on raw bytes — fast but not 100% reliable.
    """
    doi_pattern = re.compile(
        rb'(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)',
        re.IGNORECASE,
    )
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(max_bytes)
        matches = doi_pattern.findall(chunk)
        if matches:
            # Take the first plausible DOI, decode and clean
            doi = matches[0].decode("ascii", errors="ignore").rstrip(".")
            # Basic validation
            if len(doi) > 10 and "/" in doi:
                return doi
    except Exception:
        pass
    return None


def extract_metadata_from_markdown(file_path: str) -> dict:
    """Extract metadata from markdown file frontmatter (YAML) or first heading."""
    meta = {"title": "", "authors": [], "date": "", "doi": ""}
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(5000)  # First 5KB
    except Exception:
        return meta

    # Try YAML frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        # Title
        t = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", fm, re.MULTILINE)
        if t:
            meta["title"] = t.group(1).strip()
        # Authors
        a = re.search(r"^authors?:\s*(.+)$", fm, re.MULTILINE)
        if a:
            raw = a.group(1).strip()
            if raw.startswith("["):
                meta["authors"] = [
                    x.strip().strip("\"'") for x in raw.strip("[]").split(",")
                ]
            else:
                meta["authors"] = [raw.strip("\"'")]
        # Date
        d = re.search(r"^date:\s*[\"']?(.+?)[\"']?\s*$", fm, re.MULTILINE)
        if d:
            meta["date"] = d.group(1).strip()
        # DOI
        doi = re.search(r"^doi:\s*[\"']?(.+?)[\"']?\s*$", fm, re.MULTILINE)
        if doi:
            meta["doi"] = doi.group(1).strip()

    # Fallback: first heading as title
    if not meta["title"]:
        h1 = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if h1:
            meta["title"] = h1.group(1).strip()

    # Fallback: DOI from content body
    if not meta["doi"]:
        doi_match = re.search(
            r'(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', content
        )
        if doi_match:
            meta["doi"] = doi_match.group(1).rstrip(".")

    return meta


def extract_metadata_from_filename(filename: str) -> dict:
    """Heuristic extraction of author/year/title from common filename patterns.

    Handles patterns like:
        - "Smith 2020 - Some Title.pdf"
        - "Smith_Jones_2019_Title_of_Paper.pdf"
        - "2021_Smith_Title.md"
        - "Smith et al. (2018) Title.pdf"
    """
    stem = Path(filename).stem
    meta = {"title": "", "authors": [], "date": ""}

    # Pattern: "Author (Year) Title" or "Author Year - Title"
    m = re.match(
        r'^(.+?)\s*[\(]?(\d{4})[\)]?\s*[-–—:.]?\s*(.*)$', stem
    )
    if m:
        author_part = m.group(1).strip().rstrip("-–—_.,")
        meta["date"] = m.group(2)
        title_part = m.group(3).strip().lstrip("-–—_.,").strip()

        # Parse author part
        author_part = author_part.replace("_", " ").replace("  ", " ")
        if "et al" in author_part.lower():
            base = re.sub(r'\s*et\s+al\.?\s*', '', author_part).strip()
            meta["authors"] = [base + " et al."]
        elif "&" in author_part or " and " in author_part.lower():
            meta["authors"] = re.split(r'\s*[&]\s*|\s+and\s+', author_part, flags=re.IGNORECASE)
        else:
            meta["authors"] = [author_part]

        if title_part:
            meta["title"] = title_part.replace("_", " ")
        return meta

    # Pattern: "Year_Author_Title"
    m = re.match(r'^(\d{4})\s*[-_.]?\s*(.+?)\s*[-_.]?\s*(.*)$', stem)
    if m:
        meta["date"] = m.group(1)
        rest = (m.group(2) + " " + m.group(3)).strip()
        rest = rest.replace("_", " ")
        meta["title"] = rest
        return meta

    # Fallback: just use cleaned stem as title
    meta["title"] = stem.replace("_", " ").replace("-", " ")
    return meta
