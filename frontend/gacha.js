/* Summon jobs live in Python; navigating or switching accounts only changes the view. */
function gachaState(email = activeAccount) {
  const ui = accountUiState(email);
  return ui ? (ui.gacha ||= { draft: null, data: null, job: null, starting: false, stopping: false, pendingId: null, load: 0, saving: null }) : null;
}

function gachaVisible() {
  return currentSection === 'toolbox' && !$('gachaPage')?.classList.contains('hidden');
}

function applyGachaJob(email, job) {
  const state = gachaState(email);
  if (!state || !job) return;
  const previous = state.job;
  if (previous && (Number(job.sequence || 0) < Number(previous.sequence || 0)
    || job.id === previous.id && Number(job.revision || 0) < Number(previous.revision || 0))) return;
  state.job = job;
  if (job.params) {
    const pool = state.data?.pools.find(entry => entry.id === job.params.poolId);
    for (const entry of state.data?.pools || []) {
      for (const price of entry.prices) {
        if (Number.isFinite(job.balances?.[price.item])) price.balance = job.balances[price.item];
      }
    }
    if (pool && Number.isFinite(job.guaranteedCount)) pool.guaranteedCount = job.guaranteedCount;
    if (pool && Number.isFinite(job.freeRemaining)) pool.freeRemaining = job.freeRemaining;
  }
  if (job.running) state.draft = { ...job.params };
  if (activeAccount !== email || !gachaVisible()) return;
  renderGachaProgress();
  if (!!previous?.running !== !!job.running) {
    renderGachaSettings();
    if (!job.running) void loadGachaOptions(email);
  }
}

async function loadGachaOptions(email = activeAccount) {
  const state = gachaState(email);
  if (!state) return;
  const request = ++state.load;
  try {
    const data = await api('/api/gacha/options?account=' + encodeURIComponent(email));
    if (gachaState(email) !== state || request !== state.load) return;
    state.data = data;
    if (!state.draft) state.draft = { count: 100, stopRole: false, stopArtifact: false, preferExclusive: true, ...data.config };
    applyGachaJob(email, data.job);
    if (activeAccount === email && gachaVisible()) {
      renderGachaSettings();
      renderGachaProgress();
    }
  } catch (error) {
    if (activeAccount === email && gachaVisible()) {
      $('gachaSettings').textContent = `卡池加载失败：${error.message}`;
    }
  }
}

function enterGachaPage() {
  const state = gachaState();
  if (!state) return;
  renderGachaSettings();
  renderGachaProgress();
  void loadGachaOptions();
}

function saveGachaSettings(email) {
  const state = gachaState(email), account = ACCOUNTS[email];
  if (!state?.draft || !account) return;
  const params = { ...state.draft, count: Number(state.draft.count) };
  if (!params.poolId || !Number.isInteger(params.count) || params.count < 1 || params.count > 9999) return;
  account.params = { ...account.params, gacha: params };
  // Serialize changes per account so a slower old save cannot restore old choices.
  state.saving = (state.saving || Promise.resolve()).then(async () => {
    try {
      await api('/api/toggles', { account: email, toggles: null, params: { gacha: params } });
    } catch (error) {
      logTo(email, `保存抽卡设置失败：${error.message}`, 'err');
    }
  });
  return state.saving;
}

function renderGachaSettings() {
  const root = $('gachaSettings'), state = gachaState();
  if (!root || !state) return;
  const data = state.data;
  if (!data) { root.innerHTML = '<div class="tdesc">正在加载卡池…</div>'; return; }
  const pools = data.pools || [], draft = state.draft;
  if (!draft.poolId && pools.length) {
    draft.poolId = pools[0].id;
    draft.stopRole = !!pools[0].roles?.length;
  }
  const pool = pools.find(entry => entry.id === draft.poolId);
  const busy = state.starting || state.job?.running;
  const poolOptions = pools.map(entry => ({ val: entry.id, label: filterEscape(entry.name) }));
  if (draft.poolId && !pool) poolOptions.unshift({ val: draft.poolId, label: '原卡池已结束，请重新选择' });
  root.innerHTML = `<div class="card-title"><span class="dot"></span><span>招募设置</span></div>
    <div class="gacha-form">
      <div class="field"><label>卡池</label>${dropdownHTML('gacha-pool', poolOptions, draft.poolId, '暂无开放卡池')}</div>
      <div class="field" id="gachaPityField">
        <label id="gachaPityLabel">保底计数</label>
        <div class="gacha-progress-row">
          <div class="guild-weekly-progress gacha-progress" id="gachaPityProgress" role="progressbar" aria-labelledby="gachaPityLabel" aria-valuemin="0"><span></span></div>
          <span class="gacha-progress-count" id="gachaPityCount" role="status" aria-live="polite"></span>
        </div>
      </div>
      <div class="gacha-targets">${gachaTargetHTML('UP 角色', pool?.roles, 'role')}${gachaTargetHTML('UP 羁绊', pool?.artifacts, 'artifact')}</div>
      <div class="gacha-contracts" id="gachaContracts" role="group" aria-label="抽卡道具">${gachaContractsHTML(pool)}</div>
      <div class="field gacha-count-row"><label for="gachaDrawCount">最多抽取次数</label><div class="count-box gacha-count">
          <button class="count-btn" type="button" data-gacha-step="-1" aria-label="减少抽取次数"><svg aria-hidden="true"><use href="#ui-minus"></use></svg></button>
          <input class="count-in" id="gachaDrawCount" type="text" inputmode="numeric" maxlength="4" value="${filterEscape(String(draft.count ?? 100))}">
          <span class="count-unit">次</span><button class="count-btn" type="button" data-gacha-step="1" aria-label="增加抽取次数"><svg aria-hidden="true"><use href="#ui-plus"></use></svg></button>
      </div></div>
      ${gachaToggleHTML('gachaPreferExclusive', '优先使用专属契约', draft.preferExclusive !== false)}
      ${gachaToggleHTML('gachaStopRole', '抽到 UP 角色停止', draft.stopRole, !pool?.roles?.length)}
      ${gachaToggleHTML('gachaStopArtifact', '抽到 UP 羁绊停止', draft.stopArtifact, !pool?.artifacts?.length)}
      <button class="btn primary" id="gachaStart" type="button">▶ 开始抽卡</button>
    </div>`;
  const email = activeAccount;
  bindDropdown(root.querySelector('.gacha-pool'), value => {
    draft.poolId = value;
    const selected = pools.find(entry => entry.id === value);
    draft.stopRole = !!selected?.roles?.length && draft.stopRole;
    draft.stopArtifact = !!selected?.artifacts?.length && draft.stopArtifact;
    renderGachaSettings();
    void saveGachaSettings(email);
  });
  const countInput = $('gachaDrawCount');
  const setCount = value => {
    draft.count = Math.max(1, Math.min(9999, Number.parseInt(value, 10) || 1));
    countInput.value = String(draft.count);
    void saveGachaSettings(email);
  };
  countInput.addEventListener('input', () => {
    countInput.value = countInput.value.replace(/[^0-9]/g, '').slice(0, 4);
    draft.count = countInput.value;
  });
  countInput.addEventListener('blur', () => setCount(countInput.value));
  root.querySelectorAll('[data-gacha-step]').forEach(button => button.addEventListener('click', () => {
    setCount((Number.parseInt(countInput.value, 10) || 1) + Number(button.dataset.gachaStep));
  }));
  for (const [id, key] of [['gachaPreferExclusive', 'preferExclusive'], ['gachaStopRole', 'stopRole'], ['gachaStopArtifact', 'stopArtifact']]) {
    $(id).addEventListener('change', event => { draft[key] = event.target.checked; void saveGachaSettings(email); });
  }
  $('gachaStart').addEventListener('click', () => {
    if (gachaState(email)?.job?.running) void stopGacha(email);
    else void startGacha(email);
  });
  root.querySelectorAll('.gacha-target-media img').forEach(img => img.addEventListener('error', () => { img.hidden = true; }));
  bindGachaContractImages();
  root.querySelectorAll('.dd-btn, .count-btn, input').forEach(control => {
    control.disabled ||= !!busy;
  });
  renderGachaProgress();
}

function gachaContractsHTML(pool) {
  return pool?.prices.map(price => `<span class="voting-ticket gacha-contract" title="${filterEscape(price.name)}" aria-label="${filterEscape(price.name)}，持有 ${filterEscape(fmtNum2(price.balance))}">
    <img src="${filterEscape(itemIcon(price.item))}" alt="" loading="lazy"><span>×</span><b data-gacha-balance="${filterEscape(price.item)}">${fmtNum2(price.balance)}</b></span>`).join('') || '<span class="tdesc">暂无抽卡道具</span>';
}

function bindGachaContractImages() {
  $('gachaContracts')?.querySelectorAll('img').forEach(img => img.addEventListener('error', () => {
    if (img.dataset.fallback) { img.hidden = true; return; }
    img.dataset.fallback = 'true';
    img.src = '/assets/icons/recruit.png';
  }));
}

function gachaTargetHTML(label, entries, kind) {
  return `<div class="gacha-target"><span class="gacha-target-label">${label}</span>${entries?.length ? entries.map(entry => `
    <div class="gacha-target-item"><span class="gacha-target-media ${kind}"><span aria-hidden="true">✦</span><img src="${filterEscape(entry.image)}" alt="" loading="lazy"></span><span>${filterEscape(entry.name)}</span></div>`).join('')
    : '<span class="tdesc">无 UP 目标</span>'}</div>`;
}

function gachaToggleHTML(id, label, checked, disabled) {
  return `<label class="gacha-stop-row request-delay-toggle"><span>${label}</span><span class="target-score-toggle"><input type="checkbox" id="${id}" role="switch" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}><span class="target-score-switch" aria-hidden="true"></span></span></label>`;
}

function renderGachaProgress() {
  const state = gachaState(), job = state?.job;
  const button = $('gachaStart'), progress = $('gachaPityProgress');
  if (!state || !gachaVisible() || !button || !progress) return;
  const running = !!job?.running, stopping = !!state.stopping || job?.state === 'stopping';
  button.textContent = state.starting ? '启动中…' : running ? (stopping ? '停止中…' : '停止') : '▶ 开始抽卡';
  button.classList.toggle('task-stop-button', running);
  button.disabled = !!state.starting || !!state.stopping || (running ? stopping : !!job?.blocked
    || !state.data?.pools.some(pool => pool.id === state.draft?.poolId && pool.prices.length));
  button.title = job?.blocked ? '本次抽卡结果未确认，等待登录同步刷新' : '';
  const pool = state.data?.pools.find(entry => entry.id === state.draft?.poolId);
  const count = Math.max(0, Number(pool?.guaranteedCount) || 0);
  const total = Math.max(0, Number(pool?.guarantee) || 0);
  $('gachaPityField').classList.toggle('hidden', total <= 0);
  $('gachaPityCount').textContent = `${count} / ${total}`;
  const percent = total ? Math.min(100, count / total * 100) : 0;
  progress.setAttribute('aria-valuemax', String(total));
  progress.setAttribute('aria-valuenow', String(Math.min(count, total)));
  progress.setAttribute('aria-valuetext', `保底计数 ${count} / ${total}`);
  progress.firstElementChild.style.width = `${percent}%`;
  if (pool) {
    for (const price of pool.prices) {
      const amount = $('gachaContracts')?.querySelector(`[data-gacha-balance="${CSS.escape(price.item)}"]`);
      if (amount) {
        amount.textContent = fmtNum2(price.balance);
        amount.parentElement.setAttribute('aria-label', `${price.name}，持有 ${fmtNum2(price.balance)}`);
      }
    }
  }
}

async function startGacha(email) {
  const state = gachaState(email);
  if (!state || state.starting || state.job?.running) return;
  if (!/^[1-9][0-9]{0,3}$/.test(String(state.draft?.count))) {
    showMessageToast('抽取次数须为 1–9999 的整数', 'err'); return;
  }
  state.starting = true;
  state.pendingId ||= typeof crypto.randomUUID === 'function' ? crypto.randomUUID()
    : '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, value =>
      (Number(value) ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> Number(value) / 4).toString(16));
  const params = { poolId: state.draft.poolId, count: Number(state.draft.count),
    stopRole: !!state.draft.stopRole, stopArtifact: !!state.draft.stopArtifact,
    preferExclusive: state.draft.preferExclusive !== false };
  renderGachaSettings();
  try {
    await state.saving;
    const data = await api('/api/gacha/start', { account: email, params, requestId: state.pendingId });
    if (gachaState(email) !== state) return;
    state.pendingId = null;
    applyGachaJob(email, data.job);
  } catch (error) {
    if (activeAccount === email) showMessageToast(`抽卡未能开始：${error.message}`, 'err');
    // If the local start reply was lost, read the existing job before any retry.
    await loadGachaOptions(email);
  } finally {
    state.starting = false;
    if (activeAccount === email && gachaVisible()) { renderGachaSettings(); renderGachaProgress(); }
  }
}

async function stopGacha(email) {
  const state = gachaState(email);
  if (!state?.job?.running || state.stopping || state.job.state === 'stopping') return;
  state.stopping = true;
  if (activeAccount === email && gachaVisible()) renderGachaProgress();
  try {
    const data = await api('/api/gacha/cancel', { account: email, id: state.job.id });
    applyGachaJob(email, data.job);
  } catch (error) {
    if (activeAccount === email) showMessageToast(`停止失败：${error.message}`, 'err');
  } finally {
    state.stopping = false;
    if (activeAccount === email && gachaVisible()) renderGachaProgress();
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('gachaBack')?.addEventListener('click', showToolboxHome);
});
window.addEventListener('lucima:account-change', () => { if (gachaVisible()) enterGachaPage(); });
