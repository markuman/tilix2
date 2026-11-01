#!/usr/bin/env python3
# tests/checks.py — tests for tilix2.
#
#   python tests/checks.py              everything (gui checks need DISPLAY, e.g. xvfb-run -a)
#   python tests/checks.py encoder      only headless key encoder/vt checks (no DISPLAY)
#   python tests/checks.py <name>       one gui check: layout | tabs | dnd | input | clipboard | render | images |
#                                       robust | scrollbar | perf
#   (gui_robust prints a traceback on purpose: that's the tile with the broken feed)
#
# exit 0 = ok, every check prints "<name> OK". the gui checks run with TILIX2_SHELL=cat
# (no prompt -> deterministic screen content).
import os, sys, subprocess, time, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---------- headless: ghostty vt + encoder ----------

def encoder_checks():
    from pyghostty import Terminal, lib, ffi

    def enc(term, gk, mods=0, utf8=None, consumed=0, cp=None):
        e = ffi.new('GhosttyKeyEncoder*'); lib.ghostty_key_encoder_new(ffi.NULL, e)
        ev = ffi.new('GhosttyKeyEvent*'); lib.ghostty_key_event_new(ffi.NULL, ev)
        lib.ghostty_key_event_set_action(ev[0], lib.GHOSTTY_KEY_ACTION_PRESS)
        lib.ghostty_key_event_set_key(ev[0], getattr(lib, 'GHOSTTY_KEY_' + gk))
        lib.ghostty_key_event_set_mods(ev[0], mods)
        lib.ghostty_key_event_set_consumed_mods(ev[0], consumed)
        if utf8:
            b = utf8.encode(); lib.ghostty_key_event_set_utf8(ev[0], b, len(b))
        if cp is not None: lib.ghostty_key_event_set_unshifted_codepoint(ev[0], cp)
        lib.ghostty_key_encoder_setopt_from_terminal(e[0], term._t[0])
        out = ffi.new('char[]', 64); n = ffi.new('size_t*')
        assert lib.ghostty_key_encoder_encode(e[0], ev[0], out, 64, n) == 0
        lib.ghostty_key_event_free(ev[0]); lib.ghostty_key_encoder_free(e[0])
        return ffi.buffer(out, n[0])[:]

    SHIFT, CTRL = 1, 2
    t = Terminal(cols=80, rows=24)
    # this is how tilix2 calls the encoder: utf8 = text of the key, consumed = shift if text
    assert enc(t, 'D', CTRL, 'd', cp=ord('d')) == b'\x04', 'legacy ctrl+d'
    assert enc(t, 'ENTER') == b'\r', 'legacy enter'
    assert enc(t, 'ENTER', CTRL) == b'\x1b[27;5;13~', 'legacy ctrl+enter'
    assert enc(t, 'A', 0, 'a', cp=ord('a')) == b'a'
    assert enc(t, 'UNIDENTIFIED', 0, 'ä', cp=ord('ä')) == 'ä'.encode()
    t.feed(b'\x1b[>1u')   # kitty keyboard, flags=1 (disambiguate)
    assert enc(t, 'ENTER', CTRL) == b'\x1b[13;5u', 'kitty ctrl+enter'
    assert enc(t, 'D', CTRL, 'd', cp=ord('d')) == b'\x1b[100;5u', 'kitty ctrl+d'
    assert enc(t, 'A', SHIFT, 'A', SHIFT, ord('a')) == b'A', 'kitty shift+a stays text'
    assert enc(t, 'A', SHIFT, 'A', 0, ord('a')) == b'\x1b[97;2u', 'without consumed shift: CSI u'
    # utf8 / wide chars / long grapheme clusters
    t2 = Terminal(cols=80, rows=24)
    t2.feed('ab中文cd äöü')
    assert t2.text() == 'ab中文cd äöü'
    t2.resize(120, 30, 8, 16)
    assert t2.size == (120, 30)

# ---------- gui (DISPLAY needed; every check as its own process) ----------

def pump_until(cond, timeout=2.0):
    "run the main loop until cond() is true (or timeout). returns cond()."
    from gi.repository import GLib
    ctx = GLib.MainContext.default()
    end = time.time() + timeout
    while time.time() < end:
        while ctx.pending(): ctx.iteration(False)
        if cond(): return True
        time.sleep(0.01)
    return cond()

def capture(view):
    "catch everything the view would write into the pty. `del view.write` restores it."
    sent = []
    view.write = lambda b: sent.append(bytes(b))
    return sent

def key(win, keyval, state=0, keycode=0):
    return win._on_key(None, keyval, keycode, state)

def kc(keyval):
    "keycode of the key that produces keyval (layout independent: '+' is a different key on de/us)"
    from gi.repository import Gdk
    ok, keys = Gdk.Display.get_default().map_keyval(keyval)
    assert ok and keys, f'keyval {keyval:#x} not in layout'
    return keys[0].keycode

def surface(view):
    import cairo
    w, h = view.cols * view.cell_w, view.rows * view.cell_h
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    return surf, cairo.Context(surf), w, h

def pixel(surf, x, y):
    "(r, g, b) as 0..255 — cairo ARGB32 little-endian: B G R A"
    d = surf.get_data(); i = y * surf.get_stride() + x * 4
    return (d[i + 2], d[i + 1], d[i])

def gui_layout(win):
    from gi.repository import Gtk, Gdk
    t1 = win.tiles()[0]
    assert t1.view.get_width() > 500 and t1.view.get_height() > 400, 'tile too small'
    assert win.get_titlebar() is not None, 'no headerbar'
    assert t1.view.cols > 40 and t1.view.rows > 10, f'grid too small: {t1.view.cols}x{t1.view.rows}'
    win.split(t1, vertical=False)
    assert pump_until(lambda: len(win.tiles()) == 2), 'split did not work'
    p = t1.get_parent()
    assert isinstance(p, Gtk.Paned) and p.get_orientation() == Gtk.Orientation.HORIZONTAL
    t2 = p.get_end_child()
    assert win.active is t2, 'new tile not active'
    assert pump_until(lambda: abs(t1.view.get_width() - t2.view.get_width()) < 20), \
        f'split not centered: {t1.view.get_width()} vs {t2.view.get_width()}'
    assert t2.view.cwd() == t1.view.cwd(), 'split does not inherit cwd'
    win.split(t2, vertical=True)
    assert pump_until(lambda: len(win.tiles()) == 3)
    assert t2.get_parent().get_orientation() == Gtk.Orientation.VERTICAL
    key(win, Gdk.KEY_Right, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)
    assert pump_until(lambda: len(win.tiles()) == 4), 'ctrl+alt+right does not split'
    sizes = lambda: [(t.view.get_width(), t.view.get_height()) for t in win.tiles()]
    assert pump_until(lambda: all(w > 50 and h > 50 for w, h in sizes())), f'tile with 0px: {sizes()}'
    # closing does not block and the shell gets SIGHUP
    victim = win.active
    t0 = time.time(); win.close_tile(victim); dt = time.time() - t0
    assert dt < 0.2, f'close blocks for {dt:.2f}s'
    assert victim not in win.tiles() and len(win.tiles()) == 3
    assert pump_until(lambda: victim.view.proc.poll() is not None), 'shell still alive after close'
    # move via menu (swaps within the same paned)
    p = t2.get_parent()
    first = p.get_start_child()
    win.move(t2, 'down' if p.get_start_child() is t2 else 'up')
    assert pump_until(lambda: p.get_start_child() is not first), 'move did not swap'
    # drag&drop: edge under the cursor, the tile lands there
    assert t1.zone_at(1, t1.get_height() / 2) == 'left' and t1.zone_at(t1.get_width() / 2, 1) == 'top'
    win.move_tile(t2, t1, 'bottom')
    q = t2.get_parent()
    assert q.get_orientation() == Gtk.Orientation.VERTICAL and q.get_start_child() is t1 \
        and q.get_end_child() is t2, 'move_tile bottom wrong'
    assert len(win.tiles()) == 3 and win.active is t2
    assert pump_until(lambda: abs(t1.get_height() - t2.get_height()) < 20), \
        f'move_tile not centered: {t1.get_height()} vs {t2.get_height()}'
    win.move_tile(t2, t1, 'left')
    assert t2.get_parent().get_start_child() is t2 and t2.get_parent().get_end_child() is t1, 'move_tile left wrong'
    assert pump_until(lambda: abs(t1.get_width() - t2.get_width()) < 20), 'move_tile left not centered'
    # process exit closes the tile, the neighbor gets the space
    for t in win.tiles():
        if t is not t1: win.close_tile(t)
    win.split(t1, vertical=False)
    assert pump_until(lambda: len(win.tiles()) == 2)
    other = win.tiles()[1]
    t1.view.proc.kill()
    assert pump_until(lambda: t1 not in win.tiles()), 'dead tile not closed'
    assert pump_until(lambda: other.view.get_width() > win.get_width() - 100), 'space not freed'

def gui_tabs(win):
    from gi.repository import Gdk
    MT = Gdk.ModifierType
    CS = MT.CONTROL_MASK | MT.SHIFT_MASK
    t1 = win.active
    assert not win.tabbar.get_visible(), 'tab bar visible with 1 tab'
    key(win, Gdk.KEY_T, CS, kc(Gdk.KEY_t))
    assert len(win.tabs) == 2 and win.tabbar.get_visible(), 'ctrl+shift+t does not create a tab'
    t2 = win.active
    assert t2 is not t1 and win.tiles() == [t2], 'new tab not active / tiles() not per tab'
    assert pump_until(lambda: t2.view.get_width() > 100), 'new tab gets no size'
    assert t2.view.cwd() == t1.view.cwd(), 'tab does not inherit cwd'
    key(win, Gdk.KEY_Page_Up, MT.CONTROL_MASK)
    assert win.active is t1 and win.tiles() == [t1], 'ctrl+pgup does not switch'
    key(win, Gdk.KEY_Page_Down, MT.CONTROL_MASK)
    assert win.active is t2, 'ctrl+pgdn does not switch'
    # split in the second tab, then end the shell in the background tab: focus stays
    win.split(t2, vertical=False)
    assert pump_until(lambda: len(win.tiles()) == 2)
    t3 = win.active
    key(win, Gdk.KEY_Page_Up, MT.CONTROL_MASK)
    assert win.active is t1
    t3.view.proc.kill()
    assert pump_until(lambda: t3.get_root() is None), 'dead tile in background tab stays'
    assert win.active is t1 and t2.tab.active is t2, 'background close steals the focus'
    assert win.get_focus() is t1.view, f'focus lost after background close: {win.get_focus()}'
    # close the last tile in the active tab -> tab closes, neighbor tab active
    win.close_tile(t1)
    assert win.tabs == [t2.tab] and win.active is t2 and not win.tabbar.get_visible(), 'tab close wrong'
    assert pump_until(lambda: t1.view.proc.poll() is not None), 'shell still alive after tab close'

def gui_dnd(win):
    "real mouse drag via XTest (x11/xvfb only): drag the bottom header onto the right half of the top tile."
    import ctypes, threading
    from gi.repository import Gtk, Graphene
    X, T = ctypes.CDLL('libX11.so.6'), ctypes.CDLL('libXtst.so.6')
    X.XOpenDisplay.restype = ctypes.c_void_p
    dpy = ctypes.c_void_p(X.XOpenDisplay(None))
    assert dpy, 'no X display (dnd check needs x11, e.g. xvfb-run)'
    win.set_default_size(600, 440)            # xvfb-run: screen is only 640x480
    assert pump_until(lambda: win.get_width() <= 640 and win.get_height() <= 480), f'window: {win.get_width()}x{win.get_height()}'
    t1 = win.active
    win.split(t1, vertical=True)
    assert pump_until(lambda: len(win.tiles()) == 2 and win.active.get_height() > 100)
    t2 = win.active
    # screen position of the window: ask the x server (a wm places it anywhere) + csd shadow offset
    import gi
    gi.require_version('GdkX11', '4.0')
    from gi.repository import GdkX11  # noqa: F401  (registers the x11 surface type with get_xid)
    import warnings
    with warnings.catch_warnings():            # get_xid is deprecated (gtk 4.18+), still the simplest way here
        warnings.simplefilter('ignore', DeprecationWarning)
        xid = win.get_surface().get_xid()
    rx, ry, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
    X.XTranslateCoordinates(dpy, ctypes.c_ulong(xid), ctypes.c_ulong(X.XDefaultRootWindow(dpy)),
                            0, 0, ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(child))
    sx, sy = win.get_surface_transform()
    ox, oy = rx.value + sx, ry.value + sy
    def at(w, x, y):
        ok, p = w.compute_point(win, Graphene.Point().init(x, y))
        return int(p.x + ox), int(p.y + oy)
    # grab the header at its bottom: at the top edge the click still belongs to the paned handle (resize)
    head = t2.get_first_child()
    (sx, sy), (tx, ty) = at(head, 60, head.get_height() - 3), at(t1, t1.get_width() - 30, t1.get_height() / 2)
    seen = []
    def drive():
        # own thread: the dnd grab wants a freely running main loop, xtest calls don't block
        def mv(x, y): T.XTestFakeMotionEvent(dpy, -1, x, y, 0); X.XFlush(dpy); time.sleep(0.03)
        def btn(down): T.XTestFakeButtonEvent(dpy, 1, down, 0); X.XFlush(dpy); time.sleep(0.1)
        mv(sx, sy); btn(True)
        for i in range(1, 21): mv(sx + (tx - sx) * i // 20, sy + (ty - sy) * i // 20)
        seen.append(t1.drop_zone)
        btn(False)
    th = threading.Thread(target=drive, daemon=True); th.start()
    assert pump_until(lambda: not th.is_alive(), 10)
    assert seen == ['right'], f'drop preview: {seen}'
    assert pump_until(lambda: t2.get_parent().get_orientation() == Gtk.Orientation.HORIZONTAL), 'drop moved nothing'
    p = t2.get_parent()
    assert p.get_start_child() is t1 and p.get_end_child() is t2 and t1.drop_zone is None, 'drop wrong'

def gui_input(win):
    import tilix2
    from gi.repository import Gdk
    from pyghostty import lib
    MT = Gdk.ModifierType
    v = win.active.view
    sent = capture(v)
    key(win, Gdk.KEY_a, 0, kc(Gdk.KEY_a)); key(win, Gdk.KEY_Return, 0, kc(Gdk.KEY_Return))
    assert sent == [b'a', b'\r'], sent
    v.term.feed(b'\x1b[>1u')
    sent.clear(); key(win, Gdk.KEY_d, MT.CONTROL_MASK, kc(Gdk.KEY_d))
    assert sent == [b'\x1b[100;5u'], f'kitty ctrl+d: {sent}'
    sent.clear(); key(win, Gdk.KEY_A, MT.SHIFT_MASK, kc(Gdk.KEY_a))
    assert sent == [b'A'], f'kitty shift+a: {sent}'
    sent.clear(); key(win, Gdk.KEY_Return, MT.CONTROL_MASK, kc(Gdk.KEY_Return))
    assert sent == [b'\x1b[13;5u'], f'kitty ctrl+enter: {sent}'
    v.term.feed(b'\x1b[<u\x1b[>8u')     # flag 8: all keys as CSI u -> unshifted codepoint visible
    sent.clear(); key(win, Gdk.KEY_exclam, MT.SHIFT_MASK, kc(Gdk.KEY_1))
    assert sent and sent[0].startswith(b'\x1b[49;'), f'shift+1 unshifted != "1": {sent}'
    v.term.feed(b'\x1b[<u')
    # IM commit outside of filter_keypress (ibus, async) goes out directly as text
    sent.clear(); win._im_commit(None, 'é')
    assert sent == ['é'.encode()], sent
    # typing brings the viewport back to the bottom
    v.term.feed(b''.join(b'line %d\r\n' % i for i in range(200)))
    v.scroll(lib.GHOSTTY_SCROLL_VIEWPORT_DELTA, -10)
    assert not v.term.get('viewport_active')
    key(win, Gdk.KEY_a, 0, kc(Gdk.KEY_a))
    assert v.term.get('viewport_active'), 'typing does not scroll to the bottom'
    # mouse: SGR encoding, middle is button 1 (not 2)
    v.term.feed(b'\x1b[?1000h\x1b[?1006h')
    sent.clear()
    v._mencode(lib.GHOSTTY_MOUSE_ACTION_PRESS, tilix2.MOUSE_BTN[1], 10.0, 10.0, 0)
    v._mencode(lib.GHOSTTY_MOUSE_ACTION_PRESS, tilix2.MOUSE_BTN[2], 10.0, 10.0, 0)
    assert sent[0].startswith(b'\x1b[<0;') and sent[1].startswith(b'\x1b[<1;'), f'mouse: {sent}'
    v.term.feed(b'\x1b[?1000l\x1b[?1006l')
    del v.write
    # pty roundtrip through `cat`
    v.write(b'MARKER_123\r')
    assert pump_until(lambda: v.term.contents().count('MARKER_123') >= 2), v.term.contents()[-200:]
    # zoom only the active tile; matches by physical key -> keycode from the current layout
    win.split(win.active, vertical=False)
    assert pump_until(lambda: len(win.tiles()) == 2)
    a, other = win.active, [t for t in win.tiles() if t is not win.active][0]
    f0, fo = a.view.font, other.view.font
    CS = MT.CONTROL_MASK | MT.SHIFT_MASK
    key(win, Gdk.KEY_plus, CS, kc(Gdk.KEY_plus))
    assert a.view.font != f0 and other.view.font == fo, 'zoom-in wrong'
    key(win, Gdk.KEY_underscore, CS, kc(Gdk.KEY_minus))
    assert a.view.font == f0, 'zoom-out wrong'
    key(win, Gdk.KEY_plus, CS, kc(Gdk.KEY_plus))
    key(win, Gdk.KEY_parenright, CS, kc(Gdk.KEY_0))   # shift+0 is ')' on us, '=' on de
    assert a.view.font == tilix2.DEFAULT_FONT, 'zoom-reset wrong'

def gui_clipboard(win):
    from gi.repository import Gdk
    v = win.active.view
    d = Gdk.Display.get_default()
    def read(cb):
        got = []
        cb.read_text_async(None, lambda s, r: got.append(s.read_text_finish(r)))
        assert pump_until(lambda: got), 'reading the clipboard hangs'
        return got[0]
    v.term.feed(b'hello world\r\nsecond line\r\n')
    v.select((0, 0), (4, 0))
    assert v.selected_text() == 'hello'
    v.select((4, 0), (0, 0))
    assert v.selected_text() == 'hello', 'selecting backwards'
    v.copy()
    assert read(d.get_clipboard()) == 'hello' and read(d.get_primary_clipboard()) == 'hello'
    # selection is rendered inverted
    surf, cr, w, h = surface(v)
    v._draw(v, cr, w, h)
    bgs, runs, _ = v._rowcache[0]
    assert bgs and bgs[0][0] == 0 and bgs[0][1] == 5, f'selection not highlighted: {bgs}'
    # mouse selection -> primary
    class G:      # fake gesture/controller: like gtk, button 1 held shows up in the state
        def __init__(s, b): s.b = b
        def get_current_button(s): return s.b
        def get_current_event_state(s): return Gdk.ModifierType.BUTTON1_MASK if s.b == 1 else 0
    d.get_primary_clipboard().set_content(Gdk.ContentProvider.new_for_value('x'))
    v._press(G(1), 1, 1.0, v.cell_h * 1.5)                       # row 1, column 0
    v._motion(G(1), v.cell_w * 5.5, v.cell_h * 1.5)              # to column 5
    v._release(G(1), 1, v.cell_w * 5.5, v.cell_h * 1.5)
    assert read(d.get_primary_clipboard()) == 'second', 'selecting does not set primary'
    # selection survives scrolling (ghostty tracks it)
    v.term.feed(b''.join(b'row %d\r\n' % i for i in range(100)))
    assert v.selected_text() == 'second', 'selection lost while scrolling'
    # click without drag clears the selection, copy without selection leaves the clipboard alone
    v._press(G(1), 1, 1.0, 1.0); v._release(G(1), 1, 1.0, 1.0)
    assert v.selected_text() == ''
    d.get_clipboard().set_content(Gdk.ContentProvider.new_for_value('unchanged'))
    v.copy()
    assert read(d.get_clipboard()) == 'unchanged'
    # paste: ESC gets defused, bracketed paste brackets
    v.term.feed(b'\x1b[?2004h')
    d.get_clipboard().set_content(Gdk.ContentProvider.new_for_value('echo hi\x1b[201~; evil\n'))
    sent = capture(v)
    v.paste()
    assert pump_until(lambda: sent), 'paste writes nothing'
    assert sent == [b'\x1b[200~echo hi [201~; evil\n\x1b[201~'], f'paste not defused: {sent}'
    # middle-click pastes primary
    sent.clear()
    v._press(G(2), 1, 10.0, 10.0)
    assert pump_until(lambda: sent) and b'second' in sent[0], f'middle-click: {sent}'
    del v.write

def gui_render(win):
    v = win.active.view
    v.grab_focus()
    v.term.feed(b'\x1b[31mRED\x1b[0m plain \xe4\xb8\xad\xe6\x96\x87 \x1b[1mbold\x1b[0m \x1b[2mdim\x1b[0m '
                b'\x1b[4munder\x1b[0m \x1b[7minv\x1b[0m\r\n')
    surf, cr, w, h = surface(v)
    v._draw(v, cr, w, h)
    surf.flush()
    c = v.colors
    FG = (c.foreground.r, c.foreground.g, c.foreground.b)
    BG = (c.background.r, c.background.g, c.background.b)
    bgs, runs, chars = v._rowcache[0]
    texts = {r[3].get_text(): r for r in runs}
    assert 'RED' in texts and texts['RED'][2][0] > texts['RED'][2][1], f'RED not red: {texts.get("RED")}'
    assert any(r[1] == 2 and r[5] > 0 for r in runs), 'wide char not scaled to 2 cells'
    dim = [r for r in runs if 'dim' in r[3].get_text()][0]
    assert sum(dim[2]) < sum(c / 255 for c in FG), 'faint not dimmed'
    assert any(r[4][0] for r in runs if 'under' in r[3].get_text()), 'underline missing'
    inv_x = 30      # 'RED plain 中文 bold dim under inv': 中/文 are 2 cells wide each
    assert chars[inv_x] == 'i', chars
    assert any(b[0] <= inv_x < b[0] + b[1] for b in bgs), 'inverse has no background'
    red = sum(1 for y in range(v.cell_h) for x in range(3 * v.cell_w)
              if (p := pixel(surf, x, y))[0] > 150 and p[0] > p[1] + 50)
    assert red > 10, f'no red in the image: {red}'
    # cursor (row 1, column 0): block with focus, hollow without
    cx, cy = v.cell_w // 2, v.cell_h + v.cell_h // 2
    want = FG if v.has_focus() else BG
    assert pixel(surf, cx, cy) == want, f'cursor: {pixel(surf, cx, cy)} != {want} (focus={v.has_focus()})'
    # dirty tracking: one char -> exactly one row gets rebuilt
    n = []
    orig = v._build_row
    v._build_row = lambda *a: (n.append(1), orig(*a))[1]
    v._draw(v, cr, w, h)
    assert not n, f'without change {len(n)} rows rebuilt'
    v.term.feed(b'x')
    v._draw(v, cr, w, h)
    assert len(n) == 1, f'one char -> {len(n)} rows rebuilt'

def gui_images(win):
    import base64, fcntl, struct, termios
    from gi.repository import Gdk, GLib
    from pyghostty import lib
    v = win.active.view
    cw, ch = v.cell_w, v.cell_h
    # pixel size: winsize (icat checks this first) and CSI 16 t
    rows, cols, xp, yp = struct.unpack('HHHH', fcntl.ioctl(v.master, termios.TIOCGWINSZ, bytes(8)))
    assert (rows, cols) == (v.rows, v.cols) and (xp, yp) == (cols * cw, rows * ch) and xp, f'winsize: {xp}x{yp}'
    replies = []
    v.term.on_reply(replies.append)
    v.term.feed(b'\x1b[16t')
    v.term.on_reply(v.write)
    assert replies == [b'\x1b[6;%d;%dt' % (ch, cw)], f'CSI 16 t: {replies}'
    def apc(ctrl, data=b''):        # q=2: no OK reply (it would go through `cat` and come back in)
        v.term.feed(b'\x1b_G' + ctrl + b',q=2;' + base64.b64encode(data) + b'\x1b\\')
    # 1x1 red rgba pixel at 0,0 stretched to 2x2 cells, 1x1 blue png at row 4 col 9 on 3x1 cells
    apc(b'a=T,f=32,s=1,v=1,i=1,c=2,r=2', bytes([255, 0, 0, 255]))
    png = Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(bytes([0, 0, 255, 255])), 4) \
        .save_to_png_bytes().get_data()
    v.term.feed(b'\x1b[5;10H')
    apc(b'a=T,f=100,i=2,c=3,r=1', png)
    surf, cr, w, h = surface(v)
    v._draw(v, cr, w, h)
    surf.flush()
    assert pixel(surf, cw, ch) == (255, 0, 0), f'rgba image: {pixel(surf, cw, ch)}'
    assert pixel(surf, 10 * cw + cw // 2, 4 * ch + ch // 2) == (0, 0, 255), 'png image missing'
    assert pixel(surf, 5 * cw, ch) != (255, 0, 0), 'image too wide'
    assert len(v._img) == 2
    # scrolled out of the viewport -> not drawn, surface cache freed
    v.term.feed(b'\r\n' * (v.rows + 5))
    v._draw(v, cr, w, h)
    surf.flush()
    assert pixel(surf, cw, ch) != (255, 0, 0) and not v._img, f'image after scrolling away: {v._img}'
    # back up: there again; delete by id (d=A would only hit the active screen, it's in scrollback now): gone
    v.scroll(lib.GHOSTTY_SCROLL_VIEWPORT_DELTA, -(v.rows + 5))
    v._draw(v, cr, w, h); surf.flush()
    assert pixel(surf, cw, ch) == (255, 0, 0), 'image not back after scrolling up'
    apc(b'a=d,d=I,i=1')
    v._draw(v, cr, w, h); surf.flush()
    assert pixel(surf, cw, ch) != (255, 0, 0) and list(v._img) == [2], f'image still there after delete: {v._img}'

def gui_robust(win):
    "security/robustness fixes: png bombs, blocked pty, broken feed, titles, bad env, stuck selection."
    import base64, signal, struct, zlib
    import tilix2
    from gi.repository import Gdk, GLib, Pango
    v = win.active.view
    # png: only real png, size checked from IHDR *before* decoding
    def png_chunk(kind, data): return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    bomb = b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('>IIBBBBB', 30000, 30000, 8, 6, 0, 0, 0)) \
        + png_chunk(b'IDAT', zlib.compress(b'\0' * 1000)) + png_chunk(b'IEND', b'')
    tiff = Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(bytes(4)), 4).save_to_tiff_bytes().get_data()
    for name, data in (('bomb', bomb), ('tiff', tiff), ('garbage', b'x' * 100)):
        out = tilix2.ffi.new('GhosttySysImage*')
        t0 = time.time()
        assert not tilix2._decode_png(tilix2.ffi.NULL, tilix2.ffi.NULL, data, len(data), out), f'{name} decoded'
        assert time.time() - t0 < 0.1, f'{name}: rejecting took {time.time() - t0:.2f}s'
    replies = []
    v.term.on_reply(replies.append)
    v.term.feed(b'\x1b_Ga=T,f=100,i=9;' + base64.b64encode(bomb) + b'\x1b\\')
    v.term.on_reply(v.write)
    assert replies and b'OK' not in replies[0], f'bomb accepted: {replies}'
    # pty write doesn't block when the program doesn't read; rest queued, goes out after SIGCONT
    os.kill(v.proc.pid, signal.SIGSTOP)
    t0 = time.time()
    v.write(b'line\n' * 40000)               # 200 kb, far more than the pty buffer
    assert time.time() - t0 < 0.2, f'write blocked {time.time() - t0:.2f}s'
    assert v._out and v._out_src, 'nothing queued although the reader is stopped'
    os.kill(v.proc.pid, signal.SIGCONT)
    assert pump_until(lambda: not v._out and v._out_src is None, 10), f'queue not drained: {len(v._out)} bytes left'
    # long IM commit (> 128 byte encoder buffer) goes out completely
    sent = capture(v)
    v.send_key(Gdk.KEY_a, 0, 0, text='é' * 300)
    del v.write
    assert sent == ['é'.encode() * 300], f'long commit: {[len(s) for s in sent]} bytes'
    # title: no control chars, max 200
    v.term.feed(b'\x1b]2;a\tb\x7fc' + b'x' * 500 + b'\x1b\\')
    t = v._title()
    assert t.startswith('abc') and len(t) == 200, f'title: {t[:20]!r}... ({len(t)})'
    # selection whose release got lost: motion without button 1 drops it
    class G:
        def __init__(s, state): s.state = state
        def get_current_button(s): return 1
        def get_current_event_state(s): return s.state
    v._press(G(Gdk.ModifierType.BUTTON1_MASK), 1, 1.0, 1.0)
    v._motion(G(0), v.cell_w * 5.5, 1.0)
    assert v.anchor is None and v.selected_text() == '', 'selection keeps going without a button'
    # pixel sizes > 65535 (huge zoom) don't break the winsize
    cols = v.cols; v.cols = 20000
    try: v._winsize()
    finally: v.cols = cols
    # font without size gets one, missing shell -> /bin/sh instead of a traceback
    assert Pango.FontDescription.from_string(tilix2.sized_font('Monospace')).get_size() == 11 * Pango.SCALE
    os.environ['TILIX2_SHELL'] = '/nonexistent/shell'
    try: win.split(win.active, vertical=False)
    finally: os.environ['TILIX2_SHELL'] = 'cat'
    assert pump_until(lambda: len(win.tiles()) == 2)
    assert win.active.view.proc.args == ['/bin/sh'], win.active.view.proc.args
    # exception in feed: the tile closes instead of becoming a zombie; glib's reaper syncs Popen
    broken = win.active
    def boom(data): raise RuntimeError('test: feed breaks')
    broken.view.term.feed = boom
    broken.view.write(b'echo hi\n')
    assert pump_until(lambda: broken.get_root() is None, 3), 'broken tile still there'
    assert pump_until(lambda: broken.view.proc.returncode is not None, 3), 'child watch did not sync returncode'

def gui_scrollbar(win):
    tile = win.active; v = tile.view
    v.term.feed(b''.join(b'row %d\r\n' % i for i in range(200)))
    v.changed()
    assert tile.sbar.get_visible(), 'scrollbar not visible despite scrollback'
    assert pump_until(lambda: 0 < tile.sbar.get_width() <= 6), f'scrollbar too wide: {tile.sbar.get_width()}px'
    total, offset, length = tile._sb
    assert tile.adj.get_upper() == total and tile.adj.get_page_size() == length == v.rows
    tile.adj.set_value(5)
    assert not v.term.get('viewport_active') and tile._sb[1] == 5, f'sbar drag: {tile._sb}'
    v._scroll(None, 0.0, -1.0)
    assert tile._sb[1] == 2 and tile.adj.get_value() == 2, f'wheel up: {tile._sb}'
    for _ in range(5): v._scroll(None, 0.0, -1.0)
    assert tile._sb[1] == 0, 'scrolls past the beginning'
    # alternate screen (less, man): wheel -> arrow keys
    v.term.feed(b'\x1b[?1049h')
    sent = capture(v)
    v._scroll(None, 0.0, 1.0)
    assert sent == [b'\x1b[B'] * 3, f'alt screen wheel: {sent}'
    del v.write
    v.term.feed(b'\x1b[?1049l')
    lbl = tile.pwd_lbl
    assert pump_until(lambda: lbl.get_label().startswith(('/', '~')), 2.5), f'pwd label: {lbl.get_label()!r}'

def gui_perf(win):
    from pyghostty import lib, ffi
    v = win.active.view
    v.term.feed(b''.join(b'line %d: fox \xe4\xb8\xad\xe6\x96\x87 \x1b[31mred\x1b[0m %d\r\n' % (i, i) for i in range(200)))
    surf, cr, w, h = surface(v)
    v._draw(v, cr, w, h)
    def bench(prep):
        t0 = time.perf_counter()
        for _ in range(30):
            prep(); v._draw(v, cr, w, h)
        return (time.perf_counter() - t0) / 30 * 1000
    full = ffi.new('GhosttyRenderStateDirty*', lib.GHOSTTY_RENDER_STATE_DIRTY_FULL)
    warm = bench(lambda: None)
    typing = bench(lambda: v.term.feed(b'x'))
    cold = bench(lambda: lib.ghostty_render_state_set(v.rs[0], lib.GHOSTTY_RENDER_STATE_OPTION_DIRTY, full))
    print(f'perf {v.cols}x{v.rows}: warm={warm:.1f}ms typing={typing:.1f}ms cold={cold:.1f}ms', flush=True)
    assert warm < 30 and typing < 30 and cold < 80, 'too slow'

GUI = {'layout': gui_layout, 'tabs': gui_tabs, 'dnd': gui_dnd, 'input': gui_input, 'clipboard': gui_clipboard, 'render': gui_render,
       'images': gui_images, 'robust': gui_robust, 'scrollbar': gui_scrollbar, 'perf': gui_perf}

def run_gui(fn):
    "fn(win) in a fresh app; assertion -> exit 1."
    os.environ.setdefault('TILIX2_SHELL', 'cat')
    if os.environ.get('DISPLAY'):             # xvfb-run: use x11, not the wayland of the desktop session
        os.environ.setdefault('GDK_BACKEND', 'x11')
    import tilix2
    from gi.repository import GLib
    app = tilix2.App()
    def go():
        try:
            win = app.get_active_window()
            assert pump_until(lambda: win.tiles()[0].view.get_width() > 100), 'window gets no size'
            fn(win)
        except Exception:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    app.connect('activate', lambda a: GLib.timeout_add(200, go))
    app.run([])
    os._exit(1)

if __name__ == '__main__':
    name = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if name in GUI:
        run_gui(GUI[name])
    if name in ('encoder', 'all'):
        encoder_checks(); print('encoder OK', flush=True)
    if name in ('gui', 'all'):
        assert os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'), 'gui checks need DISPLAY (xvfb-run -a ...)'
        fail = 0
        for n in GUI:
            r = subprocess.run([sys.executable, __file__, n], timeout=120)
            print(f'{n} {"OK" if r.returncode == 0 else "FAIL"}', flush=True)
            fail |= r.returncode
        sys.exit(fail)
