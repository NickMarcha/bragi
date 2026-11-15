"""The HyperX Cloud III S Wireless controls: battery, charging, mic
monitor, auto-off and EQ over the dongle's vendor HID interface.

Expected bytes come from captures (HeadsetControl issue 486's NGENUITY
logs, and reads taken from the real headset on the Pi), never from the
code under test.
"""

from __future__ import annotations

import pytest

from app import headset_hid

from .fake_hyperx import SERIAL, FakeDongle


@pytest.fixture
def dongle():
    return FakeDongle()


@pytest.fixture
def headset(dongle):
    changes: list[str] = []
    hs = headset_hid.HidHeadset(SERIAL, dongle.write, changes.append)
    dongle.deliver = hs.handle_report
    hs.changes = changes
    return hs


def test_refresh_reads_battery_and_charging(headset, dongle):
    dongle.battery, dongle.charging = 87, 1

    headset.refresh()

    view = headset.view()
    assert view["battery"] == 87
    assert view["charging"] == "charging"
    assert view["headset_on"] is True
    assert dongle.sent(0x06)[0][:6] == bytes.fromhex("0c 02 03 01 00 06")


def test_a_headset_that_is_off_has_no_battery_reading(headset, dongle):
    """With the headset off, the dongle still answers: link 0, and 0xFF for
    anything it would have to ask the headset for."""
    dongle.headset_on = False

    headset.refresh()

    view = headset.view()
    assert view["headset_on"] is False
    assert view["battery"] is None


def test_replies_to_another_programs_reads_are_ignored(headset, dongle):
    """Every reader of the hidraw node sees every reply. One this process
    didn't ask for may answer a question asked at another time."""
    dongle.battery = 40
    dongle.reply_from_elsewhere(0x06)

    assert headset.view()["battery"] is None
    assert headset.changes == []


def test_a_reply_is_used_once(headset, dongle):
    headset.refresh()
    dongle.battery = 3
    dongle.reply_from_elsewhere(0x06)

    assert headset.view()["battery"] == 15


def test_battery_and_charging_notifications_keep_it_live(headset, dongle):
    headset.refresh()
    headset.changes.clear()

    dongle.notify(0x01, 14)  # battery percent
    dongle.notify(0x0A, 1)   # charging

    view = headset.view()
    assert view["battery"] == 14
    assert view["charging"] == "charging"
    assert headset.changes == [SERIAL, SERIAL]


def test_link_notifications_track_the_headset_turning_off(headset, dongle):
    headset.refresh()

    dongle.notify(0x0C, 0)

    assert headset.view()["headset_on"] is False
    assert headset.view()["battery"] is None


def test_the_headset_coming_back_asks_for_a_fresh_read(headset, dongle):
    """A headset switched back on reports its link, not its battery or
    settings, so those have to be read again."""
    dongle.headset_on = False
    headset.refresh()
    assert headset.wants_refresh is False

    dongle.headset_on = True
    dongle.notify(0x0C, 1)

    assert headset.wants_refresh is True
    headset.refresh()
    assert headset.wants_refresh is False
    assert headset.view()["battery"] == 15


def test_refresh_reads_mic_monitor_from_the_headset(headset, dongle):
    dongle.mic_monitor = True

    headset.refresh()

    assert headset.view()["mic_monitor"] is True


def test_turning_mic_monitor_on_writes_it_and_reads_it_back(headset, dongle):
    headset.refresh()

    headset.set_mic_monitor(True)

    assert dongle.sent(0x0D)[0] == bytes.fromhex("0c 02 03 00 00 0d 01").ljust(64, b"\0")
    assert dongle.mic_monitor is True
    assert headset.view()["mic_monitor"] is True


def test_mic_monitor_follows_the_headset_s_own_notification(headset, dongle):
    headset.refresh()

    dongle.notify(0x05, 1)

    assert headset.view()["mic_monitor"] is True


def test_refresh_reads_the_auto_off_timer(headset, dongle):
    """The Pi's headset answered `4b 04 b0`: 1200 seconds, 20 minutes."""
    headset.refresh()

    assert headset.view()["auto_off_minutes"] == 20


def test_setting_auto_off_writes_seconds_big_endian(headset, dongle):
    headset.refresh()

    headset.set_auto_off(10)

    assert dongle.sent(0x4A)[0][:8] == bytes.fromhex("0c 02 03 00 00 4a 02 58")
    assert headset.view()["auto_off_minutes"] == 10


def test_never_is_ngenuity_s_value_not_zero(headset, dongle):
    """NGENUITY's "Never" sends 7A C0 (HeadsetControl issue 486's
    sleeptimer capture). Nothing shows 00 00 disabling it."""
    headset.refresh()

    headset.set_auto_off(None)

    assert dongle.sent(0x4A)[0][:8] == bytes.fromhex("0c 02 03 00 00 4a 7a c0")
    assert headset.view()["auto_off_minutes"] is None
    assert headset.view()["auto_off_known"] is True


@pytest.mark.parametrize("minutes", [0, 7, -5, 2000, "20"])
def test_auto_off_only_takes_the_offered_values(headset, dongle, minutes):
    with pytest.raises(ValueError):
        headset.set_auto_off(minutes)
    assert dongle.sent(0x4A) == []


def eq_packet(band: int, gain_hex: str, commit: bool = False) -> bytes:
    tail = " 01" if commit else ""
    return bytes.fromhex(f"0c 02 03 00 00 5f {band:02x} {gain_hex}{tail}").ljust(64, b"\0")


def test_eq_is_one_report_per_band_with_a_commit_on_the_last(headset, dongle):
    """Gains are signed 16-bit centi-dB. Captured from NGENUITY: +6.09 dB is
    02 61, -1.25 dB is FF 83, and band 9 carries a commit byte."""
    headset.set_eq([6.09, 0, 0, 0, 0, 0, 0, 0, 0, -1.25])

    sent = dongle.sent(0x5F)
    assert len(sent) == 10
    assert sent[0] == eq_packet(0, "02 61")
    assert sent[1] == eq_packet(1, "00 00")
    assert sent[9] == eq_packet(9, "ff 83", commit=True)


def test_the_eq_shown_is_the_one_bragi_last_set(headset, dongle):
    """There is no EQ read command, so before Bragi sets one it is unknown."""
    assert headset.view()["eq"] is None

    headset.set_eq([1, 2, 3, 4, 5, -1, -2, -3, -4, -5])

    assert headset.view()["eq"] == [1, 2, 3, 4, 5, -1, -2, -3, -4, -5]


def test_the_last_set_eq_survives_a_restart(headset, dongle):
    headset.set_eq([3.5] * 10)

    again = headset_hid.HidHeadset(SERIAL, FakeDongle().write, lambda _serial: None)

    assert again.view()["eq"] == [3.5] * 10


@pytest.mark.parametrize("gains", [[0] * 9, [0] * 11, [12.5] + [0] * 9, ["1"] + [0] * 9, None, [True] + [0] * 9])
def test_an_eq_out_of_range_or_the_wrong_shape_is_refused(headset, dongle, gains):
    with pytest.raises(ValueError):
        headset.set_eq(gains)
    assert dongle.sent(0x5F) == []


def test_settings_are_refused_while_the_headset_is_off(headset, dongle):
    """The dongle takes the writes without the headset there to apply them,
    and an EQ kept as "last set" would then be wrong."""
    dongle.headset_on = False
    headset.refresh()

    for change in (lambda: headset.set_eq([0] * 10), lambda: headset.set_mic_monitor(True),
                   lambda: headset.set_auto_off(20)):
        with pytest.raises(headset_hid.HeadsetOff):
            change()
    assert [w for w in dongle.writes if w[3] == 0x00] == []
    assert headset.view()["eq"] is None


def fake_hidraw(root, name: str, hid_id: str, uniq: str) -> None:
    device = root / name / "device"
    device.mkdir(parents=True)
    # Copied from the Pi's /sys/class/hidraw/hidraw0/device/uevent.
    device.joinpath("uevent").write_text(
        f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME=HP, Inc HyperX Cloud III S Wireless\n"
        f"HID_PHYS=usb-0000:01:00.0-1.1.1/input3\nHID_UNIQ={uniq}\n"
    )


def test_both_hardware_revisions_are_found_by_usb_id_not_node_number(tmp_path):
    fake_hidraw(tmp_path, "hidraw3", "0003:000003F0:000006BE", "C1V5340J99")
    fake_hidraw(tmp_path, "hidraw7", "0003:000003F0:000002CC", "ABC123")
    fake_hidraw(tmp_path, "hidraw1", "0003:000017EF:000060EE", "")  # a keyboard

    assert headset_hid.find_devices(tmp_path) == {"hidraw3": "C1V5340J99", "hidraw7": "ABC123"}


def test_no_hidraw_class_at_all_finds_nothing(tmp_path):
    assert headset_hid.find_devices(tmp_path / "missing") == {}


def test_a_headset_card_is_matched_to_its_dongle_by_usb_serial():
    """The serial is the same one the knob watcher matches, embedded in
    the ALSA card id."""
    card = "usb-HP__Inc_HyperX_Cloud_III_S_Wireless_C1V5340J99-00"

    assert headset_hid.serial_matches(card, "C1V5340J99")
    assert not headset_hid.serial_matches(card, "5340J9")
    assert not headset_hid.serial_matches(card, "")
