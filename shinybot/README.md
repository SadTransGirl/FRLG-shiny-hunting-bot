# Starter shiny hunting bot

Soft resets your FireRed/LeafGreen starter until it's shiny. It uses the
Bluetooth controller in [`bt_controller/`](../bt_controller/README.md) (no
SwiCC) and a capture card. It has no AI model and nothing to train: the bot
learns what your normal starter looks like on the summary screen, then stops
when the colours change.

## Before you start
1. Pair the virtual controller once (see `bt_controller/README.md`) and check
   `python -m bt_controller connect` works.
2. Install the requirements again, since this adds OpenCV:
   `pip install -r requirements.txt`
3. **In the game:**
   - Set **Options → Text Speed → Fast**.
   - In Oak's lab, stand **in front of the Poké Ball you want, facing it**.
   - **Save.**

## The capture card
Only one program can use a capture card at a time. Either:
- **close OBS** while the bot runs, or
- in OBS click **Start Virtual Camera**, and give the bot that camera number
  instead. You can keep watching in OBS this way.

Find the right camera number:
```
python -m shinybot cameras
python -m shinybot preview --camera 1      (Q closes the window)
```
`--camera` is remembered in `shinybot.json`.

## 1. Check the button sequence
```
python -m shinybot test-sequence
```
This runs one soft reset through to the starter's summary screen and saves a
screenshot after every step in `shinybot_output/test_sequence/`. Watch it, or
look through the screenshots afterwards. It should end on the **summary
screen** of your starter.

If a step goes wrong, edit `shinybot.json`. Every step has `press`, `hold`,
`wait` (seconds afterwards) and `repeat`, so make the `wait` longer or change
`repeat`. Example: if the intro hasn't finished when the bot presses PLUS,
raise that step's `wait`.

## 2. Mark the screen regions
With the game on the summary screen (right after `test-sequence` is perfect):
```
python -m shinybot setup
```
Press SPACE to take the picture, then draw two boxes:
1. Around the **Pokémon sprite** only.
2. Around something that is **always the same** on the summary screen, like
   the title bar. Don't include the sprite, name, gender or nature, because
   those change.

## 3. Hunt
```
python -m shinybot hunt
```
- The first 3 resets are calibration: the bot learns the normal colours.
  Screenshots are saved as `shinybot_output/calibration_*.png`, so check
  they show the summary screen.
- After that, every reset is checked. The latest screenshot is always in
  `shinybot_output/last_check.png`.
- **On a shiny** the bot stops pressing buttons, beeps and saves
  `SHINY_reset_N.png`. Take over with your own controller and save.
- If 3 resets in a row miss the summary screen, it stops so it doesn't reset
  forever while stuck. Run `test-sequence` again to find which step drifts.
- If the Switch disconnects, the bot reconnects by itself.
- Total resets are kept in `shinybot_output/stats.json` across sessions.

Expect about 120 resets an hour. Starters are 1 in 8192 (full odds), so
leave it running; it can take days.
