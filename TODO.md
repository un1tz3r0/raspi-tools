# To do

## Front ends

- **Editors in the TUI.** The GUI's read-change-commit editors (users,
  system, services, wifi, config-txt, cmdline, images, changes) exist only in
  the GUI. `frontend/editors.py` holds all their logic and
  `frontend.editors.list_entries` already places them in a command list, so
  the TUI needs only the views: a list or text page per editor, the changed
  marks, Revert, Preview, Commit, and the "commit, discard or stay" question
  when you leave one with changes.
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
