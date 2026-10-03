# FRLG Shiny Hunting Bot

Automatically shiny hunts in Pokémon FireRed & LeafGreen on a **real Nintendo Switch** — no emulator, no cheats, no custom firmware. A capture card reads the screen, the app checks the pixels, and a SwiCC presses the buttons.

## What you need
- A capture card connected to your Switch
- A SwiCC (plugs into your PC via USB, and into the Switch as a wired controller)
- Windows PC

## Download
Grab the latest version from the https://github.com/alephzerogaming-rgb/FRLG-shiny-hunting-bot/releases page.

## Setup (short version)
1. Run the setup app — it detects your devices and reads your game's resolution.
2. Open the hunting app, set your colors with the color picker, and configure the bot parameters. Every option has a help icon.
3. Press start.

A full video walkthrough is on my YouTube channel.

## Note
These are unsigned `.exe` files, so Windows SmartScreen may show a warning ("Windows protected your PC"). Click **More info → Run anyway**. Some antivirus may also flag automation tools — this is normal.

## Experimental: no SwiCC (Bluetooth)
- [`bt_controller/`](bt_controller/README.md) turns a cheap USB Bluetooth adapter into a wireless Pro Controller driven from Python.
- [`shinybot/`](shinybot/README.md) uses it with a capture card to soft reset your starter until it's shiny.

## Links
- YouTube: https://youtube.com/@alephzerogaming
- Discord: https://discord.gg/njNmgdNtb8
- Patreon: https://patreon.com/AlephZeroGaming