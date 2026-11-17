"""tools.choice_group: the option group behind ask_choice, the "choose one
of the following" card. Pure, so it is tested without a LiveKit session."""

import pytest

import tools


def test_group_shape_and_pick_text():
    g = tools.choice_group("Which slot?", ["10:30 | 30 minutes", "2:00 pm", "4:15 pm"], False)
    assert g["kind"] == "choice" and g["always"] is True and g["multi"] is False
    assert g["title"] == "Which slot?"
    assert [i["n"] for i in g["items"]] == [1, 2, 3]
    assert g["items"][0]["label"] == "10:30"
    assert g["items"][0]["detail"] == "30 minutes"
    assert g["items"][1]["detail"] is None
    # A tap sends the label itself, never "Option N".
    assert all(i["pick"] == i["label"] for i in g["items"])


def test_too_few_or_too_many_choices_are_refused():
    with pytest.raises(ValueError):
        tools.choice_group("One?", ["only"], False)
    with pytest.raises(ValueError):
        tools.choice_group("Six?", ["a", "b", "c", "d", "e", "f"], False)


def test_blank_and_duplicate_choices_are_dropped():
    g = tools.choice_group("Day?", ["Monday", "", "monday", "Tuesday"], True)
    assert [i["label"] for i in g["items"]] == ["Monday", "Tuesday"]
    assert g["multi"] is True
