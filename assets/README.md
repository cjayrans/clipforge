# Bundled assets (owned by render, Phase 1 C)

Committed so renders are reproducible offline. Fetched 2026-11-22.

| File | Licence | Source | SHA-256 |
| --- | --- | --- | --- |
| `fonts/Montserrat-ExtraBold.ttf` | SIL OFL 1.1 (`fonts/Montserrat-OFL.txt`) | https://github.com/JulietaUla/Montserrat (`fonts/ttf/`, branch master) | `d3ac6a843d3ba6d5cafd44cf39e437055c8aed7e261010f595f57d3c7b3e2c1b` |
| `fonts/Inter-Bold.ttf` | SIL OFL 1.1 (`fonts/Inter-OFL.txt`) | https://github.com/rsms/inter/releases/tag/v4.1 (`Inter-4.1.zip`, `extras/ttf/`) | `288316099b1e0a47a4716d159098005eef7c0066921f34e3200393dbdb01947f` |
| `fonts/Inter-Black.ttf` | SIL OFL 1.1 (`fonts/Inter-OFL.txt`) | same release | `6342d3ea6dc088b43867f615e807d898adf100c93edb978b8e52c5eb71a264da` |
| `models/blaze_face_short_range.tflite` | Apache-2.0 | https://huggingface.co/litert-community/MediaPipe-FaceDetector-BlazeFace-Short-Range (linked from the MediaPipe Face Detector docs; the model card says it is byte-identical to Google's storage.googleapis.com artifact) | `b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f` (matches the model card) |

Config `styles.*.font` values are PostScript names; `clipforge.render.assets.missing_fonts` checks them against
these files (tested in `tests/render/test_overlays.py`). Static TTFs are used on purpose: libass picks only the
default instance of a variable font.

The Docker image must copy this folder (`COPY assets ./assets`); see `docs/contract-requests/render.md`.
