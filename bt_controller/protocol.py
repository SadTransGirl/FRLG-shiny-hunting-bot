"""Nintendo Switch Pro Controller protocol (Bluetooth HID reports).

Pure logic, no Bluetooth: builds the input reports the controller sends and
answers the subcommands the Switch sends. Based on the community reverse
engineering at https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering
(see bluetooth_hid_notes.md, bluetooth_hid_subcommands_notes.md, spi_flash_notes.md).

Report layout used here is the HID payload *without* the 1-byte HIDP header
(0xA1 for input, 0xA2 for output); bumble.hid adds/strips that header.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Input report IDs (controller -> Switch)
REPORT_SUBCOMMAND_REPLY = 0x21
REPORT_FULL = 0x30
INPUT_REPORT_SIZE = 49  # report id + 48 bytes

# Output report IDs (Switch -> controller)
OUTPUT_RUMBLE_AND_SUBCOMMAND = 0x01
OUTPUT_RUMBLE_ONLY = 0x10
OUTPUT_MCU_REQUEST = 0x11

# Subcommand IDs
SUB_BT_PAIRING = 0x01
SUB_DEVICE_INFO = 0x02
SUB_SET_INPUT_MODE = 0x03
SUB_TRIGGER_ELAPSED = 0x04
SUB_SHIPMENT_MODE = 0x08
SUB_SPI_READ = 0x10
SUB_SET_MCU_CONFIG = 0x21
SUB_SET_MCU_STATE = 0x22
SUB_SET_PLAYER_LIGHTS = 0x30
SUB_SET_HOME_LIGHT = 0x38
SUB_ENABLE_IMU = 0x40
SUB_IMU_SENSITIVITY = 0x41
SUB_ENABLE_VIBRATION = 0x48

SUBCOMMAND_NAMES = {
    SUB_BT_PAIRING: 'bluetooth pairing',
    SUB_DEVICE_INFO: 'device info',
    SUB_SET_INPUT_MODE: 'set input mode',
    SUB_TRIGGER_ELAPSED: 'trigger buttons elapsed time',
    SUB_SHIPMENT_MODE: 'shipment mode',
    SUB_SPI_READ: 'read calibration (SPI)',
    SUB_SET_MCU_CONFIG: 'NFC/IR config',
    SUB_SET_MCU_STATE: 'NFC/IR state',
    SUB_SET_PLAYER_LIGHTS: 'set player lights',
    SUB_SET_HOME_LIGHT: 'set HOME light',
    SUB_ENABLE_IMU: 'enable motion sensors',
    SUB_IMU_SENSITIVITY: 'motion sensor sensitivity',
    SUB_ENABLE_VIBRATION: 'enable vibration',
}

CONTROLLER_TYPE_PRO = 0x03
FIRMWARE_VERSION = (0x03, 0x8B)
BATTERY_AND_CONNECTION = 0x8E  # battery full + connection info, as joycontrol sends

# Button name -> (byte offset within the 3 button bytes, bit mask)
BUTTONS: dict[str, tuple[int, int]] = {
    # right byte
    'Y': (0, 0x01), 'X': (0, 0x02), 'B': (0, 0x04), 'A': (0, 0x08),
    'R': (0, 0x40), 'ZR': (0, 0x80),
    # shared byte
    'MINUS': (1, 0x01), 'PLUS': (1, 0x02), 'RSTICK': (1, 0x04), 'LSTICK': (1, 0x08),
    'HOME': (1, 0x10), 'CAPTURE': (1, 0x20),
    # left byte
    'DOWN': (2, 0x01), 'UP': (2, 0x02), 'RIGHT': (2, 0x04), 'LEFT': (2, 0x08),
    'L': (2, 0x40), 'ZL': (2, 0x80),
}
BUTTON_ALIASES = {'-': 'MINUS', '+': 'PLUS', 'SELECT': 'MINUS', 'START': 'PLUS'}

# 12-bit analog stick values. Calibration below advertises center 0x800 with
# +/-STICK_RANGE travel, so these map to full deflection.
STICK_CENTER = 0x800
STICK_RANGE = 0x600


def button_name(name: str) -> str:
    key = name.strip().upper()
    key = BUTTON_ALIASES.get(key, key)
    if key not in BUTTONS:
        raise ValueError(f'unknown button {name!r}; choose from {", ".join(BUTTONS)}')
    return key


def pack_12bit_pair(a: int, b: int) -> bytes:
    """Pack two 12-bit values into 3 bytes (layout used by sticks and calibration)."""
    return bytes([a & 0xFF, ((a >> 8) & 0x0F) | ((b & 0x0F) << 4), (b >> 4) & 0xFF])


def unpack_12bit_pair(data: bytes) -> tuple[int, int]:
    return data[0] | ((data[1] & 0x0F) << 8), (data[1] >> 4) | (data[2] << 4)


@dataclass
class ControllerState:
    """Buttons currently held and stick positions (12-bit, 0x800 = centered)."""

    buttons: set[str] = field(default_factory=set)
    left_stick: tuple[int, int] = (STICK_CENTER, STICK_CENTER)
    right_stick: tuple[int, int] = (STICK_CENTER, STICK_CENTER)

    def button_bytes(self) -> bytes:
        out = bytearray(3)
        for name in self.buttons:
            index, mask = BUTTONS[name]
            out[index] |= mask
        return bytes(out)


def build_spi_flash(colors: tuple[bytes, bytes]) -> bytearray:
    """A minimal SPI flash image with the regions the Switch reads on connect.

    Unwritten flash reads as 0xFF, which the Switch treats as "no user data".
    """
    flash = bytearray(b'\xff' * 0x80000)

    # 0x6020: factory 6-axis calibration: accel origin, accel sensitivity,
    # gyro origin, gyro sensitivity (3 x int16 LE each).
    imu = b''
    for value in (0, 0, 0, 0x4000, 0x4000, 0x4000, 0, 0, 0, 0x343B, 0x343B, 0x343B):
        imu += value.to_bytes(2, 'little')
    flash[0x6020:0x6038] = imu

    # 0x603D: factory stick calibration. Left stick is (max above center,
    # center, min below center); right stick is (center, min below, max above).
    above = pack_12bit_pair(STICK_RANGE, STICK_RANGE)
    center = pack_12bit_pair(STICK_CENTER, STICK_CENTER)
    below = pack_12bit_pair(STICK_RANGE, STICK_RANGE)
    flash[0x603D:0x6046] = above + center + below
    flash[0x6046:0x604F] = center + below + above

    # 0x6050: body color, button color, left grip, right grip (RGB each)
    body, buttons = colors
    flash[0x6050:0x6056] = body + buttons
    flash[0x6056:0x605C] = body + body

    # 0x6080: 6-axis horizontal offsets + stick device parameters (dead zone,
    # range ratio), values as found on a retail Pro Controller.
    stick_params = bytes.fromhex('0f30619630f3d41454411554c7799c333663')
    flash[0x6080:0x6086] = bytes.fromhex('50fd0000c60f')
    flash[0x6086:0x6098] = stick_params
    flash[0x6098:0x60AA] = stick_params
    return flash


class SwitchProtocol:
    """State machine for one emulated Pro Controller."""

    def __init__(
        self,
        mac_address: bytes,
        body_color: bytes = bytes([0x32, 0x32, 0x32]),
        button_color: bytes = bytes([0xFF, 0xFF, 0xFF]),
    ) -> None:
        if len(mac_address) != 6:
            raise ValueError('mac_address must be 6 bytes (big endian)')
        self.mac_address = mac_address
        self.state = ControllerState()
        self.spi_flash = build_spi_flash((body_color, button_color))
        self.input_mode: int | None = None
        self.player_lights = 0
        self.vibration_enabled = False
        self.imu_enabled = False
        self._timer = 0

    # -- input reports --------------------------------------------------------

    def _standard_header(self, report_id: int) -> bytearray:
        report = bytearray(INPUT_REPORT_SIZE)
        report[0] = report_id
        report[1] = self._timer
        self._timer = (self._timer + 1) & 0xFF
        report[2] = BATTERY_AND_CONNECTION
        report[3:6] = self.state.button_bytes()
        report[6:9] = pack_12bit_pair(*self.state.left_stick)
        report[9:12] = pack_12bit_pair(*self.state.right_stick)
        report[12] = 0x80  # vibrator input report
        return report

    def reset_session(self) -> None:
        """Forget per-connection state before a new setup handshake."""
        self.input_mode = None
        self.player_lights = 0

    def empty_report(self) -> bytes:
        """All-zero report sent once a second until the Switch starts talking."""
        return bytes(INPUT_REPORT_SIZE)

    def full_report(self) -> bytes:
        """Standard full-mode (0x30) input report; IMU data is left zeroed."""
        return bytes(self._standard_header(REPORT_FULL))

    def _subcommand_reply(self, ack: int, subcommand: int, data: bytes = b'') -> bytes:
        report = self._standard_header(REPORT_SUBCOMMAND_REPLY)
        report[13] = ack
        report[14] = subcommand
        if len(data) > INPUT_REPORT_SIZE - 15:
            raise ValueError('subcommand reply data too long')
        report[15:15 + len(data)] = data
        return bytes(report)

    # -- output reports -------------------------------------------------------

    def handle_output_report(self, report: bytes) -> bytes | None:
        """Process a report from the Switch; return an input report to send now, if any."""
        if not report:
            return None
        report_id = report[0]
        if report_id == OUTPUT_RUMBLE_AND_SUBCOMMAND and len(report) >= 11:
            return self._handle_subcommand(report[10], report[11:])
        if report_id in (OUTPUT_RUMBLE_ONLY, OUTPUT_MCU_REQUEST):
            return None  # rumble/NFC data: nothing to answer
        logger.debug('ignoring output report %s', report.hex())
        return None

    def _handle_subcommand(self, subcommand: int, args: bytes) -> bytes:
        logger.info('Switch request: %s', SUBCOMMAND_NAMES.get(subcommand, f'0x{subcommand:02X}'))
        logger.debug('subcommand 0x%02X args %s', subcommand, args[:16].hex())

        if subcommand == SUB_DEVICE_INFO:
            data = bytes([*FIRMWARE_VERSION, CONTROLLER_TYPE_PRO, 0x02])
            data += self.mac_address + bytes([0x01, 0x01])
            return self._subcommand_reply(0x82, subcommand, data)

        if subcommand == SUB_SET_INPUT_MODE:
            self.input_mode = args[0] if args else REPORT_FULL
            logger.info('input report mode set to 0x%02X', self.input_mode)
            return self._subcommand_reply(0x80, subcommand)

        if subcommand == SUB_TRIGGER_ELAPSED:
            # Elapsed time (10 ms units) of L, R, ZL, ZR, SL, SR, HOME. Report L and R
            # as recently held, as the "Press L+R" pairing screen expects.
            data = bytearray(14)
            data[0:2] = (300).to_bytes(2, 'little')
            data[2:4] = (300).to_bytes(2, 'little')
            return self._subcommand_reply(0x83, subcommand, bytes(data))

        if subcommand == SUB_SPI_READ:
            if len(args) < 5:
                return self._subcommand_reply(0x80, subcommand)
            address = int.from_bytes(args[0:4], 'little')
            size = min(args[4], 0x1D)
            chunk = bytes(self.spi_flash[address:address + size])
            chunk += b'\xff' * (size - len(chunk))
            return self._subcommand_reply(0x90, subcommand, args[0:4] + bytes([size]) + chunk)

        if subcommand == SUB_SET_MCU_CONFIG:
            # Canned "MCU ready" status; last byte is its CRC-8.
            data = bytearray(INPUT_REPORT_SIZE - 15)
            data[0:8] = bytes([0x01, 0x00, 0xFF, 0x00, 0x08, 0x00, 0x1B, 0x01])
            data[-1] = 0xC8
            return self._subcommand_reply(0xA0, subcommand, bytes(data))

        if subcommand == SUB_BT_PAIRING:
            return self._subcommand_reply(0x81, subcommand, bytes([0x03]))

        if subcommand == SUB_SET_PLAYER_LIGHTS:
            self.player_lights = args[0] if args else 0
            logger.info('player lights set to 0b%s', format(self.player_lights & 0x0F, '04b'))
        elif subcommand == SUB_ENABLE_VIBRATION:
            self.vibration_enabled = bool(args and args[0])
        elif subcommand == SUB_ENABLE_IMU:
            self.imu_enabled = bool(args and args[0])
        elif subcommand not in (
            SUB_SHIPMENT_MODE, SUB_SET_MCU_STATE, SUB_SET_HOME_LIGHT, SUB_IMU_SENSITIVITY
        ):
            logger.warning('unhandled subcommand 0x%02X, sending plain ACK', subcommand)
        return self._subcommand_reply(0x80, subcommand)

    @property
    def paired(self) -> bool:
        """True once the Switch has finished its setup handshake."""
        return self.input_mode is not None and self.player_lights != 0
