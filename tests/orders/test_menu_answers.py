"""Answers decided in code (``orders.menu_answers``): a menu question matched against the Spice Route menu (no model
call), the answer templates it picks, and the words that pick ``complaint_ack`` and ``not_on_menu``."""

from __future__ import annotations

import pytest

from restaurantos.contracts.menu import Availability
from restaurantos.contracts.orders import ApplyResult, OrderQuestion
from restaurantos.orders import menu_answers
from restaurantos.orders.menu_answers import MenuMatch, Sections, complaint, match, off_menu
from restaurantos.orders.templates import Templates, load_phrase_files
from tests.fakes.order_agent import add_op, make_cart, make_turn, seed_menu

MENU = seed_menu()
TEMPLATES = Templates.load(defaults={"url": "restaurantos.fly.dev"})
MENU_Q = OrderQuestion(topic="menu")
SECTIONS = ("biryani", "curry", "starter", "tiffin", "bread", "rice", "drink", "dessert")  # in menu order
DRINKS = ("MANGO_LASSI", "MASALA_CHAI", "FILTER_COFFEE", "WATER")


def ask(utterance: str, availability: Availability | None = None, **kw) -> MenuMatch:
    return match(kw.pop("question", MENU_Q), utterance, MENU, availability or Availability(), **kw)


@pytest.mark.parametrize(("utterance", "expected"), [
    ("What types do you have in dosa?", MenuMatch(codes=("MASALA_DOSA", "PESARATTU"))),
    ("Actually I am thinking of ordering a biryani, what veg biryanis do you have?", MenuMatch(codes=("VEG_BIRY",))),
    ("what chicken biryanis", MenuMatch(codes=("CHK_BIRY",))),
    ("Can you give me the whole menu?", MenuMatch(sections=SECTIONS)),  # the overview
    ("do you have pizza", MenuMatch(sections=SECTIONS)),  # an unknown word is ignored
    ("what drinks do you have", MenuMatch(codes=DRINKS)),
    ("what's vegetarian", MenuMatch(sections=SECTIONS)),  # 22 dishes: the sections holding them
])
def test_the_calls_questions(utterance: str, expected: MenuMatch) -> None:
    assert ask(utterance) == expected


def test_vocabulary_plurals_synonyms_and_filters() -> None:
    assert ask("any appetizers?").codes == ("CHK_65", "GOBI_MAN", "PNR_TIKKA", "CHK_TIKKA", "SAMOSA")
    assert ask("which beverages").codes == DRINKS and ask("sweets?").codes == ("GULAB_JAMUN",)
    assert ask("breakfast").codes == ("MASALA_DOSA", "IDLI", "SAMBAR_VADA", "PESARATTU")
    assert len(ask("what curries").codes) == 8  # "curries" → curry; at most MAX_LISTED dishes are named
    assert ask("veg curries").codes == ("PNR_BM", "PALAK_PNR", "VEG_KORMA", "DAL_TADKA", "CHANA_MASALA")
    assert ask("any non-veg starters").codes == ("CHK_65", "CHK_TIKKA")
    assert ask("nonveg curries").codes == ("CHK_TIKKA_MASALA", "GONGURA_MUTTON", "CHK_CHETTINAD")
    assert ask("meat biryanis").codes == ("CHK_BIRY", "GOAT_BIRY", "EGG_BIRY")
    assert len(ask("veg and non veg biryanis").codes) == 4  # both filters: none
    assert ask("vegetable korma").codes == ("VEG_KORMA",)  # a filter word never names a dish
    assert len(ask("non vegetarian please").codes) == 8  # exactly MAX_LISTED: still named one by one
    assert ask("do you have tea").codes == ("MASALA_CHAI",)  # an alias
    assert ask("non veg desserts") == MenuMatch()  # nothing fits: menu_none


def test_intersection_else_union_in_menu_order() -> None:
    assert ask("chicken or masala").codes == ("CHK_TIKKA_MASALA",)  # the dishes both words name
    assert ask("drinks and desserts").codes == ("MANGO_LASSI", "MASALA_CHAI", "FILTER_COFFEE", "GULAB_JAMUN", "WATER")
    many = ask("chicken or naan or coffee or tea")  # nothing shared: the union, nine dishes, so their sections
    assert many == MenuMatch(sections=("biryani", "curry", "starter", "bread", "drink"))


def test_item_code_only_without_known_words() -> None:
    idli = OrderQuestion(topic="menu", item_code="IDLI")
    assert ask("tell me about that one", question=idli).codes == ("IDLI",)
    assert ask("what dosas", question=idli).codes == ("MASALA_DOSA", "PESARATTU")  # the words win
    assert ask("anything", question=OrderQuestion(topic="menu", item_code="NOT_A_DISH")).sections == SECTIONS
    assert ask("anything", question=None).sections == SECTIONS


def test_sold_out_dishes_are_never_named() -> None:
    gone = Availability(unavailable={"MASALA_DOSA": "sold_out"})
    assert ask("what dosas", gone).codes == ("PESARATTU",)
    both = Availability(unavailable={"MASALA_DOSA": "sold_out", "PESARATTU": "sold_out"})
    assert ask("what dosas", both) == MenuMatch(sold_out=True)
    dessert = Availability(unavailable={"GULAB_JAMUN": "sold_out"})  # the overview drops an empty section
    assert ask("the whole menu", dessert).sections == SECTIONS[:-1]
    snapshot = MENU.model_copy(update={"items": [i.model_copy(update={"available": i.code != "PESARATTU"})
                                                 for i in MENU.items]})
    assert match(MENU_Q, "dosas", snapshot, Availability()).codes == ("MASALA_DOSA",)  # fails closed, as the guard


def test_words_for_the_dishes_ordered_defer_to_the_question() -> None:
    said = "one masala dosa, and what drinks do you have?"
    assert ask(said).sections == ("curry", "tiffin", "drink")  # without knowing what the turn ordered
    assert ask(said, ordered=["MASALA_DOSA"]).codes == DRINKS
    assert ask("two idlis, what dosas do you have", ordered=["IDLI"]).codes == ("MASALA_DOSA", "PESARATTU")
    # A dish the turn orders is never offered back (a plain "yes" would order it twice).
    assert ask("A mango lassi, and what other drinks do you have?", ordered=["MANGO_LASSI"]).codes == DRINKS[1:]
    assert ask("One chicken 65 and what other starters", ordered=["CHK_65"]).codes == (
        "GOBI_MAN", "PNR_TIKKA", "CHK_TIKKA", "SAMOSA")
    assert ask("One garlic naan, what other breads", ordered=["GARLIC_NAAN"]).codes == ("BUTTER_NAAN", "ROTI")
    assert ask("One masala dosa, what other dosas", ordered=["MASALA_DOSA"]).codes == ("PESARATTU",)  # 2nd "dosa"
    assert ask("Two samosas, and what else do you have?", ordered=["SAMOSA"]).sections == SECTIONS
    assert ask("what dosas do you have", ordered=["MASALA_DOSA"]).sections == SECTIONS  # all about the order
    # A filter word goes with the name it starts, never alone ("veg samosa" is an alias of the samosa).
    assert ask("one samosa, what veg curries", ordered=["SAMOSA"]).codes == ask("veg curries").codes
    assert len(ask("one veg biryani, what chicken dishes", ordered=["VEG_BIRY"]).codes) == 5


@pytest.mark.parametrize(("utterance", "expected"), [
    ("I don't eat meat, what can I have?", MenuMatch(sections=SECTIONS)),  # every section has vegetarian dishes
    ("which curries don't have meat?", MenuMatch(codes=("PNR_BM", "PALAK_PNR", "VEG_KORMA", "DAL_TADKA",
                                                        "CHANA_MASALA"))),
    ("Do you have anything that's not meat? Starters?", MenuMatch(codes=("GOBI_MAN", "PNR_TIKKA", "SAMOSA"))),
    ("anything except biryani?", MenuMatch(sections=SECTIONS[1:])),
    ("not the drinks", MenuMatch(sections=SECTIONS[:-2] + SECTIONS[-1:])),
    ("a biryani without chicken", MenuMatch(codes=("GOAT_BIRY", "VEG_BIRY", "EGG_BIRY"))),
    ("I don't want veg biryani, what other biryanis", MenuMatch(codes=("CHK_BIRY", "GOAT_BIRY", "EGG_BIRY"))),
    ("starters other than chicken tikka", MenuMatch(codes=("CHK_65", "GOBI_MAN", "PNR_TIKKA", "SAMOSA"))),
    ("curries without chicken or meat", MenuMatch(codes=("PNR_BM", "PALAK_PNR", "VEG_KORMA", "DAL_TADKA",
                                                         "CHANA_MASALA"))),
    # A negation never reaches past its clause or a word it does not negate.
    ("No, what drinks do you have?", MenuMatch(codes=DRINKS)),
    ("I'm not sure what veg biryanis you have", MenuMatch(codes=("VEG_BIRY",))),
    ("Don't you have veg biryani?", MenuMatch(codes=("VEG_BIRY",))),
])
def test_negations_leave_dishes_out(utterance: str, expected: MenuMatch) -> None:
    assert ask(utterance) == expected


def test_a_filter_about_one_dish() -> None:
    """"Is it vegetarian?" is about the dish asked; when the filter leaves none of the dishes named, the dishes of
    their sections that pass it are named (never ``menu_none``'s "tonight", which sounds sold out)."""
    veg_starters = ("GOBI_MAN", "PNR_TIKKA", "SAMOSA")
    assert ask("is it vegetarian?", question=OrderQuestion(topic="menu", item_code="CHK_65")).codes == veg_starters
    assert ask("is it vegetarian?", question=OrderQuestion(topic="menu", item_code="VEG_BIRY")).codes == ("VEG_BIRY",)
    assert ask("Is the chicken 65 vegetarian?").codes == veg_starters
    assert ask("veg chicken biryani").codes == ("VEG_BIRY",)
    assert ask("what's vegetarian").sections == SECTIONS  # no dish asked: the whole menu, as the call's spec says


def test_allergy_questions_never_list_dishes() -> None:
    """A menu question about an allergy is answered as an allergen question, with the staff line, never with a list
    of dishes ("dairy-free drinks" would name three drinks with dairy)."""
    def key(utterance, **question):
        return menu_answers.answer(OrderQuestion(topic="menu", **question), MENU, utterance_en=utterance)[0]

    assert key("What dairy-free drinks do you have?", allergen="dairy") == "allergen_which_dish"
    assert key("Which breads don't have gluten?") == "allergen_which_dish"  # the model left the allergen out
    assert key("is the chicken biryani nut free?", item_code="CHK_BIRY") == "allergen_listed"
    assert key("what egg dishes do you have") == "menu_list_one"  # egg is a dish word too
    nuts = make_turn("en", "question", question=OrderQuestion(topic="menu", allergen="tree_nuts"),
                     utterance_en="which curries have no nuts?")
    assert menu_answers.for_turn(nuts, None, MENU, Availability()) is None  # nothing listed, nothing offered
    assert menu_answers.browsing(OrderQuestion(topic="allergen")) and menu_answers.browsing(MENU_Q)
    assert not menu_answers.browsing(OrderQuestion(topic="allergen", item_code="CHK_BIRY"))
    assert not menu_answers.browsing(OrderQuestion(topic="price"))


def test_for_turn_only_for_menu_questions() -> None:
    turn = make_turn("en", "question", [add_op("MASALA_DOSA")], question=MENU_Q,
                     utterance_en="one masala dosa, and what drinks do you have?")
    applied = ApplyResult(cart=make_cart([("MASALA_DOSA", 1, ())]), applied=list(turn.ops))
    assert menu_answers.for_turn(turn, applied, MENU, Availability()).codes == DRINKS  # the dosa was ordered
    price = make_turn("en", "question", question=OrderQuestion(topic="price", item_code="SAMOSA"), utterance_en="?")
    assert menu_answers.for_turn(price, None, MENU, Availability()) is None
    assert menu_answers.for_turn(None, None, MENU, Availability()) is None


def render(found: MenuMatch, lang="en"):
    key, slots = menu_answers.listing(found, MENU)
    return TEMPLATES.draft(key, lang, slots)


def test_menu_answer_templates() -> None:
    one = render(ask("veg biryanis"))
    assert (one.template_key, one.text) == ("menu_list_one", "We have Veg Biryani. Would you like one?")
    dosas = render(ask("dosas"))
    assert (dosas.template_key, dosas.text) == ("menu_list", "We have Masala Dosa and Pesarattu. Which would you like?")
    overview = render(ask("the whole menu"))
    assert overview.template_key == "menu_answer"
    assert overview.text == ("We have biryanis, curries, starters, dosa and idli, breads, rice, drinks and desserts. "
                             "Which would you like to hear about?")
    assert overview.text_en == overview.text
    none = render(MenuMatch(sold_out=True))
    assert (none.template_key, none.text) == ("menu_none", "Sorry, we don't have any of those tonight. What else "
                                                           "would you like?")
    telugu = render(ask("dosas"), "te")
    assert telugu.lang == "te" and "మసాలా దోశ మరియు పెసరట్టు" in telugu.text
    assert telugu.text_en == "We have Masala Dosa and Pesarattu. Which would you like?"  # the caption stays English
    assert "దోశ మరియు ఇడ్లీ" in render(ask("menu"), "te").text


def test_sections_name_categories_per_language() -> None:
    files = load_phrase_files()
    for lang, phrases in files.items():
        assert set(phrases.words.categories) == set(SECTIONS), lang
    sections = Sections(("biryani", "tiffin", "drink"))
    assert sections.speak("es", files["es"].words) == "biryanis, dosa e idli y bebidas"
    assert sections.caption(files["en"].words) == "biryanis, dosa and idli and drinks"
    bare = files["en"].words.model_copy(update={"categories": {}})
    assert Sections(("noodles", "drink")).speak("en", bare) == "noodles and drink"  # no name: the code itself


def test_answers_from_data_keys() -> None:
    def key(topic, item_code=None, **kw):
        return menu_answers.answer(OrderQuestion(topic=topic, item_code=item_code), MENU, **kw)[0]

    assert [key("price", "SAMOSA"), key("spice", "CHK_65"), key("price"), key("allergen")] == [
        "price_answer", "spice_answer", "which_dish", "allergen_which_dish"]
    assert [key("wait_time"), key("hours"), key("other"), key("menu")] == [
        "wait_answer", "hours_answer", "question_staff", "menu_answer"]  # menu without a match: the overview
    assert key("menu", found=MenuMatch(codes=("IDLI",))) == "menu_list_one"


@pytest.mark.parametrize(("said", "complains"), [
    ("I did not like your service.", True), ("I didn't like the service", True), ("I don't like this", True),
    ("this is useless", True), ("you are not understanding me", True), ("so frustrating", True),
    ("I'm not happy with you", True), ("that was terrible", True), ("I hated it", True),
    ("Your service is not good.", True), ("I didn't like it.", True), ("I am not satisfied.", True),  # te/ta, said
    ("not bad", False), ("I don't like onions", False), ("poori please", False), ("thanks, have a good night", False),
    ("my bad", False), ("not too bad", False), ("I hate to bother you", False),
])
def test_complaint_words(said: str, complains: bool) -> None:
    assert complaint(make_turn("en", "smalltalk", utterance_en=said)) is complains
    assert complaint(make_turn("en", "off_topic", utterance_en=said)) is complains


def test_complaints_are_small_talk_without_ops() -> None:
    said = "I did not like your service"
    assert not complaint(make_turn("en", "order", [add_op("SAMOSA")], utterance_en=said))
    assert not complaint(make_turn("en", "smalltalk", [add_op("SAMOSA")], utterance_en=said))
    assert not complaint(make_turn("en", "question", question=MENU_Q, utterance_en=said)) and not complaint(None)


def test_off_menu_needs_a_real_word() -> None:
    assert off_menu("mint") and off_menu("large pepperoni pizza") and off_menu("బిర్యానీ")
    assert off_menu("పిజ్జా") and off_menu("பீட்சா") and off_menu("చీజ్ బర్గర్")  # vowel signs and viramas count
    assert not off_menu("can") and not off_menu("um uh") and not off_menu("") and not off_menu(None)
