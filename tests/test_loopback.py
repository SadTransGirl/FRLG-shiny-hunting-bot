"""End-to-end test against a simulated Switch over Bumble's virtual radio link.

The fake Switch connects like the real one does on "Change Grip/Order", runs the
setup handshake, and checks that button presses show up in the input reports.
"""

import asyncio

import pytest
from bumble import hid, sdp
from bumble.controller import Controller
from bumble.core import BT_PNP_INFORMATION_SERVICE, PhysicalTransport
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.transport.common import AsyncPipeSink

from bt_controller.bluetooth import ProController

CONTROLLER_ADDRESS = 'F0:F1:F2:F3:F4:F5'
SWITCH_ADDRESS = 'F5:F4:F3:F2:F1:F0'


class FakeSwitch:
    def __init__(self, link: LocalLink) -> None:
        controller = Controller('switch', link=link, public_address=SWITCH_ADDRESS)
        self.device = Device(address=SWITCH_ADDRESS, host=Host(controller, AsyncPipeSink(controller)))
        self.device.classic_enabled = True
        self.hid = hid.Host(self.device)
        self.reports: asyncio.Queue[bytes] = asyncio.Queue()
        self.hid.on(hid.HID.EVENT_INTERRUPT_DATA, lambda pdu: self.reports.put_nowait(pdu))
        self.packet_number = 0

    async def subcommand(self, sub: int, args: bytes = b'') -> bytes:
        """Send a subcommand and return the matching 0x21 reply (minus header)."""
        report = bytes([0x01, self.packet_number & 0xF]) + bytes(8) + bytes([sub]) + args
        self.packet_number += 1
        self.hid.send_data(report)
        while True:
            pdu = await asyncio.wait_for(self.reports.get(), 2)
            assert pdu[0] == 0xA1
            if pdu[1] == 0x21 and pdu[15] == sub:
                return pdu[1:]

    async def next_full_report(self) -> bytes:
        while True:
            pdu = await asyncio.wait_for(self.reports.get(), 2)
            if pdu[1] == 0x30:
                return pdu[1:]


@pytest.mark.asyncio
async def test_switch_handshake_and_button_press(tmp_path):
    link = LocalLink()
    switch = FakeSwitch(link)

    ctl = ProController(keystore_path=tmp_path / 'keys.json')
    hci_controller = Controller('pro', link=link, public_address=CONTROLLER_ADDRESS)
    device = Device(config=ctl.device_config(), host=Host(hci_controller, AsyncPipeSink(hci_controller)))
    device.public_address = CONTROLLER_ADDRESS

    await switch.device.power_on()
    await ctl.attach(device)
    pair_task = asyncio.create_task(ctl.pair(timeout=10))

    # The Switch connects to the discoverable controller and opens both HID channels.
    switch.hid.connection = await switch.device.connect(
        CONTROLLER_ADDRESS, transport=PhysicalTransport.BR_EDR
    )
    await switch.hid.connect_control_channel()
    await switch.hid.connect_interrupt_channel()
    await asyncio.wait_for(ctl.wait_connected(), 2)

    # Before the Switch says anything, the controller only sends empty reports.
    first = await asyncio.wait_for(switch.reports.get(), 2)
    assert first == b'\xa1' + bytes(49)

    # Like the real Switch, look up the Device ID record first.
    async with sdp.Client(switch.hid.connection) as sdp_client:
        records = await sdp_client.search_attributes(
            [BT_PNP_INFORMATION_SERVICE], [(0x0201, 0x0202)]
        )
    ids = {attribute.id: attribute.value.value for attribute in records[0]}
    assert ids == {0x0201: 0x057E, 0x0202: 0x2009}

    # Setup handshake in the order a Switch sends it.
    info = await switch.subcommand(0x02)
    assert info[13] == 0x82 and info[17] == 0x03  # Pro Controller
    assert info[19:25] == bytes.fromhex('F0F1F2F3F4F5')
    await switch.subcommand(0x08, b'\x00')
    for address, size in [(0x6000, 0x10), (0x6050, 0x0D), (0x6080, 0x18), (0x6098, 0x12),
                          (0x8010, 0x18), (0x603D, 0x19), (0x6020, 0x18)]:
        reply = await switch.subcommand(0x10, address.to_bytes(4, 'little') + bytes([size]))
        assert reply[13] == 0x90 and len(reply) == 49
    await switch.subcommand(0x03, b'\x30')
    await switch.subcommand(0x04)
    await switch.subcommand(0x40, b'\x01')
    await switch.subcommand(0x48, b'\x01')
    await switch.subcommand(0x30, b'\x01')

    # pair() completes once player lights are set, then presses L+R.
    await asyncio.wait_for(pair_task, 3)

    # Full-mode reports keep flowing and reflect a button press.
    while not switch.reports.empty():  # drop reports queued during the handshake
        switch.reports.get_nowait()
    ctl.hold('A')
    for _ in range(5):
        report = await switch.next_full_report()
        if report[3] & 0x08:
            break
    else:
        pytest.fail('A press never appeared in input reports')
    ctl.release()
    await ctl.close()
