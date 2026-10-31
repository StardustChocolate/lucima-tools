"use strict";

// The local service owns the durable cache. These requests never use game accounts.
(() => {
  const pending = new Map();
  async function request(path, body) {
    const response = await fetch(`/api/encyclopedia/${path}`, body === undefined
      ? { cache: "no-store" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "图鉴资料加载失败");
    return data;
  }
  const details = new Map();
  const fetchResource = async url => {
    const response = await fetch(url);
    if (!response.ok) throw new Error("该图鉴资源尚未缓存，联网后重试");
    return response;
  };
  window.LucimaEncyclopediaData = Object.freeze({
    catalog: () => request("catalog"),
    async check(force = false) {
      if (!pending.has("check")) pending.set("check", request("check", { force }).finally(() => pending.delete("check")));
      return pending.get("check");
    },
    async detail(version, entry) {
      const key = entry.detail.sha256;
      if (!details.has(key)) {
        const promise = request(`detail?version=${encodeURIComponent(version)}&id=${encodeURIComponent(entry.id)}`);
        details.set(key, promise);
        promise.catch(() => { if (details.get(key) === promise) details.delete(key); });
      }
      return details.get(key);
    },
    config(envelope) {
      return { schema: 2, version: envelope.version,
        baseUrl: new URL("/api/encyclopedia/", location.origin).href,
        staticPortraitBaseUrl: "portraits/static/", dynamicBaseUrl: "portraits/spine/",
        skillIconBaseUrl: "skills/", spineRuntimeUrl: "/static/vendor/spine-webgl.js",
        files: envelope.media };
    },
    fetchResource,
  });
})();
