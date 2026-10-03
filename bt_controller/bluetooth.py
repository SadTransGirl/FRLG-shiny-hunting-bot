"""Emulate a Switch Pro Controller over Bluetooth using Bumble.

Bumble is a Bluetooth host stack written in Python that drives a USB Bluetooth
adapter directly (via WinUSB/libusb), bypassing the Windows Bluetooth stack,
which cannot act as a Classic HID device.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from bumble import hci, hid
from bumble.core import (
    BT_HIDP_PROTOCOL_ID,
    BT_HUMAN_INTERFACE_DEVICE_SERVICE,
    BT_L2CAP_PROTOCOL_ID,
    BT_PNP_INFORMATION_SERVICE,
    PhysicalTransport,
)
from bumble.device import Device, DeviceConfiguration
from bumble.pairing import PairingDelegate
from bumble.sdp import (
    SDP_ADDITIONAL_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
    SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
    SDP_BROWSE_GROUP_LIST_ATTRIBUTE_ID,
    SDP_LANGUAGE_BASE_ATTRIBUTE_ID_LIST_ATTRIBUTE_ID,
    SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
    SDP_PUBLIC_BROWSE_ROOT,
    SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
    SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
    DataElement,
    ServiceAttribute,
)
from bumble.transport import open_transport

from .protocol import (
    REPORT_FULL,
    STICK_CENTER,
    STICK_RANGE,
    SwitchProtocol,
    button_name,
)

logger = logging.getLogger(__name__)

CONTROLLER_NAME = 'Pro Controller'

# Allow role switch (0x0001) and sniff mode (0x0004) on new links, as Linux does
# by default. Adapters power up with both disabled, and the Switch drops a
# controller that refuses to enter sniff mode about a second after connecting.
DEFAULT_LINK_POLICY = 0x0005
CLASS_OF_DEVICE = 0x002508  # Peripheral / Gamepad

# Device ID (PnP) record values of a genuine Pro Controller. The Switch looks
# these up before anything else and drops controllers that don't match.
NINTENDO_VENDOR_ID = 0x057E
PRO_CONTROLLER_PRODUCT_ID = 0x2009

# The report descriptor a genuine Pro Controller / Joy-Con publishes over Bluetooth.
HID_REPORT_DESCRIPTOR = bytes.fromhex(
    '05010905a101'  # Usage Page (Generic Desktop), Usage (Game Pad), Collection
    '0601ff'  # Usage Page (Vendor 0xFF01)
    '85210921750895308102'  # Input 0x21, 48 bytes
    '85300930750895308102'  # Input 0x30, 48 bytes
    '853109317508966901' '8102'  # Input 0x31, 361 bytes
    '853209327508966901' '8102'  # Input 0x32, 361 bytes
    '853309337508966901' '8102'  # Input 0x33, 361 bytes
    '853f05091901291015002501750195108102'  # Input 0x3F: 16 buttons
    '05010939150025077504950181420509750495018101'  # hat switch + padding
    '0501093009310933093416000027ffff0000751095048102'  # 4 axes, 16 bit
    '0601ff'
    '85010901750895309102'  # Output 0x01, 48 bytes
    '85100910750895309102'  # Output 0x10, 48 bytes
    '85110911750895309102'  # Output 0x11, 48 bytes
    '85120912750895309102'  # Output 0x12, 48 bytes
    'c0'
)


def _device_id_record(handle: int) -> list[ServiceAttribute]:
    return [
        ServiceAttribute(SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID, DataElement.unsigned_integer_32(handle)),
        ServiceAttribute(
            SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            DataElement.sequence([DataElement.uuid(BT_PNP_INFORMATION_SERVICE)]),
        ),
        ServiceAttribute(
            SDP_BROWSE_GROUP_LIST_ATTRIBUTE_ID,
            DataElement.sequence([DataElement.uuid(SDP_PUBLIC_BROWSE_ROOT)]),
        ),
        ServiceAttribute(
            SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            DataElement.sequence([
                DataElement.sequence([
                    DataElement.uuid(BT_PNP_INFORMATION_SERVICE),
                    DataElement.unsigned_integer_16(0x0103),
                ]),
            ]),
        ),
        ServiceAttribute(0x0200, DataElement.unsigned_integer_16(0x0103)),  # specification ID
        ServiceAttribute(0x0201, DataElement.unsigned_integer_16(NINTENDO_VENDOR_ID)),
        ServiceAttribute(0x0202, DataElement.unsigned_integer_16(PRO_CONTROLLER_PRODUCT_ID)),
        ServiceAttribute(0x0203, DataElement.unsigned_integer_16(0x0001)),  # version
        ServiceAttribute(0x0204, DataElement.boolean(True)),  # primary record
        ServiceAttribute(0x0205, DataElement.unsigned_integer_16(0x0002)),  # vendor ID source: USB-IF
    ]


def _sdp_records() -> dict[int, list[ServiceAttribute]]:
    handle = 0x00010001
    device_id_handle = 0x00010002
    text = lambda s: DataElement(DataElement.TEXT_STRING, s)  # noqa: E731
    return {
        device_id_handle: _device_id_record(device_id_handle),
        handle: [
            ServiceAttribute(SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID, DataElement.unsigned_integer_32(handle)),
            ServiceAttribute(
                SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
                DataElement.sequence([DataElement.uuid(BT_HUMAN_INTERFACE_DEVICE_SERVICE)]),
            ),
            ServiceAttribute(
                SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
                DataElement.sequence([
                    DataElement.sequence([
                        DataElement.uuid(BT_L2CAP_PROTOCOL_ID),
                        DataElement.unsigned_integer_16(hid.HID_CONTROL_PSM),
                    ]),
                    DataElement.sequence([DataElement.uuid(BT_HIDP_PROTOCOL_ID)]),
                ]),
            ),
            ServiceAttribute(
                SDP_BROWSE_GROUP_LIST_ATTRIBUTE_ID,
                DataElement.sequence([DataElement.uuid(SDP_PUBLIC_BROWSE_ROOT)]),
            ),
            ServiceAttribute(
                SDP_LANGUAGE_BASE_ATTRIBUTE_ID_LIST_ATTRIBUTE_ID,
                DataElement.sequence([
                    DataElement.unsigned_integer_16(0x656E),  # "en"
                    DataElement.unsigned_integer_16(0x006A),  # UTF-8
                    DataElement.unsigned_integer_16(0x0100),
                ]),
            ),
            ServiceAttribute(
                SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
                DataElement.sequence([
                    DataElement.sequence([
                        DataElement.uuid(BT_HUMAN_INTERFACE_DEVICE_SERVICE),
                        DataElement.unsigned_integer_16(0x0101),
                    ]),
                ]),
            ),
            ServiceAttribute(
                SDP_ADDITIONAL_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
                DataElement.sequence([
                    DataElement.sequence([
                        DataElement.sequence([
                            DataElement.uuid(BT_L2CAP_PROTOCOL_ID),
                            DataElement.unsigned_integer_16(hid.HID_INTERRUPT_PSM),
                        ]),
                        DataElement.sequence([DataElement.uuid(BT_HIDP_PROTOCOL_ID)]),
                    ]),
                ]),
            ),
            ServiceAttribute(0x0100, text(b'Wireless Gamepad')),  # service name
            ServiceAttribute(0x0101, text(b'Gamepad')),  # description
            ServiceAttribute(0x0102, text(b'Nintendo')),  # provider
            ServiceAttribute(0x0201, DataElement.unsigned_integer_16(0x0111)),  # parser version
            ServiceAttribute(0x0202, DataElement.unsigned_integer_8(0x08)),  # subclass: gamepad
            ServiceAttribute(0x0203, DataElement.unsigned_integer_8(0x00)),  # country code
            ServiceAttribute(0x0204, DataElement.boolean(True)),  # virtual cable
            ServiceAttribute(0x0205, DataElement.boolean(True)),  # reconnect initiate
            ServiceAttribute(
                0x0206,  # HID descriptor list
                DataElement.sequence([
                    DataElement.sequence([
                        DataElement.unsigned_integer_8(0x22),  # report descriptor
                        text(HID_REPORT_DESCRIPTOR),
                    ]),
                ]),
            ),
            ServiceAttribute(
                0x0207,  # LANGID base list
                DataElement.sequence([
                    DataElement.sequence([
                        DataElement.unsigned_integer_16(0x0409),
                        DataElement.unsigned_integer_16(0x0100),
                    ]),
                ]),
            ),
            ServiceAttribute(0x0209, DataElement.boolean(True)),  # battery power
            ServiceAttribute(0x020A, DataElement.boolean(True)),  # remote wake
            ServiceAttribute(0x020C, DataElement.unsigned_integer_16(0x0C80)),  # supervision timeout
            ServiceAttribute(0x020D, DataElement.boolean(False)),  # normally connectable
            ServiceAttribute(0x020E, DataElement.boolean(False)),  # boot device
        ]
    }


class _HidDevice(hid.Device):
    """bumble HID device that reports when its L2CAP channels open/close."""

    def __init__(self, device: Device, on_channels_changed) -> None:
        super().__init__(device)
        self._on_channels_changed = on_channels_changed

    def on_l2cap_channel_open(self, l2cap_channel) -> None:
        super().on_l2cap_channel_open(l2cap_channel)
        self._on_channels_changed()

    def on_l2cap_channel_close(self, l2cap_channel) -> None:
        super().on_l2cap_channel_close(l2cap_channel)
        self._on_channels_changed()


class ProController:
    """A virtual Pro Controller.

    Typical use::

        async with ProController('usb:0') as controller:
            await controller.pair()          # first time, on "Change Grip/Order"
            # or: await controller.reconnect()
            await controller.press('A')
    """

    def __init__(
        self,
        transport: str = 'usb:0',
        keystore_path: str | Path = 'switch_pairing.json',
        report_rate: float = 66.0,
    ) -> None:
        self.transport_spec = transport
        self.keystore_path = Path(keystore_path)
        self.report_rate = report_rate
        self.device: Device | None = None
        self.hid: _HidDevice | None = None
        self.protocol: SwitchProtocol | None = None
        self._transport = None
        self._report_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._ready = asyncio.Event()
        self._heard_from_switch = False  # any output report on this connection yet?
        self._skip_next_report = False  # a subcommand reply took this report slot
        self._windows_timer = False

    # -- setup ----------------------------------------------------------------

    def device_config(self) -> DeviceConfiguration:
        config = DeviceConfiguration()
        config.name = CONTROLLER_NAME
        config.class_of_device = CLASS_OF_DEVICE
        config.classic_enabled = True
        config.le_enabled = False
        config.keystore = f'JsonKeyStore:{self.keystore_path}'
        config.io_capability = PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT
        return config

    async def open(self) -> None:
        """Open the Bluetooth adapter and start the controller (not yet connected)."""
        if sys.platform == 'win32':
            # Default Windows timer resolution (~15.6 ms) makes report timing jittery.
            import ctypes

            ctypes.windll.winmm.timeBeginPeriod(1)
            self._windows_timer = True
        logger.info('opening Bluetooth adapter %s', self.transport_spec)
        try:
            self._transport = await open_transport(self.transport_spec)
        except Exception as error:
            raise RuntimeError(
                f'could not open Bluetooth adapter {self.transport_spec!r} ({error}). '
                'Is it plugged in and switched to the WinUSB driver with Zadig? '
                'Run "bumble-usb-probe" to list adapters.'
            ) from error
        device = Device.from_config_with_hci(
            self.device_config(), self._transport.source, self._transport.sink
        )
        await self.attach(device)

    async def attach(self, device: Device) -> None:
        """Set up the controller on an already-created bumble Device and power it on."""
        self.device = device
        device.sdp_service_records = _sdp_records()
        self.hid = _HidDevice(device, self._on_channels_changed)
        self.hid.on(hid.HID.EVENT_INTERRUPT_DATA, self._on_interrupt_data)
        self.hid.on(hid.HID.EVENT_VIRTUAL_CABLE_UNPLUG, lambda: logger.warning('Switch unpaired this controller'))
        self.hid.register_set_protocol_cb(
            lambda _mode: hid.Device.GetSetStatus(status=hid.Device.GetSetReturn.SUCCESS)
        )
        device.on(device.EVENT_CONNECTION, self._on_connection)

        await device.power_on()
        try:
            await device.send_command(
                hci.HCI_Write_Default_Link_Policy_Settings_Command(
                    default_link_policy_settings=DEFAULT_LINK_POLICY
                ),
                check_result=True,
            )
        except Exception as error:
            logger.warning('adapter refused to enable sniff mode / role switch (%s)', error)
        address = device.public_address
        logger.info('adapter address %s', address)
        # bytes(Address) is little endian; the protocol reports it big endian.
        self.protocol = SwitchProtocol(mac_address=bytes(reversed(bytes(address))))
        self._report_task = asyncio.create_task(self._report_loop())

    async def close(self) -> None:
        if self._report_task:
            self._report_task.cancel()
            try:
                await self._report_task
            except asyncio.CancelledError:
                pass
            self._report_task = None
        if self.hid and self.hid.connection:
            try:
                await self.hid.connection.disconnect()
            except Exception:  # already gone
                logger.debug('disconnect failed', exc_info=True)
        if self._transport:
            await self._transport.close()
            self._transport = None
        if self._windows_timer:
            import ctypes

            ctypes.windll.winmm.timeEndPeriod(1)
            self._windows_timer = False

    async def __aenter__(self) -> ProController:
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- connecting -----------------------------------------------------------

    async def pair(self, timeout: float | None = None) -> None:
        """Pair with a Switch sitting on Controllers > Change Grip/Order."""
        assert self.device
        logger.info('waiting for the Switch: open Controllers > Change Grip/Order')
        await self.device.set_discoverable(True)
        await self.device.set_connectable(True)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        finally:
            await self.device.set_discoverable(False)
        logger.info('paired; player lights 0b%s', format(self.protocol.player_lights & 0xF, '04b'))
        # The pairing screen registers a Pro Controller once L+R are pressed.
        await self.press('L', 'R')

    async def paired_switches(self) -> list[str]:
        assert self.device and self.device.keystore
        return [address for address, _keys in await self.device.keystore.get_all()]

    async def reconnect(self, switch_address: str | None = None, timeout: float = 10.0) -> None:
        """Connect to a Switch this adapter has already paired with."""
        assert self.device and self.hid
        if switch_address is None:
            known = await self.paired_switches()
            if not known:
                raise RuntimeError('no paired Switch found; run pairing first')
            switch_address = known[-1]
        logger.info('connecting to Switch %s', switch_address)
        await self.device.set_connectable(True)
        connection = await self.device.connect(
            switch_address, transport=PhysicalTransport.BR_EDR, timeout=timeout
        )
        await connection.authenticate()
        await connection.encrypt()
        await self.hid.connect_control_channel()
        await self.hid.connect_interrupt_channel()
        self._on_channels_changed()
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            logger.warning('Switch did not redo the setup handshake; continuing anyway')
        logger.info('connected')

    async def forget_switches(self) -> None:
        assert self.device and self.device.keystore
        for address in await self.paired_switches():
            await self.device.keystore.delete(address)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self) -> None:
        await self._connected.wait()

    # -- input ----------------------------------------------------------------

    def hold(self, *buttons: str) -> None:
        assert self.protocol
        self.protocol.state.buttons.update(button_name(b) for b in buttons)

    def release(self, *buttons: str) -> None:
        assert self.protocol
        if not buttons:
            self.protocol.state.buttons.clear()
        else:
            self.protocol.state.buttons.difference_update(button_name(b) for b in buttons)

    async def press(self, *buttons: str, duration: float = 0.1, wait: float = 0.05) -> None:
        """Press buttons together for `duration` seconds, release, then wait `wait` seconds."""
        # Hold for at least a few reports so the Switch can't miss the press.
        duration = max(duration, 3 / self.report_rate)
        self.hold(*buttons)
        await asyncio.sleep(duration)
        self.release(*buttons)
        await asyncio.sleep(max(wait, 2 / self.report_rate))

    def set_stick(self, stick: str, x: float, y: float) -> None:
        """Set a stick position; x and y range from -1.0 to 1.0 (up/right positive)."""
        assert self.protocol
        position = tuple(
            STICK_CENTER + round(max(-1.0, min(1.0, v)) * STICK_RANGE) for v in (x, y)
        )
        if stick.lower() in ('l', 'left'):
            self.protocol.state.left_stick = position
        elif stick.lower() in ('r', 'right'):
            self.protocol.state.right_stick = position
        else:
            raise ValueError("stick must be 'left' or 'right'")

    # -- internals ------------------------------------------------------------

    def _on_connection(self, connection) -> None:
        logger.info('Bluetooth connection from/to %s', connection.peer_address)
        connection.on(connection.EVENT_DISCONNECTION, self._on_disconnection)
        connection.on(
            connection.EVENT_MODE_CHANGE,
            lambda: logger.info(
                'link mode: %s (interval %d slots)',
                hci.HCI_Mode_Change_Event.Mode(connection.classic_mode).name,
                connection.classic_interval,
            ),
        )
        connection.on(
            connection.EVENT_MODE_CHANGE_FAILURE,
            lambda status: logger.warning('link mode change failed: %s', hci.HCI_Constant.error_name(status)),
        )
        connection.on(connection.EVENT_ROLE_CHANGE, lambda role: logger.info('link role: %s', role))

    def _on_disconnection(self, reason: int) -> None:
        logger.warning('disconnected (reason %s)', hci.HCI_Constant.error_name(reason))
        self._connected.clear()
        self._ready.clear()

    def _on_channels_changed(self) -> None:
        if self.hid and self.hid.l2cap_ctrl_channel and self.hid.l2cap_intr_channel:
            if not self._connected.is_set():
                logger.info('HID channels open')
                # The Switch redoes the setup handshake on every connection.
                self.protocol.reset_session()
                self._heard_from_switch = False
            self._connected.set()
        else:
            self._connected.clear()
            self._ready.clear()

    def _on_interrupt_data(self, pdu: bytes) -> None:
        if not pdu or pdu[0] != 0xA2:  # HIDP DATA | OUTPUT
            logger.debug('ignoring interrupt PDU %s', pdu.hex())
            return
        self._heard_from_switch = True
        reply = self.protocol.handle_output_report(pdu[1:])
        if reply is not None:
            self._send(reply)
            self._skip_next_report = True
        if self.protocol.paired:
            self._ready.set()

    def _send(self, report: bytes) -> None:
        if self.hid and self.hid.l2cap_intr_channel:
            self.hid.send_data(report)

    def _congested(self) -> bool:
        # Skip a periodic report rather than let a backlog build up input lag.
        queue = self.device.host.acl_packet_queue if self.device else None
        return queue is not None and queue.pending > queue.max_in_flight

    async def _report_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_time = loop.time()
        while True:
            if not self._connected.is_set():
                await self._connected.wait()
                next_time = loop.time()
            # Like a real controller (and joycontrol): an empty report once a
            # second until the Switch speaks, then only subcommand replies until
            # it selects full mode, then full reports at report_rate.
            report = None
            if self.protocol.input_mode == REPORT_FULL:
                period = 1 / self.report_rate
                if not self._skip_next_report and not self._congested():
                    report = self.protocol.full_report()
            elif not self._heard_from_switch:
                period = 1.0
                report = self.protocol.empty_report()
            else:
                period = 1 / self.report_rate
            self._skip_next_report = False
            if report is not None:
                try:
                    self._send(report)
                except Exception:
                    logger.debug('report send failed', exc_info=True)
            next_time += period
            delay = next_time - loop.time()
            if delay < -period:  # fell behind (e.g. system stall): resync
                next_time = loop.time()
                delay = 0
            await asyncio.sleep(max(0.0, delay))
