/* 多角色自动配装：同一批次的装备互斥由服务器校验。 */
(() => {
  const $ = id => document.getElementById(id);
  let globalBound = false;
  const SLOTS = ['weapon', 'helmet', 'armor', 'necklace', 'ring', 'boots'];
  const SLOT_CN = {weapon:'武器',helmet:'头盔',armor:'铠甲',necklace:'项链',ring:'戒指',boots:'鞋子'};
  const PANEL_SHOW = ['Attack','Defence','HP','Speed','CriticalRate','CriticalDamageRate','EffectHitRate','ResistanceRate'];
  const SECONDARY = ['Speed','ResistanceRate','EffectHitRate'];
  const STAT_SETS = {HP:'Health',Attack:'Attack',Defence:'Defense',Speed:'Speed',CriticalRate:'Critical',CriticalDamageRate:'Destruction',EffectHitRate:'Hit',ResistanceRate:'Resist'};
  const st = {account:null,data:null,busy:false,loading:false,syncQueued:false,generation:0,entries:[],batch:null,searchId:null,view:'loadout',opts:{onlyMax:true,includeWorn:false}};
  const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!=null)n.textContent=text; return n; };
  const btn = (text, cls, fn) => {const b=el('button',cls,text);b.type='button';b.addEventListener('click',LucimaActionWait.wrap(fn));return b;};
  const icon = name => {const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),use=document.createElementNS('http://www.w3.org/2000/svg','use');svg.setAttribute('class','eqp-icon');svg.setAttribute('aria-hidden','true');svg.setAttribute('viewBox','0 0 24 24');use.setAttribute('href',`#${name}`);svg.append(use);return svg;};
  const statMeta = id => (st.data?.stats||[]).find(s=>s.id===id)||{name:id};
  const setMeta = id => (st.data?.sets||[]).find(s=>s.id===id)||{name:id,count:2};
  const fmt = (s,v) => v==null?'—':`${v}${statMeta(s).percent?'%':''}`;
  const roleOf = id => (st.data?.roles||[]).find(r=>r.id===id);
  const EXCLUSION_KEY='ark_equipment_role_exclusions_v1';
  function readExclusions(){try{const value=JSON.parse(frontendStorageGet(EXCLUSION_KEY)||'{}');return value&&typeof value==='object'&&!Array.isArray(value)?value:{};}catch{return {};}}
  function roleExclusionIds(roleId){const ids=readExclusions()[st.account]?.[roleId];return Array.isArray(ids)?[...new Set(ids.filter(id=>typeof id==='string'&&id!==roleId&&roleOf(id)))]:[];}
  function storeExclusions(entry){const saved=readExclusions(),account={...(saved[st.account]||{})};account[entry.roleId]=[...new Set(entry.excludedRoleIds||[])];saved[st.account]=account;frontendStorageSet(EXCLUSION_KEY,JSON.stringify(saved));}
  const excludedRoles = entry => (entry.excludedRoleIds||[]).map(id=>({id,name:roleOf(id)?.name||id}));
  const objectiveName = e => e.mode==='advisor'?'方向参考分':e.objective==='survival'?'生存评分':'输出评分';
  const score = (c,e) => e.mode==='advisor'?(c.ranking==='speed-first'?String(c.panel.Speed):c.ranking==='hit-first'?`${c.panel.EffectHitRate}%`:Number(c.advisorScore||0).toFixed(1)):Math.round(c.metrics[e.objective]).toLocaleString();
  const runLabel = () => st.entries.some(e=>e.mode==='advisor')?'生成推荐':'开始配装';
  const normalized = s => String(s||'').toLocaleLowerCase().replace(/\s/g,'');
  const findRoles = (roles,query) => window.EquipmentRoleSearch?.find(roles,query)||roles.filter(r=>[r.name,r.searchName,r.staticID,...(r.aliases||[])].some(v=>normalized(v).includes(normalized(query))));
  const focusKey = (node,key) => {node.dataset.focusKey=key;return node;};
  const freshEntry = () => ({roleId:'',query:'',sets:[],conditions:[],excludedRoleIds:[],criticalTarget:100,objective:'damage',secondary:[],previewPick:null,resultSort:[],mode:'conditions',profileId:null,depth:'standard',hitTarget:100,advisor:{sets:[],conditions:[],objective:'survival'}});
  function focusEntry(index,selector='.eqp-search input'){$('eqpContent')?.querySelectorAll('.eqp-entry')[index]?.querySelector(selector)?.focus({preventScroll:true});}
  function addEntry(){if(st.busy||!st.data||st.entries.length>=Math.min(6,st.data.roles.length))return;st.entries.push(freshEntry());paintAll();const card=$('eqpContent').querySelector('.eqp-entry:last-of-type');card?.scrollIntoView({block:'nearest'});focusEntry(st.entries.length-1);}
  function configSection(title,content,note=''){
    const box=el('div','eqp-config-section'),heading=el('h3');heading.append(icon({'配装目标':'nav-crosshair','套装要求':'nav-layout-dashboard','属性范围':'nav-settings'}[title]||'nav-settings'),title);if(title==='套装要求')heading.append(el('span','eqp-section-hint','未选套装会比较耗时'));box.append(heading);if(note)box.append(el('p','eqp-note',note));box.append(content);return box;
  }
  function confirmAction(title,message,confirmLabel){
    const dialog=$('eqpDialog'),generation=st.generation,trigger=document.activeElement;
    const restoreFocus=confirmed=>{
      if(generation!==st.generation)return;
      const parent=trigger?.closest('details');
      if(parent&&!parent.open&&!confirmed)parent.open=true;
      (parent&&!parent.open?parent.querySelector('summary'):trigger)?.focus?.({preventScroll:true});
    };
    if(typeof dialog.showModal!=='function')return typeof openConfirmDialog==='function'?openConfirmDialog({title,message,confirmLabel,tone:'warning',returnFocus:trigger}).then(confirmed=>{restoreFocus(confirmed);return confirmed&&generation===st.generation;}):Promise.resolve(false);
    if(dialog.open)return Promise.resolve(false);
    $('eqpDialogTitle').textContent=title;$('eqpDialogMessage').textContent=message;$('eqpDialogConfirm').textContent=confirmLabel;
    return new Promise(resolve=>{
      const close=()=>{
        dialog.removeEventListener('close',close);
        restoreFocus(dialog.returnValue==='confirm');
        resolve(dialog.returnValue==='confirm'&&generation===st.generation);
      };
      dialog.addEventListener('close',close);dialog.returnValue='';dialog.showModal();$('eqpDialogCancel').focus();
    });
  }
  function status(text,tone='idle'){ if(!$('eqpStatus'))return; $('eqpStatus').textContent=text;$('eqpStatus').dataset.tone=tone;$('eqpStatus').parentElement.dataset.tone=tone; }
  function busy(){
    if(!$('eqpControls'))return;
    $('eqpControls').disabled=st.busy;
    $('eqpControls').setAttribute('aria-busy',String(st.busy));
    $('eqpTakeOffAll').disabled=st.busy||!st.account||!st.data?.equips?.some(e=>e.role);
    $('eqpNewRound').disabled=st.busy||!st.account||!st.data;
    $('eqpCancelSearch').hidden=!(st.busy&&st.searchId);
    $('eqpCancelSearch').disabled=!(st.busy&&st.searchId);
    const run=$('eqpRun');if(run){run.textContent=st.busy?'正在计算…':runLabel();run.setAttribute('aria-label',runLabel());}
  }
  async function call(action,extra={},label='操作'){
    if(!st.account||st.busy)return null;
    const generation=st.generation, account=st.account;
    st.busy=true;busy();status(`${label}中，请稍候…`,'running');
    try{
      const completeSearch=['autoMulti','chooseMulti'].includes(action);
      let value=await api('/api/equipment/action',{account,action,...extra,...(completeSearch?{background:true}:{})});
      if(generation!==st.generation){
        if(completeSearch&&value.searchId)api('/api/equipment/action',{account,action:'cancelSearch',searchId:value.searchId}).catch(()=>{});
        return null;
      }
      if(completeSearch&&value.searchId){
        st.searchId=value.searchId;busy();
        while(true){
          await new Promise(resolve=>setTimeout(resolve,750));
          if(generation!==st.generation){
            api('/api/equipment/action',{account,action:'cancelSearch',searchId:value.searchId}).catch(()=>{});
            return null;
          }
          let job;
          try{job=await api('/api/equipment/action',{account,action:'searchStatus',searchId:value.searchId});}
          catch(error){if(error instanceof TypeError){status('连接暂时中断，正在重新获取计算进度…','running');continue;}throw error;}
          if(generation!==st.generation)continue;
          if(job.status==='done'){value=job.result;break;}
          if(job.status==='failed')throw Error(job.error||'配装计算失败');
          if(job.status==='cancelled'){status('已停止筛选，未返回未完成的方案。');return null;}
          const p=job.progress||{}, elapsed=job.elapsed||0;
          const duration=elapsed<60?`${elapsed} 秒`:`${Math.floor(elapsed/60)} 分 ${elapsed%60} 秒`;
          status(job.status==='cancelling'?'正在停止筛选…':p.mode==='advisor'?`自动推荐：角色 ${p.roleIndex||1}/${p.roleCount||st.entries.length} · ${p.profileName||'比较配装方向'}，已比较 ${(p.evaluated||0).toLocaleString()} 套完整装备，已用时 ${duration}。`:`完整计算中：角色 ${p.roleIndex||1}/${p.roleCount||st.entries.length}，已检查 ${(p.nodes||0).toLocaleString()} 个分支，找到 ${p.speedCount||0} 个速度档，已用时 ${duration}。完成后显示全部方案。`,'running');
        }
      }
      if(completeSearch&&value.entries?.some(r=>!r.error&&!r.searchComplete))throw Error('完整计算尚未完成，未返回部分结果，请重新开始');
      if(generation!==st.generation)return null;
      if(value.overview)st.data=value.overview;
      return value;
    }catch(error){if(generation===st.generation)status(`${label}未完成：${error.message||error}`,'error');return null;}
    finally{if(generation===st.generation){st.busy=false;st.searchId=null;busy();if(st.syncQueued&&currentSection==='equipment'){st.syncQueued=false;setTimeout(()=>load(true),0);}}}
  }
  function inputNumber(value,onChange,{min=0,max,step='any',label=''}={}){
    const n=el('input');n.type='number';n.min=min;if(max!=null)n.max=max;n.step=step;n.value=value;n.required=true;n.setAttribute('aria-label',label);
    n.addEventListener('input',()=>onChange(n.value));return n;
  }
  function field(text,control){const l=el('label','eqp-field');l.append(el('span','eqp-label',text),control);return l;}
  function itemCard(e, { compact = false, actions = [], entry = null } = {}) {
    const card = el('div', 'eqp-item' + (compact ? ' compact' : ''));
    if (e.img) { const img = el('img'); img.src = e.img; img.alt = ''; img.loading = 'lazy'; card.append(img); }else{const art=el('span','eqp-item-art');art.append(icon('ui-mysterious-realm'));card.append(art);}
    const body = el('div', 'eqp-item-body');
    const title = el('div', 'eqp-item-title');
    if (e.setIcon) { const si = el('img', 'eqp-seticon'); si.src = e.setIcon; si.alt = ''; title.append(si); }
    title.append(`${e.name || e.staticID} +${e.enhance || 0}`);
    body.append(title);
    body.append(el('div', 'eqp-item-meta', [e.setCN, e.mainProp ? `${e.mainProp.label}${e.mainProp.text}` : '', `副分${e.auxScore ?? '-'}`].filter(Boolean).join(' · ')));
    if (!compact) body.append(el('div', 'eqp-item-subs', (e.subProps || []).map(s => `${s.label}${s.text}`).join('  ')));
    if (e.role) {
      const owner=el('div','eqp-item-owner',`穿戴中：${e.roleName || '其他角色'}`);
      if(st.opts.includeWorn&&entry&&entry.roleId!==e.role&&!excludedRoles(entry).some(r=>r.id===e.role)){
        const protect=btn('排除该角色装备','eqp-mini',ev=>{ev.stopPropagation();excludeRole(entry,e.role);});
        protect.setAttribute('aria-label',`排除${e.roleName||'该角色'}的装备`);owner.append(protect);
      }
      body.append(owner);
    }
    card.append(body);
    for (const [text, fn] of actions) card.append(btn(text, 'eqp-mini', ev => { ev.stopPropagation(); fn(); }));
    return card;
  }

  function panelRow(panel, compare) {
    const row = el('div', 'eqp-panel');
    for (const s of PANEL_SHOW) {
      const cell = el('div', 'eqp-stat');
      cell.dataset.stat=s;
      cell.append(el('span', '', statMeta(s).name));
      const v = el('strong', '', fmt(s, panel?.[s]));
      cell.append(v);
      if (compare && panel && compare[s] != null && panel[s] !== compare[s]) {
        const d = Math.round((panel[s] - compare[s]) * 10) / 10;
        cell.append(el('em', d > 0 ? 'up' : 'down', `${d > 0 ? '↑ +' : '↓ '}${d}${statMeta(s).percent ? '%' : ''}`));
      }
      row.append(cell);
    }
    return row;
  }

  function pieces(sets) { return sets.reduce((n, id) => n + setMeta(id).count, 0); }
  function setEditor(cfg, onChange) {
    const wrap = el('div', 'eqp-sets');
    const chosen = el('div', 'eqp-chosen');
    const fours=cfg.sets.filter(id=>setMeta(id).count===4),twos=cfg.sets.filter(id=>setMeta(id).count===2);
    chosen.append(el('span','eqp-label','已选套装'));
    if(!cfg.sets.length)chosen.append(el('span','eqp-dim','不限（6 个部位都可任意套装）'));
    cfg.sets.forEach((id,i)=>chosen.append(btn(`${setMeta(id).name} ${setMeta(id).count}件 ×`,'eqp-chip on',()=>{cfg.sets.splice(i,1);onChange();})));
    wrap.append(chosen);
    const names=ids=>ids.map(id=>`${setMeta(id).name}${setMeta(id).count}件`).join('、');
    let rule='';
    if(fours.length)rule=`${names(fours)}中的任一套，与${twos.length?names(twos)+'中的任一套':'任一两件套'}组合（4＋2）`;
    else if(twos.includes('Immunity'))rule=`免疫2件固定，与${twos.length>1?names(twos.filter(id=>id!=='Immunity')):'其他两件套'}任二组合，可重复（2＋2＋2）`;
    else if(twos.length)rule=`${names(twos)}任三组合，可重复（2＋2＋2）`;
    if(rule)wrap.append(el('p','eqp-note eqp-set-combination-rule',rule+'；比较所有组合的最高生存或输出。'));
    for(const count of [4,2]){
      const row=el('div','eqp-setrow');row.append(el('span','eqp-label',`${count}件套（多选）`));
      for(const item of (st.data?.sets||[]).filter(item=>item.count===count)){
        const selected=cfg.sets.includes(item.id);
        const b=btn('','eqp-chip'+(selected?' picked':''),()=>{cfg.sets=selected?cfg.sets.filter(id=>id!==item.id):[...cfg.sets,item.id];onChange();});
        const img=el('img');img.src=item.icon;img.alt='';b.append(img,item.name);b.title=item.desc;b.setAttribute('aria-pressed',String(selected));row.append(b);
      }
      wrap.append(row);
    }
    return wrap;
  }


  function roleSearch(entry){
    const wrap=el('div','eqp-search'),searchbox=el('div','eqp-searchbox'),input=el('input');
    input.type='search';input.placeholder='输入角色名、别称或拼音';input.setAttribute('aria-label','搜索角色名或别称');input.autocomplete='off';input.value=entry.query||'';
    const results=el('div','eqp-search-results'),note=el('p','eqp-note');
    const clear=btn('清除','eqp-mini',()=>{input.value='';entry.query='';paint();input.focus();});clear.setAttribute('aria-label','清除角色搜索');
    const select=r=>{entry.roleId=r.id;entry.profileId=null;entry.resultViewProfileId=null;entry.excludedRoleIds=roleExclusionIds(r.id);entry.wornPlan=null;entry.query='';entry.editingRole=false;paintAll();focusEntry(st.entries.indexOf(entry),'.eqp-mode-button');};
    const paint=()=>{
      results.replaceChildren();clear.hidden=!input.value;
      const eligible=(st.data?.roles||[]).filter(r=>!st.entries.some(e=>e!==entry&&e.roleId===r.id));
      const query=input.value.trim(),matches=query?findRoles(eligible,query):eligible;
      const limit=query?60:12;
      note.textContent=query?`找到 ${matches.length} 个角色 · Enter 选择首项，↓ 浏览结果`:`可选角色 ${eligible.length} 个 · 点击选择，也可输入名称搜索`;
      for(const r of matches.slice(0,limit)){
        const b=btn('','eqp-role',()=>select(r));
        if(r.avatar){const img=el('img');img.src=r.avatar;img.alt='';img.loading='lazy';b.append(img);}
        const text=el('span');text.append(el('strong','',r.name),el('small','',`Lv${r.level} · ${r.worn?`已穿戴 ${r.worn} 件`:'未穿戴'}${r.aliases?.length?` · ${r.aliases.join(' / ')}`:''}`));
        b.append(text);results.append(b);
      }
      if(!matches.length)results.append(el('p','eqp-note',query?'没有匹配角色，试试别称或拼音。':'本轮已选完所有可用角色。'));
      if(matches.length>limit)results.append(el('p','eqp-note',`显示前 ${limit} 个，请输入更多文字缩小范围。`));
    };
    input.addEventListener('input',()=>{entry.query=input.value;paint();});
    input.addEventListener('keydown',ev=>{
      if(ev.isComposing)return;
      if(['ArrowDown','Enter'].includes(ev.key)){const first=results.querySelector('button');if(first){ev.preventDefault();if(ev.key==='Enter')first.click();else first.focus();}}
      if(ev.key==='Escape'){ev.preventDefault();if(input.value){input.value='';entry.query='';paint();}else if(entry.roleId){entry.editingRole=false;paintAll();focusEntry(st.entries.indexOf(entry),'[data-action=edit-role]');}}
    });
    results.addEventListener('keydown',ev=>{
      const nodes=[...results.querySelectorAll('button')],index=nodes.indexOf(document.activeElement);
      if(index<0)return;
      if(ev.key==='ArrowDown'||ev.key==='ArrowUp'){ev.preventDefault();const next=index+(ev.key==='ArrowDown'?1:-1);if(next<0)input.focus();else nodes[Math.min(next,nodes.length-1)]?.focus();}
      if(ev.key==='Escape'){ev.preventDefault();input.focus();}
    });
    searchbox.append(input,clear);wrap.append(searchbox,note,results);paint();return wrap;
  }
  function rankEditor(e){
    const box=el('div','eqp-rank-editor'), row=el('div','eqp-actions');
    const objective=el('div','eqp-objectives');objective.setAttribute('role','group');objective.setAttribute('aria-label','主评分目标');
    for(const [value,name] of [['damage','输出优先'],['survival','生存优先']]){
      const choice=btn(name,'eqp-objective'+(e.objective===value?' active':''),()=>{
        if(e.objective===value)return;
        if(value==='damage'){
          e.survivalCriticalCondition=e.conditions.find(c=>c.stat==='CriticalRate');
          e.conditions=e.conditions.filter(c=>c.stat!=='CriticalRate');
        }else if(e.survivalCriticalCondition){
          e.conditions.push(e.survivalCriticalCondition);
        }
        e.objective=value;e.conditionPicker=null;paintAll();
      });
      choice.textContent='';choice.setAttribute('aria-label',name);choice.append(el('strong','',name),el('small','',value==='damage'?'攻击与暴击收益':'生命与防御收益'));
      choice.setAttribute('aria-pressed',String(e.objective===value));objective.append(choice);
    }
    const objectiveField=el('div','eqp-field');objectiveField.append(el('span','eqp-label','优先优化什么'),objective);row.append(objectiveField);
    if(e.objective==='damage'){
      // 输出优先在左、生存优先在右；只有输出优先需要设置暴击率。
      const critical=inputNumber(e.criticalTarget,v=>e.criticalTarget=v,{max:100,label:'暴击率'});
      row.append(field('暴击率（0～100）',critical));
    }
    box.append(row,el('p','eqp-note',e.objective==='damage'?'暴击率必须达到填写值；不达标的组合不参与配装。0 表示自带必暴、不限暴击率；爆伤评分封顶 350%。':'生存评分只由生命和防御计算；需要满暴生存时，在下方筛选条件中添加暴击率。'));
    const second=el('div','eqp-secondary');
    for(const s of SECONDARY){
      const label=el('label','eqp-check'),check=el('input');check.type='checkbox';check.checked=e.secondary.includes(s);
      check.addEventListener('change',LucimaActionWait.wrap(()=>{e.secondary=check.checked?[...e.secondary,s]:e.secondary.filter(x=>x!==s);paintAll();}));label.append(check,statMeta(s).name+'优先');
      const group=el('div','eqp-actions');group.append(label);
      second.append(group);
    }
    box.append(el('strong','eqp-label','额外排序优先级（可多选）'),second,
      el('p','eqp-note',e.secondary.length?`排序：${e.secondary.map(s=>statMeta(s).name).join(' → ')} → ${objectiveName(e)}，均从高到低；优先属性相同时，再比较主评分。`:'未勾选时，直接按主评分从高到低排序。'));
    box.append(el('p','eqp-note',`同速度仅保留${objectiveName(e)}最高的 1 套；不同速度的方案按上述规则排序。`));
    return box;
  }
  function goalEditor(entry){
    const box=el('div','eqp-goal-editor'),modes=el('div','eqp-mode-options');modes.setAttribute('role','group');modes.setAttribute('aria-label','配装方式');
    for(const [id,name] of [['conditions','按条件配装'],['advisor','自动推荐配装']]){
      const button=btn(name,'eqp-mode-button'+(entry.mode===id?' active':''),()=>{entry.mode=id;entry.conditionPicker=null;entry.previewPick=null;paintAll();});button.setAttribute('aria-pressed',String(entry.mode===id));modes.append(button);
    }
    box.append(modes);
    if(entry.mode!=='advisor'){box.append(rankEditor(entry));return box;}
    const profiles=roleOf(entry.roleId)?.recommendations||[];
    if(!profiles.some(profile=>profile.id===entry.profileId))entry.profileId=profiles[0]?.id||null;
    box.append(el('p','eqp-note','选择主要用途后生成推荐；切换用途需要重新生成。使用上方当前羁绊和装备来源，按角色顺序分配；不会自动穿戴。'));
    const choices=el('div','eqp-profile-grid');choices.setAttribute('role','group');choices.setAttribute('aria-label','推荐配装方向');
    for(const profile of profiles){const button=btn('','eqp-profile-option'+(profile.id===entry.profileId?' active':''),()=>{entry.profileId=profile.id;paintAll();});button.dataset.profileId=profile.id;button.setAttribute('aria-pressed',String(profile.id===entry.profileId));button.append(el('strong','',profile.title),el('small','',profile.description));choices.append(button);}
    if(!profiles.length)choices.append(el('p','eqp-warn','角色方向资料尚未读取，请重新进入装备页同步。'));
    box.append(choices);
    const selected=profiles.find(profile=>profile.id===entry.profileId);
    if(selected?.hitTarget){
      const target=el('div','eqp-hit-target'),row=el('label','eqp-hit-target-label'),value=inputNumber(entry.hitTarget??100,v=>entry.hitTarget=v,{label:'推荐最低状态命中（%）'});value.max='500';value.required=true;focusKey(value,`hitTarget-${st.entries.indexOf(entry)}`);
      row.append(el('strong','','最低状态命中（%）'),value);const presets=el('div','eqp-actions');presets.setAttribute('role','group');presets.setAttribute('aria-label','命中门槛快捷选择');
      for(const amount of [0,50,100,150,200]){const button=btn(amount?`${amount}%`:'不设最低','eqp-mini'+(Number(entry.hitTarget??100)===amount?' active':''),()=>{entry.hitTarget=amount;paintAll();});button.dataset.hitTarget=amount;button.setAttribute('aria-pressed',String(Number(entry.hitTarget??100)===amount));presets.append(button);}
      value.addEventListener('input',()=>presets.querySelectorAll('button').forEach(button=>{const active=value.value!==''&&Number(value.value)===Number(button.dataset.hitTarget);button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));}));
      target.append(row,presets,el('p','eqp-note','默认 100% 为可调配装门槛。根据敌方抗性调整；命中不提升技能自身的发生率，也不保证无视免疫、闪避或抵抗。0 表示不设最低命中。'));box.append(target);
    }
    const proof=recommendationProof(selected);if(proof)box.append(proof);
    const depth=el('div','eqp-actions');depth.setAttribute('role','group');depth.setAttribute('aria-label','推荐深度');
    for(const [id,name] of [['standard','标准推荐'],['deep','深度推荐']]){const button=btn(name,'eqp-mini'+(entry.depth===id?' active':''),()=>{entry.depth=id;paintAll();});button.setAttribute('aria-pressed',String(entry.depth===id));depth.append(button);}box.append(depth,el('p','eqp-note','按用途比较面板属性，具体要求见用途说明；参考分不代表实战伤害。深度推荐会探索更多组合，可能耗时更长。'));
    const advanced=el('details','eqp-advisor-advanced'),summary=el('summary','',`额外限制（可选）${entry.advisor.sets.length||entry.advisor.conditions.length?' · 已设置':''}`);advanced.open=!!entry.advisorOpen;advanced.addEventListener('toggle',()=>{if(advanced.isConnected)entry.advisorOpen=advanced.open;});
    advanced.append(summary,configSection('套装要求',setEditor(entry.advisor,paintAll),'不选即不限套装，推荐会自行比较。'),configSection('属性范围',conditions(entry.advisor,entry),'填写后，所有推荐方向均须同时满足这些条件。'));
    box.append(advanced);return box;
  }
  function recommendationProof(profile){
    if(!profile)return null;
    const wrap=el('div','eqp-profile-context');if(profile.caution)wrap.append(el('p','eqp-warn',profile.caution));
    if(profile.evidence?.length){
      const details=el('details','eqp-profile-proof'),summary=el('summary','','查看用途依据与触发条件');details.append(summary);
      for(const item of profile.evidence){
        if(!item||typeof item!=='object')continue;
        const skill=`技能${String(item.rootSkill||item.skillId||'').split('S').pop()}`,notes=[skill,item.label,item.requiresHit?'需状态命中':item.ignoreResistance?'所列效果无视状态抗性':'对己方效果不需状态命中'];
        if(item.conditional)notes.push('需满足技能触发条件');if(item.soulOnly)notes.push(`须烧魂 ${item.soulCost} 点`);if(item.criticalTrigger)notes.push('须触发暴击');if(item.rate<100)notes.push(`技能自身发生率 ${item.rate}%`);
        details.append(el('p','eqp-note',notes.join(' · ')));
      }
      wrap.append(details);
    }
    return wrap.childNodes.length?wrap:null;
  }
  function conditionStats(e){return (st.data?.stats||[]).filter(s=>s.id!=='PinchRate'&&(e.objective==='survival'||s.id!=='CriticalRate'));}
  function statButton(stat,cls,onClick){
    const b=btn('','eqp-chip '+cls,onClick),icon=setMeta(STAT_SETS[stat]).icon;
    b.dataset.stat=stat;
    if(icon){const img=el('img');img.src=icon;img.alt='';b.append(img);}
    b.append(statMeta(stat).name);return b;
  }
  function conditions(e,owner=e){
    const wrap=el('div','eqp-conds'),available=conditionStats(e);
    const picker=current=>{
      const list=el('div','eqp-condition-picker');list.setAttribute('role','group');list.setAttribute('aria-label','选择筛选属性');
      for(const s of available){
        const selected=current?.stat===s.id,used=e.conditions.some(c=>c!==current&&c.stat===s.id);
        const choice=statButton(s.id,selected?'picked':'',()=>{
          if(current)current.stat=s.id;
          else e.conditions.push({stat:s.id,min:'',max:''});
          e.conditionPicker=null;paintAll();
          const card=$('eqpContent').querySelectorAll('.eqp-entry')[st.entries.indexOf(owner)];
          card?.querySelector(`.eqp-stat-trigger[data-stat="${s.id}"]`)?.closest('.eqp-cond')?.querySelector('input')?.focus({preventScroll:true});
        });
        choice.disabled=used;choice.setAttribute('aria-pressed',String(selected));list.append(choice);
      }
      return list;
    };
    e.conditions.forEach((c,i)=>{
      if(!available.some(s=>s.id===c.stat))return;
      const row=el('div','eqp-cond'),values=el('div','eqp-cond-values'),open=e.conditionPicker===c;
      const sel=statButton(c.stat,'eqp-stat-trigger'+(open?' picked':''),()=>{e.conditionPicker=open?null:c;paintAll();});
      sel.append(el('span','eqp-chevron',open?'⌃':'⌄'));sel.setAttribute('aria-label',`修改筛选属性：${statMeta(c.stat).name}`);sel.setAttribute('aria-expanded',String(open));
      const unit=statMeta(c.stat).percent?'（%）':'';
      const lo=inputNumber(c.min??'',v=>c.min=v,{label:`${statMeta(c.stat).name}最低值${unit}`}),hi=inputNumber(c.max??'',v=>c.max=v,{label:`${statMeta(c.stat).name}最高值${unit}`});lo.required=false;hi.required=false;lo.placeholder='最低';hi.placeholder='最高';
      const error=el('p','eqp-range-error');error.setAttribute('aria-live','polite');error.id=`eqpRangeError-${st.entries.indexOf(owner)}-${i}`;lo.setAttribute('aria-describedby',error.id);hi.setAttribute('aria-describedby',error.id);
      const validate=()=>{const invalid=lo.value!==''&&hi.value!==''&&Number(lo.value)>Number(hi.value);const message=invalid?'最低值不能高于最高值，请调整属性范围。':'';lo.setCustomValidity(message);hi.setCustomValidity(message);lo.setAttribute('aria-invalid',String(invalid));hi.setAttribute('aria-invalid',String(invalid));error.textContent=message;error.hidden=!invalid;};
      lo.addEventListener('input',validate);hi.addEventListener('input',validate);validate();
      const remove=btn('×','eqp-mini eqp-remove-condition',()=>{e.conditions.splice(i,1);if(e.conditionPicker===c)e.conditionPicker=null;paintAll();});remove.setAttribute('aria-label',`删除${statMeta(c.stat).name}筛选条件`);
      values.append(sel,lo,el('span','eqp-dim','～'),hi,remove);row.append(values,error);if(open)row.append(picker(c));wrap.append(row);
    });
    const add=btn('+ 筛选条件','eqp-mini',()=>{e.conditionPicker=e.conditionPicker==='add'?null:'add';paintAll();});
    add.disabled=available.every(s=>e.conditions.some(c=>c.stat===s.id));add.setAttribute('aria-expanded',String(e.conditionPicker==='add'));
    wrap.append(add);if(e.conditionPicker==='add')wrap.append(picker(null));
    wrap.append(el('p','eqp-note',e.objective==='survival'?'所有属性范围须同时满足；满暴生存可添加暴击率。排序优先级不限制属性范围。':'所有属性范围须同时满足；暴击率在上方设置。留空表示不限。'));return wrap;
  }
  function planActions(res,e,index,pick){
    const actions=el('div','eqp-plan-actions'),wear=btn(res.done&&pick===res.pick?'已穿上':'确认穿上','primary',()=>equipEntry(index));
    const waiting=st.batch.entries.slice(0,index).some(r=>r.results?.length&&!r.done),current=pick===res.pick;
    wear.disabled=res.done||waiting||!current;
    const save=el('div','eqp-save-inline');save.append(el('span','eqp-label','保存并穿上'));
    for(let slot=0;slot<4;slot++){const button=btn(String(slot+1),'eqp-mini',()=>saveEntry(index,slot));button.setAttribute('aria-label',`保存到方案 ${slot+1} 并穿上`);button.disabled=waiting||!current;save.append(button);}
    actions.append(wear,save);
    if(waiting)actions.append(el('p','eqp-warn','请先完成前面角色的穿戴。'));
    if(!current)actions.append(el('p','eqp-note','已确认的配装不可改选，此方案仅供查看。'));
    return actions;
  }
  function chosenCard(c,res,e,index,pick){
    const box=el('div','eqp-combo-body'),rating=el('div','eqp-score');
    rating.append(el('span','eqp-score-label',res.advisor?c.metricLabel||objectiveName(e):objectiveName(e)),el('strong','eqp-score-value',score(c,e)),el('span','eqp-note',`副分 ${c.aux??'—'}`));
    box.append(rating,panelRow(c.panel,roleOf(e.roleId)?.current?.panel||res.current?.panel));
    const sets=c.sets.map(x=>setMeta(x).name).join(' + ')||'无';box.append(el('p','eqp-note',`套装：${sets}`));
    if(res.advisor)box.append(el('p','eqp-note',`更换 ${c.changes??0} 件 · 借用 ${c.borrowed??0} 件。${c.ruleNote||(c.ranking==='speed-first'?'先满足暴击率 100%，再按速度从高到低；同速度比较输出。':'方向参考分用于比较属性组合，不是实战伤害。')}羁绊保持上方当前穿戴。`));
    box.append(planActions(res,e,index,pick));
    const grid=el('div','eqp-grid'),map=new Map((st.data.equips||[]).map(x=>[x.id,x]));
    for(const [k,slot] of SLOTS.entries()){
      const item=map.get(c.equipIds[slot]),cell=el('div','eqp-slot'),head=el('span','eqp-slot-head');cell.dataset.slot=slot;
      head.append(el('span','eqp-slot-index',String(k+1).padStart(2,'0')),SLOT_CN[slot]);cell.append(head);
      if(item)cell.append(itemCard(item,{entry:e}));else cell.append(el('span','eqp-note','装备数据待同步'));
      grid.append(cell);
    }
    box.append(grid);return box;
  }
  function comboHead(combo,rank,onClick,entry){
    const head=btn('','eqp-combo-head',onClick),keys=el('span','eqp-combo-keys');
    head.append(el('span','eqp-rank',rank));
    for(const stat of PANEL_SHOW){const cell=el('span','eqp-combo-stat');cell.dataset.stat=stat;cell.append(el('small','',statMeta(stat).name),el('strong','',fmt(stat,combo.panel[stat])));keys.append(cell);}
    const meta=el('span','eqp-combo-meta');meta.append(el('span','eqp-combo-sets',combo.sets.map(id=>setMeta(id).name).join(' + ')||'无套装'),el('span','eqp-dim',`副分 ${combo.aux??'—'}`));head.append(keys,meta);return head;
  }
  function currentEquipment(entry){
    const role=roleOf(entry.roleId);if(!role)return null;
    const box=el('div','eqp-current'),header=el('div','eqp-current-head'),quick=el('div','eqp-quick-plans');
    header.append(el('strong','eqp-label',`当前穿戴 ${role.current?.count??role.worn??0}/6 件`));quick.append(el('span','eqp-label','穿戴'));
    for(let slot=0;slot<4;slot++){const wear=btn(String(slot+1),'eqp-mini'+(entry.wornPlan===slot?' active':''),()=>wearSavedPlan(entry,slot));wear.setAttribute('aria-label',`穿戴预存方案 ${slot+1}`);quick.append(wear);}
    const off=btn('卸下','eqp-mini',()=>takeOffRole(entry.roleId));off.setAttribute('aria-label','卸下该角色装备');off.disabled=!role.worn;quick.append(off);header.append(quick);
    const meta=el('div','eqp-current-meta');if(role.current?.aux!=null)meta.append(el('span','eqp-note',`副分 ${role.current.aux}`));
    meta.append(el('span','eqp-note eqp-current-sets',`套装：${role.current?((role.current.sets||[]).map(id=>setMeta(id).name).join(' + ')||'无生效套装'):'待同步'}`));
    box.append(header,panelRow(role.current?.panel),meta,artifactControl(entry,role));if(role.currentWarning)box.append(el('p','eqp-warn',role.currentWarning));return box;
  }
  function artifactControl(entry,role){
    const box=el('div','eqp-artifact'),head=el('div','eqp-artifact-head'),identity=el('div','eqp-artifact-identity'),current=role.artifact;
    identity.append(el('span','eqp-label','当前羁绊'),el('strong','eqp-artifact-name',current?`${current.name} · Lv${current.level}`:'未装备羁绊'));
    const picker=el('div','eqp-artifact-picker');picker.hidden=true;
    const toggle=btn('更换羁绊','eqp-mini',()=>{picker.hidden=!picker.hidden;toggle.setAttribute('aria-expanded',String(!picker.hidden));if(!picker.hidden){paint();input.focus({preventScroll:true});}});
    toggle.setAttribute('aria-expanded','false');head.append(identity,toggle);box.append(head);
    if(current?.bonuses?.length)box.append(el('p','eqp-artifact-bonuses',current.bonuses.join(' · ')));
    box.append(el('p','eqp-note',current?.metadataMissing?'羁绊属性资料暂缺，请更换后再筛选。':'面板与筛选已计入羁绊基础属性及自身常驻加成；战斗触发、条件及队伍光环不计。'));
    const input=el('input');input.type='search';input.placeholder='名称 / 拼音 / 首字母 / 谐音';input.setAttribute('aria-label','搜索羁绊名称、拼音或谐音');
    const results=el('div','eqp-artifact-results');results.setAttribute('aria-label','可用羁绊');
    let limit=12;
    const paint=()=>{
      results.replaceChildren();
      const eligible=(st.data?.artifacts||[]).filter(art=>art.job==='Other'||!!role.job&&art.job===role.job||art.id===current?.id),matches=findRoles(eligible,input.value);
      const ordered=[...matches].sort((a,b)=>Number(b.id===current?.id)-Number(a.id===current?.id));
      for(const art of ordered.slice(0,limit)){
        const chosen=art.id===current?.id,choice=btn('','eqp-artifact-choice'+(chosen?' active':''),()=>replaceArtifact(entry,art.id));
        choice.dataset.artifactId=art.id;choice.disabled=chosen||art.metadataMissing;choice.setAttribute('aria-label',`${chosen?'当前羁绊':'穿戴羁绊'} ${art.name} Lv${art.level}`);
        const row=el('span','eqp-artifact-choice-head');row.append(el('strong','',`${art.name} · Lv${art.level}`),el('small','',chosen?'当前':art.role?`穿戴中：${art.roleName}`:'背包'));choice.append(row);
        choice.append(el('span','eqp-note',`${art.jobName} · ${art.metadataMissing?'属性资料暂缺':(art.bonuses||[]).join(' · ')||'无额外面板加成'}`));
        results.append(choice);
      }
      if(!matches.length)results.append(el('p','eqp-note',input.value.trim()?'未找到匹配的羁绊':'暂无适合此职业的羁绊'));
      if(ordered.length>limit)results.append(btn(`再显示 ${Math.min(12,ordered.length-limit)} 个羁绊`,'eqp-mini',()=>{limit+=12;paint();}));
    };
    input.addEventListener('input',()=>{limit=12;paint();});input.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();picker.hidden=true;toggle.setAttribute('aria-expanded','false');toggle.focus();}});
    picker.append(input,el('p','eqp-note','只显示适合此职业的已有羁绊，点击即穿戴；其他角色使用中的羁绊会转移到此角色。'),results);
    if(current)picker.append(btn('卸下羁绊','eqp-mini',()=>replaceArtifact(entry,'')));
    box.append(picker);return box;
  }
  async function replaceArtifact(entry,artifactId){
    if(st.busy||!entry.roleId)return;
    const generation=st.generation;st.batch=null;st.entries.forEach(other=>{other.previewPick=null;other.wornPlan=null;});
    const value=await call('setArtifact',{roleId:entry.roleId,artifactId},artifactId?'更换羁绊':'卸下羁绊');
    if(generation!==st.generation)return;
    paintAll();if(!value)return;
    status(value.verified?'羁绊已同步，面板已更新；原筛选条件已保留，请重新筛选。':`羁绊更换未确认。${value.error||'请同步账号后重试。'}`,value.verified?'ok':'error');
  }
  function exclusionSummary(entry){
    const box=el('div','eqp-exclusions');box.setAttribute('aria-label',`${roleOf(entry.roleId)?.name||'该角色'}配装排除`);
    const heading=el('div','eqp-exclusions-head');heading.append(el('strong','eqp-label','此角色配装时排除'));
    const picker=el('div','eqp-exclude-picker'),input=el('input'),results=el('div','eqp-search-results');
    picker.hidden=true;input.type='search';input.placeholder='搜索要排除的角色';input.setAttribute('aria-label','搜索要排除的角色');
    const paint=()=>{
      results.replaceChildren();const eligible=(st.data?.roles||[]).filter(role=>role.id!==entry.roleId&&!entry.excludedRoleIds.includes(role.id)),matches=input.value.trim()?findRoles(eligible,input.value):eligible;
      for(const role of matches.slice(0,40)){const choice=btn('','eqp-role',()=>excludeRole(entry,role.id));if(role.avatar){const img=el('img');img.src=role.avatar;img.alt='';choice.append(img);}const text=el('span');text.append(el('strong','',role.name),el('small','',`Lv${role.level} · ${role.worn||0} 件穿戴中`));choice.append(text);results.append(choice);}
      if(!matches.length)results.append(el('p','eqp-note','没有匹配的角色'));
    };
    const add=btn('添加排除角色装备','eqp-mini eqp-add-exclusion',()=>{picker.hidden=!picker.hidden;add.setAttribute('aria-expanded',String(!picker.hidden));if(!picker.hidden){paint();input.focus();}});add.setAttribute('aria-label','添加排除角色装备');add.setAttribute('aria-expanded','false');heading.append(add);box.append(heading);
    const chips=el('div','eqp-exclusion-chips');
    for(const role of excludedRoles(entry)){
      const chip=el('span','eqp-excluded-role',role.name),remove=btn('×','eqp-remove-exclusion',()=>{entry.excludedRoleIds=entry.excludedRoleIds.filter(id=>id!==role.id);storeExclusions(entry);paintAll();});remove.setAttribute('aria-label',`取消排除${role.name}`);chip.append(remove);chips.append(chip);
    }
    if(chips.childNodes.length)box.append(chips);else box.append(el('p','eqp-note','未排除角色。此设置独立保存，新一轮仍保留。'));
    input.addEventListener('input',paint);picker.append(input,results);box.append(picker);return box;
  }
  function resultSortStats(entry){
    if(entry.mode==='advisor'){
      const result=st.batch?.entries[st.entries.indexOf(entry)],profile=selectedProfile(entry,result);
      return [...new Set((profile?.priorities||[]).flatMap(key=>key==='damage'?[profile.damageStat,...(profile.critical?['CriticalRate','CriticalDamageRate']:[])]:[key]))].filter(key=>PANEL_SHOW.includes(key));
    }
    return [...new Set(entry.conditions.filter(condition=>conditionStats(entry).some(stat=>stat.id===condition.stat)&&[condition.min,condition.max].some(value=>value!==''&&value!=null)).map(condition=>condition.stat))];
  }
  function selectedProfile(entry,result){
    if(!result?.advisor)return null;
    const profiles=result.profiles||[];if(!profiles.some(p=>p.id===entry.resultViewProfileId))entry.resultViewProfileId=profiles.find(p=>p.id===entry.profileId)?.id||profiles[0]?.id;
    return profiles.find(p=>p.id===entry.resultViewProfileId);
  }
  function recommendationResults(entry,result){
    const box=el('div','eqp-recommend-results'),choices=el('div','eqp-profile-grid'),active=selectedProfile(entry,result),picked=result.results[result.pick]?.profileId;
    choices.setAttribute('role','group');choices.setAttribute('aria-label','切换推荐方向');
    for(const profile of result.profiles||[]){
      const count=result.profileCounts?.[profile.id]??result.results.filter(combo=>combo.profileId===profile.id).length,button=btn('','eqp-profile-option'+(profile.id===active?.id?' active':''),()=>{
        if(st.busy||profile.id===active?.id)return;
        entry.profileId=profile.id;st.batch=null;
        st.entries.forEach(other=>{other.previewPick=null;other.resultViewProfileId=null;other.resultSort=[];other.candidateLimit=20;});
        paintAll();focusEntry(st.entries.indexOf(entry),`.eqp-profile-option[data-profile-id="${profile.id}"]`);
        status(`已切换为“${profile.title}”，原条件已保留，请点击“生成推荐”重新计算。`);
      });
      button.dataset.viewProfile=profile.id;button.setAttribute('aria-label',`切换为${profile.title}`);button.setAttribute('aria-pressed',String(profile.id===active?.id));button.append(el('strong','',profile.title),el('small','',`${count} 套${picked===profile.id?' · 当前选用':''}`));choices.append(button);
    }
    box.append(el('strong','eqp-label','切换主要用途'),choices,el('p','eqp-note',active?.description||''));if(active?.ruleNote)box.append(el('p','eqp-ok',active.ruleNote));const proof=recommendationProof(active);if(proof)box.append(proof);
    box.append(el('p','eqp-note',`${result.exhaustive?'已检查当前范围内的全部装备组合':'推荐未穷尽全部组合，可尝试深度推荐'}；每方向展示最多 ${result.report?.limitPerProfile||60} 套。切换用途后需重新生成，羁绊沿用上方当前穿戴。`));
    return box;
  }
  function resultSortControls(entry){
    const available=resultSortStats(entry);entry.resultSort=(entry.resultSort||[]).filter(stat=>available.includes(stat));
    if(!available.length)return null;
    const box=el('div','eqp-result-sort'),choices=el('div','eqp-result-sort-options');choices.setAttribute('role','group');choices.setAttribute('aria-label','筛选结果排序');
    box.append(el('strong','eqp-label','结果排序'));
    for(const stat of available){
      const priority=entry.resultSort.indexOf(stat),button=btn(`按${statMeta(stat).name}排序${priority>=0?` ↓ ${priority+1}`:''}`,'eqp-mini'+(priority>=0?' active':''),()=>{
        entry.resultSort=priority>=0?entry.resultSort.filter(value=>value!==stat):[...entry.resultSort,stat];entry.candidateLimit=20;entry.previewPick=null;paintAll();
      });
      button.dataset.sortStat=stat;button.setAttribute('aria-label',`按${statMeta(stat).name}排序`);button.setAttribute('aria-pressed',String(priority>=0));choices.append(button);
    }
    box.append(choices,el('p','eqp-note',entry.resultSort.length?`从高到低：${entry.resultSort.map(stat=>statMeta(stat).name).join(' → ')}；相同则按默认顺序。再次点击可取消。`:'未选择，按原配装规则排序；可按点击顺序选择多个属性，均从高到低。'));return box;
  }
  function orderedResults(result,entry){
    const profile=selectedProfile(entry,result),rows=result.results.map((combo,pick)=>({combo,pick})).filter(row=>!result.advisor||row.combo.profileId===profile?.id);
    if(!entry.resultSort?.length)return rows;
    // 只改变展示顺序，始终用服务器原序号改选、保存与穿戴。
    return rows.sort((a,b)=>{
      for(const stat of entry.resultSort){const difference=Number(b.combo.panel[stat]??0)-Number(a.combo.panel[stat]??0);if(difference)return difference;}
      return a.pick-b.pick;
    });
  }
  function paintResult(card,entry,index){
    const result=st.batch?.entries[index];if(!result)return;
    if(result.error){card.append(el('p','eqp-warn',result.error));return;}
    if(!result.searchComplete){card.append(el('p','eqp-warn','完整计算未完成，请重新开始配装。'));return;}
    if(result.advisor)card.append(recommendationResults(entry,result));
    if(!result.results.length){
      card.append(el('p','eqp-warn',result.unavailableReason||(result.advisor&&!result.exhaustive?'当前推荐范围内未找到满足限制的套装，可修改限制或尝试深度推荐。':'该筛选条件未找到符合要求的套装')));
      const bounds=Object.entries(result.bestPossible||{}).filter(([,value])=>value!=null).map(([stat,value])=>`${statMeta(stat).name}最大值 ${fmt(stat,value)}`).join('，');
      if(bounds)card.append(el('p','eqp-note',`${bounds}。各项最大值不代表可同时达到。`));return;
    }
    const rows=orderedResults(result,entry),bar=el('div','eqp-pickbar');bar.append(el('strong','',`共 ${rows.length} 组方案`),el('span','eqp-note','点击查看详情'));if(result.done)bar.append(el('span','eqp-ok','已穿上'));card.append(bar);
    if(!rows.length){card.append(el('p','eqp-warn','此方向暂未找到符合限制的套装，可切换其他方向或修改限制。'));return;}
    const sorting=resultSortControls(entry);if(sorting)card.append(sorting);
    const list=el('div','eqp-picklist'),limit=entry.candidateLimit||20;
    rows.slice(0,limit).forEach(({combo,pick},rank)=>{
      const expanded=entry.previewPick===pick,row=el('div','eqp-combo'+(expanded?' open':'')),head=comboHead(combo,rank+1,()=>choose(index,pick),entry);
      head.dataset.candidate=pick;head.setAttribute('aria-label',`查看方案 ${rank+1}，速度 ${combo.panel.Speed}`);head.setAttribute('aria-expanded',String(expanded));focusKey(head,`entry-${index}:combo-${pick}`);row.append(head);
      if(result.advisor&&result.pick===pick)head.querySelector('.eqp-combo-meta').append(el('span','eqp-ok','当前选用'));
      if(expanded)row.append(chosenCard(combo,result,entry,index,pick));list.append(row);
    });
    if(rows.length>limit)list.append(btn(`再显示 20 组（剩余 ${rows.length-limit} 组）`,'eqp-mini',()=>{entry.candidateLimit=limit+20;paintAll();}));card.append(list);
  }

  function paintAll(){
    const pane=$('eqpContent');if(!pane)return;
    const active=document.activeElement,previousKey=pane.contains(active)?active.dataset.focusKey:null,scroller=$('section-equipment').closest('.content'),scrollTop=scroller?.scrollTop;
    pane.replaceChildren();pane.classList.toggle('eqp-library-pane',st.view==='rating'||st.view==='library');$('section-equipment').classList.toggle('eqp-has-results',!!st.batch);$('eqpAccount').textContent=st.account||'未登录';
    document.querySelectorAll('#eqpViewTabs button').forEach(button=>{
      const active=button.dataset.view===st.view;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));
    });
    $('eqpAccount').title=st.account||'未登录';
    $('eqpTakeOffAll').hidden=st.view!=='loadout';$('eqpNewRound').hidden=st.view!=='loadout';
    if(!st.data){
      const empty=el('div','eqp-empty-state');empty.append(el('span','eqp-empty-icon','◇'),el('h3','',st.account?'读取装备，开始配装':'登录后开始配装'),el('p','eqp-note',st.account?'进入装备页后会自动同步角色与装备。':'请先在侧栏添加或选择一个已登录的账号。'));pane.append(empty);busy();return;
    }
    if(st.view==='rating'||st.view==='library'){window.EquipmentLibrary.render({pane,data:st.data,account:st.account,panel:st.view==='library'?'list':'overview',onRefresh:()=>load(true),onLock:changeLock});busy();return;}
    const overview=el('section','eqp-overview'),title=el('div','eqp-overview-title');
    const sourceTitle=el('strong','eqp-source-title');sourceTitle.append(icon('nav-layout-dashboard'),'装备来源');
    const counts=el('div','eqp-inventory-metrics');for(const [count,label] of [[st.data.roles.length,'个角色'],[st.data.equips.length,'件装备']]){const metric=el('span','eqp-inventory-metric');metric.append(el('strong','',count),el('span','',label));counts.append(metric);}title.append(sourceTitle,counts);overview.append(title);
    const opts=el('fieldset','eqp-options');opts.disabled=!!st.batch;opts.setAttribute('aria-label','可用装备来源');
    for(const [key,text] of [['onlyMax','只用 +15 装备'],['includeWorn','包含其他角色身上的装备']]){
      const l=el('label','eqp-check'),cb=el('input');cb.type='checkbox';cb.checked=st.opts[key];focusKey(cb,key);cb.addEventListener('change',LucimaActionWait.wrap(()=>{st.opts[key]=cb.checked;paintAll();}));l.append(cb,text);opts.append(l);
    }
    overview.append(opts,el('p','eqp-note','默认使用背包与本轮所选角色的装备。按角色顺序分配，每件装备只用于一个角色。'));
    pane.append(overview);
    if(!st.entries.length){const empty=st.data.roles.length?btn('','eqp-empty-state eqp-add-card',addEntry):el('div','eqp-empty-state');if(st.data.roles.length)empty.setAttribute('aria-label','添加第一个配装角色');empty.append(el('span','eqp-empty-icon','＋'),el('h3','',st.data.roles.length?'先添加一个配装角色':'暂无可配装角色'),el('p','eqp-note','一轮最多 6 个角色，先配装的角色优先选择装备。'));pane.append(empty);}
    for(const [i,e] of st.entries.entries()){
      const role=roleOf(e.roleId),name=role?.name||'待选角色',card=el('section','eqp-entry'),head=el('div','eqp-entry-head');card.setAttribute('aria-label',`第 ${i+1} 位：${name}`);head.append(el('span','eqp-rank',i+1));
      if(role?.avatar){const img=el('img','eqp-avatar');img.src=role.avatar;img.alt='';head.append(img);}else if(role){const avatar=el('span','eqp-avatar eqp-avatar-fallback');avatar.setAttribute('aria-hidden','true');avatar.append(icon('nav-users'));head.append(avatar);}
      const identity=el('div','eqp-entry-identity');identity.append(el('strong','',name),el('small','',role?`Lv${role.level} · 第 ${i+1} 顺位${i===0?'，优先选装备':''}`:'选择本轮需要配装的角色'));head.append(identity);
      if(!st.batch){
        const tools=el('div','eqp-actions eqp-entry-tools');
        if(e.roleId){const edit=btn(e.editingRole?'取消修改':'修改角色','eqp-mini',()=>{e.editingRole=!e.editingRole;e.query='';paintAll();if(e.editingRole)focusEntry(i);});edit.dataset.action='edit-role';tools.append(edit);}
        for(const [label,offset] of [['上移',-1],['下移',1]]){const move=btn(label,'eqp-mini',()=>{[st.entries[i+offset],st.entries[i]]=[st.entries[i],st.entries[i+offset]];paintAll();focusEntry(i+offset,'.eqp-entry-tools button');});move.disabled=i+offset<0||i+offset>=st.entries.length;tools.append(move);}
        tools.append(btn('删除','eqp-mini',()=>{st.entries.splice(i,1);paintAll();$('eqpAdd')?.focus({preventScroll:true});}));head.append(tools);
      }
      card.append(head);
      if(!st.batch&&(!e.roleId||e.editingRole))card.append(roleSearch(e));
      const current=currentEquipment(e);if(current)card.append(current);
      if(!st.batch&&e.roleId){
        if(st.opts.includeWorn)card.append(exclusionSummary(e));
        card.append(configSection('配装目标',goalEditor(e)));
        if(e.mode!=='advisor')card.append(configSection('套装要求',setEditor(e,paintAll),'不选即不限套装；最多选择 6 件，可重复选择 2 件套。'),configSection('属性范围',conditions(e),'可选。只填写你需要限制的属性。'));
      }else if(st.batch&&e.mode!=='advisor')card.append(el('p','eqp-note',`排序：${[...(e.secondary||[]).map(s=>statMeta(s).name),objectiveName(e)].join(' → ')}${e.objective==='damage'?` · 暴击率 ${e.criticalTarget}%`:''}`));
      paintResult(card,e,i);pane.append(card);
    }
    const footer=el('div','eqp-footer'),copy=el('div','eqp-footer-copy'),acts=el('div','eqp-actions');
    if(st.batch){
      acts.append(btn('修改条件','eqp-mini',()=>{st.batch=null;st.entries.forEach(e=>{e.previewPick=null;});paintAll();status('已恢复条件编辑，原条件已保留；已穿上的装备保持当前状态。');}));
    }else{
      const missing=st.entries.filter(e=>!e.roleId).length;copy.append(el('strong','',`${st.entries.length}/6 个配装角色`),el('small','',!st.entries.length?'添加角色后即可配置':missing?`还有 ${missing} 个角色待选择`:'检查配装条件后，开始计算'));
      const add=btn('添加角色','eqp-mini',addEntry);add.id='eqpAdd';add.disabled=st.entries.length>=Math.min(6,st.data.roles.length);
      const go=btn(runLabel(),'primary',run);go.id='eqpRun';go.setAttribute('aria-label',runLabel());go.disabled=!st.entries.length||!!missing||st.entries.some(e=>e.mode==='advisor'&&!e.profileId);acts.append(add,go);
    }
    if(!st.batch)footer.append(copy);footer.append(acts);pane.append(footer);
    const usedKeys=new Map();
    for(const node of pane.querySelectorAll('button,input,select,summary')){
      if(node.dataset.focusKey)continue;
      const entry=node.closest('.eqp-entry'),prefix=entry?`entry-${[...pane.querySelectorAll('.eqp-entry')].indexOf(entry)}`:'global';
      const base=`${prefix}:${node.tagName}:${node.getAttribute('aria-label')||node.textContent||node.type}`,count=usedKeys.get(base)||0;usedKeys.set(base,count+1);focusKey(node,`${base}:${count}`);
    }
    busy();
    if(previousKey){const target=[...pane.querySelectorAll('[data-focus-key]')].find(n=>n.dataset.focusKey===previousKey);target?.focus({preventScroll:true});if(scroller)scroller.scrollTop=scrollTop;}
  }
  async function run(){
    if(st.busy||!st.entries.length||st.entries.some(entry=>!entry.roleId))return;
    if(st.data?.artifactUncertain){status('羁绊状态尚未同步，请重新进入装备页同步后再筛选。','error');return;}
    const invalid=[...$('eqpControls').querySelectorAll('input,select')].find(node=>!node.checkValidity());
    if(invalid){invalid.reportValidity();status('请先修正标出的数值，再开始配装。','error');return;}
    const serialize=cfg=>cfg.conditions.filter(condition=>conditionStats(cfg).some(stat=>stat.id===condition.stat)&&(condition.min!==''||condition.max!=='')).map(condition=>({...condition,min:condition.min===''?null:Number(condition.min),max:condition.max===''?null:Number(condition.max)}));
    const entries=st.entries.map(entry=>({roleId:entry.roleId,excludedRoleIds:st.opts.includeWorn?entry.excludedRoleIds.filter(id=>roleOf(id)&&id!==entry.roleId):[],...(entry.mode==='advisor'?{mode:'advisor',profileId:entry.profileId,depth:entry.depth,hitTarget:Number(entry.hitTarget??100),setOptions:entry.advisor.sets,conditions:serialize(entry.advisor)}:{setOptions:entry.sets,conditions:serialize(entry),...(entry.objective==='damage'?{criticalTarget:Number(entry.criticalTarget)}:{}),objective:entry.objective,secondary:entry.secondary})}));
    const hasAdvisor=st.entries.some(entry=>entry.mode==='advisor');
    const value=await call('autoMulti',{entries,...st.opts},hasAdvisor?'生成角色配装推荐':'完整搜索配装');if(!value){paintAll();return;}
    st.batch=value;st.entries.forEach(entry=>{entry.previewPick=null;entry.candidateLimit=20;entry.resultViewProfileId=null;});paintAll();
    const count=value.entries.filter(result=>result.results?.length).length;
    status(count?(hasAdvisor?`已生成推荐：${count}/${value.entries.length} 个角色找到方案，可切换用途方向并选择装备。`:`已完成计算：${count}/${value.entries.length} 个角色找到方案，点击缩略方案查看详情。`):(hasAdvisor?'当前推荐未找到符合限制的完整套装，请查看角色提示。':'该筛选条件未找到符合要求的套装'),count===value.entries.length?'ok':'error');
  }
  async function choose(index,pick){
    if(st.busy||!st.batch)return;
    const entry=st.entries[index],result=st.batch.entries[index];
    if(entry.previewPick===pick){entry.previewPick=null;paintAll();focusEntry(index,`[data-candidate="${pick}"]`);return;}
    const locked=st.batch.entries.slice(index).some(row=>row.done);
    if(pick!==result.pick&&!locked){
      const value=await call('chooseMulti',{batchId:st.batch.batchId,index,pick},'切换方案并重算后续角色');if(!value)return;st.batch=value;
      st.entries.slice(index+1).forEach(other=>{other.previewPick=null;});status('已改选，后续角色已按剩余装备重新计算。','ok');
    }
    entry.previewPick=pick;paintAll();focusEntry(index,`[data-candidate="${pick}"]`);
  }
  async function wearSavedPlan(entry,planIndex){
    if(st.busy||!entry.roleId)return;
    const generation=st.generation;st.batch=null;st.entries.forEach(other=>{other.previewPick=null;other.wornPlan=null;});
    const value=await call('equipSavedPlan',{roleId:entry.roleId,planIndex},`穿戴预存方案 ${planIndex+1}`);
    if(generation!==st.generation)return;
    if(value?.verified)entry.wornPlan=planIndex;paintAll();if(!value)return;
    status(value.verified?`已穿戴方案 ${planIndex+1}，当前属性已同步。`:`方案 ${planIndex+1} 未全部穿戴完成，已刷新当前属性。${value.error||''}`,value.verified?'ok':'error');
  }
  async function equipEntry(index){const value=await call('equipMulti',{batchId:st.batch.batchId,index},'确认穿戴');if(!value)return;st.batch=value;paintAll();status('服务器已确认穿戴。','ok');}
  async function saveEntry(index,planIndex){
    const value=await call('saveMultiPlan',{batchId:st.batch.batchId,index,planIndex},`保存到方案 ${planIndex+1} 并穿上`);if(!value)return;
    st.batch=value;paintAll();status(value.saved?.verified?`方案 ${planIndex+1} 已保存并穿上（已回读确认）。`:`方案 ${planIndex+1} 已提交，但回读内容不一致，未继续穿戴。`,value.saved?.verified?'ok':'error');
  }
  async function changeLock({equipId,locked,allMax=false}){
    const value=await call(allMax?'lockAllMax':'setLock',{equipId,locked:allMax?true:locked},allMax?'一键锁+15装备':locked?'装备加锁':'装备解锁');
    paintAll();if(!value)return;
    status(value.detail,value.verified?'ok':'error');
    if(typeof showMessageToast==='function')showMessageToast(value.detail,value.verified?'ok':'err');
  }
  async function takeOffAll(){
    if(st.busy||!st.account)return;
    const count=st.data?.equips?.filter(e=>e.role).length||0;
    if(!await confirmAction('卸下所有角色的装备？',`将卸下所有角色当前穿戴的 ${count} 件装备，并使本轮候选方案失效。角色配装条件会保留。`,'确认卸下'))return;
    const generation=st.generation;
    st.batch=null;st.entries.forEach(e=>{e.previewPick=null;});
    const value=await call('takeOffAll',{},'卸下所有角色装备');
    if(generation!==st.generation)return;
    paintAll();if(!value)return;
    if(value.verified)status(`已卸下全部角色装备：${value.removed} 件（涉及 ${value.roleCount} 个角色），已同步确认。`,'ok');
    else status(`卸装未全部完成：已确认卸下 ${value.removed} 件${value.remaining!=null?`，仍有 ${value.remaining} 件穿戴中`:''}。${value.error||'请同步账号后查看。'}`,'error');
  }
  async function takeOffRole(roleId){
    if(st.busy||!st.account)return;
    const generation=st.generation, name=roleOf(roleId)?.name||'当前角色';
    if(!await confirmAction(`卸下${name}的装备？`,'将卸下该角色的全部装备，并使本轮候选方案失效。角色配装条件会保留。','确认卸下'))return;
    st.batch=null;st.entries.forEach(e=>{e.previewPick=null;});
    const value=await call('takeOffRole',{roleId},`卸下${name}装备`);
    if(generation!==st.generation)return;
    paintAll();if(!value)return;
    if(value.verified)status(`已卸下${name}的 ${value.removed} 件装备，已同步确认；可按当前条件重新配装。`,'ok');
    else status(`卸装未全部完成：已确认卸下 ${value.removed} 件${value.remaining!=null?`，该角色仍穿戴 ${value.remaining} 件`:''}。${value.error||'请同步账号后查看。'}`,'error');
  }
  function excludeRole(entry,roleId){
    if(st.busy||!entry.roleId||roleId===entry.roleId||entry.excludedRoleIds.includes(roleId))return;
    entry.excludedRoleIds.push(roleId);storeExclusions(entry);
    st.batch=null;st.entries.forEach(other=>{other.previewPick=null;});paintAll();
    status(`已保存${roleOf(entry.roleId)?.name||'该角色'}的排除设置，重新计算后生效。`,'ok');
  }
  async function newRound(){
    const value=await call('newRound',{},'开始新一轮配装');if(!value)return;
    st.batch=null;st.entries=[];paintAll();status('已开始新一轮配装，请添加角色；各角色的排除设置已保留。','ok');
  }
  async function cancelSearch(){
    if(!st.searchId)return;
    const generation=st.generation;
    $('eqpCancelSearch').disabled=true;
    try{
      await api('/api/equipment/action',{account:st.account,action:'cancelSearch',searchId:st.searchId});
      if(generation===st.generation)status('正在停止筛选…','running');
    }catch(error){if(generation===st.generation){status(`停止筛选未完成：${error.message||error}`,'error');busy();}}
  }
  async function load(fromServer=true){
    if(!st.account||st.loading)return;
    if(st.busy){st.syncQueued=true;return;}
    const generation=st.generation;st.loading=true;
    try{
      const value=await call(fromServer?'reload':'overview',{},fromServer?'自动同步账号':'读取账号装备');if(!value)return;
      st.data=value;st.batch=null;
      st.entries.forEach(entry=>{if(!roleOf(entry.roleId)){entry.roleId='';entry.query='';entry.excludedRoleIds=[];}else entry.excludedRoleIds=roleExclusionIds(entry.roleId).filter(id=>roleOf(id));entry.previewPick=null;entry.wornPlan=null;});
      paintAll();status(`已读取 ${value.roles.length} 个角色、${value.equips.length} 件装备。${st.view==='rating'?'装备库已更新。':st.entries.length?'已保留配装条件。':'添加角色开始配装。'}`,'ok');
    }finally{if(generation===st.generation){st.loading=false;paintAll();}}
  }
  function changed(){
    if(st.account===activeAccount)return;st.generation++;$('eqpDialog')?.close?.();st.account=activeAccount;st.data=null;st.busy=false;st.loading=false;st.syncQueued=false;st.batch=null;st.searchId=null;st.entries=[];paintAll();status(st.account?'进入装备页后自动同步账号。':'请先登录账号。');if(st.account&&currentSection==='equipment')load(true);
  }
  function init(){
    const root=$('section-equipment');if(!root||root.childElementCount)return;
    SECTION_TITLES.equipment='装备';root.innerHTML=`<div class="eqp-view-tabs" id="eqpViewTabs" role="tablist" aria-label="装备功能分类"><button type="button" data-view="loadout" class="active" role="tab" aria-selected="true">配装</button><button type="button" data-view="rating" role="tab" aria-label="战力总览" aria-selected="false">战力</button><button type="button" data-view="library" role="tab" aria-label="装备清单" aria-selected="false">清单</button></div><div class="eqp-head"><span class="eqp-account" id="eqpAccount"></span><div class="eqp-header-ops"><button type="button" id="eqpTakeOffAll" class="eqp-danger" aria-label="一键卸下所有角色装备">一键卸装</button><button type="button" id="eqpNewRound" aria-label="新一轮配装">新一轮配装</button></div></div><div class="eqp-status-row"><div class="eqp-status" id="eqpStatus" role="status" aria-live="polite">请先登录账号。</div><button type="button" id="eqpCancelSearch" hidden>停止筛选</button></div><fieldset id="eqpControls" class="eqp-controls"><div id="eqpContent" class="eqp-pane"></div></fieldset><dialog id="eqpDialog" class="eqp-dialog" aria-labelledby="eqpDialogTitle" aria-describedby="eqpDialogMessage"><form method="dialog"><h3 id="eqpDialogTitle"></h3><p id="eqpDialogMessage"></p><div class="eqp-actions"><button id="eqpDialogCancel" value="cancel">取消</button><button id="eqpDialogConfirm" class="primary" value="confirm">确认</button></div></form></dialog>`;
    if(typeof $('eqpDialog').showModal!=='function')$('eqpDialog').hidden=true;
    root.addEventListener('keydown',ev=>{
      if($('eqpDialog').open){
        if(ev.key==='Tab'){
          const first=$('eqpDialogCancel'),last=$('eqpDialogConfirm');
          if(ev.shiftKey&&document.activeElement===first){ev.preventDefault();last.focus();}
          else if(!ev.shiftKey&&document.activeElement===last){ev.preventDefault();first.focus();}
        }
        return;
      }
    });
    $('eqpCancelSearch').addEventListener('click',LucimaActionWait.wrap(cancelSearch));
    $('eqpNewRound').addEventListener('click',LucimaActionWait.wrap(newRound));
    $('eqpTakeOffAll').addEventListener('click',LucimaActionWait.wrap(takeOffAll));
    root.querySelectorAll('#eqpViewTabs button').forEach(button=>button.addEventListener('click',LucimaActionWait.wrap(()=>{st.view=button.dataset.view||'loadout';paintAll();if(st.data&&!st.busy)status(`已读取 ${st.data.roles.length} 个角色、${st.data.equips.length} 件装备。${st.view==='rating'?'装备战力已更新。':st.view==='library'?'装备清单已更新。':'添加角色开始配装。'}`,'ok');})));
    if(!globalBound){window.addEventListener('lucima:account-change',changed);window.addEventListener('lucima:section-change',ev=>{if(ev.detail?.section==='equipment'&&st.account)load(true);});globalBound=true;}changed();paintAll();
  }
  window.addEventListener('lucima:section-mount',ev=>{if(ev.detail.section==='equipment')init();});
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();
