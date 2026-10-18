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
(CC BY-SA). The panels are see-through over a background picture; change it on
the Setup page (**Choose picture** / **Default**). The default is
`shinybot/assets/background.jpg`. The shiny alert plays
`shinybot/assets/shiny_alert.mp3`; on the Setup page you can choose another
sound (MP3/WAV), **Test** it, or switch to **Beeps**.

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

### Random timing (important in FireRed/LeafGreen)
FRLG has no clock. It picks its random seed from how many frames you spend
on the title screen, and the RNG then advances every frame until you get the
starter. A bot that presses at exactly the same time every reset (this one
repeats to within 1-2 frames) gets **the same few starters over and over** and
may never see a shiny, however long it runs.

So every sequence needs a **Random +** wait: a random extra pause (0 to that
many seconds) picked fresh every reset. Put about 5 s on:
1. the step that waits **on the title screen** (the press that skips the intro
   to the title screen), and
2. a step where you **stand in the lab** before taking the Poke Ball (the
   step that skips the recap).

The default Starter sequence already has both. If your recorded sequence mashes
A through the intro, title screen and menu as one step, split it so one step
ends on the title screen, and give that one the random wait. Start hunt warns
you if the sequence has no random wait. Each reset is about 5 s longer on
average.

**Checking it works (seed check).** The bot can't read the Switch's RNG, but it sees what
the RNG makes: the summary screen shows the starter's nature and gender. Every reset is
compared with the one before (leaving out the sprite, so a shiny doesn't count). Two different
starters show the same screen only when nature and gender happen to agree, about 3% of the
time; resets stuck on the same few seeds would match nearly every time. The Hunt page shows
"new starter: N of last 100", the log has a "seed check" line every 100 resets, and the bot
warns if more than 25% of resets repeat the previous screen. `shinybot_output/resets.csv`
has one row per reset: the random waits it picked, the verdict and the comparison.

How many seeds the waits reach: in the emulator every frame on the title screen gives a
different seed (jumps of 18,800-27,400 per frame), so 5 s of random wait is about 300 seeds.
On a real Switch it's more random than that: the timing test (below) gave 17 different
starters in 30 resets with identical timing, and two frame-perfect new games gave TIDs ~4,000
apart. The Switch's timer doesn't land on the same count twice, so every reset gets an
effectively random seed and the odds are the usual 1 in 8,192 for any trainer. The random
waits are kept anyway: they cost a few seconds a reset and rule out any leftover pattern.

**Timing test (before aiming at a planned shiny).** Sequence > **Timing** (or `python -m
shinybot timing-test --resets 30`) plays the active sequence with every random wait fixed at its
middle and every press at an exact time after the soft reset, then groups the summary screens.
A few starters repeating means the Switch gives the same starter for the same timing, so
aiming at a planned shiny can work; a different starter nearly every time means it can't, and
the normal hunt is the way. (On the Switch tested so far: 17 different in 30, so it can't.) A picture of each different starter is saved as
`shinybot_output/timing_starter_N.png`. Start it from your hunting save, as for a hunt.

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
1. Around the **Pokémon sprite**. It's worth stretching it a little to the
   right of the picture's top corner: that's where the game draws a small
   **shiny star** (only for shinies), which makes a shiny even easier to spot.
2. Around something that is **always the same** on the summary screen, like
   the title bar. Don't include the sprite, the star spot, name, gender or
   nature, because those change.

## 3. Hunt
```
python -m shinybot hunt
```
- The first 3 resets are calibration: the bot learns the normal colours.
  Screenshots are saved as `shinybot_output/calibration_*.png`, so check
  they show the summary screen.
- After that, every reset is checked. The latest screenshot is always in
  `shinybot_output/last_check.png`.
- **On a shiny** the bot stops pressing buttons, plays the alert and saves
  `SHINY_reset_N.png`. Take over with your own controller and save. Before
  stopping it looks at the screen twice more (half a second), so a one-frame
  capture glitch doesn't stop the hunt; those are saved as
  `unconfirmed_N.png` and the hunt carries on.
- The calibration measures how noisy your capture card is and sets the
  thresholds from that, so subtle shinies (only a few colours change) are
  still caught on a clean capture.
- If 3 resets in a row miss the summary screen, it stops so it doesn't reset
  forever while stuck. Run `test-sequence` again to find which step drifts.
- If the Switch disconnects, the bot reconnects by itself.
- Total resets are kept in `shinybot_output/stats.json` across sessions.
- **Black screen and the log just stops?** The bot froze or was closed in the
  middle of the soft reset, so the Switch still thinks A+B+PLUS+MINUS are
  held and the game keeps resetting. Closing the bot (or pressing a button on
  a real controller) frees it. The log says `ShinyBot started` and `window
  closed`, and if the bot's loop gets stuck it logs where (`loop has been
  stuck`). Crashes go to `shinybot_output/crash.log`. Send those if it
  happens.

Expect about 120 resets an hour. Starters are 1 in 8192 (full odds), so
leave it running; it can take days.

## If it closes by itself
Everything is written to `shinybot_output/`:
- `hunt.log`: what the bot did, with times; every error with its traceback; every 30 minutes
  how long it has run, its reset count and memory use.
- `crash.log`: only the bad news: errors, crashes inside a driver (OpenCV, the capture card,
  Bluetooth), and runs that vanished.
- `session.json`: rewritten every 30 seconds while the bot runs, closed when it exits normally.
  If the next start finds it still open, the bot shows what it was doing and when it was last
  alive: no Python error stopped it, so Windows ended it (shutdown or an update restart, out of
  memory, Task Manager) or it crashed in a driver (then `crash.log` has a dump). Windows Event
  Viewer (Windows Logs > System / Application) around that time usually says which.
- A heartbeat minutes late means the PC was asleep or the bot was frozen; the log says when.

While a hunt runs the bot asks Windows not to sleep (the screen may still turn off). Windows
Update can still restart the PC: pause updates while you hunt overnight.

## Timed new game: finding your secret ID (SID)
Shininess depends on your trainer ID (TID, on the trainer card) **and** a secret ID (SID)
the game never shows. To plan a shiny with the emulator's RNG tools you need both. The
game makes them like this:
- leaving the naming screen sets your TID from a timer;
- the random numbers then step every frame until Oak's speech ends;
- the next value is your SID.

A person can't time that to the frame, so the bot plays the new game on a schedule worked
out in the emulator (`sloop-emu-pc`):

1. On the PC, with the game files, make the plan (10-20 minutes; it plays the new game in
   the emulator a few dozen times to check the schedule):
   ```
   python -m sloop.newgame plan --save game/save/LeafGreen_e.sav --name Naomi --gender girl --rival 1
   ```
   `--rival` is 1-4 for the preset names (LeafGreen: RED, ASH, KENE, GEKI) or a name to type.
2. On the Switch: text speed **FAST** (START menu > OPTION), saved. The bot starts a
   **NEW GAME**. Your old save stays until you save in the new game (the game asks before
   overwriting).
3. Run it (window: Hunt page > New game (SID) > Timed new game, or):
   ```
   python -m shinybot new-game --plan newgame_plan.json
   ```
   It soft resets, then presses every button at its planned moment, counted from the
   title-screen press (so small delays never add up). Don't touch the Switch while it runs.
   It ends on the trainer card and saves a picture of it.
4. Type the IDNo. it shows. You get the possible SIDs, most likely first. The plan also
   checks the bot's timing: it knows which TID each "OK pressed a few frames early/late"
   would give, so the TID tells how many frames off the bot really was:
   ```
   python -m shinybot sid --plan newgame_plan.json --tid 12345
   ```
5. In the emulator's launcher (Trainer tab), make a save with that name, gender, TID and
   SID, and plan the shiny starter on the RNG tab.
