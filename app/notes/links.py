"""Verification of papers and URLs mentioned in a lecture.

- Papers: search arXiv and Crossref by the wording heard; accept a match only
  when the title similarity is high. Otherwise mark uncertain/unidentified.
- URLs: resolve with an HTTP request; mark verified only on a 2xx/3xx response.
Nothing is invented: when lookups fail the item keeps the transcript wording.
"""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from rapidfuzz import fuzz

UA = {"User-Agent": "LectureCompanion/0.1 (personal study tool)"}
TIMEOUT = 12.0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def search_arxiv(query: str) -> list[dict[str, Any]]:
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": f'ti:"{query}"' if len(query.split()) > 2 else f"all:{query}", "max_results": 5})
    try:
        r = httpx.get(url, headers=UA, timeout=TIMEOUT, follow_redirects=True)
        r.raise_for_status()
    except Exception:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return []
    for e in root.findall("a:entry", ns):
        title = " ".join((e.findtext("a:title", default="", namespaces=ns) or "").split())
        link = e.findtext("a:id", default="", namespaces=ns) or ""
        authors = ", ".join(a.findtext("a:name", default="", namespaces=ns) for a in e.findall("a:author", ns))
        year = (e.findtext("a:published", default="", namespaces=ns) or "")[:4]
        out.append({"title": title, "link": link.replace("http://", "https://"), "authors": authors, "year": year, "source": "arxiv"})
    return out


def search_crossref(query: str) -> list[dict[str, Any]]:
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.title": query, "rows": 5})
    try:
        r = httpx.get(url, headers=UA, timeout=TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
    except Exception:
        return []
    out = []
    for it in items:
        title = " ".join(it.get("title", [""])[0].split()) if it.get("title") else ""
        authors = ", ".join(f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author", [])[:6])
        year = ""
        for k in ("published-print", "published-online", "issued"):
            parts = it.get(k, {}).get("date-parts", [[None]])
            if parts and parts[0] and parts[0][0]:
                year = str(parts[0][0])
                break
        doi = it.get("DOI", "")
        out.append({"title": title, "link": f"https://doi.org/{doi}" if doi else it.get("URL", ""),
                    "authors": authors, "year": year, "source": "crossref"})
    return out


def identify_paper(wording: str, context: str = "") -> dict[str, Any]:
    """Return best match with confidence, or unidentified."""
    q = wording.strip().strip('"').strip("'")
    if len(q) < 6:
        return {"confidence": "unidentified"}
    candidates = search_arxiv(q) + search_crossref(q)
    best, best_score = None, 0.0
    for c in candidates:
        if not c["title"]:
            continue
        score = max(fuzz.token_set_ratio(_norm(q), _norm(c["title"])), fuzz.partial_ratio(_norm(q), _norm(c["title"])))
        if score > best_score:
            best, best_score = c, score
    if best is None:
        return {"confidence": "unidentified"}
    if best_score >= 90:
        conf = "confident"
    elif best_score >= 72:
        conf = "uncertain"
    else:
        return {"confidence": "unidentified"}
    verified = verify_url(best["link"]) if best["link"] else False
    return {"confidence": conf, "title": best["title"], "authors": best["authors"], "year": best["year"],
            "link": best["link"], "link_verified": verified, "score": best_score}


def verify_url(url: str) -> bool:
    if not url or not re.match(r"^https?://", url):
        return False
    try:
        r = httpx.head(url, headers=UA, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code in (405, 403, 400):
            r = httpx.get(url, headers=UA, timeout=TIMEOUT, follow_redirects=True)
        return 200 <= r.status_code < 400
    except Exception:
        return False


def normalize_url_guess(text: str) -> str:
    """Turn 'hugging face dot co slash docs' style wording into a URL guess; '' if hopeless."""
    t = text.strip().lower()
    t = re.sub(r"\s+dot\s+", ".", t)
    t = re.sub(r"\s+slash\s+", "/", t)
    t = t.replace(" ", "")
    if re.match(r"^https?://", t):
        return t
    if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(/.*)?$", t):
        return "https://" + t
    return ""
