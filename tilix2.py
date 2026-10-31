#!/usr/bin/env python3
# tilix2.py — tilix-klon in python: gtk4-tiling (klick-splits per rechtsklick-menü,
# split-resize per drag am paned-griff, tile-move per menü) mit pyghostty/libghostty-vt
# als terminal-backend. kitty keyboard protocol inklusive, damit z. b. ctrl+enter
# in TUI-apps ankommt (CSI 13;5u statt kollidierendem \r).
#
# start:  python3 -m venv --system-site-packages venv && ./venv/bin/pip install pyghostty
#         ./venv/bin/python tilix2.py     (--system-site-packages holt sich gi/gtk4 vom system)
#         python3 tilix2.py --selftest  smoke-test (exit 0 = ok)
# env:    TILIX2_SHELL, TILIX2_FONT ("Monospace 12"), TILIX2_FG / TILIX2_BG ("#rrggbb")
# steuerung: rechtsklick auf kachel -> split/move/close-menü; split-resize per drag am
#            paned-griff; ctrl+alt+right/down split, ctrl+shift+w close, ctrl+shift+c/v
#            copy/paste (bracketed paste), ctrl+shift+ +/- zoom, maus-rad scrollt scrollback
#            (bzw. maus-events, wenn die app mouse-tracking anfordert)
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

# --- ghostty scroll_viewport (tagged union by value -> ABI-twin wie in pyghostty._ffi) ---
ffi.cdef('typedef struct { uint32_t tag; uint32_t _pad; int64_t delta; int64_t _pad2; } ScrollViewportS;')
if ffi.sizeof('ScrollViewportS') != ffi.sizeof('GhosttyTerminalScrollViewport'):
    sys.exit('ABI mismatch: ScrollViewportS twin does not match GhosttyTerminalScrollViewport')
scroll_viewport = union_fn('ghostty_terminal_scroll_viewport',
                           'GhosttyResult(*)(GhosttyTerminal, ScrollViewportS)')
_grid_ref = union_fn('ghostty_terminal_grid_ref',
                     'GhosttyResult(*)(GhosttyTerminal, GhosttyPointS, GhosttyGridRef*)')

# --- keyval-name -> GHOSTTY_KEY_* mapping (gdk keyval-namen sind x11-keysym-namen) ---
KEYMAP = {
    'Return': 'ENTER', 'KP_Enter': 'NUMPAD_ENTER', 'Escape': 'ESCAPE', 'BackSpace': 'BACKSPACE',
    'Tab': 'TAB', 'ISO_Left_Tab': 'TAB', 'space': 'SPACE',
    'Left': 'ARROW_LEFT', 'Right': 'ARROW_RIGHT', 'Up': 'ARROW_UP', 'Down': 'ARROW_DOWN',
    'Home': 'HOME', 'End': 'END', 'Prior': 'PAGE_UP', 'Page_Up': 'PAGE_UP',
    'Next': 'PAGE_DOWN', 'Page_Down': 'PAGE_DOWN', 'Insert': 'INSERT', 'Delete': 'DELETE',
    'Pause': 'PAUSE', 'Print': 'PRINT_SCREEN', 'Menu': 'CONTEXT_MENU', 'Help': 'HELP',
    'Caps_Lock': 'CAPS_LOCK', 'Num_Lock': 'NUM_LOCK', 'Scroll_Lock': 'SCROLL_LOCK',
    'Control_L': 'CONTROL_LEFT', 'Control_R': 'CONTROL_RIGHT',
    'Shift_L': 'SHIFT_LEFT', 'Shift_R': 'SHIFT_RIGHT',
    'Alt_L': 'ALT_LEFT', 'Alt_R': 'ALT_RIGHT', 'AltGr': 'ALT_RIGHT',
    'Super_L': 'META_LEFT', 'Super_R': 'META_RIGHT', 'Meta_L': 'META_LEFT', 'Meta_R': 'META_RIGHT',
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
for _i, _c in enumerate('0123456789'): KEYMAP[_c] = f'DIGIT_{_c}'
for _c in 'abcdefghijklmnopqrstuvwxyz': KEYMAP[_c] = KEYMAP[_c.upper()] = _c.upper()
for _i in range(1, 36): KEYMAP[f'F{_i}'] = f'F{_i}'
for _i in range(10): KEYMAP[f'KP_{_i}'] = f'NUMPAD_{_i}'

MODIFIER_KEYS = {'Shift_L', 'Shift_R', 'Control_L', 'Control_R', 'Alt_L', 'Alt_R', 'AltGr',
                 'Super_L', 'Super_R', 'Meta_L', 'Meta_R', 'Caps_Lock', 'Num_Lock', 'Scroll_Lock'}

# ghostty mods bit-packing (verifiziert gegen libghostty-vt): shift=1 ctrl=2 alt=4 super=8
# hyper=16 meta=32 caps=64
def ghostty_mods(state):
    MT = Gdk.ModifierType
    m = 0
    if state & MT.SHIFT_MASK: m |= 1
    if state & MT.CONTROL_MASK: m |= 2
    if state & MT.ALT_MASK: m |= 4
    if state & MT.SUPER_MASK: m |= 8
    if state & MT.HYPER_MASK: m |= 16
    if state & MT.META_MASK: m |= 32
    if state & MT.LOCK_MASK: m |= 64
    return m

def hexrgb(s, default):
    "'#rrggbb' -> (r,g,b); bei unsinnigem input (red, #fff, leer) default statt traceback."
    if not s: return default
    s = s.lstrip('#')
    try:
        if len(s) != 6: raise ValueError
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default

TERM_NAME = 'xterm-ghostty' if shutil.which('infocmp') and subprocess.run(
    ['infocmp', 'xterm-ghostty'], capture_output=True).returncode == 0 else 'xterm-256color'
DEFAULT_FONT = os.environ.get('TILIX2_FONT', 'Monospace 11')   # zoom-reset-ziel, einmal gelesen


class Tile(Gtk.Box):
    "kachel-wrapper: header (pwd + drag-to-move) oben, dünne scrollbar rechts, TermView in der mitte."
    MIME = 'application/x-tilix2-tile'

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True); self.set_vexpand(True)
        self.win = win
        self._sb_top = 0        # viewport-top in history-zeilen (für scrollbar + selection)
        # header mit pwd + drag
        self.head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.head.set_margin_top(2); self.head.set_margin_bottom(2)
        self.head.set_margin_start(6); self.head.set_margin_end(6)
        grip = Gtk.Image(icon_name='open-menu-symbolic'); grip.set_pixel_size(12)
        self.head.append(grip)
        self.pwd_lbl = Gtk.Label(label='~', xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.pwd_lbl.set_hexpand(True)
        self.head.append(self.pwd_lbl)
        self.append(self.head)
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.set_vexpand(True)
        self.append(body)
        self.view = TermView(win)
        self.view.tile = self
        body.append(self.view)
        # scrollbar rechts, dünn (css in main())
        self.adj = Gtk.Adjustment()
        self.sbar = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=self.adj)
        self.sbar.set_visible(False)
        self.sbar.add_css_class('tilix2-slim')
        self.adj.connect('value-changed', self._on_sbar)
        body.append(self.sbar)
        # drag-to-move: header ziehen -> auf anderer kachel droppen tauscht im paned
        ds = Gtk.DragSource(); ds.set_actions(Gdk.DragAction.MOVE)
        ds.connect('prepare', self._drag_prepare)
        self.head.add_controller(ds)
        dt = Gtk.DropTarget.new(Gdk.ContentFormats.new([self.MIME]), Gdk.DragAction.MOVE)
        dt.connect('drop', self._drop)
        self.add_controller(dt)
        win.terms.add(self)
        GLib.timeout_add(1000, self._tick)

    _dragging = None   # class-level: welche tile gerade gezogen wird (payload kann keine widget-ref halten)

    def _drag_prepare(self, src, x, y):
        Tile._dragging = self
        return Gdk.ContentProvider.new_for_bytes(self.MIME, GLib.Bytes.new(b'tile'))

    def _drop(self, dt, value, x, y):
        src = Tile._dragging
        if src is not None and src is not self:
            self.win.move_toward(src, self)
        return True

    def _on_sbar(self, adj):
        "scrollbar drag -> viewport absolut setzen (ROW = top-of-viewport in history-zeilen)."
        if self.view.dead: return
        v = ffi.new('ScrollViewportS*')
        v.tag = lib.GHOSTTY_SCROLL_VIEWPORT_ROW
        v.delta = int(adj.get_value())
        scroll_viewport(self.view.term._t[0], v[0])
        self._sb_top = int(adj.get_value())
        self.view._grid_version += 1
        self.view._cell_cache.clear()
        self.view.queue_draw()

    def _tick(self):
        "pwd + scrollbar aktualisieren (1x/s reicht)."
        if self.view.dead:
            return False
        try:
            cwd = os.readlink(f'/proc/{self.view.proc.pid}/cwd')
            home = os.path.expanduser('~')
            lbl = cwd.replace(home, '~', 1) if cwd.startswith(home) else cwd
            if lbl != self.pwd_lbl.get_label(): self.pwd_lbl.set_label(lbl)
        except OSError: pass
        t = self.view.term
        total = t.get('total_rows', 'uint64_t')
        sb = t.get('scrollback_rows', 'uint64_t')
        rows = self.view.rows
        if t.get('viewport_active'):
            self._sb_top = sb          # am ende -> ganz unten
        self.sbar.set_visible(sb > 0)
        if sb > 0:
            # value = viewport-top-row; block/unblock, damit value-changed nicht feuert
            self.adj.handler_block_by_func(self._on_sbar)
            self.adj.set_upper(total)
            self.adj.set_page_size(rows)
            self.adj.set_value(self._sb_top)
            self.adj.handler_unblock_by_func(self._on_sbar)
        return True


class TermView(Gtk.DrawingArea):
    "terminal-fläche: pyghostty-terminal + pty/shell + cairo-rendering + input-encoder"

    def __init__(self, win):
        super().__init__()
        self.set_hexpand(True); self.set_vexpand(True)   # gtk4: ohne expand bleibt die kachel 0x0
        self.set_focusable(True)
        self.win = win
        self.tile = None              # setzt Tile nach dem bau
        self.font = win.font          # zoom ist pro kachel (wie tilix), nicht global
        self.term = Terminal(cols=80, rows=24,
                             fg=hexrgb(os.environ.get('TILIX2_FG'), (212, 212, 212)),
                             bg=hexrgb(os.environ.get('TILIX2_BG'), (24, 26, 32)))
        self.term.on_reply(self._reply)
        self.enc = ffi.new('GhosttyKeyEncoder*');  lib.ghostty_key_encoder_new(ffi.NULL, self.enc)
        self.menc = ffi.new('GhosttyMouseEncoder*'); lib.ghostty_mouse_encoder_new(ffi.NULL, self.menc)
        self.cell_w, self.cell_h, self.ascent = 8, 16, 13
        self.cols, self.rows = 80, 24
        self.sel = None            # (x1, y1, x2, y2), evtl. vertauscht
        self.selecting = False
        self.dead = False
        self.title = ''
        self.mouse_pos = (0.0, 0.0)
        self._cell_cache = {}          # (x,y) -> (grid_version, info)
        self._grid_version = 0
        self._start_shell()
        self.set_draw_func(self._draw)
        # key-input läuft window-level (siehe Win._on_key) — widget-level ist zu fragil,
        # weil nach present() erstmal niemand fokus hat
        click = Gtk.GestureClick()
        click.set_button(0)   # alle buttons, rechtsklick = menü
        click.connect('pressed', self._press); click.connect('released', self._release)
        self.add_controller(click)
        mot = Gtk.EventControllerMotion(); mot.connect('motion', self._motion); self.add_controller(mot)
        scr = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL
                                        | Gtk.EventControllerScrollFlags.DISCRETE)
        scr.connect('scroll', self._scroll); self.add_controller(scr)
        # rechtsklick-menü
        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append('split rechts	ctrl+alt+right', 'win.split-h')
        s1.append('split unten	ctrl+alt+down', 'win.split-v')
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append('tile verschieben: links', 'win.move-left'); s2.append('tile verschieben: rechts', 'win.move-right')
        s2.append('tile verschieben: hoch', 'win.move-up');   s2.append('tile verschieben: runter', 'win.move-down')
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append('kopieren	ctrl+shift+c', 'win.copy'); s3.append('einfügen	ctrl+shift+v', 'win.paste')
        s3.append('zoom in	ctrl+shift++', 'win.zoom-in'); s3.append('zoom out	ctrl+shift+-', 'win.zoom-out')
        menu.append_section(None, s3)
        s4 = Gio.Menu(); s4.append('kachel schließen	ctrl+shift+w', 'win.close')
        menu.append_section(None, s4)
        self.pop = Gtk.PopoverMenu(); self.pop.set_menu_model(menu); self.pop.set_parent(self)

    # --- prozess/pty ---
    def _start_shell(self):
        shell = os.environ.get('TILIX2_SHELL') or os.environ.get('SHELL') or '/bin/sh'
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', self.rows, self.cols, 0, 0))
        env = dict(os.environ, TERM=TERM_NAME, COLORTERM='truecolor')
        # neue session + pty als controlling tty -> close(master) schickt der shell SIGHUP
        self.proc = subprocess.Popen([shell], stdin=slave, stdout=slave, stderr=slave, env=env,
                                     start_new_session=True,
                                     preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        # WICHTIG: io-watch erst starten, wenn das widget第一次 allokiert ist.
        # ein io-callback (feed+queue_draw) während der initialen layout-phase lässt ein
        # dynamisch eingehängtes Gtk.Paned dauerhaft 0px breit (empirisch verifiziert).
        self._io_src = None
        self._want_watch = True

    def kill(self):
        "pty zu -> kernel schickt SIGHUP (bash schreibt dann noch die history). reapen async, nix blockiert."
        if self.term._t is None: return
        self.dead = True
        if self._io_src:
            GLib.source_remove(self._io_src)
            self._io_src = None
        try: os.close(self.master)
        except OSError: pass
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.proc.pid, lambda pid, status: None)
        self.term.close()

    def write(self, data):
        if self.dead: return
        try: os.write(self.master, data)
        except OSError: pass

    def _reply(self, data):
        self.write(data)

    def _io(self, fd, cond):
        if self.term._t is None: return False
        try: data = os.read(fd, 65536)
        except OSError: data = b''
        if data:
            try: self.term.feed(data)
            except Exception: pass
            self._grid_version += 1
            t = self._title()
            if t != self.title:
                self.title = t
                if self.has_focus(): self.win.set_title(t or 'tilix2')
            # WICHTIG: queue_draw() hier — NICHT während des initialen layout (split-phase).
            # ein queue_draw auf einem noch nicht allokierten widget kann GTK4's paned-allocate
            # dauerhaft auf 0px stehen lassen (empirisch: split nach shell-start mit watch+draw
            # bleibt paned 0px breit). nach dem ersten draw ist es sicher.
            if self.get_mapped():
                self.queue_draw()
        if not data or cond & (GLib.IO_HUP | GLib.IO_ERR):
            # prozess beendet (z. b. ctrl+d/exit): kachel gibt ihren platz frei (tilix-verhalten)
            self.dead = True
            self._io_src = None                   # return False entfernt die source schon
            GLib.idle_add(self.win.close, self.tile)
            return False
        return True

    def _title(self):
        s = ffi.new('GhosttyString*')
        lib.ghostty_terminal_get(self.term._t[0], lib.GHOSTTY_TERMINAL_DATA_TITLE, s)
        return ffi.buffer(s.ptr, s.len)[:].decode('utf8', 'replace') if s.len else ''

    # --- resize ---
    _metrics_cache = {}   # font-string -> (cell_w, ascent, cell_h, desc); pango get_metrics pro frame ist teuer
    def _update_metrics(self):
        if self.font in self._metrics_cache:
            self.cell_w, self.ascent, self.cell_h, self.font_desc = self._metrics_cache[self.font]
            return
        desc = Pango.FontDescription.from_string(self.font)
        m = self.get_pango_context().get_metrics(desc)
        self.font_desc = desc
        self.cell_w = max(1, -(-m.get_approximate_char_width() // Pango.SCALE))
        self.ascent = m.get_ascent() // Pango.SCALE
        self.cell_h = max(1, (m.get_ascent() + m.get_descent()) // Pango.SCALE)
        self._metrics_cache[self.font] = (self.cell_w, self.ascent, self.cell_h, desc)

    def _fit(self, wpx, hpx):
        self._update_metrics()
        cols, rows = max(4, wpx // self.cell_w), max(2, hpx // self.cell_h)
        if (cols, rows) != (self.cols, self.rows):
            self.cols, self.rows = cols, rows
            self.term.resize(cols, rows, self.cell_w, self.cell_h)
            self._grid_version += 1
            self._cell_cache.clear()
            try: fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            except OSError: pass

    def do_size_allocate(self, width, height, baseline):
        if self.term._t is None: return   # gekillte kachel nicht mehr fitten
        Gtk.DrawingArea.do_size_allocate(self, width, height, baseline)
        if getattr(self, "_want_watch", False) and getattr(self, "master", None) is not None:
            self._want_watch = False
            self._io_src = GLib.io_add_watch(self.master, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._io)
        self._fit(width, height)
        self.queue_draw()

    # --- input ---
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
        # unshifted codepoint (kitty): taste ohne modifier über das layout auflösen (de: shift+1 -> '1')
        ok, base = False, 0
        if keycode:
            ok, base, *_ = Gdk.Display.get_default().translate_key(keycode, 0, 0)
        u = Gdk.keyval_to_unicode(base if ok else Gdk.keyval_to_lower(keyval))
        ev = ffi.new('GhosttyKeyEvent*')
        lib.ghostty_key_event_new(ffi.NULL, ev)
        lib.ghostty_key_event_set_action(ev[0], lib.GHOSTTY_KEY_ACTION_PRESS)
        lib.ghostty_key_event_set_key(ev[0], getattr(lib, 'GHOSTTY_KEY_' + gk))
        lib.ghostty_key_event_set_mods(ev[0], mods)
        # shift hat den text erzeugt ('A', '!') -> verbraucht, sonst schickt kitty CSI 97;2u statt 'A'
        lib.ghostty_key_event_set_consumed_mods(ev[0], mods & 1 if text else 0)
        if u >= 0x20: lib.ghostty_key_event_set_unshifted_codepoint(ev[0], u)
        if text:
            b = text.encode()
            lib.ghostty_key_event_set_utf8(ev[0], b, len(b))
        lib.ghostty_key_encoder_setopt_from_terminal(self.enc[0], self.term._t[0])
        out = ffi.new('char[]', 64); n = ffi.new('size_t*')
        r = lib.ghostty_key_encoder_encode(self.enc[0], ev[0], out, 64, n)
        lib.ghostty_key_event_free(ev[0])
        if r == 0 and n[0]: self.write(ffi.buffer(out, n[0])[:])
        return True

    def _cell_at(self, x, y):
        return (min(self.cols - 1, max(0, int(x // self.cell_w))),
                min(self.rows - 1, max(0, int(y // self.cell_h))))

    def _mouse_tracking(self):
        return self.term.get('mouse_tracking', 'bool')

    def _mencode(self, action, button, cx, cy):
        ev = ffi.new('GhosttyMouseEvent*')
        lib.ghostty_mouse_event_new(ffi.NULL, ev)
        lib.ghostty_mouse_event_set_action(ev[0], action)
        if button: lib.ghostty_mouse_event_set_button(ev[0], button)
        pos = ffi.new('GhosttyMousePosition*')
        pos.x, pos.y = (cx + 0.5) * self.cell_w, (cy + 0.5) * self.cell_h
        lib.ghostty_mouse_event_set_position(ev[0], pos[0])
        lib.ghostty_mouse_encoder_setopt_from_terminal(self.menc[0], self.term._t[0])
        sz = ffi.new('GhosttyMouseEncoderSize*'); sz.size = ffi.sizeof('GhosttyMouseEncoderSize')
        sz.screen_width, sz.screen_height = self.cols * self.cell_w, self.rows * self.cell_h
        sz.cell_width, sz.cell_height = self.cell_w, self.cell_h
        lib.ghostty_mouse_encoder_setopt(self.menc[0], lib.GHOSTTY_MOUSE_ENCODER_OPT_SIZE, sz)
        out = ffi.new('char[]', 32); n = ffi.new('size_t*')
        r = lib.ghostty_mouse_encoder_encode(self.menc[0], ev[0], out, 32, n)
        lib.ghostty_mouse_event_free(ev[0])
        if r == 0 and n[0]: self.write(ffi.buffer(out, n[0])[:])

    _BTN = {1: 'GHOSTTY_MOUSE_BUTTON_LEFT', 2: 'GHOSTTY_MOUSE_BUTTON_RIGHT', 3: 'GHOSTTY_MOUSE_BUTTON_MIDDLE'}

    def _press(self, g, n, x, y):
        if self.dead: return
        self.grab_focus()
        self.win.active = self.tile      # aktives terminal fürs splitten/tippen tracken
        self.win.set_title(self.title or 'tilix2')
        btn = g.get_current_button()
        cx, cy = self._cell_at(x, y)
        if btn == 3 and not self._mouse_tracking():
            # rechtsklick-menü nur wenn die app kein mouse-tracking hat (sonst sieht sie den click)
            self.pop.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
            self.pop.popup()
            return
        if self._mouse_tracking():
            self._mencode(lib.GHOSTTY_MOUSE_ACTION_PRESS, getattr(lib, self._BTN.get(btn, ''), 0), cx, cy)
        elif btn == 2:
            self.paste(primary=True)          # middle-click: primary selection einfügen (X11/tilix-verhalten)
        elif btn == 1:
            self.selecting = True
            self.sel = (cx, cy, cx, cy)
            self.queue_draw()

    def _release(self, g, n, x, y):
        if self.dead: return
        btn = g.get_current_button()
        cx, cy = self._cell_at(x, y)
        if self._mouse_tracking():
            self._mencode(lib.GHOSTTY_MOUSE_ACTION_RELEASE, getattr(lib, self._BTN.get(btn, ''), 0), cx, cy)
        elif btn == 1:
            self.selecting = False
            if self.sel and self.sel[:2] == self.sel[2:]: self.sel = None
            self.queue_draw()

    def _motion(self, m, x, y):
        if self.dead: return
        self.mouse_pos = (x, y)
        if self._mouse_tracking() and m.get_current_event_state() & Gdk.ModifierType.BUTTON1_MASK:
            cx, cy = self._cell_at(x, y)
            self._mencode(lib.GHOSTTY_MOUSE_ACTION_MOTION, lib.GHOSTTY_MOUSE_BUTTON_LEFT, cx, cy)
        elif self.selecting:
            cx, cy = self._cell_at(x, y)
            self.sel = (self.sel[0], self.sel[1], cx, cy)
            self.queue_draw()

    def _scroll(self, c, dx, dy):
        if self.dead: return True
        cx, cy = self._cell_at(*self.mouse_pos)
        if self._mouse_tracking():
            btn = lib.GHOSTTY_MOUSE_BUTTON_FOUR if dy < 0 else lib.GHOSTTY_MOUSE_BUTTON_FIVE
            self._mencode(lib.GHOSTTY_MOUSE_ACTION_PRESS, btn, cx, cy)
        else:
            v = ffi.new('ScrollViewportS*')
            v.tag = lib.GHOSTTY_SCROLL_VIEWPORT_DELTA
            v.delta = -3 if dy > 0 else 3
            scroll_viewport(self.term._t[0], v[0])
            self._grid_version += 1   # viewport-verschiebung ändert sichtbare zellen
            self._cell_cache.clear()
            self.sel = None            # selection anchert in viewport-koords -> beim scrollen lösen
            if self.tile:
                sb = self.term.get('scrollback_rows', 'uint64_t')
                # clampen: nicht negativ (anfang des scrollbacks) und nicht über scrollback hinaus
                self.tile._sb_top = max(0, min(sb, self.tile._sb_top + (-3 if dy > 0 else 3)))
            self.queue_draw()
        return True

    # --- clipboard ---
    def _sel_norm(self):
        if not self.sel: return None
        x1, y1, x2, y2 = self.sel
        return (x2, y2, x1, y1) if (x1, y1) > (x2, y2) else (x1, y1, x2, y2)

    def copy(self):
        text = ''
        s = self._sel_norm()
        if s:
            tag = 'viewport' if not self.term.get('viewport_active') else 'active'
            sel = init_sized('GhosttySelection')
            sel.start = self.term.ref(s[0], s[1], tag)[0]
            sel.end = self.term.ref(s[2], s[3], tag)[0]
            text = self.term._format_sel(sel)
        # ohne selection kopiert tilix NICHTS (kein ganzer screen) — nur markierten text
        if not text: return
        d = Gdk.Display.get_default()
        if d:
            cp = Gdk.ContentProvider.new_for_value(text)   # gtk serialisiert in alle text-formate
            d.get_clipboard().set_content(cp)          # ctrl+shift+v
            d.get_primary_clipboard().set_content(cp)  # maus-markierung -> middle-click paste

    def paste(self, primary=False):
        "clipboard (ctrl+shift+v) oder primary selection (middle-click)."
        def done(src, res):
            try: t = src.read_text_finish(res)
            except GLib.Error: return
            if not t or self.dead: return
            # ghostty_paste_encode: ESC & co. -> space (kein ausbruch aus bracketed paste),
            # klammert bei mode 2004, sonst \n -> \r. data wird in-place gesäubert.
            data = t.encode()
            src_buf = ffi.new('char[]', data)
            out = ffi.new('char[]', len(data) + 12)    # + '\e[200~' + '\e[201~'
            n = ffi.new('size_t*')
            if lib.ghostty_paste_encode(src_buf, len(data), self.term.mode(2004), out, len(out), n) == 0:
                self.write(ffi.buffer(out, n[0])[:])
        d = Gdk.Display.get_default()
        if d:
            sel = d.get_primary_clipboard() if primary else d.get_clipboard()
            sel.read_text_async(None, done)

    def _in_sel(self, x, y):
        s = self._sel_norm()
        if not s: return False
        x1, y1, x2, y2 = s
        return y1 <= y <= y2 and (y1 < y < y2 or x1 <= x <= x2)

    # --- rendering ---
    def _cell_info(self, x, y, tag):
        "grid -> (codepoint, width, text, style-dict). gecached pro (x,y,grid-version)."
        v = self._grid_version
        c = self._cell_cache.get((x, y))
        if c is not None and c[0] == v: return c[1]
        t = self.term
        if not hasattr(self, '_refs'):
            self._refs = {'pt': ffi.new('GhosttyPointS*'), 'ref': ffi.new('GhosttyGridRef*'),
                          'cell': ffi.new('GhosttyCell*'), 'cp': ffi.new('uint32_t*'),
                          'wu': ffi.new('uint8_t*'), 'n': ffi.new('size_t*'),
                          'gbuf': ffi.new('uint32_t[]', 16), 'st': ffi.new('GhosttyStyle*')}
            self._refs['st'].size = ffi.sizeof('GhosttyStyle')
            self._refs['ref'].size = ffi.sizeof('GhosttyGridRef')
        r = self._refs
        r['pt'].tag = getattr(lib, f'GHOSTTY_POINT_TAG_{tag.upper()}')
        r['pt'].value.x, r['pt'].value.y = x, y
        if _grid_ref(t._t[0], r['pt'][0], r['ref']) != 0:
            # out-of-bounds o. ä.: statt müll zeichnen leere zelle liefern
            info = (0, 1, ' ', {'bold': False, 'italic': False, 'faint': False, 'blink': False,
                               'inverse': False, 'invisible': False, 'strikethrough': False,
                               'overline': False, 'underline': 0, 'fg': None, 'bg': None,
                               'underline_color': None})
            self._cell_cache[(x, y)] = (v, info)
            return info
        ref = r['ref']
        lib.ghostty_grid_ref_cell(ref, r['cell'])
        lib.ghostty_cell_get(r['cell'][0], lib.GHOSTTY_CELL_DATA_CODEPOINT, r['cp'])
        lib.ghostty_cell_get(r['cell'][0], lib.GHOSTTY_CELL_DATA_WIDE, r['wu'])
        width = 2 if r['wu'][0] == 1 else 1
        lib.ghostty_grid_ref_graphemes(ref, ffi.NULL, 0, r['n'])
        need = r['n'][0]
        if need > 16:
            buf = ffi.new('uint32_t[]', need)
        else:
            buf = r['gbuf']
        lib.ghostty_grid_ref_graphemes(ref, buf, max(need, 1), r['n'])
        text = ''.join(chr(buf[i]) for i in range(r['n'][0])) or (chr(r['cp'][0]) if r['cp'][0] else ' ')
        st = r['st']
        lib.ghostty_grid_ref_style(ref, st)
        def color(c):
            if c.tag == lib.GHOSTTY_STYLE_COLOR_PALETTE: return ('palette', c.value.palette)
            if c.tag == lib.GHOSTTY_STYLE_COLOR_RGB: return ('rgb', (c.value.rgb.r, c.value.rgb.g, c.value.rgb.b))
            return None
        attrs = ('bold', 'italic', 'faint', 'blink', 'inverse', 'invisible', 'strikethrough', 'overline')
        style = {a: bool(getattr(st, a)) for a in attrs}
        style.update(underline=st.underline, fg=color(st.fg_color), bg=color(st.bg_color),
                     underline_color=color(st.underline_color))
        info = (r["cp"][0], width, text, style)
        if len(self._cell_cache) > self.cols * self.rows * 2:
            self._cell_cache.clear()
        self._cell_cache[(x, y)] = (v, info)
        return info

    def _draw(self, w, cr, width, height):
        if self.term._t is None: return   # gekillt
        self._fit(width, height)
        t = self.term
        cols, rows = self.cols, self.rows
        tag = 'active' if t.get('viewport_active') else 'viewport'
        pal = ffi.new('GhosttyColorRgb[]', 256)
        lib.ghostty_terminal_get(t._t[0], lib.GHOSTTY_TERMINAL_DATA_COLOR_PALETTE, pal)
        fgd = ffi.new('GhosttyColorRgb*'); lib.ghostty_terminal_get(t._t[0], lib.GHOSTTY_TERMINAL_DATA_COLOR_FOREGROUND, fgd)
        bgd = ffi.new('GhosttyColorRgb*'); lib.ghostty_terminal_get(t._t[0], lib.GHOSTTY_TERMINAL_DATA_COLOR_BACKGROUND, bgd)
        FG = (fgd.r / 255, fgd.g / 255, fgd.b / 255)
        BG = (bgd.r / 255, bgd.g / 255, bgd.b / 255)

        def col(c, default):
            if c is None: return default
            if c[0] == 'palette':
                p = pal[c[1]]; return (p.r / 255, p.g / 255, p.b / 255)
            r, g, b = c[1]; return (r / 255, g / 255, b / 255)

        cr.set_source_rgb(*BG)
        cr.paint()
        layout = Pango.Layout(self.get_pango_context())
        layout.set_font_description(self.font_desc)
        cw, ch, asc = self.cell_w, self.cell_h, self.ascent
        cursor = (t.get('cursor_x'), t.get('cursor_y')) if t.get('cursor_visible') and tag == 'active' else None
        pal_cache = {}

        for y in range(rows):
            x = 0
            run = None  # [x0, text, width, fg, bg, bold, italic, underline, strike, overline]

            def flush():
                nonlocal run
                if not run: return
                x0, text, wd, fg, bg, bold, ital, ul, strike, ol = run
                run = None
                if bg != BG:
                    cr.set_source_rgb(*bg)
                    cr.rectangle(x0 * cw, y * ch, wd * cw, ch)
                    cr.fill()
                desc = self.font_desc
                if bold or ital:
                    desc = self.font_desc.copy()
                    if bold: desc.set_weight(Pango.Weight.BOLD)
                    if ital: desc.set_style(Pango.Style.ITALIC)
                    layout.set_font_description(desc)
                layout.set_text(text)
                cr.set_source_rgb(*fg)
                if wd == 2:  # wide char: auf genau 2 zellen skalieren
                    lw, _ = layout.get_pixel_size()
                    cr.save(); cr.translate(x0 * cw, y * ch)
                    cr.scale(2 * cw / max(lw, 1), 1)
                    PangoCairo.show_layout(cr, layout)
                    cr.restore()
                else:
                    cr.move_to(x0 * cw, y * ch)
                    PangoCairo.show_layout(cr, layout)
                if ul or strike or ol:
                    cr.set_line_width(1)
                    cr.move_to(x0 * cw, y * ch + (asc + 2 if ul else (asc // 2 if strike else 1)))
                    cr.line_to((x0 + wd) * cw, y * ch + (asc + 2 if ul else (asc // 2 if strike else 1)))
                    cr.stroke()
                layout.set_font_description(self.font_desc)

            while x < cols:
                cp0, width, text, st = self._cell_info(x, y, tag)
                fg = col(st['fg'], FG)
                if st['bold'] and st['fg'] and st['fg'][0] == 'palette' and st['fg'][1] < 8:
                    p = pal[st['fg'][1] + 8]; fg = (p.r / 255, p.g / 255, p.b / 255)
                bg = col(st['bg'], BG)
                if st['inverse'] or self._in_sel(x, y) or cursor == (x, y): fg, bg = bg, fg
                # cursor == (x,y): fg/bg tauschen, dann zeichen in bg auf fg-block malen
                if st['faint']: fg = (fg[0] * 0.6, fg[1] * 0.6, fg[2] * 0.6)   # SGR 2 (dim)
                if st['invisible']: text = ' ' * width
                key = (fg, bg, st['bold'], st['italic'], st['underline'] > 0, st['strikethrough'], st['overline'])
                blank = cp0 == 0 and bg == BG and key[2:] == (False,) * 5 and not self._in_sel(x, y)
                if blank:
                    flush()
                elif run and run[3:] == key and width == 1 and run[0] + run[2] == x:
                    run[1] += text; run[2] += 1
                else:
                    flush()
                    run = [x, text, width, fg, bg, st['bold'], st['italic'],
                           st['underline'] > 0, st['strikethrough'], st['overline']]
                x += width
            flush()

        # block-cursor zeichnen wir beim cell-draw selbst via fg/bg-swap (cursor == (x,y)),
        # inkl. leerer zelle (dann ist bg nach swap = FG != BG -> zelle ist nicht blank)

        if self.dead:
            cr.set_source_rgba(1, 1, 1, 0.7)
            layout.set_text('[prozess beendet — rechtsklick → kachel schließen]')
            cr.move_to(4, 4); PangoCairo.show_layout(cr, layout)

        if self.has_focus():
            cr.set_source_rgb(0.21, 0.52, 0.89)
            cr.set_line_width(2)
            cr.rectangle(1, 1, width - 2, height - 2)
            cr.stroke()


class Win(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.font = DEFAULT_FONT
        self.terms = set()
        self.active = None            # zuletzt angeklickte kachel
        kc = Gtk.EventControllerKey(); kc.connect('key-pressed', self._on_key); self.add_controller(kc)
        # input method: dead keys (^ ´ `), compose, ibus. bekommt jede taste, committed text
        # geht als utf8 mit in den key-encoder (siehe _on_key).
        self.im = Gtk.IMMulticontext()
        self.im.set_client_widget(self)
        self.im.connect('commit', self._im_commit)
        self._im_text = None          # != None nur während filter_keypress läuft
        kc.connect('key-released', lambda c, *a: self.im.filter_keypress(c.get_current_event()))
        self.connect('notify::is-active', lambda w, p: self.im.focus_in() if w.is_active() else self.im.focus_out())
        self.set_title('tilix2')
        self.set_default_size(1000, 700)
        for name in ('split-h', 'split-v', 'close', 'copy', 'paste', 'zoom-in', 'zoom-out',
                     'zoom-reset', 'move-left', 'move-right', 'move-up', 'move-down'):
            a = Gio.SimpleAction.new(name, None)
            a.connect('activate', self._action, name)
            self.add_action(a)
        self.connect('close-request', self._on_close)

        # headerbar mit tiling-actions (wie tilix oben rechts)
        hb = Gtk.HeaderBar()
        self.count_lbl = Gtk.Label(label='1 kachel')
        hb.pack_start(self.count_lbl)
        def hbtn(action, icon, tip):
            b = Gtk.Button(icon_name=icon, focus_on_click=False)   # sonst frisst der button enter/space
            b.set_tooltip_text(tip)
            b.set_action_name('win.' + action)
            return b
        hb.pack_start(hbtn('split-h', 'go-next-symbolic', 'split rechts (ctrl+alt+right)'))
        hb.pack_start(hbtn('split-v', 'go-down-symbolic', 'split unten (ctrl+alt+down)'))
        mv = Gio.Menu()
        for label, act in (('nach links', 'move-left'), ('nach rechts', 'move-right'),
                           ('nach oben', 'move-up'), ('nach unten', 'move-down')):
            mv.append(label, 'win.' + act)
        mb = Gtk.MenuButton(icon_name='object-select-symbolic', menu_model=mv, focus_on_click=False)
        mb.set_tooltip_text('tile verschieben')
        hb.pack_start(mb)
        hb.pack_end(hbtn('close', 'window-close-symbolic', 'kachel schließen (ctrl+shift+w)'))
        hb.pack_end(hbtn('zoom-out', 'zoom-out-symbolic', 'verkleinern (ctrl+shift+-)'))
        hb.pack_end(hbtn('zoom-in', 'zoom-in-symbolic', 'vergrößern (ctrl+shift++)'))
        hb.pack_end(hbtn('paste', 'edit-paste-symbolic', 'einfügen (ctrl+shift+v)'))
        hb.pack_end(hbtn('copy', 'edit-copy-symbolic', 'kopieren (ctrl+shift+c)'))
        self.set_titlebar(hb)

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box.set_hexpand(True); self.box.set_vexpand(True)
        self.set_child(self.box)
        t = Tile(self)
        self.box.append(t)
        self.active = t
        self._refresh_count()

    def _refresh_count(self):
        n = len(self.terms)
        self.count_lbl.set_label(f'{n} kachel' + ('n' if n != 1 else ''))

    def _on_close(self, w):
        for tv in list(self.terms): tv.view.kill()
        return False

    def _focused(self):
        if getattr(self, 'active', None) in self.terms: return self.active
        for tv in self.terms:
            if tv.view.has_focus(): return tv
        return next(iter(self.terms), None)

    def _on_key(self, ctrl, keyval, keycode, state):
        "window-level key input: shortcuts global, rest an die aktive kachel."
        MT = Gdk.ModifierType
        name = Gdk.keyval_name(keyval) or ''
        if state & MT.CONTROL_MASK and state & MT.SHIFT_MASK:
            n = name.lower()
            if n == 'c': self._action(None, None, 'copy'); return True
            if n == 'v': self._action(None, None, 'paste'); return True
            if n == 'w': self._action(None, None, 'close'); return True
            # de-layout: ctrl+<+> ist ctrl(+shift)+less/greater, us-layout: plus/equal; keypad auch
            if n in ('plus', 'equal', 'greater', 'KP_Add'): self._action(None, None, 'zoom-in'); return True
            if n in ('minus', 'less', 'KP_Subtract'): self._action(None, None, 'zoom-out'); return True
            if n in ('0', 'KP_0'): self._action(None, None, 'zoom-reset'); return True
        if state & MT.CONTROL_MASK and state & MT.ALT_MASK:
            if name == 'Right': self._action(None, None, 'split-h'); return True
            if name == 'Down':  self._action(None, None, 'split-v'); return True
        tv = self._focused()
        if not tv: return False
        ev = ctrl.get_current_event() if ctrl else None   # selftest ruft ohne controller
        if ev is not None:
            self._im_text = ''
            filtered = self.im.filter_keypress(ev)
            text, self._im_text = self._im_text, None
            if filtered and not text: return True        # dead key / compose läuft noch
            if filtered: return tv.view.send_key(keyval, state, keycode, text)
        return tv.view.send_key(keyval, state, keycode)

    def _im_commit(self, im, text):
        if self._im_text is not None:
            self._im_text += text             # synchron aus filter_keypress -> _on_key schickt's
        else:
            tv = self._focused()              # asynchron (ibus): direkt als text
            if tv: tv.view.write(text.encode())

    def _action(self, a, param, name):
        tv = self._focused()
        if not tv: return
        if name == 'split-h': self.split(tv, vertical=False)
        elif name == 'split-v': self.split(tv, vertical=True)
        elif name == 'close': self.close(tv)
        elif name == 'copy': tv.view.copy()
        elif name == 'paste': tv.view.paste()
        elif name == 'zoom-in': self.zoom(1)
        elif name == 'zoom-out': self.zoom(-1)
        elif name == 'zoom-reset':
            tv.view.font = DEFAULT_FONT
            tv.view.queue_draw()
        elif name.startswith('move-'): self.move(tv, name[5:])

    # --- tiling-baum aus Gtk.Paned ---
    # WICHTIG (GTK4, empirisch verifiziert): ein dynamisch eingehängtes Paned bekommt erst
    # eine Breite, wenn der swap in EINER idle-phase passiert (alle children + position
    # gesetzt, dann erst append) — macht man remove/append in getrennten main-loop-iterationen,
    # bleibt das paned dauerhaft 0px breit. deshalb alles in einem idle_add.
    def split(self, tv, vertical):
        "selektierte kachel halbieren, neue kachel auf die andere hälfte, neue bekommt fokus."
        if isinstance(tv, TermView): tv = tv.tile
        orient = Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL

        def do_split():
            paned = Gtk.Paned(orientation=orient)   # default-handle ist schmal; wide_handle wäre zu dick
            paned.set_hexpand(True); paned.set_vexpand(True)
            # halbe größe der alten kachel (die ist schon allokiert, das neue paned noch nicht)
            paned.set_position((tv.get_height() if vertical else tv.get_width()) // 2)
            parent = tv.get_parent()
            if isinstance(parent, Gtk.Paned):
                if parent.get_start_child() is tv: parent.set_start_child(None)
                else: parent.set_end_child(None)
            else:
                self.box.remove(tv)
            new = Tile(self)
            paned.set_start_child(tv)
            paned.set_end_child(new)
            if isinstance(parent, Gtk.Paned):
                if parent.get_start_child() is None: parent.set_start_child(paned)
                else: parent.set_end_child(paned)
            else:
                self.box.append(paned)
            self.active = new
            self._refresh_count()
            new.view.grab_focus()
            return False
        GLib.idle_add(do_split)

    def close(self, tv):
        if isinstance(tv, TermView): tv = tv.tile
        parent = tv.get_parent()
        if not isinstance(parent, Gtk.Paned):   # letzte kachel: fenster zu
            tv.view.kill()
            self.terms.discard(tv)
            self.destroy()
            return
        other = parent.get_end_child() if parent.get_start_child() is tv else parent.get_start_child()
        if parent.get_start_child() is other: parent.set_start_child(None)
        else: parent.set_end_child(None)
        grand = parent.get_parent()
        if isinstance(grand, Gtk.Paned):
            if grand.get_start_child() is parent: grand.set_start_child(other)
            else: grand.set_end_child(other)
        else:
            self.box.remove(parent)
            self.box.append(other)
        tv.view.kill()
        self.terms.discard(tv)
        self._refresh_count()
        if isinstance(other, Tile):
            other.view.grab_focus(); self.active = other
        elif isinstance(other, Gtk.Paned): self._focus_first(other)

    def _focus_first(self, w):
        if isinstance(w, Tile): w.view.grab_focus(); self.active = w; return
        self._focus_first(w.get_start_child())

    def move(self, tv, direction):
        if isinstance(tv, TermView): tv = tv.tile
        p = tv.get_parent()
        if not isinstance(p, Gtk.Paned): return
        horiz = p.get_orientation() == Gtk.Orientation.HORIZONTAL
        if (direction in ('left', 'right')) != horiz: return
        c1, c2 = p.get_start_child(), p.get_end_child()
        if (c2 is tv and direction in ('left', 'up')) or (c1 is tv and direction in ('right', 'down')):
            self._swap_children(p, c2, c1)

    def move_toward(self, src, dst):
        "drag&drop: src-kachel mit dst tauschen, wenn sie im selben paned hängen."
        if src is dst: return
        p = src.get_parent()
        if isinstance(p, Gtk.Paned) and dst.get_parent() is p:
            self._swap_children(p, dst, src)
            src.view.grab_focus(); self.active = src

    def _swap_children(self, p, a, b):
        "GTK4: children erst auf None, dann neu setzen (sonst assertion, no-op). in einer idle-phase."
        def do():
            pos = p.get_position()
            p.set_start_child(None); p.set_end_child(None)
            p.set_start_child(a); p.set_end_child(b)
            p.set_position(pos)
            return False
        GLib.idle_add(do)

    def zoom(self, delta):
        "nur die aktive kachel zoomen (tilix-verhalten), nicht alle gleichzeitig."
        tv = self._focused()
        if not tv: return
        v = tv.view
        desc = Pango.FontDescription.from_string(v.font)
        desc.set_size(max(6 * Pango.SCALE, desc.get_size() + delta * Pango.SCALE))
        v.font = desc.to_string()
        v.queue_draw()   # do_size_allocate refittet cols/rows über _fit


class App(Gtk.Application):
    def __init__(self, selftest):
        # NON_UNIQUE: jeder start ist ein eigener prozess (kein d-bus-handoff an eine laufende
        # instanz -> crash reißt nur ein fenster mit, tests laufen neben der echten app)
        super().__init__(application_id='osuv.tilix2', flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.selftest = selftest

    def do_activate(self):
        _register_css()          # display existiert hier auf jeden fall
        win = Win(self)
        win.present()
        if self.selftest: self._selftest(win)

    def _selftest(self, win):
        import traceback
        def run():
            try:
                tv = next(iter(win.terms))
                win.split(tv, vertical=False)   # split läuft jetzt in idle_add — kurz warten
                import time as _t
                end = _t.time() + 2
                while _t.time() < end and not isinstance(tv.get_parent(), Gtk.Paned):
                    while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
                    _t.sleep(0.02)
                p = tv.get_parent()
                assert isinstance(p, Gtk.Paned) and p.get_orientation() == Gtk.Orientation.HORIZONTAL
                # split-position wird nach dem ersten allocate gesetzt
                end = _t.time() + 2
                while _t.time() < end and p.get_position() <= 0:
                    while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
                    _t.sleep(0.02)
                assert p.get_position() > 0, p.get_position()
                tv2 = p.get_end_child()
                assert win.active is tv2 or win._focused() is tv2
                win.split(tv2, vertical=True)
                end = _t.time() + 2
                while _t.time() < end and not isinstance(tv2.get_parent(), Gtk.Paned):
                    while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
                    _t.sleep(0.02)
                assert isinstance(tv2.get_parent(), Gtk.Paned)
                p.set_position(123)   # resize per drag geht über paned, hier nur position setzen
                # key-input: window-level handler -> key-encoder -> pty (pty echo't zurück)
                import select as _sel, time as _time
                atv = win._focused().view
                win._on_key(None, Gdk.KEY_a, 38, 0)
                win._on_key(None, Gdk.KEY_Return, 36, 0)
                got = b''
                end = _time.time() + 2.0
                while _time.time() < end:
                    r, _, _ = _sel.select([atv.master], [], [], 0.3)
                    if r:
                        try: got += os.read(atv.master, 4096)
                        except OSError: break
                assert b'a' in got and b'\r' in got, f'keys nicht im pty: {got!r}'
                # ctrl+d im kitty-mode -> CSI 100;5u (pyrate-style TUIs parsen das).
                # write direkt abfangen (pty-echo verfälscht bytes sonst)
                atv.term.feed(b'\x1b[>1u')
                sent = []
                orig_write = os.write
                os.write = lambda fd, b: (sent.append(bytes(b)), len(b))[1] if fd == atv.master else orig_write(fd, b)
                try:
                    win._on_key(None, Gdk.KEY_d, 40, Gdk.ModifierType.CONTROL_MASK)
                finally:
                    os.write = orig_write
                assert b'\x1b[100;5u' in b''.join(sent), f'ctrl+d kitty falsch: {sent!r}'
                atv.term.feed(b'\x1b[<u')   # kitty mode wieder aus
                # zoom-shortcut de-layout: ctrl+shift+less == ctrl+greater -> zoom-in
                f0 = atv.font
                win._on_key(None, Gdk.KEY_greater, 0, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
                assert atv.font != f0, 'zoom-shortcut ctrl+<+> ging nicht'
                # backend: kitty keyboard -> ctrl+enter eindeutig
                v = tv.view
                v.term.feed(b'\x1b[>1u')
                ev = ffi.new('GhosttyKeyEvent*'); lib.ghostty_key_event_new(ffi.NULL, ev)
                lib.ghostty_key_event_set_action(ev[0], lib.GHOSTTY_KEY_ACTION_PRESS)
                lib.ghostty_key_event_set_key(ev[0], lib.GHOSTTY_KEY_ENTER)
                lib.ghostty_key_event_set_mods(ev[0], 2)  # ctrl
                lib.ghostty_key_encoder_setopt_from_terminal(v.enc[0], v.term._t[0])
                out = ffi.new('char[]', 64); n = ffi.new('size_t*')
                assert lib.ghostty_key_encoder_encode(v.enc[0], ev[0], out, 64, n) == 0
                got = ffi.buffer(out, n[0])[:]
                assert got == b'\x1b[13;5u', got
                # feed/render/resize
                v.term.feed(b'hello tilix2\r\n')
                assert 'hello tilix2' in v.term.text()
                v.term.resize(100, 30, 8, 16)
                assert v.term.size == (100, 30)
                # close entfernt kachel aus dem baum
                win.close(tv2)
                assert tv2 not in win.terms
                # pty-roundtrip: echo landet im terminal
                v.write(b'echo MARKER_123\r')
                def stage2():
                    try:
                        import time as _t
                        assert 'MARKER_123' in tv.view.term.contents(), tv.view.term.contents()[:200]
                        # mouse-encoder + scroll + selection-copy dürfen nicht crashen
                        tv.view.term.feed(b'\x1b[?1002;1006h')
                        tv.view._mencode(lib.GHOSTTY_MOUSE_ACTION_PRESS, lib.GHOSTTY_MOUSE_BUTTON_LEFT, 3, 2)
                        tv.view._scroll(None, 0.0, 1.0)
                        tv.view.sel = (0, 0, 5, 0)
                        tv.view.copy()
                        # move per menü: noch ein split, damit tv in einem paned mit nachbar sitzt
                        win.split(tv, vertical=False)
                        end = _t.time() + 2
                        while _t.time() < end and not isinstance(tv.get_parent(), Gtk.Paned):
                            while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
                            _t.sleep(0.02)
                        p2 = tv.get_parent()
                        assert isinstance(p2, Gtk.Paned), f'tv parent ist {type(p2).__name__}'
                        other_tile = p2.get_end_child() if p2.get_start_child() is tv else p2.get_start_child()
                        win.active = tv
                        win.move(tv, 'right' if p2.get_orientation() == Gtk.Orientation.HORIZONTAL else 'down')
                        end = _t.time() + 2
                        while _t.time() < end and p2.get_start_child() is tv:
                            while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
                            _t.sleep(0.02)
                        assert p2.get_start_child() is not tv, 'move-menü swapped nicht'
                        # drag&drop-pfad: move_toward tauscht ebenfalls (nur wenn selbes paned)
                        sib = p2.get_end_child() if p2.get_start_child() is tv else p2.get_start_child()
                        if sib is not None:
                            win.move_toward(tv, sib)
                            end = _t.time() + 2
                            while _t.time() < end and p2.get_start_child() is tv:
                                while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
                                _t.sleep(0.02)
                            assert p2.get_start_child() is not tv, 'move_toward swapped nicht'
                        # middle-click pasted aus primary selection (mouse-tracking war zufällig an -> reset)
                        tv.view.term.feed(b'\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l')
                        import unittest.mock as _mock2
                        with _mock2.patch.object(Gdk.Display.get_default().get_primary_clipboard(), 'read_text_async') as m:
                            g = _mock2.Mock()
                            g.get_current_button.return_value = 2
                            tv.view._press(g, 1, 10.0, 10.0)
                            assert m.called, 'middle-click liest nicht primary selection'
                        # pwd-header + scrollbar existieren; _tick darf mit scrollback nicht crashen
                        assert tv.pwd_lbl is not None and tv.sbar is not None
                        # css-provider registriert -> scrollbar ist schmal (5px statt 24px theme-default)
                        _register_css()
                        GLib.timeout_add(0, lambda: None)
                        # faint wird gerendert: style direkt am grid prüfen (unabhängig von cursor-pos)
                        v0 = tv.view
                        v0.term.feed(b'\r\n\x1b[2mdim-text\x1b[0m\r\n')
                        v0._grid_version += 1; v0._cell_cache.clear()
                        found = False
                        for yy in range(v0.rows):
                            for xx in range(v0.cols):
                                i2 = v0._cell_info(xx, yy, 'active')
                                if i2[2] == 'd' and i2[3]['faint']:
                                    found = True; break
                            if found: break
                        assert found, 'faint-style nicht im grid gefunden'
                        tv.view.term.feed(b''.join(b'row%d\r\n' % i for i in range(100)))
                        assert tv._tick() is True, 'Tile._tick crasht mit scrollback'
                        assert tv.sbar.get_visible(), 'scrollbar erst ab scrollback sichtbar'
                        assert tv.adj.get_upper() > tv.view.rows
                        # scrollbar-drag bewegt den viewport (ROW-tag)
                        tv.adj.set_value(5)
                        assert not tv.view.term.get('viewport_active'), 'sbar-drag hat viewport nicht bewegt'
                        # copy ohne auswahl kopiert nichts (tilix-verhalten)
                        tv.view.sel = None
                        import unittest.mock as _mock
                        with _mock.patch.object(Gdk.Display.get_default().get_clipboard(), 'set_content') as m:
                            tv.view.copy()
                            assert not m.called, 'copy ohne selection darf nicht in die zwischenablage'
                        # hexrgb fallback
                        assert hexrgb('red', (1, 2, 3)) == (1, 2, 3)
                        assert hexrgb('#aabbcc', (1, 2, 3)) == (170, 187, 204)
                        # unshifted codepoint: shift+a -> 'a' (kitty)
                        win.active = tv
                        v2 = tv.view
                        v2.term.feed(b'\x1b[>1u')
                        sent2 = []
                        ow = os.write
                        os.write = lambda fd, b: (sent2.append(bytes(b)), len(b))[1] if fd == v2.master else ow(fd, b)
                        try:
                            win._on_key(None, Gdk.KEY_A, 38, Gdk.ModifierType.SHIFT_MASK)
                        finally:
                            os.write = ow
                        # kitty flags=1 (disambiguate): text-tasten mit nur shift bleiben text -> 'A'
                        assert b''.join(sent2) == b'A', f'shift+a im kitty-mode falsch: {sent2!r}'
                        v2.term.feed(b'\x1b[<u')
                        # ctrl+alt+right shortcut geht window-level ans aktive terminal
                        # (split läuft in idle_add -> kurz pumpen bis terms wächst)
                        n_before = len(win.terms)
                        win._on_key(None, Gdk.KEY_Right, 114, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)
                        end = _t.time() + 2
                        while _t.time() < end and len(win.terms) == n_before:
                            while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
                            _t.sleep(0.02)
                        assert len(win.terms) == n_before + 1
                        # zoom nur aktive kachel
                        win.active = tv
                        fonts_before = {id(t): t.view.font for t in win.terms}
                        win.zoom(1)
                        assert tv.view.font != fonts_before[id(tv)], 'aktive kachel nicht gezoomt'
                        for t in win.terms:
                            if t is not tv:
                                assert t.view.font == fonts_before[id(t)], 'zoom hat andere kachel betroffen'
                        # prozess-ende (ctrl+d/exit/kill) schließt die kachel automatisch
                        tv.view.proc.kill()   # -> HUP am pty-master -> _io -> win.close
                        def stage3():
                            try:
                                assert tv not in win.terms, 'tote kachel wurde nicht geschlossen'
                                print('SELFTEST OK', flush=True)
                                os._exit(0)
                            except Exception:
                                traceback.print_exc()
                                print('SELFTEST FAIL', flush=True)
                                os._exit(1)
                        GLib.timeout_add(1200, stage3)
                        return
                    except Exception:
                        traceback.print_exc()
                        print('SELFTEST FAIL', flush=True)
                        os._exit(1)
                GLib.timeout_add(800, stage2)
            except Exception:
                traceback.print_exc()
                print('SELFTEST FAIL', flush=True)
                os._exit(1)
        GLib.timeout_add(400, run)


SLIM_CSS = b"""
/* GTK4-Adwaita setzt scrollbar.vertical > range > trough margin 8px + slider border 8px
   (-> 24px breit). ueberschreiben wir komplett: 5px, trough/slider ohne margin/border. */
scrollbar.vertical.tilix2-slim { min-width: 5px; min-height: 5px; }
scrollbar.vertical.tilix2-slim > range > trough {
  margin: 0; padding: 0; min-width: 5px; min-height: 5px;
  background: transparent; border: none;
}
scrollbar.vertical.tilix2-slim > range > trough > slider {
  min-width: 5px; min-height: 20px; margin: 0; padding: 0;
  border: none; border-radius: 2px;
  background-color: rgba(160,160,160,0.45);
}
scrollbar.vertical.tilix2-slim > range > trough > slider:hover {
  background-color: rgba(180,180,180,0.7);
}
paned separator { min-width: 1px; min-height: 1px; background: rgba(128,128,128,0.35); }
paned separator:hover { background: rgba(90,150,220,0.8); }
"""

def _register_css():
    "css-provider fürs display registrieren (idempotent genug für main+activate)."
    d = Gdk.Display.get_default()
    if d is None: return
    css = Gtk.CssProvider()
    css.load_from_data(SLIM_CSS)
    Gtk.StyleContext.add_provider_for_display(d, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

def main():
    Gtk.init()   # öffnet den default-display — sonst ist Gdk.Display.get_default() noch None
    _register_css()
    selftest = '--selftest' in sys.argv
    sys.argv = [a for a in sys.argv if a != '--selftest']
    sys.exit(App(selftest).run(sys.argv))


if __name__ == '__main__':
    main()
