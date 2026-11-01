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
  `ctrl+shift+c/v` copy/paste, `ctrl+shift +/-/0` zoom/reset (active tile only),
  `ctrl+shift+t` new tab, `ctrl+pgup/pgdn` previous/next tab
- tabs: the tab bar shows up from 2 tabs on; closing the last tile of a tab closes the tab,
  the last tab closes the window. a new tab starts in the cwd of the active tile
- dark theme (headerbar, menus, tile headers)
- select with the mouse (shift+drag inside mouse-grabbing apps) -> primary selection,
  middle-click pastes it. selections survive scrolling.
- pastes are sanitized by ghostty (no escape out of bracketed paste)
- drag a tile's header onto another tile: the half nearest to the cursor lights up,
  the tile gets moved there (left/right/top/bottom of the target)
- a new split starts in the cwd of the active tile
- each tile has a header with its cwd and a slim scrollbar
- closing a tile sends SIGHUP to its shell (bash saves its history); a shell exiting
  closes its tile, the last tile closes the window
- kitty graphics protocol (e.g. `kitten icat`): images via direct, file and
  shared memory transfer, png/rgb/rgba/gray, z-layers below/above text. the pty reports its
  size in pixels, `CSI 14/16/18 t` get answered. 64 MB of images per tile; png only (no other
  image formats sneak in through gdk), size checked from the header before decoding
- a program that doesn't read (stopped, `ctrl+s`) doesn't freeze the window on a big paste,
  output floods (`yes`) don't stop redrawing
- every start is its own process (no D-Bus single instance)
- `tilix2 --version`

env vars: `TILIX2_SHELL` (a single executable; falls back to `/bin/sh` if it doesn't exist),
`TILIX2_FONT` (e.g. `Monospace 12`, without a size 11 pt), `TILIX2_FG`/`TILIX2_BG` (`#rrggbb`)

## Tests

```sh
python tests/checks.py encoder            # headless: key encoder / vt (no display needed)
xvfb-run -a python tests/checks.py        # everything
xvfb-run -a python tests/checks.py render # one gui check: layout|tabs|dnd|input|clipboard|render|images|robust|scrollbar|perf
```

the gui checks use x11 when `DISPLAY` is set (xvfb), `dnd` needs x11 (it drives the real mouse via XTest).

## Known limitations

- software rendering (cairo on CPU) — fine for typing; cached pango layouts per row,
  converted images are cached per image
- kitty graphics: no unicode placeholders (virtual placements: tmux, probably yazi),
  no animation, no temp-file transfer (that option is broken in the libghostty-vt
  bundled with pyghostty 0.1.4; icat falls back to file/shm/direct)
- tabs can't be reordered; tiles can't be dragged to another tab, the only tile of a tab
  can't be dragged at all
- `ctrl+pgup/pgdn` switch tabs, programs never see them (same as tilix)
- no sessions/profiles, no cursor blinking, no hyperlinks, no OSC 52 clipboard
