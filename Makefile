.PHONY: build release test clean deb rpm install install-user fmt lint

VERSION := $(shell grep '^version' Cargo.toml | head -1 | sed 's/.*"\(.*\)"/\1/')
BINARY := target/release/gamepulse-agent

# ─── Build ───────────────────────────────────────────────────────────────────

build:
	cargo build

release:
	cargo build --release
	@echo "Built: $(BINARY)"
	@ls -lh $(BINARY)

# Cross-compile for Steam Deck (same arch, just need musl for static linking)
release-static:
	cargo build --release --target x86_64-unknown-linux-musl
	@echo "Static binary: target/x86_64-unknown-linux-musl/release/gamepulse-agent"

# ─── eBPF ────────────────────────────────────────────────────────────────────

ebpf:
	cd ebpf && \
	cargo xtask build-ebpf
	@echo "eBPF programs built in ebpf/target/bpfel-unknown-none/release/"

# ─── Test ────────────────────────────────────────────────────────────────────

test:
	cargo test

test-debug:
	$(BINARY) --debug --once

test-game-detection:
	$(BINARY) --debug --once 2>&1 | grep -E "Detected|Steam|game"

# ─── Code quality ────────────────────────────────────────────────────────────

fmt:
	cargo fmt

lint:
	cargo clippy -- -D warnings

# ─── Install ─────────────────────────────────────────────────────────────────

install: release
	sudo install -Dm755 $(BINARY) /usr/local/bin/gamepulse-agent
	sudo mkdir -p /etc/gamepulse
	@if [ ! -f /etc/gamepulse/gamepulse.toml ]; then \
		sudo cp config/gamepulse.toml /etc/gamepulse/gamepulse.toml; \
	fi
	sudo install -Dm644 packaging/systemd/gamepulse-agent.service /etc/systemd/system/
	sudo systemctl daemon-reload
	@echo ""
	@echo "Installed. Configure /etc/gamepulse/gamepulse.toml then:"
	@echo "  sudo systemctl enable --now gamepulse-agent"

install-user: release
	mkdir -p ~/.local/bin ~/.config/gamepulse ~/.config/systemd/user
	cp $(BINARY) ~/.local/bin/gamepulse-agent
	@if [ ! -f ~/.config/gamepulse/gamepulse.toml ]; then \
		cp config/gamepulse.toml ~/.config/gamepulse/gamepulse.toml; \
	fi
	cp packaging/systemd/gamepulse-agent-user.service ~/.config/systemd/user/gamepulse-agent.service
	sed -i "s|%h|$$HOME|g" ~/.config/systemd/user/gamepulse-agent.service
	systemctl --user daemon-reload
	@echo ""
	@echo "Installed for current user. Configure ~/.config/gamepulse/gamepulse.toml then:"
	@echo "  systemctl --user enable --now gamepulse-agent"

# ─── Packaging ───────────────────────────────────────────────────────────────

DEB_DIR := /tmp/gamepulse-deb

deb: release
	rm -rf $(DEB_DIR)
	mkdir -p $(DEB_DIR)/DEBIAN
	mkdir -p $(DEB_DIR)/usr/bin
	mkdir -p $(DEB_DIR)/usr/share/gamepulse/kibana
	mkdir -p $(DEB_DIR)/usr/lib/systemd/system
	mkdir -p $(DEB_DIR)/usr/lib/systemd/user
	cp packaging/deb/control $(DEB_DIR)/DEBIAN/
	cp packaging/deb/postinst $(DEB_DIR)/DEBIAN/
	chmod 755 $(DEB_DIR)/DEBIAN/postinst
	sed -i "s/Version:.*/Version: $(VERSION)/" $(DEB_DIR)/DEBIAN/control
	cp $(BINARY) $(DEB_DIR)/usr/bin/
	cp config/gamepulse.toml $(DEB_DIR)/usr/share/gamepulse/gamepulse.toml.default
	cp kibana/gamepulse-dashboard.ndjson $(DEB_DIR)/usr/share/gamepulse/kibana/
	cp packaging/systemd/gamepulse-agent.service $(DEB_DIR)/usr/lib/systemd/system/
	cp packaging/systemd/gamepulse-agent-user.service $(DEB_DIR)/usr/lib/systemd/user/
	dpkg-deb --build $(DEB_DIR) gamepulse-agent_$(VERSION)_amd64.deb
	@echo "Built: gamepulse-agent_$(VERSION)_amd64.deb"

rpm: release
	rpmbuild -bb packaging/rpm/gamepulse-agent.spec \
		--define "_sourcedir $(PWD)" \
		--define "version $(VERSION)"

# ─── Kibana ──────────────────────────────────────────────────────────────────

import-dashboards:
	@read -p "Kibana URL: " KIBANA_URL; \
	curl -X POST "$$KIBANA_URL/api/saved_objects/_import" \
		-H "kbn-xsrf: true" \
		--form "file=@kibana/gamepulse-dashboard.ndjson"

# ─── Clean ───────────────────────────────────────────────────────────────────

clean:
	cargo clean
	rm -rf $(DEB_DIR)
	rm -f *.deb *.rpm
