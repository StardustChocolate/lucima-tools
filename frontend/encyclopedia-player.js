/* Ark2 portrait playback on the canvas owned by the mounted dossier card. */
(() => {
  "use strict";

  function bytesToDataUri(bytes, mime) {
    let binary = "";
    for (let index = 0; index < bytes.length; index += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
    }
    return `data:${mime};base64,${btoa(binary)}`;
  }

  function atlasPageNames(text) {
    const lines = String(text).split(/\r?\n/);
    const pages = new Set();
    for (let index = 0; index + 1 < lines.length; index += 1) {
      const name = lines[index].trim();
      if (name && !name.startsWith("#") && /^size:\s*/i.test(lines[index + 1])) pages.add(name);
    }
    return [...pages];
  }

  class EncyclopediaSpinePlayer {
    constructor({ host, canvas, config, fetchResource } = {}) {
      if (!host || !canvas || !config || typeof fetchResource !== "function" || !host.contains(canvas)) {
        throw new TypeError("Spine portrait requires a host, its canvas, config, and fetchResource");
      }
      this.host = host;
      this.canvas = canvas;
      this.config = config;
      this.fetchResource = fetchResource;
      this.disposed = false;
      this.loadingAssets = false;
      this.cleanedUp = false;
      this.running = false;
      this.visible = true;
      this.frameRequest = 0;
      this.lastFrameTime = 0;
      this.motionQuery = window.matchMedia?.("(prefers-reduced-motion: reduce)") || null;
      this.reducedMotion = Boolean(this.motionQuery?.matches);
      this.onMotionChange = (event) => {
        this.reducedMotion = event.matches;
        if (this.reducedMotion) this.stopFrame();
        else if (this.running && this.visible && !this.frameRequest && !this.disposed) {
          this.lastFrameTime = 0;
          this.frameRequest = requestAnimationFrame((time) => this.draw(time));
        }
      };
    }

    assertActive() {
      if (this.disposed) throw new Error("Portrait load cancelled");
    }

    initialize() {
      if (!this.initializePromise) {
        this.initializePromise = this.load().catch((error) => {
          this.dispose();
          throw error;
        });
      }
      return this.initializePromise;
    }

    async fetchChecked(url) {
      this.assertActive();
      const response = await this.fetchResource(url);
      this.assertActive();
      if (!response?.ok) throw new Error(`Portrait resource unavailable: ${url}`);
      return response;
    }

    async load() {
      this.assertActive();
      const runtime = window.spine;
      if (!runtime) throw new Error("Spine WebGL is unavailable");
      const framing = window.EncyclopediaPortraitFraming;
      if (!framing) throw new Error("Portrait framing is unavailable");
      const base = String(this.config.base || "");
      if (!/^H\d+$/.test(base) || (this.config.id && this.config.id !== base)) {
        throw new Error("Portrait resources must use an exact Hxxx base name");
      }
      if (!this.config.root) throw new Error("Portrait resource root is missing");
      const root = new URL(String(this.config.root).replace(/\/?$/, "/"), location.href);
      if (!/^https?:$/.test(root.protocol)) throw new Error("Invalid portrait resource URL");
      const atlasUrl = new URL(`${base}.atlas`, root).href;
      const skeletonUrl = new URL(`${base}.skel`, root).href;
      const [atlasResponse, skeletonResponse] = await Promise.all([
        this.fetchChecked(atlasUrl), this.fetchChecked(skeletonUrl),
      ]);
      const [atlasText, skeletonBuffer] = await Promise.all([
        atlasResponse.text(), skeletonResponse.arrayBuffer(),
      ]);
      this.assertActive();
      const pages = atlasPageNames(atlasText);
      if (!pages.length) throw new Error("Portrait atlas has no texture pages");
      const textures = await Promise.all(pages.map(async (name) => {
        const pageUrl = new URL(name, atlasUrl);
        if (pageUrl.origin !== root.origin || !pageUrl.pathname.startsWith(root.pathname)) {
          throw new Error("Portrait atlas page escapes its resource directory");
        }
        const response = await this.fetchChecked(pageUrl.href);
        const bytes = new Uint8Array(await response.arrayBuffer());
        this.assertActive();
        return [name, bytesToDataUri(bytes, response.headers.get("content-type") || "image/png")];
      }));
      this.assertActive();

      const gl = this.canvas.getContext("webgl", {
        alpha: true, antialias: true, premultipliedAlpha: true, powerPreference: "high-performance",
      });
      if (!gl) throw new Error("Spine WebGL is unavailable");
      this.gl = gl;
      const assets = new runtime.AssetManager(gl, root.href);
      this.assets = assets;
      // Spine's data-URI text reader uses atob without decoding UTF-8. Give
      // this manager the decoded atlas text so non-ASCII region names still
      // match the skeleton, while keeping all downloads in our media cache.
      const downloadText = assets.downloader.downloadText.bind(assets.downloader);
      assets.downloader.downloadText = (url, success, error) => {
        if (url === atlasUrl) success(atlasText);
        else downloadText(url, success, error);
      };
      assets.setRawDataURI(`${base}.skel`, bytesToDataUri(new Uint8Array(skeletonBuffer), "application/octet-stream"));
      textures.forEach(([name, dataUri]) => assets.setRawDataURI(name, dataUri));
      this.loadingAssets = true;
      try {
        assets.loadBinary(`${base}.skel`);
        assets.loadTextureAtlas(`${base}.atlas`);
        await this.waitForAssets();
      } finally {
        this.loadingAssets = false;
        assets.downloader.downloadText = downloadText;
        if (assets.downloader?.rawDataUris) assets.downloader.rawDataUris = {};
        if (this.disposed) this.cleanup();
      }
      this.assertActive();

      const atlas = assets.require(`${base}.atlas`);
      const loader = new runtime.AtlasAttachmentLoader(atlas);
      const binary = new runtime.SkeletonBinary(loader);
      this.skeletonData = binary.readSkeletonData(assets.require(`${base}.skel`));
      const animationName = String(this.config.animation || "00_Idle");
      const animation = this.skeletonData.findAnimation(animationName);
      if (!animation) throw new Error(`Animation not found: ${animationName}`);
      const skin = String(this.config.skin || "LV1");
      this.skeleton = framing.createSkeleton(runtime, this.skeletonData, skin);
      this.state = new runtime.AnimationState(new runtime.AnimationStateData(this.skeletonData));
      this.state.setAnimation(0, animationName, true);
      this.bounds = framing.measureAnimationBounds(runtime, this.skeletonData, {
        skin, animation, assertActive: () => this.assertActive(),
      });
      this.assertActive();
      this.renderer = new runtime.SceneRenderer(this.canvas, gl);
      this.resizeObserver = typeof ResizeObserver === "function"
        ? new ResizeObserver(() => this.resize()) : null;
      this.resizeObserver?.observe(this.host);
      this.onResize = () => this.resize();
      window.addEventListener("resize", this.onResize);
      this.observeVisibility();
      this.motionQuery?.addEventListener?.("change", this.onMotionChange);
      if (this.motionQuery && !this.motionQuery.addEventListener) this.motionQuery.addListener?.(this.onMotionChange);
      this.resize();
      this.assertActive();
      this.canvas.hidden = false;
      this.running = true;
      // Draw the static pose without starting the clock. RAF timestamps mark
      // the start of a frame and can precede performance.now() during loading.
      this.draw(0);
      this.assertActive();
      this.host.dataset.state = "ready";
      return this;
    }

    waitForAssets() {
      return new Promise((resolve, reject) => {
        const timeoutAt = performance.now() + 30000;
        const check = () => {
          if (this.assets.isLoadingComplete()) {
            if (this.disposed) reject(new Error("Portrait load cancelled"));
            else if (this.assets.hasErrors?.()) {
              reject(new Error(`Spine resource load failed: ${JSON.stringify(this.assets.getErrors?.() || {})}`));
            } else resolve();
          } else if (performance.now() >= timeoutAt) {
            reject(new Error("Timed out while loading Spine portrait"));
          } else {
            this.assetPollTimer = window.setTimeout(check, 30);
          }
        };
        check();
      });
    }

    observeVisibility() {
      if (typeof IntersectionObserver !== "function") return;
      this.intersectionObserver = new IntersectionObserver(([entry]) => {
        this.visible = entry.isIntersecting;
        if (!this.visible) this.stopFrame();
        else if (this.running && !this.reducedMotion && !this.frameRequest && !this.disposed) {
          this.lastFrameTime = 0;
          this.frameRequest = requestAnimationFrame((time) => this.draw(time));
        }
      });
      this.intersectionObserver.observe(this.host);
    }

    resize() {
      if (!this.gl || !this.renderer || !this.bounds || this.disposed) return;
      // Layout dimensions remain 400 x 520 even when the archive is CSS-zoomed.
      const width = Math.max(1, this.host.clientWidth);
      const height = Math.max(1, this.host.clientHeight);
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      const outputWidth = Math.round(width * pixelRatio);
      const outputHeight = Math.round(height * pixelRatio);
      const bufferChanged = this.canvas.width !== outputWidth || this.canvas.height !== outputHeight;
      if (bufferChanged) {
        this.canvas.width = outputWidth;
        this.canvas.height = outputHeight;
      }
      const viewport = window.EncyclopediaPortraitFraming.fitViewport(this.bounds, outputWidth, outputHeight);
      this.gl.viewport(0, 0, outputWidth, outputHeight);
      this.renderer.camera.position.x = viewport.centerX;
      this.renderer.camera.position.y = viewport.centerY;
      this.renderer.camera.setViewport(viewport.viewportWidth, viewport.viewportHeight);
      if (bufferChanged && this.running && this.reducedMotion) {
        this.draw(this.lastFrameTime || performance.now(), true);
      }
    }

    draw(time, force = false) {
      this.frameRequest = 0;
      if (!this.running || (!this.visible && !force) || this.disposed) return;
      const delta = this.lastFrameTime
        ? Math.max(0, Math.min((time - this.lastFrameTime) / 1000, 0.05)) : 0;
      this.lastFrameTime = Math.max(this.lastFrameTime, time);
      this.state.update(delta);
      this.state.apply(this.skeleton);
      this.skeleton.updateWorldTransform();
      this.gl.clearColor(0, 0, 0, 0);
      this.gl.clear(this.gl.COLOR_BUFFER_BIT);
      this.renderer.begin();
      this.renderer.drawSkeleton(this.skeleton, true);
      this.renderer.end();
      if (!this.reducedMotion) this.frameRequest = requestAnimationFrame((next) => this.draw(next));
    }

    stopFrame() {
      if (this.frameRequest) cancelAnimationFrame(this.frameRequest);
      this.frameRequest = 0;
      this.lastFrameTime = 0;
    }

    cleanup() {
      if (this.cleanedUp) return;
      this.cleanedUp = true;
      if (this.assetPollTimer) window.clearTimeout(this.assetPollTimer);
      this.resizeObserver?.disconnect();
      this.intersectionObserver?.disconnect();
      if (this.onResize) window.removeEventListener("resize", this.onResize);
      this.motionQuery?.removeEventListener?.("change", this.onMotionChange);
      if (this.motionQuery && !this.motionQuery.removeEventListener) this.motionQuery.removeListener?.(this.onMotionChange);
      this.stopFrame();
      this.renderer?.dispose?.();
      this.assets?.dispose?.();
      this.renderer = null;
      this.assets = null;
      this.gl = null;
    }

    dispose() {
      if (this.disposed) return;
      this.disposed = true;
      this.running = false;
      this.canvas.hidden = true;
      this.stopFrame();
      if (!this.loadingAssets) this.cleanup();
    }
  }

  window.createEncyclopediaSpinePlayer = (options) => new EncyclopediaSpinePlayer(options);
})();
