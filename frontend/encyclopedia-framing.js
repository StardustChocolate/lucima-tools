/* Shared camera geometry for Ark2's first-frame portraits and live Spine art. */
(() => {
  "use strict";

  function createSkeleton(runtime, skeletonData, skin = "LV1") {
    const skeleton = new runtime.Skeleton(skeletonData);
    if (skin && skeletonData.findSkin(skin)) skeleton.setSkinByName(skin);
    skeleton.setToSetupPose();
    skeleton.updateWorldTransform();
    return skeleton;
  }

  function measureAnimationBounds(runtime, skeletonData, {
    skin = "LV1", animation = "00_Idle", assertActive = () => {}, times = null,
  } = {}) {
    const selected = typeof animation === "string" ? skeletonData.findAnimation(animation) : animation;
    if (!selected) throw new Error(`Animation not found: ${animation}`);
    const skeleton = createSkeleton(runtime, skeletonData, skin);
    const vertices = [];
    const duration = Math.max(selected.duration, 1 / 30);
    const samples = Math.max(30, Math.ceil(duration * 30));
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;

    const sampleTimes = times || Array.from({ length: samples + 1 }, (_, i) => duration * i / samples);
    for (const time of sampleTimes) {
      assertActive();
      skeleton.setToSetupPose();
      selected.apply(
        skeleton, 0, time, true, [], 1,
        runtime.MixBlend.replace, runtime.MixDirection.mixIn,
      );
      skeleton.updateWorldTransform();
      // Whole-skeleton bounds include fully transparent attack effects.
      if (skeleton.color.a <= 0) continue;
      for (const slot of skeleton.drawOrder) {
        if (!slot.bone.active || slot.color.a <= 0) continue;
        const attachment = slot.getAttachment();
        if (!attachment?.color || attachment.color.a <= 0) continue;
        let length;
        if (attachment instanceof runtime.RegionAttachment) {
          length = 8;
          attachment.computeWorldVertices(slot, vertices, 0, 2);
        } else if (attachment instanceof runtime.MeshAttachment) {
          length = attachment.worldVerticesLength;
          attachment.computeWorldVertices(slot, 0, length, vertices, 0, 2);
        } else {
          continue;
        }
        for (let vertex = 0; vertex < length; vertex += 2) {
          minX = Math.min(minX, vertices[vertex]);
          minY = Math.min(minY, vertices[vertex + 1]);
          maxX = Math.max(maxX, vertices[vertex]);
          maxY = Math.max(maxY, vertices[vertex + 1]);
        }
      }
    }

    if (!Number.isFinite(minX) || maxX <= minX || maxY <= minY) {
      throw new Error("The portrait has no visible bounds");
    }
    return {
      centerX: (minX + maxX) / 2,
      centerY: (minY + maxY) / 2,
      width: maxX - minX,
      height: maxY - minY,
    };
  }

  function fitViewport(bounds, width, height) {
    const targetWidth = Number(width);
    const targetHeight = Number(height);
    if (!bounds || !(bounds.width > 0) || !(bounds.height > 0)
        || !(targetWidth > 0) || !(targetHeight > 0)) {
      throw new Error("Invalid portrait bounds or viewport size");
    }
    const aspect = targetWidth / targetHeight;
    let viewportWidth = bounds.width;
    let viewportHeight = bounds.height;
    if (viewportWidth / viewportHeight < aspect) viewportWidth = viewportHeight * aspect;
    else viewportHeight = viewportWidth / aspect;
    return { centerX: bounds.centerX, centerY: bounds.centerY, viewportWidth, viewportHeight };
  }

  const VISIBLE_RULE = "whole-animation-visible-rgba-60fps-v1";

  function animationSampleTimes(animation, fps = 60) {
    const duration = Math.max(animation.duration, 1 / fps);
    const samples = Math.max(fps, Math.ceil(duration * fps));
    const times = new Set(Array.from({ length: samples + 1 }, (_, i) => duration * i / samples));
    // Include attachment/alpha switches even when a key lies between samples.
    for (const timeline of animation.timelines) {
      const step = timeline.getFrameEntries();
      for (let i = 0; i < timeline.frames.length; i += step) {
        const time = timeline.frames[i];
        if (time >= 0 && time <= duration) {
          times.add(time);
          if (time > 0) times.add(Math.max(0, time - 0.000001));
        }
      }
    }
    return [...times].sort((a, b) => a - b);
  }

  function pixelBounds(pixels, width, height) {
    let left = width, bottom = height, right = -1, top = -1;
    for (let y = 0, offset = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1, offset += 4) {
        // Additive light can contribute RGB with zero alpha. readPixels sees
        // that contribution before canvas PNG unpremultiplication discards it.
        if (!(pixels[offset] | pixels[offset + 1] | pixels[offset + 2] | pixels[offset + 3])) continue;
        left = Math.min(left, x); right = Math.max(right, x);
        bottom = Math.min(bottom, y); top = Math.max(top, y);
      }
    }
    return right < left ? null : { left, bottom, right: right + 1, top: top + 1 };
  }

  function measureVisibleAnimationBounds(runtime, skeletonData, { canvas, gl, renderer,
    skin = "LV1", animation = "00_Idle", assertActive = () => {},
  }) {
    const selected = skeletonData.findAnimation(animation);
    if (!selected) throw new Error(`Animation not found: ${animation}`);
    const times = animationSampleTimes(selected);
    const coarse = measureAnimationBounds(runtime, skeletonData, { skin, animation: selected, times, assertActive });
    // Offline only. Fixed resolution makes exported cameras independent of
    // device size/DPR; the players merely load the resulting tiny sidecar.
    canvas.width = 800; canvas.height = 1040;
    const viewport = fitViewport(coarse, canvas.width, canvas.height);
    const scale = Math.max(viewport.viewportWidth / canvas.width, viewport.viewportHeight / canvas.height);
    viewport.viewportWidth += scale * 4;
    viewport.viewportHeight += scale * 4 * canvas.height / canvas.width;
    const unit = viewport.viewportWidth / canvas.width;
    renderer.camera.position.x = viewport.centerX;
    renderer.camera.position.y = viewport.centerY;
    renderer.camera.setViewport(viewport.viewportWidth, viewport.viewportHeight);
    gl.viewport(0, 0, canvas.width, canvas.height);
    const skeleton = createSkeleton(runtime, skeletonData, skin);
    const pixels = new Uint8Array(canvas.width * canvas.height * 4);
    let left = canvas.width, bottom = canvas.height, right = 0, top = 0;
    for (const time of times) {
      assertActive();
      skeleton.setToSetupPose();
      selected.apply(skeleton, 0, time, true, [], 1, runtime.MixBlend.replace, runtime.MixDirection.mixIn);
      skeleton.updateWorldTransform();
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      renderer.begin(); renderer.drawSkeleton(skeleton, true); renderer.end();
      gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      const frame = pixelBounds(pixels, canvas.width, canvas.height);
      if (!frame) continue;
      left = Math.min(left, frame.left); bottom = Math.min(bottom, frame.bottom);
      right = Math.max(right, frame.right); top = Math.max(top, frame.top);
    }
    if (right <= left || top <= bottom) throw new Error("The portrait has no visible pixels");
    // Two measurement pixels protect antialiasing, low-opacity texture edges
    // and subpixel movement. This is quantization tolerance, not an art crop.
    const minX = viewport.centerX - viewport.viewportWidth / 2 + (left - 2) * unit;
    const minY = viewport.centerY - viewport.viewportHeight / 2 + (bottom - 2) * unit;
    const maxX = viewport.centerX - viewport.viewportWidth / 2 + (right + 2) * unit;
    const maxY = viewport.centerY - viewport.viewportHeight / 2 + (top + 2) * unit;
    return { bounds: { centerX: (minX + maxX) / 2, centerY: (minY + maxY) / 2,
      width: maxX - minX, height: maxY - minY },
      measurement: { rule: VISIBLE_RULE, resolution: [800, 1040], samples: times.length,
        fps: 60, guardPixels: 2, pixels: { left, bottom, right, top }, coarse, viewport } };
  }

  function validateProfile(profile, { base, skin, animation, pages }) {
    const bounds = profile?.bounds;
    const expected = [`${base}.skel`, `${base}.atlas`, ...pages].sort();
    if (profile?.schema !== 1 || profile.rule !== VISIBLE_RULE || profile.base !== base
        || profile.skin !== skin || profile.animation !== animation
        || !bounds || ![bounds.centerX, bounds.centerY, bounds.width, bounds.height].every(Number.isFinite)
        || bounds.width <= 0 || bounds.height <= 0
        || JSON.stringify(Object.keys(profile.sources || {}).sort()) !== JSON.stringify(expected)) {
      throw new Error("Invalid or incompatible portrait framing profile");
    }
    return { ...bounds };
  }

  async function verifySource(profile, name, bytes) {
    const record = profile.sources[name];
    if (!record || record.bytes !== bytes.byteLength || !/^[a-f0-9]{64}$/.test(record.sha256)) {
      throw new Error(`Portrait framing source mismatch: ${name}`);
    }
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    const hash = [...digest].map(value => value.toString(16).padStart(2, "0")).join("");
    if (hash !== record.sha256) throw new Error(`Portrait framing source mismatch: ${name}`);
  }

  window.EncyclopediaPortraitFraming = Object.freeze({
    createSkeleton, measureAnimationBounds, fitViewport, VISIBLE_RULE,
    animationSampleTimes, pixelBounds, measureVisibleAnimationBounds, validateProfile, verifySource,
  });
})();
