# TK-2 follow-up — regression tests for asset bundle tools

Reviewer note (2026-07-22, TK-2 gate): build_asset_bundle.py's dependency-resolution
asserts and install_assets.py's no-skip re-read verification are exactly the logic that
regresses silently. Reviewer proved them by induced-failure testing manually; encode those
as pytest cases: (1) missing referenced pipeline fails build, (2) missing composed_of
component fails build, (3) non-conforming filename fails build, (4) installer failure table
+ nonzero exit on any asset error, (5) transform update path strips pivot.
Priority: ride along with the next tools/ change or TK-3 dispatch.
