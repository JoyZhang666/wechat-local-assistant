# Third-party provenance

Source: https://github.com/freanocank/wechat-decrypt
Commit: `f0b3f9f95dda2d954ee3dbe1d139fb1944a5ecfd` (2026-10-14).
Copyright (c) 2026 cocofeng. MIT; see LICENSE in this directory.

- `src/wechat_local/wal_snapshot.py`: scripts/common/wal_snapshot.py, blob
  `1780b0a7378c25e3f6ac59de99addd6fb9c15f9f`; retained committed WAL logic.
- `src/wechat_local/database_crypto.py`: adapted authenticated page algorithm
  from scripts/windows/decrypt_all.py, blob
  `f6e8e6634083718f73769971334a545bb6e0d7ee`.
  Uses existing PyCryptodome, raises on invalid pages, accepts an already stable
  snapshot and atomically publishes output. CLI, plaintext key cache, account
  discovery and full-account mirror behavior were excluded.

Authorization, DPAPI storage, account-scoped provider and integration are
independently authored for this project. No upstream native binaries bundled.
