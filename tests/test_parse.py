"""Parser tests against a fixture exercising the awkward parts of PubMed XML."""

from exposome_ehr.parse import parse_efetch_response


def test_two_records_parsed(sample_xml):
    records = parse_efetch_response(sample_xml)
    assert [r["pmid"] for r in records] == ["99900001", "99900002"]


def test_core_metadata(sample_xml):
    record = parse_efetch_response(sample_xml)[0]
    assert record["doi"] == "10.1000/test.2024.0001"
    assert record["pmc"] == "PMC9990001"
    assert record["journal_iso"] == "J Test Auton"
    assert record["journal_country"] == "England"
    assert (record["pub_year"], record["pub_month"], record["pub_day"]) == (2024, 3, 7)
    assert record["article_date"] == "2024-02-28"
    assert record["entrez_date"] == "2024-03-01"
    assert record["pagination"] == "101-115"


def test_structured_abstract_keeps_labels_and_inline_markup(sample_xml):
    record = parse_efetch_response(sample_xml)[0]
    assert record["abstract"].startswith("BACKGROUND: Phenotypes overlap substantially.")
    assert "QSART was reduced in 33%" in record["abstract"]
    assert record["abstract_section_count"] == 2
    labels = [s["label"] for s in record["abstract_sections"]]
    assert labels == ["BACKGROUND", "RESULTS"]


def test_mesh_qualifiers_expand_to_one_row_each(sample_xml):
    record = parse_efetch_response(sample_xml)[0]
    pots_rows = [
        m for m in record["mesh_headings"] if m["descriptor_ui"] == "D000068917"
    ]
    assert len(pots_rows) == 2
    assert all(row["descriptor_major"] for row in pots_rows)
    assert {row["qualifier_name"] for row in pots_rows} == {"diagnosis", "physiopathology"}
    assert [row["qualifier_major"] for row in sorted(pots_rows, key=lambda r: r["qualifier_name"])] == [
        False,
        True,
    ]
    # A heading with no qualifier still produces exactly one row.
    humans = [m for m in record["mesh_headings"] if m["descriptor_name"] == "Humans"]
    assert len(humans) == 1 and humans[0]["qualifier_ui"] is None


def test_species_check_tags_and_flags(sample_xml):
    record = parse_efetch_response(sample_xml)[0]
    assert record["species_check_tags"] == ["Humans"]
    assert record["is_human_tagged"] is True
    assert record["is_animal_tagged"] is False
    assert record["is_mesh_indexed"] is True
    assert record["mesh_descriptor_count"] == 3


def test_authors_including_collective_and_orcid(sample_xml):
    record = parse_efetch_response(sample_xml)[0]
    assert record["author_count"] == 2
    first, second = record["authors"]
    assert first["last_name"] == "Doe"
    assert first["orcid"] == "0000-0002-1825-0097"
    assert first["affiliation_count"] == 2
    assert second["collective_name"] == "The Test Autonomic Consortium"


def test_keywords_chemicals_grants_references(sample_xml):
    record = parse_efetch_response(sample_xml)[0]
    assert {k["term"] for k in record["keywords"]} == {"dysautonomia", "QSART"}
    assert [k["major"] for k in record["keywords"] if k["term"] == "dysautonomia"] == [True]
    assert record["chemicals"][0]["name"] == "Norepinephrine"
    assert record["grants"][0]["agency"] == "NINDS NIH HHS"
    assert record["reference_pmids"] == ["19207771"]


def test_erratum_is_not_a_retraction(sample_xml):
    record = parse_efetch_response(sample_xml)[0]
    assert record["integrity_flags"] == ["has_erratum"]
    assert record["is_retracted"] is False


def test_retraction_and_medline_date_and_other_abstract(sample_xml):
    record = parse_efetch_response(sample_xml)[1]
    assert record["is_retracted"] is True
    assert "retracted" in record["integrity_flags"]
    assert record["medline_date"] == "2019 Sep-Oct"
    assert record["pub_year"] == 2019
    assert record["pub_month"] == 9
    assert record["languages"] == ["spa"]
    assert record["vernacular_title"] == "Taquicardia ortostática postural."
    # Non-English record with no Abstract falls back to OtherAbstract.
    assert record["abstract"] == "An English publisher abstract for a Spanish record."
    assert record["is_mesh_indexed"] is False
    assert "Retracted Publication" in record["publication_type_names"]


def test_present_but_childless_article_is_not_discarded():
    """A present-but-empty <Article> must be used, not dropped for being falsy.

    ElementTree treats a childless element as falsy, so the old
    `citation.find("Article") or citation` silently fell back to the citation.
    """
    import xml.etree.ElementTree as ET
    from exposome_ehr.parse import parse_article

    xml = (
        "<PubmedArticle><MedlineCitation Status='MEDLINE'>"
        "<PMID Version='1'>123</PMID>"
        "<Article><ArticleTitle>Only a title</ArticleTitle></Article>"
        "</MedlineCitation></PubmedArticle>"
    )
    record = parse_article(ET.fromstring(xml))
    assert record["title"] == "Only a title"
