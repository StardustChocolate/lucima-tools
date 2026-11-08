/* 装备库：只读统计、筛选和互斥配装榜，数据来自账号快照。 */
(() => {
  const SLOTS = ['weapon','helmet','armor','necklace','ring','boots'];
  const NAMES = {weapon:'武器',helmet:'头盔',armor:'铠甲',necklace:'项链',ring:'戒指',boots:'鞋子'};
  const PATHS = {weapon:'M5 19 19 5M13 4h7v7M4 14l6 6M3 21l3-3',helmet:'M4 16v-3a8 8 0 0 1 16 0v3H4Zm3 0v4m10-4v4M9 5V3h6v2',armor:'m8 3-5 4 3 5 2-2v11h8V10l2 2 3-5-5-4-4 3-4-3Z',necklace:'M5 4c0 11 14 11 14 0M12 12l4 5-4 5-4-5 4-5Z',ring:'M9 3h6l2 4-5 4-5-4 2-4Zm-3 9a7 7 0 1 0 12 0',boots:'M8 3h8v10l5 3v5H4v-6l4-2V3Z'};
  const GRADES = ['满配','高配','达标','潜力胚','待提升'];
  const SHORT = {AttackValue:'攻',AttackRate:'攻',DefenceValue:'防',DefenceRate:'防',HPValue:'生',HPRate:'生',SpeedValue:'速',CriticalRate:'暴',CriticalDamageRate:'伤',EffectHitRate:'命',ResistanceRate:'抗'};
  const state = {account:null,panel:'overview',slot:'',enhance:'max',set:'',sort:'score',query:'',grade:''};
  const el = (tag,cls,text) => {const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;};
  const button = (label,cls,fn) => {const n=el('button',cls,label);n.type='button';n.addEventListener('click',LucimaActionWait.wrap(fn));return n;};
  const image = (src,cls) => {const n=el('img',cls);n.src=src;n.alt='';n.loading='lazy';return n;};
  function svgNode(name,attrs={}){const n=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [key,value] of Object.entries(attrs))n.setAttribute(key,value);return n;}
  function slotIcon(slot){const n=document.createElement('img');n.className='eqp-slot-icon';n.src=`/assets/slots/${slot}.png`;n.alt='';n.loading='lazy';return n;}
  function surface(axes,compact=false){
    const host=el('div','eqp-library-three-surface');host.setAttribute('role','img');host.setAttribute('aria-label',axes.map(a=>`${a.label} ${a.value}`).join('，'));
    if(!window.THREE){host.append(el('p','eqp-empty','当前环境未加载 Three.js'));return host;}
    const THREE=window.THREE,plot=el('div','eqp-library-surface-plot'),canvas=document.createElement('canvas'),overlay=el('div','eqp-library-three-labels'),legend=el('div','eqp-library-surface-legend'),hint=el('div','eqp-library-surface-hint'),rotateHint=el('p','eqp-library-rotate-hint','左右拖动可水平旋转查看');plot.tabIndex=0;plot.setAttribute('role','group');plot.setAttribute('aria-label','装备战力曲面，可左右拖动或使用左右方向键水平旋转查看');plot.append(canvas,overlay);host.append(rotateHint,plot,legend,hint);
    const renderer=new THREE.WebGLRenderer({canvas,alpha:true,antialias:true});renderer.setPixelRatio(Math.min(2,window.devicePixelRatio||1));renderer.setClearColor(0x000000,0);renderer.outputEncoding=THREE.sRGBEncoding;
    const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(32,1,.1,100);camera.position.set(3.1,2.55,3.9);camera.lookAt(0,.42,0);
    scene.add(new THREE.AmbientLight(0xffffff,.85));const light=new THREE.DirectionalLight(0xffffff,1.2);light.position.set(2,4,3);scene.add(light);
    const vals=axes.map(a=>Math.max(0,Math.min(1,Number(a.value||0)/Math.max(1,Number(a.target||100))))),ang=vals.map((_,i)=>-Math.PI/2+i*Math.PI/3);
    const centers=ang.map(a=>({x:.78*Math.cos(a),y:.78*Math.sin(a)})),height=(x,y)=>{let strength=0;for(let i=0;i<6;i++){const dx=x-centers[i].x,dy=y-centers[i].y;strength=Math.max(strength,vals[i]*Math.max(0,1-(dx*dx+dy*dy)/(.74*.74))**2);}return .035+strength*1.15;};
    const stops=['#3fe0d0','#4a7bff','#a05bff','#ff7a8a'],maxScore=Math.max(...vals),gradient=(z)=>{const t=maxScore?Math.max(0,Math.min(1,(z-.035)/(1.15*maxScore))):0,k=t*3,i=Math.min(2,Math.floor(k)),u=k-i,a=new THREE.Color(stops[i]),b=new THREE.Color(stops[i+1]);return a.lerp(b,u).convertSRGBToLinear();};
    // Include the six peak coordinates in the mesh, so markers and actual peaks agree exactly.
    const coordinates=key=>[...new Set([...Array.from({length:33},(_,i)=>-1+2*i/32),...centers.map(p=>p[key])])].sort((a,b)=>a-b),xs=coordinates('x'),ys=coordinates('y'),positions=[],colors=[],indices=[];
    for(const y of ys)for(const x of xs){const z=height(x,y),c=gradient(z);positions.push(x,z,y);colors.push(c.r,c.g,c.b);}
    for(let iy=0;iy<ys.length-1;iy++)for(let ix=0;ix<xs.length-1;ix++){const a=iy*xs.length+ix,b=a+1,c=a+xs.length,d=c+1;indices.push(a,b,d,a,d,c);}
    legend.append(el('span','','低 · 0分'),el('i',''),el('span','',`高 · ${(maxScore*100).toFixed(1)}分`));hint.textContent='峰高＝该维度得分（0–100分）；颜色从本图最低点渐变至最高点';
    const geo=new THREE.BufferGeometry();geo.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));geo.setAttribute('color',new THREE.Float32BufferAttribute(colors,3));geo.setIndex(indices);geo.computeVertexNormals();
    const mesh=new THREE.Mesh(geo,new THREE.MeshBasicMaterial({vertexColors:true,side:THREE.DoubleSide}));mesh.userData={kind:'scoreSurface',scores:vals.map(v=>v*100),colorStops:stops,colorRange:[0,maxScore*100]};scene.add(mesh);
    const grid=new THREE.GridHelper(3.2,14,0x4f52a8,0x252957);grid.position.y=-.03;grid.rotation.y=0;scene.add(grid);
    const lines=new THREE.LineSegments(new THREE.EdgesGeometry(geo),new THREE.LineBasicMaterial({color:0xcdeaff,transparent:true,opacity:.3}));scene.add(lines);
    const pointWorld=[];axes.forEach((a,i)=>{const {x,y}=centers[i],z=height(x,y)+.012;const p=new THREE.Vector3(x,z,y);pointWorld.push(p);const dot=new THREE.Mesh(new THREE.SphereGeometry(.032,12,8),new THREE.MeshBasicMaterial({color:0xffffff}));dot.position.copy(p);dot.userData={axis:a.label,score:vals[i]*100};scene.add(dot);});
    // The canvas and labels share the same CSS pixel coordinates, including on narrow screens.
    let disposed=false,yaw=0,frameId=0,drag=null;
    const resize=()=>{
      if(disposed)return;
      if(!host.isConnected){host.dispose?.();return;}
      const w=plot.clientWidth;if(!w)return;
      const h=compact?390:560;plot.style.height=`${h}px`;renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();
      const azimuth=Math.atan2(3.1,3.9)+yaw,radius=Math.hypot(3.1,3.9);camera.position.set(Math.sin(azimuth)*radius,2.55,Math.cos(azimuth)*radius);camera.lookAt(0,.42,0);camera.updateMatrixWorld();
      // Fit the actual surface inside the plot while leaving space for leader labels.
      for(let pass=0;pass<3;pass++){
        let extent=0;const vertex=new THREE.Vector3();
        for(let i=0;i<positions.length;i+=3){vertex.set(positions[i],positions[i+1],positions[i+2]).project(camera);extent=Math.max(extent,Math.abs(vertex.x)/.86,Math.abs(vertex.y)/.7);}
        if(extent<=1)break;
        camera.position.sub(new THREE.Vector3(0,.42,0)).multiplyScalar(extent).add(new THREE.Vector3(0,.42,0));camera.lookAt(0,.42,0);camera.updateMatrixWorld();
      }
      renderer.render(scene,camera);overlay.replaceChildren();
      const frame=svgNode('svg',{width:w,height:h,viewBox:`0 0 ${w} ${h}`,'aria-hidden':'true'});overlay.append(frame);
      const pad=8,labelGap=40;
      const points=pointWorld.map(p=>{const q=p.clone().project(camera);return {x:(q.x*.5+.5)*w,y:(-q.y*.5+.5)*h};});
      const order=points.map((_,i)=>i).sort((a,b)=>points[a].x-points[b].x),rows={left:order.slice(0,3),right:order.slice(3)};
      let topElbow=Infinity;
      for(const group of Object.values(rows)){
        const packed=group.map(i=>points[i].y-32).sort((a,b)=>a-b);
        for(let k=packed.length-2;k>=0;k--)packed[k]=Math.min(packed[k],packed[k+1]-labelGap);
        topElbow=Math.min(topElbow,packed[0]);
      }
      if(topElbow<26){
        const shift=26-topElbow;camera.projectionMatrix.elements[9]+=shift*2/h;
        points.forEach(p=>p.y+=shift);renderer.render(scene,camera);
      }
      for(const side of ['left','right']){
        const group=rows[side].sort((a,b)=>points[a].y-points[b].y);
        const ys=group.map(i=>points[i].y-32);
        // Pack from the bottom upwards so every elbow remains above its marker.
        for(let k=ys.length-2;k>=0;k--)ys[k]=Math.min(ys[k],ys[k+1]-labelGap);
        group.forEach((i,k)=>{
          const p=points[i],dir=side==='left'?-1:1,ky=ys[k];
          const label=svgNode('text',{'class':'eqp-library-surface-label','text-anchor':'middle',y:ky-7});
          const name=svgNode('tspan');name.textContent=axes[i].label+' ';
          const value=svgNode('tspan',{'class':'eqp-library-surface-value'});value.textContent=(vals[i]*100).toFixed(1)+'分';label.append(name,value);frame.append(label);
          const run=Math.max(52,label.getComputedTextLength()+8),low=side==='left'?pad+run:pad,high=side==='left'?w-pad:w-pad-run;
          let kx=Math.max(low,Math.min(high,p.x+dir*22));
          if(Math.abs(kx-p.x)<14)kx=Math.max(low,Math.min(high,p.x-dir*18));
          const tail=kx+dir*run;label.setAttribute('x',(kx+tail)/2);
          const leader=svgNode('polyline',{'class':'eqp-library-surface-leader',points:`${p.x},${p.y} ${kx},${ky} ${tail},${ky}`,'data-axis':axes[i].label});frame.insertBefore(leader,label);
        });
      }
    };
    const schedule=()=>{if(!disposed&&!frameId)frameId=requestAnimationFrame(()=>{frameId=0;resize();});};
    const rotate=value=>{yaw=((value%(Math.PI*2))+Math.PI*2)%(Math.PI*2);plot.dataset.rotation=String(yaw);schedule();};
    const down=event=>{if(!event.isPrimary||event.button!==0)return;drag={id:event.pointerId,x:event.clientX,y:event.clientY,yaw,active:false};plot.setPointerCapture(event.pointerId);};
    const move=event=>{if(!drag||drag.id!==event.pointerId)return;const dx=event.clientX-drag.x,dy=event.clientY-drag.y;if(!drag.active){if(Math.abs(dx)<6||Math.abs(dx)<Math.abs(dy))return;drag.active=true;plot.classList.add('is-dragging');}rotate(drag.yaw-dx*.012);};
    const end=event=>{if(!drag||drag.id!==event.pointerId)return;drag=null;plot.classList.remove('is-dragging');if(plot.hasPointerCapture(event.pointerId))plot.releasePointerCapture(event.pointerId);};
    const keyboard=event=>{if(!['ArrowLeft','ArrowRight','Home'].includes(event.key))return;event.preventDefault();rotate(event.key==='Home'?0:yaw+(event.key==='ArrowLeft'?-1:1)*Math.PI/18);};
    plot.addEventListener('pointerdown',down);plot.addEventListener('pointermove',move);plot.addEventListener('pointerup',end);plot.addEventListener('pointercancel',end);plot.addEventListener('lostpointercapture',end);plot.addEventListener('keydown',keyboard);plot.dataset.rotation='0';schedule();
    const observer=window.ResizeObserver?new ResizeObserver(resize):null;observer?.observe(host);
    host.dispose=()=>{if(disposed)return;disposed=true;cancelAnimationFrame(frameId);observer?.disconnect();plot.removeEventListener('pointerdown',down);plot.removeEventListener('pointermove',move);plot.removeEventListener('pointerup',end);plot.removeEventListener('pointercancel',end);plot.removeEventListener('lostpointercapture',end);plot.removeEventListener('keydown',keyboard);scene.traverse(object=>{object.geometry?.dispose();object.material?.dispose();});renderer.dispose();};
    return host;
  }
  function pie3d(values,labels,colors){
    const host=el('div','eqp-library-three-pie');host.setAttribute('role','img');
    const total=values.reduce((sum,value)=>sum+Math.max(0,Number(value)||0),0);
    host.setAttribute('aria-label',values.map((v,i)=>`${labels[i]} ${v}件，占比${total?(v/total*100).toFixed(1):0}%`).join('，'));
    if(!total){host.append(el('p','eqp-empty','暂无符合条件的装备'));return host;}
    if(!window.THREE){host.append(el('p','eqp-empty','当前环境未加载 Three.js'));return host;}
    const THREE=window.THREE,canvas=el('canvas'),overlay=el('div','eqp-library-three-pie-labels');host.append(canvas,overlay);
    const renderer=new THREE.WebGLRenderer({canvas,alpha:true,antialias:true});renderer.setPixelRatio(Math.min(2,window.devicePixelRatio||1));renderer.setClearColor(0,0);
    const scene=new THREE.Scene(),camera=new THREE.OrthographicCamera(-2.2,2.2,1.6,-1.6,.1,50);
    camera.position.set(0,4.5,7);camera.lookAt(0,.2,0);
    scene.add(new THREE.AmbientLight(0xffffff,.85));const light=new THREE.DirectionalLight(0xffffff,1.05);light.position.set(-3,7,4);scene.add(light);
    let angle=-Math.PI*.18;const points=[];
    values.forEach((raw,i)=>{
      const value=Math.max(0,Number(raw)||0);if(!value)return;
      const share=value/total,end=angle+share*Math.PI*2,mid=(angle+end)/2,depth=(.5+share*25)/45;
      const shape=new THREE.Shape();shape.moveTo(0,0);shape.absarc(0,0,1,angle,end,false);shape.lineTo(0,0);
      const geo=new THREE.ExtrudeGeometry(shape,{depth,bevelEnabled:false,curveSegments:64});
      geo.rotateX(-Math.PI/2);
      const mesh=new THREE.Mesh(geo,new THREE.MeshPhongMaterial({color:colors[i],shininess:40}));
      const offset=.065;mesh.position.set(Math.cos(mid)*offset,0,-Math.sin(mid)*offset);mesh.userData={value,share,height:depth};scene.add(mesh);
      const rim=new THREE.LineSegments(new THREE.EdgesGeometry(geo,25),new THREE.LineBasicMaterial({color:0xffffff,transparent:true,opacity:.4}));mesh.add(rim);
      points.push({index:i,world:new THREE.Vector3(Math.cos(mid)*.72+mesh.position.x,depth+.012,-Math.sin(mid)*.72+mesh.position.z)});angle=end;
    });
    let disposed=false;
    const resize=()=>{
      if(disposed)return;if(!host.isConnected){host.dispose?.();return;}
      const w=host.clientWidth;if(!w)return;const h=200;renderer.setSize(w,h,false);
      const extent=Math.max(1.45,1.65*h/w);camera.left=-extent*w/h;camera.right=extent*w/h;camera.top=extent;camera.bottom=-extent;camera.updateProjectionMatrix();camera.updateMatrixWorld();renderer.render(scene,camera);
      overlay.replaceChildren();const svg=svgNode('svg',{width:w,height:h,viewBox:`0 0 ${w} ${h}`,'aria-hidden':'true'});overlay.append(svg);
      const projected=points.map(q=>{const p=q.world.clone().project(camera);return {...q,x:(p.x*.5+.5)*w,y:(-p.y*.5+.5)*h};});
      for(const side of [-1,1]){
        const rows=projected.filter(p=>(p.x<w/2?-1:1)===side).sort((a,b)=>a.y-b.y);
        rows.forEach((p,i)=>{
          const run=Math.min(72,Math.max(34,w*.24)),pad=5,low=side<0?pad+run:pad,high=side<0?w-pad:w-pad-run;
          let kx=side<0?pad+run:w-pad-run;
          if(Math.abs(kx-p.x)<12)kx=Math.max(low,Math.min(high,p.x-side*14));
          const tail=kx+side*run,cx=(kx+tail)/2;
          const preferred=rows.length===1?p.y+(p.y<h/2?-30:42):rows.length===2?h*(i===0?.28:.76):32+i*(h-64)/(rows.length-1),y=Math.max(28,Math.min(h-24,preferred));
          svg.append(svgNode('polyline',{'class':'eqp-library-pie-leader',points:`${p.x},${p.y} ${kx},${y} ${tail},${y}`,'data-index':p.index}));
          svg.append(svgNode('circle',{cx:p.x,cy:p.y,r:w<220?2.5:3.2,fill:'#fff','data-index':p.index}));
          const value=svgNode('text',{x:cx,y:y-7,'text-anchor':'middle','class':'eqp-library-pie-count','data-index':p.index,style:`font-size:${w<220?14:18}px`});value.textContent=String(values[p.index]);
          const label=svgNode('text',{x:cx,y:y+15,'text-anchor':'middle','class':'eqp-library-pie-caption','data-index':p.index,style:`font-size:${w<220?10:12}px`});label.textContent=labels[p.index];svg.append(value,label);
        });
      }
    };
    requestAnimationFrame(resize);const observer=window.ResizeObserver?new ResizeObserver(resize):null;observer?.observe(host);
    host.dispose=()=>{if(disposed)return;disposed=true;observer?.disconnect();scene.traverse(o=>{o.geometry?.dispose();o.material?.dispose();});renderer.dispose();};
    return host;
  }
  function ruleDetails(){
    const d=el('details','eqp-library-rules');d.append(el('summary','','评分与统计说明'));
    for(const text of [
      '装备分仅计算副属性：攻防生百分比、命中、抗性每1%计1分；暴击率×1.5，暴击伤害×1.1，速度×2。固定攻防生按评分参考角色的基础属性折成百分比。',
      '曲面六轴的原始单位不同：一速是最高副速度，其余为装备件数。先分别除以参考上限30、20、80、80、40、80，最高按100分，再以六项得分的平均值作为综合分。峰高按维度得分，颜色按本图最低至最高峰渐变，图例显示实际范围。',
      '高速：副速度≥16；输出/肉装：装备分≥48且输出/生存分≥30；双效：命中与抗性合计≥30%；半肉：装备分≥48且输出、生存分各≥20。以上仅统计+15装备。',
      '装备质量饼图互不重复：达标48–57分、高配57–62分、满配≥62分；潜力胚为未满强化且装备分≥20或副速度≥4。饼图扇区面积和厚度都随占比增加；速度质量统计副速度14–15、16–18、19–20、21+。',
      '四类配装均为Lv60满觉醒、无羁绊，仅使用现有+15装备。每类分别选出最优完整六部位配装，移除已选六件，再算下一名；不同类别独立计算。同类名次装备互不重复。',
      '速度取前三，沿用最快六部位组合和速度/复仇套常驻加成；命中取前三且速度≥200，抗性取前三且速度≥150。优先6件目标套，再4件目标套+2件生命或防御，最后2件目标套+4件生命/防御；生命、防御必须以2件成套。没有可行配装就只显示已有名次。',
      '生存取前六，只允许生命套、防御套且都必须成套。生存评分＝生命×（1+防御÷300）。各名次显示实际速度、命中%、抗性%或生存分；点击排名卡片后，在该分类下方展示对应的六件装备，每次只展开一套。'
    ])d.append(el('p','',text));return d;
  }
  function rankingList(rankings,data,root){
    const host=el('section','eqp-library-rankings');host.append(el('h4','','装备配装榜'));
    if(!rankings.length){host.append(el('p','eqp-empty','暂无可展示的完整配装'));return host;}
    const colors=['#67d9ff','#ffbc75','#c9a1ff','#7fe0a7'],rows=el('div','eqp-library-ranking-rows'),detail=el('div','eqp-library-build-detail'),sections=new Map();
    detail.id='eqp-library-selected-build';detail.setAttribute('aria-live','polite');
    function choose(category,index){
      const build=category.builds[index];detail.replaceChildren();
      host.querySelectorAll('.eqp-library-ranking-value').forEach(n=>{const selected=n.dataset.key===category.key&&+n.dataset.rank===index;n.setAttribute('aria-pressed',String(selected));n.setAttribute('aria-expanded',String(selected));});
      detail.append(el('strong','',`${category.label} · 第${index+1}名 · ${format(build.value)}${category.unit}`));
      const p=build.panel||{},sets=Array.isArray(build.sets)?build.sets.join('＋'):Object.entries(build.sets||{}).map(([k,v])=>k+'×'+v).join(' · ');
      detail.append(el('p','',`速度 ${format(build.speed??p.Speed)} · 命中 ${format(p.EffectHitRate)}% · 抗性 ${format(p.ResistanceRate)}% · 生命 ${p.HP??'—'} · 防御 ${p.Defence??'—'}${sets?' · '+sets:''}`));
      const gear=el('div','eqp-library-build-gear');
      for(const id of build.equipIds||[]){const e=data.equips.find(x=>x.id===id);if(!e)continue;const b=button('','',()=>details(e,data,root));b.setAttribute('aria-label',`${NAMES[e.slot]}，查看装备详情`);if(e.img)b.append(image(e.img,''));b.append(el('span','',NAMES[e.slot]||e.slotCN),el('small','',e.setCN||''));gear.append(b);}detail.append(gear);sections.get(category.key).append(detail);
    }
    function format(n){return n==null?'—':Number(n).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});}
    rankings.forEach((category,i)=>{
      const row=el('div','eqp-library-ranking-row');row.dataset.key=category.key;row.style.setProperty('--rank-color',colors[i%colors.length]);sections.set(category.key,row);
      row.append(el('b','',`${category.label} · 前${category.limit}`),el('small','',`${category.reference}${category.minSpeed?' · 速度≥'+category.minSpeed:''}`));
      const values=el('div','eqp-library-ranking-values'),builds=category.builds||[];
      builds.forEach((b,j)=>{const value=button('','eqp-library-ranking-value',()=>choose(category,j));value.dataset.key=category.key;value.dataset.rank=j;value.setAttribute('aria-pressed','false');value.setAttribute('aria-expanded','false');value.setAttribute('aria-controls',detail.id);const text=category.key==='survival'&&b.value>=10000?(b.value/10000).toFixed(2)+'万分':format(b.value)+category.unit;value.title=format(b.value)+category.unit;value.append(el('span','',`第${j+1}名`),el('strong','',text));if(category.minSpeed)value.append(el('small','',`速度 ${format(b.speed??b.panel?.Speed)}`));values.append(value);});
      if(!builds.length)values.append(el('span','eqp-library-ranking-empty','暂无满足套装及速度条件的完整配装'));row.append(values);rows.append(row);
    });
    host.append(el('p','eqp-library-ranking-reference','参考角色：Lv60 满觉醒 · 无羁绊 · 仅+15 · 同类名次不重复装备'),el('p','eqp-library-ranking-help','点击可查看对应装备详情'),rows,detail);return host;
  }
  function lockButton(e,root){
    const b=el('button','eqp-library-lock');b.type='button';
    const locked=Boolean(e.locked),svg=svgNode('svg',{viewBox:'0 0 24 24','aria-hidden':'true'});
    svg.append(svgNode('rect',{x:5,y:10,width:14,height:11,rx:2,fill:'none',stroke:'currentColor','stroke-width':2}));
    svg.append(svgNode('path',{d:locked?'M8 10V7a4 4 0 0 1 8 0v3':'M8 10V7a4 4 0 0 1 7.5-2',fill:'none',stroke:'currentColor','stroke-width':2,'stroke-linecap':'round'}));
    b.append(svg);b.dataset.equipLock=e.id;b.setAttribute('aria-pressed',String(locked));
    b.title=locked?'已加锁，点击解锁':'未加锁，点击加锁';b.setAttribute('aria-label',b.title);b.disabled=!root._onLock;
    b.onclick=LucimaActionWait.wrap(event=>{event.stopPropagation();return root._onLock?.({equipId:e.id,locked:!locked});});return b;
  }
  function details(e,data,root){
    const dialog=el('dialog','eqp-dialog eqp-library-detail');dialog.setAttribute('aria-label','装备详情');
    const close=button('关闭','',()=>dialog.close());dialog.append(el('h3','',`${e.name||e.staticID} +${e.enhance||0}`));
    if(e.img)dialog.append(image(e.img,'eqp-library-detail-art'));
    const meta=el('p','eqp-library-detail-meta');if(e.setIcon){const set=image(e.setIcon,'');set.alt=e.setCN||'套装';set.title=e.setCN||'套装';meta.append(set);}meta.append(el('span','',`${e.slotCN||NAMES[e.slot]||''} · Lv${e.level??'—'} · ${e.tierName||''}`));dialog.append(meta);
    dialog.append(el('strong','eqp-library-detail-score',`装备分 ${Number(e.quality?.score||0).toFixed(1)} · ${e.quality?.grade||'待提升'}`));
    const protection=el('div','eqp-library-protection');protection.append(el('span','',e.locked?'已加锁':'未加锁'),lockButton(e,root));dialog.append(protection);
    dialog.append(el('p','',`主属性：${e.mainProp?.label||'—'} ${e.mainProp?.text||''}`));
    const props=el('dl');for(const p of e.subProps||[]){props.append(el('dt','',p.label),el('dd','',p.text));}dialog.append(props,el('p','',`${e.locked?'已锁定':'未锁定'} · ${e.role?`穿戴中：${e.roleName||'其他角色'}`:'未穿戴'}`),close);root.append(dialog);
    const outside=event=>{const rect=dialog.getBoundingClientRect();return event.target===dialog&&(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom);};
    let outsideStart=false;dialog.addEventListener('pointerdown',event=>{outsideStart=outside(event);});dialog.addEventListener('click',LucimaActionWait.wrap(event=>{if(outsideStart&&outside(event))dialog.close();}));
    dialog.addEventListener('close',()=>dialog.remove(),{once:true});if(dialog.showModal)dialog.showModal();else dialog.setAttribute('open','');close.focus();
  }
  function gearCard(e,data,root){
    const card=el('div','eqp-library-card');card.tabIndex=0;card.setAttribute('role','button');card.onclick=LucimaActionWait.wrap(()=>details(e,data,root));card.onkeydown=event=>{if(event.target===card&&['Enter',' '].includes(event.key)){event.preventDefault();details(e,data,root);}};card.setAttribute('aria-label',`${e.setCN||''}${e.slotCN||NAMES[e.slot]||''}，装备分 ${e.quality?.score??0}，查看装备详情`);
    const top=el('div','eqp-library-card-top'),art=el('div','eqp-library-art');if(e.img)art.append(image(e.img,''));else art.append(slotIcon(e.slot));
    art.append(el('span','eqp-library-level',e.level??'—'),el('span','eqp-library-enhance',`+${e.enhance||0}`),lockButton(e,root));
    const owner=(data.roles||[]).find(r=>r.id===e.role);
    const props=el('div','eqp-library-props');props.append(el('strong','eqp-library-main',`${SHORT[e.mainProp?.type]||e.mainProp?.label||'—'} ${e.mainProp?.text||''}`));
    for(const p of e.subProps||[])props.append(el('span','',`${SHORT[p.type]||p.label} ${p.text}`));top.append(art,props);
    const meta=el('span','eqp-library-item-meta');if(owner?.avatar){const portrait=image(owner.avatar,'eqp-library-owner-icon');portrait.alt=owner.name||e.roleName||'穿戴角色';portrait.title=portrait.alt;meta.append(portrait);}
    if(e.setIcon){const set=image(e.setIcon,'eqp-library-set-icon');set.alt=e.setCN||'套装';set.title=e.setCN||'套装';meta.append(set);}meta.append(el('span','eqp-library-item-slot',NAMES[e.slot]||e.slotCN||'未知部位'));
    meta.append(el('span','eqp-library-card-score',`装备分 ${Number(e.quality?.score||0).toFixed(1)} ›`));card.append(top,meta);
    card.dataset.equipId=e.id;card.dataset.grade=e.quality?.grade||'待提升';return card;
  }
  function render({pane,data,account,panel='overview',onRefresh,onLock}){
    if(state.account!==account){Object.assign(state,{account,panel:'overview',slot:'',enhance:'max',set:'',sort:'score',query:'',grade:''});}
    const root=el('div','eqp-library'),body=el('div','eqp-library-body');root._onLock=onLock;root.append(body);pane.append(root);state.panel=panel||'overview';
    function navigate(panel){state.panel=panel;draw();}
    function drill({grade='',enhance='max'}={}){Object.assign(state,{panel:'list',grade,enhance,slot:'',set:'',query:''});draw();}
    function draw(){
      const focus=document.activeElement,key=focus?.dataset.libraryFocus,start=focus?.selectionStart;body.querySelectorAll('.eqp-library-three-surface,.eqp-library-three-pie').forEach(n=>n.dispose?.());body.replaceChildren();
      if(state.panel==='overview')drawOverview();else drawList();
      if(key){const target=[...root.querySelectorAll('[data-library-focus]')].find(n=>n.dataset.libraryFocus===key);target?.focus({preventScroll:true});if(typeof start==='number'&&target?.setSelectionRange)target.setSelectionRange(start,start);}
    }
    function drawOverview(){
      const s=data.library;if(!s){body.append(el('p','eqp-empty','请刷新账号以读取装备库统计'));return;}
      const card=el('section','eqp-library-overview'),head=el('div','eqp-library-title'),heading=el('h3','','装备战力曲面');heading.append(el('small','eqp-library-total','（总分100分）'));head.append(heading);card.append(head);
      const visual=el('div','eqp-library-visual'),meta=el('div','eqp-library-top-meta');meta.append(el('span','',`刷新时间：${new Date(s.updatedAt).toLocaleString()}`),el('strong','eqp-library-power',`综合 ${s.power.toFixed(1)}`));visual.append(meta);
      visual.append(surface(s.axes,true));
      const scores=el('div','eqp-library-axis-scores');
      for(const axis of s.axes){const score=Math.min(100,axis.value/axis.target*100),cell=el('div');cell.append(el('span','',`${axis.label} · ${axis.value}${axis.label==='一速'?'速':'件'} / ${axis.target}`),el('strong','',`${score.toFixed(1)}分`));scores.append(cell);}
      visual.append(scores,el('p','eqp-library-score-formula',`维度得分＝实际值÷参考上限×100（最高100分）\n综合＝六项维度得分平均值＝${s.power.toFixed(1)} / 100`));card.append(visual);
      const pies=el('div','eqp-library-pies'),quality=el('div','eqp-library-pie-card');quality.append(el('h4','','装备质量'),pie3d([Math.max(0,s.qualified-s.small),Math.max(0,s.small-s.large),s.large,s.embryos],['达标','高配','满配','潜力胚'],['#9be34b','#54b9e8','#e7424c','#f4b13b']));
      const speed=el('div','eqp-library-pie-card'),speedRows=s.speedBands||[];speed.append(el('h4','','速度质量'),pie3d(speedRows.map(x=>x.count||0),speedRows.map(x=>x.label),['#9be34b','#54b9e8','#e7424c','#f4b13f']));pies.append(quality,speed);card.append(pies,rankingList(s.rankings||[],data,root));body.append(card,ruleDetails());
    }
    function select(label,value,options,key,change){
      const wrap=el('div');wrap.innerHTML=dropdownHTML('eqp-library-dropdown',options.map(([val,text,src])=>({val,label:`${src?`<img class="eqp-library-option-icon" src="${filterEscape(src)}" alt="">`:''}<span>${filterEscape(text)}</span>`})),value,label);
      const control=wrap.firstElementChild,trigger=control.querySelector('.dd-btn'),menu=control.querySelector('.dd-menu');trigger.setAttribute('aria-label',label);trigger.setAttribute('aria-haspopup','listbox');trigger.dataset.libraryFocus=key;menu.setAttribute('role','listbox');menu.setAttribute('aria-label',label);
      for(const opt of menu.querySelectorAll('.dd-opt')){opt.setAttribute('role','option');opt.setAttribute('aria-selected',String(opt.dataset.val===value));opt.tabIndex=-1;opt.dataset.libraryFocus=key;}
      bindDropdown(control,v=>{change(v);draw();});
      control.addEventListener('keydown',event=>{
        const open=control.classList.contains('open');if(event.key==='Escape'||event.key==='Tab'){if(open)trigger.click();if(event.key==='Escape'){event.preventDefault();trigger.focus();}return;}
        if(!['ArrowDown','ArrowUp','Home','End','Enter',' '].includes(event.key))return;
        if((event.key==='Enter'||event.key===' ')&&document.activeElement!==trigger){event.preventDefault();document.activeElement.click();return;}
        if(event.key==='Enter'||event.key===' ')return;
        event.preventDefault();if(!open)trigger.click();const choices=[...menu.querySelectorAll('.dd-opt')],current=choices.indexOf(document.activeElement),selected=choices.findIndex(n=>n.dataset.val===value);
        const index=event.key==='Home'?0:event.key==='End'?choices.length-1:current<0?Math.max(0,selected):Math.max(0,Math.min(choices.length-1,current+(event.key==='ArrowUp'?-1:1)));choices[index]?.focus({preventScroll:true});if(choices[index])menu.scrollTop=choices[index].offsetTop-menu.clientHeight/2;
      });return control;
    }
    function drawList(){
      const slots=el('div','eqp-library-slots');slots.setAttribute('aria-label','按装备部位筛选');
      const all=button('',state.slot===''?'active':'',()=>{state.slot='';draw();});all.setAttribute('aria-pressed',String(!state.slot));all.setAttribute('aria-label','全部');all.append(el('span','eqp-slot-symbol','✦'),el('b','', '全部'));slots.append(all);
      for(const slot of SLOTS){const b=button('',state.slot===slot?'active':'',()=>{state.slot=slot;draw();});b.setAttribute('aria-label',NAMES[slot]);b.title=NAMES[slot];b.setAttribute('aria-pressed',String(state.slot===slot));b.append(slotIcon(slot),el('b','',NAMES[slot]));slots.append(b);}body.append(slots);
      const toolbar=el('div','eqp-library-filters');toolbar.append(
        select('装备库排序',state.sort,[['score','排序：装备分'],['speed','排序：副速度'],['level','排序：等级'],['enhance','排序：强化']],'sort',v=>state.sort=v),
        select('强化筛选',state.enhance,[['max','强化：+15'],['all','强化：全部'],['unfinished','强化：未满'],['embryo','强化：胚子']],'enhance',v=>state.enhance=v),
        select('套装筛选',state.set,[['','套装：全部'],...(data.sets||[]).map(s=>[s.id,s.name,s.icon])],'set',v=>state.set=v));body.append(toolbar);
      const search=el('input');search.type='search';search.placeholder='搜索装备、套装或穿戴角色';search.setAttribute('aria-label','搜索装备库');search.dataset.libraryFocus='query';search.value=state.query;search.addEventListener('input',()=>{state.query=search.value;draw();});body.append(search);
      let rows=(data.equips||[]).filter(e=>(!state.slot||e.slot===state.slot)&&(!state.set||e.set===state.set)&&(state.enhance==='all'||state.enhance==='max'&&e.enhance>=15||state.enhance==='unfinished'&&e.enhance<15||state.enhance==='embryo'&&e.enhance<15&&(e.quality?.score>=20||e.quality?.speed>=4)));
      if(state.query.trim()){
        const q=state.query.trim().toLocaleLowerCase();
        const owners=new Set((window.EquipmentRoleSearch?.find(data.roles||[],state.query)||[]).map(role=>role.id));
        rows=rows.filter(e=>owners.has(e.role)||[e.name,e.staticID,e.setCN,e.slotCN,e.roleName].some(v=>String(v||'').toLocaleLowerCase().includes(q)));
      }
      if(state.grade)rows=rows.filter(e=>state.grade==='qualified'?e.quality?.score>=48:state.grade==='small'?e.quality?.score>=57:e.quality?.grade===state.grade);
      const value=e=>state.sort==='score'?e.quality?.score||0:state.sort==='speed'?e.quality?.speed||0:Number(e[state.sort]||0);rows.sort((a,b)=>value(b)-value(a)||String(a.id).localeCompare(String(b.id)));
      const caption=el('div','eqp-library-caption');caption.append(el('span','',`共 ${rows.length} 件${state.enhance==='max'?'+15':''}装备 · ${state.sort==='score'?'装备分':state.sort==='speed'?'副速度':state.sort==='level'?'等级':'强化'}从高到低`));if(state.grade)caption.append(button(`${state.grade==='qualified'?'达标+':state.grade==='small'?'高配+':state.grade} ×`,'',()=>{state.grade='';draw();}));if(onLock)caption.append(button('一键锁+15装备','eqp-library-lock-all',()=>onLock({allMax:true})));body.append(caption);
      if(!rows.length){body.append(el('div','eqp-empty-state','暂无符合条件的装备'),button('重置筛选','',()=>{Object.assign(state,{slot:'',set:'',enhance:'all',grade:'',query:''});draw();}));return;}
      const groups=state.sort==='score'?GRADES:['全部'];for(const grade of groups){const items=grade==='全部'?rows:rows.filter(e=>(e.quality?.grade||'待提升')===grade);if(!items.length)continue;const section=el('section','eqp-library-group');section.append(el('h4','',`${grade} · ${items.length}件`));const grid=el('div','eqp-library-grid');items.forEach(e=>grid.append(gearCard(e,data,root)));section.append(grid);body.append(section);}body.append(ruleDetails());
    }
    draw();
  }
  window.EquipmentLibrary={render};
})();
