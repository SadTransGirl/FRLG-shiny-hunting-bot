# Bluetooth Pro Controller (no SwiCC)

**Experimental.** Makes a USB Bluetooth adapter act as a wireless Nintendo
Switch Pro Controller, controlled from Python. It uses
[Bumble](https://github.com/google/bumble) (Google's Bluetooth stack written in
Python) to drive the adapter directly, because the normal Windows Bluetooth
stack can't pretend to be a controller.

This is step 1 of a SwiCC-free bot: it pairs with the Switch and presses
buttons. Screen reading and the hunting logic come next.

## What you need
- Windows PC with Python 3.10 or newer (python.org installer; tick "Add to PATH")
- A USB Bluetooth adapter **used only for this**. While the bot uses it,
  Windows can't. Your built-in Bluetooth keeps working for other devices.

## One-time setup

1. **Install the Python packages.** In a terminal in this repo folder, run:
   ```
   pip install -r requirements.txt
   ```
2. **Hand the adapter to Python with Zadig.**
   - Plug in the adapter and download Zadig from https://zadig.akeo.ie.
   - In Zadig choose **Options → List All Devices**, then pick your USB adapter
     from the dropdown. Check that the USB ID matches the adapter, **not** your
     built-in Bluetooth. Device Manager → Bluetooth → adapter → Properties →
     Details → Hardware Ids shows the ID.
   - Set the target driver to **WinUSB** and click **Replace Driver**.
   - To undo later: Device Manager → find the adapter → Uninstall device
     (tick "delete the driver"), then unplug it and plug it back in.
3. **Check Python can see it.** Run `bumble-usb-probe`. The adapter should be
   listed. If you have more than one adapter, note its `usb:VID:PID` name and
   pass it with `--transport usb:VID:PID`.
4. **Realtek adapters only:** run `bumble-rtk-fw-download` once to fetch the
   adapter firmware.

## Pairing (first time)
1. On the Switch: **Controllers → Change Grip/Order**. Stay on that screen.
2. On the PC: `python -m bt_controller pair`
3. When it says "Paired!", type `a` and press Enter. This presses A, which
   closes the screen.

The pairing is saved to `switch_pairing.json`.

## Later sessions
Wake the Switch, then run `python -m bt_controller connect`. You don't need
the pairing screen.

## Testing buttons
After connecting you get a prompt:
```
> a                 press A
> home              press HOME
> a 0.5             hold A for half a second
> l+r               press L and R together
> spam a 10 1.0     press A ten times, once a second
> stick left 0 1    push the left stick up ('stick left 0 0' re-centers)
> quit
```

## Using it from Python
```python
import asyncio
from bt_controller import ProController

async def main():
    async with ProController('usb:0') as controller:
        await controller.reconnect()
        for _ in range(10):
            await controller.press('A')
            await asyncio.sleep(1)

asyncio.run(main())
```

## Reporting problems
Every run writes a detailed log to `bt_controller.log`. If pairing or
connecting fails, send that file along with your adapter's hardware ID
(`USB\VID_xxxx&PID_xxxx`).

To start over: `python -m bt_controller forget`, then remove the controller
on the Switch (System Settings → Controllers and Sensors → Disconnect
Controllers).

## Known limits
- Not yet tested on a real Switch. The protocol is based on community reverse
  engineering (dekuNukem's notes, joycontrol and NXBT, all proven on Linux),
  but the first real pairing may need fixes.
- Motion controls, NFC and rumble are ignored.
- Some adapters don't support everything Bumble needs. Cheap CSR8510-based and
  Realtek RTL8761B-based adapters are the usual safe picks.
