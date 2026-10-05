"""Canonical paragraphs for the decision models: PMC JATS full text, else the abstract.

Ported from POTS-phenotyping's scripts/build_paragraphs.py. One record per
paper: ARTICLE -> SECTION -> PARAGRAPH, every paragraph with a stable id, its
raw text, a normalised text and where it came from. Only running text is kept:
abstract and body paragraphs plus the data-availability statement. Figures,
tables, boxed text, supplementary material and references are excluded by
document structure, not by pattern matching. A paragraph nested in another
(a list item) is part of its parent's text, never a paragraph of its own.

    raw_text         every text node in document order (itertext), as found
    normalized_text  whitespace collapsed, citation markers replaced by [CITATION]
    id               <paper>:p<NNN>, in reading order (<paper> is the PMID)
    sha              first 12 hex of sha256(normalized_text): detects a changed parse
    kind             abstract | methods | results | discussion | availability | other,
                     which config/study_coding.yaml `read` selects on

Paper texts are copyrighted and are never committed; manifest() gives ids and
hashes only.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SKIP_JATS = {"fig", "table-wrap", "supplementary-material", "boxed-text", "ref-list", "fn-group", "app-group", "ack"}
CITE = "\x00CITE\x00"   # placeholder inserted for citation markers while walking
XLINK = "{http://www.w3.org/1999/xlink}href"

# Section headings -> kind. Matched against the whole section path, first rule wins.
KINDS = [
    ("availability", re.compile(r"data (availability|sharing|access)|availability of data|code availability", re.I)),
    ("methods", re.compile(r"method|material|study (design|population|setting|sample)|participants|data source|"
                           r"exposure assessment|outcome (assessment|ascertainment|definition)|covariates|"
                           r"statistical|analysis|cohort|setting", re.I)),
    ("results", re.compile(r"result|finding", re.I)),
    ("discussion", re.compile(r"discussion|conclusion|limitation|strength|implication", re.I)),
]


def norm(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(rf"\s*(?:[\[(]\s*)?(?:{CITE}[\s,;–-]*)+(?:\s*[\])])?", " [CITATION]", text)
    return re.sub(r"\s+([.,;:])", r"\1", text).strip()


def kind_of(section: list[str]) -> str:
    """The top-level heading decides ("Results > Sensitivity analysis" is results);
    the full path is consulted only when the top level says nothing."""
    if section and section[0] == "Abstract":
        return "abstract"
    for text in ([section[0]] if section else []) + [" > ".join(section)]:
        for kind, pattern in KINDS:
            if pattern.search(text):
                return kind
    return "other"


def text_of(el: ET.Element, is_cite) -> tuple[str, str]:
    """(raw, with citation placeholders) for an element, in document order."""
    raw, marked = [], []

    def walk(e):
        if e.text:
            raw.append(e.text)
            marked.append(CITE if is_cite(e) else e.text)
        for child in e:
            if is_cite(child):
                raw.append("".join(child.itertext()))
                marked.append(CITE)
            else:
                walk(child)
            if child.tail:
                raw.append(child.tail)
                marked.append(child.tail)
    walk(el)
    return "".join(raw), "".join(marked)


def paragraph(paper: str, n: int, section: list[str], raw: str, marked: str, provenance: dict,
              kind: str | None = None) -> dict:
    normalized = norm(marked)
    return {"id": f"{paper}:p{n:03d}", "section": " > ".join(section) or "Body", "kind": kind or kind_of(section),
            "raw_text": raw, "normalized_text": normalized,
            "sha": hashlib.sha256(normalized.encode()).hexdigest()[:12], "provenance": provenance}


def jats_license(root: ET.Element) -> str:
    """The licence URL, else its text, else ''. CC BY decides what may go to a hosted model."""
    lic = root.find(".//article-meta/permissions/license")
    if lic is None:
        lic = root.find(".//license")
    if lic is None:
        return ""
    href = lic.get(XLINK) or lic.get("href")
    if not href:
        ref = lic.find(".//{http://www.niso.org/schemas/ali/1.0/}license_ref")
        href = ref.text if ref is not None else None
    return (href or " ".join("".join(lic.itertext()).split())[:200]).strip()


def is_cc_by(license_text: str) -> bool:
    """True only for plain CC BY (any version) or CC0; NC and ND variants are not."""
    t = license_text.lower()
    if "publicdomain/zero" in t or "cc0" in t:
        return True
    return bool(re.search(r"creativecommons\.org/licenses/by/\d", t))


def from_jats(paper: str, root: ET.Element) -> list[dict]:
    is_cite = lambda e: e.tag == "xref" and e.get("ref-type") == "bibr"  # noqa: E731
    out: list[dict] = []

    def walk(el, section, xpath, kind=None):
        counts: dict[str, int] = {}
        for child in el:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            xp = f"{xpath}/{child.tag}[{counts[child.tag]}]"
            if child.tag in SKIP_JATS:
                continue
            if child.tag == "p":
                raw, marked = text_of(child, is_cite)
                if raw.strip():
                    out.append(paragraph(paper, len(out) + 1, section, raw, marked,
                                         {"source": "jats", "xpath": xp}, kind))
                continue   # nested paragraphs (list items) stay inside their parent
            title = child.find("title") if child.tag == "sec" else None
            sub = section + [" ".join("".join(title.itertext()).split())] if title is not None else section
            walk(child, sub, xp, kind)

    article = root if root.tag == "article" else root.find(".//article")
    front = article.find("./front/article-meta")
    for i, abstract in enumerate(front.findall("abstract") if front is not None else []):
        if abstract.get("abstract-type") in ("graphical", "teaser"):
            continue
        walk(abstract, ["Abstract"], f"/article/front/article-meta/abstract[{i + 1}]")
    body = article.find("./body")
    if body is not None:
        walk(body, [], "/article/body")
    # The data-availability statement lives in <back> (or as a <notes> or
    # <sec>), which the body walk never reaches.
    back = article.find("./back")
    for el in ([] if back is None else back.iter()):
        title = el.find("title")
        heading = " ".join("".join(title.itertext()).split()) if title is not None else ""
        if el.tag == "data-availability" or (
                el.tag in ("sec", "notes") and (
                    "data-availability" in (el.get("sec-type") or "") + (el.get("notes-type") or "")
                    or KINDS[0][1].search(heading))):
            walk(el, [heading or "Data availability"], f"/article/back//{el.tag}", "availability")
    return out


def from_abstract(paper: str, record: dict) -> list[dict]:
    """Paragraphs from a PubMed record: one per structured-abstract section."""
    sections = record.get("abstract_sections") or []
    out: list[dict] = []
    if sections:
        for s in sections:
            text = s.get("text") or ""
            if text.strip():
                label = s.get("label") or ""
                label = "" if label == "OtherAbstract" else label   # parse.py's marker, not a heading
                out.append(paragraph(paper, len(out) + 1, ["Abstract"] + ([label] if label else []), text, text,
                                     {"source": "pubmed", "label": label}, "abstract"))
    elif (record.get("abstract") or "").strip():
        out.append(paragraph(paper, 1, ["Abstract"], record["abstract"], record["abstract"],
                             {"source": "pubmed"}, "abstract"))
    return out


def build(record: dict, jats_path: Path | None = None) -> dict:
    """One paper: JATS full text if given and parseable, else the PubMed abstract."""
    paper = str(record["pmid"])
    title = record.get("title") or ""
    if jats_path is not None and jats_path.exists():
        try:
            root = ET.parse(jats_path).getroot()
            paras = from_jats(paper, root)
            if any(p["kind"] != "abstract" for p in paras):
                return {"paper": paper, "pmc": record.get("pmc"), "title": title, "source": "jats",
                        "license": jats_license(root), "paragraphs": paras}
        except ET.ParseError:
            pass
    return {"paper": paper, "pmc": record.get("pmc"), "title": title, "source": "abstract",
            "license": "", "paragraphs": from_abstract(paper, record)}


def manifest(papers: list[dict]) -> str:
    """Ids, kinds and hashes, no text: committed so a re-parse can be checked."""
    rows = ["id\tsha\tchars\tkind\tsection"]
    for paper in papers:
        rows += [f"{p['id']}\t{p['sha']}\t{len(p['normalized_text'])}\t{p['kind']}\t{p['section']}"
                 for p in paper["paragraphs"]]
    return "\n".join(rows) + "\n"
