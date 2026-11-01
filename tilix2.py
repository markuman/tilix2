#!/usr/bin/env python3
# tilix2.py — tilix-style tiling terminal: gtk4 paned tree, every tile = pty + libghostty-vt
# (via pyghostty). drawn with cairo/pango from ghostty's render state (only dirty rows get
# rebuilt), keys go through ghostty's key encoder (kitty keyboard protocol). kitty graphics:
# ghostty keeps images + placements, we only draw them (kitten icat works).
#
# start:  python3 -m venv --system-site-packages venv && ./venv/bin/pip install pyghostty==0.1.4
#         ./venv/bin/python tilix2.py      (--system-site-packages: gi/gtk4 come from the system)
# env:    TILIX2_SHELL, TILIX2_FONT ("Monospace 12"), TILIX2_FG / TILIX2_BG ("#rrggbb")
# keys:   ctrl+alt+right/down split, ctrl+shift+w close, ctrl+shift+c/v copy/paste,
#         ctrl+shift +/-/0 zoom, ctrl+shift+x maximize/restore tile, ctrl+shift+t new tab,
#         ctrl+pgup/pgdn switch tab.
#         right-click = menu, drag a tile header = move the tile
# license: WTFPL v2 — do what the fuck you want to (see LICENSE)
import os, sys, pty, shlex, struct, fcntl, termios, shutil, subprocess, traceback, unicodedata

VERSION = '1.0.0'
if __name__ == '__main__' and '--version' in sys.argv[1:]:   # before gi/pyghostty: works without display/venv
    sys.exit(print(f'tilix2 {VERSION}'))

import cairo

try:
    import gi
except ModuleNotFoundError:
    sys.exit('PyGObject (gi) is missing — recreate the venv with `python3 -m venv --system-site-packages venv` '
             'or use the system python with python3-gi/pygobject.')
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, Gio, GLib, GObject, Pango, PangoCairo

from pyghostty import Terminal, lib, ffi
from pyghostty._ffi import union_fn
from pyghostty.core import init_sized

# ghostty_terminal_scroll_viewport takes a tagged union by value -> ABI twin like in pyghostty._ffi
ffi.cdef('typedef struct { uint32_t tag; uint32_t _pad; int64_t delta; int64_t _pad2; } ScrollViewportS;')
if ffi.sizeof('ScrollViewportS') != ffi.sizeof('GhosttyTerminalScrollViewport'):
    sys.exit('ABI mismatch: ScrollViewportS does not match GhosttyTerminalScrollViewport')
_scroll_viewport = union_fn('ghostty_terminal_scroll_viewport', 'GhosttyResult(*)(GhosttyTerminal, ScrollViewportS)')

# gdk keyval name (= x11 keysym) -> GHOSTTY_KEY_*
KEYMAP = {
    'Return': 'ENTER', 'KP_Enter': 'NUMPAD_ENTER', 'Escape': 'ESCAPE', 'BackSpace': 'BACKSPACE',
    'Tab': 'TAB', 'ISO_Left_Tab': 'TAB', 'space': 'SPACE',
    'Left': 'ARROW_LEFT', 'Right': 'ARROW_RIGHT', 'Up': 'ARROW_UP', 'Down': 'ARROW_DOWN',
    'Home': 'HOME', 'End': 'END', 'Prior': 'PAGE_UP', 'Page_Up': 'PAGE_UP',
    'Next': 'PAGE_DOWN', 'Page_Down': 'PAGE_DOWN', 'Insert': 'INSERT', 'Delete': 'DELETE',
    'Pause': 'PAUSE', 'Print': 'PRINT_SCREEN', 'Menu': 'CONTEXT_MENU', 'Help': 'HELP',
    'KP_Add': 'NUMPAD_ADD', 'KP_Subtract': 'NUMPAD_SUBTRACT', 'KP_Multiply': 'NUMPAD_MULTIPLY',
    'KP_Divide': 'NUMPAD_DIVIDE', 'KP_Decimal': 'NUMPAD_DECIMAL', 'KP_Separator': 'NUMPAD_SEPARATOR',
    'KP_Equal': 'NUMPAD_EQUAL', 'KP_Left': 'NUMPAD_LEFT', 'KP_Right': 'NUMPAD_RIGHT',
    'KP_Up': 'NUMPAD_UP', 'KP_Down': 'NUMPAD_DOWN', 'KP_Home': 'NUMPAD_HOME', 'KP_End': 'NUMPAD_END',
    'KP_Page_Up': 'NUMPAD_PAGE_UP', 'KP_Page_Down': 'NUMPAD_PAGE_DOWN',
    'KP_Insert': 'NUMPAD_INSERT', 'KP_Delete': 'NUMPAD_DELETE', 'KP_Begin': 'NUMPAD_BEGIN',
    'minus': 'MINUS', 'equal': 'EQUAL', 'comma': 'COMMA', 'period': 'PERIOD', 'slash': 'SLASH',
    'semicolon': 'SEMICOLON', 'apostrophe': 'QUOTE', 'grave': 'BACKQUOTE', 'backslash': 'BACKSLASH',
    'bracketleft': 'BRACKET_LEFT', 'bracketright': 'BRACKET_RIGHT',
}
for _c in '0123456789': KEYMAP[_c] = f'DIGIT_{_c}'
for _c in 'abcdefghijklmnopqrstuvwxyz': KEYMAP[_c] = KEYMAP[_c.upper()] = _c.upper()
for _i in range(1, 36): KEYMAP[f'F{_i}'] = f'F{_i}'
for _i in range(10): KEYMAP[f'KP_{_i}'] = f'NUMPAD_{_i}'

MODIFIER_KEYS = {'Shift_L', 'Shift_R', 'Control_L', 'Control_R', 'Alt_L', 'Alt_R', 'AltGr',
                 'ISO_Level3_Shift', 'Super_L', 'Super_R', 'Meta_L', 'Meta_R', 'Hyper_L', 'Hyper_R',
                 'Caps_Lock', 'Num_Lock', 'Scroll_Lock'}

MT = Gdk.ModifierType
SHIFT, CTRL, ALT = 1, 2, 4      # ghostty mod bits: shift=1 ctrl=2 alt=4 super=8 hyper=16 meta=32 caps=64

def ghostty_mods(state):
    m = 0
    for mask, bit in ((MT.SHIFT_MASK, 1), (MT.CONTROL_MASK, 2), (MT.ALT_MASK, 4), (MT.SUPER_MASK, 8),
                      (MT.HYPER_MASK, 16), (MT.META_MASK, 32), (MT.LOCK_MASK, 64)):
        if state & mask: m |= bit
    return m

def hexrgb(s, default):
    "'#rrggbb' -> (r,g,b); on nonsense input (red, #fff, empty) the default instead of a traceback."
    s = (s or '').lstrip('#')
    try:
        if len(s) != 6: raise ValueError
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default

def base_keyval(keycode, keyval):
    "keyval of the physical key without modifiers (de: shift+0 -> '0' instead of '='). fallback: lowercase."
    ok, kv, *_ = Gdk.Display.get_default().translate_key(keycode, 0, 0) if keycode else (False, 0)
    return kv if ok else Gdk.keyval_to_lower(keyval)

def rgb(c):
    "GhosttyColorRgb -> cairo tuple"
    return (c.r / 255, c.g / 255, c.b / 255)

TERM_NAME = 'xterm-ghostty' if shutil.which('infocmp') and subprocess.run(
    ['infocmp', 'xterm-ghostty'], capture_output=True).returncode == 0 else 'xterm-256color'
def sized_font(s):
    "font string with a size: 'Monospace' alone would make the first zoom-in jump to 6pt."
    d = Pango.FontDescription.from_string(s)
    if not d.get_size(): d.set_size(11 * Pango.SCALE)
    return d.to_string()

DEFAULT_FONT = sized_font(os.environ.get('TILIX2_FONT', 'Monospace 11'))
TERM_FG = hexrgb(os.environ.get('TILIX2_FG'), (212, 212, 212))
TERM_BG = hexrgb(os.environ.get('TILIX2_BG'), (24, 26, 32))
MOUSE_BTN = {1: lib.GHOSTTY_MOUSE_BUTTON_LEFT, 2: lib.GHOSTTY_MOUSE_BUTTON_MIDDLE,
             3: lib.GHOSTTY_MOUSE_BUTTON_RIGHT}

# kitty graphics: pixel format of a stored image -> (gdk memory format, bytes per pixel).
# png never shows up here, ghostty stores it decoded as rgba (via _decode_png below).
KITTY_FMT = {lib.GHOSTTY_KITTY_IMAGE_FORMAT_RGB: (Gdk.MemoryFormat.R8G8B8, 3),
             lib.GHOSTTY_KITTY_IMAGE_FORMAT_RGBA: (Gdk.MemoryFormat.R8G8B8A8, 4),
             lib.GHOSTTY_KITTY_IMAGE_FORMAT_GRAY: (Gdk.MemoryFormat.G8, 1),
             lib.GHOSTTY_KITTY_IMAGE_FORMAT_GRAY_ALPHA: (Gdk.MemoryFormat.G8A8, 2)}
# bytes of images per tile. kitty's 320 MB are per window, here every tile has its own storage
# (+ a drawn copy as cairo surface per visible image) -> deliberately smaller.
KITTY_STORAGE = 64_000_000
KITTY_Z_BELOW_BG = -(2 ** 31) // 2   # z below this: under the cell backgrounds, below 0: under the text

@ffi.callback('GhosttySysDecodePngFn')
def _decode_png(userdata, alloc, data, n, out):
    "png bytes -> tightly packed rgba in a buffer from ghostty's allocator (ghostty owns it afterwards)."
    raw = ffi.buffer(data, n)[:]
    # untrusted input from any program in the terminal. gdk sniffs the format itself, so only
    # real png gets through (no jpeg/tiff/svg loaders), and the size comes from the IHDR header
    # *before* decoding: a 1 kb png can claim 30000x30000 = 3.6 gb of rgba (decompression bomb).
    if len(raw) < 24 or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR': return False
    w, h = struct.unpack('>II', raw[16:24])
    if not (w and h) or w * h * 4 > KITTY_STORAGE: return False
    try: tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
    except GLib.Error: return False
    if (tex.get_width(), tex.get_height()) != (w, h): return False
    dl = Gdk.TextureDownloader.new(tex)
    dl.set_format(Gdk.MemoryFormat.R8G8B8A8)
    px, stride = dl.download_bytes()
    px = px.get_data()
    if stride != w * 4: px = b''.join(px[i * stride:i * stride + w * 4] for i in range(h))
    buf = lib.ghostty_alloc(alloc, len(px))
    if buf == ffi.NULL: return False      # over ghostty's per-image memory limit
    ffi.memmove(buf, px, len(px))
    out.width, out.height, out.data, out.data_len = w, h, buf, len(px)
    return True
lib.ghostty_sys_set(lib.GHOSTTY_SYS_OPT_DECODE_PNG, _decode_png)


# box drawing + block elements are drawn on the cell grid ourselves (like kitty): font glyphs
# don't fill the cell (gaps in vertical lines) and tuis build their borders from them.
def _box_lines():
    """U+2500-257F from their unicode names, e.g. 'DOWN LIGHT AND RIGHT HEAVY' ->
    {char: ((left, right, up, down) with 0 none / 1 light / 2 heavy, rounded)}.
    dashes, diagonals and double lines stay with the font."""
    weights = {'LIGHT': 1, 'SINGLE': 1, 'HEAVY': 2}
    dirs = {'LEFT': (0,), 'RIGHT': (1,), 'UP': (2,), 'DOWN': (3,), 'HORIZONTAL': (0, 1), 'VERTICAL': (2, 3)}
    out = {}
    for cp in range(0x2500, 0x2580):
        name = unicodedata.name(chr(cp), '').removeprefix('BOX DRAWINGS ')
        if any(s in name for s in ('DASH', 'DIAGONAL', 'DOUBLE')): continue
        default = weights.get(name.split()[0], 1)          # 'LIGHT DOWN AND RIGHT': for every arm
        arms = [0, 0, 0, 0]
        for part in name.split(' AND '):                     # 'DOWN LIGHT', 'RIGHT HEAVY': per arm
            w = next((weights[s] for s in part.split() if s in weights), default)
            for s in part.split():
                for i in dirs.get(s, ()): arms[i] = w
        out[chr(cp)] = (tuple(arms), 'ARC' in name)
    return out

def _blocks():
    "U+2580-259F: char -> rects (x, y, w, h) as fractions of the cell."
    b = {'▀': [(0, 0, 1, .5)], '▐': [(.5, 0, .5, 1)], '▔': [(0, 0, 1, 1 / 8)], '▕': [(7 / 8, 0, 1 / 8, 1)]}
    for i, c in enumerate('▁▂▃▄▅▆▇█', 1): b[c] = [(0, 1 - i / 8, 1, i / 8)]
    for i, c in enumerate('▏▎▍▌▋▊▉', 1): b[c] = [(0, 0, i / 8, 1)]
    q = {'UL': (0, 0, .5, .5), 'UR': (.5, 0, .5, .5), 'LL': (0, .5, .5, .5), 'LR': (.5, .5, .5, .5)}
    for c, parts in (('▘', 'UL'), ('▝', 'UR'), ('▖', 'LL'), ('▗', 'LR'), ('▚', 'UL LR'), ('▞', 'UR LL'),
                     ('▙', 'UL LL LR'), ('▛', 'UL UR LL'), ('▜', 'UL UR LR'), ('▟', 'UR LL LR')):
        b[c] = [q[p] for p in parts.split()]
    return b

BOX_LINES, BLOCKS, SHADES = _box_lines(), _blocks(), {'░': .25, '▒': .5, '▓': .75}
BOX_CHARS = BOX_LINES.keys() | BLOCKS.keys() | SHADES.keys()

def draw_box(cr, c, x, y, w, h):
    "box/block char c into the cell at x,y (w x h px) in the current source color, snapped to pixels."
    if c in SHADES:
        cr.save(); cr.rectangle(x, y, w, h); cr.clip(); cr.paint_with_alpha(SHADES[c]); cr.restore()
    elif c in BLOCKS:
        for fx, fy, fw, fh in BLOCKS[c]:
            x1, y1 = x + round(fx * w), y + round(fy * h)
            cr.rectangle(x1, y1, x + round((fx + fw) * w) - x1, y + round((fy + fh) * h) - y1)
        cr.fill()
    else:
        arms, arc = BOX_LINES[c]
        light = max(1, h // 16)
        cx, cy = x + w // 2, y + h // 2                      # lines run through these pixels
        if arc:                                              # ╭╮╯╰: curve from the vertical to the horizontal edge
            px, py = cx + light % 2 / 2, cy + light % 2 / 2  # stroke centre (odd widths sit on .5)
            cr.set_line_width(light)
            cr.move_to(px, y + h if arms[3] else y)
            cr.curve_to(px, py, px, py, x + w if arms[1] else x, py)
            cr.stroke()
            return
        for i, weight in enumerate(arms):                    # each arm from the edge into the centre
            if not weight: continue
            t = light if weight == 1 else 2 * light + 1
            a = t // 2
            if i == 0: cr.rectangle(x, cy - a, cx - x + t - a, t)
            elif i == 1: cr.rectangle(cx - a, cy - a, x + w - cx + a, t)
            elif i == 2: cr.rectangle(cx - a, y, t, cy - y + t - a)
            else: cr.rectangle(cx - a, cy - a, t, y + h - cy + a)
        cr.fill()


class Tile(Gtk.Box):
    "tile: header (pwd, drag it to move the tile) on top, TermView in the middle, slim scrollbar on the right."

    def __init__(self, win, tab, cwd=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.win, self.tab = win, tab
        head = Gtk.Box(spacing=6, margin_top=2, margin_bottom=2, margin_start=6, margin_end=6,
                       css_classes=['tilix2-head'])
        head.append(Gtk.Image(icon_name='open-menu-symbolic', pixel_size=12))
        self.pwd_lbl = Gtk.Label(label='~', xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        head.append(self.pwd_lbl)
        self.max_btn = Gtk.Button(css_classes=['flat'], focus_on_click=False)
        self.max_btn.connect('clicked', lambda b: win.toggle_maximize(self))
        self.set_maximized(False)
        head.append(self.max_btn)
        self.append(head)
        self.adj = Gtk.Adjustment()
        self._sbar_handler = self.adj.connect('value-changed', self._on_sbar)
        self._sb = None             # last (total, offset, len) -> only touch it on change
        self.sbar = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=self.adj, visible=False)
        self.sbar.add_css_class('tilix2-slim')
        self.view = TermView(self, cwd)
        body = Gtk.Box(vexpand=True)
        body.append(self.view)
        body.append(self.sbar)
        self.append(body)
        # drag&drop: the tile itself is what gets dragged (GObject value, only works within the process).
        # preload: the value is already there in 'motion' -> we know who is being dragged.
        ds = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        ds.connect('prepare', lambda src, x, y: Gdk.ContentProvider.new_for_value(self))
        head.add_controller(ds)
        self.drop_zone = None       # 'left'/'right'/'top'/'bottom' while a tile is dragged over this one
        dt = Gtk.DropTarget.new(Tile, Gdk.DragAction.MOVE)     # 1st arg is a GType, not a mime list
        dt.set_preload(True)
        dt.connect('motion', self._drop_motion)
        dt.connect('leave', lambda dt: self._set_zone(None))
        dt.connect('drop', self._drop)
        self.add_controller(dt)
        GLib.timeout_add(1000, self._tick)

    def set_maximized(self, on):
        "header button: maximize <-> restore."
        self.max_btn.set_icon_name('view-restore-symbolic' if on else 'view-fullscreen-symbolic')
        self.max_btn.set_tooltip_text(('restore tiles' if on else 'maximize tile') + ' (ctrl+shift+x)')

    def zone_at(self, x, y):
        "edge nearest to the cursor (tilix behavior): that's where the dragged tile lands."
        fx, fy = x / max(1, self.get_width()), y / max(1, self.get_height())
        return min((fx, 'left'), (1 - fx, 'right'), (fy, 'top'), (1 - fy, 'bottom'))[1]

    def _set_zone(self, zone):
        if zone != self.drop_zone:
            self.drop_zone = zone
            self.view.queue_draw()

    def _drop_motion(self, dt, x, y):
        src = dt.get_value()
        if src is None or src is self or src.tab is not self.tab:   # not loaded yet / onto itself
            self._set_zone(None)
            return 0
        self._set_zone(self.zone_at(x, y))
        return Gdk.DragAction.MOVE

    def _drop(self, dt, src, x, y):
        self._set_zone(None)
        if src is self or src.tab is not self.tab: return False
        self.win.move_tile(src, self, self.zone_at(x, y))
        return True

    def sync_sbar(self):
        "scrollbar = ghostty's scrollbar data (total/offset/len in rows). ghostty says: poll it."
        if self.view.dead: return
        sb = ffi.new('GhosttyTerminalScrollbar*')
        lib.ghostty_terminal_get(self.view.term._t[0], lib.GHOSTTY_TERMINAL_DATA_SCROLLBAR, sb)
        vals = (sb.total, sb.offset, sb.len)
        if vals == self._sb: return
        self._sb = vals
        self.sbar.set_visible(sb.total > sb.len)
        with self.adj.handler_block(self._sbar_handler):   # otherwise configure() scrolls the viewport
            self.adj.configure(sb.offset, 0, sb.total, 1, sb.len, sb.len)

    def _on_sbar(self, adj):
        "scrollbar dragged -> set the viewport top absolutely (ROW, in rows from the start of scrollback)."
        self.view.scroll(lib.GHOSTTY_SCROLL_VIEWPORT_ROW, int(adj.get_value()))

    def _tick(self):
        "pwd in the header (once a second is enough)."
        if self.view.dead: return False
        try: cwd = self.view.cwd()
        except OSError: return True
        home = os.path.expanduser('~')
        lbl = '~' + cwd[len(home):] if cwd == home or cwd.startswith(home + '/') else cwd
        if lbl != self.pwd_lbl.get_label(): self.pwd_lbl.set_label(lbl)
        return True


class TermView(Gtk.DrawingArea):
    "terminal area: pty + shell, ghostty terminal, input encoders, rendering from the render state."

    def __init__(self, tile, cwd=None):
        super().__init__(hexpand=True, vexpand=True, focusable=True)   # without expand it stays 0x0
        self.tile, self.win = tile, tile.win
        self.dead = False
        self.title = ''
        self.cols, self.rows = 80, 24
        self._grid = None       # last (cols, rows, cell_w, cell_h) given to ghostty + pty
        self.term = Terminal(cols=80, rows=24, fg=TERM_FG, bg=TERM_BG)
        self.term.on_reply(self.write)      # replies (DA, cursor report, ...) go back into the pty
        t = self.term._t[0]
        # size reports (CSI 14/16/18 t): ghostty asks us, without the callback it stays silent
        self._size_cb = ffi.callback('GhosttyTerminalSizeFn', self._size_report)
        lib.ghostty_terminal_set(t, lib.GHOSTTY_TERMINAL_OPT_SIZE, self._size_cb)
        # kitty graphics: storage + transfer media file and shm (icat prefers them locally, like kitty).
        # no TEMP_FILE: in the libghostty-vt bundled with pyghostty 0.1.4 that option number is
        # something else (the getter returns a pointer, setting a bool* segfaults).
        lib.ghostty_terminal_set(t, lib.GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_STORAGE_LIMIT, ffi.new('uint64_t*', KITTY_STORAGE))
        for opt in ('FILE', 'SHARED_MEM'):
            lib.ghostty_terminal_set(t, getattr(lib, 'GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_' + opt), ffi.new('bool*', True))
        self.kit = ffi.new('GhosttyKittyGraphicsPlacementIterator*')
        lib.ghostty_kitty_graphics_placement_iterator_new(ffi.NULL, self.kit)
        self._img = {}          # kitty image id -> (generation, cairo surface)
        self.enc = ffi.new('GhosttyKeyEncoder*'); lib.ghostty_key_encoder_new(ffi.NULL, self.enc)
        self.menc = ffi.new('GhosttyMouseEncoder*'); lib.ghostty_mouse_encoder_new(ffi.NULL, self.menc)
        # create render state + iterators once, only fill them per frame
        self.rs = ffi.new('GhosttyRenderState*'); lib.ghostty_render_state_new(ffi.NULL, self.rs)
        self.rows_it = ffi.new('GhosttyRenderStateRowIterator*')
        lib.ghostty_render_state_row_iterator_new(ffi.NULL, self.rows_it)
        self.cells = ffi.new('GhosttyRenderStateRowCells*')
        lib.ghostty_render_state_row_cells_new(ffi.NULL, self.cells)
        self.colors = init_sized('GhosttyRenderStateColors')
        self.cursor = init_sized('GhosttyRenderStateCursor')
        self._rowcache = []     # per viewport row: (bg rects, text runs, box chars, char per column)
        self.anchor = None      # cell where the mouse selection started
        self.mouse_pos = (0.0, 0.0)
        self.set_font(self.win.font)
        # cell size to ghostty right away, not only at the first layout: a program that starts with the
        # shell (icat in .bashrc, tests) would otherwise place images with 0px cells -> cursor doesn't move
        self.term.resize(self.cols, self.rows, self.cell_w, self.cell_h)
        self._start_shell(cwd)
        self.set_draw_func(self._draw)
        self.connect('resize', lambda area, w, h: self._fit(w, h))
        # focus border + block/hollow cursor depend on the focus: redraw when it comes *and* goes,
        # otherwise the tile that lost it keeps its blue frame
        self.connect('notify::has-focus', lambda *a: self.queue_draw())
        # keys come in at window level (Win._on_key), only mouse here
        click = Gtk.GestureClick(button=0)          # all buttons
        click.connect('pressed', self._press); click.connect('released', self._release)
        self.add_controller(click)
        mot = Gtk.EventControllerMotion(); mot.connect('motion', self._motion); self.add_controller(mot)
        scr = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL
                                        | Gtk.EventControllerScrollFlags.DISCRETE)
        scr.connect('scroll', self._scroll); self.add_controller(scr)
        # drops from other apps: files (text/uri-list, e.g. nautilus) or plain text. dragged tiles
        # don't match these types, they fall through to the Tile's drop target.
        ext = Gtk.DropTarget(actions=Gdk.DragAction.COPY)
        ext.set_gtypes([Gdk.FileList, GObject.TYPE_STRING])     # order = preference: files first
        ext.connect('drop', self._drop_external)
        self.add_controller(ext)
        menu = Gio.Menu()
        for items in ((('split right\tctrl+alt+right', 'split-h'), ('split down\tctrl+alt+down', 'split-v'),
                       ('maximize / restore tile\tctrl+shift+x', 'maximize'), ('new tab\tctrl+shift+t', 'new-tab')),
                      (('move tile left', 'move-left'), ('move tile right', 'move-right'),
                       ('move tile up', 'move-up'), ('move tile down', 'move-down')),
                      (('copy\tctrl+shift+c', 'copy'), ('paste\tctrl+shift+v', 'paste'),
                       ('zoom in\tctrl+shift++', 'zoom-in'), ('zoom out\tctrl+shift+-', 'zoom-out')),
                      (('close tile\tctrl+shift+w', 'close'),)):
            sec = Gio.Menu()
            for label, act in items: sec.append(label, 'win.' + act)
            menu.append_section(None, sec)
        self.pop = Gtk.PopoverMenu(menu_model=menu)
        self.pop.set_parent(self)

    # --- process / pty ---
    def _start_shell(self, cwd):
        shell = os.environ.get('TILIX2_SHELL') or os.environ.get('SHELL') or '/bin/sh'
        if not shutil.which(shell):
            print(f'tilix2: shell {shell!r} not found, using /bin/sh', file=sys.stderr)
            shell = '/bin/sh'
        self.master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, self._winsize())
            env = dict(os.environ, TERM=TERM_NAME, COLORTERM='truecolor')
            # new session + pty as controlling tty -> close(master) sends SIGHUP to the shell
            self.proc = subprocess.Popen([shell], stdin=slave, stdout=slave, stderr=slave, env=env,
                                         cwd=cwd if cwd and os.path.isdir(cwd) else None,
                                         start_new_session=True,
                                         preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        except OSError:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        # non-blocking: a program that doesn't read (sleep, ctrl+s, stopped job) must not freeze the
        # gui on a big paste. what doesn't fit goes into _out, an IO_OUT watch pushes it later.
        os.set_blocking(self.master, False)
        self._out, self._out_src = bytearray(), None
        # below GTK's redraw priority (like vte): `yes` or `cat /dev/urandom` must not hold off painting
        self._io_src = GLib.io_add_watch(self.master, GLib.PRIORITY_DEFAULT_IDLE,
                                         GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._io)

    def cwd(self):
        return os.readlink(f'/proc/{self.proc.pid}/cwd')

    def _winsize(self):
        "struct winsize incl. pixels: icat & co. refuse to work without the pixel size. (u16: huge zoom on 8k)"
        return struct.pack('HHHH', self.rows, self.cols,
                           min(65535, self.cols * self.cell_w), min(65535, self.rows * self.cell_h))

    def _size_report(self, term, userdata, out):
        out.rows, out.columns, out.cell_width, out.cell_height = self.rows, self.cols, self.cell_w, self.cell_h
        return True

    def kill(self):
        "close the pty -> kernel sends SIGHUP (bash still writes its history). reap async, nothing blocks."
        if self.term is None: return
        self.dead = True
        if self._io_src: GLib.source_remove(self._io_src)
        if self._out_src: GLib.source_remove(self._out_src)
        os.close(self.master)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.proc.pid, self._reaped)
        lib.ghostty_render_state_row_cells_free(self.cells[0])
        lib.ghostty_render_state_row_iterator_free(self.rows_it[0])
        lib.ghostty_render_state_free(self.rs[0])
        lib.ghostty_key_encoder_free(self.enc[0])
        lib.ghostty_mouse_encoder_free(self.menc[0])
        lib.ghostty_kitty_graphics_placement_iterator_free(self.kit[0])
        self._img.clear()
        self.term.close()
        self.term = None
        self.pop.unparent()

    def _reaped(self, pid, status):
        "glib reaped the child -> tell Popen, otherwise its next poll() gets ECHILD and makes up exit code 0."
        if self.proc.returncode is None: self.proc.returncode = os.waitstatus_to_exitcode(status)

    def write(self, data):
        "to the pty without blocking: queue it, write what fits, the rest goes out via _flush on IO_OUT."
        if self.dead: return
        self._out += data
        if self._out_src is None: self._flush()

    def _flush(self, *args):
        while self._out:
            try: n = os.write(self.master, self._out)
            except BlockingIOError: break        # pty buffer full, the program isn't reading
            except OSError:                      # EIO = shell gone, the read side closes the tile
                self._out.clear()
                break
            del self._out[:n]                    # partial writes: the rest stays queued
        if self._out:
            if self._out_src is None:
                self._out_src = GLib.io_add_watch(self.master, GLib.IO_OUT, self._flush)
            return True
        self._out_src = None                     # return False removes the watch
        return False

    def _io(self, fd, cond):
        try: data = os.read(fd, 65536)
        except BlockingIOError: return True      # non-blocking + spurious wakeup: not an eof
        except OSError: data = b''               # EIO = shell gone
        if not data: return self._gone()
        try:
            self.term.feed(data)
            t = self._title()
            if t != self.title:
                self.title = t
                self.win.retitle(self.tile)
            self.changed()
        except Exception:
            # without this pygobject would drop the watch: tile never reads again, never closes
            traceback.print_exc()
            return self._gone()
        return True

    def _gone(self):
        "process ended (ctrl+d/exit) or the tile broke: it gives up its space (tilix behavior)."
        self.dead = True
        self._io_src = None                      # return False removes the source
        GLib.idle_add(self.win.close_tile, self.tile)
        return False

    def _title(self):
        "terminal title (OSC 0/2) without control chars (\\n would make a 2-line tab label), max 200 chars."
        s = ffi.new('GhosttyString*')
        lib.ghostty_terminal_get(self.term._t[0], lib.GHOSTTY_TERMINAL_DATA_TITLE, s)
        t = ffi.buffer(s.ptr, s.len)[:].decode('utf8', 'replace') if s.len else ''
        return ''.join(c for c in t if c.isprintable())[:200]

    def changed(self):
        "after every change to the terminal: redraw, update the scrollbar."
        self.queue_draw()
        self.tile.sync_sbar()

    def scroll(self, tag, delta=0):
        "move the viewport: DELTA (rows, - = up), ROW (absolute), BOTTOM."
        if self.dead: return
        v = ffi.new('ScrollViewportS*')
        v.tag, v.delta = tag, delta
        _scroll_viewport(self.term._t[0], v[0])
        self.changed()

    # --- font / size ---
    def set_font(self, font):
        "set the font (zoom), measure the cell size, refit the grid."
        self.font = font
        desc = Pango.FontDescription.from_string(font)
        m = self.get_pango_context().get_metrics(desc)
        self.cell_w = max(1, -(-m.get_approximate_char_width() // Pango.SCALE))
        self.cell_h = max(1, (m.get_ascent() + m.get_descent()) // Pango.SCALE)
        self.ascent = m.get_ascent() // Pango.SCALE
        self.fonts = {}
        for bold in (False, True):
            for ital in (False, True):
                d = desc.copy()
                if bold: d.set_weight(Pango.Weight.BOLD)
                if ital: d.set_style(Pango.Style.ITALIC)
                self.fonts[bold, ital] = d
        # glyphs are narrower than the cell (8.8 px advance in a 9 px cell): a run of 100 chars would
        # end 20 px short and borders drift apart. letter spacing stretches every glyph to the cell.
        probe = Pango.Layout(self.get_pango_context())
        probe.set_font_description(self.fonts[False, False])
        probe.set_text('M')
        self._grid_attrs = Pango.AttrList()
        self._grid_attrs.insert(Pango.attr_letter_spacing_new(self.cell_w * Pango.SCALE - probe.get_extents()[1].width))
        self._rowcache.clear()      # layouts are tied to the old font
        if self.get_width():
            self._fit(self.get_width(), self.get_height())
        self.queue_draw()

    def _fit(self, wpx, hpx):
        cols, rows = max(4, wpx // self.cell_w), max(2, hpx // self.cell_h)
        # cell size counts too: zoom without a grid change still changes the pixel sizes (images, reports)
        if (cols, rows, self.cell_w, self.cell_h) == self._grid or self.dead: return
        self._grid = (cols, rows, self.cell_w, self.cell_h)
        self.cols, self.rows = cols, rows
        self.term.resize(cols, rows, self.cell_w, self.cell_h)
        try: fcntl.ioctl(self.master, termios.TIOCSWINSZ, self._winsize())
        except OSError: pass
        self.tile.sync_sbar()


    # --- keyboard ---
    def send_key(self, keyval, state, keycode=0, text=None):
        """keyval/state -> ghostty key encoder -> pty. returns True if handled.
        text: what the IM committed (dead keys/compose); otherwise derived from the keyval."""
        if self.dead: return False
        name = Gdk.keyval_name(keyval) or ''
        if name in MODIFIER_KEYS: return False
        mods = ghostty_mods(state)
        if text is None:
            u = Gdk.keyval_to_unicode(keyval)
            text = chr(u) if u >= 0x20 and u != 0x7f else None   # control chars are done by the encoder
        gk = KEYMAP.get(name)
        if gk is None:
            if not text: return False
            gk = 'UNIDENTIFIED'
        u = Gdk.keyval_to_unicode(base_keyval(keycode, keyval))   # kitty wants the unshifted codepoint
        ev = ffi.new('GhosttyKeyEvent*')
        lib.ghostty_key_event_new(ffi.NULL, ev)
        lib.ghostty_key_event_set_action(ev[0], lib.GHOSTTY_KEY_ACTION_PRESS)
        lib.ghostty_key_event_set_key(ev[0], getattr(lib, 'GHOSTTY_KEY_' + gk))
        lib.ghostty_key_event_set_mods(ev[0], mods)
        # shift produced the text ('A', '!') -> consumed, otherwise kitty sends CSI 97;2u instead of 'A'
        lib.ghostty_key_event_set_consumed_mods(ev[0], mods & SHIFT if text else 0)
        if u >= 0x20: lib.ghostty_key_event_set_unshifted_codepoint(ev[0], u)
        if text:
            b = text.encode()
            lib.ghostty_key_event_set_utf8(ev[0], b, len(b))
        lib.ghostty_key_encoder_setopt_from_terminal(self.enc[0], self.term._t[0])
        out = ffi.new('char[]', 128); n = ffi.new('size_t*')
        r = lib.ghostty_key_encoder_encode(self.enc[0], ev[0], out, len(out), n)
        if r == lib.GHOSTTY_OUT_OF_SPACE:      # long IM commit: n says how much is needed
            out = ffi.new('char[]', n[0])
            r = lib.ghostty_key_encoder_encode(self.enc[0], ev[0], out, len(out), n)
        lib.ghostty_key_event_free(ev[0])
        if r == 0 and n[0]:
            if not self.term.get('viewport_active'):   # typing jumps back to the bottom
                self.scroll(lib.GHOSTTY_SCROLL_VIEWPORT_BOTTOM)
            self.write(ffi.buffer(out, n[0])[:])
        return True

    # --- mouse ---
    def _cell_at(self, x, y):
        return (min(self.cols - 1, max(0, int(x // self.cell_w))),
                min(self.rows - 1, max(0, int(y // self.cell_h))))

    def _mouse_to_app(self, state):
        "the app wants the mouse (mouse tracking) — unless shift is held (then we select ourselves)."
        return self.term.get('mouse_tracking', 'bool') and not state & MT.SHIFT_MASK

    def _mencode(self, action, button, x, y, state):
        ev = ffi.new('GhosttyMouseEvent*')
        lib.ghostty_mouse_event_new(ffi.NULL, ev)
        lib.ghostty_mouse_event_set_action(ev[0], action)
        if button: lib.ghostty_mouse_event_set_button(ev[0], button)
        lib.ghostty_mouse_event_set_mods(ev[0], ghostty_mods(state))
        pos = ffi.new('GhosttyMousePosition*')
        pos.x, pos.y = x, y
        lib.ghostty_mouse_event_set_position(ev[0], pos[0])
        lib.ghostty_mouse_encoder_setopt_from_terminal(self.menc[0], self.term._t[0])
        sz = init_sized('GhosttyMouseEncoderSize')
        sz.screen_width, sz.screen_height = self.cols * self.cell_w, self.rows * self.cell_h
        sz.cell_width, sz.cell_height = self.cell_w, self.cell_h
        lib.ghostty_mouse_encoder_setopt(self.menc[0], lib.GHOSTTY_MOUSE_ENCODER_OPT_SIZE, sz)
        pressed = ffi.new('bool*', bool(state & (MT.BUTTON1_MASK | MT.BUTTON2_MASK | MT.BUTTON3_MASK)))
        lib.ghostty_mouse_encoder_setopt(self.menc[0], lib.GHOSTTY_MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED, pressed)
        out = ffi.new('char[]', 64); n = ffi.new('size_t*')
        r = lib.ghostty_mouse_encoder_encode(self.menc[0], ev[0], out, len(out), n)
        lib.ghostty_mouse_event_free(ev[0])
        if r == 0 and n[0]: self.write(ffi.buffer(out, n[0])[:])

    def _press(self, g, n, x, y):
        if self.dead: return
        self.grab_focus()
        self.win.activate_tile(self.tile)
        btn, state = g.get_current_button(), g.get_current_event_state()
        if self._mouse_to_app(state):
            self._mencode(lib.GHOSTTY_MOUSE_ACTION_PRESS, MOUSE_BTN.get(btn, 0), x, y, state)
        elif btn == 3:
            self.pop.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
            self.pop.popup()
        elif btn == 2:
            self.paste(primary=True)          # middle-click: primary selection (X11/tilix behavior)
        elif btn == 1:
            self.anchor = self._cell_at(x, y)
            self.select(None)

    def _release(self, g, n, x, y):
        if self.dead: return
        btn, state = g.get_current_button(), g.get_current_event_state()
        if self.anchor is None and self._mouse_to_app(state):
            self._mencode(lib.GHOSTTY_MOUSE_ACTION_RELEASE, MOUSE_BTN.get(btn, 0), x, y, state)
        elif btn == 1 and self.anchor is not None:
            self.anchor = None
            self.copy(primary_only=True)      # selecting = primary selection (X11 convention)

    def _motion(self, m, x, y):
        if self.dead: return
        self.mouse_pos = (x, y)
        state = m.get_current_event_state()
        if self.anchor is not None and not state & MT.BUTTON1_MASK:
            self.anchor = None      # release got lost (alt+tab while dragging): don't keep selecting
        if self.anchor is not None:
            cell = self._cell_at(x, y)
            if cell == self.anchor: self.select(None)      # click jitter is not a selection
            else: self.select(self.anchor, cell)
        elif self._mouse_to_app(state):
            btn = next((b for mask, b in ((MT.BUTTON1_MASK, 1), (MT.BUTTON2_MASK, 2), (MT.BUTTON3_MASK, 3))
                        if state & mask), 0)
            self._mencode(lib.GHOSTTY_MOUSE_ACTION_MOTION, MOUSE_BTN.get(btn, 0), x, y, state)

    def _scroll(self, c, dx, dy):
        if self.dead: return True
        state = c.get_current_event_state() if c else 0
        if self._mouse_to_app(state):
            btn = lib.GHOSTTY_MOUSE_BUTTON_FOUR if dy < 0 else lib.GHOSTTY_MOUSE_BUTTON_FIVE
            self._mencode(lib.GHOSTTY_MOUSE_ACTION_PRESS, btn, *self.mouse_pos, state)
        elif (self.term.get('active_screen', 'int') == lib.GHOSTTY_TERMINAL_SCREEN_ALTERNATE
              and self.term.mode(1007)):
            # alternate screen without mouse (less, man): wheel -> arrow keys (xterm "alternate scroll")
            for _ in range(3): self.send_key(Gdk.KEY_Up if dy < 0 else Gdk.KEY_Down, 0)
        else:
            self.scroll(lib.GHOSTTY_SCROLL_VIEWPORT_DELTA, 3 if dy > 0 else -3)
        return True

    # --- selection / clipboard ---
    def select(self, a, b=None):
        "selection from cell a to b (viewport coordinates); ghostty tracks it while scrolling."
        if self.dead: return
        sel = ffi.NULL
        if a is not None:
            sel = init_sized('GhosttySelection')
            sel.start = self.term.ref(*a, 'viewport')[0]
            sel.end = self.term.ref(*(b or a), 'viewport')[0]
        lib.ghostty_terminal_set(self.term._t[0], lib.GHOSTTY_TERMINAL_OPT_SELECTION, sel)
        self.queue_draw()

    def selected_text(self):
        sel = init_sized('GhosttySelection')
        if self.dead or lib.ghostty_terminal_get(self.term._t[0], lib.GHOSTTY_TERMINAL_DATA_SELECTION, sel):
            return ''
        return self.term._format_sel(sel, unwrap=True)

    def copy(self, primary_only=False):
        "selected text to clipboard + primary. without a selection nothing happens (tilix behavior)."
        text = self.selected_text()
        if not text: return
        cp = Gdk.ContentProvider.new_for_value(text)   # gtk serializes into all text formats
        self.get_display().get_primary_clipboard().set_content(cp)
        if not primary_only: self.get_display().get_clipboard().set_content(cp)

    def paste(self, primary=False):
        "clipboard (ctrl+shift+v) or primary selection (middle-click)."
        def done(src, res):
            try: self.paste_text(src.read_text_finish(res))
            except GLib.Error: pass
        d = self.get_display()
        (d.get_primary_clipboard() if primary else d.get_clipboard()).read_text_async(None, done)

    def paste_text(self, t):
        """text into the pty as a paste (clipboard, drag&drop). ghostty_paste_encode: ESC & co. -> space
        (no escaping out of bracketed paste), brackets on mode 2004, otherwise \\n -> \\r."""
        if not t or self.dead: return
        data = t.encode()
        out = ffi.new('char[]', len(data) + 12)    # + '\e[200~' + '\e[201~'
        n = ffi.new('size_t*')
        if lib.ghostty_paste_encode(ffi.new('char[]', data), len(data), self.term.mode(2004),
                                    out, len(out), n) == 0:
            self.write(ffi.buffer(out, n[0])[:])

    def _drop_external(self, dt, value, x, y):
        "files from a file manager -> their paths, shell-quoted (like gnome-terminal/tilix); text -> as is."
        if isinstance(value, Gdk.FileList):
            value = ''.join(shlex.quote(f.get_path() or f.get_uri()) + ' ' for f in value.get_files())
        self.paste_text(value)
        self.win.activate_tile(self.tile)
        return True

    # --- rendering ---
    def _build_row(self, FG, BG):
        """current iterator row -> (bg rects, text runs, char per column).
        only runs for dirty rows; the pango layouts get cached too (shaping is the expensive part)."""
        it, cells = self.rows_it[0], self.cells[0]
        lib.ghostty_render_state_row_get(it, lib.GHOSTTY_RENDER_STATE_ROW_DATA_CELLS, self.cells)
        rsel = init_sized('GhosttyRenderStateRowSelection')
        sel = (rsel.start_x, rsel.end_x) if lib.ghostty_render_state_row_get(
            it, lib.GHOSTTY_RENDER_STATE_ROW_DATA_SELECTION, rsel) == 0 else (-1, -1)
        c = ffi.new('GhosttyColorRgb*')
        st = init_sized('GhosttyStyle')
        buf = ffi.new('uint8_t[]', 1024)
        gb = ffi.new('GhosttyBuffer*'); gb.ptr, gb.cap = buf, len(buf)
        flag = ffi.new('bool*'); raw = ffi.new('GhosttyCell*'); wide = ffi.new('uint32_t*')
        get = lib.ghostty_render_state_row_cells_get
        bgs, runs, boxes, chars = [], [], [], []
        run = None              # [x, text, ncells, wide, fg, bold, italic, deco]
        x = 0
        while lib.ghostty_render_state_row_cells_next(cells):
            if get(cells, lib.GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8, gb) == 0:
                text = ffi.buffer(buf, gb.len)[:].decode('utf8', 'replace')
            else:
                text = '\ufffd'          # grapheme cluster > 1 KiB: we don't show that

            fg = FG
            bg = rgb(c) if get(cells, lib.GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR, c) == 0 else None
            bold = ital = False
            deco = (False, False, False)
            get(cells, lib.GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_HAS_STYLING, flag)
            if flag[0]:
                get(cells, lib.GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE, st)
                if get(cells, lib.GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR, c) == 0: fg = rgb(c)
                bold, ital = st.bold, st.italic
                deco = (st.underline > 0, st.strikethrough, st.overline)
                if st.inverse: fg, bg = bg or BG, fg
                if st.faint: fg = (fg[0] * 0.6, fg[1] * 0.6, fg[2] * 0.6)
                if st.invisible: text = ''
            if sel[0] <= x <= sel[1]: fg, bg = bg or BG, fg
            if bg is not None and bg != BG:
                if bgs and bgs[-1][2] == bg and bgs[-1][0] + bgs[-1][1] == x: bgs[-1][1] += 1
                else: bgs.append([x, 1, bg])
            chars.append(text)
            if not text:
                run = None
            elif text in BOX_CHARS:     # drawn by draw_box, not by the font
                boxes.append((x, text, fg))
                run = None
            else:
                get(cells, lib.GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW, raw)
                lib.ghostty_cell_get(raw[0], lib.GHOSTTY_CELL_DATA_WIDE, wide)
                is_wide = wide[0] == lib.GHOSTTY_CELL_WIDE_WIDE   # cjk/emoji: own run, never merged
                key = (fg, bold, ital, deco)
                if run and not is_wide and not run[3] and run[0] + run[2] == x and run[4:] == list(key):
                    run[1] += text; run[2] += 1
                else:
                    run = [x, text, 2 if is_wide else 1, is_wide, *key]
                    runs.append(run)
            x += 1
        out = []
        for x, text, n, is_wide, fg, bold, ital, deco in runs:
            layout = Pango.Layout(self.get_pango_context())
            layout.set_font_description(self.fonts[bold, ital])
            layout.set_text(text)
            if not is_wide: layout.set_attributes(self._grid_attrs)    # every glyph exactly one cell wide
            # scale wide chars (cjk, emoji) to exactly 2 cells
            scale = 2 * self.cell_w / max(1, layout.get_pixel_size()[0]) if is_wide else 0
            out.append((x, n, fg, layout, deco, scale))
        return bgs, out, boxes, chars

    # --- kitty graphics ---
    def _images(self):
        """kitty placements in the viewport -> 3 layers (below bg, below text, above text), each a list of
        (surface, src x/y/w/h, dst x/y/w/h). virtual placements (unicode placeholders) aren't supported."""
        layers = ([], [], [])
        t = self.term._t[0]
        g = ffi.new('GhosttyKittyGraphics*')     # borrowed, only valid until the next feed -> fetch per frame
        if lib.ghostty_terminal_get(t, lib.GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS, g) or \
                lib.ghostty_kitty_graphics_get(g[0], lib.GHOSTTY_KITTY_GRAPHICS_DATA_PLACEMENT_ITERATOR, self.kit):
            return layers
        it, get = self.kit[0], lib.ghostty_kitty_graphics_placement_get
        iid, xo, yo = ffi.new('uint32_t*'), ffi.new('uint32_t*'), ffi.new('uint32_t*')
        z, virtual = ffi.new('int32_t*'), ffi.new('bool*')
        info = init_sized('GhosttyKittyGraphicsPlacementRenderInfo')
        seen = set()
        while lib.ghostty_kitty_graphics_placement_next(it):
            get(it, lib.GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IS_VIRTUAL, virtual)
            if virtual[0]: continue
            get(it, lib.GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IMAGE_ID, iid)
            img = lib.ghostty_kitty_graphics_image(g[0], iid[0])
            if img == ffi.NULL or lib.ghostty_kitty_graphics_placement_render_info(it, img, t, info) \
                    or not info.viewport_visible or not (info.source_width and info.source_height):
                continue
            surf = self._image_surface(img, iid[0])
            if surf is None: continue
            seen.add(iid[0])
            get(it, lib.GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_X_OFFSET, xo)
            get(it, lib.GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Y_OFFSET, yo)
            get(it, lib.GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Z, z)
            layer = 0 if z[0] < KITTY_Z_BELOW_BG else 1 if z[0] < 0 else 2
            layers[layer].append((surf, info.source_x, info.source_y, info.source_width, info.source_height,
                                  info.viewport_col * self.cell_w + xo[0], info.viewport_row * self.cell_h + yo[0],
                                  info.pixel_width, info.pixel_height))
        for k in self._img.keys() - seen: del self._img[k]   # deleted / scrolled away -> free the surface
        return layers

    def _image_surface(self, img, iid):
        "kitty image -> cairo surface, cached per image id until ghostty bumps the generation."
        gen = ffi.new('uint64_t*')
        lib.ghostty_kitty_graphics_image_get(img, lib.GHOSTTY_KITTY_IMAGE_DATA_GENERATION, gen)
        hit = self._img.get(iid)
        if hit and hit[0] == gen[0]: return hit[1]
        w, h, fmt = ffi.new('uint32_t*'), ffi.new('uint32_t*'), ffi.new('GhosttyKittyImageFormat*')
        ptr, n = ffi.new('uint8_t**'), ffi.new('size_t*')
        for key, out in ((lib.GHOSTTY_KITTY_IMAGE_DATA_WIDTH, w), (lib.GHOSTTY_KITTY_IMAGE_DATA_HEIGHT, h),
                         (lib.GHOSTTY_KITTY_IMAGE_DATA_FORMAT, fmt), (lib.GHOSTTY_KITTY_IMAGE_DATA_DATA_PTR, ptr),
                         (lib.GHOSTTY_KITTY_IMAGE_DATA_DATA_LEN, n)):
            lib.ghostty_kitty_graphics_image_get(img, key, out)
        mf, bpp = KITTY_FMT.get(fmt[0], (None, 0))
        if mf is None or not (w[0] and h[0]) or n[0] < w[0] * h[0] * bpp: return None
        # gdk converts in C: rgb/rgba/gray -> premultiplied bgra = cairo ARGB32 (little endian)
        tex = Gdk.MemoryTexture.new(w[0], h[0], mf, GLib.Bytes.new(ffi.buffer(ptr[0], n[0])[:]), w[0] * bpp)
        dl = Gdk.TextureDownloader.new(tex)
        dl.set_format(Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED)
        px, stride = dl.download_bytes()
        surf = cairo.ImageSurface.create_for_data(bytearray(px.get_data()), cairo.FORMAT_ARGB32, w[0], h[0], stride)
        self._img[iid] = (gen[0], surf)
        return surf

    def _draw_images(self, cr, items):
        for surf, sx, sy, sw, sh, dx, dy, dw, dh in items:
            cr.save()
            cr.rectangle(dx, dy, dw, dh)
            cr.clip()
            cr.translate(dx, dy)
            cr.scale(dw / sw, dh / sh)
            cr.set_source_surface(surf, -sx, -sy)
            cr.get_source().set_extend(cairo.EXTEND_PAD)   # otherwise scaling fades the edges to transparent
            cr.paint()
            cr.restore()

    def _draw(self, area, cr, width, height):
        if self.dead: return
        rs = self.rs[0]
        lib.ghostty_render_state_update(rs, self.term._t[0])
        lib.ghostty_render_state_get(rs, lib.GHOSTTY_RENDER_STATE_DATA_COLORS, self.colors)
        lib.ghostty_render_state_get(rs, lib.GHOSTTY_RENDER_STATE_DATA_CURSOR, self.cursor)
        dirty = ffi.new('GhosttyRenderStateDirty*')
        lib.ghostty_render_state_get(rs, lib.GHOSTTY_RENDER_STATE_DATA_DIRTY, dirty)
        full = dirty[0] == lib.GHOSTTY_RENDER_STATE_DIRTY_FULL
        FG, BG = rgb(self.colors.foreground), rgb(self.colors.background)
        cw, ch = self.cell_w, self.cell_h
        cr.set_source_rgb(*BG)
        cr.paint()
        lib.ghostty_render_state_get(rs, lib.GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR, self.rows_it)
        row_dirty = ffi.new('bool*')
        cache = self._rowcache
        y = 0
        while lib.ghostty_render_state_row_iterator_next(self.rows_it[0]):
            lib.ghostty_render_state_row_get(self.rows_it[0], lib.GHOSTTY_RENDER_STATE_ROW_DATA_DIRTY, row_dirty)
            if y >= len(cache): cache.append(self._build_row(FG, BG))
            elif full or row_dirty[0]: cache[y] = self._build_row(FG, BG)
            y += 1
        del cache[y:]
        lib.ghostty_render_state_clean(rs)
        # layers bottom to top: images below bg, cell bgs, images below text, text, images above text
        below_bg, below_text, above_text = self._images()
        self._draw_images(cr, below_bg)
        for y, (bgs, _, _, _) in enumerate(cache):
            for x, n, col in bgs:
                cr.set_source_rgb(*col)
                cr.rectangle(x * cw, y * ch, n * cw, ch)
                cr.fill()
        self._draw_images(cr, below_text)
        for y, (_, runs, boxes, _) in enumerate(cache):
            for x, c, fg in boxes:
                cr.set_source_rgb(*fg)
                draw_box(cr, c, x * cw, y * ch, cw, ch)
            for x, n, fg, layout, deco, scale in runs:
                cr.set_source_rgb(*fg)
                if scale:
                    cr.save(); cr.translate(x * cw, y * ch); cr.scale(scale, 1)
                    cr.move_to(0, 0); PangoCairo.show_layout(cr, layout)
                    cr.restore()
                else:
                    cr.move_to(x * cw, y * ch)
                    PangoCairo.show_layout(cr, layout)
                for on, off in zip(deco, (self.ascent + 2, ch // 2, 1)):   # underline/strike/overline
                    if on:
                        cr.rectangle(x * cw, y * ch + off, n * cw, 1)
                        cr.fill()
        self._draw_images(cr, above_text)
        self._draw_cursor(cr, FG, BG)
        if self.has_focus() and len(self.win.tiles()) > 1 and self.tile.tab.maximized is None:
            cr.set_source_rgb(0.21, 0.52, 0.89)
            cr.set_line_width(2)
            cr.rectangle(1, 1, width - 2, height - 2)
            cr.stroke()
        zone = self.tile.drop_zone
        if zone:          # drag&drop preview: the half where the dragged tile will land
            w2, h2 = width / 2, height / 2
            x, y, w, h = {'left': (0, 0, w2, height), 'right': (w2, 0, w2, height),
                          'top': (0, 0, width, h2), 'bottom': (0, h2, width, h2)}[zone]
            cr.set_source_rgba(0.21, 0.52, 0.89, 0.25)
            cr.rectangle(x, y, w, h)
            cr.fill_preserve()
            cr.set_source_rgb(0.21, 0.52, 0.89)
            cr.set_line_width(2)
            cr.stroke()

    def _draw_cursor(self, cr, FG, BG):
        cur = self.cursor
        if not (cur.visible and cur.viewport_has_value): return
        x, y = cur.viewport_x - cur.wide_tail, cur.viewport_y
        if y >= len(self._rowcache): return
        cw, ch = self.cell_w, self.cell_h
        cr.set_source_rgb(*(rgb(self.colors.cursor) if self.colors.cursor_has_value else FG))
        style = cur.visual_style
        if style == lib.GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BAR:
            cr.rectangle(x * cw, y * ch, 2, ch); cr.fill()
        elif style == lib.GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_UNDERLINE:
            cr.rectangle(x * cw, (y + 1) * ch - 2, cw, 2); cr.fill()
        elif style == lib.GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK_HOLLOW or not self.has_focus():
            cr.set_line_width(1)
            cr.rectangle(x * cw + 0.5, y * ch + 0.5, cw - 1, ch - 1); cr.stroke()
        else:
            # block: fill the cell, draw the char underneath on top in the background color
            cr.rectangle(x * cw, y * ch, cw, ch); cr.fill()
            chars = self._rowcache[y][3]
            if x < len(chars) and chars[x] in BOX_CHARS:
                cr.set_source_rgb(*BG)
                draw_box(cr, chars[x], x * cw, y * ch, cw, ch)
            elif x < len(chars) and chars[x]:
                layout = Pango.Layout(self.get_pango_context())
                layout.set_font_description(self.fonts[False, False])
                layout.set_text(chars[x])
                cr.set_source_rgb(*BG)
                cr.move_to(x * cw, y * ch)
                PangoCairo.show_layout(cr, layout)


def split_icon(vertical):
    "16px icon for the headerbar: '|+' (split right), flipped '—' with '+' below (split down)."
    def draw(area, cr, w, h):
        c = area.get_color()                     # text color of the theme -> fits light and dark
        cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
        cr.set_line_width(2)
        for x1, y1, x2, y2 in ((4, 2, 4, 14), (8, 8, 14, 8), (11, 5, 11, 11)):   # bar, plus
            if vertical: x1, y1, x2, y2 = y1, x1, y2, x2
            cr.move_to(x1, y1); cr.line_to(x2, y2)
        cr.stroke()
    area = Gtk.DrawingArea(content_width=16, content_height=16)
    area.set_draw_func(draw)
    return area


class Tab(Gtk.Box):
    "tab: page in the Gtk.Stack (holds the paned tree) + button in the tab bar."

    def __init__(self, win, group):
        super().__init__(hexpand=True, vexpand=True)
        self.active = None            # last active tile in this tab
        self.maximized = None         # tile that fills the tab right now (the others are hidden)
        # width_chars=1: the title must not dictate the tab width (the bar splits it evenly)
        self.label = Gtk.Label(label='tilix2', ellipsize=Pango.EllipsizeMode.MIDDLE, width_chars=1, hexpand=True)
        self.count = Gtk.Label(css_classes=['dim-label', 'numeric'], tooltip_text='tiles in this tab')
        inner = Gtk.Box(spacing=6)
        inner.append(self.label)
        inner.append(self.count)
        self.btn = Gtk.ToggleButton(child=inner, group=group, hexpand=True, focus_on_click=False)
        self.btn.connect('toggled', lambda b: b.get_active() and win.show_tab(self))
        close = Gtk.Button(icon_name='window-close-symbolic', focus_on_click=False, tooltip_text='close tab')
        close.connect('clicked', lambda b: win.close_tab(self))
        self.head = Gtk.Box(css_classes=['linked'], hexpand=True)
        self.head.append(self.btn)
        self.head.append(close)


class Win(Gtk.ApplicationWindow):
    "window: headerbar, tab bar, Gtk.Stack with one Tab per page; inside a tab a Gtk.Paned tree of Tiles."

    def __init__(self, app):
        super().__init__(application=app, title='tilix2', default_width=1000, default_height=700)
        self.font = DEFAULT_FONT
        self.active = None            # active tile: gets the keys, gets split/zoomed
        kc = Gtk.EventControllerKey()
        kc.connect('key-pressed', self._on_key)
        self.add_controller(kc)
        # input method: dead keys (^ ´ `), compose, ibus. gets every key, committed text
        # goes into the key encoder as utf8 (see _on_key).
        self.im = Gtk.IMMulticontext()
        self.im.set_client_widget(self)
        self.im.connect('commit', self._im_commit)
        self._im_text = None          # != None only while filter_keypress is running
        kc.connect('key-released', lambda c, *a: self.im.filter_keypress(c.get_current_event()))
        self.connect('notify::is-active', lambda w, p: self.im.focus_in() if w.is_active() else self.im.focus_out())
        for name in ('split-h', 'split-v', 'close', 'copy', 'paste', 'zoom-in', 'zoom-out',
                     'zoom-reset', 'move-left', 'move-right', 'move-up', 'move-down',
                     'new-tab', 'tab-next', 'tab-prev', 'maximize'):
            a = Gio.SimpleAction.new(name, None)
            a.connect('activate', lambda a, p, name=name: self.action(name))
            self.add_action(a)
        self.connect('close-request', self._on_close)

        hb = Gtk.HeaderBar()
        def hbtn(action, icon, tip):
            # focus_on_click=False: otherwise the button keeps the focus afterwards and eats enter/space
            b = Gtk.Button(tooltip_text=tip, action_name='win.' + action, focus_on_click=False)
            if isinstance(icon, str): b.set_icon_name(icon)
            else:
                b.set_child(icon)
                b.add_css_class('image-button')     # otherwise not a flat headerbar button
            return b
        hb.pack_start(hbtn('new-tab', 'tab-new-symbolic', 'new tab (ctrl+shift+t)'))
        hb.pack_start(hbtn('split-h', split_icon(False), 'split right (ctrl+alt+right)'))
        hb.pack_start(hbtn('split-v', split_icon(True), 'split down (ctrl+alt+down)'))
        mv = Gio.Menu()
        for label, act in (('left', 'move-left'), ('right', 'move-right'),
                           ('up', 'move-up'), ('down', 'move-down')):
            mv.append(label, 'win.' + act)
        hb.pack_start(Gtk.MenuButton(icon_name='object-select-symbolic', menu_model=mv,
                                     tooltip_text='move tile', focus_on_click=False))
        hb.pack_end(hbtn('close', 'window-close-symbolic', 'close tile (ctrl+shift+w)'))
        hb.pack_end(hbtn('zoom-out', 'zoom-out-symbolic', 'zoom out (ctrl+shift+-)'))
        hb.pack_end(hbtn('zoom-in', 'zoom-in-symbolic', 'zoom in (ctrl+shift++)'))
        hb.pack_end(hbtn('paste', 'edit-paste-symbolic', 'paste (ctrl+shift+v)'))
        hb.pack_end(hbtn('copy', 'edit-copy-symbolic', 'copy (ctrl+shift+c)'))
        self.set_titlebar(hb)

        # own tab bar instead of Gtk.Notebook: its key bindings (ctrl+arrows, ctrl+tab)
        # would fire before the terminal. bar only visible from 2 tabs on.
        self.tabs = []
        # homogeneous: every tab gets total width / number of tabs, no matter how long the title is
        self.tabbar = Gtk.Box(spacing=2, margin_start=2, margin_end=2, margin_top=2, margin_bottom=2,
                              homogeneous=True, visible=False, css_classes=['tilix2-tabs'])
        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.append(self.tabbar)
        vbox.append(self.stack)
        self.set_child(vbox)
        self.new_tab()

    # --- tabs ---
    def new_tab(self, cwd=None):
        tab = Tab(self, self.tabs[0].btn if self.tabs else None)
        tab.active = Tile(self, tab, cwd)
        tab.append(tab.active)
        self.tabs.append(tab)
        self.stack.add_child(tab)
        self.tabbar.append(tab.head)
        self.tabbar.set_visible(len(self.tabs) > 1)
        self.recount(tab)
        tab.btn.set_active(True)              # -> show_tab

    def recount(self, tab):
        "number of tiles in the tab label (after split / close)."
        tab.count.set_label(str(len(self.tiles(tab.get_first_child()))))

    def show_tab(self, tab):
        self.stack.set_visible_child(tab)
        self.activate_tile(tab.active)

    def close_tab(self, tab):
        "kill all tiles in the tab, remove the tab. the right neighbor (else the left one) becomes active."
        for t in self.tiles(tab.get_first_child()): t.view.kill()
        i = self.tabs.index(tab)
        self.tabs.remove(tab)
        self.stack.remove(tab)
        self.tabbar.remove(tab.head)
        if not self.tabs:
            self.destroy()
            return
        self.tabbar.set_visible(len(self.tabs) > 1)
        if tab.btn.get_active():
            self.tabs[min(i, len(self.tabs) - 1)].btn.set_active(True)

    def tiles(self, w=None):
        "all tiles below w (default: current tab), left/top first."
        if w is None: w = self.stack.get_visible_child().get_first_child()
        if isinstance(w, Tile): return [w]
        if isinstance(w, Gtk.Paned): return self.tiles(w.get_start_child()) + self.tiles(w.get_end_child())
        return []

    def activate_tile(self, tile):
        self.active = tile
        tile.tab.active = tile
        tile.view.grab_focus()
        self.retitle(tile)

    def retitle(self, tile):
        "terminal title -> window title (active tile) and tab label (active tile in the tab)."
        t = tile.view.title or 'tilix2'
        if tile.tab.active is tile:
            tile.tab.label.set_label(t)
            tile.tab.btn.set_tooltip_text(t)  # the label gets ellipsized in narrow tabs
        if self.active is tile: self.set_title(t)

    def _on_close(self, w):
        for tab in self.tabs:
            for t in self.tiles(tab.get_first_child()): t.view.kill()
        return False

    def _on_key(self, ctrl, keyval, keycode, state):
        "window-level key input: shortcuts are global, the rest goes to the active tile."
        name = Gdk.keyval_name(keyval) or ''
        if state & MT.CONTROL_MASK and state & MT.SHIFT_MASK:
            # match by physical key, otherwise ctrl+shift+0 is '=' on de (and ')' on us)
            act = {'c': 'copy', 'v': 'paste', 'w': 'close', 't': 'new-tab', 'x': 'maximize',
                   'plus': 'zoom-in', 'equal': 'zoom-in', 'KP_Add': 'zoom-in',
                   'minus': 'zoom-out', 'KP_Subtract': 'zoom-out',
                   '0': 'zoom-reset', 'KP_0': 'zoom-reset'}.get(Gdk.keyval_name(base_keyval(keycode, keyval)))
            if act: self.action(act); return True
        if state & MT.CONTROL_MASK and state & MT.ALT_MASK and name in ('Right', 'Down'):
            self.action('split-h' if name == 'Right' else 'split-v'); return True
        if state & MT.CONTROL_MASK and not state & (MT.SHIFT_MASK | MT.ALT_MASK) and name in ('Page_Up', 'Page_Down'):
            self.action('tab-prev' if name == 'Page_Up' else 'tab-next'); return True
        if not self.active: return False
        ev = ctrl.get_current_event() if ctrl else None   # tests call this without a controller
        if ev is not None:
            self._im_text = ''
            filtered = self.im.filter_keypress(ev)
            text, self._im_text = self._im_text, None
            if filtered and not text: return True        # dead key / compose still in progress
            if filtered: return self.active.view.send_key(keyval, state, keycode, text)
        return self.active.view.send_key(keyval, state, keycode)

    def _im_commit(self, im, text):
        if self._im_text is not None:
            self._im_text += text             # synchronously from filter_keypress -> _on_key sends it
        elif self.active:
            self.active.view.write(text.encode())   # asynchronously (ibus): straight as text

    def action(self, name):
        t = self.active
        if not t: return
        if name == 'split-h': self.split(t, vertical=False)
        elif name == 'split-v': self.split(t, vertical=True)
        elif name == 'close': self.close_tile(t)
        elif name == 'copy': t.view.copy()
        elif name == 'paste': t.view.paste()
        elif name == 'zoom-reset': t.view.set_font(DEFAULT_FONT)
        elif name in ('zoom-in', 'zoom-out'):
            desc = Pango.FontDescription.from_string(t.view.font)
            step = Pango.SCALE if name == 'zoom-in' else -Pango.SCALE
            desc.set_size(max(6 * Pango.SCALE, desc.get_size() + step))
            t.view.set_font(desc.to_string())    # only the active tile (tilix behavior)
        elif name.startswith('move-'): self.move(t, name[5:])
        elif name == 'maximize': self.toggle_maximize(t)
        elif name == 'new-tab':
            try: cwd = t.view.cwd()
            except OSError: cwd = None
            self.new_tab(cwd)
        elif name in ('tab-next', 'tab-prev'):
            i = self.tabs.index(t.tab) + (1 if name == 'tab-next' else -1)
            self.tabs[i % len(self.tabs)].btn.set_active(True)

    # --- tiling tree of Gtk.Paned ---
    def toggle_maximize(self, tile):
        "maximize: hide the sibling branches on the way up to the tab, the paneds give the tile all the space."
        tab = tile.tab
        if tab.maximized is not None:
            self.restore(tab)
            return
        w = tile
        while isinstance(p := w.get_parent(), Gtk.Paned):
            (p.get_end_child() if p.get_start_child() is w else p.get_start_child()).set_visible(False)
            w = p
        if w is tile: return                   # only tile in the tab: nothing to hide
        tab.maximized = tile
        tile.set_maximized(True)
        self.activate_tile(tile)
        tile.view.queue_draw()                 # focus frame goes away (one visible tile)

    def restore(self, tab):
        "undo maximize: everything in the tab visible again (paned positions were never touched)."
        if tab.maximized is None: return
        def show(w):
            w.set_visible(True)
            if isinstance(w, Gtk.Paned):
                show(w.get_start_child())
                show(w.get_end_child())
        show(tab.get_first_child())
        tab.maximized.set_maximized(False)
        tab.maximized.view.queue_draw()        # focus frame comes back
        tab.maximized = None

    def split(self, tile, vertical):
        "halve the tile, new tile (in the same cwd) on the other half, the new one becomes active."
        self.restore(tile.tab)                 # tree changes while maximized: show everything first
        try: cwd = tile.view.cwd()
        except OSError: cwd = None
        new = Tile(self, tile.tab, cwd)
        self._insert(tile, new, vertical, before=False)
        self.recount(tile.tab)
        self.activate_tile(new)

    def _insert(self, dst, new, vertical, before):
        "replace dst with a paned [dst|new] (before: [new|dst]); vertical = stacked."
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                          hexpand=True, vexpand=True)
        parent = dst.get_parent()
        self._replace(parent, dst, None)
        paned.set_start_child(new if before else dst)
        paned.set_end_child(dst if before else new)
        self._replace(parent, None, paned)
        def halve(p, clock):
            # only set the middle once the paned knows its size. a position taken from a not yet
            # (or stale) allocated tile gives 0px halves (split right after split, move).
            size = p.get_height() if vertical else p.get_width()
            if not size: return True
            p.set_position(size // 2)
            return False
        paned.add_tick_callback(halve)

    def _detach(self, tile):
        "detach the tile from its paned, the neighbor takes over the space. returns the neighbor."
        parent = tile.get_parent()
        other = parent.get_end_child() if parent.get_start_child() is tile else parent.get_start_child()
        # detaching a focused child from a paned -> gtk warning 'last focus widget'. so drop the
        # focus briefly and restore it afterwards if the widget is still in the window (neighbor).
        f = self.get_focus()
        if f and f.is_ancestor(parent): self.set_focus(None)
        parent.set_start_child(None)
        parent.set_end_child(None)
        self._replace(parent.get_parent(), parent, other)
        if f and f.get_root() is self: f.grab_focus()
        return other

    def _replace(self, parent, old, new):
        "replace child old of parent (Paned or Tab) with new; None = empty / attach."
        if isinstance(parent, Gtk.Paned):
            if parent.get_start_child() is old: parent.set_start_child(new)
            else: parent.set_end_child(new)
        else:
            if old is not None: parent.remove(old)
            if new is not None: parent.append(new)

    def close_tile(self, tile):
        "close the tile; the neighbor takes over the space. last tile -> close tab, last tab -> close window."
        if tile.get_root() is None: return     # already gone (e.g. ctrl+shift+w and exit at the same time)
        self.restore(tile.tab)
        parent = tile.get_parent()
        if not isinstance(parent, Gtk.Paned):
            self.close_tab(tile.tab)
            return
        tile.view.kill()
        other = self._detach(tile)
        self.recount(tile.tab)
        if tile.tab.active is tile:            # in a background tab the focus stays where it is
            nxt = other if isinstance(other, Tile) else self.tiles(other)[0]
            if self.active is tile:
                self.activate_tile(nxt)
            else:
                tile.tab.active = nxt
                self.retitle(nxt)

    def move(self, tile, direction):
        "swap the tile with its neighbor in the same paned if the direction fits."
        self.restore(tile.tab)
        p = tile.get_parent()
        if not isinstance(p, Gtk.Paned): return
        if (direction in ('left', 'right')) != (p.get_orientation() == Gtk.Orientation.HORIZONTAL): return
        first, second = p.get_start_child(), p.get_end_child()
        if (second is tile and direction in ('left', 'up')) or (first is tile and direction in ('right', 'down')):
            self._swap_children(p)

    def move_tile(self, src, dst, zone):
        "drag&drop: detach src and split it in at edge zone ('left'/'right'/'top'/'bottom') of dst."
        if src is dst or src.tab is not dst.tab or not isinstance(src.get_parent(), Gtk.Paned): return
        self.restore(src.tab)
        self._detach(src)
        self._insert(dst, src, vertical=zone in ('top', 'bottom'), before=zone in ('left', 'top'))
        self.activate_tile(src)

    def _swap_children(self, p):
        "GTK4: set children to None first, then swapped (a widget can't occupy two slots)."
        a, b, pos = p.get_start_child(), p.get_end_child(), p.get_position()
        p.set_start_child(None); p.set_end_child(None)
        p.set_start_child(b); p.set_end_child(a)
        p.set_position(pos)


SLIM_CSS = b"""
/* Adwaita: trough margin 8px + slider border -> 24px wide scrollbar. here: 5px. */
scrollbar.vertical.tilix2-slim { min-width: 5px; min-height: 5px; }
scrollbar.vertical.tilix2-slim > range > trough {
  margin: 0; padding: 0; min-width: 5px; min-height: 5px; background: transparent; border: none;
}
scrollbar.vertical.tilix2-slim > range > trough > slider {
  min-width: 5px; min-height: 20px; margin: 0; padding: 0; border: none; border-radius: 2px;
  background-color: rgba(160,160,160,0.45);
}
scrollbar.vertical.tilix2-slim > range > trough > slider:hover { background-color: rgba(180,180,180,0.7); }
.tilix2-head button { min-height: 0; min-width: 0; padding: 0 4px; }   /* keep the tile header slim */
.tilix2-tabs button { min-height: 0; padding: 2px 8px; }               /* slimmer tabs */
paned separator { min-width: 1px; min-height: 1px; background: rgba(128,128,128,0.35); }
paned separator:hover { background: rgba(90,150,220,0.8); }
"""


class App(Gtk.Application):
    def __init__(self):
        # NON_UNIQUE: every start is its own process (no d-bus handoff to a running instance
        # -> a crash only takes one window down, tests run next to the real app)
        super().__init__(application_id='osuv.tilix2', flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        # dark theme (headerbar/window decoration, menus, tile headers) to match the terminal
        Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = True
        css = Gtk.CssProvider()
        css.load_from_data(SLIM_CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Win(self).present()


if __name__ == '__main__':
    if Gdk.Display.get_default() is None:
        sys.exit('tilix2: no display (neither WAYLAND_DISPLAY nor DISPLAY works)')
    sys.exit(App().run(sys.argv))
