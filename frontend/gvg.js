/* GVG 双队建议：当前穿戴、账号隔离、整页滑动。 */
(() => {
  let globalBound=false;
  const $=id=>document.getElementById(id), halves=['upper','lower'], names={upper:'上半',lower:'下半'};
  const statNames={Attack:'攻击',Defence:'防御',HP:'生命',Speed:'速度',CriticalRate:'暴击率',CriticalDamageRate:'暴击伤害',EffectHitRate:'状态命中',ResistanceRate:'状态抗性'};
  const sets={Attack:'攻击',Defense:'防御',Health:'生命',Speed:'速度',Critical:'暴击',Destruction:'暴伤',Hit:'命中',Resist:'抗性',Lifesteal:'吸血',Counter:'反击',Pinch:'追击',Immunity:'免疫',Rage:'暴怒',Penetration:'贯穿',Revenge:'复仇',Injury:'创伤',Torrent:'飞流'};
  const st={account:null,generation:0,catalog:null,roster:null,tab:'enemies',draft:null,result:null,jobId:null,busy:false,loading:false,stale:false,activePlan:0,selectedRole:null};
  const clone=x=>JSON.parse(JSON.stringify(x));
  const el=(tag,cls='',text='')=>{const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;};
  const btn=(text,action,cls='')=>{const b=el('button',cls,text);b.type='button';b.addEventListener('click',LucimaActionWait.wrap(action));return b;};
  const note=text=>el('p','gvg-note',text);
  const empty=()=>({staticID:'',speed:{kind:'unknown',context:'panel'},health:{kind:'unknown',context:'max'},artifact:'unknown',artifactLevel:null,stats:{}});
  const blank=()=>({target:'',enemies:{upper:Array.from({length:3},empty),lower:Array.from({length:3},empty)},options:{policy:'manual',goal:'balanced',minLevel:60,locks:{upper:[],lower:[]},excluded:[]}});
  function fixedDraft(draft){for(const half of halves)for(const row of draft.enemies[half])for(const name of ['speed','health']){const old=row[name]||{},value=old.value??old.min??old.max??'';row[name]={kind:value===''?'unknown':'exact',context:old.context||(name==='health'?'max':'panel'),value};}return draft;}
  const key=type=>`ark_gvg_${type}_${st.account||'none'}`;
  function read(type,fallback){try{return JSON.parse(frontendStorageGet(key(type)))||fallback;}catch{return fallback;}}
  function persist(){if(st.account)frontendStorageSet(key('draft'),JSON.stringify(st.draft));}
  function saved(){const v=read('plans',[]);return Array.isArray(v)?v:[];}
  const character=sid=>st.catalog?.characters.find(r=>r.id===sid),owned=id=>st.roster?.roles.find(r=>r.id===id);
  const selected=()=>Object.values(st.draft?.enemies||{}).flat().filter(r=>r.staticID).length;
  function status(text,tone=''){const n=$('gvgStatus');if(!n)return;n.textContent=text;n.dataset.tone=tone;}
  function change(paintNow=false){if(paintNow)document.querySelectorAll('#gvgContent [data-enemy]').forEach(card=>{const[h,i]=card.dataset.enemy.split('-'),disclosure=card.querySelector('details');if(disclosure&&st.draft.enemies[h]?.[i])st.draft.enemies[h][i].moreOpen=disclosure.open;});st.result=null;st.stale=false;st.jobId=null;persist();if(paintNow)paint();else footer();}
  function field(text,control){const n=el('label','gvg-field');n.append(el('span','',text),control);return n;}
  function select(value,choices,label,update){const n=el('select');n.setAttribute('aria-label',label);for(const[id,text]of choices){const op=el('option','',text);op.value=id;n.append(op);}n.value=String(value);n.addEventListener('change',LucimaActionWait.wrap(()=>update(n.value)));return n;}
  function number(value,label,update){const n=el('input');n.type='number';n.min=0;n.step='any';n.inputMode='decimal';n.value=value??'';n.placeholder='选填';n.setAttribute('aria-label',label);n.addEventListener('input',()=>{update(n.value);change();});return n;}
  function details(label,content){const n=el('details','gvg-details');n.append(el('summary','',label),content);return n;}
  function title(r){const box=el('div','gvg-role-title');if(r?.avatar){const img=el('img','gvg-avatar');img.src=r.avatar;img.alt='';img.loading='lazy';box.append(img);}const copy=el('div');copy.append(el('strong','',r?.name||'请选择角色'),el('small','',r?`${r.elementName||''} · ${r.jobName||''}`:''));box.append(copy);return box;}
  function panel(values){const grid=el('div','gvg-stats');for(const[id,label]of Object.entries(statNames)){const c=el('div');c.append(el('small','',label),el('strong','',`${values?.[id]??'—'}${id.endsWith('Rate')&&values?.[id]!=null?'%':''}`));grid.append(c);}return grid;}
  function skills(rows,actual=false){const box=el('div','gvg-skill-list');box.append(note(actual?'等级来自账号；说明可能含图鉴强化区间，未做整场战斗模拟。':'敌方技能等级未知，以下仅说明可能机制，不表示必控或必杀。'));for(const s of rows||[]){const n=el('div');n.append(el('strong','',`${s.id} · ${s.name}${s.hidden?'（追加/隐藏）':''}${s.level!=null?` · Lv${s.level}`:' · 等级未知'}`),note(s.description));box.append(n);}return box;}
  let pickerReturn=null;
  function closePicker(){const d=$('gvgDialog');if(!d)return;if(d.close)d.close();else d.removeAttribute('open');d.hidden=true;if(pickerReturn?.isConnected)pickerReturn.focus({preventScroll:true});}
  function picker(label,rows,choose,disabled=()=>false){const d=$('gvgDialog'),body=$('gvgDialogBody');pickerReturn=document.activeElement;$('gvgDialogTitle').textContent=label;body.replaceChildren();const input=el('input');input.type='search';input.placeholder='名称、别称、拼音或谐音';input.setAttribute('aria-label',label+'搜索');const list=el('div','gvg-picker-list');body.append(input,list);
    function render(){list.replaceChildren();const matches=window.EquipmentRoleSearch.find(rows,input.value);for(const r of matches){const b=btn('',()=>{if(disabled(r))return;closePicker();choose(r);},'gvg-picker-role');b.disabled=disabled(r);b.dataset.roleId=r.id;b.append(title(r));list.append(b);}if(!matches.length)list.append(note('未找到，请尝试正式名、别称或拼音。'));}
    input.addEventListener('input',render);input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();list.querySelector('button:not(:disabled)')?.click();}});render();d.hidden=false;if(d.showModal)d.showModal();else d.setAttribute('open','');input.focus();
  }
  function chooseEnemy(half,index){picker(`选择敌方${names[half]}第${index+1}名角色`,st.catalog.characters,r=>{st.draft.enemies[half][index]={...empty(),staticID:r.id};change(true);},r=>st.draft.enemies[half].some((u,i)=>i!==index&&u.staticID===r.id));}
  function chooseBond(item,role){picker('选择敌方羁绊',[{id:'unknown',name:'未知羁绊'},{id:'none',name:'确认未装备羁绊'},...st.catalog.artifacts.filter(a=>a.job==='Other'||a.job===role.job)],r=>{item.artifact=r.id;item.artifactLevel=null;change(true);});}
  function observation(item,label,prefix,health=false){const input=number(item.value,prefix+label,v=>{item.kind=v===''?'unknown':'exact';item.value=v;delete item.min;delete item.max;if(health&&item.context!=='max'){item.context='max';input.removeAttribute('max');const labelNode=input.closest('.gvg-field')?.querySelector('span');if(labelNode)labelNode.textContent='生命值';}});input.placeholder='选填';if(!health)input.max=2000;else if(item.context==='percent')input.max=100;
    const context=health?({current:'（当前生命记录）',percent:'（百分比记录）',observed:'（观察值记录）'}[item.context]||''):'';return field(label+context,input);
  }
  function enemyCard(half,index,item){
    const card=el('article','gvg-enemy');card.dataset.enemy=`${half}-${index}`;
    const head=el('div','gvg-row');head.append(el('span','gvg-slot',String(index+1)));
    const role=character(item.staticID);
    if(role)head.append(title(role),btn('更换',()=>chooseEnemy(half,index),'gvg-small'));
    else head.append(btn('＋ 选择敌方角色',()=>chooseEnemy(half,index),'gvg-add'));
    card.append(head);if(!role)return card;
    const prefix=names[half]+(index+1),stats=el('div','gvg-enemy-stats');
    const speed=observation(item.speed,'速度',prefix);speed.classList.add('gvg-speed-field');
    stats.append(speed,observation(item.health,'生命值',prefix,true));
    for(const[id,name]of[['Defence','防御'],['ResistanceRate','抗性 %'],['EffectHitRate','命中 %']])
      stats.append(field(name,number(item.stats[id],prefix+name,v=>item.stats[id]=v)));
    const bond=st.catalog.artifacts.find(a=>a.id===item.artifact);
    const row=el('div','gvg-row gvg-bond-row');
    row.append(el('span','gvg-note','羁绊'),btn(bond?.name||(item.artifact==='none'?'未装备羁绊':'选择羁绊（选填）'),()=>chooseBond(item,role),'gvg-grow'));
    card.append(stats,row,details('查看技能与机制',skills(role.skills)));return card;
  }
  function enemies(){const box=el('div'),top=el('div','gvg-row'),target=el('input');target.type='text';target.maxLength=80;target.value=st.draft.target;target.placeholder='对手名称（可选）';target.setAttribute('aria-label','对手名称');target.addEventListener('input',()=>{st.draft.target=target.value;change();});top.append(target,btn('清空敌方',()=>{st.draft.enemies=blank().enemies;change(true);},'gvg-small'));box.append(top,note('上下半各选三名敌人，属性填写固定值，均可选填。生命值填写面板最大生命；我方一速需比敌方一速快超过 10 速，避免乱速反超。'));
    const grid=el('div','gvg-halves');for(const h of halves){const section=el('div','gvg-half');section.append(el('h3','',`敌方${names[h]}`));st.draft.enemies[h].forEach((r,i)=>section.append(enemyCard(h,i,r)));grid.append(section);}box.append(grid);return box;
  }
  function options(){const n=el('div','gvg-option-grid');n.append(field('角色等级',select(st.draft.options.minLevel,[[60,'只用 Lv60'],[50,'Lv50 及以上'],[1,'全部等级']],'角色等级',v=>{st.draft.options.minLevel=Number(v);change(true);})),field('操作方式',select(st.draft.options.policy,[['manual','手动操作建议'],['auto','游戏自动 AI']],'操作方式',v=>{st.draft.options.policy=v;change(true);})),field('推荐目标',select(st.draft.options.goal,[['balanced','两半均衡'],['upper','优先上半'],['lower','优先下半']],'推荐目标',v=>{st.draft.options.goal=v;change(true);})));return n;}
  function roster(){const box=el('div');box.append(options(),note('使用当前装备与羁绊。锁定到某半后另一半不再使用；装备不足或资料异常的角色不会入选。'));const input=el('input');input.type='search';input.placeholder='搜索我方角色、别称、拼音或谐音';input.setAttribute('aria-label','搜索我方角色');const list=el('div','gvg-roster-grid');box.append(input,list);
    function render(){list.replaceChildren();for(const r of window.EquipmentRoleSearch.find(st.roster.roles,input.value)){const card=el('article','gvg-own-role');card.dataset.ownedRole=r.id;const head=el('div','gvg-row');head.append(title(r),el('span','gvg-level',`Lv${r.level}`));card.append(head);const ready=r.ready&&r.level>=st.draft.options.minLevel;card.append(el('p',ready?'gvg-ok gvg-note':'gvg-warn gvg-note',r.unavailable.join('；')||(!ready?'低于所选等级':'当前装备齐全')));const choice=st.draft.options.excluded.includes(r.id)?'excluded':st.draft.options.locks.upper.includes(r.id)?'upper':st.draft.options.locks.lower.includes(r.id)?'lower':'free';
        card.append(select(choice,[['free','自动选择'],['upper','锁定上半'],['lower','锁定下半'],['excluded','排除 / 保留不用']],`设置${r.name}的出战状态`,v=>{for(const h of halves)st.draft.options.locks[h]=st.draft.options.locks[h].filter(id=>id!==r.id);st.draft.options.excluded=st.draft.options.excluded.filter(id=>id!==r.id);if(v==='excluded')st.draft.options.excluded.push(r.id);if(halves.includes(v))st.draft.options.locks[v].push(r.id);change(true);}));
        const more=el('div');if(r.current)more.append(panel(r.current.panel));more.append(note(`套装：${(r.current?.sets||[]).map(s=>sets[s]||s).join('＋')||'无生效套装'}`),note(`羁绊：${r.artifact?`${r.artifact.name} · Lv${r.artifact.level}`:'未装备'}`));if(r.artifact?.effect)more.append(note(r.artifact.effect));more.append(skills(r.skills,true));card.append(details('当前面板、羁绊与实际技能',more));list.append(card);}if(!list.childNodes.length)list.append(note('未找到符合搜索的角色。'));}input.addEventListener('input',render);render();return box;
  }
  function roleDetail(r){
    const card=el('article','gvg-plan-role');card.dataset.detailRole=r.id;
    card.append(title(r),panel(r.current.panel),note(`套装：${(r.current.sets||[]).map(s=>sets[s]||s).join('＋')||'无'}`),
      note(`羁绊：${r.artifact?`${r.artifact.name} · Lv${r.artifact.level}`:'未装备'} · 保持当前穿戴`));
    const tags=el('div','gvg-tags');for(const t of r.tags)if(st.catalog.traitNames[t])tags.append(el('span','',st.catalog.traitNames[t]));
    const more=skills(r.skills,true);if(r.artifact)more.append(note(r.artifact.effect));
    card.append(tags,details('技能与羁绊详情',more));return card;
  }
  function planHalf(h,value){
    const n=el('div','gvg-plan-half');n.dataset.half=h;
    n.append(el('h3','',names[h]));const team=el('div','gvg-team-grid');
    for(const r of value.roles){
      const b=btn('',()=>{st.selectedRole=st.selectedRole===r.id?null:r.id;paint();},'gvg-team-role');
      b.dataset.teamRole=r.id;b.setAttribute('aria-label',`查看${r.name}面板`);b.setAttribute('aria-pressed',String(st.selectedRole===r.id));
      if(r.avatar){const img=el('img');img.src=r.avatar;img.alt='';img.loading='lazy';b.append(img);}
      b.append(el('strong','',r.name));team.append(b);
    }
    n.append(team);
    const speed=value.checks.find(c=>c.id==='panel-speed');
    if(speed&&speed.status!=='pass')n.append(el('p','gvg-speed-alert',speed.reason));
    const order=el('div','gvg-checks');order.append(note(value.strategy),note(value.orderNote));
    for(const step of value.steps)order.append(note(`${step.name}（${step.speed} 速）${value.policy==='manual'?' · '+step.skillIds.join(' / '):''}：${step.advice}`));
    n.append(details('配速与操作思路',order));
    const checks=el('div','gvg-checks');
    for(const c of value.checks){const row=el('div','gvg-check '+c.status);row.append(el('span','',c.status==='pass'?'已有条件':c.status==='fail'?'未满足':'待确认'),el('p','',c.reason));checks.append(row);}
    n.append(details('条件与风险依据',checks));return n;
  }
  async function savePlan(plan){
    if(st.stale||!st.result)return;const generation=st.generation,result=st.result,jobId=st.jobId;
    try{const current=await api('/api/gvg/action',{account:st.account,action:'status',jobId});if(generation!==st.generation||result!==st.result)return;
      if(current.status!=='completed'){if(current.status==='expired'){st.stale=true;paint();}throw Error(current.status==='verifying'?'账号正在同步，请稍后收藏。':'方案已过期或未完成，请刷新后重新生成。');}
      const list=saved(),id=result.inputHash+':'+plan.id,old=list.find(r=>r.id===id),item={id,createdAt:Date.now(),target:result.request.target,request:clone(result.request),inputHash:result.inputHash,snapshotHash:result.snapshotHash,versions:result.versions,plan:clone(plan),outcomes:old?.outcomes||{upper:'unknown',lower:'unknown'}};
      frontendStorageSet(key('plans'),JSON.stringify([item,...list.filter(r=>r.id!==id)].slice(0,12)));status('双队方案已收藏；账号面板变化后会提示重新生成。','ok');
    }catch(e){if(generation===st.generation)status(e.message,'error');}
  }

  async function exportPlan(plan){const generation=st.generation;try{let v=await api('/api/gvg/action',{account:st.account,action:'export',jobId:st.jobId,planId:plan.id});if(generation!==st.generation)return;if(v.content){if(!window.LucimaFiles?.saveEquipmentFilterScheme)throw Error('当前环境缺少系统导出接口');const raw=window.LucimaFiles.saveEquipmentFilterScheme(v.filename,v.content);v=typeof raw==='string'?JSON.parse(raw):raw;if(!v.ok)throw Error(v.detail||'导出失败');}status(`已导出至 ${v.path}`,'ok');}catch(e){if(generation===st.generation)status(e.message,'error');}}
  function results(){
    const box=el('div');if(!st.result){box.append(el('div','gvg-empty','先填写敌方两队，再生成双队建议。'));return box;}
    if(st.stale)box.append(el('p','gvg-warn','账号面板或资料版本已变化，以下方案已过期，请重新生成。'));
    const plans=st.result.plans;
    if(!plans.length){box.append(el('div','gvg-empty',st.result.reason||'本次搜索未找到合适的双队。'));return box;}
    st.activePlan=Math.min(st.activePlan,plans.length-1);
    const nav=el('nav','gvg-plan-nav');nav.setAttribute('aria-label','选择配队方案');nav.append(el('span','','方案'));
    plans.forEach((p,i)=>{const b=btn(String(i+1),()=>{st.activePlan=i;st.selectedRole=null;paint();});b.dataset.planIndex=String(i);b.setAttribute('aria-label',`方案 ${i+1}`);b.setAttribute('aria-pressed',String(st.activePlan===i));nav.append(b);});
    box.append(nav,note('按对局适配、关键机制与风险排序，优先展示更稳妥的双队。实战胜率尚未模拟。'));
    const p=plans[st.activePlan],card=el('article','gvg-plan');card.dataset.planId=p.id;
    const head=el('div','gvg-row');head.append(el('h2','',`方案 ${st.activePlan+1}`),el('span','gvg-badge',st.activePlan===0?'优先推荐':p.status));
    const actions=el('div','gvg-actions'),fav=btn('收藏方案',()=>savePlan(p)),exp=btn('导出 JSON',()=>exportPlan(p));fav.disabled=exp.disabled=st.stale;actions.append(fav,exp);
    card.append(head,note(p.resourceCheck),actions);
    const grid=el('div','gvg-result-halves');for(const h of halves)grid.append(planHalf(h,p.halves[h]));card.append(grid);
    const role=halves.flatMap(h=>p.halves[h].roles).find(r=>r.id===st.selectedRole);
    if(role)card.append(roleDetail(role));else card.append(note('点击上方角色，查看当前面板、套装与羁绊。'));
    box.append(card);
    const d=st.result.diagnostics,diag=el('div');diag.append(note(`可用角色 ${d.availableRoles||0} · 上半核心 ${d.coresExamined?.upper||0} · 下半核心 ${d.coresExamined?.lower||0} · 联合比较 ${d.pairsCompared||0} · 排除资源冲突 ${d.resourceConflicts||0}`),note('搜索设有候选预算，不保证全局最优。'),note(`${st.result.versions.game} · ${st.result.versions.engine}`));box.append(details('查看计算范围',diag));return box;
  }
  function favorites(){const box=el('div'),list=saved();box.append(note('收藏和实战记录按账号保存在本机。记录仅用于复盘，不自动转换成胜率。'));if(!list.length)box.append(el('div','gvg-empty','还没有收藏的双队方案。'));for(const row of list){const card=el('article','gvg-saved'),expired=row.snapshotHash!==st.roster.snapshotHash||JSON.stringify(row.versions)!==JSON.stringify(st.catalog.versions);card.append(el('h3','',row.target),note(new Date(row.createdAt).toLocaleString()+(expired?' · 已过期，请重新生成':' · 当前快照一致')));for(const h of halves)card.append(note(`${names[h]}：${row.plan.halves[h].roles.map(r=>r.name).join(' / ')}`));const actions=el('div','gvg-actions');actions.append(btn('使用此对手重新配队',()=>{st.draft=fixedDraft(clone(row.request));for(const h of halves)st.draft.options.locks[h]=st.draft.options.locks[h].filter(id=>owned(id));st.draft.options.excluded=st.draft.options.excluded.filter(id=>owned(id));st.tab='enemies';change(true);}),btn('删除',()=>{frontendStorageSet(key('plans'),JSON.stringify(list.filter(r=>r.id!==row.id)));paint();},'gvg-danger'));card.append(actions);const outcomes=el('div','gvg-option-grid');for(const h of halves)outcomes.append(field(`${names[h]}实战结果`,select(row.outcomes?.[h]||'unknown',[['unknown','未记录 / 未确认'],['win','胜利'],['loss','失败'],['draw','平局'],['disconnect','断线 / 未结算']],`记录${row.target}${names[h]}结果`,v=>{row.outcomes={...row.outcomes,[h]:v};row.recordedAt=Date.now();frontendStorageSet(key('plans'),JSON.stringify(list));})));card.append(details('记录实战结果',outcomes));box.append(card);}return box;
  }
  function footer(){const n=$('gvgFooter');if(!n)return;n.replaceChildren();if(!st.account)return;const text=el('div','gvg-footer-copy');text.append(el('strong','',st.busy?'正在联合配队…':st.tab==='results'?'双队建议':`敌方角色 ${selected()}/6`),el('small','',st.busy?'可随时停止，原情报会保留':'使用当前装备和羁绊，不自动开战'));const actions=el('div','gvg-actions');if(st.busy)actions.append(btn('停止配队',cancel,'gvg-danger'));else{if(st.tab==='results')actions.append(btn('修改情报',()=>{st.tab='enemies';paint();}));const go=btn(st.tab==='results'?'重新生成':'生成双队建议',run,'gvg-primary');go.id='gvgRun';go.disabled=!st.roster||st.loading||selected()!==6;actions.append(go);}n.append(text,actions);}
  function paint(){const n=$('gvgContent');if(!n)return;n.replaceChildren();$('gvgAccount').textContent=st.account||'未登录';$('gvgRefresh').disabled=st.busy||st.loading||!st.account;const tabs=$('gvgTabs');tabs.replaceChildren();for(const[id,text]of[['enemies','敌方阵容'],['roster','我方角色'],['results','双队建议'],['saved','收藏记录']]){const b=btn(text,()=>{st.tab=id;paint();});b.disabled=st.busy;b.setAttribute('aria-pressed',String(st.tab===id));tabs.append(b);}if(!st.account)n.append(el('div','gvg-empty','请先登录账号，再读取我方角色。'));else if(!st.catalog||!st.roster)n.append(el('div','gvg-empty',st.loading?'正在读取账号角色与当前穿戴…':'尚未读取资料，请点击刷新。'));else n.append(st.tab==='enemies'?enemies():st.tab==='roster'?roster():st.tab==='results'?results():favorites());n.disabled=st.busy||st.loading;footer();}
  async function cancel(){if(!st.jobId)return;const generation=st.generation;try{await api('/api/gvg/action',{account:st.account,action:'cancel',jobId:st.jobId});if(generation===st.generation)status('正在停止配队…');}catch(e){if(generation===st.generation)status(e.message,'error');}}
  async function run(){if(st.busy||!st.roster||selected()!==6)return;const account=st.account,generation=st.generation,requestId=window.crypto?.randomUUID?.()||`gvg-${Date.now()}-${Math.random().toString(36).slice(2)}`;st.busy=true;st.result=null;st.stale=false;st.activePlan=0;st.selectedRole=null;paint();status('正在生成上下半候选并检查六人资源…');try{let job=await api('/api/gvg/action',{account,action:'recommend',requestId,...clone(st.draft)});if(generation!==st.generation){if(job.jobId)api('/api/gvg/action',{account,action:'cancel',jobId:job.jobId}).catch(()=>{});return;}st.jobId=job.jobId;footer();while(['running','queued','cancelling','verifying'].includes(job.status)){await new Promise(resolve=>setTimeout(resolve,400));if(generation!==st.generation)return;job=await api('/api/gvg/action',{account,action:'status',jobId:st.jobId});if(generation!==st.generation)return;const p=job.progress||{};status(job.status==='verifying'?'账号正在同步，等待核对快照…':p.phase==='pairing'?`正在比较双队 · ${p.compared||0} 组 · ${job.elapsed||0} 秒`:`正在筛选${names[p.half]||'上下半'} · ${p.examined||0} 个核心 · ${job.elapsed||0} 秒`);}if(job.status==='cancelled'){status('已停止配队，原情报与设置已保留。');return;}if(job.status==='expired')throw Error('账号装备、技能或羁绊已变化，请刷新后重新生成。');if(job.status!=='completed')throw Error(job.error||'配队未完成');st.result=job.result;st.tab='results';status(`已生成 ${job.result.plans.length} 组完整双队建议。请展开条件与风险依据核对。`,'ok');$('section-gvg').closest('.content').scrollTop=0;}catch(e){api('/api/gvg/action',{account,action:'cancel',...(st.jobId?{jobId:st.jobId}:{requestId})}).catch(()=>{});if(generation===st.generation)status(e.message||String(e),'error');}finally{if(generation===st.generation){st.busy=false;paint();}}}
  async function load(){if(st.loading||st.busy||!st.account)return;const generation=st.generation,account=st.account;st.loading=true;paint();status('读取当前账号角色、装备和羁绊…');try{const[cat,roster]=await Promise.all([st.catalog||api('/api/gvg/catalog'),api('/api/gvg/action',{account,action:'overview'})]);if(generation!==st.generation)return;st.catalog=cat;st.roster=roster;if(st.result)st.stale=st.result.snapshotHash!==roster.snapshotHash||JSON.stringify(st.result.versions)!==JSON.stringify(cat.versions);status(`已读取 ${roster.roles.length} 名角色，其中 ${roster.roles.filter(r=>r.ready&&r.level>=st.draft.options.minLevel).length} 名满足当前装备和等级要求。`,'ok');}catch(e){if(generation===st.generation)status(e.message,'error');}finally{if(generation===st.generation){st.loading=false;paint();}}}
  function accountChanged(force=false){if(st.account===activeAccount&&!force)return;const account=st.account,jobId=st.jobId;if(account&&jobId&&st.busy)api('/api/gvg/action',{account,action:'cancel',jobId}).catch(()=>{});st.generation++;closePicker();st.account=activeAccount;st.roster=null;st.busy=false;st.loading=false;st.result=null;st.jobId=null;st.stale=false;st.tab='enemies';const value=read('draft',null);st.draft=value?.enemies?.upper?.length===3&&value?.enemies?.lower?.length===3&&value?.options?.locks?fixedDraft(value):blank();paint();status(st.account?'进入 GVG 后读取当前账号。':'请先登录账号。');if(currentSection==='gvg')load();}
  function init(){const root=$('section-gvg');if(!root||root.childElementCount)return;root.innerHTML='<div class="gvg-head"><div><h2>GVG配队</h2><small id="gvgAccount"></small></div><button id="gvgRefresh" type="button">刷新资料</button></div><p id="gvgStatus" class="gvg-status" role="status" aria-live="polite"></p><nav id="gvgTabs" class="gvg-tabs" aria-label="GVG 配队步骤"></nav><fieldset id="gvgContent"></fieldset><div id="gvgFooter" class="gvg-footer"></div><dialog id="gvgDialog" class="gvg-dialog" aria-labelledby="gvgDialogTitle" hidden><div class="gvg-row"><h3 id="gvgDialogTitle"></h3><button id="gvgDialogClose" type="button">关闭</button></div><div id="gvgDialogBody"></div></dialog>';
    $('gvgRefresh').addEventListener('click',LucimaActionWait.wrap(load));$('gvgDialogClose').addEventListener('click',LucimaActionWait.wrap(closePicker));$('gvgDialog').addEventListener('cancel',()=>{$('gvgDialog').hidden=true;});$('gvgDialog').addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();closePicker();}if(e.key==='Tab'&&!$('gvgDialog').showModal){const focusable=[...$('gvgDialog').querySelectorAll('button:not(:disabled),input')];if(e.shiftKey&&document.activeElement===focusable[0]){e.preventDefault();focusable[focusable.length-1]?.focus();}else if(!e.shiftKey&&document.activeElement===focusable[focusable.length-1]){e.preventDefault();focusable[0]?.focus();}}});if(!globalBound){window.addEventListener('lucima:account-change',()=>accountChanged(true));window.addEventListener('lucima:section-change',e=>{if(e.detail?.section==='gvg'){accountChanged();load();}});globalBound=true;}accountChanged();paint();if(!st.account)status('请先登录账号。');}
  window.addEventListener('lucima:section-mount',e=>{if(e.detail.section==='gvg')init();});
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();
