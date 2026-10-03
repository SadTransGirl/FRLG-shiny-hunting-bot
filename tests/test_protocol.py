import pytest

from bt_controller.protocol import (
    INPUT_REPORT_SIZE,
    STICK_CENTER,
    STICK_RANGE,
    SwitchProtocol,
    pack_12bit_pair,
    unpack_12bit_pair,
)

MAC = bytes.fromhex('a1b2c3d4e5f6')


def subcommand(sub: int, args: bytes = b'') -> bytes:
    """An output report 0x01 as sent by the Switch (without HIDP header)."""
    return bytes([0x01, 0x00]) + bytes(8) + bytes([sub]) + args


@pytest.fixture
def proto():
    return SwitchProtocol(MAC)


def test_pack_roundtrip():
    for a, b in [(0, 0), (0x800, 0x800), (0xFFF, 0x123), (0x200, 0xE00)]:
        assert unpack_12bit_pair(pack_12bit_pair(a, b)) == (a, b)


def test_full_report_layout(proto):
    proto.state.buttons.update({'A', 'HOME', 'DOWN'})
    proto.state.left_stick = (0x123, 0xABC)
    report = proto.full_report()
    assert len(report) == INPUT_REPORT_SIZE
    assert report[0] == 0x30
    assert report[3:6] == bytes([0x08, 0x10, 0x01])
    assert unpack_12bit_pair(report[6:9]) == (0x123, 0xABC)
    assert unpack_12bit_pair(report[9:12]) == (STICK_CENTER, STICK_CENTER)


def test_device_info(proto):
    reply = proto.handle_output_report(subcommand(0x02))
    assert reply[0] == 0x21
    assert reply[13:15] == bytes([0x82, 0x02])
    assert reply[17] == 0x03  # Pro Controller
    assert reply[19:25] == MAC


def test_set_input_mode_and_player_lights(proto):
    assert not proto.paired
    reply = proto.handle_output_report(subcommand(0x03, b'\x30'))
    assert reply[13:15] == bytes([0x80, 0x03])
    assert proto.input_mode == 0x30
    proto.handle_output_report(subcommand(0x30, b'\x01'))
    assert proto.player_lights == 1
    assert proto.paired


def test_spi_read_echoes_address_and_size(proto):
    reply = proto.handle_output_report(subcommand(0x10, bytes([0x50, 0x60, 0, 0, 0x0D])))
    assert reply[13:15] == bytes([0x90, 0x10])
    assert reply[15:20] == bytes([0x50, 0x60, 0, 0, 0x0D])
    assert reply[20:23] == bytes([0x32, 0x32, 0x32])  # body color


def test_spi_stick_calibration_decodes(proto):
    reply = proto.handle_output_report(subcommand(0x10, bytes([0x3D, 0x60, 0, 0, 0x12])))
    left = reply[20:29]
    # Left stick: max above center, center, min below center.
    assert unpack_12bit_pair(left[0:3]) == (STICK_RANGE, STICK_RANGE)
    assert unpack_12bit_pair(left[3:6]) == (STICK_CENTER, STICK_CENTER)
    right = reply[29:38]
    # Right stick: center first.
    assert unpack_12bit_pair(right[0:3]) == (STICK_CENTER, STICK_CENTER)


def test_spi_read_unwritten_is_ff(proto):
    reply = proto.handle_output_report(subcommand(0x10, bytes([0x10, 0x80, 0, 0, 0x18])))
    assert reply[20:20 + 0x18] == b'\xff' * 0x18


def test_mcu_config_reply(proto):
    reply = proto.handle_output_report(subcommand(0x21, bytes([0x21, 0x00, 0x00])))
    assert reply[13:15] == bytes([0xA0, 0x21])
    assert reply[-1] == 0xC8
    assert len(reply) == INPUT_REPORT_SIZE


def test_unknown_subcommand_gets_ack(proto):
    reply = proto.handle_output_report(subcommand(0x77))
    assert reply[13:15] == bytes([0x80, 0x77])


def test_rumble_only_needs_no_reply(proto):
    assert proto.handle_output_report(bytes([0x10, 0x01]) + bytes(8)) is None
