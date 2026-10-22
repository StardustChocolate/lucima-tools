/* Member-shop controls share the account cache; opening a shelf sends no game request. */
(() => {
  const $ = id => document.getElementById(id);
  const states = new WeakMap();
  const currencyNames = { '29': '同盟徽章', '30': '勇气纹章', '80': '荣誉金币', '81': '英勇证明' };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  function state(session) {
    if (!states.has(session)) states.set(session, { saving: false, buying: '', quantities: {} });
    return states.get(session);
  }
  function selected(session) {
    const items = session?.params?.guild_auto_store?.items;
    return Array.isArray(items) ? items : [];
  }
  function selectedCaptain(session) {
    const items = session?.params?.guild_auto_captain_store?.items;
    return Array.isArray(items) ? items : [];
  }
  function balances(store) {
    const ids = store?.currencies ? Object.keys(store.currencies) : ['29', '30'];
    return ids.filter(id => currencyNames[id]).map(id => `<span><img src="/assets/items/${id}.png" alt="">${currencyNames[id]} <b>${store?.currencies?.[id] ?? '—'}</b></span>`).join('');
  }
  function card(row, auto, picked, disabled, st, status) {
    status ??= row.blocked ? '购买未确认，请重新登录' : row.error ? '请重新登录以同步记录'
      : !row.available ? '尚未开放' : row.remaining === 0 ? '已购齐'
      : row.limit === null ? '不限购' : `剩余 ${row.remaining ?? '—'}/${row.limit}`;
    const caption = `<img class="guild-store-icon" src="/assets/items/${encodeURIComponent(row.item || row.id)}.png" alt="">
      <span class="guild-store-copy"><span class="guild-store-name">${escape(row.name)}${row.amount > 1 ? ` ×${row.amount}` : ''}</span>
      <span class="guild-store-price"><img src="/assets/items/${row.currency}.png" alt="${escape(currencyNames[row.currency])}">${row.price}</span>
      <span class="guild-store-stock${row.blocked || row.error ? ' error' : ''}">${escape(status)}</span></span>`;
    if (auto) return `<label class="bt-opt guild-store-good${picked ? ' selected' : ''}">
      <input type="checkbox" value="${escape(row.id)}" ${picked ? 'checked' : ''} ${disabled ? 'disabled' : ''}>${checkboxUIHTML()}${caption}</label>`;
    const max = row.buyable || 0;
    const quantity = Math.max(1, Math.min(max || 1, st?.quantities[row.id] || 1));
    return `<div class="guild-store-good guild-store-manual"><div class="guild-store-product">${caption}</div>
      <div class="guild-store-actions"><input type="number" min="1" max="${max || 1}" step="1" inputmode="numeric"
        aria-label="${escape(row.name)}购买数量" data-quantity="${escape(row.id)}" value="${quantity}" ${disabled || !max ? 'disabled' : ''}>
        <button type="button" class="mini" data-buy="${escape(row.id)}" ${disabled || !max ? 'disabled' : ''}>${st?.buying === row.id ? '购买中…' : '购买'}</button></div></div>`;
  }
  function captainStatus(row, access) {
    if (!access?.allowed) return access?.reason || '职位信息未确认，暂停购买';
    if (row.statusPending) return '购买已确认，等待 Buff 状态同步';
    if (row.blocked) return '结果未确认，等待下次登录';
    if (row.error) return '状态待同步';
    if (row.conditionMet && !row.affordable) {
      return row.balance == null ? `等待${currencyNames[row.currency]}余额同步`
        : '货币不足';
    }
    if (row.kind === 'buff') {
      if (row.remainingMs <= 0) return '当前没有 Buff，可购买';
      const hours = Math.floor(row.remainingMs / 3600000), minutes = Math.floor(row.remainingMs % 3600000 / 60000);
      const remaining = `${hours} 小时 ${minutes} 分`;
      return row.eligible ? `剩余 ${remaining}，可购买` : `剩余 ${remaining}，低于 11.9 小时后购买`;
    }
    if (row.remaining === 0) return row.reset === 'Week' ? '本周已购买' : '今日已购买';
    return row.reset === 'Week' ? `本周可购 ${row.remaining}` : `今日可购 ${row.remaining}`;
  }
  function paint() {
    if (!$('guildStoreRow')) return;
    const session = ACCOUNTS[activeAccount], store = session?.guild?.store, st = session && state(session);
    const joined = session?.guild?.joined === true, picked = selected(session);
    const saving = Boolean(st?.saving), running = Boolean(store?.running);
    const toggle = $('guildAutoStore');
    toggle.checked = Boolean(session?.toggles?.guild_auto_store);
    toggle.disabled = !joined || saving;
    $('guildStoreRow').classList.toggle('on', toggle.checked);
    $('guildStoreOptions').disabled = !joined;
    $('guildManualStoreOptions').disabled = !joined;
    const rows = (store?.goods || []).filter(row => row.reset);
    const choices = rows.filter(row => picked.includes(row.id));
    $('guildStoreState').textContent = !joined ? '未加入佣兵团，暂不执行' : !picked.length ? '请在选项中选择商品'
      : running ? '正在购买…' : choices.some(row => row.blocked || row.error) ? '购买记录待确认，请重新登录'
      : choices.length && choices.every(row => row.remaining === 0) ? '本周期已购齐，等待周／月刷新'
      : !toggle.checked ? '自动购买已关闭' : '本周期尚未购齐，货币充足时自动购买';
    $('guildStoreBalances').innerHTML = $('guildManualStoreBalances').innerHTML = balances(store);
    const autoHTML = ['Week', 'Month'].map(reset => `<div class="guild-store-group"><div class="guild-weekly-heading">${reset === 'Week' ? '每周' : '每月'}</div>
      <div class="guild-store-grid">${rows.filter(row => row.reset === reset).map(row => card(row, true, picked.includes(row.id), !joined || saving, st)).join('')}</div></div>`).join('');
    const manualHTML = (store?.goods || []).filter(row => !row.reset).map(row => card(row, false, false,
      !joined || Boolean(st?.buying) || running || session?.connection?.connected === false, st)).join('');
    // Retain input focus and an in-progress quantity while regular status polling paints.
    for (const [id, html] of [['guildStoreGoods', autoHTML], ['guildManualStoreGoods', manualHTML]]) {
      const el = $(id);
      if (el.dataset.rendered !== html) {
        const input = document.activeElement;
        const focused = el.dataset.account === activeAccount && el.contains(input) && input.dataset.quantity
          ? { id: input.dataset.quantity, value: input.value } : null;
        el.innerHTML = html; el.dataset.rendered = html; el.dataset.account = activeAccount;
        if (focused) {
          const replacement = [...el.querySelectorAll('[data-quantity]')].find(node => node.dataset.quantity === focused.id);
          if (replacement && !replacement.disabled) { replacement.value = focused.value; replacement.focus({ preventScroll: true }); }
        }
      }
    }
    const captainStore = session?.guild?.captainStore;
    const captainRows = captainStore?.goods || [];
    const captainAccess = captainStore?.access;
    const captainAllowed = captainAccess?.allowed === true;
    const captainPicked = selectedCaptain(session);
    const captainToggle = $('guildAutoCaptainStore');
    captainToggle.checked = Boolean(session?.toggles?.guild_auto_captain_store);
    captainToggle.disabled = !joined || !captainAllowed || saving;
    $('guildCaptainStoreRow').classList.toggle('on', captainToggle.checked);
    $('guildCaptainStoreOptions').disabled = !joined || !captainAllowed;
    $('guildCaptainStoreBalances').innerHTML = balances(captainStore);
    const captainChoices = captainRows.filter(row => captainPicked.includes(row.id));
    const captainRunning = captainStore?.running;
    $('guildCaptainStoreState').textContent = !joined ? '未加入佣兵团，暂不执行'
      : !captainAllowed ? (captainAccess?.reason || '职位信息未确认，暂停购买')
      : !captainPicked.length ? '请在选项中选择商品'
      : captainRunning ? '正在购买…'
      : captainChoices.some(row => row.blocked || row.error) ? '购买结果待确认或状态待同步'
      : captainChoices.some(row => row.conditionMet && !row.affordable) ? '货币不足'
      : !captainToggle.checked ? '自动购买已关闭'
      : captainChoices.every(row => !row.eligible) ? '当前没有符合购买条件的商品'
      : '有符合条件的商品时自动购买';
    const captainHTML = captainRows.map(row => card(row, true, captainPicked.includes(row.id),
      !joined || !captainAllowed || saving, st, captainStatus(row, captainAccess))).join('');
    const captainGoods = $('guildCaptainStoreGoods');
    if (captainGoods.dataset.rendered !== captainHTML) {
      captainGoods.innerHTML = captainHTML;
      captainGoods.dataset.rendered = captainHTML;
      captainGoods.dataset.account = activeAccount;
    }
  }
  async function save(items) {
    const account = activeAccount, session = ACCOUNTS[account];
    if (!session) return;
    const st = state(session);
    if (st.saving) return;
    const before = session.toggles.guild_auto_store, previous = session.params?.guild_auto_store;
    const enabled = $('guildAutoStore').checked;
    st.saving = true;
    session.toggles.guild_auto_store = enabled;
    session.params = { ...session.params, guild_auto_store: { items: items ?? selected(session) } };
    paint();
    try {
      await api('/api/toggles', { account, toggles: { ...session.toggles }, params: { guild_auto_store: session.params.guild_auto_store } });
    } catch (error) {
      session.toggles.guild_auto_store = before;
      if (previous === undefined) delete session.params.guild_auto_store;
      else session.params.guild_auto_store = previous;
      logTo(account, `[佣兵团] 团员商店设置保存失败：${error.message}`, 'err');
    } finally { st.saving = false; paint(); }
  }
  async function saveCaptain(items) {
    const account = activeAccount, session = ACCOUNTS[account];
    if (!session) return;
    const st = state(session);
    if (st.saving) return;
    const before = session.toggles.guild_auto_captain_store;
    const previous = session.params?.guild_auto_captain_store;
    const enabled = $('guildAutoCaptainStore').checked;
    st.saving = true;
    session.toggles.guild_auto_captain_store = enabled;
    session.params = { ...session.params, guild_auto_captain_store: { items: items ?? selectedCaptain(session) } };
    paint();
    try {
      await api('/api/toggles', { account, toggles: { ...session.toggles },
        params: { guild_auto_captain_store: session.params.guild_auto_captain_store } });
    } catch (error) {
      session.toggles.guild_auto_captain_store = before;
      if (previous === undefined) delete session.params.guild_auto_captain_store;
      else session.params.guild_auto_captain_store = previous;
      logTo(account, `[佣兵团] 团长商店设置保存失败：${error.message}`, 'err');
    } finally { st.saving = false; paint(); }
  }
  async function buy(id) {
    const account = activeAccount, session = ACCOUNTS[account];
    if (!session) return;
    const st = state(session);
    if (st.buying) return;
    const row = session.guild?.store?.goods.find(row => row.id === id && !row.reset);
    const input = [...$('guildManualStoreGoods').querySelectorAll('[data-quantity]')].find(el => el.dataset.quantity === id);
    const count = Number(input?.value);
    if (!row || !Number.isSafeInteger(count) || count < 1 || count > row.buyable) {
      input?.reportValidity(); return;
    }
    st.quantities[id] = count;
    st.buying = id; paint();
    try {
      const data = await api('/api/guild/store/buy', { account, id, count });
      if (ACCOUNTS[account] === session) {
        applyStateSyncMeta(account, data);
        applyStatus(account, data.status, data.nextClaims, data.arena, data.connection, data.redeemCodes);
      }
    } catch (error) {
      logTo(account, `[佣兵团] 团员商店购买失败：${error.message}`, 'err');
    } finally { st.buying = ''; paint(); }
  }
  function init() {
    for (const [button, body, label] of [['guildStoreOptions', 'guildStoreBody', '选项'], ['guildManualStoreOptions', 'guildManualStoreBody', '商店']]) {
      $(button).addEventListener('click', () => {
        const expanded = $(body).classList.contains('hidden');
        $(body).classList.toggle('hidden', !expanded);
        $(button).setAttribute('aria-expanded', String(expanded));
        $(button).textContent = `${label} ${expanded ? '▾' : '▸'}`;
      });
    }
    $('guildAutoStore').addEventListener('change', () => save());
    $('guildStoreGoods').addEventListener('change', () => save([...$('guildStoreGoods').querySelectorAll('input:checked')].map(input => input.value)));
    $('guildAutoCaptainStore').addEventListener('change', () => saveCaptain());
    $('guildCaptainStoreOptions').addEventListener('click', () => {
      const expanded = $('guildCaptainStoreBody').classList.contains('hidden');
      $('guildCaptainStoreBody').classList.toggle('hidden', !expanded);
      $('guildCaptainStoreOptions').setAttribute('aria-expanded', String(expanded));
      $('guildCaptainStoreOptions').textContent = `选项 ${expanded ? '▾' : '▸'}`;
    });
    $('guildCaptainStoreGoods').addEventListener('change', () => saveCaptain(
      [...$('guildCaptainStoreGoods').querySelectorAll('input:checked')].map(input => input.value)));
    $('guildManualStoreGoods').addEventListener('input', event => {
      if (event.target.dataset.quantity && ACCOUNTS[activeAccount]) state(ACCOUNTS[activeAccount]).quantities[event.target.dataset.quantity] = event.target.value;
    });
    $('guildManualStoreGoods').addEventListener('click', event => {
      const button = event.target.closest('[data-buy]');
      if (button) buy(button.dataset.buy);
    });
    window.addEventListener('lucima:account-change', paint);
    window.addEventListener('lucima:guild-change', event => { if (event.detail.account === activeAccount) paint(); });
    paint();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
