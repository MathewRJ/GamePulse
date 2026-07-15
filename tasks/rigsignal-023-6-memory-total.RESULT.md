# rigsignal-023-6-memory-total RESULT

Status: complete.

Change:
- Linux memory collector now emits `total_mb` in the per-tick memory payload, sourced from `MemTotal` in the same `/proc/meminfo` read as the existing memory fields.
- Added a Linux memory collector unit test asserting `rigsignal.memory.total_mb` is present, greater than 0, and equals `MemTotal / 1024`.

Full emitted field path:
- `rigsignal.memory.total_mb`
- The collector returns `{ "rigsignal": { "memory": ... } }`; the main loop deep-merges that payload into each tick doc and sets `data_stream.dataset = "rigsignal.memory"`.

Conversion convention:
- `/proc/meminfo` values are kB.
- The collector uses integer division by `1024` for MB, matching existing `system_used_mb`, `page_cache_mb`, `shared_mb`, and `swap_used_mb` conversion.

Integration package fields:
- No `packages/rigsignal` directory, `fields.yml`, or `*fields*.yml`/`*fields*.yaml` files are present in this repo, so no package fields file was updated.

Verification:
- `cargo test` passed: 50 tests, including `collectors::linux::memory::tests::collect_emits_total_mb_from_memtotal`.
- `cargo check` passed.
- `cargo fmt --check` passed.

Deviations:
- None.
