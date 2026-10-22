"use strict";

// Export an independent static card at its natural size, outside the viewer's
// zoom/scroll container. The small DOM conversion library is loaded on demand.
(() => {
  let libraryPromise = null;
  const XHTML = "http://www.w3.org/1999/xhtml";
  const nextFrame = () => new Promise((resolve) => requestAnimationFrame(resolve));

  function loadLibrary() {
    if (window.htmlToImage) return Promise.resolve(window.htmlToImage);
    if (libraryPromise) return libraryPromise;
    libraryPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      const timer = setTimeout(() => finish(new Error("图片生成组件加载超时")), 15000);
      function finish(error) {
        clearTimeout(timer);
        script.onload = script.onerror = null;
        if (error) {
          script.remove();
          reject(error);
        } else resolve(window.htmlToImage);
      }
      script.src = "/static/vendor/html-to-image.js?v=1";
      script.onload = () => finish(window.htmlToImage ? null : new Error("图片生成组件不可用"));
      script.onerror = () => finish(new Error("图片生成组件加载失败"));
      document.head.appendChild(script);
    }).catch((error) => {
      libraryPromise = null;
      throw error;
    });
    return libraryPromise;
  }

  function cardStyles() {
    const sheet = [...document.styleSheets].find((item) => item.href
      && new URL(item.href).pathname.endsWith("/encyclopedia-card.css"));
    if (!sheet) throw new Error("图鉴样式尚未加载");
    return [...sheet.cssRules].map((rule) => rule.cssText).join("\n");
  }

  function freezeEffects(card) {
    for (const node of card.querySelectorAll(".badge-ring, .starfield__star")) {
      const style = getComputedStyle(node);
      const transform = style.transform;
      const opacity = style.opacity;
      node.style.animation = "none";
      node.style.transform = transform;
      node.style.opacity = opacity;
    }
    // SVG pauseAnimations() is runtime state and is lost on serialization.
    // Bake the paused motion into a local transform before removing SMIL.
    for (const motion of card.querySelectorAll("animateMotion")) {
      const node = motion.parentElement;
      const own = node.getCTM();
      const parent = node.parentElement.getCTM?.();
      const matrix = own && parent ? parent.inverse().multiply(own) : own;
      motion.remove();
      if (matrix) node.style.transform = `matrix(${matrix.a},${matrix.b},${matrix.c},${matrix.d},${matrix.e},${matrix.f})`;
    }
  }

  function prepareSvg(uri, css) {
    const svg = new DOMParser().parseFromString(decodeURIComponent(uri.slice(uri.indexOf(",") + 1)), "image/svg+xml");
    if (svg.querySelector("parsererror")) throw new Error("图鉴图片转换失败");
    const style = svg.createElementNS(XHTML, "style");
    // html-to-image deep-clones SVG without copying its descendants' computed
    // styles. Include the original card CSS to retain paths, masks and glows.
    style.textContent = `${css}\n* { animation: none !important; transition: none !important; }`;
    svg.querySelector("foreignObject").prepend(style);
    const ids = new Set([...svg.querySelectorAll("[id]")].map((node) => node.id));
    for (const node of svg.querySelectorAll("*")) {
      for (const attr of [...node.attributes]) {
        const value = attr.value.replace(/url\((["']?)([^)]+?)\1\)/g, (match, quote, url) => {
          const hash = url.indexOf("#");
          const id = hash >= 0 ? url.slice(hash + 1) : "";
          return ids.has(id) ? `url(#${id})` : match;
        });
        if (value !== attr.value) node.setAttributeNS(attr.namespaceURI, attr.name, value);
      }
    }
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(svg))}`;
  }

  async function encodePng(canvas, context) {
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
    const worker = new Worker("/static/encyclopedia-png-worker.js?v=1");
    let timer;
    try {
      const png = await new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(new Error("PNG 无损压缩超时")), 60000);
        worker.onmessage = ({ data }) => {
          if (!data?.ok) reject(new Error(data?.error || "PNG 无损压缩失败"));
          else if (data.png === null || data.png instanceof ArrayBuffer) resolve(data.png);
          else reject(new Error("PNG 无损压缩结果无效"));
        };
        worker.onerror = () => reject(new Error("PNG 无损压缩组件加载失败"));
        worker.onmessageerror = () => reject(new Error("PNG 无损压缩结果读取失败"));
        worker.postMessage({ width: canvas.width, height: canvas.height, rgba: pixels.data.buffer }, [pixels.data.buffer]);
      });
      clearTimeout(timer);
      if (png === null) return canvas.toDataURL("image/png");
      return await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("PNG 图片读取失败"));
        reader.readAsDataURL(new Blob([png], { type: "image/png" }));
      });
    } finally {
      clearTimeout(timer);
      worker.terminate();
    }
  }

  async function render({ entry, data, translations = {}, config = {}, fetchResource }) {
    const library = await loadLibrary();
    const css = cardStyles();
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.inert = true;
    host.style.cssText = "position:fixed;left:-20000px;top:0;width:1000px;pointer-events:none;";
    document.body.appendChild(host);
    let renderer = null;
    let media = null;
    let canvas = null;
    try {
      renderer = window.LucimaEncyclopediaCard.mount({ host, data, translations, staticMode: true });
      // Reuse the app's persistent resource cache. A missing or timed-out
      // portrait/icon remains empty, just as it does in the on-screen card.
      const boundedFetch = async (url) => {
        let timer;
        try {
          return await Promise.race([
            fetchResource(url),
            new Promise((resolve, reject) => {
              timer = setTimeout(() => reject(new Error("资源加载超时")), 25000);
            }),
          ]);
        } finally { clearTimeout(timer); }
      };
      media = window.LucimaEncyclopediaMedia.mount({
        renderer, entry, data, config, fetchResource: boundedFetch,
      });
      await Promise.all([media.ready, document.fonts?.ready]);
      await Promise.all([...renderer.card.querySelectorAll("img[src]")].map((image) => image.decode().catch(() => {
        image.removeAttribute("src");
        image.style.visibility = "hidden";
      })));
      await nextFrame();
      await nextFrame();
      const card = renderer.card;
      renderer.setStaticMode(true);
      freezeEffects(card);
      for (const link of card.querySelectorAll("a.character-auto-cast__credit-value")) {
        link.style.textDecoration = "none";
      }
      for (const node of [card, ...card.querySelectorAll("*")]) {
        if (node.namespaceURI === XHTML) node.style.fontSize = getComputedStyle(node).fontSize;
      }
      const width = card.offsetWidth;
      const height = card.offsetHeight;
      // Use the natural card size while bounding canvas allocation on mobile.
      const ratio = Math.min(1, 16384 / width, 16384 / height, Math.sqrt(16000000 / (width * height)));
      const uri = await library.toSvg(card, {
        width, height, skipFonts: true,
        // Preserve fractional font sizes; the library otherwise rounds them.
        includeStyleProperties: [...getComputedStyle(card)].filter((key) => key !== "font-size"),
        filter: (node) => node.nodeName !== "CANVAS"
          && !(node instanceof HTMLImageElement && !node.getAttribute("src")),
      });
      const image = new Image();
      image.src = prepareSvg(uri, css);
      await image.decode();
      canvas = document.createElement("canvas");
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      const context = canvas.getContext("2d");
      if (!context) throw new Error("无法创建图鉴图片画布");
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const png = await encodePng(canvas, context);
      if (!png.startsWith("data:image/png;base64,")) throw new Error("图鉴图片生成失败");
      const pngBase64 = png.slice(png.indexOf(",") + 1);
      if (pngBase64.length > Math.ceil(32 * 1024 * 1024 / 3) * 4) throw new Error("图鉴图片超过 32 MB，无法保存");
      return { pngBase64, width: canvas.width, height: canvas.height };
    } finally {
      media?.dispose();
      renderer?.dispose();
      host.remove();
      if (canvas) canvas.width = canvas.height = 0;
    }
  }

  window.LucimaEncyclopediaExport = Object.freeze({ render });
})();
