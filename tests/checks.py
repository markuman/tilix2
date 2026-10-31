#!/usr/bin/env python3
# tests/checks.py — tests für tilix2.
#
#   python tests/checks.py              alles (gui-checks brauchen DISPLAY, z. b. xvfb-run -a)
#   python tests/checks.py encoder      nur headless key-encoder/vt-checks (kein DISPLAY)
#   python tests/checks.py <name>       ein gui-check: layout | input | clipboard | render | scrollbar | perf
#
# exit 0 = ok, jeder check printet "<name> OK". die gui-checks laufen mit TILIX2_SHELL=cat
# (kein prompt -> deterministischer bildschirminhalt).
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
    # so ruft tilix2 den encoder auf: utf8 = text der taste, consumed = shift wenn text
    assert enc(t, 'D', CTRL, 'd', cp=ord('d')) == b'\x04', 'legacy ctrl+d'
    assert enc(t, 'ENTER') == b'\r', 'legacy enter'
    assert enc(t, 'ENTER', CTRL) == b'\x1b[27;5;13~', 'legacy ctrl+enter'
    assert enc(t, 'A', 0, 'a', cp=ord('a')) == b'a'
    assert enc(t, 'UNIDENTIFIED', 0, 'ä', cp=ord('ä')) == 'ä'.encode()
    t.feed(b'\x1b[>1u')   # kitty keyboard, flags=1 (disambiguate)
    assert enc(t, 'ENTER', CTRL) == b'\x1b[13;5u', 'kitty ctrl+enter'
    assert enc(t, 'D', CTRL, 'd', cp=ord('d')) == b'\x1b[100;5u', 'kitty ctrl+d'
    assert enc(t, 'A', SHIFT, 'A', SHIFT, ord('a')) == b'A', 'kitty shift+a bleibt text'
    assert enc(t, 'A', SHIFT, 'A', 0, ord('a')) == b'\x1b[97;2u', 'ohne consumed shift: CSI u'
    # utf8 / wide chars / lange grapheme-cluster
    t2 = Terminal(cols=80, rows=24)
    t2.feed('ab中文cd äöü')
    assert t2.text() == 'ab中文cd äöü'
    t2.resize(120, 30, 8, 16)
    assert t2.size == (120, 30)

# ---------- gui (DISPLAY nötig; jeder check als eigener prozess) ----------

def pump_until(cond, timeout=2.0):
    "main-loop laufen lassen, bis cond() wahr ist (oder timeout). gibt cond() zurück."
    from gi.repository import GLib
    ctx = GLib.MainContext.default()
    end = time.time() + timeout
    while time.time() < end:
        while ctx.pending(): ctx.iteration(False)
        if cond(): return True
        time.sleep(0.01)
    return cond()

def capture(view):
    "alles abfangen, was die view ins pty schreiben würde. `del view.write` stellt's wieder her."
    sent = []
    view.write = lambda b: sent.append(bytes(b))
    return sent

def key(win, keyval, state=0, keycode=0):
    return win._on_key(None, keyval, keycode, state)

def kc(keyval):
    "keycode der taste, die keyval erzeugt (layout-unabhängig: '+' ist auf de/us eine andere taste)"
    from gi.repository import Gdk
    ok, keys = Gdk.Display.get_default().map_keyval(keyval)
    assert ok and keys, f'keyval {keyval:#x} nicht im layout'
    return keys[0].keycode

def surface(view):
    import cairo
    w, h = view.cols * view.cell_w, view.rows * view.cell_h
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    return surf, cairo.Context(surf), w, h

def pixel(surf, x, y):
    "(r, g, b) als 0..255 — cairo ARGB32 little-endian: B G R A"
    d = surf.get_data(); i = y * surf.get_stride() + x * 4
    return (d[i + 2], d[i + 1], d[i])

def gui_layout(win):
    from gi.repository import Gtk, Gdk
    t1 = win.tiles()[0]
    assert t1.view.get_width() > 500 and t1.view.get_height() > 400, 'kachel zu klein'
    assert win.get_titlebar() is not None, 'keine headerbar'
    assert t1.view.cols > 40 and t1.view.rows > 10, f'grid zu klein: {t1.view.cols}x{t1.view.rows}'
    win.split(t1, vertical=False)
    assert pump_until(lambda: len(win.tiles()) == 2), 'split ging nicht'
    p = t1.get_parent()
    assert isinstance(p, Gtk.Paned) and p.get_orientation() == Gtk.Orientation.HORIZONTAL
    t2 = p.get_end_child()
    assert win.active is t2, 'neue kachel nicht aktiv'
    assert pump_until(lambda: t2.view.get_width() > 100), f'neue kachel zu schmal: {t2.view.get_width()}'
    assert abs(t1.view.get_width() - t2.view.get_width()) < 20, 'split nicht mittig'
    assert t2.view.cwd() == t1.view.cwd(), 'split erbt cwd nicht'
    win.split(t2, vertical=True)
    assert pump_until(lambda: len(win.tiles()) == 3)
    assert t2.get_parent().get_orientation() == Gtk.Orientation.VERTICAL
    key(win, Gdk.KEY_Right, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)
    assert pump_until(lambda: len(win.tiles()) == 4), 'ctrl+alt+right splittet nicht'
    sizes = lambda: [(t.view.get_width(), t.view.get_height()) for t in win.tiles()]
    assert pump_until(lambda: all(w > 50 and h > 50 for w, h in sizes())), f'kachel mit 0px: {sizes()}'
    # schließen blockiert nicht und die shell bekommt SIGHUP
    victim = win.active
    t0 = time.time(); win.close_tile(victim); dt = time.time() - t0
    assert dt < 0.2, f'close blockiert {dt:.2f}s'
    assert victim not in win.tiles() and len(win.tiles()) == 3
    assert pump_until(lambda: victim.view.proc.poll() is not None), 'shell lebt nach close noch'
    # move per menü + per drag&drop (tauscht im selben paned)
    p = t2.get_parent()
    first = p.get_start_child()
    win.move(t2, 'down' if p.get_start_child() is t2 else 'up')
    assert pump_until(lambda: p.get_start_child() is not first), 'move swapped nicht'
    sib = p.get_start_child() if p.get_end_child() is t2 else p.get_end_child()
    first = p.get_start_child()
    win.move_toward(t2, sib)
    assert pump_until(lambda: p.get_start_child() is not first), 'move_toward swapped nicht'
    # prozess-ende schließt die kachel, der nachbar bekommt den platz
    for t in win.tiles()[1:]: win.close_tile(t)
    win.split(t1, vertical=False)
    assert pump_until(lambda: len(win.tiles()) == 2)
    other = win.tiles()[1]
    t1.view.proc.kill()
    assert pump_until(lambda: t1 not in win.tiles()), 'tote kachel nicht geschlossen'
    assert pump_until(lambda: other.view.get_width() > win.get_width() - 100), 'platz nicht freigegeben'

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
    v.term.feed(b'\x1b[<u\x1b[>8u')     # flag 8: alle tasten als CSI u -> unshifted codepoint sichtbar
    sent.clear(); key(win, Gdk.KEY_exclam, MT.SHIFT_MASK, kc(Gdk.KEY_1))
    assert sent and sent[0].startswith(b'\x1b[49;'), f'shift+1 unshifted != "1": {sent}'
    v.term.feed(b'\x1b[<u')
    # IM-commit außerhalb von filter_keypress (ibus asynchron) geht direkt als text raus
    sent.clear(); win._im_commit(None, 'é')
    assert sent == ['é'.encode()], sent
    # tippen holt den viewport nach unten
    v.term.feed(b''.join(b'line %d\r\n' % i for i in range(200)))
    v.scroll(lib.GHOSTTY_SCROLL_VIEWPORT_DELTA, -10)
    assert not v.term.get('viewport_active')
    key(win, Gdk.KEY_a, 0, kc(Gdk.KEY_a))
    assert v.term.get('viewport_active'), 'tippen scrollt nicht nach unten'
    # maus: SGR-encoding, middle ist button 1 (nicht 2)
    v.term.feed(b'\x1b[?1000h\x1b[?1006h')
    sent.clear()
    v._mencode(lib.GHOSTTY_MOUSE_ACTION_PRESS, tilix2.MOUSE_BTN[1], 10.0, 10.0, 0)
    v._mencode(lib.GHOSTTY_MOUSE_ACTION_PRESS, tilix2.MOUSE_BTN[2], 10.0, 10.0, 0)
    assert sent[0].startswith(b'\x1b[<0;') and sent[1].startswith(b'\x1b[<1;'), f'maus: {sent}'
    v.term.feed(b'\x1b[?1000l\x1b[?1006l')
    del v.write
    # pty-roundtrip durch `cat`
    v.write(b'MARKER_123\r')
    assert pump_until(lambda: v.term.contents().count('MARKER_123') >= 2), v.term.contents()[-200:]
    # zoom nur aktive kachel; matcht nach physischer taste -> keycode aus dem aktuellen layout
    win.split(win.active, vertical=False)
    assert pump_until(lambda: len(win.tiles()) == 2)
    a, other = win.active, [t for t in win.tiles() if t is not win.active][0]
    f0, fo = a.view.font, other.view.font
    CS = MT.CONTROL_MASK | MT.SHIFT_MASK
    key(win, Gdk.KEY_plus, CS, kc(Gdk.KEY_plus))
    assert a.view.font != f0 and other.view.font == fo, 'zoom-in falsch'
    key(win, Gdk.KEY_underscore, CS, kc(Gdk.KEY_minus))
    assert a.view.font == f0, 'zoom-out falsch'
    key(win, Gdk.KEY_plus, CS, kc(Gdk.KEY_plus))
    key(win, Gdk.KEY_parenright, CS, kc(Gdk.KEY_0))   # shift+0 ist auf us ')', auf de '='
    assert a.view.font == tilix2.DEFAULT_FONT, 'zoom-reset falsch'

def gui_clipboard(win):
    from gi.repository import Gdk
    v = win.active.view
    d = Gdk.Display.get_default()
    def read(cb):
        got = []
        cb.read_text_async(None, lambda s, r: got.append(s.read_text_finish(r)))
        assert pump_until(lambda: got), 'clipboard lesen hängt'
        return got[0]
    v.term.feed(b'hello world\r\nzweite zeile\r\n')
    v.select((0, 0), (4, 0))
    assert v.selected_text() == 'hello'
    v.select((4, 0), (0, 0))
    assert v.selected_text() == 'hello', 'rückwärts markieren'
    v.copy()
    assert read(d.get_clipboard()) == 'hello' and read(d.get_primary_clipboard()) == 'hello'
    # markierung wird invertiert gerendert
    surf, cr, w, h = surface(v)
    v._draw(v, cr, w, h)
    bgs, runs, _ = v._rowcache[0]
    assert bgs and bgs[0][0] == 0 and bgs[0][1] == 5, f'markierung nicht hinterlegt: {bgs}'
    # maus-markierung -> primary
    class G:
        def __init__(s, b): s.b = b
        def get_current_button(s): return s.b
        def get_current_event_state(s): return 0
    d.get_primary_clipboard().set_content(Gdk.ContentProvider.new_for_value('x'))
    v._press(G(1), 1, 1.0, v.cell_h * 1.5)                       # zeile 1, spalte 0
    v._motion(G(1), v.cell_w * 5.5, v.cell_h * 1.5)              # bis spalte 5
    v._release(G(1), 1, v.cell_w * 5.5, v.cell_h * 1.5)
    assert read(d.get_primary_clipboard()) == 'zweite', 'markieren setzt primary nicht'
    # markierung bleibt beim scrollen (ghostty trackt sie)
    v.term.feed(b''.join(b'row %d\r\n' % i for i in range(100)))
    assert v.selected_text() == 'zweite', 'markierung beim scrollen verloren'
    # klick ohne ziehen löscht die markierung, copy ohne markierung lässt das clipboard in ruhe
    v._press(G(1), 1, 1.0, 1.0); v._release(G(1), 1, 1.0, 1.0)
    assert v.selected_text() == ''
    d.get_clipboard().set_content(Gdk.ContentProvider.new_for_value('unverändert'))
    v.copy()
    assert read(d.get_clipboard()) == 'unverändert'
    # paste: ESC wird entschärft, bracketed paste klammert
    v.term.feed(b'\x1b[?2004h')
    d.get_clipboard().set_content(Gdk.ContentProvider.new_for_value('echo hi\x1b[201~; evil\n'))
    sent = capture(v)
    v.paste()
    assert pump_until(lambda: sent), 'paste schreibt nichts'
    assert sent == [b'\x1b[200~echo hi [201~; evil\n\x1b[201~'], f'paste nicht entschärft: {sent}'
    # middle-click fügt primary ein
    sent.clear()
    v._press(G(2), 1, 10.0, 10.0)
    assert pump_until(lambda: sent) and b'zweite' in sent[0], f'middle-click: {sent}'
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
    assert 'RED' in texts and texts['RED'][2][0] > texts['RED'][2][1], f'RED nicht rot: {texts.get("RED")}'
    assert any(r[1] == 2 and r[5] > 0 for r in runs), 'wide char nicht auf 2 zellen skaliert'
    dim = [r for r in runs if 'dim' in r[3].get_text()][0]
    assert sum(dim[2]) < sum(c / 255 for c in FG), 'faint nicht gedimmt'
    assert any(r[4][0] for r in runs if 'under' in r[3].get_text()), 'underline fehlt'
    inv_x = 30      # 'RED plain 中文 bold dim under inv': 中/文 sind je 2 zellen breit
    assert chars[inv_x] == 'i', chars
    assert any(b[0] <= inv_x < b[0] + b[1] for b in bgs), 'inverse hat keinen hintergrund'
    red = sum(1 for y in range(v.cell_h) for x in range(3 * v.cell_w)
              if (p := pixel(surf, x, y))[0] > 150 and p[0] > p[1] + 50)
    assert red > 10, f'kein rot im bild: {red}'
    # cursor (zeile 1, spalte 0): block mit fokus, hohl ohne
    cx, cy = v.cell_w // 2, v.cell_h + v.cell_h // 2
    want = FG if v.has_focus() else BG
    assert pixel(surf, cx, cy) == want, f'cursor: {pixel(surf, cx, cy)} != {want} (fokus={v.has_focus()})'
    # dirty-tracking: ein zeichen -> genau eine zeile wird neu aufgebaut
    n = []
    orig = v._build_row
    v._build_row = lambda *a: (n.append(1), orig(*a))[1]
    v._draw(v, cr, w, h)
    assert not n, f'ohne änderung {len(n)} zeilen neu gebaut'
    v.term.feed(b'x')
    v._draw(v, cr, w, h)
    assert len(n) == 1, f'ein zeichen -> {len(n)} zeilen neu gebaut'

def gui_scrollbar(win):
    tile = win.active; v = tile.view
    v.term.feed(b''.join(b'row %d\r\n' % i for i in range(200)))
    v.changed()
    assert tile.sbar.get_visible(), 'scrollbar nicht sichtbar trotz scrollback'
    assert pump_until(lambda: 0 < tile.sbar.get_width() <= 6), f'scrollbar zu breit: {tile.sbar.get_width()}px'
    total, offset, length = tile._sb
    assert tile.adj.get_upper() == total and tile.adj.get_page_size() == length == v.rows
    tile.adj.set_value(5)
    assert not v.term.get('viewport_active') and tile._sb[1] == 5, f'sbar-drag: {tile._sb}'
    v._scroll(None, 0.0, -1.0)
    assert tile._sb[1] == 2 and tile.adj.get_value() == 2, f'rad hoch: {tile._sb}'
    for _ in range(5): v._scroll(None, 0.0, -1.0)
    assert tile._sb[1] == 0, 'scrollt über den anfang hinaus'
    # alternate screen (less, man): rad -> pfeiltasten
    v.term.feed(b'\x1b[?1049h')
    sent = capture(v)
    v._scroll(None, 0.0, 1.0)
    assert sent == [b'\x1b[B'] * 3, f'alt-screen-rad: {sent}'
    del v.write
    v.term.feed(b'\x1b[?1049l')
    lbl = tile.pwd_lbl
    assert pump_until(lambda: lbl.get_label().startswith(('/', '~')), 2.5), f'pwd-label: {lbl.get_label()!r}'

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
    assert warm < 30 and typing < 30 and cold < 80, 'zu langsam'

GUI = {'layout': gui_layout, 'input': gui_input, 'clipboard': gui_clipboard, 'render': gui_render,
       'scrollbar': gui_scrollbar, 'perf': gui_perf}

def run_gui(fn):
    "fn(win) in einer frischen app; assertion -> exit 1."
    os.environ.setdefault('TILIX2_SHELL', 'cat')
    import tilix2
    from gi.repository import GLib
    app = tilix2.App()
    def go():
        try:
            win = app.get_active_window()
            assert pump_until(lambda: win.tiles()[0].view.get_width() > 100), 'fenster bekommt keine größe'
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
        assert os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'), 'gui-checks brauchen DISPLAY (xvfb-run -a ...)'
        fail = 0
        for n in GUI:
            r = subprocess.run([sys.executable, __file__, n], timeout=120)
            print(f'{n} {"OK" if r.returncode == 0 else "FAIL"}', flush=True)
            fail |= r.returncode
        sys.exit(fail)
