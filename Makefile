.PHONY: build release test clean install install-user fmt lint

VERSION := $(shell grep '^version' src/Cargo.toml | head -1 | sed 's/.*"\(.*\)"/\1/')
BINARY := target/release/rigsignal-agent

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
	@echo "Static binary: target/x86_64-unknown-linux-musl/release/rigsignal-agent"

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
	sudo install -Dm755 $(BINARY) /usr/local/bin/rigsignal-agent
	sudo mkdir -p /etc/rigsignal
	@if [ ! -f /etc/rigsignal/rigsignal.toml ]; then \
		sudo cp config/rigsignal.toml /etc/rigsignal/rigsignal.toml; \
	fi
	sudo install -Dm644 packaging/systemd/rigsignal-agent.service /etc/systemd/system/
	sudo systemctl daemon-reload
	@echo ""
	@echo "Installed. Configure /etc/rigsignal/rigsignal.toml then:"
	@echo "  sudo systemctl enable --now rigsignal-agent"

install-user: release
	mkdir -p ~/.local/bin ~/.config/rigsignal ~/.config/systemd/user
	cp $(BINARY) ~/.local/bin/rigsignal-agent
	@if [ ! -f ~/.config/rigsignal/rigsignal.toml ]; then \
		cp config/rigsignal.toml ~/.config/rigsignal/rigsignal.toml; \
	fi
	cp packaging/systemd/rigsignal-agent.user-install.service ~/.config/systemd/user/rigsignal-agent.service
	sed -i "s|%h|$$HOME|g" ~/.config/systemd/user/rigsignal-agent.service
	systemctl --user daemon-reload
	@echo ""
	@echo "Installed for current user. Configure ~/.config/rigsignal/rigsignal.toml then:"
	@echo "  systemctl --user enable --now rigsignal-agent"

# ─── Clean ───────────────────────────────────────────────────────────────────

clean:
	cargo clean
	rm -f *.deb *.rpm
