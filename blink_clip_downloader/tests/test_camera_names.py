"""Which camera a name belongs to after renames."""

from __future__ import annotations

import pytest

from blink_downloader.camera_names import current_camera_name

ALIASES = {"Front Door": "Entryway", "Garage": "Workshop"}


def test_a_name_that_was_never_renamed_is_itself() -> None:
    assert current_camera_name("Driveway", ALIASES) == "Driveway"


def test_a_renamed_camera_is_found_under_its_new_name() -> None:
    assert current_camera_name("Front Door", ALIASES) == "Entryway"


def test_names_are_matched_without_regard_to_capitals() -> None:
    assert current_camera_name("front DOOR", ALIASES) == "Entryway"
    assert current_camera_name("front door", {"FRONT DOOR": "Entryway"}) == "Entryway"


def test_a_rename_chain_is_followed_to_the_end() -> None:
    chain = {"A": "B", "b": "C", "C": "D"}
    assert current_camera_name("A", chain) == "D"
    assert current_camera_name("B", chain) == "D"


def test_a_name_a_camera_carries_today_is_that_camera() -> None:
    """A name can be taken again after its camera was renamed away: the alias
    is then stale, and the new camera's learning must not move to the old one's
    successor."""
    assert (
        current_camera_name("Front Door", ALIASES, live=["front door"]) == "Front Door"
    )


def test_the_chain_stops_at_the_first_name_that_is_live() -> None:
    chain = {"A": "B", "B": "C"}
    assert current_camera_name("A", chain, live=["B"]) == "B"
    assert current_camera_name("A", chain, live=["C"]) == "C"
    assert current_camera_name("A", chain, live=["Other"]) == "C"


def test_an_empty_live_list_is_a_startup_window_not_no_cameras() -> None:
    assert current_camera_name("Garage", ALIASES, live=[]) == "Workshop"
    assert current_camera_name("Garage", ALIASES) == "Workshop"


@pytest.mark.parametrize(
    ("aliases", "start"),
    [({"A": "B", "B": "A"}, "A"), ({"A": "A"}, "A")],
)
def test_a_loop_ends_instead_of_spinning(aliases: dict[str, str], start: str) -> None:
    assert current_camera_name(start, aliases) == "A"


def test_a_change_of_capitals_ends_on_the_new_spelling() -> None:
    assert (
        current_camera_name("Front door", {"Front door": "Front Door"}) == "Front Door"
    )


def test_blank_aliases_are_ignored() -> None:
    assert current_camera_name("A", {"": "B", "A": ""}) == "A"
