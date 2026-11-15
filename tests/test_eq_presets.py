"""EQ presets for the HyperX headset: Bragi's built-in ones and presets
people save from the dialog, shared by every browser through data/."""

from __future__ import annotations

import pytest

from app import eq_presets, headset_hid


def names(presets: list[dict]) -> list[str]:
    return [p["name"] for p in presets]


def test_the_built_ins_are_the_researched_set_in_order():
    """docs/eq-presets.md has the basis for each."""
    presets = eq_presets.list_all()

    assert names(presets) == ["Flat", "Balanced (measured)", "Bass boost", "Less bass", "Voice clarity",
                              "Footsteps", "Music", "Movies", "Late night", "Smooth", "Brighter"]
    assert all(p["builtin"] for p in presets)
    assert presets[0]["gains"] == [0] * 10


def test_the_measured_preset_has_the_fitted_gains():
    """From the fit to SoundGuys' Cloud III S measurement (docs/eq-presets.md)."""
    assert eq_presets.gains_for("Balanced (measured)") == [2.7, 1.1, -0.4, 2.3, -1.0, -0.2, -0.9, 2.8, -1.6, 0.0]


def test_every_built_in_has_ten_bands_within_six_db_and_a_description():
    """+/-6 dB is the research's budget for presets: the dongle has no
    preamp, and nothing above about +6 dB has been tried on it."""
    for preset in eq_presets.list_all():
        assert len(preset["gains"]) == len(headset_hid.EQ_BANDS_HZ), preset["name"]
        assert all(abs(g) <= 6 for g in preset["gains"]), preset["name"]
        assert preset["description"], preset["name"]


def test_a_saved_preset_is_listed_after_the_built_ins_and_survives_a_restart():
    eq_presets.save("My bass", [3, 3, 2, 0, 0, 0, 0, 0, 0, 0])

    presets = eq_presets.list_all()
    assert presets[-1] == {"name": "My bass", "description": "", "gains": [3, 3, 2, 0, 0, 0, 0, 0, 0, 0],
                           "builtin": False}
    assert eq_presets.FILE.exists()


def test_saving_under_an_existing_custom_name_replaces_it():
    eq_presets.save("Mine", [1] * 10)
    eq_presets.save(" mine ", [2] * 10)

    customs = [p for p in eq_presets.list_all() if not p["builtin"]]
    assert customs == [{"name": "mine", "description": "", "gains": [2] * 10, "builtin": False}]


@pytest.mark.parametrize("name", ["", "   ", "flat", "Bass Boost", "smooth", "x" * 41, None, 5])
def test_a_preset_name_must_be_new_and_short(name):
    with pytest.raises(ValueError):
        eq_presets.save(name, [0] * 10)


def test_a_preset_with_a_bad_eq_is_refused():
    with pytest.raises(ValueError):
        eq_presets.save("Loud", [20] * 10)
    assert names(eq_presets.list_all())[-1] != "Loud"


def test_a_custom_preset_can_be_deleted_but_a_built_in_cannot():
    eq_presets.save("Mine", [1] * 10)

    eq_presets.delete("Mine")
    assert "Mine" not in names(eq_presets.list_all())

    with pytest.raises(ValueError):
        eq_presets.delete("Flat")
    with pytest.raises(ValueError):
        eq_presets.delete("Never saved")


def test_the_active_preset_is_the_one_whose_gains_match():
    """Worked out from the gains rather than remembered, so every tab
    agrees, and moving one slider makes it custom."""
    bass = next(p for p in eq_presets.list_all() if p["name"] == "Bass boost")

    assert eq_presets.matching(bass["gains"]) == "Bass boost"
    assert eq_presets.matching([0] * 10) == "Flat"
    assert eq_presets.matching([bass["gains"][0] - 1] + bass["gains"][1:]) is None
    assert eq_presets.matching(None) is None


def test_a_saved_preset_is_matched_too():
    eq_presets.save("Mine", [1.5] * 10)

    assert eq_presets.matching([1.5] * 10) == "Mine"


def test_gains_are_found_by_name_whatever_the_case():
    assert eq_presets.gains_for("bass BOOST") == next(
        p["gains"] for p in eq_presets.list_all() if p["name"] == "Bass boost")
    with pytest.raises(ValueError):
        eq_presets.gains_for("Nope")
