# Exposome / EHR PubMed harvest report
| field | value |
| --- | --- |
| run_id | run_20261005T023634Z |
| started_at | 2026-10-05T02:36:34+00:00 |
| finished_at | 2026-10-05T02:40:26+00:00 |
| query_profile | core |
| esearch_count | 15913 |
| pmids_retrieved | 15913 |
| articles_stored | 15913 |
| facet_queries_run | 67 |
| eutils_requests | 181 |
| tool_version | 0.1.0 |

## Checks

24 of 26 passed.

| result | check | detail |
| --- | --- | --- |
| PASS | corpus is non-empty | 15913 articles stored |
| PASS | every PMID esearch reported was retrieved | esearch reported 15913, retrieved 15913 |
| PASS | seed wild2005 is in the corpus | expected PMID 16103423 found |
| PASS | seed wild2005 lookup query still resolves correctly | lookup returned 1 hits, resolved to 16103423 by title_exact; expected 16103423 |
| PASS | seed patel2010 is in the corpus | expected PMID 20505766 found |
| PASS | seed patel2010 lookup query still resolves correctly | lookup returned 2 hits, resolved to 20505766 by title_exact; expected 20505766 |
| PASS | seed maitre2018 is in the corpus | expected PMID 30206078 found |
| PASS | seed maitre2018 lookup query still resolves correctly | lookup returned 2 hits, resolved to 30206078 by title_exact; expected 30206078 |
| PASS | seed vrijheid2020 is in the corpus | expected PMID 32579081 found |
| PASS | seed vrijheid2020 lookup query still resolves correctly | lookup returned 1 hits, resolved to 32579081 by title_exact; expected 32579081 |
| PASS | seed brokamp2018 is in the corpus | expected PMID 29126118 found |
| PASS | seed brokamp2018 lookup query still resolves correctly | lookup returned 1 hits, resolved to 29126118 by title_exact; expected 29126118 |
| PASS | seed hu2023 is in the corpus | expected PMID 37089437 found |
| PASS | seed hu2023 lookup query still resolves correctly | lookup returned 1 hits, resolved to 37089437 by title_exact; expected 37089437 |
| PASS | seed correa2022 is in the corpus | expected PMID 35970309 found |
| PASS | seed correa2022 lookup query still resolves correctly | lookup returned 1 hits, resolved to 35970309 by title_exact; expected 35970309 |
| PASS | seed macdonald2014 is in the corpus | expected PMID 24914115 found |
| PASS | seed macdonald2014 lookup query still resolves correctly | lookup returned 5 hits, resolved to 24914115 by title_exact; expected 24914115 |
| PASS | every facet tagged at least one article | all facets non-empty |
| FAIL | every facet has at least one candidate concept | 48 of 67 facets mapped, 181 candidate rows |
| FAIL | no undeclared concept-resolution misses | 0 declared fallback gaps, undeclared: [('biomonitoring', 'biological monitoring'), ('drinking_water_contamination', 'contaminated drinking water'), ('ehr', 'electronic health record'), ('endocrine_disruptors', 'exposure to endocrine disrupting chemical'), ('exposome_wide_design', 'exposome')] |
| PASS | declared fallback gaps are still gaps (informational) | 0 of 0 declared gaps unresolved by the public fallback, as expected; supply --athena-dir to close them |
| PASS | ontology graph has no dangling edges | 0 dangling |
| PASS | no node is its own ancestor | 0 self-loops |
| PASS | curated vocabulary is internally consistent | clean |
| PASS | every declared MeSH descriptor is a real NLM descriptor | 201 descriptors checked against PubMed's MeSH index; 0 are valid but unused in this corpus |

## Corpus query clause contribution

`esearch_count` is the clause's hit count across all of PubMed; `n_in_corpus` is how many of those ended up in this corpus.

| block_id | esearch_count | n_in_corpus | rationale |
| --- | --- | --- | --- |
| environment_in_health_records | 11624 | 11624 | The core of the old Tiers 2-4: an environmental or social exposure, studied in routinely collected individual- |
| ewas | 297 | 297 | Agnostic, many-exposure association designs (Patel 2010) whose abstracts do not always say "exposome". |
| exposome | 3123 | 3123 | The exposome concept itself (Wild 2005). MeSH "Exposome" was introduced in 2023, so the text words carry the o |
| vaccine_as_exposure | 1116 | 1116 | Old Tier 5: a vaccine as the exposure and a health event as the outcome, in surveillance, registry, claims or  |

### Clause exclusivity

`n_only_this_block` counts records that NO other clause retrieved. A clause with a large exclusive count is carrying the corpus on its own, which is where recall is won and where off-target noise enters.

| block_id | n_in_corpus | n_only_this_block |
| --- | --- | --- |
| environment_in_health_records | 11624 | 11558 |
| exposome | 3123 | 2892 |
| vaccine_as_exposure | 1116 | 1109 |
| ewas | 297 | 113 |

## Corpus composition

| metric | n | share |
| --- | --- | --- |
| articles | 15913 | 100.0% |
| with abstract | 15444 | 97.1% |
| MeSH indexed | 13389 | 84.1% |
| human check-tag | 13045 | 82.0% |
| animal check-tag | 690 | 4.3% |
| retracted | 2 | 0.0% |
| has DOI | 15206 | 95.6% |
| has PMC id | 8692 | 54.6% |
| non-English | 345 | 2.2% |

### Publication types, top 15

| type_name | n_articles |
| --- | --- |
| Journal Article | 15610 |
| Research Support, Non-U.S. Gov't | 4745 |
| Research Support, N.I.H., Extramural | 1996 |
| Review | 1432 |
| Research Support, U.S. Gov't, P.H.S. | 817 |
| Comparative Study | 501 |
| Research Support, U.S. Gov't, Non-P.H.S. | 424 |
| Multicenter Study | 360 |
| Observational Study | 358 |
| Systematic Review | 196 |
| English Abstract | 175 |
| Randomized Controlled Trial | 165 |
| Editorial | 141 |
| Preprint | 141 |
| Research Support, N.I.H., Intramural | 120 |

## Facet coverage

`query` counts title/abstract query hits inside the corpus; `mesh` counts records NLM indexed with one of the facet's MeSH descriptors. The two paths are independent.

| group | facet_id | query | mesh | any |
| --- | --- | --- | --- | --- |
| assessment | questionnaire | 1880 | 549 | 2062 |
| assessment | residential_geocoding | 1247 | 159 | 1332 |
| assessment | biomonitoring | 1253 | 542 | 1324 |
| assessment | monitoring_station | 119 | 490 | 583 |
| assessment | spatial_model | 214 | 103 | 311 |
| assessment | record_based_exposure | 171 | 129 | 298 |
| assessment | personal_sensor | 173 | 91 | 243 |
| data_source | health_registry | 5468 | 2435 | 6040 |
| data_source | research_cohort | 1007 | 2581 | 2914 |
| data_source | ehr | 2267 | 545 | 2348 |
| data_source | claims | 570 | 484 | 964 |
| data_source | record_linkage | 413 | 52 | 443 |
| data_source | immunization_surveillance | 344 | 174 | 440 |
| data_source | national_survey | 184 | 202 | 305 |
| data_source | biobank | 272 | 48 | 279 |
| design | exposome_wide_design | 320 | 767 | 948 |
| design | machine_learning | 551 | 157 | 554 |
| design | self_controlled_design | 403 | 137 | 415 |
| design | time_series_design | 330 | 10 | 331 |
| design | gene_environment | 257 | 117 | 300 |
| design | mixture_methods | 224 | 9 | 229 |
| exposure | individual_social_factors | 3771 | 1089 | 3840 |
| exposure | neighborhood_deprivation | 1833 | 1150 | 2446 |
| exposure | prenatal_medication | 162 | 1657 | 1681 |
| exposure | vaccination | 1315 | 683 | 1324 |
| exposure | particulate_matter | 1263 | 829 | 1318 |
| exposure | built_environment | 695 | 709 | 1259 |
| exposure | pesticides | 903 | 482 | 931 |
| exposure | tobacco_smoke | 565 | 693 | 872 |
| exposure | traffic_related_air_pollution | 683 | 353 | 738 |
| exposure | persistent_organic_pollutants | 562 | 266 | 568 |
| exposure | other_metals | 449 | 213 | 464 |
| exposure | ozone | 421 | 168 | 430 |
| exposure | temperature_heat | 400 | 145 | 422 |
| exposure | endocrine_disruptors | 402 | 166 | 416 |
| exposure | green_blue_space | 302 | 34 | 302 |
| exposure | indoor_air | 191 | 189 | 276 |
| exposure | lead | 219 | 211 | 249 |
| exposure | drinking_water_contamination | 63 | 189 | 209 |
| exposure | noise | 127 | 91 | 137 |
| exposure | wildfire_smoke | 100 | 75 | 126 |
| exposure | structural_racism | 111 | 26 | 120 |
| exposure | violence_crime | 50 | 46 | 75 |
| outcome | mortality | 2731 | 232 | 2748 |
| outcome | infection | 1952 | 742 | 1973 |
| outcome | healthcare_utilization | 1621 | 837 | 1778 |
| outcome | mental_health | 1072 | 360 | 1161 |
| outcome | childhood_cancer | 600 | 857 | 1083 |
| outcome | obesity_adiposity | 980 | 309 | 998 |
| outcome | preterm_birth | 840 | 536 | 956 |
| outcome | cardiometabolic | 904 | 350 | 931 |
| outcome | fetal_growth | 840 | 566 | 919 |
| outcome | neurodevelopment | 719 | 297 | 809 |
| outcome | asthma_wheeze | 756 | 483 | 766 |
| outcome | congenital_anomalies | 455 | 284 | 516 |
| outcome | vaccine_adverse_event | 400 | 212 | 466 |
| outcome | pregnancy_complications | 393 | 235 | 419 |
| outcome | type1_diabetes_autoimmunity | 342 | 226 | 371 |
| outcome | adhd_autism | 339 | 223 | 343 |
| outcome | allergic_disease | 213 | 84 | 219 |
| outcome | lung_function | 161 | 75 | 169 |
| outcome | puberty_growth | 71 | 20 | 74 |
| population | adult | 2176 | 6668 | 7290 |
| population | child | 3786 | 3122 | 4356 |
| population | prenatal_window | 3507 | 3003 | 3697 |
| population | infant | 1612 | 2858 | 3236 |
| population | adolescent | 578 | 2456 | 2629 |

## Age strata

Articles by life stage, from either tag path. Age is a tag, not a filter: the pediatric corpus is the rows with any of prenatal, infant, child or adolescent.

| stratum | n_articles |
| --- | --- |
| prenatal | 3697 |
| infant | 3236 |
| child | 4356 |
| adolescent | 2629 |
| adult | 7290 |
| pediatric | 6951 |
| no age tag | 5114 |
| all articles | 15913 |

## Most co-tagged exposure-outcome pairs

Articles tagged with both an exposure and an outcome facet. A co-tag says where the literature is, not what it found; the quoted links do that.

| exposure | outcome | n_articles |
| --- | --- | --- |
| vaccination | infection | 814 |
| individual_social_factors | mortality | 761 |
| individual_social_factors | healthcare_utilization | 746 |
| neighborhood_deprivation | mortality | 635 |
| individual_social_factors | mental_health | 452 |
| vaccination | vaccine_adverse_event | 417 |
| individual_social_factors | infection | 415 |
| neighborhood_deprivation | healthcare_utilization | 407 |
| prenatal_medication | fetal_growth | 367 |
| prenatal_medication | preterm_birth | 359 |
| particulate_matter | mortality | 350 |
| prenatal_medication | neurodevelopment | 312 |
| individual_social_factors | cardiometabolic | 296 |
| built_environment | mortality | 263 |
| individual_social_factors | obesity_adiposity | 255 |
| neighborhood_deprivation | infection | 255 |
| vaccination | healthcare_utilization | 244 |
| neighborhood_deprivation | obesity_adiposity | 227 |
| prenatal_medication | mental_health | 222 |
| particulate_matter | healthcare_utilization | 219 |
| vaccination | mortality | 214 |
| neighborhood_deprivation | mental_health | 212 |
| prenatal_medication | adhd_autism | 209 |
| prenatal_medication | congenital_anomalies | 206 |
| prenatal_medication | obesity_adiposity | 195 |

## Declared MeSH descriptors

| status | n_descriptors |
| --- | --- |
| real descriptor, used in this corpus | 201 |
| real descriptor, unused in this corpus | 0 |
| not recognised by PubMed | 0 |

## Ontology graph

| predicate | n_edges | transitive | subsumption |
| --- | --- | --- | --- |
| is_a | 87 | yes | yes |
| risk_factor_for | 0 | no | no |
| protective_for | 0 | no | no |
| no_association_with | 0 | no | no |
| assessed_by | 0 | no | no |
| ascertained_from | 0 | no | no |
| linked_with | 0 | no | no |

Nodes: 88. Edges: 87. Closure rows: 221.

## OMOP concept resolution

| resolver | status | n_attempts |
| --- | --- | --- |
| athena_api | error | 1 |
| nlm_clinical_tables | ok | 7 |
| ols4 | no_match | 20 |
| ols4 | ok | 62 |
| rxnav | no_match | 2 |
| vocabulary_id | n_candidates | n_facets | n_with_concept_id |
| --- | --- | --- | --- |
| SNOMED | 148 | 42 | 0 |
| LOINC | 20 | 6 | 0 |
| NCIt | 13 | 5 | 0 |

### Match quality of candidate mappings

Token-based: `exact` means the same set of meaningful words once case, punctuation, SNOMED's semantic tag, LOINC's unit brackets and British spellings are normalised away. `contains` means every word of the search term appears in the concept name, usually a more specifically named form of the same thing. `loose` means at least one word is missing, so the service matched on something else.

| label_match | n_candidates | n_rank_1 |
| --- | --- | --- |
| contains | 109 | 25 |
| exact | 39 | 38 |
| loose | 33 | 6 |

6 best-candidate rows are loose matches and should be reviewed first. They are exported to `data/derived/mapping_review_priority.tsv`.

| facet_id | vocabulary | search_term | concept_code | concept_name |
| --- | --- | --- | --- | --- |
| mixture_methods | NCIt | statistical method | C19044 | Statistical Technique |
| particulate_matter | SNOMED | exposure to air pollution | 102424008 | Exposure to polluted air |
| questionnaire | SNOMED | questionnaire | 763111006 | Assessment using ICIQ-UI-SF (International Consultation on Incontinence Questionnaire-Urinary Incontinence-Short Form) |
| residential_geocoding | SNOMED | geographic location | 758638001 | Geographical location |
| tobacco_smoke | LOINC | cotinine | 35140-3 | Trans-3-Hydroxycotinine [Mass/volume] in Serum or Plasma |
| type1_diabetes_autoimmunity | SNOMED | coeliac disease | 396331005 | Celiac disease (disorder) |

181 candidate mappings are `unreviewed`. None of them should be treated as a phenotype definition until a human has checked them against ATHENA.

Facets with no candidate concept: biomonitoring, drinking_water_contamination, ehr, endocrine_disruptors, exposome_wide_design, indoor_air, monitoring_station, neighborhood_deprivation, ozone, persistent_organic_pollutants, personal_sensor, pesticides, prenatal_medication, record_linkage, research_cohort, spatial_model, structural_racism, temperature_heat, violence_crime

## Failing checks

- **every facet has at least one candidate concept**: 48 of 67 facets mapped, 181 candidate rows
- **no undeclared concept-resolution misses**: 0 declared fallback gaps, undeclared: [('biomonitoring', 'biological monitoring'), ('drinking_water_contamination', 'contaminated drinking water'), ('ehr', 'electronic health record'), ('endocrine_disruptors', 'exposure to endocrine disrupting chemical'), ('exposome_wide_design', 'exposome')]
