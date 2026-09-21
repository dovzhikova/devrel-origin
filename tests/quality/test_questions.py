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


def test_questions_module_does_not_import_the_optional_sdk():
    src = q.__file__ or ""
    assert src.endswith("questions.py")
    with open(src, encoding="utf-8") as fh:
        assert "typesafe_sdk" not in fh.read()
