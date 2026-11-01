# tilix2 — makefile. plain `make` shows what's available.
.PHONY: help install run test clean uninstall

PREFIX   ?= $(HOME)/.local
BINDIR   := $(PREFIX)/bin
APPDIR   := $(PREFIX)/share/tilix2
VENV     := $(APPDIR)/venv
PY       := $(VENV)/bin/python
# tilix2 uses pyghostty internals (_t, _format_sel) -> pin the version
PYGHOSTTY := 0.1.4

help:
	@echo "tilix2 — tiling terminal (gtk4 + libghostty-vt via pyghostty)"
	@echo ""
	@echo "  make install    install to $(BINDIR)/tilix2 (+ venv with pyghostty in $(APPDIR)/venv)"
	@echo "  make run        run from the repo (uses repo venv if present, else system python)"
	@echo "  make test       run test-suite (gui checks need DISPLAY: xvfb-run -a make test)"
	@echo "  make uninstall  remove $(BINDIR)/tilix2 and $(APPDIR)"
	@echo "  make clean      remove __pycache__"

install:
	@mkdir -p $(BINDIR) $(APPDIR)
	cp tilix2.py $(APPDIR)/tilix2.py
	cp LICENSE $(APPDIR)/LICENSE
	@if [ ! -x $(VENV)/bin/python ]; then \
		echo "creating venv at $(VENV) (needs python3-gi/gtk4 on system, hence --system-site-packages)"; \
		python3 -m venv --system-site-packages $(VENV) && $(VENV)/bin/pip install -q pyghostty==$(PYGHOSTTY); \
	fi
	@printf '#!/bin/sh\nexec %s %s/tilix2.py "$$@"\n' '$(PY)' '$(APPDIR)' > $(BINDIR)/tilix2
	@chmod +x $(BINDIR)/tilix2
	@echo "installed: $(BINDIR)/tilix2"

run:
	@if [ -x venv/bin/python ]; then venv/bin/python tilix2.py; else python3 tilix2.py; fi

test:
	@if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi; \
	$$PY tests/checks.py all

uninstall:
	rm -f $(BINDIR)/tilix2
	rm -rf $(APPDIR)
	@echo "removed $(BINDIR)/tilix2 and $(APPDIR)"

clean:
	rm -rf __pycache__ tests/__pycache__
