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

- `src/wechat_local/key_hook.py`: JS hook literal from
  scripts/windows/extract_raw_key.py at the same fixed commit, blob
  `8a813f8fb9e7b393710b07769674181483e4795c`. Preserved MIT copyright above.
  Upstream process killing, account discovery, plaintext output and setup script
  are excluded. The independently authored adapter requires a separate, explicit
  local access action and preserves existing authorization on failure.

Frida is an optional, separately installed runtime dependency, not MIT-relicensed
by this project. Frida Python/core use wxWindows Library Licence 3.1; retain its
distributed notices, including Gum's third-party BSD notices. Official sources:
https://github.com/frida/frida-python and https://github.com/frida/frida-core.
