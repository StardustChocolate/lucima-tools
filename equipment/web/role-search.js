/* 两处角色搜索共用：原文优先，其次按不带声调的拼音匹配。 */
(function(root,factory){
  if(typeof module==='object'&&module.exports)module.exports=factory(require('./vendor/pinyin-pro-3.29.5.js'));
  else root.EquipmentRoleSearch=factory(root.pinyinPro);
})(typeof globalThis!=='undefined'?globalThis:this,function(pinyin){
  const profiles=new WeakMap();
  const normalize=value=>String(value||'').normalize('NFKC').toLowerCase().replace(/\s/g,'');
  const roman=value=>normalize(value).replace(/ü|u:/g,'v').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9]/g,'');
  function syllables(value){
    if(!pinyin?.pinyin)return [];
    return pinyin.pinyin(normalize(value),{toneType:'none',type:'array',nonZh:'consecutive',v:true}).map(roman).filter(Boolean);
  }
  function readings(value){
    if(!pinyin?.polyphonic)return syllables(value).map(s=>[s]);
    return pinyin.polyphonic(normalize(value),{toneType:'none',type:'array'}).map(row=>row.map(roman).filter(Boolean)).filter(row=>row.length);
  }
  function sharesReading(parts,query){
    return parts.some((_,start)=>start+query.length<=parts.length&&query.every((sounds,i)=>sounds.some(sound=>parts[start+i].includes(sound))));
  }
  function profile(role){
    let cached=profiles.get(role);
    if(!cached){
      const fields=[role.name,role.searchName,role.staticID,...(role.aliases||[])].filter(Boolean);
      cached={raw:fields.map(normalize),sounds:fields.map(syllables),readings:fields.map(readings)};
      profiles.set(role,cached);
    }
    return cached;
  }
  function find(roles,value){
    const query=normalize(value);if(!query)return roles.slice();
    const tokens=syllables(query),han=/[\u3400-\u9fff\uf900-\ufaff]/u.test(query);
    const spaced=` ${tokens.join(' ')} `,joined=roman(query);
    const allHan=/^[\u3400-\u9fff\uf900-\ufaff]+$/u.test(query),alternatives=allHan?readings(query):[];
    return roles.map((role,index)=>{
      const keys=profile(role);
      if(keys.raw.some(text=>text.includes(query)))return {role,index,rank:0};
      if(allHan&&keys.readings.some(parts=>sharesReading(parts,alternatives)))return {role,index,rank:1};
      if(tokens.length&&keys.sounds.some(parts=>{
        if(han)return ` ${parts.join(' ')} `.includes(spaced);
        // 全拼从音节开头匹配，避免把 ao 错配到 mao 中间；同时支持拼音首字母。
        return !!joined&&(parts.some((_,i)=>parts.slice(i).join('').startsWith(joined))||parts.map(p=>p[0]).join('').includes(joined));
      }))return {role,index,rank:1};
      return null;
    }).filter(Boolean).sort((a,b)=>a.rank-b.rank||a.index-b.index).map(row=>row.role);
  }
  return {find};
});
