/* Event-shop options use cached account state; purchases run in the scheduler. */
function activityStoreGoodName(good) {
  const name = String(good.name || good.item || '');
  const amount = Number(good.amount);
  const namedAmount = name.match(/^(\d+)\s*[个個]/u)?.[1]
    ?? name.match(/[x×]\s*(\d+)\s*$/iu)?.[1];
  return name + (amount > 1 && Number(namedAmount) !== amount ? ` ×${amount}` : '');
}

function renderActivityStoreOptions() {
  return '<div class="activity-store-wrap arena-wrap"><div class="tdesc">正在加载活动商店…</div></div>';
}

function bindActivityStoreOptions(id) {
  const root = $(`auto-${id}`);
  bindAccountPanel(root.querySelector('.activity-store-toggle'), root.querySelector('.auto-body'), `auto:${id}`, {
    onOpen: () => loadActivityStoreOptions(id),
    onRestore: () => renderActivityStoreProgress(id),
  });
  renderActivityStoreProgress(id);
}

async function loadActivityStoreOptions(id) {
  const root = $(`auto-${id}`), email = activeAccount;
  if (!root || !email || root._activityStoreLoading) return;
  root._activityStoreLoading = true;
  try {
    const data = await api('/api/activity-store/options?account=' + encodeURIComponent(email));
    setAccountOptionCache('activityStore', data, email);
    if (activeAccount === email && root.isConnected) renderActivityStoreProgress(id, data);
  } catch (error) {
    if (activeAccount === email && root.isConnected)
      root.querySelector('.activity-store-wrap').textContent = `加载失败：${error.message}`;
  } finally { root._activityStoreLoading = false; }
}

function renderActivityStoreProgress(id = 'activity_store', data = null) {
  const root = $(`auto-${id}`), email = activeAccount;
  const host = root?.querySelector('.activity-store-wrap');
  if (!host) return;
  const state = data || [NEXT_CLAIMS[id], accountOptionCache('activityStore')].filter(Boolean)
    .sort((a, b) => (b.serverNowMs || 0) - (a.serverNowMs || 0))[0];
  if (!state) return;
  const offset = (state.localNowMs || Date.now()) - (state.serverNowMs || Date.now());
  const shops = (state.stores || []).filter(shop => shop.endMs == null || shop.endMs + offset > Date.now());
  const ui = accountUiState(email);
  if (!ui) return;
  const shop = shops.find(shop => shop.id === ui.activityStoreTab) || shops[0];
  const selected = new Set(ACCOUNTS[email]?.params?.[id]?.items || []);
  const periods = ACCOUNTS[email]?.params?.[id]?.periods || {};
  const shape = JSON.stringify([email, state, shop?.id, [...selected], periods]);
  if (root._activityStoreShape === shape) return;
  root._activityStoreShape = shape;
  if (!shop) {
    host.innerHTML = `<div class="tdesc">${filterEscape(state.stores?.length ? '暂无开放的活动商店' : state.note || '暂无开放的活动商店')}</div>`;
    return;
  }
  ui.activityStoreTab = shop.id;
  const isPicked = g => selected.has(g.id) && (!(shop.id in periods) || periods[shop.id] === shop.startMs);
  const required = Object.fromEntries(Object.keys(shop.wallet || {}).map(sid => [sid, 0]));
  shop.goods.filter(g => g.limit > 0 && isPicked(g)).forEach(g => {
    if (g.remaining == null || required[g.currency] === null) required[g.currency] = null;
    else required[g.currency] = (required[g.currency] || 0) + g.price * g.remaining;
  });
  host.innerHTML = `<div class="b-tabs">${shops.map(s => `<button type="button"
      class="b-tab ${s.id === shop.id ? 'active' : ''}" data-event-store="${filterEscape(s.id)}">
      ${filterEscape(s.name)}</button>`).join('')}</div>
    <div class="activity-store-heading">
      <div class="guild-store-balances">${Object.entries(shop.wallet || {}).map(([sid, count]) =>
        `<span title="持有数量 / 所需数量"><img src="${itemIcon(sid)}" alt=""><b>${count == null ? '—' : fmtNum2(count)}</b><span>/ ${required[sid] == null ? '—' : fmtNum2(required[sid])}</span></span>`).join('')}</div>
      <span class="tdesc">${shop.endMs == null ? '' : `兑换截止：${fmtClock(shop.endMs + offset)}`}</span>
    </div>
    <div class="guild-store-grid">${shop.goods.filter(g => g.limit > 0).map(g => {
      const picked = isPicked(g);
      const note = g.waiting || g.error ? '等待登录同步刷新'
        : g.unsupported ? '购买规则暂不支持' : !g.available ? '暂未开放'
        : g.remaining === 0 ? '已售罄' : `剩余 ${g.remaining ?? '—'}/${g.limit}`;
      return `<label class="bt-opt guild-store-good ${picked ? 'selected' : ''}" data-event-good="${filterEscape(g.id)}">
        <input type="checkbox" ${picked ? 'checked' : ''}>
        ${checkboxUIHTML()}
        <img class="guild-store-icon" src="${/^H\d+$/.test(g.item) ? `/assets/avatars/${g.item}.png` : itemIcon(g.item)}" alt="" loading="lazy">
        <span class="guild-store-copy">
          <span class="guild-store-name">${filterEscape(activityStoreGoodName(g))}</span>
          <span class="guild-store-price"><img src="${itemIcon(g.currency)}" alt="">${fmtNum2(g.price)}</span>
          <span class="guild-store-stock ${g.waiting || g.error ? 'error' : ''}">${filterEscape(note)}</span>
        </span>
      </label>`;
    }).join('')}</div>`;
  host.querySelectorAll('img').forEach(image => image.addEventListener('error', () => {
    if (image.dataset.fallback) { image.hidden = true; return; }
    image.dataset.fallback = 'true';
    image.src = '/assets/ui/activity.png';
  }));
  host.querySelectorAll('[data-event-store]').forEach(button => button.addEventListener('click', () => {
    ui.activityStoreTab = button.dataset.eventStore;
    renderActivityStoreProgress(id, state);
  }));
  host.querySelectorAll('[data-event-good] input').forEach(input => input.addEventListener('change', async () => {
    const account = ACCOUNTS[email];
    if (!account || email !== activeAccount) return;
    const items = new Set(account.params?.[id]?.items || []);
    const sid = input.closest('[data-event-good]').dataset.eventGood;
    const periods = {...account.params?.[id]?.periods};
    if (shop.id in periods && periods[shop.id] !== shop.startMs)
      shop.goods.forEach(g => items.delete(g.id));
    periods[shop.id] = shop.startMs;
    input.checked ? items.add(sid) : items.delete(sid);
    account.params = {...account.params, [id]: {...account.params?.[id], items: [...items], periods}};
    renderActivityStoreProgress(id, state);
    try {
      await api('/api/toggles', {account: email, toggles: account.toggles, params: {[id]: account.params[id]}});
    } catch (error) { logTo(email, `保存购买目标失败：${error.message}`, 'err'); }
  }));
}
