#!/usr/bin/env python3
# tilix2.py — tilix-artiges tiling-terminal: gtk4-paned-baum, jede kachel = pty + libghostty-vt
# (via pyghostty). gezeichnet wird mit cairo/pango aus ghosttys render-state (nur dirty rows
# werden neu aufgebaut), tasten laufen durch ghosttys key-encoder (kitty keyboard protocol).
#
# start:  python3 -m venv --system-site-packages venv && ./venv/bin/pip install pyghostty==0.1.4
#         ./venv/bin/python tilix2.py      (--system-site-packages: gi/gtk4 kommen vom system)
# env:    TILIX2_SHELL, TILIX2_FONT ("Monospace 12"), TILIX2_FG / TILIX2_BG ("#rrggbb")
# keys:   ctrl+alt+right/down split, ctrl+shift+w close, ctrl+shift+c/v copy/paste,
#         ctrl+shift +/-/0 zoom. rechtsklick = menü, kachel-header ziehen = kacheln tauschen
import os, sys, pty, struct, fcntl, termios, shutil, subprocess

try:
    import gi
except ModuleNotFoundError:
    sys.exit('PyGObject (gi) fehlt — venv mit `python3 -m venv --system-site-packages venv` '
             'neu anlegen oder system-python mit python3-gi/pygobject verwenden.')
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, Gio, GLib, Pango, PangoCairo

from pyghostty import Terminal, lib, ffi
from pyghostty._ffi import union_fn
from pyghostty.core import init_sized

# ghostty_terminal_scroll_viewport nimmt eine tagged union by value -> ABI-twin wie in pyghostty._ffi
ffi.cdef('typedef struct { uint32_t tag; uint32_t _pad; int64_t delta; int64_t _pad2; } ScrollViewportS;')
if ffi.sizeof('ScrollViewportS') != ffi.sizeof('GhosttyTerminalScrollViewport'):
    sys.exit('ABI mismatch: ScrollViewportS passt nicht zu GhosttyTerminalScrollViewport')
_scroll_viewport = union_fn('ghostty_terminal_scroll_viewport', 'GhosttyResult(*)(GhosttyTerminal, ScrollViewportS)')

# gdk keyval-name (= x11 keysym) -> GHOSTTY_KEY_*
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
SHIFT, CTRL, ALT = 1, 2, 4      # ghostty mods-bits: shift=1 ctrl=2 alt=4 super=8 hyper=16 meta=32 caps=64

def ghostty_mods(state):
    m = 0
    for mask, bit in ((MT.SHIFT_MASK, 1), (MT.CONTROL_MASK, 2), (MT.ALT_MASK, 4), (MT.SUPER_MASK, 8),
                      (MT.HYPER_MASK, 16), (MT.META_MASK, 32), (MT.LOCK_MASK, 64)):
        if state & mask: m |= bit
    return m

def hexrgb(s, default):
    "'#rrggbb' -> (r,g,b); bei unsinnigem input (red, #fff, leer) default statt traceback."
    s = (s or '').lstrip('#')
    try:
        if len(s) != 6: raise ValueError
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default

def base_keyval(keycode, keyval):
    "keyval der physischen taste ohne modifier (de: shift+0 -> '0' statt '='). fallback: lowercase."
    ok, kv, *_ = Gdk.Display.get_default().translate_key(keycode, 0, 0) if keycode else (False, 0)
    return kv if ok else Gdk.keyval_to_lower(keyval)

def rgb(c):
    "GhosttyColorRgb -> cairo-tupel"
    return (c.r / 255, c.g / 255, c.b / 255)

TERM_NAME = 'xterm-ghostty' if shutil.which('infocmp') and subprocess.run(
    ['infocmp', 'xterm-ghostty'], capture_output=True).returncode == 0 else 'xterm-256color'
DEFAULT_FONT = os.environ.get('TILIX2_FONT', 'Monospace 11')
TERM_FG = hexrgb(os.environ.get('TILIX2_FG'), (212, 212, 212))
TERM_BG = hexrgb(os.environ.get('TILIX2_BG'), (24, 26, 32))
MOUSE_BTN = {1: lib.GHOSTTY_MOUSE_BUTTON_LEFT, 2: lib.GHOSTTY_MOUSE_BUTTON_MIDDLE,
             3: lib.GHOSTTY_MOUSE_BUTTON_RIGHT}


class Tile(Gtk.Box):
    "kachel: header (pwd, zum tauschen ziehen) oben, TermView mitte, dünne scrollbar rechts."
    MIME = 'application/x-tilix2-tile'
    _dragging = None   # welche kachel gerade gezogen wird (der dnd-payload kann keine widget-ref tragen)

    def __init__(self, win, cwd=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.win = win
        head = Gtk.Box(spacing=6, margin_top=2, margin_bottom=2, margin_start=6, margin_end=6)
        head.append(Gtk.Image(icon_name='open-menu-symbolic', pixel_size=12))
        self.pwd_lbl = Gtk.Label(label='~', xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        head.append(self.pwd_lbl)
        self.append(head)
        self.adj = Gtk.Adjustment()
        self._sbar_handler = self.adj.connect('value-changed', self._on_sbar)
        self._sb = None             # letzter (total, offset, len) -> nur bei änderung anfassen
        self.sbar = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=self.adj, visible=False)
        self.sbar.add_css_class('tilix2-slim')
        self.view = TermView(self, cwd)
        body = Gtk.Box(vexpand=True)
        body.append(self.view)
        body.append(self.sbar)
        self.append(body)
        ds = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        ds.connect('prepare', self._drag_prepare)
        head.add_controller(ds)
        dt = Gtk.DropTarget.new(Gdk.ContentFormats.new([self.MIME]), Gdk.DragAction.MOVE)
        dt.connect('drop', self._drop)
        self.add_controller(dt)
        GLib.timeout_add(1000, self._tick)

    def _drag_prepare(self, src, x, y):
        Tile._dragging = self
        return Gdk.ContentProvider.new_for_bytes(self.MIME, GLib.Bytes.new(b'tile'))

    def _drop(self, dt, value, x, y):
        if Tile._dragging not in (None, self):
            self.win.move_toward(Tile._dragging, self)
        return True

    def sync_sbar(self):
        "scrollbar = ghosttys scrollbar-daten (total/offset/len in zeilen). ghostty sagt: pollen."
        if self.view.dead: return
        sb = ffi.new('GhosttyTerminalScrollbar*')
        lib.ghostty_terminal_get(self.view.term._t[0], lib.GHOSTTY_TERMINAL_DATA_SCROLLBAR, sb)
        vals = (sb.total, sb.offset, sb.len)
        if vals == self._sb: return
        self._sb = vals
        self.sbar.set_visible(sb.total > sb.len)
        with self.adj.handler_block(self._sbar_handler):   # sonst scrollt configure() den viewport
            self.adj.configure(sb.offset, 0, sb.total, 1, sb.len, sb.len)

    def _on_sbar(self, adj):
        "scrollbar gezogen -> viewport-oberkante absolut setzen (ROW, in zeilen ab scrollback-anfang)."
        self.view.scroll(lib.GHOSTTY_SCROLL_VIEWPORT_ROW, int(adj.get_value()))

    def _tick(self):
        "pwd im header (1x/s reicht)."
        if self.view.dead: return False
        try: cwd = self.view.cwd()
        except OSError: return True
        home = os.path.expanduser('~')
        lbl = '~' + cwd[len(home):] if cwd == home or cwd.startswith(home + '/') else cwd
        if lbl != self.pwd_lbl.get_label(): self.pwd_lbl.set_label(lbl)
        return True


class TermView(Gtk.DrawingArea):
    "terminal-fläche: pty + shell, ghostty-terminal, input-encoder, rendering aus dem render-state."

    def __init__(self, tile, cwd=None):
        super().__init__(hexpand=True, vexpand=True, focusable=True)   # ohne expand bleibt sie 0x0
        self.tile, self.win = tile, tile.win
        self.dead = False
        self.title = ''
        self.cols, self.rows = 80, 24
        self.term = Terminal(cols=80, rows=24, fg=TERM_FG, bg=TERM_BG)
        self.term.on_reply(self.write)      # antworten (DA, cursor-report, ...) gehen zurück ins pty
        self.enc = ffi.new('GhosttyKeyEncoder*'); lib.ghostty_key_encoder_new(ffi.NULL, self.enc)
        self.menc = ffi.new('GhosttyMouseEncoder*'); lib.ghostty_mouse_encoder_new(ffi.NULL, self.menc)
        # render-state + iteratoren einmal anlegen, pro frame nur befüllen
        self.rs = ffi.new('GhosttyRenderState*'); lib.ghostty_render_state_new(ffi.NULL, self.rs)
        self.rows_it = ffi.new('GhosttyRenderStateRowIterator*')
        lib.ghostty_render_state_row_iterator_new(ffi.NULL, self.rows_it)
        self.cells = ffi.new('GhosttyRenderStateRowCells*')
        lib.ghostty_render_state_row_cells_new(ffi.NULL, self.cells)
        self.colors = init_sized('GhosttyRenderStateColors')
        self.cursor = init_sized('GhosttyRenderStateCursor')
        self._rowcache = []     # pro viewport-zeile: (bg-rects, text-runs, zeichen je spalte)
        self.anchor = None      # zelle, an der die maus-markierung angefangen hat
        self.mouse_pos = (0.0, 0.0)
        self.set_font(self.win.font)
        self._start_shell(cwd)
        self.set_draw_func(self._draw)
        self.connect('resize', lambda area, w, h: self._fit(w, h))
        # tasten kommen window-level rein (Win._on_key), hier nur maus
        click = Gtk.GestureClick(button=0)          # alle buttons
        click.connect('pressed', self._press); click.connect('released', self._release)
        self.add_controller(click)
        mot = Gtk.EventControllerMotion(); mot.connect('motion', self._motion); self.add_controller(mot)
        scr = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL
                                        | Gtk.EventControllerScrollFlags.DISCRETE)
        scr.connect('scroll', self._scroll); self.add_controller(scr)
        menu = Gio.Menu()
        for items in ((('split rechts\tctrl+alt+right', 'split-h'), ('split unten\tctrl+alt+down', 'split-v')),
                      (('tile verschieben: links', 'move-left'), ('tile verschieben: rechts', 'move-right'),
                       ('tile verschieben: hoch', 'move-up'), ('tile verschieben: runter', 'move-down')),
                      (('kopieren\tctrl+shift+c', 'copy'), ('einfügen\tctrl+shift+v', 'paste'),
                       ('zoom in\tctrl+shift++', 'zoom-in'), ('zoom out\tctrl+shift+-', 'zoom-out')),
                      (('kachel schließen\tctrl+shift+w', 'close'),)):
            sec = Gio.Menu()
            for label, act in items: sec.append(label, 'win.' + act)
            menu.append_section(None, sec)
        self.pop = Gtk.PopoverMenu(menu_model=menu)
        self.pop.set_parent(self)

    # --- prozess / pty ---
    def _start_shell(self, cwd):
        shell = os.environ.get('TILIX2_SHELL') or os.environ.get('SHELL') or '/bin/sh'
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', self.rows, self.cols, 0, 0))
        env = dict(os.environ, TERM=TERM_NAME, COLORTERM='truecolor')
        # neue session + pty als controlling tty -> close(master) schickt der shell SIGHUP
        self.proc = subprocess.Popen([shell], stdin=slave, stdout=slave, stderr=slave, env=env,
                                     cwd=cwd if cwd and os.path.isdir(cwd) else None,
                                     start_new_session=True,
                                     preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        self._io_src = GLib.io_add_watch(self.master, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._io)

    def cwd(self):
        return os.readlink(f'/proc/{self.proc.pid}/cwd')

    def kill(self):
        "pty zu -> kernel schickt SIGHUP (bash schreibt dann noch die history). reapen async, nix blockiert."
        if self.term is None: return
        self.dead = True
        if self._io_src: GLib.source_remove(self._io_src)
        os.close(self.master)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.proc.pid, lambda pid, status: None)
        lib.ghostty_render_state_row_cells_free(self.cells[0])
        lib.ghostty_render_state_row_iterator_free(self.rows_it[0])
        lib.ghostty_render_state_free(self.rs[0])
        lib.ghostty_key_encoder_free(self.enc[0])
        lib.ghostty_mouse_encoder_free(self.menc[0])
        self.term.close()
        self.term = None
        self.pop.unparent()

    def write(self, data):
        if self.dead: return
        try: os.write(self.master, data)
        except OSError: pass

    def _io(self, fd, cond):
        try: data = os.read(fd, 65536)
        except OSError: data = b''        # EIO = shell weg
        if not data:
            # prozess beendet (ctrl+d/exit): kachel gibt ihren platz frei (tilix-verhalten)
            self.dead = True
            self._io_src = None           # return False entfernt die source
            GLib.idle_add(self.win.close_tile, self.tile)
            return False
        self.term.feed(data)
        t = self._title()
        if t != self.title:
            self.title = t
            if self.win.active is self.tile: self.win.set_title(t or 'tilix2')
        self.changed()
        return True

    def _title(self):
        s = ffi.new('GhosttyString*')
        lib.ghostty_terminal_get(self.term._t[0], lib.GHOSTTY_TERMINAL_DATA_TITLE, s)
        return ffi.buffer(s.ptr, s.len)[:].decode('utf8', 'replace') if s.len else ''

    def changed(self):
        "nach jeder änderung am terminal: neu zeichnen, scrollbar nachziehen."
        self.queue_draw()
        self.tile.sync_sbar()

    def scroll(self, tag, delta=0):
        "viewport verschieben: DELTA (zeilen, - = hoch), ROW (absolut), BOTTOM."
        if self.dead: return
        v = ffi.new('ScrollViewportS*')
        v.tag, v.delta = tag, delta
        _scroll_viewport(self.term._t[0], v[0])
        self.changed()

    # --- font / größe ---
    def set_font(self, font):
        "font setzen (zoom), zellgröße messen, grid neu einpassen."
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
        self._rowcache.clear()      # layouts hängen am alten font
        if self.get_width():
            self._fit(self.get_width(), self.get_height())
        self.queue_draw()

    def _fit(self, wpx, hpx):
        cols, rows = max(4, wpx // self.cell_w), max(2, hpx // self.cell_h)
        if (cols, rows) == (self.cols, self.rows) or self.dead: return
        self.cols, self.rows = cols, rows
        self.term.resize(cols, rows, self.cell_w, self.cell_h)
        try: fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        except OSError: pass
        self.tile.sync_sbar()


    # --- tastatur ---
    def send_key(self, keyval, state, keycode=0, text=None):
        """keyval/state -> ghostty key-encoder -> pty. returns True wenn behandelt.
        text: was das IM committed hat (dead keys/compose); sonst aus dem keyval abgeleitet."""
        if self.dead: return False
        name = Gdk.keyval_name(keyval) or ''
        if name in MODIFIER_KEYS: return False
        mods = ghostty_mods(state)
        if text is None:
            u = Gdk.keyval_to_unicode(keyval)
            text = chr(u) if u >= 0x20 and u != 0x7f else None   # steuerzeichen macht der encoder selbst
        gk = KEYMAP.get(name)
        if gk is None:
            if not text: return False
            gk = 'UNIDENTIFIED'
        u = Gdk.keyval_to_unicode(base_keyval(keycode, keyval))   # kitty will den unshifted codepoint
        ev = ffi.new('GhosttyKeyEvent*')
        lib.ghostty_key_event_new(ffi.NULL, ev)
        lib.ghostty_key_event_set_action(ev[0], lib.GHOSTTY_KEY_ACTION_PRESS)
        lib.ghostty_key_event_set_key(ev[0], getattr(lib, 'GHOSTTY_KEY_' + gk))
        lib.ghostty_key_event_set_mods(ev[0], mods)
        # shift hat den text erzeugt ('A', '!') -> verbraucht, sonst schickt kitty CSI 97;2u statt 'A'
        lib.ghostty_key_event_set_consumed_mods(ev[0], mods & SHIFT if text else 0)
        if u >= 0x20: lib.ghostty_key_event_set_unshifted_codepoint(ev[0], u)
        if text:
            b = text.encode()
            lib.ghostty_key_event_set_utf8(ev[0], b, len(b))
        lib.ghostty_key_encoder_setopt_from_terminal(self.enc[0], self.term._t[0])
        out = ffi.new('char[]', 128); n = ffi.new('size_t*')
        r = lib.ghostty_key_encoder_encode(self.enc[0], ev[0], out, len(out), n)
        lib.ghostty_key_event_free(ev[0])
        if r == 0 and n[0]:
            if not self.term.get('viewport_active'):   # beim tippen zurück nach unten
                self.scroll(lib.GHOSTTY_SCROLL_VIEWPORT_BOTTOM)
            self.write(ffi.buffer(out, n[0])[:])
        return True

    # --- maus ---
    def _cell_at(self, x, y):
        return (min(self.cols - 1, max(0, int(x // self.cell_w))),
                min(self.rows - 1, max(0, int(y // self.cell_h))))

    def _mouse_to_app(self, state):
        "app will die maus (mouse-tracking) — außer shift ist gedrückt (dann markieren wir selbst)."
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
            self.paste(primary=True)          # middle-click: primary selection (X11/tilix-verhalten)
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
            self.copy(primary_only=True)      # markieren = primary selection (X11-konvention)

    def _motion(self, m, x, y):
        if self.dead: return
        self.mouse_pos = (x, y)
        state = m.get_current_event_state()
        if self.anchor is not None:
            cell = self._cell_at(x, y)
            if cell == self.anchor: self.select(None)      # klick-zittern ist keine markierung
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
            # alternate screen ohne maus (less, man): rad -> pfeiltasten (xterm "alternate scroll")
            for _ in range(3): self.send_key(Gdk.KEY_Up if dy < 0 else Gdk.KEY_Down, 0)
        else:
            self.scroll(lib.GHOSTTY_SCROLL_VIEWPORT_DELTA, 3 if dy > 0 else -3)
        return True

    # --- markierung / clipboard ---
    def select(self, a, b=None):
        "markierung von zelle a bis b (viewport-koordinaten); ghostty trackt sie beim scrollen mit."
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
        "markierten text ins clipboard + primary. ohne markierung passiert nichts (tilix-verhalten)."
        text = self.selected_text()
        if not text: return
        cp = Gdk.ContentProvider.new_for_value(text)   # gtk serialisiert in alle text-formate
        self.get_display().get_primary_clipboard().set_content(cp)
        if not primary_only: self.get_display().get_clipboard().set_content(cp)

    def paste(self, primary=False):
        "clipboard (ctrl+shift+v) oder primary selection (middle-click)."
        def done(src, res):
            try: t = src.read_text_finish(res)
            except GLib.Error: return
            if not t or self.dead: return
            # ghostty_paste_encode: ESC & co. -> space (kein ausbruch aus bracketed paste),
            # klammert bei mode 2004, sonst \n -> \r. data wird in-place gesäubert.
            data = t.encode()
            out = ffi.new('char[]', len(data) + 12)    # + '\e[200~' + '\e[201~'
            n = ffi.new('size_t*')
            if lib.ghostty_paste_encode(ffi.new('char[]', data), len(data), self.term.mode(2004),
                                        out, len(out), n) == 0:
                self.write(ffi.buffer(out, n[0])[:])
        d = self.get_display()
        (d.get_primary_clipboard() if primary else d.get_clipboard()).read_text_async(None, done)

    # --- rendering ---
    def _build_row(self, FG, BG):
        """aktuelle iterator-zeile -> (bg-rects, text-runs, zeichen je spalte).
        läuft nur für dirty rows; die pango-layouts werden mitgecached (shaping ist das teure)."""
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
        bgs, runs, chars = [], [], []
        run = None              # [x, text, ncells, fg, bold, italic, deco]
        x = 0
        while lib.ghostty_render_state_row_cells_next(cells):
            if get(cells, lib.GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8, gb) == 0:
                text = ffi.buffer(buf, gb.len)[:].decode('utf8', 'replace')
            else:
                text = '\ufffd'          # grapheme-cluster > 1 KiB: zeigen wir nicht

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
            else:
                get(cells, lib.GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW, raw)
                lib.ghostty_cell_get(raw[0], lib.GHOSTTY_CELL_DATA_WIDE, wide)
                n = 2 if wide[0] == lib.GHOSTTY_CELL_WIDE_WIDE else 1
                key = (fg, bold, ital, deco)
                if run and n == 1 and run[2] == x - run[0] and run[3:] == list(key):
                    run[1] += text; run[2] += 1
                else:
                    run = [x, text, n, *key]
                    runs.append(run)
            x += 1
        out = []
        for x, text, n, fg, bold, ital, deco in runs:
            layout = Pango.Layout(self.get_pango_context())
            layout.set_font_description(self.fonts[bold, ital])
            layout.set_text(text)
            # wide chars (cjk, emoji) auf genau 2 zellen skalieren
            scale = 2 * self.cell_w / max(1, layout.get_pixel_size()[0]) if n == 2 else 0
            out.append((x, n, fg, layout, deco, scale))
        return bgs, out, chars

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
            bgs, runs, _ = cache[y]
            for x, n, col in bgs:
                cr.set_source_rgb(*col)
                cr.rectangle(x * cw, y * ch, n * cw, ch)
                cr.fill()
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
            y += 1
        del cache[y:]
        lib.ghostty_render_state_clean(rs)
        self._draw_cursor(cr, FG, BG)
        if self.has_focus() and len(self.win.tiles()) > 1:
            cr.set_source_rgb(0.21, 0.52, 0.89)
            cr.set_line_width(2)
            cr.rectangle(1, 1, width - 2, height - 2)
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
            # block: zelle füllen, zeichen darunter in hintergrundfarbe drüber
            cr.rectangle(x * cw, y * ch, cw, ch); cr.fill()
            chars = self._rowcache[y][2]
            if x < len(chars) and chars[x]:
                layout = Pango.Layout(self.get_pango_context())
                layout.set_font_description(self.fonts[False, False])
                layout.set_text(chars[x])
                cr.set_source_rgb(*BG)
                cr.move_to(x * cw, y * ch)
                PangoCairo.show_layout(cr, layout)


class Win(Gtk.ApplicationWindow):
    "fenster: headerbar + baum aus Gtk.Paned, blätter sind Tiles."

    def __init__(self, app):
        super().__init__(application=app, title='tilix2', default_width=1000, default_height=700)
        self.font = DEFAULT_FONT
        self.active = None            # aktive kachel: bekommt tasten, wird gesplittet/gezoomt
        kc = Gtk.EventControllerKey()
        kc.connect('key-pressed', self._on_key)
        self.add_controller(kc)
        # input method: dead keys (^ ´ `), compose, ibus. bekommt jede taste, committed text
        # geht als utf8 mit in den key-encoder (siehe _on_key).
        self.im = Gtk.IMMulticontext()
        self.im.set_client_widget(self)
        self.im.connect('commit', self._im_commit)
        self._im_text = None          # != None nur während filter_keypress läuft
        kc.connect('key-released', lambda c, *a: self.im.filter_keypress(c.get_current_event()))
        self.connect('notify::is-active', lambda w, p: self.im.focus_in() if w.is_active() else self.im.focus_out())
        for name in ('split-h', 'split-v', 'close', 'copy', 'paste', 'zoom-in', 'zoom-out',
                     'zoom-reset', 'move-left', 'move-right', 'move-up', 'move-down'):
            a = Gio.SimpleAction.new(name, None)
            a.connect('activate', lambda a, p, name=name: self.action(name))
            self.add_action(a)
        self.connect('close-request', self._on_close)

        hb = Gtk.HeaderBar()
        self.count_lbl = Gtk.Label(label='1 kachel')
        hb.pack_start(self.count_lbl)
        def hbtn(action, icon, tip):
            # focus_on_click=False: sonst hat der button danach den fokus und frisst enter/space
            return Gtk.Button(icon_name=icon, tooltip_text=tip, action_name='win.' + action, focus_on_click=False)
        hb.pack_start(hbtn('split-h', 'go-next-symbolic', 'split rechts (ctrl+alt+right)'))
        hb.pack_start(hbtn('split-v', 'go-down-symbolic', 'split unten (ctrl+alt+down)'))
        mv = Gio.Menu()
        for label, act in (('nach links', 'move-left'), ('nach rechts', 'move-right'),
                           ('nach oben', 'move-up'), ('nach unten', 'move-down')):
            mv.append(label, 'win.' + act)
        hb.pack_start(Gtk.MenuButton(icon_name='object-select-symbolic', menu_model=mv,
                                     tooltip_text='tile verschieben', focus_on_click=False))
        hb.pack_end(hbtn('close', 'window-close-symbolic', 'kachel schließen (ctrl+shift+w)'))
        hb.pack_end(hbtn('zoom-out', 'zoom-out-symbolic', 'verkleinern (ctrl+shift+-)'))
        hb.pack_end(hbtn('zoom-in', 'zoom-in-symbolic', 'vergrößern (ctrl+shift++)'))
        hb.pack_end(hbtn('paste', 'edit-paste-symbolic', 'einfügen (ctrl+shift+v)'))
        hb.pack_end(hbtn('copy', 'edit-copy-symbolic', 'kopieren (ctrl+shift+c)'))
        self.set_titlebar(hb)

        self.box = Gtk.Box(hexpand=True, vexpand=True)
        self.set_child(self.box)
        self.active = Tile(self)
        self.box.append(self.active)

    def tiles(self, w=None):
        "alle kacheln im baum (links/oben zuerst)."
        w = w or self.box.get_first_child()
        if isinstance(w, Tile): return [w]
        if isinstance(w, Gtk.Paned): return self.tiles(w.get_start_child()) + self.tiles(w.get_end_child())
        return []

    def activate_tile(self, tile):
        self.active = tile
        tile.view.grab_focus()
        self.set_title(tile.view.title or 'tilix2')
        n = len(self.tiles())
        self.count_lbl.set_label(f'{n} kachel' + ('n' if n != 1 else ''))

    def _on_close(self, w):
        for t in self.tiles(): t.view.kill()
        return False

    def _on_key(self, ctrl, keyval, keycode, state):
        "window-level key input: shortcuts global, rest an die aktive kachel."
        name = Gdk.keyval_name(keyval) or ''
        if state & MT.CONTROL_MASK and state & MT.SHIFT_MASK:
            # nach physischer taste matchen, sonst ist ctrl+shift+0 auf de ein '=' (und auf us ')')
            act = {'c': 'copy', 'v': 'paste', 'w': 'close', 'plus': 'zoom-in', 'equal': 'zoom-in',
                   'KP_Add': 'zoom-in', 'minus': 'zoom-out', 'KP_Subtract': 'zoom-out',
                   '0': 'zoom-reset', 'KP_0': 'zoom-reset'}.get(Gdk.keyval_name(base_keyval(keycode, keyval)))
            if act: self.action(act); return True
        if state & MT.CONTROL_MASK and state & MT.ALT_MASK and name in ('Right', 'Down'):
            self.action('split-h' if name == 'Right' else 'split-v'); return True
        if not self.active: return False
        ev = ctrl.get_current_event() if ctrl else None   # tests rufen ohne controller
        if ev is not None:
            self._im_text = ''
            filtered = self.im.filter_keypress(ev)
            text, self._im_text = self._im_text, None
            if filtered and not text: return True        # dead key / compose läuft noch
            if filtered: return self.active.view.send_key(keyval, state, keycode, text)
        return self.active.view.send_key(keyval, state, keycode)

    def _im_commit(self, im, text):
        if self._im_text is not None:
            self._im_text += text             # synchron aus filter_keypress -> _on_key schickt's
        elif self.active:
            self.active.view.write(text.encode())   # asynchron (ibus): direkt als text

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
            t.view.set_font(desc.to_string())    # nur die aktive kachel (tilix-verhalten)
        elif name.startswith('move-'): self.move(t, name[5:])

    # --- tiling-baum aus Gtk.Paned ---
    def split(self, tile, vertical):
        "kachel halbieren, neue kachel (im selben cwd) auf die andere hälfte, die neue wird aktiv."
        def do_split(widget, clock):
            # die split-position kommt aus der größe der kachel. eine gerade erst entstandene
            # kachel (split direkt nach split) hat noch kein layout -> einen frame warten.
            if not tile.get_width():
                return True
            paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                              hexpand=True, vexpand=True)
            paned.set_position((tile.get_height() if vertical else tile.get_width()) // 2)
            try: cwd = tile.view.cwd()
            except OSError: cwd = None
            parent = tile.get_parent()
            self._replace(parent, tile, None)
            new = Tile(self, cwd)
            paned.set_start_child(tile)
            paned.set_end_child(new)
            self._replace(parent, None, paned)
            self.activate_tile(new)
            return False
        tile.add_tick_callback(do_split)

    def _replace(self, parent, old, new):
        "child old von parent (Paned oder self.box) durch new ersetzen; None = leer / einhängen."
        if isinstance(parent, Gtk.Paned):
            if parent.get_start_child() is old: parent.set_start_child(new)
            else: parent.set_end_child(new)
        else:
            if old is not None: self.box.remove(old)
            if new is not None: self.box.append(new)

    def close_tile(self, tile):
        "kachel schließen; der nachbar übernimmt den platz. letzte kachel -> fenster zu."
        if tile not in self.tiles(): return    # schon weg (z. b. ctrl+shift+w und exit gleichzeitig)
        parent = tile.get_parent()
        tile.view.kill()
        if not isinstance(parent, Gtk.Paned):
            self.destroy()
            return
        other = parent.get_end_child() if parent.get_start_child() is tile else parent.get_start_child()
        self._replace(parent, other, None)
        self._replace(parent.get_parent(), parent, other)
        self.activate_tile(other if isinstance(other, Tile) else self.tiles(other)[0])

    def move(self, tile, direction):
        "kachel mit ihrem nachbarn im selben paned tauschen, wenn die richtung passt."
        p = tile.get_parent()
        if not isinstance(p, Gtk.Paned): return
        if (direction in ('left', 'right')) != (p.get_orientation() == Gtk.Orientation.HORIZONTAL): return
        first, second = p.get_start_child(), p.get_end_child()
        if (second is tile and direction in ('left', 'up')) or (first is tile and direction in ('right', 'down')):
            self._swap_children(p)

    def move_toward(self, src, dst):
        "drag&drop: src mit dst tauschen, wenn beide im selben paned hängen."
        p = src.get_parent()
        if src is not dst and isinstance(p, Gtk.Paned) and dst.get_parent() is p:
            self._swap_children(p)
            self.activate_tile(src)

    def _swap_children(self, p):
        "GTK4: children erst auf None, dann vertauscht neu setzen (ein widget kann nicht zwei slots haben)."
        a, b, pos = p.get_start_child(), p.get_end_child(), p.get_position()
        p.set_start_child(None); p.set_end_child(None)
        p.set_start_child(b); p.set_end_child(a)
        p.set_position(pos)


SLIM_CSS = b"""
/* Adwaita: trough margin 8px + slider border -> 24px breite scrollbar. hier: 5px. */
scrollbar.vertical.tilix2-slim { min-width: 5px; min-height: 5px; }
scrollbar.vertical.tilix2-slim > range > trough {
  margin: 0; padding: 0; min-width: 5px; min-height: 5px; background: transparent; border: none;
}
scrollbar.vertical.tilix2-slim > range > trough > slider {
  min-width: 5px; min-height: 20px; margin: 0; padding: 0; border: none; border-radius: 2px;
  background-color: rgba(160,160,160,0.45);
}
scrollbar.vertical.tilix2-slim > range > trough > slider:hover { background-color: rgba(180,180,180,0.7); }
paned separator { min-width: 1px; min-height: 1px; background: rgba(128,128,128,0.35); }
paned separator:hover { background: rgba(90,150,220,0.8); }
"""


class App(Gtk.Application):
    def __init__(self):
        # NON_UNIQUE: jeder start ist ein eigener prozess (kein d-bus-handoff an eine laufende
        # instanz -> crash reißt nur ein fenster mit, tests laufen neben der echten app)
        super().__init__(application_id='osuv.tilix2', flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_data(SLIM_CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Win(self).present()


if __name__ == '__main__':
    sys.exit(App().run(sys.argv))
