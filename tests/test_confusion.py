"""How likely one code is to have been read as another.

This table sat in the build for a while doing nothing: no case ever produced a
misread, so nothing exercised it and nothing would have noticed if it were
wrong. These tests pin the behaviour down directly.
"""

from __future__ import annotations

import pytest

from agent.confusion import (
    P_CORRECT,
    P_LOOKALIKE,
    P_UNRELATED,
    character_prob,
    looks_like,
    misread_prob,
)
from common.coding import label_code


def test_the_probabilities_are_ordered():
    assert P_CORRECT > P_LOOKALIKE > P_UNRELATED > 0


@pytest.mark.parametrize(("a", "b"), [("1", "7"), ("0", "O"), ("5", "S"), ("8", "B")])
def test_shapes_that_look_alike(a, b):
    assert looks_like(a, b) and looks_like(b, a)


@pytest.mark.parametrize(("a", "b"), [("0", "8"), ("3", "8"), ("5", "6"), ("8", "9")])
def test_digits_that_ink_damage_bridges(a, b):
    """A blot fills the hole in a 0 or closes the open side of a 3, 5, 6 or 9."""
    assert looks_like(a, b) and looks_like(b, a)


@pytest.mark.parametrize(("a", "b"), [("1", "4"), ("2", "7"), ("0", "1"), ("4", "9")])
def test_digits_that_are_not_confusable(a, b):
    assert not looks_like(a, b)


def test_a_character_read_correctly_scores_highest():
    assert character_prob("3", "3") > character_prob("3", "8") > character_prob("3", "4")


def test_an_exact_reading_is_the_most_likely_reading():
    code = label_code("B-2290")
    assert misread_prob(code, code) == pytest.approx(P_CORRECT ** len(code))


def test_a_lookalike_substitution_beats_an_unrelated_one_by_a_wide_margin():
    """This ratio is the whole point of the table over plain edit distance."""
    truth = label_code("B-2290")  # B-2290-5
    lookalike = misread_prob(truth, "B-2298-5")  # 0 -> 8, ink can do that
    unrelated = misread_prob(truth, "B-2294-5")  # 0 -> 4, ink cannot
    assert lookalike / unrelated == pytest.approx(P_LOOKALIKE / P_UNRELATED, rel=0.01)
    assert lookalike / unrelated > 30


def test_more_wrong_characters_means_a_less_likely_misread():
    truth = label_code("B-2290")
    one_off = misread_prob(truth, "B-2298-5")
    two_off = misread_prob(truth, "B-2298-6")
    assert one_off > two_off


def test_a_partial_reading_only_scores_what_was_read():
    """The characters a tear removed carry no information either way."""
    truth = label_code("B-2290")
    assert misread_prob(truth, "B-229") == pytest.approx(P_CORRECT**5)


def test_a_reading_longer_than_the_code_is_impossible():
    assert misread_prob("B-2290-5", "B-2290-55") == 0.0


def test_an_empty_reading_says_nothing():
    assert misread_prob("B-2290-5", "") == 1.0


def test_the_table_picks_the_right_code_on_the_ink_blot_case():
    """S8 in one assertion: a blotted 0 reads as 8, and only one real code fits."""
    read = "B-2298-5"
    scores = {b: misread_prob(label_code(b), read) for b in ["B-2288", "B-2290", "B-2291"]}
    best = max(scores, key=lambda k: scores[k])
    assert best == "B-2290"
    runner_up = sorted(scores.values())[-2]
    assert scores[best] / runner_up > 100
