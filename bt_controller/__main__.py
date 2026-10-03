"""Command line: pair with a Switch and press buttons by hand.

    python -m bt_controller pair            # first time (Change Grip/Order screen)
    python -m bt_controller connect         # later sessions
    python -m bt_controller forget          # delete saved pairings
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .bluetooth import ProController
from .protocol import BUTTONS

HELP = f"""Commands:
  a                 press A (any button: {', '.join(b.lower() for b in BUTTONS)})
  a 0.5             hold A for 0.5 seconds
  l+r               press several buttons together
  spam a 10 1.0     press A 10 times, once every 1.0 seconds
  stick left 0 1    push left stick up (x, y from -1 to 1); 'stick left 0 0' centers it
  help / quit"""


async def console(controller: ProController) -> None:
    print(HELP)
    while True:
        try:
            line = await asyncio.to_thread(input, '> ')
        except EOFError:
            return
        words = line.strip().lower().split()
        if not words:
            continue
        try:
            if words[0] in ('quit', 'exit', 'q'):
                return
            if words[0] == 'help':
                print(HELP)
            elif words[0] == 'spam':
                count = int(words[2]) if len(words) > 2 else 10
                interval = float(words[3]) if len(words) > 3 else 1.0
                for i in range(count):
                    await controller.press(*words[1].split('+'))
                    print(f'  pressed {words[1]} ({i + 1}/{count})')
                    await asyncio.sleep(max(0.0, interval - 0.15))
            elif words[0] == 'stick':
                controller.set_stick(words[1], float(words[2]), float(words[3]))
            else:
                duration = float(words[1]) if len(words) > 1 else 0.1
                await controller.press(*words[0].split('+'), duration=duration)
            if not controller.connected:
                print('  (not connected to the Switch right now)')
        except (ValueError, IndexError) as error:
            print(f'  {error}')


async def run(args: argparse.Namespace) -> None:
    async with ProController(args.transport, args.keys) as controller:
        if args.command == 'forget':
            await controller.forget_switches()
            print('Saved pairings deleted. Also remove the controller on the Switch: '
                  'System Settings > Controllers and Sensors > Disconnect Controllers.')
            return
        if args.command == 'pair':
            print('On the Switch open Controllers > Change Grip/Order and leave it there...')
            await controller.pair(timeout=args.timeout)
            print('Paired! Press A on the Switch screen (type "a" below) to finish.')
        else:
            await controller.reconnect(args.switch, timeout=args.timeout or 10.0)
        await console(controller)


def main() -> None:
    parser = argparse.ArgumentParser(prog='python -m bt_controller', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['pair', 'connect', 'forget'])
    parser.add_argument('--transport', default='usb:0',
                        help='Bumble transport for the adapter, e.g. usb:0 or usb:0A12:0001 (default usb:0)')
    parser.add_argument('--keys', default='switch_pairing.json', help='file storing pairing keys')
    parser.add_argument('--switch', help='Switch address to connect to (default: last paired)')
    parser.add_argument('--timeout', type=float, default=None, help='seconds to wait for the Switch')
    parser.add_argument('--log-file', default='bt_controller.log',
                        help='detailed debug log (send this when reporting problems)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG, handlers=[], force=True)
    file_handler = logging.FileHandler(args.log_file, mode='w', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    console_handler.addFilter(lambda record: record.name.startswith('bt_controller'))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().addHandler(console_handler)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except asyncio.TimeoutError:
        sys.exit('Timed out waiting for the Switch.')
    except RuntimeError as error:
        sys.exit(f'Error: {error}')


if __name__ == '__main__':
    main()
