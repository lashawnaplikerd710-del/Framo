# Native draft codec attribution

`scripts/vendor/jianying_crypto_bridge.py` is adapted from the Apache-2.0
`draft_crypto.py` implementation in aoguai/pyJianYingDraft:

https://github.com/aoguai/pyJianYingDraft/blob/4a7730c9a14e91aa497e723c85b5c433a62a163c/pyJianYingDraft/draft_crypto.py

Upstream project: Copyright 2024 Gary Guan and contributors. The upstream module
states that it mirrors the relevant interface portions of `jy-draftc`.
This adaptation retains that provenance. The corresponding Apache-2.0 license
is included as `LICENSE-pyJianYingDraft.txt`.

Changes made on 2026-09-20: retain only the local native ABI bridge; remove
environment-based installation discovery, generic persistence, public CLI,
and optional roundtrip bypasses; require a mandatory encode/decode byte
roundtrip. The surrounding `native_draft_codec.py` adds exact build fingerprint
and Authenticode verification, hidden isolated execution, explicit single-file
copy destinations, JSON validation, and reports with narrowly stated scope.

No Jianying DLL, paid resource, private key or account credential is included.
The installed proprietary DLL and all editor/service capabilities remain under
their respective owners' terms. This bridge is not an official Jianying SDK.

The local media-bin metadata schema in native_build.py follows the
sync_draft_materials implementation in aoguai/pyJianYingDraft's
draft_registration.py (Apache-2.0): type=0 local bucket, file_Path, metetype,
source duration and roughcut/sub-time ranges. The implementation here is
limited to deduplicated local sources in the explicitly selected native draft
and measures source metadata with MediaInfo. It does not import user libraries.
