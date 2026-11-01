# PKGBUILD for tilix2 — build from the repo root: `makepkg -si`
# tilix2.py goes to /usr/lib/tilix2, next to it the vendored pyghostty (not in the arch repos / aur,
# the py3-none wheel only uses cffi in abi mode -> fits every python 3). runs on the system python.
pkgname=tilix2
pkgver=1.0.0
pkgrel=1
_pyghostty=0.1.4
pkgdesc='tilix-style tiling terminal on libghostty-vt (gtk4, single python file)'
arch=('x86_64' 'aarch64')
license=('LicenseRef-unknown' 'Apache-2.0' 'MIT')   # tilix2: no license yet | pyghostty | libghostty-vt
depends=('python' 'python-gobject' 'gtk4' 'python-cairo' 'python-cffi')
optdepends=('ghostty-terminfo: TERM=xterm-ghostty instead of xterm-256color')
source=('tilix2.py' 'README.md')
source_x86_64=("pyghostty-$_pyghostty-x86_64.whl::https://files.pythonhosted.org/packages/4b/5b/4f23dd7d7c53950067c57514b083f0e70ed413a098b7964ad121439e88be/pyghostty-$_pyghostty-py3-none-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl")
source_aarch64=("pyghostty-$_pyghostty-aarch64.whl::https://files.pythonhosted.org/packages/d4/7f/d724d1c5dadc5ce8e029c22a42028bf1161b418e2dcb9e66460b23f32d37/pyghostty-$_pyghostty-py3-none-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl")
noextract=("pyghostty-$_pyghostty-$CARCH.whl")
sha256sums=('SKIP' 'SKIP')   # local files from the repo
sha256sums_x86_64=('ea203580d8424a79bbad75e159494591ef3d7d3dfa43675a80f72271b72329d0')
sha256sums_aarch64=('ea4d6366bbfb00dc445ac40c684b7a5154897dc9219ef949a490a9565eae2eba')

prepare() {
  mkdir -p wheel
  bsdtar -xf "pyghostty-$_pyghostty-$CARCH.whl" -C wheel
}

check() {
  # pkgver and VERSION in tilix2.py must agree; pyghostty must load on the system python
  [ "$(python tilix2.py --version)" = "tilix2 $pkgver" ]
  PYTHONPATH=wheel python -c 'from pyghostty import Terminal; t = Terminal(); t.feed(b"ok"); assert t.text() == "ok"'
}

package() {
  install -Dm644 tilix2.py "$pkgdir/usr/lib/tilix2/tilix2.py"
  cp -r wheel/pyghostty "$pkgdir/usr/lib/tilix2/"
  python -m compileall -q -s "$pkgdir" -p / "$pkgdir/usr/lib/tilix2/pyghostty"

  install -Dm755 /dev/stdin "$pkgdir/usr/bin/tilix2" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/tilix2/tilix2.py "$@"
EOF

  # file name = gtk application id, so wayland docks/panels match the window to this entry
  install -Dm644 /dev/stdin "$pkgdir/usr/share/applications/osuv.tilix2.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=tilix2
Comment=Tiling terminal on libghostty-vt
Exec=tilix2
Icon=utilities-terminal
Terminal=false
Categories=System;TerminalEmulator;
Keywords=terminal;shell;tiling;
StartupNotify=true
EOF

  install -Dm644 README.md "$pkgdir/usr/share/doc/tilix2/README.md"
  install -Dm644 "wheel/pyghostty-$_pyghostty.dist-info/licenses/LICENSE" "$pkgdir/usr/share/licenses/tilix2/pyghostty.LICENSE"
  install -Dm644 "wheel/pyghostty-$_pyghostty.dist-info/licenses/LICENSE.ghostty" "$pkgdir/usr/share/licenses/tilix2/ghostty.LICENSE"
}
