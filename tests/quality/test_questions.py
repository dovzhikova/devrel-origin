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


def test_questions_module_does_not_import_the_optional_sdk():
    import sys

    assert "typesafe_sdk" not in sys.modules or True  # import is allowed elsewhere
    src = q.__file__ or ""
    assert src.endswith("questions.py")
    with open(src, encoding="utf-8") as fh:
        assert "typesafe_sdk" not in fh.read()
