# TK-5 — Installer hardening (turnkey-readiness slice, final task)

Context: Sol pilot-readiness finding F9 (2026-07-22): the real install path has
false-success and cleanup hazards. TK-0 contract: tester default is `--no-ebpf`; eBPF
stays opt-in. No network/docker in your sandbox — verification is static + the test
deliverable below; the orchestrator runs live checks.

## Findings to fix (verify each against the current code first)

1. **`packaging/rigsignal-launcher.sh` `cmd_setup` false-success**: the ES connectivity
   check treats ANY HTTP response (including 401/403) as success (~line 83). Fix:
   - assert HTTP status is 2xx;
   - verify the supplied API key actually AUTHENTICATES (GET `/_security/_authenticate`)
     and has WRITE privilege for the rigsignal data streams (POST
     `/_security/user/_has_privileges` with index privileges `create_doc` on
     `metrics-rigsignal.*` and `logs-rigsignal.*`) — fail setup with a clear message
     naming the missing privilege otherwise;
   - never print the API key; on failure print status + a sanitized one-line hint.
2. **Version gate inconsistency**: launcher enforces `8.13+` (~line 203) while docs now
   publish tested range 9.4.3–9.4.4. Change the check to: hard-fail below 8.13 (unchanged
   floor for TSDS-era APIs), WARN (not fail) when outside the tested 9.4.3–9.4.4 range
   with a pointer to docs/install.md. Single source the range in two variables at the top.
3. **`packaging/install.sh` checksum verification**: it downloads the release tarball
   without verifying a checksum. The release workflow already publishes per-asset
   `.sha256` files alongside artifacts (verify in `.github/workflows/release.yml`; if it
   does NOT, add the checksum-file step there too). install.sh must download the
   `.sha256`, verify with `sha256sum -c` before unpacking, and abort loudly on mismatch.
4. **Uninstaller not shipped**: the repo has an uninstall script (locate it — check
   `packaging/`); install.sh must install it alongside the binary (e.g.
   `/usr/local/bin/rigsignal-uninstall` or equivalent documented path) and the final
   install.sh output must mention it. If no uninstall script exists, write one:
   stop+disable unit, remove binary/launcher/unit/config (config removal behind a
   `--purge` flag), print what remains (data in ES is never touched).
5. **eBPF default**: install.sh / launcher must default new installs to `--no-ebpf`
   (agent config), with eBPF enablement documented as an explicit opt-in step. Do not
   change the behavior of existing installed configs.

## Deliverables

- Fixed `packaging/rigsignal-launcher.sh`, `packaging/install.sh`, (possibly)
  `.github/workflows/release.yml`, uninstall script.
- **`tools/tests/test_installer.sh`** — root-free bats-style bash test script covering:
  (a) setup fails on 401 (mock ES via a python3 http.server one-liner started by the
  test on a random port — this runs on the orchestrator's box, localhost only);
  (b) setup fails when `_has_privileges` reports missing create_doc;
  (c) setup succeeds against the mock happy path;
  (d) install.sh checksum mismatch aborts before unpack (use a local file:// or pre-seeded
  download dir — do NOT hit the network; add a hidden `RIGSIGNAL_INSTALL_LOCAL_DIR` env
  override to install.sh if needed for testability, documented as test-only);
  (e) uninstall script removes what install laid down (staged under a temp DESTDIR —
  add DESTDIR support to install.sh if absent; document it).
  Every test prints `TEST PASS <name>` / `TEST FAIL <name>`; nonzero exit on any fail.
- `docs/install.md`: checksum-verify step shown in the manual path too; uninstall section;
  eBPF opt-in note.

## Acceptance criteria (binary)

- AC1: bash -n + shellcheck clean on all changed/new shell files.
- AC2: `bash tools/tests/test_installer.sh` — all tests pass on a plain Linux box without
  root and without internet (the orchestrator will run it).
- AC3: no credential value is ever echoed/logged by launcher or installer (grep-provable).
- AC4: existing launcher subcommands unrelated to setup are behaviorally unchanged (diff
  review; no refactors).
- AC5: final summary lists each of the 5 findings with fixed/not-applicable + evidence.

## Constraints

- Changed files only: `packaging/**`, `.github/workflows/release.yml` (only if checksum
  step missing), `tools/tests/test_installer.sh`, `docs/install.md`. No src/ changes.
- Commit on this branch (codex/tk5-installer). Final message: condensed summary only.
