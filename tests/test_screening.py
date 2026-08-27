from screening import (
    classify_population,
    load_screening_overrides,
    partition_screening,
    screen_candidate,
)


def _article(pmcid: str, title: str, **extra):
    item = {
        "uid": pmcid.removeprefix("PMC"),
        "title": title,
        "pubtype": ["Journal Article"],
    }
    item.update(extra)
    return item


def test_known_positive_core_and_mixture_seeds_are_included():
    seeds = [
        (_article("PMC9678903", "Multi-omics signatures of the human early-life exposome",
                  mesh_terms=["Humans"], metadata_complete=True), "core"),
        (_article("PMC6144482", "The Human Early-Life Exposome (HELIX) cohort",
                  mesh_terms=["Humans"], metadata_complete=True), "core"),
        (_article("PMC11117089", "Prenatal chemical mixtures and metabolic-syndrome risk in children",
                  mesh_terms=["Humans"], metadata_complete=True), "operational_mixtures"),
        (_article("PMC10099694", "Prenatal exposures and childhood health outcomes in a cohort",
                  mesh_terms=["Humans"], metadata_complete=True), "operational_mixtures"),
        (_article("PMC13099396", "A large-scale study of the human exposome in adults",
                  mesh_terms=["Humans"], metadata_complete=True), "core"),
    ]

    decisions = [
        screen_candidate(item, query_names=[f"q-{arm}"], query_arms=[arm])
        for item, arm in seeds
    ]

    assert {decision.pmcid for decision in decisions} == {
        "PMC9678903", "PMC6144482", "PMC11117089", "PMC10099694", "PMC13099396"
    }
    assert all(decision.decision == "included" for decision in decisions)
    assert decisions[-1].population_facet == "adult"


def test_vaccine_exposure_outcome_is_valid_all_age_evidence():
    decision = screen_candidate(
        _article(
            "PMC42",
            "Influenza vaccination and hospitalization risk in older adults: a cohort study",
        ),
        query_names=["vaccine-outcomes"],
        query_arms=["operational_vaccine"],
    )

    assert decision.decision == "included"
    assert decision.scope_classification == "vaccine-exposure"
    assert decision.population_facet == "adult"


def test_vaccine_hesitancy_without_exposure_outcome_is_excluded():
    decision = screen_candidate(
        _article("PMC43", "Parental attitudes and vaccine hesitancy in a pediatric clinic"),
        query_arms=["operational_vaccine"],
    )

    assert decision.decision == "excluded"
    assert "no exposure-outcome design" in decision.exclusion_reasons[0]


def test_adversarial_uptake_association_is_not_mistaken_for_health_outcome():
    for title, abstract in (
        ("Risk factors associated with influenza vaccine uptake among adult patients", ""),
        ("Vaccine hesitancy and its association with social determinants in adults", ""),
        (
            "Vaccine hesitancy during COVID-19 infection among adults: a cross-sectional study",
            "We surveyed willingness to receive vaccination and knowledge of coronavirus disease.",
        ),
        (
            "Influenza vaccine uptake among children during respiratory disease season",
            "Parents reported immunization attitudes and respiratory disease knowledge.",
        ),
        (
            "Association between vaccine hesitancy and disease knowledge among parents",
            "We assessed attitudes toward vaccination and infection risk knowledge.",
        ),
        ("COVID-19 vaccine safety concerns and trust among adults", ""),
        ("Beliefs about vaccine adverse events among parents", ""),
        ("Knowledge of vaccine effectiveness among adult patients", ""),
        ("Vaccine uptake and parental attitudes about respiratory disease", ""),
        ("Vaccine coverage and knowledge about infection prevention", ""),
        ("Vaccine coverage and perceptions of infection severity", ""),
        ("Vaccine uptake and trust during infectious disease outbreaks", ""),
    ):
        decision = screen_candidate(
            _article("PMC44", title, abstract=abstract),
            query_names=["operational_vaccine_adverse_events"],
            query_arms=["operational_vaccine"],
        )
        assert decision.decision == "excluded"
        assert "no exposure-outcome design" in decision.exclusion_reasons[0]

    valid = screen_candidate(
        _article(
            "PMC45",
            "Influenza vaccine coverage and subsequent hospitalization in adult patients",
        ),
        query_names=["operational_vaccine_adverse_events"],
        query_arms=["operational_vaccine"],
    )
    assert valid.decision == "included"


def test_vaccine_gate_handles_scope_overlap_and_explicit_all_age_outcomes():
    dual_hit_survey = screen_candidate(
        _article(
            "PMC48",
            "The social exposome and vaccine hesitancy among adults: a cross-sectional study",
        ),
        query_names=["core_exposome", "operational_vaccine_adverse_events"],
        query_arms=["core", "operational_vaccine"],
    )
    pediatric_safety = screen_candidate(
        _article(
            "PMC49",
            "Influenza vaccine coverage and safety in children: a prospective cohort study",
        ),
        query_names=["operational_vaccine_adverse_events"],
        query_arms=["operational_vaccine"],
    )
    pediatric_temporal = screen_candidate(
        _article(
            "PMC50",
            "Vaccine uptake and subsequent hospitalization in children: a cohort study",
        ),
        query_names=["operational_vaccine_adverse_events"],
        query_arms=["operational_vaccine"],
    )
    mixed_immunity = screen_candidate(
        _article(
            "PMC51",
            "Influenza vaccine immunogenicity in children and adults: a clinical trial",
            abstract="To our knowledge, this is the first randomized study.",
        ),
        query_names=["operational_vaccine_adverse_events"],
        query_arms=["operational_vaccine"],
    )

    assert dual_hit_survey.scope_classification == "core-exposomics"
    assert dual_hit_survey.decision == "excluded"
    assert "no exposure-outcome design" in dual_hit_survey.exclusion_reasons[0]
    assert pediatric_safety.decision == "included"
    assert pediatric_safety.population_facet == "pediatric"
    assert pediatric_temporal.decision == "included"
    assert pediatric_temporal.population_facet == "pediatric"
    assert mixed_immunity.decision == "included"
    assert mixed_immunity.population_facet == "mixed"

    effectiveness_with_secondary_survey = screen_candidate(
        _article(
            "PMC53",
            "COVID-19 vaccine effectiveness against hospitalization in adults: a cohort study",
            abstract="A secondary questionnaire measured patient acceptance.",
        ),
        query_names=["operational_vaccine_adverse_events"],
        query_arms=["operational_vaccine"],
    )
    assert effectiveness_with_secondary_survey.decision == "included"


def test_vaccine_record_without_explicit_outcome_remains_pending():
    decision = screen_candidate(
        _article("PMC52", "A randomized vaccination program among adult patients"),
        query_names=["operational_vaccine_linked_data"],
        query_arms=["operational_vaccine"],
    )

    assert decision.decision == "pending"
    assert not decision.exclusion_reasons
    assert any("not explicit" in item for item in decision.eligibility_evidence)


def test_vaccine_gate_leaves_non_health_topics_and_indications_pending():
    for title, abstract in (
        ("Post-vaccination travel behavior among adults", "We also recorded infection history."),
        ("Vaccine-associated social media discourse among adults", "Participants reported infections."),
        ("Infectious disease surveillance in vaccination programs", ""),
        ("Vaccination and infectious disease education in schools", ""),
        ("Implementation of vaccination against infectious disease", ""),
    ):
        decision = screen_candidate(
            _article("PMC54", title, abstract=abstract),
            query_names=["operational_vaccine_adverse_events"],
            query_arms=["operational_vaccine"],
        )
        assert decision.decision == "pending"

    for title in (
        "Vaccination followed by infection-control training among adults",
        "Influenza vaccination associated with diabetes education attendance among adults",
        "COVID vaccination associated with infection surveillance participation among adults",
        "COVID vaccine safety communication among adults",
        "Vaccine—safety training among adult patients",
        "Evaluation of vaccine safety reporting systems among clinicians",
        "Vaccine adverse-event reporting systems among adult clinicians",
    ):
        decision = screen_candidate(
            _article("PMC55", title, abstract="Prospective cohort participants."),
            query_names=["operational_vaccine_adverse_events"],
            query_arms=["operational_vaccine"],
        )
        assert decision.decision == "pending"

    for title in (
        "Vaccine coverage associated with infection knowledge among parents",
        "Vaccine status associated with cancer screening uptake among adults",
    ):
        decision = screen_candidate(
            _article("PMC56", title, abstract="Cross-sectional human participants."),
            query_names=["operational_vaccine_adverse_events"],
            query_arms=["operational_vaccine"],
        )
        assert decision.decision == "excluded"


def test_vaccine_gate_accepts_subtitle_structured_and_generic_clinical_outcomes():
    titles = (
        "BNT162b2 vaccination in adolescents: safety and immunogenicity in a clinical trial",
        "COVID-19 vaccine—safety and immunogenicity in adults: a cohort study",
        "Risk of myocarditis after COVID-19 vaccination in adolescents: a cohort study",
    )
    for index, title in enumerate(titles, 60):
        decision = screen_candidate(
            _article(f"PMC{index}", title),
            query_names=["operational_vaccine_adverse_events"],
            query_arms=["operational_vaccine"],
        )
        assert decision.decision == "included"

    structured = screen_candidate(
        _article(
            "PMC63",
            "Prospective cohort in children",
            abstract="Exposure: vaccine coverage; Outcome: hospitalization.",
        ),
        query_names=["operational_vaccine_adverse_events"],
        query_arms=["operational_vaccine"],
    )
    assert structured.decision == "included"

    for index, abstract in enumerate((
        "EXPOSURES: Vaccine coverage. MAIN OUTCOMES AND MEASURES: Hospitalization.",
        "Exposures: vaccination status; Primary outcome: infection.",
    ), 64):
        labeled = screen_candidate(
            _article(
                f"PMC{index}",
                "Prospective cohort among adult patients",
                abstract=abstract,
            ),
            query_names=["operational_vaccine_linked_data"],
            query_arms=["operational_vaccine"],
        )
        assert labeled.decision == "included"


def test_vaccine_gate_accepts_rates_status_comparisons_and_named_vaccines():
    titles = (
        "Vaccine coverage and rates of infection in children: a cohort study",
        "Infection among vaccinated versus unvaccinated children: a cohort study",
        "SARS-CoV-2 infection by vaccination status among adult patients: a cohort study",
        "MMR and febrile seizures in children: a cohort study",
        "DTaP-associated fever in infants: a clinical trial",
        "Vaccination status and incidence of infection in adult patients: a cohort study",
        "COVID-19 vaccination and subsequent infection in adults: a cohort study",
        "COVID-19 vaccination and infection risk in adults: a cohort study",
        "Vaccination schedule and risk of autism spectrum disorder in children: a cohort study",
        "Vaccine exposure and risk of stroke in adult patients: a cohort study",
        "Vaccination schedule and neurodevelopmental outcomes in children: a cohort study",
        "Vaccine exposure and risk of Bell palsy in adult patients: a cohort study",
    )
    for index, title in enumerate(titles, 70):
        decision = screen_candidate(
            _article(f"PMC{index}", title),
            query_names=["operational_named_vaccine_safety"],
            query_arms=["operational_vaccine"],
        )
        assert decision.decision == "included"


def test_genetics_only_and_human_cell_in_vitro_records_are_excluded():
    genetics = screen_candidate(
        _article("PMC46", "Human genome-wide association results in an exposome cohort"),
        query_arms=["core"],
    )
    cells = screen_candidate(
        _article("PMC47", "In vitro effects of vaccine exposure in human cell lines"),
        query_arms=["operational_vaccine"],
    )

    assert genetics.decision == "excluded"
    assert genetics.scope_classification == "out-of-scope"
    assert "genetics-only" in genetics.exclusion_reasons[-1]
    assert cells.decision == "excluded"
    assert cells.human_study is False


def test_review_animal_and_correction_negative_seeds_are_excluded():
    review = screen_candidate(
        _article("PMC1", "The human exposome: a systematic review", pubtype=["Review"]),
        query_arms=["core"],
    )
    animal = screen_candidate(
        _article("PMC2", "Prenatal mixture exposure in rat brain"),
        query_arms=["operational_mixtures"],
    )
    correction = screen_candidate(
        _article("PMC3", "Correction: human exposome cohort", pubtype=["Published Erratum"]),
        query_arms=["core"],
    )

    assert {review.decision, animal.decision, correction.decision} == {"excluded"}
    assert animal.human_study is False
    assert review.primary_study is False


def test_ambiguous_candidate_is_pending_not_silently_included():
    decision = screen_candidate(
        {"uid": "9", "title": "Integrated exposome signatures", "pubtype": []},
        query_names=["core-exposome"],
        query_arms=["core"],
    )

    assert decision.decision == "pending"
    assert decision.human_study is None
    assert decision.primary_study is None
    assert decision.query_provenance == ("core-exposome",)


def test_missing_pubmed_enrichment_is_pending_even_with_title_heuristics():
    decision = screen_candidate(
        _article(
            "PMC90",
            "Human exposome cohort",
            metadata_complete=False,
            metadata_missing=["pmid"],
        ),
        query_arms=["core"],
    )
    assert decision.human_study is True
    assert decision.primary_study is True
    assert decision.decision == "pending"
    assert decision.metadata_complete is False


def test_manual_override_records_reviewer_and_evidence():
    decision = screen_candidate(
        {"uid": "9", "title": "Integrated exposome signatures", "pubtype": []},
        query_names=["core_exposome"],
        query_arms=["core"],
        override={
            "decision": "included",
            "reviewer": "AB",
            "reviewed_at": "2026-08-21T12:00:00Z",
            "eligibility_evidence": ["Full text reports a prospective human cohort."],
            "human_study": True,
            "primary_study": True,
            "population_facet": "mixed",
        },
    )

    assert decision.decision == "included"
    assert decision.screening_method == "manual-override"
    assert decision.reviewer == "AB"
    assert decision.population_facet == "mixed"
    assert "prospective human cohort" in decision.eligibility_evidence[-1]


def test_incomplete_manual_inclusion_remains_pending():
    decision = screen_candidate(
        {"uid": "10", "title": "Integrated exposome signatures", "pubtype": []},
        query_arms=["core"],
        override={"decision": "included", "reviewer": "AB"},
    )

    assert decision.decision == "pending"
    assert "manual inclusion requires" in decision.exclusion_reasons[-1]


def test_durable_override_loader_requires_reviewer_timestamp_and_evidence(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text("""{
      "records": {
        "9": {
          "decision": "included",
          "reviewer": "AB",
          "reviewed_at": "2026-08-21T12:00:00Z",
          "eligibility_evidence": ["Verified prospective human cohort."],
          "human_study": true,
          "primary_study": true
        }
      }
    }""")
    loaded = load_screening_overrides(valid)
    assert list(loaded) == ["PMC9"]
    assert loaded["PMC9"]["reviewer"] == "AB"

    invalid = tmp_path / "invalid.json"
    invalid.write_text("""{
      "PMC9": {"decision": "included", "reviewer": "AB",
               "eligibility_evidence": ["evidence"]}
    }""")
    import pytest
    with pytest.raises(ValueError, match="reviewed_at"):
        load_screening_overrides(invalid)


def test_partition_keeps_candidates_separate_from_included_records():
    included = screen_candidate(
        _article("PMC1", "Human exposome cohort"),
        query_names=["core_exposome"], query_arms=["core"],
    )
    pending = screen_candidate(
        {"uid": "2", "title": "Exposome signatures", "pubtype": []},
        query_arms=["core"],
    )

    split = partition_screening([pending, included])

    assert [row["pmcid"] for row in split["included"]] == ["PMC1"]
    assert [row["pmcid"] for row in split["pending"]] == ["PMC2"]
    assert split["excluded"] == []


def test_population_is_a_facet_not_an_inclusion_rule():
    assert classify_population("children and adults in a national cohort") == "mixed"
    adult = screen_candidate(
        _article("PMC7", "Adult workers in an environment-wide association study"),
        query_names=["core_exposome"],
        query_arms=["core"],
    )
    pediatric = screen_candidate(
        _article("PMC8", "Childhood environment-wide association cohort study"),
        query_names=["core_exposome"],
        query_arms=["core"],
    )

    assert adult.decision == pediatric.decision == "included"
    assert adult.population_facet == "adult"
    assert pediatric.population_facet == "pediatric"
