"""The KVM layout drawn as monitors (app/kvm_layout.py). The monitors are
the ones the tray apps reported on the desk this was built for."""

from __future__ import annotations

import pytest

from app import kvm_layout

SAMSUNG = {"id": "SAM-0F1D", "name": "C34J79x", "x": 0, "y": 0, "width": 3440, "height": 1440}
BENQ_R = {"id": "BNQ-7F1C", "name": "BenQ RL2455", "x": 3440, "y": 356, "width": 1920, "height": 1080}
MSI = {"id": "MSI-3CD9", "name": "MAG 27CQ6F", "x": 0, "y": 0, "width": 1707, "height": 960}
BENQ_DEV = {"id": "BNQ-78A7", "name": "BenQ GL2450H", "x": 0, "y": 0, "width": 1920, "height": 1080}


def group(x, y, *monitors, scale=1.0, size="os"):
    return {"x": x, "y": y, "scale": scale, "size": size, "monitors": list(monitors)}


def desk(**over):
    """sage's two monitors above sagedeck (left) and sage-dev (right)."""
    groups = {
        "sage": group(0, 0, SAMSUNG, BENQ_R),
        "sagedeck": group(0, 1440, MSI),
        "sage-dev": group(3440, 1440, BENQ_DEV),
    }
    groups.update(over)
    return groups


def test_the_desk_splits_sages_bottom_edge_between_the_two_below():
    result = kvm_layout.analyse(desk())
    assert result["errors"] == []
    assert result["links"] == {
        "sage": ["down(0,31.85) = sagedeck", "down(64.18,100) = sage-dev"],
        "sagedeck": ["up = sage(0,31.85)"],
        "sage-dev": ["up = sage(64.18,100)"],
    }


def test_the_benq_ending_4px_short_still_counts_as_the_outline():
    # Its bottom is at 1436, sage's outline at 1440: no warning, no corner link.
    result = kvm_layout.analyse(desk())
    assert result["warnings"] == []
    assert all(e["outer"] for e in result["edges"])


def test_a_computer_under_both_monitors_gets_one_continuous_range():
    wide = {**MSI, "width": 5360}
    result = kvm_layout.analyse({"sage": group(0, 0, SAMSUNG, BENQ_R), "tv": group(0, 1440, wide)})
    assert result["links"] == {"sage": ["down = tv"], "tv": ["up = sage"]}


def test_scale_changes_where_the_split_lands():
    # sagedeck drawn at real size: its 1707 px are 2560.5 canvas units wide.
    result = kvm_layout.analyse(desk(sagedeck=group(0, 1440, MSI, scale=1.5, size="physical")))
    assert result["links"]["sage"][0] == "down(0,47.77) = sagedeck"


def test_offset_groups_map_the_shared_stretch_on_both_sides():
    # sage-dev half a screen lower than sage's right monitor, to its right.
    result = kvm_layout.analyse({"sage": group(0, 0, SAMSUNG, BENQ_R), "sage-dev": group(5360, 896, BENQ_DEV)})
    assert result["links"] == {"sage": ["right(62.22,99.72) = sage-dev(0,50)"],
                               "sage-dev": ["left(0,50) = sage(62.22,99.72)"]}


def test_an_inner_monitor_edge_links_with_a_warning():
    # An L: sage's BenQ sits lower, so sagedeck under the Samsung touches an
    # edge that isn't sage's outline when the BenQ hangs below it.
    low = {**BENQ_R, "y": 900}
    result = kvm_layout.analyse({"sage": group(0, 0, SAMSUNG, low), "sagedeck": group(0, 1440, MSI)})
    assert result["links"]["sage"] == ["down(0,31.85) = sagedeck"]
    assert any("outline" in w for w in result["warnings"])


def test_overlapping_computers_are_an_error():
    result = kvm_layout.analyse(desk(sagedeck=group(100, 1000, MSI)))
    assert result["errors"] == ["sage and sagedeck overlap. Move one so they only touch."]


def test_a_computer_nothing_touches_is_warned_about():
    result = kvm_layout.analyse(desk(**{"sage-dev": group(9000, 9000, BENQ_DEV)}))
    assert "Nothing touches sage-dev: the mouse can't reach it, only hotkeys can." in result["warnings"]


def test_a_corner_is_not_an_edge():
    result = kvm_layout.analyse({"a": group(0, 0, BENQ_DEV), "b": group(1920, 1075, BENQ_DEV)})
    assert result["links"] == {}


def test_groups_are_checked():
    names = {"sage", "sagedeck", "sage-dev"}
    assert kvm_layout.check_groups(desk(), names)["sage"]["monitors"][1]["y"] == 356
    with pytest.raises(kvm_layout.LayoutError, match="Unknown computer"):
        kvm_layout.check_groups({"nope": group(0, 0, MSI)}, names)
    with pytest.raises(kvm_layout.LayoutError, match="needs its monitors"):
        kvm_layout.check_groups({"sage": group(0, 0)}, names)
    with pytest.raises(kvm_layout.LayoutError, match="size"):
        kvm_layout.check_groups({"sage": group(0, 0, MSI, scale=0)}, names)
    with pytest.raises(kvm_layout.LayoutError, match="size setting"):
        kvm_layout.check_groups({"sage": group(0, 0, MSI, size="huge")}, names)


def test_with_monitor_shapes_an_inner_edge_is_no_warning():
    low = {**BENQ_R, "y": 900}
    groups = {"sage": group(0, 0, SAMSUNG, low), "sagedeck": group(0, 1440, MSI)}
    result = kvm_layout.analyse(groups, shapes=True)
    assert result["links"]["sage"] == ["down(0,31.85) = sagedeck"]
    assert not any("outline" in w for w in result["warnings"])
    assert kvm_layout.monitor_shapes(groups) == {"sage": "0,0,64.1791,72.7273 64.1791,45.4545,35.8209,54.5455"}
