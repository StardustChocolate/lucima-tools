"use strict";

// Keep lossless PNG filtering and compression off the UI thread. The encoder
// is only loaded for opaque cards; true transparency uses the browser encoder.
self.onmessage = ({ data }) => {
  try {
    const { width, height, rgba } = data || {};
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1
        || width * height > 16000000 || !(rgba instanceof ArrayBuffer)
        || rgba.byteLength !== width * height * 4) {
      throw new Error("图鉴图片像素数据无效");
    }
    const pixels = new Uint8Array(rgba);
    for (let offset = 3; offset < pixels.length; offset += 4) {
      if (pixels[offset] !== 255) {
        // UPNG 2.1 can truncate high-entropy RGBA output. Preserve actual alpha
        // with the browser's original PNG instead of stripping or quantizing it.
        self.postMessage({ ok: true, png: null });
        return;
      }
    }
    // UPNG's unmodified browser build expects window even inside a worker.
    self.window = self;
    importScripts("/static/vendor/pako_deflate.min.js?v=1");
    const deflate = self.pako.deflate;
    self.pako.deflate = (bytes) => deflate(bytes, { level: 9 });
    importScripts("/static/vendor/UPNG.js?v=1");
    // Zero palette size disables quantization; forbidPlte keeps 8-bit RGB.
    const png = self.UPNG.encode([rgba], width, height, 0, undefined, true);
    const bytes = new Uint8Array(png);
    const iend = [0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130];
    if (!iend.every((byte, index) => bytes[bytes.length - iend.length + index] === byte)) {
      self.postMessage({ ok: true, png: null });
      return;
    }
    self.postMessage({ ok: true, png }, [png]);
  } catch (error) {
    self.postMessage({ ok: false, error: error.message || "PNG 无损压缩失败" });
  }
};
