# License scope

The MIT license applies to the newly authored public-source manifest only:
`src/wechat_local/`, the selected portable launcher/build scripts,
`skills/wechat-local-summary/`, public documentation/configuration examples,
and the synthetic tests included by `build_public_source.py`.

It does not relicense retained vendor code, original installation archives,
native database/decryption libraries, model weights, or private deployment
records. Those assets are not included in the public-source package.

The built-in reader includes MIT code adapted from wechat-decrypt; retain
`third_party/wechat-decrypt/LICENSE` and its provenance NOTICE. This preserves
the upstream copyright and license; it is not newly authored here.
A user-supplied external WeChat provider requires its own valid license and
local authorization. Third-party Python/model dependencies retain their respective
licenses. This source package is not a certificate of those components' safety
or of cross-machine compatibility.

Direct optional dependencies are not bundled or relicensed: psutil and
zstandard (BSD-3-Clause), Pillow (MIT-CMU), PyCryptodome (BSD/public domain),
PyJWT (MIT), cryptography (Apache-2.0 OR BSD-3-Clause), silk-python (BSD),
NumPy (its distributed multi-component license), sherpa-onnx/core (Apache-2.0).
Refer to each installed distribution's license for complete terms and notices.
