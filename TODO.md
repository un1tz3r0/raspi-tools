# To do

## Front ends

- **Click through the GUI on a real card.** The root path itself was run
  on a card on 2026-11-14, with the command lines the GUI builds: pkexec and
  the polkit launcher, one password per five minutes, `info` and `users` as
  root through an overlay, the overlay given back to the user, and Stop
  ending a root `diff` at once. Not yet done from the GUI's own windows
  (open a card with an overlay, run, Stop, an editor's Commit to the overlay).
  The older pkexec path without the launcher (`pkexec env ...`) was only run
  as far as polkit's dialog.
- **Press the GUI's keyboard shortcuts for real in a test.** The GUI test
  driver calls the actions directly; only the TUI's keys are pressed for
  real (through Textual's pilot).

## Recipes

- **Try `waveshare-35dsi` on the panel.** Checked only on an image file
  (2026-11-14): the overlay lands in the boot partition and config.txt and
  cmdline.txt get the wiki's lines. Still to see on a Pi: picture and touch on
  DSI1, `port=dsi0` on a Pi 5, and whether the console `rotation` shows on
  `DSI-1` or `DSI-2`. Desktop rotation (labwc/kanshi, and touch that follows
  it) is not in the recipe yet. Do that once it is known which files Control
  Centre > Screens writes.
