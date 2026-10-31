/* Cached activity options; game requests remain in the account scheduler. */
function renderVotingOptions() {
  return '<div class="voting-wrap arena-wrap"><div class="tdesc">正在加载投票活动…</div></div>';
}

function bindVotingOptions(id) {
  const root = $(`auto-${id}`);
  const hydrate = () => renderVotingProgress(id);
  bindAccountPanel(root.querySelector('.voting-toggle'), root.querySelector('.auto-body'), `auto:${id}`, {
    onOpen: () => loadVotingOptions(id), onRestore: hydrate,
  });
  hydrate();
}

async function loadVotingOptions(id) {
  const root = $(`auto-${id}`), email = activeAccount;
  if (!root || root._votingLoading) return;
  root._votingLoading = true;
  try {
    const data = await api('/api/voting/options?account=' + encodeURIComponent(email));
    setAccountOptionCache('voting', data, email);
    if (activeAccount === email && root.isConnected) renderVotingProgress(id);
  } catch (error) {
    if (activeAccount === email && root.isConnected) {
      root.querySelector('.voting-wrap').textContent = `加载失败：${error.message}`;
      root._votingShape = null;
    }
  } finally { root._votingLoading = false; }
}

function votingAbstainIcon() {
  return '<span class="guild-aid-material"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/></svg></span>';
}

function votingFilters(aid, email = activeAccount) {
  const ui = accountUiState(email);
  if (!ui) return { element: [], job: [] };
  ui.votingFilters ||= Object.create(null);
  return ui.votingFilters[aid] ||= { element: [], job: [] };
}

function bindVotingPicker(section, id, dataVersion) {
  const email = activeAccount, aid = section.dataset.voteActivity;
  const key = `auto:${id}:target:${aid}`;
  const dropdown = section.querySelector('.dropdown');
  const button = dropdown.querySelector('.dd-btn'), menu = dropdown.querySelector('.dd-menu');
  bindDropdown(dropdown);
  button.addEventListener('click', () => {
    const expanded = !menu.classList.contains('hidden');
    setAccountPanelExpanded(key, expanded, email);
    if (expanded) {
      const saved = accountOptionCache('voting', email);
      if (saved?.dataVersion !== dataVersion || !saved?.activities?.find(a => a.id === aid)?.candidates?.length)
        void loadVotingOptions(id);
    }
  });
  // The shared dropdown also closes on outside clicks or when another menu opens.
  const observer = new MutationObserver(() => {
    if (section.isConnected) setAccountPanelExpanded(key, !menu.classList.contains('hidden'), email);
  });
  observer.observe(menu, { attributes: true, attributeFilter: ['class'] });
  section.querySelector('.voting-filters').addEventListener('click', event => {
    const button = event.target.closest('[data-voting-filter]');
    if (!button) return;
    event.stopPropagation();
    const type = button.dataset.votingFilter, value = button.dataset.value;
    const filters = votingFilters(aid, email), selected = filters[type];
    filters[type] = value === 'ALL' ? []
      : selected.includes(value) ? selected.filter(item => item !== value) : [...selected, value];
    renderVoteCandidates(section, id);
    section.querySelector(`[data-voting-filter="${type}"][data-value="${value}"]`)?.focus({ preventScroll: true });
    positionDropdown(dropdown);
  });
  renderVoteCandidates(section, id);
  if (accountPanelExpanded(key, false, email)) {
    menu.classList.remove('hidden');
    dropdown.classList.add('open');
    button.setAttribute('aria-expanded', 'true');
    positionDropdown(dropdown);
  }
  dropdown.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    menu.classList.add('hidden');
    dropdown.classList.remove('open');
    button.setAttribute('aria-expanded', 'false');
    button.focus({ preventScroll: true });
    event.stopPropagation();
  });
}

function renderVotingProgress(id = 'activity_vote') {
  const root = $(`auto-${id}`);
  if (!root) return;
  const host = root.querySelector('.voting-wrap');
  if (!host) return;
  const cache = accountOptionCache('voting');
  const state = NEXT_CLAIMS[id] || cache;
  if (!state) return;
  const activities = state.activities || [];
  const shape = JSON.stringify([activeAccount, state.dataVersion, activities.map(a => a.id),
    cache?.dataVersion, cache?.activities?.map(a => [a.id, a.candidates?.length])]);
  if (root._votingShape !== shape) {
    root._votingShape = shape;
    host.innerHTML = activities.map(a => `<div class="voting-activity" data-vote-activity="${filterEscape(a.id)}">
      <div class="voting-heading"><strong>${filterEscape(a.name)}</strong><span class="voting-ticket"></span></div>
      <div class="tdesc voting-end"></div>
      <div class="dropdown voting-select">
        <button type="button" class="dd-btn voting-target" aria-controls="vote-target-${filterEscape(a.id)}"><span class="dd-cur"></span><span class="dd-caret">▾</span></button>
        <div class="dd-menu voting-picker hidden" id="vote-target-${filterEscape(a.id)}" aria-label="选择投票团员">
          <div class="voting-filters">
            <div class="encyclopedia-filter-group"><span class="encyclopedia-filter-label">属性</span><div class="encyclopedia-filter-options" data-vote-filters="element"></div></div>
            <div class="encyclopedia-filter-group"><span class="encyclopedia-filter-label">职业</span><div class="encyclopedia-filter-options" data-vote-filters="job"></div></div>
          </div>
          <div class="encyclopedia-grid voting-candidates"></div>
        </div>
      </div>
      <div class="guild-weekly-tasks voting-quests"></div>
    </div>`).join('') || `<div class="tdesc">${filterEscape(state.note || '暂无进行中的投票活动')}</div>`;
    host.querySelectorAll('.voting-activity').forEach(section => bindVotingPicker(section, id, state.dataVersion));
  }
  activities.forEach(a => {
    const section = [...host.querySelectorAll('.voting-activity')].find(el => el.dataset.voteActivity === a.id);
    if (!section) return;
    section.querySelector('.voting-ticket').innerHTML = `<img src="${itemIcon(filterEscape(a.ticket))}" alt="投票道具" title="投票道具" loading="lazy"><span>×${Number(a.tickets).toLocaleString()}</span>`;
    section.querySelector('.voting-end').textContent = `结束时间：${fmtClock(a.endMs)}`;
    const cfg = ACCOUNTS[activeAccount]?.params?.[id]?.targets || {};
    const target = cfg[a.id] || '';
    const entry = (cache?.activities || []).find(row => row.id === a.id)?.candidates?.find(row => row.id === target);
    const name = entry?.name || (a.target === target ? a.targetName : '') || target;
    section.querySelector('.dd-cur').innerHTML = `${target ? `<span class="guild-aid-material"><img src="/assets/avatars/${filterEscape(target)}.png" alt="" loading="lazy"></span>` : votingAbstainIcon()}<span class="guild-aid-selected-name">${filterEscape(target ? name : '暂不投票')}</span>`;
    section.querySelector('.voting-select').dataset.val = target;
    section.querySelector('.voting-quests').innerHTML = (a.quests || []).map(q => {
      const known = Number.isFinite(q.count) && Number.isFinite(q.target) && q.target > 0;
      const percent = known ? Math.max(0, Math.min(100, q.count / q.target * 100)) : 0;
      const status = q.claimed ? 'claimed' : q.ready ? 'ready' : known ? 'progress' : 'unknown';
      const label = { claimed: '已领取', ready: '可领取', progress: '进行中', unknown: '待同步' }[status];
      return `<div class="guild-weekly-task ${status}">
        <span class="guild-weekly-task-name">${filterEscape(q.name.replace(/^【每日】/, ''))}</span>
        <span class="guild-weekly-task-state">${label}</span>
        <div class="guild-weekly-progress" role="progressbar" aria-label="${filterEscape(q.name)}" aria-valuemin="0" aria-valuemax="${known ? q.target : 100}" ${known ? `aria-valuenow="${Math.max(0, Math.min(q.count, q.target))}"` : ''}><span style="width:${percent}%"></span></div>
        <span class="guild-weekly-count">${known ? `${q.count.toLocaleString()}/${q.target.toLocaleString()}` : '—'}</span>
      </div>`;
    }).join('');
    section.querySelectorAll('[data-vote-role]').forEach(button => {
      const selected = button.dataset.voteRole === target;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  });
}

function renderVoteCandidates(section, id) {
  const aid = section.dataset.voteActivity, email = activeAccount;
  const data = accountOptionCache('voting');
  const candidates = data?.activities?.find(a => a.id === aid)?.candidates || [];
  const filters = votingFilters(aid, email);
  for (const [type, options] of [['element', ENCYCLOPEDIA_ELEMENTS], ['job', ENCYCLOPEDIA_JOBS]]) {
    section.querySelector(`[data-vote-filters="${type}"]`).innerHTML = options.map(option =>
      encyclopediaFilterButtonHTML(option, type, option.id === 'ALL' ? !filters[type].length : filters[type].includes(option.id), 'voting')).join('');
  }
  const target = ACCOUNTS[email]?.params?.[id]?.targets?.[aid] || '';
  const list = section.querySelector('.voting-candidates');
  const visible = candidates.filter(c => characterMatchesFilter(c.element, filters.element) && characterMatchesFilter(c.job, filters.job));
  const choices = [{ id: '', name: '暂不投票' }, ...visible];
  list.innerHTML = choices.map(c => `<button type="button" class="encyclopedia-preview voting-candidate ${c.id === target ? 'selected' : ''}" data-vote-role="${filterEscape(c.id)}" aria-pressed="${c.id === target}" title="${filterEscape(c.name)}">
    <span class="encyclopedia-preview-avatar">${c.id ? `<img src="/assets/avatars/${filterEscape(c.id)}.png" alt="" loading="lazy">` : votingAbstainIcon()}</span>
    <span class="encyclopedia-preview-name">${filterEscape(c.name)}</span>
  </button>`).join('') + (!candidates.length ? '<div class="tdesc voting-candidates-empty">正在加载候选名单…</div>'
    : !visible.length ? '<div class="tdesc voting-candidates-empty">没有符合条件的团员</div>' : '');
  list.querySelectorAll('[data-vote-role]').forEach(button => button.addEventListener('click', async () => {
    const account = ACCOUNTS[email];
    if (!account || email !== activeAccount || section._votingSaving) return;
    const previous = account.params?.[id] || {};
    const selected = { ...previous, targets: { ...previous.targets, [aid]: button.dataset.voteRole } };
    section._votingSaving = true;
    section.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      const result = await api('/api/toggles', { account: email, toggles: account.toggles, params: { [id]: selected } });
      account.params = { ...account.params, [id]: result.params?.[id] || selected };
      setAccountPanelExpanded(`auto:${id}:target:${aid}`, false, email);
      if (email === activeAccount && section.isConnected) {
        section.querySelector('.voting-picker').classList.add('hidden');
        section.querySelector('.dropdown').classList.remove('open');
        section.querySelector('.voting-target').setAttribute('aria-expanded', 'false');
        renderVotingProgress(id);
        await refreshAccountStatus(email);
      }
    } catch (error) { logTo(email, `保存投票目标失败：${error.message}`, 'err'); }
    finally {
      section._votingSaving = false;
      if (section.isConnected) section.querySelectorAll('button').forEach(b => { b.disabled = false; });
    }
  }));
}
