/* Guild data is fetched after login. This page only reads the shared cache. */
function applyGuildSnapshot(account, value) {
  const session = ACCOUNTS[account];
  if (!session || !value) return;
  const previous = session.guild;
  const login = Number(value.loginRevision || 0);
  if (login < Number(session.stateRevision || 0)) return;
  if (previous && (login < Number(previous.loginRevision || 0)
      || (login === Number(previous.loginRevision || 0)
          && Number(value.revision || 0) < Number(previous.revision || 0)))) return;
  session.guild = value;
  window.dispatchEvent(new CustomEvent('lucima:guild-change', { detail: { account } }));
}

(() => {
  const $ = id => document.getElementById(id);
  const states = new Map();
  const donationLabels = { gold: '金币', courage: '英勇证明' };
  const donationCosts = { gold: 5000, courage: 1 };
  const donationReserveDefaults = { gold: 200000, courage: 10 };
  const aidLabels = { hunt: '讨伐材料', element: '元素材料', catalyst: '催化剂' };
  let aidCatalog = null;
  let aidCatalogPromise = null;
  function aidOptions(session) {
    const value = session?.params?.guild_auto_aid || {};
    // A previously disabled request must stay disabled when removing its checkbox.
    const item = value.request === false ? '' : value.item || '';
    return { request: Boolean(item), item,
      hunt: value.hunt ?? true, element: value.element ?? true, catalyst: value.catalyst ?? false };
  }
  async function loadAidCatalog() {
    if (aidCatalog) return;
    if (!aidCatalogPromise) {
      aidCatalogPromise = fetch('/assets/guild_aid.json').then(async response => {
        if (!response.ok) throw new Error('求助材料清单加载失败');
        const value = await response.json();
        if (value.schema !== 1 || !value.items || !value.rules) throw new Error('求助材料清单无效');
        aidCatalog = value;
      }).finally(() => { aidCatalogPromise = null; });
    }
    return aidCatalogPromise;
  }
  function paintAidItem(session, disabled) {
    const holder = $('guildAidItem');
    const options = aidOptions(session);
    const key = `${Boolean(aidCatalog)}:${options.item}`;
    if (holder.dataset.selection !== key) {
      holder.dataset.selection = key;
      const items = Object.entries(aidCatalog?.items || {}).sort((a, b) => {
        const order = ['hunt', 'element', 'catalyst'];
        return order.indexOf(aidCatalog.rules[a[1].rule].group) - order.indexOf(aidCatalog.rules[b[1].rule].group)
          || a[0].localeCompare(b[0]);
      }).map(([id, item]) => ({ val: id, name: item.name, label:
        `<span class="guild-aid-material"><img src="/assets/items/${encodeURIComponent(id)}.png" alt=""></span>` }));
      const choices = [{ val: '', name: '不指定（不发布求助）', label:
        '<span class="guild-aid-material"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/></svg></span>' }, ...items];
      holder.innerHTML = dropdownHTML('guild-aid-select', choices, options.item);
      holder.querySelectorAll('.dd-opt').forEach((option, index) => {
        option.title = choices[index].name;
        option.setAttribute('aria-label', choices[index].name);
      });
      bindDropdown(holder.querySelector('.dropdown'), item => {
        saveSettings('guild_auto_aid', $('guildAutoAid').checked,
          { ...aidOptions(activeAcc()), request: Boolean(item), item });
      });
    }
    const button = holder.querySelector('.dd-btn');
    const name = aidCatalog?.items[options.item]?.name;
    // bindDropdown copies the icon-only option even when the value is unchanged.
    // Restore the selected name independently of the cached menu construction.
    let selectedName = holder.querySelector('.dd-cur .guild-aid-selected-name');
    if (!selectedName) {
      selectedName = document.createElement('span');
      selectedName.className = 'guild-aid-selected-name';
      holder.querySelector('.dd-cur').append(selectedName);
    }
    selectedName.textContent = name || '不指定';
    button.title = name || '不指定';
    button.setAttribute('aria-label', name ? `求助材料：${name}` : '不指定（不发布求助）');
    button.disabled = disabled || !aidCatalog;
    if (button.disabled) {
      holder.querySelector('.dd-menu').classList.add('hidden');
      holder.querySelector('.dropdown').classList.remove('open');
      button.setAttribute('aria-expanded', 'false');
    }
  }
  function aidSettings() {
    const item = aidCatalog ? ($('guildAidItem').querySelector('.dropdown')?.dataset.val || '') : aidOptions(activeAcc()).item;
    return { request: Boolean(item), item,
      ...Object.fromEntries([...$('guildAidChoices').querySelectorAll('input')]
        .map(input => [input.value, input.checked])) };
  }
  function donationOptions(session) {
    const options = session?.params?.guild_auto_donate || {};
    return { gold: options.gold ?? true, courage: options.courage ?? true };
  }
  function donationReserves(session) {
    const reserve = session?.params?.guild_auto_donate?.reserve || {};
    const value = key => {
      const raw = reserve[key];
      const number = Number(raw);
      return (typeof raw === 'number' || (typeof raw === 'string' && /^\d+$/.test(raw.trim())))
        && Number.isInteger(number) && number >= 0 ? number : donationReserveDefaults[key];
    };
    return { gold: value('gold'), courage: value('courage') };
  }
  function donationSettings() {
    const options = Object.fromEntries([...$('guildDonateChoices').querySelectorAll('input[type="checkbox"]')]
      .map(input => [input.value, input.checked]));
    const reserve = Object.fromEntries([...$('guildDonateChoices').querySelectorAll('.guild-donate-reserve')]
      .map(input => {
        const number = Number(input.value);
        const value = Number.isInteger(number) && number >= 0 ? number : 0;
        input.value = String(value);
        return [input.dataset.key, value];
      }));
    // Save zero explicitly so disabling resource retention survives a reload.
    return { ...options, reserve };
  }
  function state(account) {
    if (!states.has(account) || states.get(account).session !== ACCOUNTS[account]) {
      states.set(account, { session: ACCOUNTS[account], refreshPending: false, savePending: false,
        donationReserveDrafts: {} });
    }
    return states.get(account);
  }
  function paintAvatar(guild) {
    const joined = guild?.joined === true;
    [['guildIcon', guild?.icon], ['guildFrame', guild?.frame]].forEach(([id, path]) => {
      const img = $(id);
      const src = joined && typeof path === 'string' && /^\/assets\/guild\/[A-Za-z0-9_]+\.png$/.test(path) ? path : '';
      if (img.getAttribute('src') !== src) {
        img.classList.add('hidden');
        if (src) img.setAttribute('src', src);
        else img.removeAttribute('src');
      }
      img.classList.toggle('hidden', !src || !img.complete || img.naturalWidth === 0);
    });
    $('guildAvatar').classList.toggle('hidden', $('guildIcon').classList.contains('hidden'));
  }
  function paintWeekly(session, st, guild, joined, noGuild) {
    const weekly = guild?.weekly || {};
    const toggle = $('guildAutoWeekly');
    toggle.checked = Boolean(session?.toggles?.guild_auto_weekly);
    toggle.disabled = !joined || st.savePending || Boolean(weekly.running);
    $('guildWeeklyRow').classList.toggle('on', toggle.checked);
    $('guildWeeklyDetails').disabled = !joined;
    const personal = weekly.personal || [];
    const tiers = weekly.tiers || [];
    const status = $('guildWeeklyState');
    status.classList.toggle('error', joined && !guild.waiting && Boolean(weekly.error));
    status.classList.toggle('ready', joined && !guild.waiting && !weekly.error && [...personal, ...tiers].some(row => row.state === 'ready'));
    status.textContent = guild?.waiting ? guild.waiting : noGuild ? '未加入佣兵团，暂不执行'
      : !joined || weekly.week == null ? '等待同步周任务'
      : weekly.running ? ({ query: '正在检查周任务…', personal: '正在领取个人任务奖励…', tiers: '正在领取全团积分奖励…' }[weekly.running] || '正在领取…')
      : weekly.error ? '领取未确认，5 分钟后查询确认'
      : `个人已领 ${weekly.personalClaimed ?? 0}/${personal.length} · 积分奖励已领 ${weekly.tiersClaimed ?? 0}/${tiers.length}`;
    const labels = { claimed: '已领取', ready: '可领取', progress: '进行中', unknown: '待同步', uncertain: '待确认' };
    const content = personal.map(row => {
      const known = Number.isFinite(row.count) && Number.isFinite(row.target) && row.target > 0;
      const percent = known ? Math.max(0, Math.min(100, row.count / row.target * 100)) : 0;
      const state = labels[row.state] ? row.state : 'unknown';
      return `<div class="guild-weekly-task ${state}">
        <span class="guild-weekly-task-name">${filterEscape(row.name)}</span><span class="guild-weekly-task-state">${labels[state]}</span>
        <div class="guild-weekly-progress" role="progressbar" aria-label="${filterEscape(row.name)}" aria-valuemin="0" aria-valuemax="${known ? row.target : 100}" ${known ? `aria-valuenow="${Math.min(row.count, row.target)}"` : ''}><span style="width:${percent}%"></span></div>
        <span class="guild-weekly-count">${known ? `${row.count.toLocaleString()}/${row.target.toLocaleString()}` : '—'}</span>
      </div>`;
    }).join('');
    const note = !weekly.personalKnown && joined && weekly.week != null
      ? '<div class="guild-weekly-note">个人进度等待登录快照更新</div>' : '';
    const taskHTML = content + note || '<div class="guild-weekly-note">暂无本周任务资料</div>';
    if ($('guildWeeklyTasks').innerHTML !== taskHTML) $('guildWeeklyTasks').innerHTML = taskHTML;
    $('guildWeeklyScore').textContent = Number.isFinite(weekly.score) ? weekly.score.toLocaleString() : '—';
    const tierHTML = tiers.map(row => {
      const state = labels[row.state] ? row.state : 'unknown';
      const rewards = (row.rewards || []).map(reward => `<span class="guild-weekly-reward" title="${filterEscape(reward.name)} ×${reward.count}"><img src="/assets/items/${encodeURIComponent(reward.item)}.png" alt="${filterEscape(reward.name)}"><span>×${reward.count}</span></span>`).join('');
      return `<div class="guild-weekly-tier ${state}" aria-label="${row.target} 积分，${labels[state]}">
        <span class="guild-weekly-tier-target">${Number(row.target).toLocaleString()}</span>${rewards}
        <span class="guild-weekly-tier-state">${state === 'claimed' ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>' : ''}${state === 'progress' ? '未达成' : labels[state]}</span>
      </div>`;
    }).join('');
    if ($('guildWeeklyTiers').innerHTML !== tierHTML) $('guildWeeklyTiers').innerHTML = tierHTML;
  }
  function paint() {
    const account = activeAccount;
    const session = ACCOUNTS[account];
    const st = state(account);
    const guild = session?.guild;
    const joined = guild?.joined === true;
    const noGuild = Boolean(session) && guild?.joined === false;
    const connected = session?.connection?.connected !== false;
    const loading = st.refreshPending || guild?.loading === true;
    const claiming = joined && guild?.claiming === true;
    const donating = joined && Boolean(guild?.donating);
    const aiding = joined && Boolean(guild?.aid?.running);
    paintAvatar(guild);
    $('guildName').textContent = !session ? '未选择账号' : !guild ? '等待同步佣兵团信息'
      : joined ? (guild.name || '佣兵团信息待同步') : '当前尚未加入佣兵团';
    $('guildStats').classList.toggle('hidden', !joined);
    $('guildLevel').textContent = guild?.level ?? '—';
    $('guildMembers').textContent = guild?.memberCount ?? '—';
    const refresh = $('guildRefresh');
    refresh.classList.toggle('hidden', noGuild);
    refresh.disabled = !session || !connected || loading || claiming || donating || aiding || Boolean(guild?.weekly?.running)
      || noGuild || guild?.waiting === '等待每日重登';
    refresh.classList.toggle('loading', loading);
    refresh.setAttribute('aria-busy', String(loading));
    const toggle = $('guildAutoSignin');
    toggle.checked = Boolean(session?.toggles?.guild_auto_signin);
    toggle.disabled = !joined || st.savePending || claiming;
    toggle.closest('.auto-item').classList.toggle('on', toggle.checked);
    $('guildSigninState').classList.toggle('ready', joined && !guild.waiting && guild.canClaim && !guild.claimError);
    $('guildSigninState').classList.toggle('error', joined && !guild.waiting && Boolean(guild.claimError));
    $('guildSigninState').textContent = guild?.waiting ? guild.waiting : noGuild ? '未加入佣兵团，暂不执行'
      : !joined ? '每日首次进入佣兵团会自动报到'
      : guild.claimError ? '领取未确认，等待下次登录或手动刷新'
      : claiming ? '正在领取日奖励…'
      : !guild.checkedIn ? '今日尚未报到，进入佣兵团时自动完成'
      : guild.canClaim ? (toggle.checked ? '今日已报到，等待自动领取日奖励' : '今日已报到，日奖励可领取')
      : '今日已报到，暂无可领取的日奖励';
    const donateToggle = $('guildAutoDonate');
    donateToggle.checked = Boolean(session?.toggles?.guild_auto_donate);
    donateToggle.disabled = !joined || st.savePending || donating;
    $('guildDonateRow').classList.toggle('on', donateToggle.checked);
    $('guildDonateOptions').disabled = !joined;
    const options = donationOptions(session);
    $('guildDonateChoices').querySelectorAll('input[type="checkbox"]').forEach(input => {
      input.checked = Boolean(options[input.value]);
      input.disabled = !joined || st.savePending || donating;
    });
    const reserves = donationReserves(session);
    $('guildDonateChoices').querySelectorAll('.guild-donate-reserve').forEach(input => {
      const value = st.donationReserveDrafts[input.dataset.key] ?? String(reserves[input.dataset.key] ?? 0);
      if (input.value !== value) input.value = value;
      input.disabled = !joined || st.savePending || donating;
    });
    const selected = Object.keys(donationLabels).filter(key => options[key]);
    const completed = selected.length > 0 && selected.every(key => {
      const row = guild?.donations?.[key];
      return Number.isFinite(row?.donated) && Number.isFinite(row?.limit)
        && row.limit > 0 && row.donated >= row.limit;
    });
    const blocked = selected.some(key => guild?.donationErrors?.[key]);
    const status = $('guildDonateState');
    status.classList.toggle('error', joined && !guild.waiting && blocked);
    status.textContent = guild?.waiting ? guild.waiting : noGuild ? '未加入佣兵团，暂不执行'
      : !joined ? '等待同步捐赠状态'
      : donating ? `正在捐赠${donationLabels[guild.donating] || ''}…`
      : !selected.length ? '请在选项中勾选捐赠资源'
      : blocked ? '捐赠未确认，等待下次登录或手动刷新'
      : !guild.checkedIn ? '等待今日报到后确认捐赠次数'
      : `${completed ? '已捐赠：' : ''}${selected.map(key => {
          const row = guild.donations?.[key];
          const cost = donationCosts[key] || 1;
          const donated = Number.isFinite(Number(row?.donated)) ? Number(row.donated) * cost : '—';
          const limit = Number.isFinite(Number(row?.limit)) ? Number(row.limit) * cost : '—';
          return `${donationLabels[key]} ${donated}/${limit}`;
        }).join(' · ')}`;
    const aidToggle = $('guildAutoAid');
    const aid = guild?.aid || {};
    const aidConfig = aidOptions(session);
    const aidDisabled = !joined || st.savePending || aiding;
    aidToggle.checked = Boolean(session?.toggles?.guild_auto_aid);
    aidToggle.disabled = aidDisabled;
    $('guildAidRow').classList.toggle('on', aidToggle.checked);
    $('guildAidOptions').disabled = !joined;
    $('guildAidChoices').querySelectorAll('input').forEach(input => {
      input.checked = Boolean(aidConfig[input.value]);
      input.disabled = aidDisabled;
    });
    paintAidItem(session, aidDisabled);
    const aidState = $('guildAidState');
    aidState.classList.toggle('error', joined && !guild.waiting && Boolean(aid.error));
    const requestState = !aidConfig.item ? ''
      : !aid.requestKnown ? '等待同步求助状态' : aid.requested ? '今日已求助' : '今日尚未求助';
    const supportState = aidConfig.hunt || aidConfig.element || aidConfig.catalyst
      ? `援助奖励 ${aid.reward ?? '—'}/${aid.limit || 200}` : '';
    aidState.textContent = guild?.waiting ? guild.waiting : noGuild ? '未加入佣兵团，暂不执行'
      : !joined ? '等待同步援助状态'
      : aid.running ? ({ query: '正在检查团员求助…', request: '正在发布求助…', support: '正在援助团员…' }[aid.running] || '正在援助…')
      : aid.error ? '操作未确认，5 分钟后查询确认'
      : [requestState, supportState].filter(Boolean).join(' · ') || '请在选项中配置求助或援助范围';
    paintWeekly(session, st, guild, joined, noGuild);
  }
  async function refresh() {
    const account = activeAccount;
    const session = ACCOUNTS[account];
    if (!session || session.guild?.joined === false) return;
    const st = state(account);
    if (st.refreshPending) return;
    st.refreshPending = true; paint();
    try {
      const data = await api('/api/guild/overview', { account });
      if (ACCOUNTS[account] !== session) return;
      applyStateSyncMeta(account, data);
      applyStatus(account, data.status, data.nextClaims, data.arena, data.connection, data.redeemCodes);
    } catch (error) {
      if (ACCOUNTS[account] === session) {
        logTo(account, `[佣兵团] 刷新失败：${error.message}`, 'err');
      }
    } finally {
      st.refreshPending = false;
      if (account === activeAccount && ACCOUNTS[account] === session) paint();
    }
  }
  async function saveSettings(key, enabled, options) {
    const account = activeAccount;
    const session = ACCOUNTS[account];
    if (!session) return;
    const st = state(account);
    if (st.savePending) return;
    const before = Boolean(session.toggles[key]);
    const previousOptions = session.params?.[key];
    st.savePending = true;
    if (key === 'guild_auto_donate' && options) st.donationReserveDrafts = {};
    session.toggles[key] = enabled;
    if (options) session.params = { ...session.params, [key]: options };
    paint();
    try {
      await api('/api/toggles', { account, toggles: { ...session.toggles },
        ...(options ? { params: { [key]: options } } : {}) });
    } catch (error) {
      if (ACCOUNTS[account] === session) {
        session.toggles[key] = before;
        if (options) {
          if (previousOptions === undefined) delete session.params[key];
          else session.params[key] = previousOptions;
        }
        const name = { guild_auto_donate: '自动捐赠', guild_auto_aid: '自动援助', guild_auto_weekly: '周任务' }[key] || '自动签到';
        logTo(account, `[佣兵团] ${name}设置保存失败：${error.message}`, 'err');
      }
    } finally {
      st.savePending = false;
      if (account === activeAccount && ACCOUNTS[account] === session) paint();
    }
  }
  function init() {
    if (!$('section-guild')) return;
    $('guildRefresh').addEventListener('click', refresh);
    $('guildAutoSignin').addEventListener('change', () => saveSettings('guild_auto_signin', $('guildAutoSignin').checked));
    $('guildAutoDonate').addEventListener('change', () => saveSettings('guild_auto_donate', $('guildAutoDonate').checked));
    $('guildAutoAid').addEventListener('change', () => saveSettings('guild_auto_aid', $('guildAutoAid').checked));
    $('guildAutoWeekly').addEventListener('change', () => saveSettings('guild_auto_weekly', $('guildAutoWeekly').checked));
    // 佣兵团内所有 aria-controls 折叠区统一自动接入；新增同结构控件无需登记。
    const restorePanels = bindControlledAccountPanels($('section-guild'), 'guild');
    bindAccountPanel($('guildAidOptions'), $('guildAidBody'), 'guild:guildAidBody', { onOpen: async () => {
        const account = activeAccount;
        try { await loadAidCatalog(); paint(); }
        catch (error) {
          if (activeAccount !== account) return;
          const holder = $('guildAidItem');
          delete holder.dataset.selection;
          holder.textContent = '材料加载失败，请重新打开选项重试';
          logTo(account, `[佣兵团] ${error.message}`, 'err');
        }
    }});
    $('guildAidChoices').innerHTML = Object.entries(aidLabels).map(([key, label]) =>
      `<label class="bt-opt"><input type="checkbox" value="${key}">${checkboxUIHTML()}<span>${label}</span></label>`).join('');
    $('guildAidChoices').addEventListener('change', () =>
      saveSettings('guild_auto_aid', $('guildAutoAid').checked, aidSettings()));
    $('guildDonateChoices').innerHTML = Object.entries(donationLabels).map(([key, label]) =>
      `<div class="guild-donate-choice">
        <label class="bt-opt guild-donate-check"><input type="checkbox" value="${key}">${checkboxUIHTML()}<span class="guild-donate-label">${label}</span></label>
        <span class="guild-donate-reserve-label">保留</span>
        <input class="guild-donate-reserve" data-key="${key}" type="number" min="0" step="1" inputmode="numeric" aria-label="保留${label}数量" value="0">
      </div>`).join('');
    $('guildDonateChoices').addEventListener('input', event => {
      if (event.target.matches('.guild-donate-reserve') && ACCOUNTS[activeAccount]) {
        state(activeAccount).donationReserveDrafts[event.target.dataset.key] = event.target.value;
      }
    });
    $('guildDonateChoices').addEventListener('change', () => {
      saveSettings('guild_auto_donate', $('guildAutoDonate').checked, donationSettings());
    });
    ['guildIcon', 'guildFrame'].forEach(id => {
      $(id).addEventListener('load', paint);
      $(id).addEventListener('error', paint);
    });
    window.addEventListener('lucima:account-change', () => {
      restorePanels.forEach((restore) => restore?.());
      paint();
    });
    window.addEventListener('lucima:guild-change', event => {
      if (event.detail.account !== activeAccount) return;
      paint();
    });
    paint();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
