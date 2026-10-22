# html-to-image

`html-to-image.js` is the unmodified UMD build of html-to-image 1.11.13 (MIT).
License: `html-to-image.LICENSE`.
Source: https://github.com/bubkoo/html-to-image/tree/v1.11.13

Loaded locally on the first encyclopedia image export. No network request is needed.

# Lossless PNG Export

`UPNG.js` is the unmodified browser build of upng-js 2.1.0 (MIT).
License: `UPNG.LICENSE`.
Source: https://github.com/photopea/UPNG.js

`pako_deflate.min.js` is the unmodified deflate-only UMD build of pako 2.1.0
(MIT AND Zlib). Licenses: `pako.LICENSE` and `pako-zlib.LICENSE`.
Source: https://github.com/nodeca/pako/tree/2.1.0

The encyclopedia PNG worker loads both scripts locally on demand, sets zlib
level 9, and disables quantization and palette encoding. Fully opaque cards
use 8-bit RGB; transparent output uses the browser PNG encoder to avoid an
UPNG 2.1 allocation issue with high-entropy RGBA images.
