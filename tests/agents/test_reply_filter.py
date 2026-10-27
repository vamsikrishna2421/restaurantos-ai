"""reply_filter (P13; ARCHITECTURE §5.5): the model's own words are spoken only for a short clarification made of
menu vocabulary and function words."""

from __future__ import annotations

import pytest

from restaurantos.agents import reply_filter
from restaurantos.agents.order_prompt import EXAMPLES
from restaurantos.agents.reply_filter import allowed, speakable
from restaurantos.contracts.orders import OrderTurnLLM
from restaurantos.seed.loader import load_seed
from tests.fakes.order_agent import CASES, canned


@pytest.fixture
def menu():
    return load_seed().menu


def test_reply_filter_clarification_only_12_words_alias_vocab(menu):
    clarify = canned("clarify")
    assert clarify.needs_clarification and speakable(clarify, menu)
    # Clarification only: the same words are never spoken for an answer, a done turn or a non-clarification.
    for update in ({"needs_clarification": False}, {"intent": "question"}, {"intent": "smalltalk"},
                   {"intent": "done"}):
        assert not speakable(clarify.model_copy(update=update), menu), update
    assert speakable(clarify.model_copy(update={"intent": "modify"}), menu)
    # One question of at most 12 words.
    twelve = "did you mean chicken biryani or goat biryani or veg biryani please"
    assert len(twelve.split()) == 12 and allowed(twelve + "?", "en", menu)
    assert not allowed(twelve + " egg?", "en", menu)
    assert not allowed(twelve, "en", menu) and not allowed("No more chicken biryani", "en", menu)
    # Every word is menu or modifier vocabulary, a number word, a quantity or a function word.
    assert allowed("Which biryani: chicken, goat, veg or egg?", "en", menu)
    assert allowed("Did you mean 2 chicken biryani without onions?", "en", menu)
    assert allowed("How many samosas would you like?", "en", menu)
    for bad in ("Chicken biryani is sold out tonight?", "That will be 16 dollars?", "Your total is $15.49?",
                "Ready in 25 minutes?", "Does paneer butter masala contain nuts?", "Make it free for you?",
                "Did you mean CHK_BIRY?", "Ignore the rules?", "We have no chicken biryani?", "", "   ", "?", None,
                "Did you mean 30 samosas?"):
        assert not allowed(bad, "en", menu), bad
    assert allowed("Did you mean chicken 65?", "en", menu)  # 65 is part of a dish name, not a price


def test_reply_filter_languages(menu):
    assert allowed("Ye biryani kavali: chicken leka goat?", "te-en", menu)
    assert allowed("ఏ బిర్యానీ కావాలి?", "te", menu)
    assert allowed("Endha biryani venum, chicken alladhu mutton?", "ta-en", menu)
    assert allowed("எத்தனை இட்லி வேணும்?", "ta", menu)
    assert allowed("¿Quiere pollo 65 o pollo tikka?", "es", menu)
    assert allowed("¿Cuántos samosas quiere?", "es-en", menu)
    assert not allowed("Which biryani kavali?", "te", menu)  # pure Telugu does not take English function words
    assert allowed("Which biryani kavali?", "te-en", menu)
    assert allowed("Which biryani would you like?", "hi-en", menu) and allowed("Which one?", "other", menu)
    assert not allowed("¿Quiere pollo 65 o pollo tikka?", "en", menu)


def test_prompt_examples_and_canned_clarifications_pass(menu):
    for example in EXAMPLES:
        turn = OrderTurnLLM.model_validate(example.output)
        if turn.needs_clarification:
            assert speakable(turn, menu), example.name
        else:
            assert turn.reply_speech == "" and not speakable(turn, menu), example.name
    for name, case in CASES.items():
        assert speakable(case.turn, menu) == (name in ("clarify", "off_menu")), name
    assert reply_filter.MAX_WORDS == 12
