"""Turn a PubMed ``PubmedArticle`` XML element into a flat record.

Nothing is filtered here. Language, publication type, retraction status and MeSH
species check-tags are all captured so that inclusion rules can be applied later
in SQL without re-crawling, which is the project's stated harvest policy.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

SPECIES_CHECK_TAGS = {"Humans", "Animals", "Mice", "Rats", "Dogs", "Rabbits", "Cats", "Swine"}

# CommentsCorrections RefTypes that mark this record as withdrawn or amended.
INTEGRITY_REFTYPES = {
    "RetractionIn": "retracted",
    "RetractionOf": "retraction_notice",
    "ErratumIn": "has_erratum",
    "ErratumFor": "erratum_notice",
    "ExpressionOfConcernIn": "expression_of_concern",
    "ExpressionOfConcernFor": "expression_of_concern_notice",
    "RepublishedIn": "republished",
    "PartialRetractionIn": "partially_retracted",
}


def _text(element: ET.Element | None) -> str | None:
    """Flattened text of an element, including inline markup children."""
    if element is None:
        return None
    joined = "".join(element.itertext())
    cleaned = " ".join(joined.split())
    return cleaned or None


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _month(value: str | None) -> int | None:
    if not value:
        return None
    as_int = _int_or_none(value)
    if as_int is not None:
        return as_int
    return MONTHS.get(value.strip()[:3].lower())


def _parse_pubdate(node: ET.Element | None) -> dict[str, Any]:
    """Year/month/day from a PubDate, coping with MedlineDate free text."""
    out: dict[str, Any] = {"year": None, "month": None, "day": None, "medline_date": None}
    if node is None:
        return out
    medline = _text(node.find("MedlineDate"))
    if medline:
        out["medline_date"] = medline
        head = medline.split("-")[0].strip()
        parts = head.split()
        out["year"] = _int_or_none(parts[0]) if parts else None
        if len(parts) > 1:
            out["month"] = _month(parts[1])
        return out
    out["year"] = _int_or_none(_text(node.find("Year")))
    out["month"] = _month(_text(node.find("Month")))
    out["day"] = _int_or_none(_text(node.find("Day")))
    return out


def _abstract(
    article: ET.Element, citation: ET.Element | None = None
) -> tuple[str | None, list[dict[str, Any]]]:
    """Return the concatenated abstract and its structured sections.

    ``citation`` is the MedlineCitation element, needed because OtherAbstract is
    a sibling of Article rather than a child of it.
    """
    sections: list[dict[str, Any]] = []
    pieces: list[str] = []
    for order, node in enumerate(article.findall("./Abstract/AbstractText")):
        body = _text(node)
        if not body:
            continue
        label = node.get("Label")
        sections.append(
            {
                "order": order,
                "label": label,
                "nlm_category": node.get("NlmCategory"),
                "text": body,
            }
        )
        pieces.append(f"{label}: {body}" if label else body)
    joined = " ".join(pieces) if pieces else None

    if joined is None:
        # Non-English records sometimes carry only an OtherAbstract, which hangs
        # off MedlineCitation rather than Article.
        containers = [article] if citation is None else [citation, article]
        others = [node for c in containers for node in c.findall("./OtherAbstract")]
        for other in others:
            texts = [t for t in (_text(n) for n in other.findall("AbstractText")) if t]
            if texts:
                joined = " ".join(texts)
                sections.append(
                    {
                        "order": 0,
                        "label": "OtherAbstract",
                        "nlm_category": other.get("Language"),
                        "text": joined,
                    }
                )
                break
    return joined, sections


def _authors(article: ET.Element) -> list[dict[str, Any]]:
    authors: list[dict[str, Any]] = []
    for position, node in enumerate(article.findall("./AuthorList/Author"), start=1):
        collective = _text(node.find("CollectiveName"))
        affiliations = [
            a for a in (_text(x) for x in node.findall("./AffiliationInfo/Affiliation")) if a
        ]
        orcid = None
        for ident in node.findall("Identifier"):
            if (ident.get("Source") or "").upper() == "ORCID":
                orcid = _text(ident)
        authors.append(
            {
                "position": position,
                "last_name": _text(node.find("LastName")),
                "fore_name": _text(node.find("ForeName")),
                "initials": _text(node.find("Initials")),
                "collective_name": collective,
                "orcid": orcid,
                "affiliation": affiliations[0] if affiliations else None,
                "affiliation_count": len(affiliations),
            }
        )
    return authors


def _mesh(article: ET.Element) -> list[dict[str, Any]]:
    """One row per descriptor/qualifier pair; qualifier is NULL when absent."""
    rows: list[dict[str, Any]] = []
    for heading in article.findall("./MeshHeadingList/MeshHeading"):
        descriptor = heading.find("DescriptorName")
        if descriptor is None:
            continue
        base = {
            "descriptor_ui": descriptor.get("UI"),
            "descriptor_name": _text(descriptor),
            "descriptor_major": descriptor.get("MajorTopicYN") == "Y",
        }
        qualifiers = heading.findall("QualifierName")
        if not qualifiers:
            rows.append({**base, "qualifier_ui": None, "qualifier_name": None, "qualifier_major": False})
            continue
        for qualifier in qualifiers:
            rows.append(
                {
                    **base,
                    "qualifier_ui": qualifier.get("UI"),
                    "qualifier_name": _text(qualifier),
                    "qualifier_major": qualifier.get("MajorTopicYN") == "Y",
                }
            )
    return rows


def _ids(citation_parent: ET.Element) -> dict[str, str | None]:
    out: dict[str, str | None] = {"doi": None, "pmc": None, "pii": None}
    for node in citation_parent.findall("./PubmedData/ArticleIdList/ArticleId"):
        kind = (node.get("IdType") or "").lower()
        if kind in out and out[kind] is None:
            out[kind] = _text(node)
    return out


def _history(citation_parent: ET.Element) -> dict[str, str | None]:
    """PubMed workflow dates, keyed by PubStatus, as ISO-ish strings."""
    out: dict[str, str | None] = {}
    for node in citation_parent.findall("./PubmedData/History/PubMedPubDate"):
        status = node.get("PubStatus")
        if not status:
            continue
        year = _text(node.find("Year"))
        month = _month(_text(node.find("Month")))
        day = _int_or_none(_text(node.find("Day")))
        if year:
            out[status] = f"{int(year):04d}-{(month or 1):02d}-{(day or 1):02d}"
    return out


def _integrity(article_root: ET.Element) -> tuple[list[str], list[dict[str, Any]]]:
    flags: list[str] = []
    links: list[dict[str, Any]] = []
    for node in article_root.findall(".//CommentsCorrections"):
        ref_type = node.get("RefType") or ""
        target = _text(node.find("PMID"))
        links.append(
            {
                "ref_type": ref_type,
                "ref_source": _text(node.find("RefSource")),
                "target_pmid": target,
            }
        )
        flag = INTEGRITY_REFTYPES.get(ref_type)
        if flag and flag not in flags:
            flags.append(flag)
    return flags, links


def parse_article(element: ET.Element) -> dict[str, Any]:
    """Parse one PubmedArticle (or PubmedBookArticle) element into a record."""
    citation = element.find("MedlineCitation")
    if citation is None:
        citation = element.find("BookDocument")
    if citation is None:
        raise ValueError("element has neither MedlineCitation nor BookDocument")

    # An ElementTree element with no children is falsy, so `find(...) or citation`
    # would silently discard a present-but-childless <Article>. Test identity.
    article = citation.find("Article")
    if article is None:
        article = citation
    journal = article.find("Journal")

    pmid_node = citation.find("PMID")
    pmid = _text(pmid_node)
    if not pmid:
        raise ValueError("record has no PMID")

    pub_date = _parse_pubdate(journal.find("./JournalIssue/PubDate") if journal is not None else None)
    article_date = _parse_pubdate(article.find("ArticleDate"))
    abstract_text, abstract_sections = _abstract(article, citation)
    mesh = _mesh(citation)
    mesh_names = {row["descriptor_name"] for row in mesh}
    integrity_flags, integrity_links = _integrity(element)
    history = _history(element)

    title = _text(article.find("ArticleTitle")) or _text(citation.find("./Book/BookTitle"))
    vernacular = _text(article.find("VernacularTitle"))

    languages = [lang for lang in (_text(n) for n in article.findall("Language")) if lang]
    pub_types = [
        {"ui": n.get("UI"), "name": _text(n)}
        for n in article.findall("./PublicationTypeList/PublicationType")
        if _text(n)
    ]

    record: dict[str, Any] = {
        "pmid": pmid,
        "pmid_version": pmid_node.get("Version") if pmid_node is not None else None,
        "record_type": element.tag,
        "title": title,
        "vernacular_title": vernacular,
        "abstract": abstract_text,
        "abstract_section_count": len(abstract_sections),
        "has_abstract": abstract_text is not None,
        "journal_title": _text(journal.find("Title")) if journal is not None else None,
        "journal_iso": _text(journal.find("ISOAbbreviation")) if journal is not None else None,
        "journal_nlm_id": _text(citation.find("./MedlineJournalInfo/NlmUniqueID")),
        "journal_country": _text(citation.find("./MedlineJournalInfo/Country")),
        "issn": _text(journal.find("ISSN")) if journal is not None else None,
        "volume": _text(journal.find("./JournalIssue/Volume")) if journal is not None else None,
        "issue": _text(journal.find("./JournalIssue/Issue")) if journal is not None else None,
        "pagination": _text(article.find("./Pagination/MedlinePgn")),
        "pub_year": pub_date["year"],
        "pub_month": pub_date["month"],
        "pub_day": pub_date["day"],
        "medline_date": pub_date["medline_date"],
        "article_date": (
            f"{article_date['year']:04d}-{(article_date['month'] or 1):02d}-{(article_date['day'] or 1):02d}"
            if article_date["year"]
            else None
        ),
        "entrez_date": history.get("entrez") or history.get("pubmed"),
        "medline_status": citation.get("Status"),
        "owner": citation.get("Owner"),
        "indexing_method": citation.get("IndexingMethod"),
        "publication_status": _text(element.find("./PubmedData/PublicationStatus")),
        "languages": languages,
        "publication_types": pub_types,
        "mesh_headings": mesh,
        "mesh_descriptor_count": len({row["descriptor_ui"] for row in mesh}),
        "is_mesh_indexed": bool(mesh),
        "species_check_tags": sorted(mesh_names & SPECIES_CHECK_TAGS),
        "chemicals": [
            {"ui": n.get("UI") if n is not None else None, "name": _text(n)}
            for n in citation.findall("./ChemicalList/Chemical/NameOfSubstance")
            if _text(n)
        ],
        "keywords": [
            {"term": kw, "major": maj}
            for kw, maj in (
                (_text(n), n.get("MajorTopicYN") == "Y")
                for n in citation.findall("./KeywordList/Keyword")
            )
            if kw
        ],
        "grants": [
            {
                "grant_id": _text(n.find("GrantID")),
                "agency": _text(n.find("Agency")),
                "country": _text(n.find("Country")),
            }
            for n in article.findall("./GrantList/Grant")
        ],
        "authors": _authors(article),
        "abstract_sections": abstract_sections,
        "integrity_flags": integrity_flags,
        "integrity_links": integrity_links,
        "reference_pmids": sorted(
            {
                pm
                for pm in (
                    _text(n)
                    for n in element.findall(
                        "./PubmedData/ReferenceList//Reference/ArticleIdList/ArticleId[@IdType='pubmed']"
                    )
                )
                if pm
            }
        ),
        **_ids(element),
    }
    record["author_count"] = len(record["authors"])
    record["is_retracted"] = "retracted" in integrity_flags
    record["is_human_tagged"] = "Humans" in mesh_names
    record["is_animal_tagged"] = "Animals" in mesh_names
    record["publication_type_names"] = [pt["name"] for pt in pub_types]
    return record


def parse_efetch_response(raw_xml: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(raw_xml)
    records = []
    for element in list(root.findall("PubmedArticle")) + list(root.findall("PubmedBookArticle")):
        records.append(parse_article(element))
    return records
