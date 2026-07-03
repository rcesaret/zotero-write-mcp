"""Utilities for DOI resolution, BibTeX parsing, and fuzzy matching."""
import os
import re
from difflib import SequenceMatcher
from typing import Any, Optional

import httpx
import bibtexparser

from zotero_write_mcp import __version__


# ── DOI Resolution via Crossref ──────────────────────────────────

CROSSREF_URL = "https://api.crossref.org/works/{doi}"

# Crossref's polite pool keys on a REAL mailto (the old placeholder got rate-limited). Read the contact
# from the CROSSREF_MAILTO env var (default the owner's) and interpolate the real package __version__.
CROSSREF_MAILTO_DEFAULT = "rcesaret@asu.edu"


def _crossref_user_agent() -> str:
    """The Crossref polite-pool User-Agent: real package version + a real mailto (CROSSREF_MAILTO env var,
    default the owner's). Built per call so the env var is honored at runtime."""
    mailto = os.environ.get("CROSSREF_MAILTO", CROSSREF_MAILTO_DEFAULT)
    return f"ZoteroWriteMCP/{__version__} (mailto:{mailto})"


def resolve_doi(doi: str) -> Optional[dict]:
    """Fetch metadata from Crossref for a given DOI. Returns Zotero-style fields."""
    doi = doi.strip().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    try:
        resp = httpx.get(
            CROSSREF_URL.format(doi=doi),
            headers={"User-Agent": _crossref_user_agent()},
            timeout=15.0,
        )
        resp.raise_for_status()
        work = resp.json()["message"]
    except Exception as e:
        return None

    # Map Crossref to Zotero fields
    item: dict[str, Any] = {"itemType": "journalArticle"}

    item["title"] = " ".join(work.get("title", [""]))
    item["DOI"] = work.get("DOI", doi)
    item["url"] = work.get("URL", "")

    # Authors
    creators = []
    for author in work.get("author", []):
        creators.append({
            "creatorType": "author",
            "firstName": author.get("given", ""),
            "lastName": author.get("family", ""),
        })
    item["creators"] = creators

    # Date
    date_parts = work.get("published", work.get("issued", {})).get("date-parts", [[]])
    if date_parts and date_parts[0]:
        parts = date_parts[0]
        item["date"] = "-".join(str(p) for p in parts)

    # Journal info
    container = work.get("container-title", [])
    item["publicationTitle"] = container[0] if container else ""
    item["volume"] = work.get("volume", "")
    item["issue"] = work.get("issue", "")
    item["pages"] = work.get("page", "")
    item["ISSN"] = (work.get("ISSN") or [""])[0]
    item["publisher"] = work.get("publisher", "")
    item["abstractNote"] = work.get("abstract", "")

    # Clean HTML from abstract
    if item["abstractNote"]:
        item["abstractNote"] = re.sub(r"<[^>]+>", "", item["abstractNote"])

    # Determine item type
    ctype = work.get("type", "")
    type_map = {
        "journal-article": "journalArticle",
        "book": "book",
        "book-chapter": "bookSection",
        "proceedings-article": "conferencePaper",
        "report": "report",
        "thesis": "thesis",
    }
    item["itemType"] = type_map.get(ctype, "journalArticle")

    if item["itemType"] == "bookSection":
        book_title = work.get("container-title", [""])
        item["bookTitle"] = book_title[0] if book_title else ""

    return item


# ── BibTeX Parsing ───────────────────────────────────────────────

BIBTEX_TYPE_MAP = {
    "article": "journalArticle",
    "book": "book",
    "inbook": "bookSection",
    "incollection": "bookSection",
    "inproceedings": "conferencePaper",
    "conference": "conferencePaper",
    "mastersthesis": "thesis",
    "phdthesis": "thesis",
    "techreport": "report",
    "misc": "document",
    "unpublished": "manuscript",
}


def parse_bibtex(bibtex_str: str) -> Optional[dict]:
    """Parse a BibTeX string into Zotero-style item fields."""
    try:
        parser = bibtexparser.bparser.BibTexParser(common_strings=True)
        bib_db = bibtexparser.loads(bibtex_str, parser=parser)
    except Exception:
        return None

    if not bib_db.entries:
        return None

    entry = bib_db.entries[0]
    etype = entry.get("ENTRYTYPE", "article").lower()
    item: dict[str, Any] = {
        "itemType": BIBTEX_TYPE_MAP.get(etype, "journalArticle")
    }

    # Title
    item["title"] = _clean_latex(entry.get("title", ""))

    # Authors
    creators = []
    for field, ctype in [("author", "author"), ("editor", "editor")]:
        raw = entry.get(field, "")
        if raw:
            for name in re.split(r"\s+and\s+", raw):
                name = _clean_latex(name.strip())
                if "," in name:
                    parts = name.split(",", 1)
                    creators.append({
                        "creatorType": ctype,
                        "lastName": parts[0].strip(),
                        "firstName": parts[1].strip(),
                    })
                else:
                    parts = name.rsplit(" ", 1)
                    if len(parts) == 2:
                        creators.append({
                            "creatorType": ctype,
                            "firstName": parts[0].strip(),
                            "lastName": parts[1].strip(),
                        })
                    else:
                        creators.append({
                            "creatorType": ctype,
                            "lastName": name,
                            "firstName": "",
                        })
    item["creators"] = creators

    # Direct field mappings
    field_map = {
        "journal": "publicationTitle",
        "booktitle": "bookTitle",
        "volume": "volume",
        "number": "issue",
        "pages": "pages",
        "year": "date",
        "doi": "DOI",
        "url": "url",
        "abstract": "abstractNote",
        "publisher": "publisher",
        "isbn": "ISBN",
        "issn": "ISSN",
        "edition": "edition",
        "series": "series",
        "address": "place",
    }
    for bib_key, zot_key in field_map.items():
        val = entry.get(bib_key, "")
        if val:
            item[zot_key] = _clean_latex(val)

    # Pages: normalize to Zotero format
    if "pages" in item:
        item["pages"] = item["pages"].replace("--", "-")

    return item


def _clean_latex(s: str) -> str:
    """Remove common LaTeX markup."""
    s = re.sub(r"[{}]", "", s)
    s = re.sub(r"\\textit\s*", "", s)
    s = re.sub(r"\\textbf\s*", "", s)
    s = re.sub(r"\\emph\s*", "", s)
    s = re.sub(r"~", " ", s)
    return s.strip()


# ── Fuzzy Matching / Duplicate Detection ─────────────────────────

def normalize_for_comparison(s: str) -> str:
    """Normalize a string for fuzzy comparison."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def title_similarity(a: str, b: str) -> float:
    """Compute similarity ratio between two titles."""
    a_norm = normalize_for_comparison(a)
    b_norm = normalize_for_comparison(b)
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def authors_match(creators_a: list[dict], creators_b: list[dict]) -> float:
    """Compare two creator lists, return similarity score 0-1."""
    if not creators_a or not creators_b:
        return 0.0

    def last_names(creators):
        return sorted(
            normalize_for_comparison(c.get("lastName", ""))
            for c in creators
            if c.get("creatorType") == "author"
        )

    names_a = last_names(creators_a)
    names_b = last_names(creators_b)

    if not names_a or not names_b:
        return 0.0

    # Check overlap
    matches = 0
    for na in names_a:
        for nb in names_b:
            if SequenceMatcher(None, na, nb).ratio() > 0.85:
                matches += 1
                break

    return matches / max(len(names_a), len(names_b))


def compute_duplicate_score(item_a: dict, item_b: dict) -> float:
    """Compute overall duplicate likelihood score between two items (0-1)."""
    data_a = item_a.get("data", item_a)
    data_b = item_b.get("data", item_b)

    title_a = data_a.get("title", "")
    title_b = data_b.get("title", "")

    if not title_a or not title_b:
        return 0.0

    t_sim = title_similarity(title_a, title_b)
    a_sim = authors_match(
        data_a.get("creators", []), data_b.get("creators", [])
    )

    # Date proximity
    date_a = data_a.get("date", "")
    date_b = data_b.get("date", "")
    date_match = 0.0
    if date_a and date_b:
        year_a = re.search(r"\d{4}", date_a)
        year_b = re.search(r"\d{4}", date_b)
        if year_a and year_b:
            diff = abs(int(year_a.group()) - int(year_b.group()))
            date_match = 1.0 if diff == 0 else (0.5 if diff <= 1 else 0.0)

    # DOI exact match is definitive
    doi_a = data_a.get("DOI", "").strip().lower()
    doi_b = data_b.get("DOI", "").strip().lower()
    if doi_a and doi_b and doi_a == doi_b:
        return 1.0

    # Weighted composite
    score = (t_sim * 0.55) + (a_sim * 0.30) + (date_match * 0.15)
    return round(score, 3)


def format_item_summary(item: dict) -> str:
    """Format an item as a concise one-line summary."""
    data = item.get("data", item)
    key = item.get("key", data.get("key", "???"))
    title = data.get("title", "Untitled")
    creators = data.get("creators", [])
    authors = ", ".join(c.get("lastName", "") for c in creators[:3] if c.get("creatorType") == "author")
    if len(creators) > 3:
        authors += " et al."
    date = data.get("date", "n.d.")
    year = ""
    yr_match = re.search(r"\d{4}", date)
    if yr_match:
        year = yr_match.group()
    else:
        year = date
    itype = data.get("itemType", "")
    return f"[{key}] {authors} ({year}) \"{title}\" [{itype}]"


def format_item_detail(item: dict) -> str:
    """Format an item with all key fields for comparison."""
    data = item.get("data", item)
    lines = []
    key = item.get("key", data.get("key", "???"))
    version = item.get("version", data.get("version", "?"))
    lines.append(f"**Item Key:** {key} (version {version})")
    lines.append(f"**Type:** {data.get('itemType', '?')}")
    lines.append(f"**Title:** {data.get('title', '')}")

    creators = data.get("creators", [])
    if creators:
        author_strs = []
        for c in creators:
            ctype = c.get("creatorType", "author")
            name = f"{c.get('lastName', '')}, {c.get('firstName', '')}".strip(", ")
            author_strs.append(f"{name} ({ctype})")
        lines.append(f"**Creators:** {'; '.join(author_strs)}")

    fields = [
        ("date", "Date"), ("publicationTitle", "Journal/Publication"),
        ("bookTitle", "Book Title"), ("volume", "Volume"), ("issue", "Issue"),
        ("pages", "Pages"), ("publisher", "Publisher"), ("place", "Place"),
        ("DOI", "DOI"), ("ISBN", "ISBN"), ("ISSN", "ISSN"),
        ("url", "URL"), ("abstractNote", "Abstract"),
    ]
    for field, label in fields:
        val = data.get(field, "")
        if val:
            if field == "abstractNote" and len(val) > 200:
                val = val[:200] + "..."
            lines.append(f"**{label}:** {val}")

    tags = data.get("tags", [])
    if tags:
        tag_str = ", ".join(t.get("tag", "") for t in tags)
        lines.append(f"**Tags:** {tag_str}")

    return "\n".join(lines)
