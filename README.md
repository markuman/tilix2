# tilix2

A tilix-style tiling terminal in a single Python file, powered by
[libghostty-vt](https://github.com/ghostty-org/ghostty) (the same VT core ghostty uses)
via [pyghostty](https://pypi.org/project/pyghostty/) — not VTE.

One file, no framework acrobatics: `tilix2.py` is the whole app. GTK4 for the UI,
cairo/pango for rendering from ghostty's render state (only dirty rows get rebuilt),
each tile gets its own pty + ghostty terminal. Keys go through ghostty's key encoder
(kitty keyboard protocol, so `ctrl+enter` in a TUI arrives as `CSI 13;5u`) and through
a GTK input method first (dead keys, compose, ibus).

## Install

```sh
make install        # -> ~/.local/bin/tilix2 with its own venv in ~/.local/share/tilix2/venv
```

or manually:

```sh
python3 -m venv --system-site-packages venv   # --system-site-packages: gi/gtk4 come from the system
./venv/bin/pip install pyghostty==0.1.4
./venv/bin/python tilix2.py
```

System requirements: GTK4 + gobject-introspection (`python3-gi`), pycairo, a monospace font.

## Usage

- right-click a tile: split right/down, move tile, copy/paste, zoom, close
  (shift+right-click when the app grabbed the mouse)
- shortcuts: `ctrl+alt+right/down` split, `ctrl+shift+w` close tile,
  `ctrl+shift+c/v` copy/paste, `ctrl+shift +/-/0` zoom/reset (active tile only)
- select with the mouse (shift+drag inside mouse-grabbing apps) -> primary selection,
  middle-click pastes it. selections survive scrolling.
- pastes are sanitized by ghostty (no escape out of bracketed paste)
- drag a tile's header onto its neighbor to swap them
- a new split starts in the cwd of the active tile
- each tile has a header with its cwd and a slim scrollbar
- closing a tile sends SIGHUP to its shell (bash saves its history); a shell exiting
  closes its tile, the last tile closes the window
- every start is its own process (no D-Bus single instance)

env vars: `TILIX2_SHELL`, `TILIX2_FONT` (e.g. `Monospace 12`), `TILIX2_FG`/`TILIX2_BG` (`#rrggbb`)

## Tests

```sh
python tests/checks.py encoder            # headless: key encoder / vt (no display needed)
xvfb-run -a python tests/checks.py        # everything
xvfb-run -a python tests/checks.py render # one gui check: layout|input|clipboard|render|scrollbar|perf
```

## Known limitations

- software rendering (cairo on CPU) — fine for typing; cached pango layouts per row
- no tabs, no sessions/profiles, no kitty graphics, no cursor blinking, no hyperlinks
