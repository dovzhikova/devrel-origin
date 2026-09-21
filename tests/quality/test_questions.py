from devrel_origin.quality import questions as q


def test_relation_offers_exactly_the_three_cookbook_outcomes():
    assert set(q.RELATION.criteria) == {"supports", "contradicts", "says_nothing"}
    assert all(v.strip() for v in q.RELATION.criteria.values())


def test_pattern_question_includes_a_no_match_outcome():
    # The model cannot report "clean" unless a no-match option exists.
    assert q.PATTERN_NONE in q.PATTERN.criteria
    assert len(q.PATTERN.criteria) > 5


def test_every_criterion_is_a_concrete_definition_not_a_bare_label():
    for spec in (q.RELATION, q.PATTERN):
        for name, definition in spec.criteria.items():
            assert len(definition.split()) >= 4, name


def test_binary_contrast_excludes_informative_contrast():
    # v2 wording: an honest technical distinction is not the rhetorical device.
    definition = q.PATTERN.criteria["binary_contrast"]
    assert "straw man" in definition
    assert "Not this pattern" in definition


def test_question_version_is_3_for_per_pattern_nouls():
    assert q.QUESTION_VERSION == 3


def test_pattern_nouls_covers_every_pattern_except_none():
    assert set(q.PATTERN_NOULS) == set(q.PATTERN.criteria) - {q.PATTERN_NONE}


def test_pattern_nouls_instructions_ask_about_unit_and_keep_the_definition():
    for key, spec in q.PATTERN_NOULS.items():
        definition = q.PATTERN.criteria[key]
        assert spec.instructions == f"Does `unit` exhibit this writing pattern? {definition}"


def test_pattern_thresholds_covers_every_pattern_noul():
    # A pattern with no threshold could never be a hit; a threshold with no
    # pattern is dead weight. The two dicts describe the same set.
    assert set(q.PATTERN_THRESHOLDS) == set(q.PATTERN_NOULS)


def test_pattern_thresholds_match_the_fitted_calibration_file():
    # Values fixed 2026-09-21 against evidence-verify-typesafe/calibration/
    # thresholds.json (sha256 prefix 3b4ba9b2). Shipped by owner override
    # after a failed held-out test; see task-6d-brief.md.
    assert q.PATTERN_THRESHOLDS == {
        "binary_contrast": 0.30,
        "throat_clearing": 0.50,
        "faux_insight": 0.30,
        "colon_reveal": 0.45,
        "importance_puffery": 0.90,
        "weasel_attribution": 0.65,
        "metadiscourse": 0.55,
        "fake_profound_kicker": 0.75,
        "summary_recap": 0.75,
        "superficial_analysis": 0.75,
    }


def test_questions_module_does_not_import_the_optional_sdk():
    src = q.__file__ or ""
    assert src.endswith("questions.py")
    with open(src, encoding="utf-8") as fh:
        assert "typesafe_sdk" not in fh.read()
