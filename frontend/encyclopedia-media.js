"use strict";

// Fill Ark2's original media slots without delaying the text card. Large files
// are fetched only for the open character; the app supplies a persistent cache.
(() => {
  function resourceUrl(config, path) {
    if (!path || !config.baseUrl) return "";
    try {
      if (config.schema === 2) {
        const file = config.files?.[path];
        const extension = String(path).match(/\.(png|jpg|jpeg|webp|skel|atlas|js)$/i)?.[0].toLowerCase();
        if (!file || !/^[a-f0-9]{64}$/.test(file.sha256) || !extension) return "";
        path = `objects/${file.sha256}${extension}`;
      }
      const url = new URL(path, config.baseUrl);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch { return ""; }
  }

  function mount({ renderer, entry, data, config = {}, fetchResource, loadRuntime }) {
    const card = renderer.card;
    const host = card.querySelector(".portrait-layer");
    const canvas = card.querySelector(".portrait-canvas");
    const image = card.querySelector(".portrait-fallback");
    const objectUrls = new Set();
    let disposed = false;
    let generation = 0;
    let player = null;
    let staticReady = false;
    let mode = "static";

    function clearImage(node) {
      node.removeAttribute("src");
      node.style.visibility = "hidden";
    }

    async function fillImage(node, url, alt) {
      if (!url || disposed) return false;
      let objectUrl = "";
      try {
        const response = await fetchResource(url);
        if (disposed) return false;
        objectUrl = URL.createObjectURL(await response.blob());
        objectUrls.add(objectUrl);
        // Decode before exposing the slot so corrupt files never show a broken
        // image glyph, and no completed request can fill a disposed card.
        const decoded = new Image();
        decoded.src = objectUrl;
        await decoded.decode();
        if (disposed) return false;
        node.src = objectUrl;
        node.alt = alt;
        node.hidden = false;
        node.style.visibility = "visible";
        return true;
      } catch {
        if (!disposed) clearImage(node);
        if (objectUrl) {
          URL.revokeObjectURL(objectUrl);
          objectUrls.delete(objectUrl);
        }
        return false;
      } finally {
        if (disposed && objectUrl) {
          URL.revokeObjectURL(objectUrl);
          objectUrls.delete(objectUrl);
        }
      }
    }

    function showFallback() {
      canvas.style.visibility = "hidden";
      host.dataset.state = staticReady ? "fallback" : "unavailable";
      host.setAttribute("aria-hidden", String(!staticReady));
    }

    clearImage(image);
    canvas.style.visibility = "hidden";
    showFallback();
    const staticPath = `${String(config.staticPortraitBaseUrl || "portraits/static/").replace(/\/?$/, "/")}${entry.id}.png`;
    const staticLoad = entry.hasStaticPortrait === false
      ? Promise.resolve(false)
      : fillImage(image, resourceUrl(config, staticPath), `${data.NAME || entry.name || entry.id}立绘`);
    staticLoad.then((ready) => {
      if (disposed) return;
      staticReady = ready;
      if (mode === "static" || host.dataset.state !== "ready") showFallback();
    });

    const imageLoads = [staticLoad];
    for (const icon of card.querySelectorAll(".character-skill__icon")) {
      clearImage(icon);
      const sprite = icon.dataset.iconSprite || "";
      if (!/^Icon_Skill_[A-Za-z0-9_]+$/.test(sprite)) continue;
      const path = `${String(config.skillIconBaseUrl || "skills/").replace(/\/?$/, "/")}${sprite}.png`;
      imageLoads.push(fillImage(icon, resourceUrl(config, path), "技能图标"));
    }

    async function setMode(nextMode) {
      mode = nextMode === "dynamic" ? "dynamic" : "static";
      const request = ++generation;
      player?.dispose();
      player = null;
      showFallback();
      if (disposed || mode !== "dynamic" || !entry.hasSpine || !config.dynamicBaseUrl) return;
      const spine = data.assets?.spine;
      if (!spine?.Base || !config.spineRuntimeUrl) return;
      let pending = null;
      try {
        await loadRuntime();
        if (disposed || request !== generation) return;
        const directory = String(config.dynamicBaseUrl).replaceAll("{id}", entry.id);
        const path = String(config.dynamicBaseUrl).includes("{id}")
          ? directory : `${directory.replace(/\/?$/, "/")}${entry.id}/`;
        const root = new URL(path, config.baseUrl).href;
        if (!root || typeof window.createEncyclopediaSpinePlayer !== "function") return;
        // The player keeps the atlas's original relative names. Only its
        // download boundary translates those virtual paths into pool objects.
        const fetchPortrait = config.schema === 2 ? (url) => {
          const base = new URL(config.baseUrl);
          const requested = new URL(url);
          if (requested.origin !== base.origin || !requested.pathname.startsWith(base.pathname)) {
            throw new Error("动态立绘资源路径无效");
          }
          const logical = decodeURIComponent(requested.pathname.slice(base.pathname.length));
          const resolved = resourceUrl(config, logical);
          if (!resolved) throw new Error("动态立绘资源未列入清单");
          return fetchResource(resolved);
        } : fetchResource;
        canvas.hidden = false;
        pending = window.createEncyclopediaSpinePlayer({
          host, canvas, fetchResource: fetchPortrait,
          config: { id: entry.id, root, base: spine.Base, skin: spine.Skin, animation: spine.Animation },
        });
        player = pending;
        await pending.initialize();
        if (disposed || request !== generation) {
          pending.dispose();
          return;
        }
        canvas.style.visibility = "visible";
        host.dataset.state = "ready";
        host.setAttribute("aria-hidden", "false");
      } catch {
        pending?.dispose();
        if (disposed || request !== generation) return;
        player = null;
        showFallback();
      }
    }

    return {
      ready: Promise.all(imageLoads),
      setMode,
      dispose() {
        if (disposed) return;
        disposed = true;
        generation += 1;
        player?.dispose();
        player = null;
        clearImage(image);
        canvas.style.visibility = "hidden";
        for (const icon of card.querySelectorAll(".character-skill__icon")) clearImage(icon);
        for (const url of objectUrls) URL.revokeObjectURL(url);
        objectUrls.clear();
      },
    };
  }

  window.LucimaEncyclopediaMedia = Object.freeze({ mount, resourceUrl });
})();
