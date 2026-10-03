# Starter shiny hunting bot

Soft resets your FireRed/LeafGreen starter until it's shiny. It uses the
Bluetooth controller in [`bt_controller/`](../bt_controller/README.md) (no
SwiCC) and a capture card. It has no AI model and nothing to train: the bot
learns what your normal starter looks like on the summary screen, then stops
when the colours change.

## The window (easiest)
Double-click **`ShinyBot.pyw`** in the repo folder, or run `python -m shinybot gui`.
The sidebar has three pages: **Hunt** (video, reset counter, Test/Start/Stop,
sequences and regions, log), **Play** (video, on-screen controller, key list)
and **Setup** (connect/pair the controller, camera, sound). The pills in the
top bar show the controller, camera and sound status; click **Sound** to
mute/unmute from any page. Text uses "Pokemon Pixel Font" by SpyroSteak
(CC BY-SA).

In the window:
- **Connect** reconnects to a paired Switch. **Pair…** pairs a new one (open
  Controllers → Change Grip/Order first).
- The **video** shows the capture card. Pick the camera number at the top.
- **Audio** plays the capture card's sound through your PC. Pick the capture
  card's audio input in the list (it guesses the first time; it's often called
  "Digital Audio Interface" or the card's name). **Mute** toggles the sound,
  and you can set the volume. Choose "(off)" to disable audio. All of these are
  remembered.
- **Play from the PC:** click the on-screen buttons, or click the video and
  use the keyboard (the key list is shown in the window). Keys only reach the
  Switch while the window is focused, and everything is released when you
  switch to another app. Input is ignored while the bot is running a test or
  a hunt.
- **Shiny hunt row:** *Test sequence* runs one reset. Then get to the summary
  screen and use *Mark sprite box* and *Mark screen box*: drag the boxes on
  the video. *Start hunt* starts hunting and *Stop* stops it. Edits to
  `shinybot.json` (timings) are picked up when you press Test or Start, with
  no restart needed.
- Keyboard bindings can be changed in `shinybot.json` under `"keys"`.

### Recording a sequence (no guessing timings)
1. Get the game where the sequence starts. For the starter, that's in the lab
   right after saving.
2. Click **● Record**, then play it yourself with the keyboard or the
   on-screen buttons: Soft reset, intro, title, Continue, take the Pokémon,
   decline the nickname, then open the menu, the Pokémon and its Summary.
3. Once the summary screen is fully showing, click **■ Stop recording**.
4. The editor opens with one row per press: buttons, how long you held them,
   and the wait until your next press. The last wait is the time until you
   clicked Stop. Double-click any cell to change it and add a **note** to each
   input (Tab jumps to the next cell). **Merge repeats** turns mashing (e.g.
   12 x B) into one row. Then **Save as…** with a name like "Eevee".
5. The saved sequence becomes the active one in the **Sequence** dropdown.
   **Test sequence** and **Start hunt** use whichever is selected, and
   **Edit…** opens it again.

Tips: press one button at a time (buttons pressed together are recorded as a
chord, which is how the soft reset is recorded). Stick movements aren't
recorded, so use the D-pad. If a step is flaky when replayed, add a little
to its wait.

The rest of this page describes the same steps on the command line.

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
