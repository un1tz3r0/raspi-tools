"""What piimg's front ends share, with no toolkit in it.

The TUI (piimg.tui, Textual) and the GUI (piimg.gui, GTK 4 / libadwaita) are
views over these models:

  fields    FieldDef, one form field, and converting values to and from text
  commands  the CLI's argparse definitions as forms, and form values back to a
            command line
  proc      running `python -m piimg --progress=json --json ...` as a child
            and reading its progress events and tables
  target    describing the open image, overlay or card
  recipe    the recipe editor's document model: YAML round trip, rows, checks,
            undo/redo, marks, clipboard and the list operations

Styled text is rich.text.Text (rich is a core dependency, not a toolkit):
the TUI shows it as is and the GUI turns it into Pango markup.
Nothing here may import textual or gi.
"""
