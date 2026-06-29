PREFIX ?= /usr/local
DESTDIR ?=
PYTHON ?= python3

BINDIR = $(DESTDIR)$(PREFIX)/bin
DATADIR = $(DESTDIR)$(PREFIX)/share
APPDIR = $(DATADIR)/openwave
DESKTOPDIR = $(DATADIR)/applications
DOCDIR = $(DATADIR)/doc/openwave
LICENSEDIR = $(DATADIR)/licenses/openwave

SITEPKG := $(shell $(PYTHON) -c "import site; print(site.getsitepackages()[0])")

.PHONY: install uninstall

install:
	install -dm755 $(DESTDIR)$(SITEPKG)/openwave
	install -m644 openwave/*.py openwave/style.css $(DESTDIR)$(SITEPKG)/openwave/
	install -dm755 $(BINDIR)
	printf '#!/bin/sh\nexec %s -m openwave "$$@"\n' "$(PYTHON)" > $(BINDIR)/openwave
	chmod 755 $(BINDIR)/openwave
	install -Dm644 openwave.desktop $(DESKTOPDIR)/openwave.desktop
	install -Dm644 wireplumber/51-openwave-wave-xlr.conf $(APPDIR)/wireplumber/51-openwave-wave-xlr.conf
	install -Dm644 pipewire/52-openwave-mixes.conf $(APPDIR)/pipewire/52-openwave-mixes.conf
	install -Dm644 README.md $(DOCDIR)/README.md
	install -Dm644 LICENSE $(LICENSEDIR)/LICENSE

uninstall:
	rm -rf $(DESTDIR)$(SITEPKG)/openwave
	rm -f $(BINDIR)/openwave
	rm -f $(DESKTOPDIR)/openwave.desktop
	rm -rf $(APPDIR)
	rm -rf $(DOCDIR)
	rm -rf $(LICENSEDIR)
