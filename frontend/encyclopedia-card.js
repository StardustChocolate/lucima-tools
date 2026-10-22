"use strict";

// Ark2's archive card embedded in LucimaTools. The surrounding app owns navigation,
// data fetching, zoom and pan; this module owns one card and its effects.
(() => {
  let nextMountId = 1;
  const CARD_TEMPLATE = `<article class="archive-card encyclopedia-ark2" data-character="H003" aria-label="角色图鉴卡">
        <div class="starfield" id="starfield" aria-hidden="true"></div>
        <div class="portrait-layer" id="portrait-layer" aria-label="动态立绘">
          <canvas class="portrait-canvas" id="portrait-canvas"></canvas>
          <img class="portrait-fallback" id="portrait-fallback" alt="立绘">
        </div>
        <div class="frame-tech" aria-hidden="true">
          <svg class="frame-svg" id="frame-svg" viewBox="0 0 1000 1200" preserveAspectRatio="none" focusable="false">
            <defs>
              <linearGradient id="inner-frame-gradient" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="1000" y2="0">
                <stop class="frame-inner-fade-stop" offset="0" stop-opacity="0"></stop>
                <stop class="frame-inner-fade-stop" offset="10%" stop-opacity="0"></stop>
                <stop class="frame-inner-fade-stop" offset="18%" stop-opacity="1"></stop>
                <stop class="frame-inner-fade-stop" offset="82%" stop-opacity="1"></stop>
                <stop class="frame-inner-fade-stop" offset="90%" stop-opacity="0"></stop>
                <stop class="frame-inner-fade-stop" offset="100%" stop-opacity="0"></stop>
              </linearGradient>
              <linearGradient id="endpoint-fade-gradient" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0" stop-color="white" stop-opacity="0"></stop>
                <stop offset="10%" stop-color="white" stop-opacity="0"></stop>
                <stop offset="18%" stop-color="white" stop-opacity="1"></stop>
                <stop offset="82%" stop-color="white" stop-opacity="1"></stop>
                <stop offset="90%" stop-color="white" stop-opacity="0"></stop>
                <stop offset="100%" stop-color="white" stop-opacity="0"></stop>
              </linearGradient>
              <mask id="endpoint-fade-mask" mask-type="alpha" maskUnits="userSpaceOnUse" maskContentUnits="userSpaceOnUse" x="0" y="0" width="1000" height="1200">
                <rect id="endpoint-fade-rect" x="0" y="0" width="1000" height="1200" fill="url(#endpoint-fade-gradient)"></rect>
              </mask>
              <radialGradient id="badge-frame-fade-gradient" gradientUnits="userSpaceOnUse" cx="90" cy="90" r="150">
                <stop offset="0%" stop-color="white" stop-opacity="0"></stop>
                <stop offset="48%" stop-color="white" stop-opacity="0"></stop>
                <stop offset="88%" stop-color="white" stop-opacity="1"></stop>
                <stop offset="100%" stop-color="white" stop-opacity="1"></stop>
              </radialGradient>
              <mask id="badge-frame-fade-mask" mask-type="alpha" maskUnits="userSpaceOnUse" maskContentUnits="userSpaceOnUse" x="0" y="0" width="1000" height="1200">
                <rect id="badge-frame-fade-rect" x="0" y="0" width="1000" height="1200" fill="url(#badge-frame-fade-gradient)"></rect>
              </mask>
              <radialGradient id="point-light-mask-gradient">
                <stop offset="0%" stop-color="white" stop-opacity="1"></stop>
                <stop offset="18%" stop-color="white" stop-opacity="1"></stop>
                <stop offset="52%" stop-color="white" stop-opacity="0.658824"></stop>
                <stop offset="80%" stop-color="white" stop-opacity="0.188235"></stop>
                <stop offset="100%" stop-color="white" stop-opacity="0"></stop>
              </radialGradient>
              <path id="point-light-route" d="M 42 42 H 958 V 1158 H 42 Z"></path>
              <mask id="point-light-mask" mask-type="alpha" maskUnits="userSpaceOnUse" maskContentUnits="userSpaceOnUse" x="0" y="0" width="1000" height="1200">
                <rect id="point-light-mask-background" x="0" y="0" width="1000" height="1200" fill="white" fill-opacity="0"></rect>
                <circle id="point-light-source" cx="0" cy="0" r="180" fill="url(#point-light-mask-gradient)">
                  <animateMotion id="point-light-motion" dur="15s" begin="-3s" repeatCount="indefinite" calcMode="paced">
                    <mpath href="#point-light-route"></mpath>
                  </animateMotion>
                </circle>
              </mask>
              <filter id="point-light-halo-filter" x="-6%" y="-6%" width="112%" height="112%" color-interpolation-filters="sRGB">
                <feGaussianBlur in="SourceGraphic" stdDeviation="14"></feGaussianBlur>
              </filter>
              <filter id="point-light-mid-filter" x="-4%" y="-4%" width="108%" height="108%" color-interpolation-filters="sRGB">
                <feGaussianBlur in="SourceGraphic" stdDeviation="4"></feGaussianBlur>
              </filter>
            </defs>
            <g id="frame-lines" mask="url(#badge-frame-fade-mask)">
              <path class="frame-path" id="frame-path" pathLength="1000" d=""></path>
              <path class="frame-path frame-path--upper-inner" id="upper-inner-path" pathLength="1000" d=""></path>
              <path class="frame-path frame-path--lower-inner" id="lower-inner-path" pathLength="1000" d=""></path>
            </g>
            <path class="frame-solid-trapezoid" id="top-solid-trapezoid" d=""></path>
            <path class="frame-solid-trapezoid" id="bottom-solid-trapezoid" d=""></path>
            <path class="frame-corner-line" id="top-right-corner-line" d=""></path>
            <path class="frame-corner-line" id="bottom-right-corner-line" d=""></path>
            <path class="frame-corner-line" id="bottom-left-corner-line" d=""></path>
            <g mask="url(#badge-frame-fade-mask)">
              <g class="frame-point-light frame-point-light--halo" mask="url(#point-light-mask)">
                <path class="point-light-stroke point-light-stroke--outer" data-frame-shape="frame-path" d=""></path>
                <g mask="url(#endpoint-fade-mask)">
                  <path class="point-light-stroke point-light-stroke--inner" data-frame-shape="upper-inner-path" mask="url(#endpoint-fade-mask)" d=""></path>
                  <path class="point-light-stroke point-light-stroke--inner" data-frame-shape="lower-inner-path" mask="url(#endpoint-fade-mask)" d=""></path>
                </g>
                <path class="point-light-fill" data-frame-shape="top-solid-trapezoid" d=""></path>
                <path class="point-light-fill" data-frame-shape="bottom-solid-trapezoid" d=""></path>
                <path class="point-light-fill" data-frame-shape="top-right-corner-line" d=""></path>
                <path class="point-light-fill" data-frame-shape="bottom-right-corner-line" d=""></path>
                <path class="point-light-fill" data-frame-shape="bottom-left-corner-line" d=""></path>
              </g>
              <g class="frame-point-light frame-point-light--mid" mask="url(#point-light-mask)">
                <path class="point-light-stroke point-light-stroke--outer" data-frame-shape="frame-path" d=""></path>
                <g mask="url(#endpoint-fade-mask)">
                  <path class="point-light-stroke point-light-stroke--inner" data-frame-shape="upper-inner-path" mask="url(#endpoint-fade-mask)" d=""></path>
                  <path class="point-light-stroke point-light-stroke--inner" data-frame-shape="lower-inner-path" mask="url(#endpoint-fade-mask)" d=""></path>
                </g>
                <path class="point-light-fill" data-frame-shape="top-solid-trapezoid" d=""></path>
                <path class="point-light-fill" data-frame-shape="bottom-solid-trapezoid" d=""></path>
                <path class="point-light-fill" data-frame-shape="top-right-corner-line" d=""></path>
                <path class="point-light-fill" data-frame-shape="bottom-right-corner-line" d=""></path>
                <path class="point-light-fill" data-frame-shape="bottom-left-corner-line" d=""></path>
              </g>
              <g class="frame-point-light frame-point-light--core" mask="url(#point-light-mask)">
                <path class="point-light-stroke point-light-stroke--outer" data-frame-shape="frame-path" d=""></path>
                <g mask="url(#endpoint-fade-mask)">
                  <path class="point-light-stroke point-light-stroke--inner" data-frame-shape="upper-inner-path" mask="url(#endpoint-fade-mask)" d=""></path>
                  <path class="point-light-stroke point-light-stroke--inner" data-frame-shape="lower-inner-path" mask="url(#endpoint-fade-mask)" d=""></path>
                </g>
                <path class="point-light-fill" data-frame-shape="top-solid-trapezoid" d=""></path>
                <path class="point-light-fill" data-frame-shape="bottom-solid-trapezoid" d=""></path>
                <path class="point-light-fill" data-frame-shape="top-right-corner-line" d=""></path>
                <path class="point-light-fill" data-frame-shape="bottom-right-corner-line" d=""></path>
                <path class="point-light-fill" data-frame-shape="bottom-left-corner-line" d=""></path>
              </g>
            </g>
            <g class="corner-badge" id="top-left-badge" aria-hidden="true">
            </g>
          </svg>
        </div>
        <div class="card-content">
          <header class="character-identity" aria-label="角色身份">
            <h1 class="character-name"></h1>
            <p class="character-aliases" aria-label="英文名与别名"></p>
            <p class="character-counterpart"></p>
          </header>
          <div class="character-stars" id="character-stars" aria-label="角色星级"></div>
          <section class="character-job" data-job="" aria-label="角色职业">
            <img class="character-job__icon" alt="">
            <span class="character-job__name"></span>
          </section>
          <section class="character-facts" id="character-facts" aria-label="角色基础资料"></section>
          <table class="character-stats" id="character-stats" aria-label="角色基础与满调教属性"></table>
          <table class="character-potential" id="character-potential" aria-label="角色潜能"></table>
          <section class="character-faction" id="character-faction" aria-label="角色阵营"></section>
          <section class="character-skills" id="character-skills" aria-label="角色技能"></section>
          <p class="character-data-error" id="character-data-error" role="alert" aria-live="polite" hidden></p>
        </div>
      </article>`;

  function mount({ host, data, translations = {}, staticMode = true } = {}) {
    if (!host || typeof host.replaceChildren !== "function") {
      throw new TypeError("LucimaEncyclopediaCard.mount requires a host element.");
    }
    if (!data || typeof data !== "object") {
      throw new TypeError("LucimaEncyclopediaCard.mount requires character data.");
    }
    const palette = window.LucimaEncyclopediaPalette;
    if (!palette) throw new Error("Encyclopedia palette is not loaded.");
    const template = document.createElement("template");
    template.innerHTML = CARD_TEMPLATE;
    const card = template.content.firstElementChild;
    const suffix = `lucima-archive-${nextMountId++}`;
    const ids = new Map();
    for (const node of card.querySelectorAll("[id]")) {
      const original = node.id;
      node.id = `${original}-${suffix}`;
      ids.set(original, node.id);
    }
    for (const node of card.querySelectorAll("*")) {
      for (const attribute of ["href", "xlink:href", "mask", "fill", "filter", "clip-path", "stroke"]) {
        const value = node.getAttribute(attribute);
        if (!value) continue;
        const updated = value.replace(/url\(#([^)]+)\)|^#(.+)$/g, (match, urlId, hrefId) => {
          const original = urlId || hrefId;
          if (!ids.has(original)) return match;
          return urlId ? `url(#${ids.get(original)})` : `#${ids.get(original)}`;
        });
        if (updated !== value) node.setAttribute(attribute, updated);
      }
    }
    const query = (selector) => selector.startsWith("#")
      ? card.querySelector(`#${ids.get(selector.slice(1)) || selector.slice(1)}`)
      : card.querySelector(selector);
    let activeCharacterData = null;
    let activeTranslations = translations || {};
    let statusIconMap = Object.freeze({});
    const resolveSmallIcon = (path) => activeTranslations.small_icons?.[path] || path;
    const SOUL_ICON_URL = resolveSmallIcon("../assets/界面图标/其它/Skill/Icon_Skill_Soul.png");
    const SOUL_ICON_UNIT = 10;
    const frameSvg = query("#frame-svg");
    const framePath = query("#frame-path");
    const topSolidTrapezoid = query("#top-solid-trapezoid");
    const bottomSolidTrapezoid = query("#bottom-solid-trapezoid");
    const topRightCornerLine = query("#top-right-corner-line");
    const bottomRightCornerLine = query("#bottom-right-corner-line");
    const bottomLeftCornerLine = query("#bottom-left-corner-line");
    const topLeftBadge = query("#top-left-badge");
    const upperInnerPath = query("#upper-inner-path");
    const lowerInnerPath = query("#lower-inner-path");
    const endpointFadeMask = query("#endpoint-fade-mask");
    const endpointFadeRect = query("#endpoint-fade-rect");
    const badgeFrameFadeMask = query("#badge-frame-fade-mask");
    const badgeFrameFadeRect = query("#badge-frame-fade-rect");
    const badgeFrameFadeGradient = query("#badge-frame-fade-gradient");
    const pointLightMask = query("#point-light-mask");
    const pointLightMaskBackground = query("#point-light-mask-background");
    const pointLightRoute = query("#point-light-route");
    const pointLightMotion = query("#point-light-motion");
    const pointLightPaths = [...card.querySelectorAll(".frame-point-light path")].map((path) => ({
      path,
      basePath: query(`#${path.dataset.frameShape}`)
    }));
    const starfield = query("#starfield");
    const characterStars = query("#character-stars");
    const characterStats = query("#character-stats");
    const characterPotential = query("#character-potential");
    const characterFacts = query("#character-facts");
    const characterFaction = query("#character-faction");
    const characterSkills = query("#character-skills");
    const characterIdentity = query(".character-identity");
    const characterName = query(".character-name");
    const characterAliases = query(".character-aliases");
    const characterCounterpart = query(".character-counterpart");
    const characterDataError = query("#character-data-error");
    const characterJob = query(".character-job");
    const characterJobIcon = query(".character-job__icon");
    const characterJobName = query(".character-job__name");

    const elementThemes = Object.freeze({
      Fire: { color: "rgb(238 78 72)", icon: "../assets/界面图标/属性/Fire_火属性.png" },
      Ice: { color: "rgb(66 133 255)", icon: "../assets/界面图标/属性/Ice_水属性.png" },
      Earth: { color: "rgb(63 189 100)", icon: "../assets/界面图标/属性/Earth_木属性.png" },
      Light: { color: "rgb(234 207 67)", icon: "../assets/界面图标/属性/Light_光属性.png" },
      Dark: { color: "rgb(183 92 229)", icon: "../assets/界面图标/属性/Dark_暗属性.png" }
    });

    const elementAliases = Object.freeze({
      fire: "Fire",
      ice: "Ice",
      water: "Ice",
      earth: "Earth",
      nature: "Earth",
      light: "Light",
      dark: "Dark"
    });

    // Frozen copy of preset 1 for the standalone encyclopedia frame.
    const badgeRings = [
      { innerRadius: 40, width: 20, mode: "solid", direction: "cw", speed: 0.1, opacity: 0.28, gap: 21, angle: 0 },
      { innerRadius: 50, width: 17, mode: "solid", direction: "ccw", speed: 0.5, opacity: 0.22, gap: 40, angle: 20 },
      { innerRadius: 40, width: 4, mode: "hollow", direction: "cw", speed: 0.3, opacity: 0.5, gap: 92, angle: 40 },
      { innerRadius: 54, width: 4, mode: "hollow", direction: "ccw", speed: 0.4, opacity: 0.39, gap: 86, angle: 220 },
      { innerRadius: 50, width: 15, mode: "hollow", direction: "cw", speed: 0.5, opacity: 0.38, gap: 84, angle: 60 },
      { innerRadius: 70, width: 2, mode: "solid", direction: "ccw", speed: 0.2, opacity: 0.46, gap: 67, angle: 80 },
      { innerRadius: 50, width: 10, mode: "hollow", direction: "cw", speed: 0.2, opacity: 0.4, gap: 99, angle: 200 },
      { innerRadius: 40, width: 8, mode: "hollow", direction: "cw", speed: 0.2, opacity: 0.51, gap: 97, angle: 160 },
      { innerRadius: 80, width: 4, mode: "hollow", direction: "cw", speed: 0.1, opacity: 0.62, gap: 67, angle: 0 },
      { innerRadius: 80, width: 4, mode: "hollow", direction: "cw", speed: 0.1, opacity: 0.73, gap: 79, angle: 160 },
      { innerRadius: 74, width: 6, mode: "hollow", direction: "cw", speed: 0.1, opacity: 0.84, gap: 99, angle: 220 },
      { innerRadius: 78, width: 6, mode: "hollow", direction: "cw", speed: 0.1, opacity: 0.66, gap: 99.8, angle: 260 }
    ];

    const frameWidth = 1000;
    const frameInset = 30;
    const chamfer = 24;
    const topProjectionHalf = 180;
    const topProjectionBaseHalf = 158;
    const topProjectionHeight = 22;
    const upperInnerOffset = 12;
    const upperInnerTrim = 24;
    const solidTrapezoidGap = 9;
    const solidTrapezoidThickGap = 10;
    const solidTrapezoidTopHalf = 100;
    const solidTrapezoidBottomHalf = 88;
    const solidTrapezoidHeight = 14;
    const cornerLineOffset = 12;
    const cornerLineHorizontalLength = 48;
    const cornerLineVerticalLength = 48;
    const cornerLineThickness = 8;
    const cornerLineCapSkew = 4;
    const badgeOffsetX = -36;
    const badgeOffsetY = -36;
    const badgeCenterX = 126 + badgeOffsetX;
    const badgeCenterY = 126 + badgeOffsetY;
    const badgeElementIconSize = 58;
    const pointLightInset = 42;
    const pointLightSpeed = 260;
    const starCount = 96;

    function seededRandom(seed) {
      let state = seed >>> 0;
      return () => {
        state = Math.imul(state ^ state >>> 15, 1 | state);
        state ^= state + Math.imul(state ^ state >>> 7, 61 | state);
        return ((state ^ state >>> 14) >>> 0) / 4294967296;
      };
    }

    function renderStarfield() {
      if (!starfield) return;
      const random = seededRandom(0x48303033);
      const colorVariants = ["neutral", "neutral", "cool", "warm", "theme"];
      const stars = document.createDocumentFragment();

      for (let index = 0; index < starCount; index += 1) {
        const star = document.createElement("i");
        const sizeRoll = random();
        const size = sizeRoll > 0.94
          ? 3.2 + random() * 1.1
          : sizeRoll > 0.72
            ? 1.8 + random() * 1.1
            : 0.8 + random() * 0.9;
        const restOpacity = 0.12 + random() * 0.3;
        const peakOpacity = Math.min(0.92, restOpacity + 0.28 + random() * 0.32);
        const color = colorVariants[Math.floor(random() * colorVariants.length)];

        star.className = `starfield__star starfield__star--${color}`;
        star.style.left = `${(random() * 100).toFixed(2)}%`;
        star.style.top = `${(random() * 100).toFixed(2)}%`;
        star.style.setProperty("--star-size", `${size.toFixed(2)}px`);
        star.style.setProperty("--star-glow", `${(size * (1.2 + random())).toFixed(2)}px`);
        star.style.setProperty("--star-rest-opacity", restOpacity.toFixed(2));
        star.style.setProperty("--star-peak-opacity", peakOpacity.toFixed(2));
        star.style.setProperty("--star-duration", `${(4 + random() * 5.6).toFixed(2)}s`);
        star.style.setProperty("--star-delay", `${(-random() * 9.6).toFixed(2)}s`);
        stars.append(star);
      }

      starfield.replaceChildren(stars);
    }

    function renderCharacterStars(ratingValue = 0) {
      if (!characterStars) return;
      const rating = Math.max(0, Math.min(6, Number.parseInt(ratingValue, 10) || 0));
      const stars = document.createDocumentFragment();
      const starPath = "M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.12 2.12 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.12 2.12 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.769.559l-4.618-2.428a2.12 2.12 0 0 0-1.976 0l-4.618 2.428a.53.53 0 0 1-.769-.56l.882-5.139a2.12 2.12 0 0 0-.611-1.878L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.12 2.12 0 0 0 1.597-1.16z";

      for (let index = 0; index < rating; index += 1) {
        const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        icon.classList.add("character-star");
        icon.setAttribute("viewBox", "0 0 24 24");
        icon.setAttribute("aria-hidden", "true");
        path.setAttribute("d", starPath);
        icon.append(path);
        stars.append(icon);
      }

      characterStars.setAttribute("aria-label", `${rating}星角色`);
      characterStars.replaceChildren(stars);
    }

    const statDefinitions = Object.freeze([
      { key: "HP", label: "生命", icon: "HP_生命力.png", percent: false },
      { key: "Attack", label: "攻击", icon: "Attack_攻击力.png", percent: false },
      { key: "Defence", label: "防御", icon: "Defence_防御力.png", percent: false },
      { key: "Speed", label: "速度", icon: "Speed_速度.png", percent: false },
      { key: "CriticalRate", label: "暴击率", icon: "Critical_暴击率.png", percent: true },
      { key: "CriticalDamageRate", label: "暴击伤害", icon: "CriticalDamage_暴击伤害.png", percent: true },
      { key: "EffectHitRate", label: "状态命中", icon: "EffectHit_状态命中.png", percent: true },
      { key: "ResistanceRate", label: "状态抗性", icon: "Resistance_状态抗性.png", percent: true },
      { key: "PinchRate", label: "追击率", icon: "Pinch_追击率.png", percent: true }
    ]);

    const propertyIcons = Object.freeze({
      HP: "HP_生命力.png",
      HPRate: "HP_生命力.png",
      HPValue: "HP_生命力.png",
      Attack: "Attack_攻击力.png",
      AttackRate: "Attack_攻击力.png",
      AttackValue: "Attack_攻击力.png",
      Defence: "Defence_防御力.png",
      DefenceRate: "Defence_防御力.png",
      DefenceValue: "Defence_防御力.png",
      Speed: "Speed_速度.png",
      SpeedValue: "Speed_速度.png",
      CriticalRate: "Critical_暴击率.png",
      CriticalDamageRate: "CriticalDamage_暴击伤害.png",
      EffectHitRate: "EffectHit_状态命中.png",
      ResistanceRate: "Resistance_状态抗性.png",
      PinchRate: "Pinch_追击率.png"
    });

    const propertyLabels = Object.freeze({
      HP: "生命",
      HPRate: "生命",
      HPValue: "生命",
      Attack: "攻击",
      AttackRate: "攻击",
      AttackValue: "攻击",
      Defence: "防御",
      DefenceRate: "防御",
      DefenceValue: "防御",
      Speed: "速度",
      SpeedValue: "速度",
      CriticalRate: "暴击率",
      CriticalDamageRate: "暴击伤害",
      EffectHitRate: "状态命中",
      ResistanceRate: "状态抗性",
      PinchRate: "追击率"
    });

    function createElement(tag, className, text) {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined && text !== null) element.textContent = String(text);
      return element;
    }

    function hasValue(value) {
      return value !== null && value !== undefined && value !== "";
    }

    function formatNumber(value, decimals = 2) {
      if (!hasValue(value)) return "";
      if (typeof value === "string") return value;
      const number = Number(value);
      if (!Number.isFinite(number)) return String(value);
      let rendered = number.toFixed(decimals);
      if (rendered.includes(".")) rendered = rendered.replace(/0+$/, "").replace(/\.$/, "");
      return rendered;
    }

    function formatStatValue(value, definition) {
      if (!hasValue(value)) return "";
      return definition.percent ? `${formatNumber(Number(value) * 100)}%` : formatNumber(value, 0);
    }

    function formatPotentialValue(value, property) {
      if (!hasValue(value)) return "";
      return property.endsWith("Rate") ? `${formatNumber(Number(value) * 100)}%` : formatNumber(value);
    }

    function statIsEqual(left, right) {
      if (!hasValue(left) || !hasValue(right)) return false;
      return Math.abs(Number(left) - Number(right)) < 0.000001;
    }

    function tunedStats(data) {
      const base = data.base_stats_lv60 || {};
      const finalLevel = Array.isArray(data.RoleAwaken) ? data.RoleAwaken[data.RoleAwaken.length - 1] : null;
      const cumulative = finalLevel?.CumulativeBonus || {};
      const result = { ...base };
      result.HP = Math.round(Number(base.HP || 0) * (1 + Number(cumulative.HPRate || 0)) + Number(cumulative.HPValue || 0));
      result.Attack = Math.round(Number(base.Attack || 0) * (1 + Number(cumulative.AttackRate || 0)) + Number(cumulative.AttackValue || 0));
      result.Defence = Math.round(Number(base.Defence || 0) * (1 + Number(cumulative.DefenceRate || 0)) + Number(cumulative.DefenceValue || 0));
      result.Speed = Math.round(Number(base.Speed || 0) + Number(cumulative.SpeedValue || 0));
      for (const definition of statDefinitions.filter((item) => item.percent)) {
        result[definition.key] = Number(base[definition.key] || 0) + Number(cumulative[definition.key] || 0);
      }
      return result;
    }

    function renderIdentity(data) {
      if (!characterIdentity) return;
      characterName.textContent = data.NAME || data.ID || "";
      characterIdentity.setAttribute("aria-label", `${data.NAME || data.ID || ""}角色资料`);
      characterAliases.replaceChildren();
      if (data.NAME_EN) characterAliases.append(createElement("span", "", data.NAME_EN));
      for (const alias of Array.isArray(data.NAME_ALIASES) ? data.NAME_ALIASES : []) {
        if (alias) characterAliases.append(createElement("span", "", alias));
      }
      characterCounterpart.textContent = data.NAME_e7 ? `对应E7角色：${data.NAME_e7}` : "";
    }

    function renderJob(data, labels) {
      if (!characterJob) return;
      const job = data.Job || "";
      characterJob.dataset.job = job;
      const mappedIcon = activeTranslations.job_icons?.[job];
      if (mappedIcon) characterJobIcon.src = mappedIcon;
      else characterJobIcon.removeAttribute("src");
      characterJobIcon.style.visibility = mappedIcon ? "" : "hidden";
      characterJobIcon.alt = labels.job || job;
      characterJobName.textContent = labels.job || job;
    }

    function renderStats(data) {
      if (!characterStats) return;
      const base = data.base_stats_lv60 || {};
      const tuned = tunedStats(data);
      characterStats.replaceChildren();
      const colgroup = createElement("colgroup");
      colgroup.append(createElement("col"), createElement("col"), createElement("col"));
      characterStats.append(colgroup);
      const thead = createElement("thead");
      const headingRow = createElement("tr");
      headingRow.append(createElement("th"), createElement("th", "", "基础"), createElement("th", "", "调教"));
      thead.append(headingRow);
      characterStats.append(thead);
      const tbody = createElement("tbody");
      for (const definition of statDefinitions) {
        const row = createElement("tr");
        const labelCell = createElement("th");
        labelCell.scope = "row";
        const label = createElement("span", "character-stat__label");
        const icon = createElement("img");
        icon.src = resolveSmallIcon(`../assets/界面图标/能力/${definition.icon}`);
        icon.alt = definition.label;
        label.append(icon, document.createTextNode(definition.label));
        labelCell.append(label);
        const baseCell = createElement("td", "", formatStatValue(base[definition.key], definition));
        const tunedCell = createElement("td", "", statIsEqual(base[definition.key], tuned[definition.key]) ? "" : formatStatValue(tuned[definition.key], definition));
        row.append(labelCell, baseCell, tunedCell);
        tbody.append(row);
      }
      characterStats.append(tbody);
      characterStats.setAttribute("aria-label", `${data.NAME || data.ID || "角色"}基础与满调教属性`);
    }

    function potentialCell(record, property) {
      const cell = createElement("td");
      if (!record || !hasValue(record.Value)) return cell;
      const value = createElement("span", "potential-value");
      const icon = createElement("img");
      icon.src = resolveSmallIcon(`../assets/界面图标/能力/${propertyIcons[property] || "Attack_攻击力.png"}`);
      icon.alt = propertyLabels[property] || property;
      value.append(icon, document.createTextNode(formatPotentialValue(record.Value, property)));
      cell.append(value);
      return cell;
    }

    function renderPotential(data) {
      if (!characterPotential) return;
      const self = data.SelfImprint || {};
      const team = data.TeamImprint || {};
      const selfByRank = new Map((self.Levels || []).filter((item) => item.Reachable).map((item) => [item.Rank, item]));
      const teamByRank = new Map((team.Levels || []).filter((item) => item.Reachable).map((item) => [item.Rank, item]));
      const ranks = ["D", "C", "B", "A", "S", "SS", "SSS"].filter((rank) => selfByRank.has(rank) || teamByRank.has(rank));
      characterPotential.replaceChildren();
      const colgroup = createElement("colgroup");
      colgroup.append(createElement("col"), createElement("col"), createElement("col"));
      characterPotential.append(colgroup);
      const thead = createElement("thead");
      const headingRow = createElement("tr");
      headingRow.append(createElement("th"));
      const selfHeading = createElement("th");
      const selfType = createElement("span", "potential-type");
      const selfIcon = createElement("img");
      selfIcon.src = resolveSmallIcon("../assets/界面图标/潜能类型/UI03_Icon_Mission_自身潜能.png");
      selfIcon.alt = "自身";
      selfType.append(selfIcon, document.createTextNode("自身"));
      selfHeading.append(selfType);
      const teamHeading = createElement("th");
      const teamType = createElement("span", "potential-type");
      const teamIcon = createElement("img");
      teamIcon.src = resolveSmallIcon("../assets/界面图标/潜能类型/UI03_Icon_Mission_2_队伍潜能.png");
      teamIcon.alt = "队伍";
      teamType.append(teamIcon, document.createTextNode("队伍"));
      teamHeading.append(teamType);
      headingRow.append(selfHeading, teamHeading);
      thead.append(headingRow);
      characterPotential.append(thead);
      const tbody = createElement("tbody");
      for (const rank of ranks) {
        const row = createElement("tr");
        const rankCell = createElement("th");
        rankCell.scope = "row";
        const rankIcon = createElement("img", "potential-rank");
        rankIcon.src = resolveSmallIcon(`../assets/界面图标/潜能等级/UI03_Icon_Mission_txt_${rank}.png`);
        rankIcon.alt = rank;
        rankCell.append(rankIcon);
        row.append(rankCell, potentialCell(selfByRank.get(rank), self.Property), potentialCell(teamByRank.get(rank), team.Property));
        tbody.append(row);
      }
      characterPotential.append(tbody);
      characterPotential.setAttribute("aria-label", `${data.NAME || data.ID || "角色"}潜能`);
    }

    function renderFact(iconPath, value, label, zodiac = false) {
      const fact = createElement("div", "character-fact");
      fact.setAttribute("aria-label", `${label}：${value || ""}`);
      if (zodiac) {
        const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        icon.classList.add("character-fact__icon", "character-fact__icon--zodiac");
        icon.setAttribute("viewBox", "0 0 48 48");
        icon.setAttribute("aria-hidden", "true");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", "M13 35 35 13M23 13h12v12M13 23l12 12");
        icon.append(path);
        fact.append(icon);
      } else {
        const icon = createElement("img", "character-fact__icon");
        icon.src = resolveSmallIcon(iconPath);
        icon.alt = label;
        fact.append(icon);
      }
      fact.append(createElement("span", "character-fact__value", value || ""));
      return fact;
    }

    function renderFacts(data, labels) {
      if (!characterFacts) return;
      characterFacts.replaceChildren();
      const firstRow = createElement("div", "character-facts__row");
      firstRow.append(
        renderFact("../assets/界面图标/角色资料/UI078_Icon_Height_身高.png", data.Height, "身高"),
        renderFact("../assets/界面图标/角色资料/UI078_Icon_Weight_体重.png", data.Weight, "体重"),
        renderFact("../assets/界面图标/角色资料/UI078_Icon_Measurements_胸围.png", data.CupSize, "胸围")
      );
      const secondRow = createElement("div", "character-facts__row");
      const constellation = String(labels.constellation || data.Constellation || "").replace(/座$/, "");
      secondRow.append(
        renderFact("", constellation, "星座", true),
        renderFact("../assets/界面图标/角色资料/UI078_Icon_Birthday_生日.png", data.Birthday, "生日")
      );
      characterFacts.append(firstRow, secondRow);
    }

    function fitProfileFactValues() {
      for (const value of card.querySelectorAll(".character-facts__row:first-child .character-fact__value")) {
        const fact = value.closest(".character-fact");
        fact?.classList.remove("character-fact--compact");
        value.style.fontSize = "";
        const defaultSize = Number.parseFloat(getComputedStyle(value).fontSize);
        if (value.scrollWidth <= value.clientWidth) continue;
        fact?.classList.add("character-fact--compact");
        value.style.fontSize = "";
        let size = defaultSize;
        while (value.scrollWidth > value.clientWidth && size > 10) {
          size = Math.max(10, size - 0.5);
          value.style.fontSize = `${size}px`;
        }
      }
    }

    function renderFaction(data, labels) {
      if (!characterFaction) return;
      characterFaction.replaceChildren();
      const identity = createElement("div", "character-faction__identity");
      const icon = createElement("img", "character-faction__icon");
      const mappedIcon = activeTranslations.faction_icons?.[data.Group];
      if (mappedIcon) icon.src = mappedIcon;
      else icon.style.visibility = "hidden";
      icon.alt = labels.group || data.Group || "";
      identity.append(icon, createElement("span", "character-faction__name", labels.group || data.Group || ""));
      characterFaction.append(identity);
      characterFaction.setAttribute("aria-label", `阵营：${labels.group || data.Group || ""}`);
    }


    function skillStatuses(skill) {
      const statuses = Array.isArray(skill.Status) ? skill.Status : [skill.Status];
      const unique = new Map();
      for (const status of statuses) {
        if (!status || !status.ID || unique.has(status.ID)) continue;
        const name = status.Name || status.ID;
        const description = status.Description || '';
        unique.set(status.ID, { ...status, name, description });
      }
      // Generated from game UI localization; source keys are validated at build time.
      // Never add handwritten descriptions or infer copy from DSL parameters.
      const details = activeTranslations.skill_function_cards || {};
      const markup = String(skill.Description_Markup || '').match(/\[[^\]:]+:([^\]:]+):StatusFun\]/g) || [];
      for (const token of markup) {
        const id = token.slice(1, -1).split(':')[1];
        if (details[id] && !unique.has(id)) unique.set(id, { ID: id, name: details[id].Name, description: details[id].Description, IsBuffType: '' });
      }
      return [...unique.values()];
    }

    const statusIconPrefixes = Object.freeze({
      AddAttack: 'Buff_IncreaseAttack',
      AddAttackBig: 'Buff_IncreaseAttackGreater',
      AddDefence: 'Buff_IncreaseDefense',
      Bleed: 'Debuff_Bleed',
      Bomb: 'Debuff_Bomb',
      Brand: 'Debuff_Brand',
      Burn: 'Debuff_Burn',
      CannotBuff: 'Debuff_CannotBuff',
      ContinuousHeal: 'Buff_ContinuousHeal',
      Counterattack: 'Buff_Counterattack',
      Curse: 'Debuff_Curse',
      DecreaseHitChance: 'Debuff_DecreaseHitChance',
      Enrage: 'Buff_Rage',
      Fascination: 'Debuff_Fascination',
      Flash: 'Buff_Flash',
      Frostburn: 'Debuff_Frostburn',
      Guard: 'Buff_Guard',
      H003S3: 'Buff_Idol',
      Hypertoxic: 'Debuff_Hypertoxic',
      Immortal: 'Buff_Immortal',
      Immune: 'Buff_Immune',
      IncreaseCriticalHitChance: 'Buff_IncreaseCriticalHitChance',
      IncreaseCriticalHitDamage: 'Buff_IncreaseCriticalHitDamage',
      IncreaseCriticalHitResistance: 'Buff_IncreaseCriticalHitResistance',
      IncreaseEffectiveness: 'Buff_IncreaseEffectiveness',
      IncreaseEvasion: 'Buff_IncreaseEvasion',
      IncreaseHitChance: 'Buff_IncreaseHitChance',
      IncreasePenetrateResistance: 'Buff_IncreasePenetrateResistance',
      IncreaseResistance: 'Buff_IncreaseEffectResistance',
      IncreaseSpeed: 'Buff_IncreaseSpeed',
      Invincible: 'Buff_Invincible',
      Knowledge: 'Buff_Perception',
      Lifesteal: 'Buff_Lifesteal',
      MaxSkillDamage: 'Buff_MaxSkillDamage',
      PinchEnhance: 'Buff_PinchEnhance',
      Poison: 'Debuff_Poison',
      Rage: 'Buff_Rage2',
      ReduceAttack: 'Debuff_DecreaseAttack',
      ReduceDef: 'Debuff_DecreaseDefense',
      ReduceSpeed: 'Debuff_DecreaseSpeed',
      Reflection: 'Buff_Reflect',
      Restrict: 'Debuff_Restrict',
      Revive: 'Buff_Revive',
      Seal: 'Debuff_Seal',
      Shield: 'Buff_Barrier',
      Silence: 'Debuff_Silence',
      SkillNullifier: 'Buff_SkillNullifier',
      Sleep: 'Debuff_Sleep',
      Stealth: 'Buff_Stealth',
      Stun: 'Debuff_Stun',
      Suppress: 'Debuff_Suppress',
      Target: 'Debuff_Target',
      TargetTaunt: 'Debuff_RedirectedProvoke',
      Taunt: 'Debuff_Provoke',
      Unhealable: 'Debuff_Unhealable',
      VamoiricTouch: 'Debuff_VamoiricTouch',
      Vigor: 'Buff_Vigor'
    });

    function statusTone(status) {
      const mappedTone = activeTranslations.status_tones?.[status.ID];
      if (['buff', 'debuff', 'neutral'].includes(mappedTone)) return mappedTone;
      const type = String(status.IsBuffType || '').toLowerCase();
      if (type.includes('debuff')) return 'debuff';
      if (type.includes('buff')) return 'buff';
      const icon = String(statusIconMap[status.ID] || statusIconPrefixes[status.ID] || '');
      if (/\bdebuff_/i.test(icon)) return 'debuff';
      if (/\bbuff_/i.test(icon)) return 'buff';
      return 'neutral';
    }

    function statusIconPath(status) {
      const tone = statusTone(status);
      const mappedIcon = statusIconMap[status.ID];
      if (mappedIcon) return mappedIcon;
      return tone === 'debuff'
        ? resolveSmallIcon('../assets/界面图标/其它/UI_QuestionMark/Debuff1.png')
        : resolveSmallIcon('../assets/界面图标/其它/UI_QuestionMark/Buff1.png');
    }

    function renderSkillStatuses(skill) {
      const statuses = skillStatuses(skill);
      if (!statuses.length) return null;
      const section = createElement('div', 'character-skill__statuses');
      for (const status of statuses) {
        const tone = statusTone(status);
        const card = createElement('article', `character-status character-status--${tone}`);
        const icon = createElement('img', 'character-status__icon');
        if (status.ID === 'Penetrate' || status.ID === 'SetDieOut' || status.ID === 'SetTime' || status.ID === 'TransferDeBuff' || status.ID === 'ReduceSoul' || status.ID === 'Torrent' || status.ID === 'CantMove' || status.ID === 'ShieldConversions' || status.ID === 'Injury' || status.ID === 'AddPinchSkill' || status.ID === 'RandomAddStatus_H141S2' || status.ID === 'ReduceSP' || status.ID === 'DirectHitReduction') {
          icon.classList.add('character-status__icon--empty');
          icon.alt = '';
        } else {
          icon.src = statusIconPath(status);
          icon.alt = `${status.name || status.ID}图标`;
        }
        const body = createElement('div', 'character-status__body');
        body.append(createElement('h3', 'character-status__name', status.name || status.ID));
        if (status.description) body.append(createElement('p', 'character-status__description', status.description));
        card.append(icon, body);
        card.setAttribute('aria-label', `${status.name || status.ID}：${status.description || '暂无说明'}`);
        section.append(card);
      }
      return section;
    }

    function clearSkillIcon(icon) {
      icon.removeAttribute("src");
      icon.alt = "";
      icon.style.visibility = "hidden";
    }

    function renderHighlightedText(parent, text, statuses) {
      // Localized descriptions sometimes retain 復 while status names use 复.
      const matchText = text.replace(/復/g, '复');
      const names = statuses
        .map((status) => ({ name: status.name || status.ID, tone: statusTone(status) }))
        .filter((status) => status.name)
        .sort((left, right) => right.name.length - left.name.length);
      let cursor = 0;
      while (cursor < text.length) {
        let match = null;
        for (const status of names) {
          const index = matchText.indexOf(status.name.replace(/復/g, '复'), cursor);
          if (index >= 0 && (!match || index < match.index)) match = { ...status, index };
        }
        if (!match) {
          parent.append(document.createTextNode(text.slice(cursor)));
          break;
        }
        if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
        parent.append(createElement('span', `character-skill__status-highlight character-skill__status-highlight--${match.tone}`, text.slice(match.index, match.index + match.name.length)));
        cursor = match.index + match.name.length;
      }
    }

    function renderSkillDescription(description, statuses = []) {
      const wrapper = createElement('div', 'character-skill__description');
      const lines = String(description || '').split(/\r?\n/);
      for (const line of lines) {
        const paragraph = createElement('p');
        const markup = line.match(/\[([^\]:]+):([^\]:]+):StatusFun\]/g);
        if (markup) {
          let cursor = 0;
          for (const token of markup) {
            const index = line.indexOf(token, cursor);
            paragraph.append(document.createTextNode(line.slice(cursor, index)));
            const parts = token.slice(1, -1).split(':');
            const status = statuses.find(item => item.ID === parts[1]);
            paragraph.append(createElement('span', `character-skill__status-highlight character-skill__status-highlight--${status ? statusTone(status) : 'neutral'}`, parts[0]));
            cursor = index + token.length;
          }
          paragraph.append(document.createTextNode(line.slice(cursor)));
        } else {
          paragraph.append(document.createTextNode(line));
        }
        wrapper.append(paragraph);
      }
      return wrapper;
    }

    // Explicit exceptions for source descriptions that omit StatusFun markup.
    // Keep this list centralized so future cases are easy to audit and remove.
    const descriptionStatusExceptions = Object.freeze({
      H010S1: [{ text: '无法恢復', id: 'Unhealable' }],
      H010S2: [{ text: '潜伏', id: 'Stealth' }]
    });

    function renderMappedSkillDescription(skill, statuses) {
      const exceptions = descriptionStatusExceptions[skill.ID] || [];
      let description = String(skill.Description_Markup || '');
      if (exceptions.length) {
        // Preserve generated tokens; only add audited highlights to plain text.
        description = description.split(/(\[[^\]:]+:[^\]:]+:StatusFun\])/g).map((part, index) => {
          if (index % 2) return part;
          for (const exception of exceptions) {
            const escaped = exception.text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            part = part.replace(new RegExp(escaped, 'g'), `[${exception.text}:${exception.id}:StatusFun]`);
          }
          return part;
        }).join('');
      }
      return renderSkillDescription(description, statuses);
    }

    function renderSkillScaling(skill) {
      return window.renderArchiveScaling?.(skill, activeCharacterData, activeTranslations) || null;
    }

    function fitSkillNames() {
      for (const name of card.querySelectorAll(".character-skill__name")) {
        name.style.fontSize = "";
        const defaultSize = Number.parseFloat(getComputedStyle(name).fontSize);
        let size = defaultSize;
        while (name.scrollWidth > name.clientWidth && size > 12) {
          size = Math.max(12, size - 0.5);
          name.style.fontSize = `${size}px`;
        }
      }
    }

    function renderSkill(skill) {
      const article = createElement("article", "character-skill");
      const header = createElement("header", "character-skill__header");
      const icon = createElement("img", "character-skill__icon");
      // The media loader fills this original 68px slot after the card mounts.
      clearSkillIcon(icon);
      icon.dataset.iconSprite = skill.IconSprite || "";
      const summary = createElement("div", "character-skill__summary");
      summary.append(createElement("h2", "character-skill__name", skill.NAME || skill.ID || ""));
      const specialResource = skill.SpecialResource;
      const specialResourceName = activeTranslations.values_CHS?.SpecialResource?.[specialResource?.SPType] || specialResource?.SPType || "";
      const specialResourceTone = {
        FightingSpirit: "character-skill__special-resource--fighting-spirit",
        Focus: "character-skill__special-resource--focus"
      }[specialResource?.SPType];
      let specialResourceRow = null;
      if (specialResourceName && hasValue(specialResource?.SP)) {
        specialResourceRow = createElement("div", "character-skill__special-resource");
        if (specialResourceTone) specialResourceRow.classList.add(specialResourceTone);
        const amount = specialResource.SP;
        specialResourceRow.textContent = `${specialResourceName} ${Number(amount) > 0 ? "+" : ""}${formatNumber(amount)}`;
      }
      const meta = createElement("div", "character-skill__meta");
      if (hasValue(skill.Soul_Final) && Number(skill.Soul_Final) !== 0) {
        const resource = createElement("span", "character-skill__resource");
        resource.setAttribute("aria-label", `获得${skill.Soul_Final}点星源`);
        const soulIcon = createElement("img");
        soulIcon.src = SOUL_ICON_URL;
        soulIcon.alt = "星源";
        resource.append(soulIcon, createElement("span", "", `+${skill.Soul_Final}`));
        meta.append(resource);
      }
      if (hasValue(skill.CoolDown_Final)) {
        const cooldown = createElement("span", "character-skill__cooldown");
        cooldown.setAttribute("aria-label", `冷却${skill.CoolDown_Final}回合`);
        cooldown.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg>';
        cooldown.append(createElement("span", "", `${skill.CoolDown_Final} 回合`));
        meta.append(cooldown);
      }
      summary.append(meta);
      header.append(icon, summary);
      const statuses = skillStatuses(skill);
      article.append(header);
      if (specialResourceRow) article.append(specialResourceRow);
      const description = renderMappedSkillDescription(skill, statuses);
      if (specialResourceRow) description.classList.add("character-skill__description--with-special-resource");
      article.append(description);
      const soulDescription = renderSoulDescription(skill, statuses);
      if (soulDescription) article.append(soulDescription);
      const statusCards = renderSkillStatuses(skill);
      if (statusCards) article.append(statusCards);
      const scaling = renderSkillScaling(skill);
      if (scaling) article.append(scaling);
      return article;
    }

    function autoCastConditionText(condition, camp = "", targets = [], resourceType = "") {
      if (!condition || typeof condition !== "object") return "";
      if (condition.CheckFun === "AllyHaveDeBuff") return "我方拥有负向状态";
      if (condition.CheckFun === "AnyAllyHaveCoolDown") return "我方有技能处于冷却";
      if (condition.CheckFun === "HaveBuff") return "我方拥有增益状态";
      // AllyCount includes the acting character and only counts living allies.
      if (condition.Property === "AllyCount" && condition.Comparer === "Lt" && condition.Value === 1
          && (condition.Who === undefined || condition.Who === "Source")
          && Object.keys(condition).every((key) => ["Property", "Comparer", "Value", "Who"].includes(key))) {
        return "我方仅自身存活";
      }
      // TargetType All selects the candidates; IsAllWhoPass defaults to Any.
      // Keep the existential gate separate from the later target selection.
      const info = condition.TargetInfo;
      if (["DeBuffCount", "DebuffCount"].includes(condition.Property) && condition.Value === 0
          && ["Gtn", "Eq"].includes(condition.Comparer)
          && (condition.IsAllWhoPass === undefined || condition.IsAllWhoPass === false)
          && Object.keys(condition).every((key) => ["TargetInfo", "Property", "Comparer", "Value", "IsAllWhoPass"].includes(key))
          && info?.TargetType === "All" && info.TargetCamp === "My" && info.NoSelf === true
          && Object.keys(info).every((key) => ["TargetType", "TargetCamp", "NoSelf"].includes(key))) {
        return condition.Comparer === "Gtn"
          ? "存在拥有负向状态的其他我方目标"
          : "存在无负向状态的其他我方目标";
      }
      // Resolve a simple self-resource gate from the character owning this table.
      // Other owners or compound conditions retain the existing fallback.
      if (condition.Property === "NowSP" && (!condition.Who || condition.Who === "Source")
          && Number.isFinite(condition.Value)
          && (!condition.Comparer || ["Eq", "Ne", "Lt", "Gt", "Ltn", "Gtn"].includes(condition.Comparer))
          && Object.keys(condition).every((key) => ["Property", "Comparer", "Value", "Who"].includes(key))) {
        const resource = activeTranslations.values_CHS?.SpecialResource?.[resourceType]
          || activeTranslations.scaling_CHS?.properties?.NowSP || "NowSP";
        const owner = activeTranslations.scaling_CHS?.targets?.Self || "自身";
        const comparator = activeTranslations.scaling_CHS?.comparers?.[condition.Comparer] || condition.Comparer || "=";
        return `${owner}${resource} ${comparator} ${condition.Value}`;
      }
      if (condition.Property === "NowHPRate") {
        const comparator = activeTranslations.scaling_CHS?.comparers?.[condition.Comparer] || condition.Comparer || "=";
        const owner = condition.Who === "Target"
          ? camp === "My" ? "我方生命比例最低者" : camp === "Enemy" ? "敌方生命比例最低者" : "生命比例最低者"
          : "自身生命比例";
        return `${owner} ${comparator} ${formatNumber(Number(condition.Value) * 100)}%`;
      }
      return condition.Raw || "";
    }

    function autoCastStatusMap(data) {
      const statuses = new Map();
      for (const skill of Array.isArray(data?.Skill) ? data.Skill : []) {
        for (const status of skillStatuses(skill)) {
          if (!statuses.has(status.ID)) statuses.set(status.ID, status);
        }
      }
      return statuses;
    }

    function autoCastConditionNode(condition, camp, statuses, targets = [], resourceType = "") {
      const status = condition?.CheckFun === "HaveBuff" && condition.CheckID
        ? statuses.get(condition.CheckID)
        : null;
      if (!status) return document.createTextNode(autoCastConditionText(condition, camp, targets, resourceType));
      const fragment = document.createDocumentFragment();
      fragment.append(document.createTextNode("自身处于"));
      fragment.append(createElement("span", `character-skill__status-highlight character-skill__status-highlight--${statusTone(status)}`, status.name));
      return fragment;
    }

    function autoCastCamp(camp) {
      if (camp === "My") return "我方";
      if (camp === "Enemy") return "敌方";
      return "目标";
    }

    function autoCastTargetText(target, camp) {
      const type = target?.Type;
      if (type === "Self") return activeTranslations.scaling_CHS?.targets?.Self || type;
      const otherAlly = target?.NoSelf === true ? "（自身除外）" : "";
      if (type === "MaxProperty" && camp === "My"
          && ["DebuffCount", "DeBuffCount"].includes(target.Property)
          && Object.keys(target).every((key) => ["Type", "Property", "NoSelf"].includes(key))) {
        return `负向状态最多的我方目标${otherAlly}`;
      }
      if (type === "HightestAtkOne") {
        // HightestAtkOne selects by attack value; the skill's parsed TargetCamp
        // supplies the camp. An empty source camp follows the game's default
        // Enemy value for skill targeting, while an absent/unknown value stays
        // neutral instead of being guessed.
        if (camp === "My") return `攻击力最高的我方目标${otherAlly}`;
        if (camp === "Enemy" || camp === "") return "攻击力最高的敌方目标";
        return "攻击力最高的目标";
      }
      const prefix = autoCastCamp(camp);
      const targetNoun = prefix === "目标" ? "目标" : `${prefix}目标`;
      if (type === "DeadOne") return "死亡的我方目标";
      if (type === "HaveDeBuffOne") return `拥有负向状态的${targetNoun}`;
      if (type === "LessHPRate") return `生命比例最低的${targetNoun}`;
      if (type === "Boss") return "首领";
      if (type === "MostTime") return "行动值最高目标";
      if (type === "ConquerOKTarget") return "属性判定目标";
      if (type === "HightestSkillCD") return `技能冷却最长的${targetNoun}`;
      if (type === "HaveCoolDown") return `有技能处于冷却的${targetNoun}`;
      if (type === "RandomOne") return "随机目标";
      return target?.Raw || type || "";
    }

    function renderAutoCast(data) {
      const rules = Array.isArray(data.AutoCast) ? data.AutoCast : [];
      if (!rules.length) return null;
      const skillNames = new Map((data.Skill || []).map((skill) => [skill.ID, skill.NAME || skill.ID]));
      const statuses = autoCastStatusMap(data);
      const displayOverrides = activeTranslations.auto_cast_display_CHS?.[data.ID] || [];
      const section = createElement("section", "character-auto-cast");
      section.append(createElement("h3", "character-auto-cast__title", "自动施放"));
      const table = createElement("div", "character-auto-cast__table");
      const header = createElement("div", "character-auto-cast__header-row");
      header.append(
        createElement("div", "character-auto-cast__header", "技能"),
        createElement("div", "character-auto-cast__header", "条件"),
        createElement("div", "character-auto-cast__header", "目标")
      );
      table.append(header);
      for (const [index, rule] of rules.entries()) {
        const skillNumber = Number(rule.SkillIndex) + 1;
        const skillLabel = rule.SkillLabel || `S${skillNumber}`;
        const skillCell = createElement("div", "character-auto-cast__skill", skillLabel);
        skillCell.title = skillNames.get(rule.SkillID) || rule.SkillID || "";
        const displayOverride = displayOverrides.find((display) =>
          display.SkillID === rule.SkillID && display.Priority === rule.Priority);
        const conditions = [...(rule.Conditions || [])];
        const targets = displayOverride?.TargetTexts || (rule.Targets || []).map((target) => {
          if (target?.Condition) {
            conditions.push(target.Condition);
          }
          return autoCastTargetText(target, rule.TargetCamp);
        }).filter(Boolean);
        const conditionCell = createElement("div", "character-auto-cast__condition");
        if (displayOverride) {
          conditionCell.textContent = displayOverride.ConditionText;
        } else if (!conditions.length) {
          conditionCell.textContent = "无";
        } else {
          conditions.forEach((condition, conditionIndex) => {
            if (conditionIndex) conditionCell.append(document.createTextNode("；"));
            conditionCell.append(autoCastConditionNode(condition, rule.TargetCamp, statuses, rule.Targets, data.SPType));
          });
        }
        const row = createElement("div", "character-auto-cast__row");
        row.append(
          skillCell,
          conditionCell,
          createElement("div", "character-auto-cast__target", targets.join(" → ") || "—")
        );
        table.append(row);
      }
      const divider = createElement("div", "character-auto-cast__divider");
      divider.setAttribute("aria-hidden", "true");
      const dividerHalf = `
        <path d="M0 14H348L362 28H298" />
        <path d="M364 14L378 28M380 14L394 28" />
        <path class="character-auto-cast__divider-dashes" d="M138 28H148M158 28H168M178 28H188M198 28H208M218 28H228M238 28H248M258 28H268M278 28H288" />`;
      divider.innerHTML = `<svg viewBox="0 0 900 45" focusable="false">
        <g class="character-auto-cast__divider-halo">${dividerHalf}</g>
        <g class="character-auto-cast__divider-halo" transform="translate(900 0) scale(-1 1)">${dividerHalf}</g>
        <g class="character-auto-cast__divider-core">${dividerHalf}</g>
        <g class="character-auto-cast__divider-core" transform="translate(900 0) scale(-1 1)">${dividerHalf}</g>
        <text class="character-auto-cast__divider-label" x="450" y="21" xml:space="preserve">鸣   谢</text>
      </svg>`;
      const creditField = (className, label, value) => {
        const field = createElement("div", className);
        field.append(
          createElement("span", "character-auto-cast__credit-label", label),
          typeof value === "string" ? createElement("span", "character-auto-cast__credit-value", value) : value
        );
        return field;
      };
      const discordLink = createElement("a", "character-auto-cast__credit-value", "NnTce6zvKY");
      discordLink.href = "https://discord.com/invite/NnTce6zvKY";
      discordLink.target = "_blank";
      discordLink.rel = "noopener noreferrer";
      const qqLink = createElement("a", "character-auto-cast__credit-value", "1032070842");
      qqLink.href = "https://qm.qq.com/q/YsvXane7Wq";
      qqLink.target = "_blank";
      qqLink.rel = "noopener noreferrer";
      const credits = createElement("div", "character-auto-cast__credits");
      const creditMain = createElement("div", "character-auto-cast__credit-main");
      creditMain.append(
        creditField(
          "character-auto-cast__credit-line character-auto-cast__credit-line--developers",
          "开发：",
          "花崎桜雪、巧克力、以及所有 OpenRubi 的贡献者和捐赠者们"
        ),
        creditField("character-auto-cast__credit-line character-auto-cast__credit-line--discord", "Discord：", discordLink)
      );
      credits.append(creditMain);
      const meta = createElement("div", "character-auto-cast__credit-meta");
      meta.append(
        creditField("character-auto-cast__credit-meta-item", "版本：", "v2026.9.9"),
        creditField("character-auto-cast__credit-meta-item", "QQ Group：", qqLink)
      );
      credits.append(meta);
      section.append(table, divider, credits);
      return section;
    }

    function renderSoulDescription(skill, statuses) {
      const soul = skill.SoulBurst || {};
      const cost = Number(soul.SkillSoulCost);
      if (!Number.isFinite(cost) || cost <= 0 || !soul.SoulSkillDescription) return null;
      const section = createElement("div", "character-skill__soul-description");
      const icons = createElement("div", "character-skill__soul-icons");
      icons.setAttribute("role", "img");
      icons.setAttribute("aria-label", `${activeTranslations.field_labels_CHS?.SkillSoulCost || "星源爆发消耗"} ${cost}`);
      const count = cost / SOUL_ICON_UNIT;
      for (let index = 0; index < count; index += 1) {
        const icon = createElement("img");
        icon.src = SOUL_ICON_URL;
        icon.alt = "";
        icons.append(icon);
      }
      const costLabel = createElement("span", "character-skill__soul-cost", `-${formatNumber(cost)}`);
      icons.append(costLabel);
      const description = renderSkillDescription(soul.SoulSkillDescription, statuses);
      description.classList.add("character-skill__soul-text");
      section.append(icons, description);
      return section;
    }

    function visibleSkills(data) {
      const hidden = new Set(data.HiddenSkillIDs || []);
      return (Array.isArray(data.Skill) ? data.Skill : [])
        .filter((skill) => skill && skill.ID && !hidden.has(skill.ID) && !skill.missing)
        .slice(0, 3);
    }

    function renderSkills(data) {
      if (!characterSkills) return;
      characterSkills.replaceChildren();
      for (const skill of visibleSkills(data)) characterSkills.append(renderSkill(skill));
      const autoCast = renderAutoCast(data);
      if (autoCast) characterSkills.append(autoCast);
      fitSkillNames();
      characterSkills.setAttribute("aria-label", `${data.NAME || data.ID || "角色"}技能`);
    }

    function resolveCharacterLabels(data, translations) {
      const values = translations?.values_CHS || {};
      return {
        element: values.Element?.[data.Element] || data.Element || "",
        job: values.Job?.[data.Job] || data.Job || "",
        constellation: values.Constellation?.[data.Constellation] || data.Constellation || "",
        group: values.Group?.[data.Group] || data.Group || ""
      };
    }

    function reportMissingData(data, translations) {
      const missing = [];
      for (const field of ["NAME", "NAME_EN", "Element", "Job", "Constellation", "Group", "base_stats_lv60", "RoleAwaken", "TeamImprint", "SelfImprint", "Skill"]) {
        if (!hasValue(data[field]) && !(Array.isArray(data[field]) && data[field].length)) missing.push(field);
      }
      for (const skill of visibleSkills(data)) {
        if (!hasValue(skill.Description_Markup)) missing.push(`${skill.ID}.Description_Markup`);
      }
      if (missing.length) console.warn("角色资料缺少图鉴字段，请确认生成源后补齐：", missing);
      const values = translations?.values_CHS || {};
      const missingLabels = [
        ["Element", data.Element],
        ["Job", data.Job],
        ["Constellation", data.Constellation],
        ["Group", data.Group]
      ].filter(([kind, code]) => hasValue(code) && !hasValue(values[kind]?.[code])).map(([kind, code]) => `${kind}.${code}`);
      if (missingLabels.length) console.warn("translation_map.json 缺少枚举映射，请补充映射文件：", missingLabels);
    }

    function renderCharacter(data, translations) {
      activeCharacterData = data;
      activeTranslations = translations;
      card.dataset.character = data.ID;
      statusIconMap = translations?.status_icons || {};
      const labels = resolveCharacterLabels(data, translations);
      reportMissingData(data, translations);
      card.dataset.element = data.Element || "Fire";
      card.setAttribute("aria-label", `${data.NAME || data.ID || "角色"}角色图鉴卡`);
      renderIdentity(data);
      renderJob(data, labels);
      renderStats(data);
      renderPotential(data);
      renderFacts(data, labels);
      fitProfileFactValues();
      renderFaction(data, labels);
      renderSkills(data);
      updateCardHeight();
      applyElementTheme(data.Element);
      renderCharacterStars(data.InitStar);
    }

    function updateCardHeight() {
      if (!card || !characterSkills) return;
      const skillsHeight = Number.parseFloat(getComputedStyle(characterSkills).height) || characterSkills.offsetHeight;
      const requiredHeight = Math.ceil(characterSkills.offsetTop + skillsHeight + 54);
      const nextHeight = Math.max(1200, requiredHeight);
      if (Math.abs(card.offsetHeight - nextHeight) > 1) card.style.height = `${nextHeight}px`;
    }

    function resolveElement(value) {
      return elementAliases[String(value || "").trim().toLowerCase()] || "Fire";
    }

    function activeElementConfig(value) {
      const element = resolveElement(value || card.dataset.element);
      return { element, ...elementThemes[element] };
    }

    function pointLightRouteData(height) {
      const right = frameWidth - pointLightInset;
      const bottom = Math.max(pointLightInset, height - pointLightInset);
      return `M ${pointLightInset} ${pointLightInset} H ${right} V ${bottom} H ${pointLightInset} Z`;
    }

    function pointLightDuration(height) {
      const routeWidth = frameWidth - pointLightInset * 2;
      const routeHeight = Math.max(0, height - pointLightInset * 2);
      return (2 * (routeWidth + routeHeight) / pointLightSpeed).toFixed(2);
    }

    function framePathData(height) {
      const right = frameWidth - frameInset;
      const bottom = height - frameInset;
      const topLeft = frameInset + chamfer;
      const topRight = right - chamfer;
      const topProjectionLeft = frameWidth / 2 - topProjectionHalf;
      const topProjectionLeftBase = frameWidth / 2 - topProjectionBaseHalf;
      const topProjectionRightBase = frameWidth / 2 + topProjectionBaseHalf;
      const topProjectionRight = frameWidth / 2 + topProjectionHalf;
      const projectionBaseY = frameInset - topProjectionHeight;
      return [
        `M ${topLeft} ${frameInset}`,
        `H ${topProjectionLeft}`,
        `L ${topProjectionLeftBase} ${projectionBaseY}`,
        `H ${topProjectionRightBase}`,
        `L ${topProjectionRight} ${frameInset}`,
        `H ${topRight}`,
        `L ${right} ${frameInset + chamfer}`,
        `V ${bottom - chamfer}`,
        `L ${right - chamfer} ${bottom}`,
        `H ${topProjectionRight}`,
        `L ${topProjectionRightBase} ${bottom - topProjectionHeight}`,
        `H ${topProjectionLeftBase}`,
        `L ${topProjectionLeft} ${bottom}`,
        `H ${frameInset + chamfer}`,
        `L ${frameInset} ${bottom - chamfer}`,
        `V ${frameInset + chamfer}`,
        "Z"
      ].join(" ");
    }

    function topSolidTrapezoidData() {
      const topY = frameInset - topProjectionHeight + upperInnerOffset + solidTrapezoidGap;
      const bottomY = topY + solidTrapezoidHeight;
      const topLeft = frameWidth / 2 - solidTrapezoidTopHalf;
      const topRight = frameWidth / 2 + solidTrapezoidTopHalf;
      const bottomLeft = frameWidth / 2 - solidTrapezoidBottomHalf;
      const bottomRight = frameWidth / 2 + solidTrapezoidBottomHalf;
      return [
        `M ${topLeft} ${topY}`,
        `H ${topRight}`,
        `L ${bottomRight} ${bottomY}`,
        `H ${bottomLeft}`,
        "Z"
      ].join(" ");
    }

    function bottomSolidTrapezoidData(height) {
      const bottom = height - frameInset;
      const shortEdgeY = bottom - topProjectionHeight;
      const topY = shortEdgeY + solidTrapezoidThickGap;
      const bottomY = topY + solidTrapezoidHeight;
      const topLeft = frameWidth / 2 - solidTrapezoidTopHalf;
      const topRight = frameWidth / 2 + solidTrapezoidTopHalf;
      const bottomLeft = frameWidth / 2 - solidTrapezoidBottomHalf;
      const bottomRight = frameWidth / 2 + solidTrapezoidBottomHalf;
      return [
        `M ${topLeft} ${topY}`,
        `H ${topRight}`,
        `L ${bottomRight} ${bottomY}`,
        `H ${bottomLeft}`,
        "Z"
      ].join(" ");
    }

    function cornerLineOutline(points) {
      const halfThickness = cornerLineThickness / 2;
      const units = points.slice(0, -1).map((point, index) => {
        const next = points[index + 1];
        const length = Math.hypot(next.x - point.x, next.y - point.y);
        return { x: (next.x - point.x) / length, y: (next.y - point.y) / length };
      });
      const normals = units.map((unit) => ({ x: -unit.y, y: unit.x }));
      const add = (point, vector, amount) => ({ x: point.x + vector.x * amount, y: point.y + vector.y * amount });
      const cross = (a, b) => a.x * b.y - a.y * b.x;
      const intersect = (pointA, directionA, pointB, directionB) => {
        const denominator = cross(directionA, directionB);
        const delta = { x: pointB.x - pointA.x, y: pointB.y - pointA.y };
        return add(pointA, directionA, cross(delta, directionB) / denominator);
      };
      const leftStart = add(add(points[0], normals[0], halfThickness), units[0], -cornerLineCapSkew);
      const rightStart = add(add(points[0], normals[0], -halfThickness), units[0], cornerLineCapSkew);
      const leftJoins = [];
      const rightJoins = [];
      for (let index = 1; index < points.length - 1; index += 1) {
        leftJoins.push(intersect(
          add(points[index], normals[index - 1], halfThickness), units[index - 1],
          add(points[index], normals[index], halfThickness), units[index]
        ));
        rightJoins.push(intersect(
          add(points[index], normals[index - 1], -halfThickness), units[index - 1],
          add(points[index], normals[index], -halfThickness), units[index]
        ));
      }
      const lastIndex = points.length - 1;
      const leftEnd = add(add(points[lastIndex], normals[lastIndex - 1], halfThickness), units[lastIndex - 1], cornerLineCapSkew);
      const rightEnd = add(add(points[lastIndex], normals[lastIndex - 1], -halfThickness), units[lastIndex - 1], -cornerLineCapSkew);
      const outline = [leftStart, ...leftJoins, leftEnd, rightEnd, ...rightJoins.reverse(), rightStart];
      return `${outline.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ")} Z`;
    }

    function topRightCornerLineData() {
      const cornerX = frameWidth - frameInset;
      const cornerY = frameInset;
      const diagonalOffset = cornerLineOffset / Math.SQRT2;
      const diagonalStartX = cornerX - chamfer;
      const diagonalEndY = cornerY + chamfer;
      const diagonalStartOffsetX = diagonalStartX + diagonalOffset;
      const diagonalStartOffsetY = cornerY - diagonalOffset;
      const diagonalEndOffsetX = cornerX + diagonalOffset;
      const diagonalEndOffsetY = diagonalEndY - diagonalOffset;
      const horizontalY = cornerY - cornerLineOffset;
      const verticalX = cornerX + cornerLineOffset;
      const diagonalHorizontalJoinX = diagonalStartOffsetX + (horizontalY - diagonalStartOffsetY);
      const diagonalVerticalJoinY = diagonalEndOffsetY + (verticalX - diagonalEndOffsetX);
      return cornerLineOutline([
        { x: diagonalHorizontalJoinX - cornerLineHorizontalLength, y: horizontalY },
        { x: diagonalHorizontalJoinX, y: horizontalY },
        { x: verticalX, y: diagonalVerticalJoinY },
        { x: verticalX, y: diagonalVerticalJoinY + cornerLineVerticalLength }
      ]);
    }

    function bottomRightCornerLineData(height) {
      const cornerX = frameWidth - frameInset;
      const bottomY = height - frameInset;
      const diagonalOffset = cornerLineOffset / Math.SQRT2;
      const diagonalStartX = cornerX - chamfer;
      const diagonalEndY = bottomY - chamfer;
      const diagonalStartOffsetX = diagonalStartX + diagonalOffset;
      const diagonalStartOffsetY = bottomY + diagonalOffset;
      const diagonalEndOffsetX = cornerX + diagonalOffset;
      const diagonalEndOffsetY = diagonalEndY + diagonalOffset;
      const horizontalY = bottomY + cornerLineOffset;
      const verticalX = cornerX + cornerLineOffset;
      const diagonalHorizontalJoinX = diagonalStartOffsetX - (horizontalY - diagonalStartOffsetY);
      const diagonalVerticalJoinY = diagonalEndOffsetY - (verticalX - diagonalEndOffsetX);
      return cornerLineOutline([
        { x: diagonalHorizontalJoinX - cornerLineHorizontalLength, y: horizontalY },
        { x: diagonalHorizontalJoinX, y: horizontalY },
        { x: verticalX, y: diagonalVerticalJoinY },
        { x: verticalX, y: diagonalVerticalJoinY - cornerLineVerticalLength }
      ]);
    }

    function bottomLeftCornerLineData(height) {
      const cornerX = frameInset;
      const bottomY = height - frameInset;
      const diagonalOffset = cornerLineOffset / Math.SQRT2;
      const diagonalStartX = cornerX + chamfer;
      const diagonalEndY = bottomY - chamfer;
      const diagonalStartOffsetX = diagonalStartX - diagonalOffset;
      const diagonalStartOffsetY = bottomY + diagonalOffset;
      const diagonalEndOffsetX = cornerX - diagonalOffset;
      const diagonalEndOffsetY = diagonalEndY + diagonalOffset;
      const horizontalY = bottomY + cornerLineOffset;
      const verticalX = cornerX - cornerLineOffset;
      const diagonalHorizontalJoinX = diagonalStartOffsetX + (horizontalY - diagonalStartOffsetY);
      const diagonalVerticalJoinY = diagonalEndOffsetY + (verticalX - diagonalEndOffsetX);
      return cornerLineOutline([
        { x: diagonalHorizontalJoinX + cornerLineHorizontalLength, y: horizontalY },
        { x: diagonalHorizontalJoinX, y: horizontalY },
        { x: verticalX, y: diagonalVerticalJoinY },
        { x: verticalX, y: diagonalVerticalJoinY - cornerLineVerticalLength }
      ]);
    }

    function upperInnerPathData() {
      const pathY = frameInset + upperInnerOffset;
      const projectionY = frameInset - topProjectionHeight + upperInnerOffset;
      const pathLeft = frameInset + chamfer + upperInnerTrim;
      const pathRight = frameWidth - pathLeft;
      const diagonalHorizontalShift = upperInnerOffset * (Math.SQRT2 - 1);
      const topProjectionLeft = frameWidth / 2 - topProjectionHalf;
      const topProjectionLeftBase = frameWidth / 2 - topProjectionBaseHalf;
      const topProjectionRightBase = frameWidth / 2 + topProjectionBaseHalf;
      const topProjectionRight = frameWidth / 2 + topProjectionHalf;
      return [
        `M ${pathLeft} ${pathY}`,
        `H ${topProjectionLeft + diagonalHorizontalShift}`,
        `L ${topProjectionLeftBase + diagonalHorizontalShift} ${projectionY}`,
        `H ${topProjectionRightBase - diagonalHorizontalShift}`,
        `L ${topProjectionRight - diagonalHorizontalShift} ${pathY}`,
        `H ${pathRight}`
      ].join(" ");
    }

    function lowerInnerPathData(height) {
      const bottom = height - frameInset;
      const pathY = bottom - upperInnerOffset;
      const projectionY = bottom - topProjectionHeight - upperInnerOffset;
      const pathLeft = frameInset + chamfer + upperInnerTrim;
      const pathRight = frameWidth - pathLeft;
      const diagonalHorizontalShift = upperInnerOffset * (Math.SQRT2 - 1);
      const topProjectionLeft = frameWidth / 2 - topProjectionHalf;
      const topProjectionLeftBase = frameWidth / 2 - topProjectionBaseHalf;
      const topProjectionRightBase = frameWidth / 2 + topProjectionBaseHalf;
      const topProjectionRight = frameWidth / 2 + topProjectionHalf;
      return [
        `M ${pathLeft} ${pathY}`,
        `H ${topProjectionLeft - diagonalHorizontalShift}`,
        `L ${topProjectionLeftBase - diagonalHorizontalShift} ${projectionY}`,
        `H ${topProjectionRightBase + diagonalHorizontalShift}`,
        `L ${topProjectionRight + diagonalHorizontalShift} ${pathY}`,
        `H ${pathRight}`
      ].join(" ");
    }

    function polarPoint(radius, angle) {
      const radians = (angle - 90) * Math.PI / 180;
      return {
        x: 126 + radius * Math.cos(radians),
        y: 126 + radius * Math.sin(radians)
      };
    }

    function annularSectorPath(innerRadius, outerRadius, gap) {
      if (outerRadius <= innerRadius || gap >= 100) return "";
      const gapAngle = gap * 3.6;
      if (gap <= 0) {
        const outerTop = polarPoint(outerRadius, 0);
        const outerBottom = polarPoint(outerRadius, 180);
        const innerTop = polarPoint(innerRadius, 0);
        const innerBottom = polarPoint(innerRadius, 180);
        return [
          `M ${outerTop.x} ${outerTop.y}`,
          `A ${outerRadius} ${outerRadius} 0 1 1 ${outerBottom.x} ${outerBottom.y}`,
          `A ${outerRadius} ${outerRadius} 0 1 1 ${outerTop.x} ${outerTop.y}`,
          `M ${innerTop.x} ${innerTop.y}`,
          `A ${innerRadius} ${innerRadius} 0 1 0 ${innerBottom.x} ${innerBottom.y}`,
          `A ${innerRadius} ${innerRadius} 0 1 0 ${innerTop.x} ${innerTop.y}`,
          "Z"
        ].join(" ");
      }
      const startAngle = gapAngle / 2;
      const endAngle = 360 - gapAngle / 2;
      const visibleAngle = 360 - gapAngle;
      const largeArc = visibleAngle > 180 ? 1 : 0;
      const outerStart = polarPoint(outerRadius, startAngle);
      const outerEnd = polarPoint(outerRadius, endAngle);
      const innerEnd = polarPoint(innerRadius, endAngle);
      const innerStart = polarPoint(innerRadius, startAngle);
      return [
        `M ${outerStart.x} ${outerStart.y}`,
        `A ${outerRadius} ${outerRadius} 0 ${largeArc} 1 ${outerEnd.x} ${outerEnd.y}`,
        `L ${innerEnd.x} ${innerEnd.y}`,
        `A ${innerRadius} ${innerRadius} 0 ${largeArc} 0 ${innerStart.x} ${innerStart.y}`,
        "Z"
      ].join(" ");
    }

    function renderBadge(elementConfig) {
      if (!topLeftBadge) return;
      topLeftBadge.setAttribute("transform", `translate(${badgeOffsetX} ${badgeOffsetY})`);
      topLeftBadge.replaceChildren();
      badgeRings.forEach((ring) => {
        const sector = document.createElementNS("http://www.w3.org/2000/svg", "path");
        const innerRadius = Math.max(40, Math.min(200, ring.innerRadius));
        const width = Math.max(0, Math.min(100, ring.width));
        sector.setAttribute("d", annularSectorPath(innerRadius, innerRadius + width, ring.gap));
        sector.classList.add(
          "badge-ring",
          ring.mode === "hollow" ? "badge-ring--hollow" : "badge-ring--solid",
          ring.direction === "ccw" ? "badge-ring--ccw" : "badge-ring--cw"
        );
        sector.style.setProperty("--ring-opacity", String(ring.opacity));
        sector.style.setProperty("--rotation", `${ring.angle}deg`);
        sector.style.setProperty("--duration", `${(1 / ring.speed).toFixed(4)}s`);
        topLeftBadge.append(sector);
      });

      const elementIcon = document.createElementNS("http://www.w3.org/2000/svg", "image");
      const iconOffset = 126 - badgeElementIconSize / 2;
      elementIcon.classList.add("badge-element-icon");
      elementIcon.setAttribute("x", iconOffset);
      elementIcon.setAttribute("y", iconOffset);
      elementIcon.setAttribute("width", badgeElementIconSize);
      elementIcon.setAttribute("height", badgeElementIconSize);
      elementIcon.setAttribute("preserveAspectRatio", "xMidYMid meet");
      elementIcon.setAttribute("href", resolveSmallIcon(elementConfig.icon));
      topLeftBadge.append(elementIcon);
    }

    function applyElementTheme(value) {
      const elementConfig = activeElementConfig(value);
      card.dataset.element = elementConfig.element;
      card.style.setProperty("--theme-color", elementConfig.color);
      for (const [property, color] of Object.entries(palette.themes[elementConfig.element])) {
        card.style.setProperty(property, color);
      }
      for (const colors of Object.values(palette.statuses)) {
        for (const [property, color] of Object.entries(colors)) {
          card.style.setProperty(property, color);
        }
      }
      renderBadge(elementConfig);
    }

    function syncFrame() {
      const height = Math.max(1, card.offsetHeight);
      frameSvg.setAttribute("viewBox", `0 0 ${frameWidth} ${height}`);
      endpointFadeMask.setAttribute("height", height);
      endpointFadeRect.setAttribute("height", height);
      badgeFrameFadeMask.setAttribute("height", height);
      badgeFrameFadeRect.setAttribute("height", height);
      badgeFrameFadeGradient.setAttribute("cx", badgeCenterX);
      badgeFrameFadeGradient.setAttribute("cy", badgeCenterY);
      pointLightMask.setAttribute("height", height);
      pointLightMaskBackground.setAttribute("height", height);
      pointLightRoute.setAttribute("d", pointLightRouteData(height));
      pointLightMotion.setAttribute("dur", `${pointLightDuration(height)}s`);
      framePath.setAttribute("d", framePathData(height));
      topSolidTrapezoid.setAttribute("d", topSolidTrapezoidData());
      bottomSolidTrapezoid.setAttribute("d", bottomSolidTrapezoidData(height));
      topRightCornerLine.setAttribute("d", topRightCornerLineData());
      bottomRightCornerLine.setAttribute("d", bottomRightCornerLineData(height));
      bottomLeftCornerLine.setAttribute("d", bottomLeftCornerLineData(height));
      upperInnerPath.setAttribute("d", upperInnerPathData());
      lowerInnerPath.setAttribute("d", lowerInnerPathData(height));
      // Independent paths avoid target CSS overriding <use> styles in older WebViews.
      for (const { path, basePath } of pointLightPaths) {
        path.setAttribute("d", basePath.getAttribute("d"));
      }
    }


    // Fade the resting strokes directly, avoiding nested full-card masks on mobile.
    for (const path of [upperInnerPath, lowerInnerPath]) {
      path.style.stroke = `url(#${ids.get("inner-frame-gradient")})`;
    }
    // CSS filters refer to the original IDs in Ark2; point each mounted card to
    // its own defs so simultaneous cards and repeated mounts cannot collide.
    for (const [className, filterId] of [
      ["frame-point-light--halo", "point-light-halo-filter"],
      ["frame-point-light--mid", "point-light-mid-filter"]
    ]) {
      query(`.${className}`).style.filter = `url(#${ids.get(filterId)})`;
    }
    // Start empty; the surrounding app owns remote media and its cache.
    const portraitLayer = query(".portrait-layer");
    portraitLayer.setAttribute("aria-hidden", "true");
    query(".portrait-canvas").hidden = true;
    query(".portrait-fallback").hidden = true;
    host.replaceChildren(card);

    let disposed = false;
    let layoutFrame = 0;
    const updateLayout = () => {
      if (disposed) return;
      fitProfileFactValues();
      fitSkillNames();
      updateCardHeight();
      syncFrame();
    };
    const scheduleLayout = () => {
      if (disposed || layoutFrame) return;
      layoutFrame = requestAnimationFrame(() => {
        layoutFrame = 0;
        updateLayout();
      });
    };
    const setStaticMode = (enabled) => {
      if (disposed) return;
      const isStatic = Boolean(enabled);
      card.classList.toggle("encyclopedia-ark2--static", isStatic);
      card.dataset.portraitMode = isStatic ? "static" : "dynamic";
      card.dataset.staticMode = String(isStatic);
      // Freeze CSS effects at their authored phase; SVG motion also needs its
      // own pause because SMIL is independent from CSS animation-play-state.
      for (const node of card.querySelectorAll(".badge-ring, .starfield__star")) {
        node.style.animationPlayState = isStatic ? "paused" : "running";
      }
      if (isStatic) {
        frameSvg.setCurrentTime?.(0);
        frameSvg.pauseAnimations?.();
      } else {
        frameSvg.unpauseAnimations?.();
      }
    };

    renderStarfield();
    renderCharacter(data, activeTranslations);
    updateLayout();
    setStaticMode(staticMode);
    const resizeObserver = "ResizeObserver" in window ? new ResizeObserver(scheduleLayout) : null;
    resizeObserver?.observe(card);
    resizeObserver?.observe(characterSkills);
    window.addEventListener("resize", scheduleLayout);
    scheduleLayout();

    return {
      card,
      setStaticMode,
      dispose() {
        if (disposed) return;
        setStaticMode(true);
        disposed = true;
        resizeObserver?.disconnect();
        window.removeEventListener("resize", scheduleLayout);
        if (layoutFrame) cancelAnimationFrame(layoutFrame);
        layoutFrame = 0;
        card.remove();
      }
    };
  }

  window.LucimaEncyclopediaCard = Object.freeze({ mount });
})();
