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
    skin = "LV1", animation = "00_Idle", assertActive = () => {},
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

    for (let index = 0; index <= samples; index += 1) {
      assertActive();
      skeleton.setToSetupPose();
      selected.apply(
        skeleton, 0, duration * index / samples, true, [], 1,
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

  window.EncyclopediaPortraitFraming = Object.freeze({
    createSkeleton, measureAnimationBounds, fitViewport,
  });
})();
