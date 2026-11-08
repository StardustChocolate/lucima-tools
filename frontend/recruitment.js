/* Recruitment archive: each entry fetches fresh data; session objects isolate accounts. */
(() => {
  'use strict';
  const sessions = new WeakMap();
  const labels = {normal:'常驻', mystic:'神秘', limited:'限时', galaxy:'银河'};
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const number = value => Number(value || 0).toLocaleString('zh-CN');
  const percent = value => value == null || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(2)}%`;
  const date = (value, short = false) => value ? new Intl.DateTimeFormat('zh-CN', {
    timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', ...(short ? {} : {year:'numeric', hour:'2-digit', minute:'2-digit', hour12:false})
  }).format(new Date(value)) : '—';
  const icon = name => `<svg aria-hidden="true"><use href="#${name}"></use></svg>`;

  function state() {
    const session = activeAcc();
    if (!session) return null;
    if (!sessions.has(session)) sessions.set(session, {data:null, error:'', pending:false, again:false, tab:'summon', filter:'all', closed:new Set(), monthsOpen:new Set(), daysOpen:new Set(), latestMonth:null, starMonthsOpen:new Set(), latestStarMonth:null});
    return sessions.get(session);
  }
  function metric(label, value, note, tone = '') {
    return `<div class="ra-metric ${tone}"><span>${escape(label)}</span><strong>${escape(value)}</strong><small>${escape(note)}</small></div>`;
  }
  function resultCards(rows) {
    if (!rows.length) return '<div class="ra-empty-inline">尚未收录五星结果</div>';
    return `<div class="ra-results">${rows.map(row => `<div class="ra-result ${row.lightDark ? 'ra-ml' : ''}">
      <div class="ra-portrait">${row.icon ? `<img src="${escape(row.icon)}" alt="" loading="lazy">` : ''}<span>${icon('ui-star')}</span></div>
      <strong title="${escape(row.name)}">${escape(row.name)}</strong><small>${row.lightDark ? '光暗' : row.kind === 'artifact' ? '羁绊' : '角色'} · ${date(row.time, true)}</small>
    </div>`).join('')}</div>`;
  }
  function breakdown(pool) {
    const c = pool.counts, rows = pool.kind === 'limited'
      ? [['卡池UP角色',c.upRole],['非UP五星角色',c.otherRole],['卡池UP羁绊',c.upArtifact],['非UP五星羁绊',c.otherArtifact]]
      : [['五星光暗角色',c.lightDark],['五星三色角色',c.rgb],['五星羁绊',c.artifact]];
    return `<div class="ra-breakdown">${rows.map(([label, count]) => `<div><span>${label}</span><b>${number(count)} 次</b><em>${pool.counts.five ? percent((count || 0) / pool.counts.five) : '—'}</em></div>`).join('')}</div>`;
  }
  function poolCard(pool, st) {
    const c = pool.counts, p = pool.pity;
    const ml = pool.kind === 'mystic';
    const rate = ml ? pool.lightDarkRate : pool.fiveRate;
    const baseline = pool.rates?.[ml ? 'lightDark' : 'five'];
    const closed = st.closed.has(pool.id);
    const recent = ml ? pool.recentLightDark : pool.recent;
    const up = pool.recent?.find(row => pool.upRoles.includes(row.sid));
    const poolIcon = pool.icon || up?.icon;
    const current = p.current == null ? '等待同步' : `${number(p.current)} / ${number(p.limit)}`;
    const pityValue = !p.limit ? '无保底' : p.remaining == null ? '—' : `差 ${number(p.remaining)} 抽`;
    const period = pool.periodStart ? `${date(pool.periodStart, true)} — ${pool.active ? '进行中' : date(pool.periodEnd, true)}`
      : ['normal','galaxy'].includes(pool.kind) ? '常驻卡池' : pool.active ? '当前开放' : '历史卡池';
    return `<article class="card ra-pool ${closed ? 'ra-closed' : ''}" data-pool="${escape(pool.id)}">
      <button class="ra-pool-head" data-collapse="${escape(pool.id)}" aria-expanded="${!closed}">
        <span class="ra-pool-symbol ${pool.kind}">${poolIcon ? `<img src="${escape(poolIcon)}" alt="">` : icon('ui-star')}</span>
        <span class="ra-pool-title"><strong>${escape(pool.name)}</strong><small>${labels[pool.kind]} · ${period}</small><span class="ra-pool-luck">${number(pool.total)} 抽 · ${escape(pool.luck)}</span></span>
        <span class="ra-collapse">${closed ? '展开' : '收起'}${icon('ui-chevron-down')}</span>
      </button>
      ${closed ? '' : `<div class="ra-pool-body">
        ${pool.error ? `<div class="ra-notice error" role="status">本池同步失败：${escape(pool.error)}。显示已保存记录。</div>` : ''}
        <div class="ra-metrics">
          ${metric('总抽数',number(pool.total),`${number(c.five)} 个五星 · ${number(c.four)} 个四星`,'blue')}
          ${metric(ml ? '光暗五星率' : '五星率',percent(rate),baseline == null ? '当前卡池未提供可计算基准' : `游戏基准 ${percent(baseline)}`,'gold')}
          ${metric(pool.kind === 'limited' ? 'UP角色' : '光暗五星',`${number(pool.kind === 'limited' ? c.upRole : c.lightDark)} 次`,pool.kind === 'limited' ? '按本池五星角色统计' : `${percent(pool.lightDarkRate)} · 总抽占比`,'green')}
          ${metric('当前保底',pityValue,!p.limit ? '该卡池不设保底计数' : `当前 ${current}`,'blue')}
        </div>
        ${p.limit && p.current != null ? `<div class="ra-pity-track" role="meter" aria-label="保底进度" aria-valuemin="0" aria-valuemax="${p.limit}" aria-valuenow="${Math.min(p.current,p.limit)}"><i style="width:${Math.max(0,Math.min(100,p.current / p.limit * 100))}%"></i></div>` : ''}
        <h3>结果拆分 <small>占五星结果比例</small></h3>${breakdown(pool)}
        <h3>${ml ? '光暗五星结果' : '五星结果'} <small>最近 ${recent.length} 次</small></h3>${resultCards(recent)}
        ${ml ? `<h3>神秘契约 · 光暗五星间隔</h3>${pool.lightDarkIntervals.length ? `<div class="ra-intervals">${pool.lightDarkIntervals.map(row => `<span>${escape(row.name)} <b>${number(row.pulls)} 抽</b></span>`).join('')}</div>` : '<div class="ra-empty-inline">收录两个光暗五星后显示间隔抽数</div>'}` : ''}
        <p class="ra-coverage">已收录 ${number(pool.total)} 抽${pool.startAt ? ` · 最早记录 ${date(pool.startAt)}` : ''}${pool.updatedAt ? ` · 同步于 ${date(pool.updatedAt)}` : ''}</p>
        ${c.unknown ? `<p class="ra-notice">${number(c.unknown)} 条结果尚无对应游戏资料，概率暂不计算，请更新资料后刷新。</p>` : ''}
      </div>`}
    </article>`;
  }
  function summonHTML(data, st) {
    const filters = [['all','全部卡池'],['normal','常规'],['mystic','神秘'],['limited','限时UP'],['galaxy','银河']];
    const pools = data.pools.filter(pool => st.filter === 'all' || pool.kind === st.filter);
    return `<div class="ra-filter" aria-label="卡池筛选">${filters.map(([key,label]) => `<button class="${st.filter === key ? 'active' : ''}" data-filter="${key}" aria-pressed="${st.filter === key}">${label}</button>`).join('')}</div>
      <p class="ra-hint">统计已收录的游戏历史，保底读取当前账号数据。基准概率随游戏资料更新；偏欧、偏非只描述当前样本。</p>
      <div class="ra-pools">${pools.map(pool => poolCard(pool,st)).join('') || '<div class="card ra-empty">当前分类暂无已开放或已收录的卡池</div>'}</div>`;
  }
  function ticketMetric(entry, type, data, compact = false) {
    const sid = type === 'recruit' ? '5' : '6', label = type === 'recruit' ? '招募契约' : '神秘契约';
    const baseline = data.baselines?.find(row => row.item === sid);
    const bought = entry[type === 'recruit' ? 'boughtRecruit' : 'boughtMystery'];
    return `<div class="ra-ticket" data-ticket="${type}"><img src="/assets/icons/${type === 'recruit' ? 'recruit' : 'mystery'}.png" alt=""><div><strong>${label}</strong><b>${number(bought)}<small> 张</small></b><span>已${entry.legacyRefreshes ? '记录' : ''}购买</span><small>出现 ${number(entry[type])} 张 · ${number(entry[type+'Hits'])} 次</small><small>出现率 ${percent(entry.refreshes ? entry[type+'Hits'] / entry.refreshes : null)}${compact ? '' : ` · 每档基准 ${percent(baseline?.slotRate)}`}</small></div></div>`;
  }
  function equipmentHTML(equipment) {
    const picture = source => source ? `<img src="${escape(source)}" alt="" loading="lazy">` : '';
    return `<div class="ra-equipment">${equipment?.length ? equipment.map(row => `<div class="ra-equipment-row"><span>${escape(row.slotName)}${!row.slot || !row.set ? ` · ${escape(row.staticID)}` : ''}${picture(row.slotIcon)}</span><strong>${escape(row.setName)}套装${picture(row.setIcon)}</strong><b>${number(row.count)} 件</b></div>`).join('') : '<p class="ra-empty-inline">暂无已购装备记录</p>'}</div>`;
  }
  function purchasesHTML(entry, shop) {
    const chips = new Map((entry.chips || []).map(row => [row.id,row.count]));
    return `<div class="ra-purchased"><h3>已购芯片 <small>按部位累计数量</small></h3><div class="ra-chips">${(shop.chipTypes || []).map(chip => `<div class="ra-chip" data-chip="${escape(chip.id)}"><img src="${escape(chip.icon)}" alt="" loading="lazy"><span>${escape(chip.name)}</span><b>${number(chips.get(chip.id))}<small> 个</small></b></div>`).join('')}</div>
      <h3>已购装备 <small>${number((entry.equipment || []).reduce((sum,row) => sum + row.count,0))} 件</small></h3>${equipmentHTML(entry.equipment)}</div>`;
  }
  function spendingHTML(entry) {
    const partial = entry.legacyRefreshes || entry.unknownCosts;
    return `<div class="ra-shop-spending" aria-label="花费和刷新统计">
      <div><span>${partial ? '已记录金币花费' : '金币总花费'}</span><b><img src="/assets/icons/gold.png" alt="">${number(entry.gold)}</b></div>
      <div><span>${partial ? '已记录钻石花费' : '钻石总花费'}</span><b><img src="/assets/icons/diamond.png" alt="">${number(entry.diamonds)}</b></div>
      <div><span>钻石刷新</span><b>${number(entry.paid)}<small> 次</small></b></div><div><span>免费刷新</span><b>${number(entry.free)}<small> 次</small></b></div>
      ${partial ? `<p class="ra-ledger-note">${entry.legacyRefreshes ? `${number(entry.legacyRefreshes)} 次旧版刷新未保存购买和花费，购买明细仅累计升级后的记录。` : ''}${entry.unknownCosts ? `${number(entry.unknownCosts)} 笔操作回包缺少完整扣费数据，花费仅显示已确认部分。` : ''}</p>` : ''}
    </div>`;
  }
  function shopTotals(entry, shop, compact = false) {
    return `<div class="ra-shop-overview ${compact ? 'ra-compact' : ''}"><div class="ra-tickets">${ticketMetric(entry,'recruit',shop,compact)}${ticketMetric(entry,'mystery',shop,compact)}</div>${purchasesHTML(entry,shop)}${spendingHTML(entry)}</div>`;
  }
  function dailyTotals(day) {
    const gained = (day.gains || []).filter(row => row.count > 0).map(row => [row.name,row.count]);
    for (const row of day.equipment || []) if (row.count > 0) gained.push([`${row.setName}·${row.slotName}`,row.count]);
    const costs = [['金币',day.gold],['购买钻石',day.purchaseDiamonds]].filter(([,count]) => count > 0);
    const values = rows => rows.map(([name,count]) => `<span>${escape(name)}：<b>${number(count)}</b></span>`).join('');
    const refreshCosts = `<span>钻石：<b>3×${number(day.paid)}次=${number(3 * (day.paid || 0))}</b></span><span>免费刷新次数：<b>${number(day.free)}</b></span>`;
    return `<div class="ra-day-lines">
      <div class="ra-day-gains"><strong>获得：</strong><div>${values(gained) || `<span class="ra-day-empty">${day.legacyRefreshes ? '旧版未记录购买明细' : '暂无获得'}</span>`}</div></div>
      <div class="ra-day-costs"><strong>消耗：</strong><div>${values(costs)}${refreshCosts}</div></div>
    </div>`;
  }
  function shopHTML(data, st) {
    const shop = data.shop;
    const latest = shop.months[0]?.month;
    if (latest && st.latestMonth !== latest) {st.monthsOpen.add(latest); st.latestMonth = latest;}
    return `<p class="ra-hint">${escape(shop.coverage)}</p>${data.recordingError ? `<div class="ra-notice error">最近一次商店记录未保存：${escape(data.recordingError)}</div>` : ''}
      ${shop.months.length ? shop.months.map(month => {
        const open = st.monthsOpen.has(month.month), daysOpen = st.daysOpen.has(month.month);
        const bodyID = `ra-month-${month.month}`, daysID = `ra-days-${month.month}`;
        return `<article class="card ra-month" data-month="${month.month}"><button class="ra-month-head" data-shop-month="${month.month}" aria-expanded="${open}" aria-controls="${bodyID}"><strong>${month.month.replace('-','年')}月</strong><span>共刷新 ${number(month.refreshes)} 次 ${icon(open ? 'ui-chevron-up' : 'ui-chevron-down')}</span></button>
          ${open ? `<div class="ra-month-body" id="${bodyID}">${shopTotals(month,shop)}
            <button class="ra-days-toggle" data-shop-days="${month.month}" aria-expanded="${daysOpen}" aria-controls="${daysID}"><strong>每日记录 <small>${number(month.days.length)} 天</small></strong><span>${daysOpen ? '折叠' : '展开'}${icon(daysOpen ? 'ui-chevron-up' : 'ui-chevron-down')}</span></button>
            ${daysOpen ? `<div class="ra-shop-days" id="${daysID}">${month.days.map(day => `<article class="ra-shop-day" data-day="${day.day}"><div class="ra-day-heading"><strong>${day.day.slice(8)} 日</strong><span>${day.legacyRefreshes || day.unknownCosts ? '部分明细未记录' : ''}</span></div>${dailyTotals(day)}</article>`).join('')}</div>` : ''}</div>` : ''}</article>`;
      }).join('') : `<div class="card ra-empty">${icon('nav-scroll-text')}<strong>${st.pending ? '正在同步秘密商店记录' : '还没有商店刷新记录'}</strong><p>${st.pending ? '正在核对当前账号的已保存记录，请稍候。' : '在本工具完成免费或钻石刷新后，会自动累计到这里。'}</p></div>`}
      <p class="ra-hint">契约出现与已购买分别统计；出现率＝出现契约的刷新次数 ÷ 已记录刷新次数。花费含刷新和购买的实际扣费。${shop.firstRecordAt ? `最早记录：${date(shop.firstRecordAt)}。` : ''}</p>`;
  }
  function starShopHTML(data, st) {
    const shop = data.shop.starSource || {months:[], coverage:'按月汇总本工具确认购买成功的星源商店装备，显示部位、套装和件数；启用前及其他客户端的购买不计入。'};
    const latest = shop.months[0]?.month;
    if (latest && st.latestStarMonth !== latest) {st.starMonthsOpen.add(latest); st.latestStarMonth = latest;}
    return `<p class="ra-hint">${escape(shop.coverage)}</p>${data.recordingError ? `<div class="ra-notice error">最近一次商店记录未保存：${escape(data.recordingError)}</div>` : ''}
      ${shop.months.length ? shop.months.map(month => {
        const open = st.starMonthsOpen.has(month.month), bodyID = `ra-star-month-${month.month}`;
        return `<article class="card ra-month ra-star-month" data-star-month="${month.month}"><button class="ra-month-head" data-star-toggle="${month.month}" aria-expanded="${open}" aria-controls="${bodyID}"><strong>${month.month.replace('-','年')}月</strong><span>已购 ${number(month.total)} 件 ${icon(open ? 'ui-chevron-up' : 'ui-chevron-down')}</span></button>
          ${open ? `<div class="ra-month-body" id="${bodyID}">${equipmentHTML(month.equipment)}</div>` : ''}</article>`;
      }).join('') : `<div class="card ra-empty">${icon('nav-scroll-text')}<strong>${st.pending ? '正在同步星源商店记录' : '还没有星源商店购买记录'}</strong><p>${st.pending ? '正在核对当前账号的已保存记录，请稍候。' : '在本工具购买星源商店装备后，会自动按月累计到这里。'}</p></div>`}`;
  }
  function paint(forceBody = false) {
    const root = document.getElementById('recruitmentRoot');
    if (!root || currentSection !== 'recruitment') return;
    const st = state(), account = activeAcc();
    if (!st) { root._raState = null; root.innerHTML = '<div class="card ra-empty">请先选择已登录账号</div>'; return; }
    const data = st.data, status = account.status || {};
    const fresh = root._raState !== st;
    if (fresh) {
      root._raState = st;
      root.innerHTML = `<div class="ra-heading"><div><span class="ra-eyebrow">DROP RECORDS</span><h2>出货记录</h2><p>每一份幸运，都有记录</p></div><div class="ra-account"><strong></strong><span></span></div></div>
      <div class="ra-toolbar"><div class="ra-tabs" role="tablist" aria-label="记录类型"><button role="tab" aria-selected="${st.tab === 'summon'}" class="${st.tab === 'summon' ? 'active' : ''}" data-tab="summon">${icon('ui-star')}抽卡</button><button role="tab" aria-selected="${st.tab === 'shop'}" class="${st.tab === 'shop' ? 'active' : ''}" data-tab="shop">${icon('nav-scroll-text')}秘密商店</button><button role="tab" aria-selected="${st.tab === 'star'}" class="${st.tab === 'star' ? 'active' : ''}" data-tab="star">${icon('nav-scroll-text')}星源商店</button></div><button class="mini ra-refresh" ${st.pending ? 'disabled' : ''} data-refresh>${icon('ui-rotate-cw')}${st.pending ? '同步中…' : '刷新数据'}</button></div>
      <div class="ra-sync" role="status" aria-live="polite"></div><div class="ra-view"></div>`;
    }
    root.querySelector('.ra-account strong').textContent = status.name || activeAccount;
    root.querySelector('.ra-account span').textContent = `UID ${status.cuid ?? '—'}`;
    root.querySelectorAll('[data-tab]').forEach(button => {
      const selected = st.tab === button.dataset.tab;
      button.classList.toggle('active',selected);
      button.setAttribute('aria-selected',String(selected));
      button.onclick = LucimaActionWait.wrap(() => {
        if (st.tab === button.dataset.tab) return;
        st.tab = button.dataset.tab;
        root.closest('.content').scrollTop = 0;
        paint();
      });
    });
    const refreshButton = root.querySelector('[data-refresh]');
    refreshButton.disabled = st.pending;
    if (fresh || root._raPending !== st.pending) refreshButton.innerHTML = `${icon('ui-rotate-cw')}${st.pending ? '同步中…' : '刷新数据'}`;
    root._raPending = st.pending;
    const sync = `<i class="${st.error || data?.errors.length ? 'error' : ''}"></i>${st.pending ? '正在读取游戏最新数据…' : st.error ? `同步失败：${escape(st.error)}${data ? '，以下为上次保存数据' : ''}` : data?.errors.length ? '部分卡池同步失败，已保留上次记录' : data ? `更新于 ${date(data.updatedAt)} · 每次进入自动同步` : '等待同步'}`;
    if (root._raSync !== sync || fresh) {root.querySelector('.ra-sync').innerHTML = sync; root._raSync = sync;}
    const bodyPending = !data || (st.tab !== 'summon' && !(st.tab === 'star' ? data.shop.starSource?.months.length : data.shop.months.length));
    const pendingView = bodyPending && st.pending;
    if (fresh || forceBody || root._raData !== data || root._raTab !== st.tab || root._raFilter !== st.filter || root._raViewPending !== pendingView) {
      // Replace only the bounded content view. Retaining the toolbar avoids
      // repainting it as long summon history collapses into an empty shop.
      const view = document.createElement('div'); view.className = 'ra-view';
      view.innerHTML = data ? (st.tab === 'summon' ? summonHTML(data,st) : st.tab === 'star' ? starShopHTML(data,st) : shopHTML(data,st)) : `<div class="card ra-empty">${icon('ui-star')}<strong>${st.pending ? '正在同步出货记录' : '暂无可展示的数据'}</strong><p>${st.pending ? '读取账号保底和游戏抽卡历史，请稍候。' : '点击刷新数据重新读取。'}</p></div>`;
      root.querySelector('.ra-view').replaceWith(view);
      root._raData = data; root._raTab = st.tab; root._raFilter = st.filter; root._raViewPending = pendingView;
    }
    root.querySelectorAll('[data-filter]').forEach(button => button.onclick = LucimaActionWait.wrap(() => {st.filter = button.dataset.filter; paint();}));
    root.querySelectorAll('[data-collapse]').forEach(button => button.onclick = LucimaActionWait.wrap(() => {
      const key = button.dataset.collapse;
      if (st.closed.has(key)) st.closed.delete(key); else st.closed.add(key);
      paint(true);
    }));
    root.querySelectorAll('[data-shop-month], [data-shop-days]').forEach(button => button.onclick = LucimaActionWait.wrap(() => {
      const month = button.dataset.shopMonth || button.dataset.shopDays;
      const opened = button.hasAttribute('data-shop-month') ? st.monthsOpen : st.daysOpen;
      if (opened.has(month)) opened.delete(month); else opened.add(month);
      paint(true);
    }));
    root.querySelectorAll('[data-star-toggle]').forEach(button => button.onclick = LucimaActionWait.wrap(() => {
      const month = button.dataset.starToggle;
      if (st.starMonthsOpen.has(month)) st.starMonthsOpen.delete(month); else st.starMonthsOpen.add(month);
      paint(true);
    }));
    root.querySelector('[data-refresh]').onclick = LucimaActionWait.wrap(refresh);
    root.querySelectorAll('.ra-portrait img, .ra-pool-symbol img').forEach(img => img.onerror = () => {
      if (img.parentElement.classList.contains('ra-pool-symbol')) img.parentElement.innerHTML = icon('ui-star');
      else img.remove();
    });
  }
  async function refresh() {
    const session = activeAcc(), account = activeAccount, st = state();
    if (!st || currentSection !== 'recruitment') return;
    if (st.pending) { st.again = true; return; }
    st.pending = true; st.error = ''; paint();
    try {
      const response = await api('/api/recruitment/refresh', {account});
      if (ACCOUNTS[account] !== session) return;
      if (!response.recruitment) throw new Error('未收到游戏档案数据');
      st.data = response.recruitment;
      applyStateSyncMeta(account,response);
      if (response.status) applyStatus(account,response.status,response.nextClaims,undefined,response.connection,response.redeemCodes);
    } catch (error) {
      if (ACCOUNTS[account] === session) st.error = error.message;
    } finally {
      st.pending = false;
      if (ACCOUNTS[account] === session && account === activeAccount && currentSection === 'recruitment') {
        paint();
        if (st.again) {st.again = false; void refresh();}
      } else st.again = false;
    }
  }
  window.addEventListener('lucima:section-mount', event => {if (event.detail?.section === 'recruitment') paint();});
  window.addEventListener('lucima:section-change', event => {if (event.detail?.section === 'recruitment') void refresh();});
  window.addEventListener('lucima:account-change', () => {if (currentSection === 'recruitment') {paint(); void refresh();}});
  window.addEventListener('lucima:resume', () => {if (currentSection === 'recruitment') void refresh();});
})();
