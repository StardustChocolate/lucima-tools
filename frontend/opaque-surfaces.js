/* Compute solid theme fills only when the user changes theme, never on scroll. */
(() => {
  const entries = [["accent",25],["accent",30],["accent",40],["accent",45],["accent",50],["accent",60],["accent",70],["accent",80],["accent",90],["accent",100],["accent",110],["accent",120],["accent",130],["accent",140],["accent",150],["accent",160],["accent",180],["accent",220],["accent",230],["accent",240],["accent",250],["accent",260],["accent",280],["accent",300],["accent",320],["accent",350],["accent",380],["accent",400],["accent",420],["accent",440],["accent",450],["accent",480],["accent",500],["accent",520],["accent",550],["accent",580],["accent",600],["accent",620],["accent",800],["accent",950],["accent2",120],["accent2",140],["accent2",450],["accent2",500],["accent2",920]];
  function rgb(hex) {
    const text=hex.replace('#','');
    const value=parseInt(text.length===3?text.split('').map(c=>c+c).join(''):text,16);
    return [value>>16&255,value>>8&255,value&255];
  }
  window.LucimaSurfaceColors={opaque(value, surface = 'rgb(13,20,36)') {
    const match=String(value).match(/^rgba\(([^)]+)\)$/);
    if(!match) return value;
    const parts=match[1].split(',').map(Number), base=String(surface).match(/[\d.]+/g)?.map(Number);
    if(parts.length!==4 || !base || base.length<3) return value;
    return '#'+parts.slice(0,3).map((v,i)=>Math.round(v*parts[3]+base[i]*(1-parts[3])).toString(16).padStart(2,'0')).join('');
  }, rule(rgbText) {
    const color=rgbText.split(",").map(Number), declarations=[];
    for(const [mode,base] of [["dark",[13,20,36]],["light",[255,255,255]]]) {
      for(const amount of [500]) {
        const a=amount/1000,value="#"+color.map((v,i)=>Math.round(v*a+base[i]*(1-a)).toString(16).padStart(2,"0")).join("");
        declarations.push(`--solid-rule-${amount}-${mode}:${value}`);
      }
    }
    return declarations.join(";");
  }, apply(theme) {
    const root=document.documentElement, base=theme.mode==='light'?[255,255,255]:[13,20,36];
    root.style.setProperty('--surface', theme.mode==='light' ? '#ffffff' : '#0d1424');
    root.style.setProperty('--surface-subtle', theme.mode==='light' ? '#f3f5fa' : '#111a2c');
    root.style.setProperty('--surface-warning', theme.mode==='light' ? '#fff5e6' : '#2b2215');
    const names=new Set();
    try {
      for (const sheet of document.styleSheets) for (const rule of (sheet.cssRules || [])) {
        const text=rule.cssText || '';
        for (const match of text.matchAll(/--surface-(\d+)-(\d+)-(\d+)-(\d+)/g)) names.add(match[0]);
      }
    } catch { /* stylesheet inspection can be blocked by an embedded WebView */ }
    for (const name of names) {
      const parts=name.slice('--surface-'.length).split('-').map(Number);
      if (parts.length !== 4) continue;
      const alpha=parts[3]/1000;
      const value='#'+parts.slice(0,3).map((v,i)=>Math.round(v*alpha+base[i]*(1-alpha)).toString(16).padStart(2,'0')).join('');
      root.style.setProperty(name,value);
    }
    const tones={accent:rgb(theme.accent),accent2:rgb(theme.accent2)};
    for(const [kind,amount] of entries) {
      const alpha=amount/1000;
      const value='#'+tones[kind].map((v,i)=>Math.round(v*alpha+base[i]*(1-alpha)).toString(16).padStart(2,'0')).join('');
      root.style.setProperty(`--surface-${kind}-${amount}`,value);
    }
  }};
})();
