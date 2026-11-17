"""suggestions.derive: quick replies from the agent's own question, no
model call. Narrow on purpose: an open question must give nothing."""

import suggestions


def test_either_or_question_gives_both_sides():
    assert suggestions.derive("Would you prefer morning or afternoon?") == ["Morning", "Afternoon"]
    assert suggestions.derive("Sure. Pickup or delivery?") == ["Pickup", "Delivery"]


def test_offer_gives_yes_please_and_not_now():
    assert suggestions.derive("I found a 10:30 slot. Would you like me to book that?") == ["Yes, please", "Not now"]
    assert suggestions.derive("Shall I send the itinerary to your email?") == ["Yes, please", "Not now"]


def test_plain_yes_no_question():
    assert suggestions.derive("Is the delivery address still the same?") == ["Yes", "No"]
    assert suggestions.derive("Do you have your order number handy?") == ["Yes", "No"]


def test_open_questions_give_nothing():
    assert suggestions.derive("What would you like to do today?") == []
    assert suggestions.derive("Which day works for you?") == []
    assert suggestions.derive("Where are you flying from?") == []


def test_statements_give_nothing():
    assert suggestions.derive("Your order shipped yesterday.") == []
    assert suggestions.derive("") == []
    assert suggestions.derive(None) == []


def test_only_the_last_sentence_counts():
    assert suggestions.derive("Is that right? Great, your table is booked.") == []
    assert suggestions.derive("Your table is booked. Is there anything else?") == ["Yes", "No"]


def test_long_alternatives_give_nothing_not_yes_no():
    # "X or Y?" with long sides: no chips at all, and never a Yes/No pair.
    assert suggestions.derive(
        "Would you prefer the early morning express service or the slower overnight sleeper train?"
    ) == []


def test_leading_function_words_are_stripped_from_sides():
    assert suggestions.derive("Is it for today or tomorrow?") == ["Today", "Tomorrow"]
    assert suggestions.derive("Do you want it delivered or will you collect?") == ["Delivered", "Collect"]


def test_openers_are_cleaned_and_capped():
    assert suggestions.clean_openers(["Book", " book ", "", "Opening hours", "A", "B", "C"]) == [
        "Book", "Opening hours", "A", "B",
    ]
    assert suggestions.clean_openers(None) == []
