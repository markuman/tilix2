# tilix2

A tilix-style tiling terminal in a single Python file, powered by
[libghostty-vt](https://github.com/mitchellh/ghostty) (the same VT core ghostty uses)
via [pyghostty](https://pypi.org/project/pyghostty/) — not VTE.

One file, no framework acrobatics: `tilix2.py` (~1000 lines) is the whole app.
GTK4 for the UI, cairo/pango for rendering, each tile gets its own pty + ghostty
terminal instance. Kitty keyboard protocol is handled by ghostty's own key encoder,
so `ctrl+enter` in a TUI arrives as `CSI 13;5u` instead of a colliding `\r`.

## Install

```sh
make install        # -> ~/.local/bin/tilix2 with its own venv in ~/.local/share/tilix2/venv
```

or manually:

```sh
python3 -m venv --system-site-packages venv   # --system-site-packages: gi/gtk4 come from the system
./venv/bin/pip install pyghostty
./venv/bin/python tilix2.py
```

System requirements: GTK4 + gobject-introspection (`python3-gi`), a font with pango.

## Usage

- right-click a tile: split right/down, move tile, copy/paste, zoom, close
- shortcuts: `ctrl+alt+right/down` split, `ctrl+shift+w` close tile,
  `ctrl+shift+c/v` copy/paste (bracketed paste aware), `ctrl+shift +/-` zoom (active tile only),
  `ctrl+shift+0` zoom reset
- drag a tile's header onto a neighbor to swap them
- each tile has a header with the cwd and a slim scrollbar on the right (drag = scroll viewport)
- closing the last tile (`ctrl+d`/`exit` in the shell) closes the window

env vars: `TILIX2_SHELL`, `TILIX2_FONT` (e.g. `Monospace 12`), `TILIX2_FG`/`TILIX2_BG` (`#rrggbb`)

## Tests

```sh
python tests/checks.py encoder     # headless: vt/encoder (no display needed)
python tests/checks.py gui         # needs DISPLAY (xvfb-run -a python tests/checks.py gui)
python tests/checks.py selftest    # app-level selftest (needs DISPLAY)
```

## Layout

```
tilix2.py        the app (Win: headerbar + paned-tree, Tile: header+scrollbar+view, TermView: pty+vt+render)
tests/checks.py  encoder | layout | render | scrollbar | perf | selftest
Makefile         install / run / test / clean
```

## Known limitations

- software rendering (cairo on CPU), no GPU path — ~5 ms/frame warm at 1920 cells, fine for typing
- no tabs, no sessions/profiles, no kitty graphics protocol rendering (images pass through as text)
- selection is viewport-anchored and cleared on scroll
