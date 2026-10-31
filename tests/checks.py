#!/usr/bin/env python3
# tests/checks.py — test-suite für tilix2.
#
#   python tests/checks.py              alles (gui-checks brauchen DISPLAY, z. B. xvfb-run -a)
#   python tests/checks.py encoder      nur headless vt/encoder-checks (kein DISPLAY nötig)
#   python tests/checks.py gui          nur gui-checks (einzeln als subprocess, DISPLAY nötig)
#   python tests/checks.py <name>       einzelner check: encoder | selftest | layout | render | scrollbar | perf
#
# exit 0 = ok. jede stufe printet "<NAME> OK".
import os, sys, subprocess, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---------- headless: ghostty vt + encoder (kein display) ----------

def encoder_checks():
    from pyghostty import Terminal, lib, ffi
    import gi; gi.require_version('Gdk', '4.0')
    from gi.repository import Gdk

    def enc(term, encoder, gk, mods=0, utf8=None, consumed=0, cp=None):
        ev = ffi.new('GhosttyKeyEvent*'); lib.ghostty_key_event_new(ffi.NULL, ev)
        lib.ghostty_key_event_set_action(ev[0], lib.GHOSTTY_KEY_ACTION_PRESS)
        lib.ghostty_key_event_set_key(ev[0], getattr(lib, 'GHOSTTY_KEY_' + gk))
        lib.ghostty_key_event_set_mods(ev[0], mods)
        lib.ghostty_key_event_set_consumed_mods(ev[0], consumed)
        if utf8:
            b = utf8.encode(); lib.ghostty_key_event_set_utf8(ev[0], b, len(b))
        if cp is not None: lib.ghostty_key_event_set_unshifted_codepoint(ev[0], cp)
        lib.ghostty_key_encoder_setopt_from_terminal(encoder[0], term._t[0])
        out = ffi.new('char[]', 64); n = ffi.new('size_t*')
        r = lib.ghostty_key_encoder_encode(encoder[0], ev[0], out, 64, n)
        lib.ghostty_key_event_free(ev[0])
        assert r == 0, r
        return ffi.buffer(out, n[0])[:]

    CTRL, SHIFT = 2, 1
    t = Terminal(cols=80, rows=24)
    encoder = ffi.new('GhosttyKeyEncoder*'); lib.ghostty_key_encoder_new(ffi.NULL, encoder)
    assert enc(t, encoder, 'D', CTRL, consumed=CTRL, cp=ord('d')) == b'\x04', 'legacy ctrl+d'
    assert enc(t, encoder, 'ENTER', CTRL, consumed=CTRL) == b'\x1b[27;5;13~', 'legacy ctrl+enter'
    assert enc(t, encoder, 'A', 0, 'a', cp=ord('a')) == b'a'
    assert enc(t, encoder, 'UNIDENTIFIED', 0, 'ä', cp=ord('ä')) == 'ä'.encode()
    t.feed(b'\x1b[>1u')   # kitty keyboard an
    assert enc(t, encoder, 'ENTER', CTRL, consumed=CTRL) == b'\x1b[13;5u', 'kitty ctrl+enter'
    assert enc(t, encoder, 'D', CTRL, consumed=CTRL, cp=ord('d')) == b'\x1b[100;5u', 'kitty ctrl+d'
    assert enc(t, encoder, 'W', CTRL, consumed=CTRL, cp=ord('w')) == b'\x1b[119;5u', 'kitty ctrl+w'
    assert enc(t, encoder, 'A', SHIFT, 'A', cp=ord('a')) == b'\x1b[97;2u', 'kitty shift+a unshifted'
    # utf8 / wide chars / grapheme-cluster mit size-query
    t2 = Terminal(cols=80, rows=24)
    t2.feed('ab中文cd äöü')
    assert t2.text() == 'ab中文cd äöü'
    ref = t2.ref(2, 0)
    cell = ffi.new('GhosttyCell*'); lib.ghostty_grid_ref_cell(ref, cell)
    wu = ffi.new('uint8_t*'); lib.ghostty_cell_get(cell[0], lib.GHOSTTY_CELL_DATA_WIDE, wu)
    assert wu[0] == 1, '中 ist wide'
    t2.feed('\r\nx' + '́' * 20)
    n = ffi.new('size_t*')
    lib.ghostty_grid_ref_graphemes(t2.ref(0, 1), ffi.NULL, 0, n)
    assert n[0] > 8, 'combining-cluster > 8 codepoints, size-query nötig'
    t2.resize(120, 30, 8, 16)
    assert t2.size == (120, 30)
    # scroll viewport (tagged union -> ABI-twin)
    from pyghostty._ffi import union_fn
    ffi.cdef('typedef struct { uint32_t tag; uint32_t _pad; int64_t delta; int64_t _pad2; } SVS;')
    sv = union_fn('ghostty_terminal_scroll_viewport', 'GhosttyResult(*)(GhosttyTerminal, SVS)')
    t3 = Terminal(cols=20, rows=5)
    t3.feed('\r\n'.join(f'line{i:02d}' for i in range(30)) + '\r\n')
    v = ffi.new('SVS*'); v.tag = lib.GHOSTTY_SCROLL_VIEWPORT_DELTA; v.delta = -3
    assert sv(t3._t[0], v[0]) == 0 and t3.get('viewport_active') == 0
    v2 = ffi.new('SVS*'); v2.tag = lib.GHOSTTY_SCROLL_VIEWPORT_BOTTOM
    assert sv(t3._t[0], v2[0]) == 0 and t3.get('viewport_active') == 1

# ---------- gui-checks (DISPLAY nötig; jeder als eigener prozess) ----------

def _run_gui(fn):
    "fn(win) in frischer app ausführen; assertions -> exit code."
    import tilix2
    from gi.repository import GLib
    app = tilix2.App(False)
    def act(a):
        app.do_activate()
        fn(app.get_active_window())
    app.connect('activate', act)
    sys.exit(app.run([]))

def gui_layout(win):
    from gi.repository import GLib, Gdk
    import select
    def body():
        tile = next(iter(win.terms)); tv = tile.view
        assert tv.get_width() > 500 and tv.get_height() > 400, f'kachel zu klein: {tv.get_width()}x{tv.get_height()}'
        assert win.get_titlebar() is not None, 'keine headerbar'
        assert tv.cols > 40 and tv.rows > 10, f'grid zu klein: {tv.cols}x{tv.rows}'
        win.split(tile, vertical=False)   # split läuft in idle_add
        import time as _t
        end = _t.time() + 2
        p = tile.get_parent()
        while _t.time() < end and (not hasattr(p, 'get_position') or p.get_position() <= 0):
            while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
            _t.sleep(0.02)
            p = tile.get_parent()
        assert hasattr(p, 'get_position') and p.get_position() > 0, f'split-position nicht gesetzt: parent={type(p).__name__}'
        new = p.get_end_child()
        assert win.active is new, 'neue kachel nicht aktiv'
        def widths():
            # erst nach allocate/queue_draw ist die breite gesetzt
            for _ in range(20):
                if new.view.get_width() > 100: break
                import time as _t; _t.sleep(0.1)
                while GLib.MainContext.default().pending(): GLib.MainContext.default().iteration(False)
            assert new.view.get_width() > 100, f'neue kachel zu schmal: {new.view.get_width()}'
            atv = win._focused().view
            win._on_key(None, Gdk.KEY_a, 38, 0)
            win._on_key(None, Gdk.KEY_Return, 36, 0)
            got = b''; end = time.time() + 2
            while time.time() < end:
                r, _, _ = select.select([atv.master], [], [], 0.3)
                if r:
                    try: got += os.read(atv.master, 4096)
                    except OSError: break
            assert b'a' in got and b'\r' in got, f'keys nicht im pty: {got!r}'
            atv.term.feed(b'\x1b[>1u')
            sent = []; ow = os.write
            os.write = lambda fd, b: (sent.append(bytes(b)), len(b))[1] if fd == atv.master else ow(fd, b)
            try: win._on_key(None, Gdk.KEY_d, 40, Gdk.ModifierType.CONTROL_MASK)
            finally: os.write = ow
            assert any(b'\x1b[100;5u' in s for s in sent), f'kitty ctrl+d falsch: {sent!r}'
            atv.term.feed(b'\x1b[<u')
            n = len(win.terms)
            other = [t for t in win.terms if t is not tile][0]
            tile.view.proc.kill()
            def done():
                assert len(win.terms) == n - 1 and tile not in win.terms, 'auto-close ging nicht'
                assert other.view.get_width() > win.get_width() - 100, 'platz nicht freigegeben'
                os._exit(0)
            GLib.timeout_add(1500, done)
        GLib.timeout_add(700, widths)
    GLib.timeout_add(700, body)

def gui_render(win):
    import cairo
    from gi.repository import GLib
    def body():
        tv = next(iter(win.terms)).view
        tv._update_metrics()
        w, h = tv.cols * tv.cell_w, tv.rows * tv.cell_h
        tv.term.resize(tv.cols, tv.rows, tv.cell_w, tv.cell_h)
        tv.term.feed(b'\x1b[31mRED\x1b[0m plain \xe4\xb8\xad\xe6\x96\x87 \xf0\x9f\x98\x80 \x1b[1;34mboldblue\x1b[0m\r\n')
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        tv._draw(tv, cr, w, h)
        surf.flush()
        data = surf.get_data()
        bg = bytes([0x20, 0x1a, 0x18, 0xff])
        diff = sum(1 for i in range(0, len(data), 4) if data[i:i+4] != bg)
        # cairo ARGB32 little-endian: byte0=B, byte1=G, byte2=R, byte3=A
        red = sum(1 for i in range(0, len(data), 4) if data[i+2] > 150 and data[i+1] < 100 and data[i] < 100)
        print(f'render stats: diff={diff} red={red}', flush=True)
        assert diff > 500, f'render schwach: diff={diff} (red={red} ist theme-abhängig)'
        os._exit(0)
    GLib.timeout_add(700, body)

def gui_scrollbar(win):
    from gi.repository import GLib
    tile = next(iter(win.terms))
    tile.view.term.feed(b''.join(b'row %d\r\n' % i for i in range(200)))
    def done():
        tile._tick()
        assert tile.sbar.get_visible(), 'scrollbar nicht sichtbar trotz scrollback'
        assert tile.sbar.get_allocation().width <= 6, f'scrollbar zu breit: {tile.sbar.get_allocation().width}px'
        lbl = tile.pwd_lbl.get_label()
        assert '/' in lbl or '~' in lbl, f'pwd-label falsch: {lbl!r}'
        tile.adj.set_value(5)
        assert not tile.view.term.get('viewport_active'), 'sbar-drag bewegt viewport nicht'
        tile.view._scroll(None, 0.0, -1.0); tile.view._scroll(None, 0.0, -1.0)
        assert tile._sb_top >= 0, f'_sb_top driftet negativ: {tile._sb_top}'
        os._exit(0)
    GLib.timeout_add(1500, done)

def gui_perf(win):
    import cairo, time
    from gi.repository import GLib
    def body():
        tv = next(iter(win.terms)).view
        tv._update_metrics()
        w, h = tv.cols * tv.cell_w, tv.rows * tv.cell_h
        tv.term.resize(tv.cols, tv.rows, tv.cell_w, tv.cell_h)
        tv.term.feed(b''.join(b'line %d: fox \xe4\xb8\xad\xe6\x96\x87 \x1b[31mred\x1b[0m %d\r\n' % (i, i) for i in range(200)))
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        tv._draw(tv, cr, w, h)
        n = 30
        t0 = time.perf_counter()
        for _ in range(n): tv._draw(tv, cr, w, h)
        warm = (time.perf_counter() - t0) / n * 1000
        t0 = time.perf_counter()
        for _ in range(n):
            tv._grid_version += 1
            tv._draw(tv, cr, w, h)
        cold = (time.perf_counter() - t0) / n * 1000
        print(f'perf: warm={warm:.1f}ms cold={cold:.1f}ms', flush=True)
        assert warm < 30 and cold < 60, f'zu langsam: warm={warm:.1f} cold={cold:.1f}'
        os._exit(0)
    GLib.timeout_add(700, body)

GUI = {'layout': gui_layout, 'render': gui_render, 'scrollbar': gui_scrollbar, 'perf': gui_perf}

def selftest():
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'tilix2.py'), '--selftest'],
                       capture_output=True, text=True, timeout=180)
    assert 'SELFTEST OK' in r.stdout, (r.stdout + r.stderr)[-3000:]

if __name__ == '__main__':
    name = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if name == 'encoder':
        encoder_checks(); print('encoder OK', flush=True); sys.exit(0)
    if name == 'selftest':
        selftest(); print('selftest OK', flush=True); sys.exit(0)
    if name in GUI:
        assert os.environ.get('DISPLAY'), 'DISPLAY fehlt (xvfb-run -a ...)'
        _run_gui(GUI[name])
    if name in ('gui', 'all'):
        assert os.environ.get('DISPLAY'), 'gui-checks brauchen DISPLAY (xvfb-run -a ...)'
        fail = 0
        for n in GUI:
            r = subprocess.run([sys.executable, __file__, n], timeout=120)
            print(f'{n} {"OK" if r.returncode == 0 else "FAIL"}', flush=True)
            fail |= r.returncode
        if name == 'all':
            selftest(); print('selftest OK', flush=True)
        sys.exit(fail)
    if name == 'all':
        encoder_checks(); print('encoder OK', flush=True)
        selftest(); print('selftest OK', flush=True)
        print('ALL OK', flush=True)
