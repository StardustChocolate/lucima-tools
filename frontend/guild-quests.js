/* Quest rewards use api() and the selected account's existing game session. */
(() => {
  const $ = id => document.getElementById(id);
  const states = new Map();
  const active = new Set(['running', 'stopping']);
  const labels = { idle: '待操作', running: '执行中', stopping: '正在停止', completed: '已完成', failed: '执行失败', cancelled: '已停止', unknown: '待确认' };
  const idleText = '尚未开始';
  let initialized = false;
  const storageKey = account => `ark_guild_quests_pending::${account}`;
  const isCurrent = st => activeAccount === st.account && states.get(st.account) === st;
  const isVisible = st => isCurrent(st) && currentSection === 'guild' && !document.hidden;
  const message = error => error?.message || String(error || '未知错误');
  function requestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }
  function getState(account) {
    if (!states.has(account) || states.get(account).session !== ACCOUNTS[account]) {
      const id = frontendStorageGet(storageKey(account));
      states.set(account, { account, session: ACCOUNTS[account], id, job: { state: 'idle' }, rounds: '30', questIds: [], guildName: '', queried: false,
        uncertain: Boolean(id), checked: false, starting: false, checking: false, querying: false, cancelling: false,
        recovering: false, sequence: 0, timer: null, syncedJob: null, text: idleText, tone: 'idle', queryError: '' });
    }
    return states.get(account);
  }
  function persist(st) {
    if (st.id && (st.uncertain || st.starting || active.has(st.job.state))) frontendStorageSet(storageKey(st.account), st.id);
    else frontendStorageRemove(storageKey(st.account));
  }
  function result(st, text, tone = 'idle') { st.text = text; st.tone = tone; paint(st); }
  function paint(st) {
    if (!isCurrent(st) && !(st.account == null && activeAccount == null)) return;
    const job = st.job, running = active.has(job.state);
    const busy = !st.account || st.starting || st.checking || st.recovering || st.uncertain || running || !st.checked;
    $('guildQuestStart').disabled = busy || st.querying;
    $('guildQuestStart').textContent = st.starting ? '正在提交…' : running ? '领取进行中…' : st.uncertain ? '等待确认任务' : '开始领取';
    $('guildQuestStop').disabled = !running || st.starting || st.cancelling || job.state === 'stopping';
    $('guildQuestStop').textContent = st.cancelling || job.state === 'stopping' ? '正在停止…' : '停止';
    $('guildQuestRounds').disabled = busy;
    $('guildQuestQuery').disabled = busy || st.querying;
    $('guildQuestQuery').textContent = st.querying ? '查询中…' : '查询任务';
    $('guildQuestRefresh').disabled = !st.account || st.checking || st.starting || st.recovering;
    $('guildQuestRecoveryActions').classList.toggle('hidden', job.state !== 'unknown');
    $('guildQuestResolve').disabled = st.checking || st.recovering;
    $('guildQuestBadge').textContent = st.starting ? '正在提交' : st.uncertain ? '待确认' : labels[job.state] || '待确认';
    $('guildQuestBadge').dataset.tone = st.uncertain ? 'unknown' : job.state;
    $('guildQuestResult').textContent = st.text;
    $('guildQuestResult').dataset.tone = st.tone;
    $('guildQuestListText').textContent = st.querying ? '正在查询当前佣兵团…' : st.queryError || (st.queried ? `${st.guildName ? st.guildName + ' · ' : ''}${st.questIds.length} 项任务` : '尚未查询');
    $('guildQuestListDetails').classList.toggle('hidden', !st.questIds.length);
    $('guildQuestIds').replaceChildren(...st.questIds.map(id => { const li = document.createElement('li'); li.textContent = id; return li; }));
    const total = Math.max(0, Number(job.requested) || 0), done = Math.max(0, Number(job.completed) || 0);
    const round = Math.max(0, Number(job.round) || 0), rounds = Math.max(1, Number(job.rounds) || Number(st.rounds) || 30);
    $('guildQuestProgressText').textContent = job.state === 'idle' ? '尚未开始' : st.uncertain ? '正在核对任务状态' : `第 ${round} / ${rounds} 轮 · ${labels[job.state] || '待确认'}`;
    $('guildQuestProgressCount').textContent = `已处理 ${done} / ${total} 项`;
    $('guildQuestProgressBar').style.width = `${total ? Math.min(100, done / total * 100) : 0}%`;
    $('guildQuestProgress').setAttribute('aria-valuenow', String(done));
    $('guildQuestProgress').setAttribute('aria-valuemax', String(Math.max(total, done, 1)));
    $('guildQuestSucceeded').textContent = String(Math.max(0, Number(job.succeeded) || 0));
    $('guildQuestFailed').textContent = String(Math.max(0, Number(job.failed) || 0));
    $('guildQuestNoDetails').textContent = String(Math.max(0, Number(job.noDetails) || 0));
    $('guildQuestRequested').textContent = String(total);
    const results = Array.isArray(job.results) ? job.results : [];
    $('guildQuestResultsDetails').classList.toggle('hidden', !results.length);
    $('guildQuestResults').replaceChildren(...results.map(item => {
      const li = document.createElement('li'), heading = document.createElement('div'), id = document.createElement('strong'), status = document.createElement('span'), text = document.createElement('div');
      li.dataset.state = item.state;
      heading.className = 'guild-quest-result-heading';
      id.textContent = `第 ${item.round} 轮 · ${item.questId}`;
      status.textContent = item.state === 'success' ? '返回数据' : item.state === 'received' ? '无奖励明细' : item.state === 'rejected' ? '被拒绝' : '待确认';
      text.className = 'guild-quest-result-message';
      text.textContent = item.state === 'received' ? '' : item.message || (item.state === 'success' ? '游戏返回了奖励或任务相关数据，请以游戏内实际结果为准。' : item.state === 'rejected' ? '游戏明确拒绝本次请求。' : '请求状态待确认。');
      heading.append(id, status); li.append(heading);
      if (text.textContent) li.append(text);
      if (item.responseFields) {
        const details = document.createElement('details'), summary = document.createElement('summary'), fields = document.createElement('pre');
        details.className = 'guild-quest-response-fields';
        summary.textContent = '返回字段';
        fields.textContent = String(item.responseFields);
        details.append(summary, fields); li.append(details);
      }
      return li;
    }));
  }
  function updateJob(st, job) {
    if (states.get(st.account) !== st) return;
    if (!job || !Object.prototype.hasOwnProperty.call(labels, job.state) || (job.account && job.account !== st.account) || (job.kind && job.kind !== 'quest_rewards')) throw new Error('任务状态响应不完整，请刷新进度确认。');
    st.job = job;
    st.id = job.id || st.id;
    st.checked = true;
    st.uncertain = job.state === 'unknown';
    if (Array.isArray(job.questIds)) { st.questIds = job.questIds.map(String); st.queried = true; st.queryError = ''; }
    if (job.guildName) st.guildName = String(job.guildName);
    if (job.state === 'idle') { st.id = null; result(st, idleText); }
    else if (st.uncertain) result(st, '查不到此任务的确定状态。请先核对游戏内结果，再解除待确认状态；不会自动重试领取。', 'error');
    else result(st, job.message || (job.state === 'completed' ? '逐个领取任务已结束，请查看逐项结果。' : job.state === 'cancelled' ? '已停止后续领取。' : '等待游戏返回领取结果…'), job.state === 'failed' ? 'error' : active.has(job.state) ? 'running' : 'ok');
    persist(st);
    if (job.id && ['completed', 'failed', 'cancelled'].includes(job.state) && st.syncedJob !== job.id) {
      st.syncedJob = job.id;
      const session = ACCOUNTS[st.account];
      api(`/api/status?account=${encodeURIComponent(st.account)}`)
        .then(data => { if (st.job.id === job.id) applyGuildStatus(st.account, data, session); })
        .catch(() => { if (st.syncedJob === job.id) st.syncedJob = null; });
    }
  }
  async function timedRequest(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try { return await api(path, body, { signal: controller.signal }); }
    catch (error) { if (controller.signal.aborted) throw new Error('等待响应超时'); throw error; }
    finally { clearTimeout(timer); }
  }
  function stopPolling(st) { clearTimeout(st.timer); st.timer = null; }
  function schedule(st) {
    stopPolling(st);
    if (isVisible(st) && !st.recovering && (active.has(st.job.state) || st.uncertain)) st.timer = setTimeout(() => { st.timer = null; check(st); }, st.uncertain ? 3500 : 1200);
  }
  async function check(st) {
    if (!isVisible(st) || st.checking || st.starting || st.recovering) return;
    stopPolling(st); st.checking = true; paint(st);
    const seq = ++st.sequence, id = st.uncertain || active.has(st.job.state) ? st.id : null;
    try {
      const job = await timedRequest(`/api/guild/quests?account=${encodeURIComponent(st.account)}${id ? '&id=' + encodeURIComponent(id) : ''}`);
      if (seq === st.sequence) updateJob(st, job);
    } catch (error) {
      if (seq !== st.sequence) return;
      st.uncertain = true;
      result(st, `无法确认任务状态：${message(error)}\n请刷新进度确认后再操作。`, 'error');
      persist(st);
    } finally { if (seq === st.sequence) { st.checking = false; paint(st); schedule(st); } }
  }
  async function query() {
    const account = activeAccount;
    if (!account) return;
    const st = getState(account);
    if (st.querying || st.starting || st.uncertain || active.has(st.job.state)) return;
    st.querying = true; st.queryError = ''; paint(st);
    try {
      const data = await timedRequest('/api/guild/quests/query', { account });
      if (!Array.isArray(data?.questIds)) throw new Error('游戏未返回可识别的任务列表。');
      st.questIds = data.questIds.map(String); st.guildName = String(data.guildName || ''); st.queried = true;
      result(st, st.questIds.length ? `已查询到 ${st.questIds.length} 项任务；领取条件由游戏逐项判断。` : '当前佣兵团没有返回任务，暂时没有可提交的领取请求。', 'idle');
    } catch (error) { st.queryError = '任务列表查询未完成'; result(st, `查询失败：${message(error)}`, 'error'); }
    finally { st.querying = false; paint(st); }
  }
  async function start() {
    const account = activeAccount;
    if (!account) return;
    const st = getState(account);
    if (!st.checked || st.starting || st.checking || st.querying || st.uncertain || active.has(st.job.state)) return;
    st.rounds = $('guildQuestRounds').value;
    const rounds = Number(st.rounds);
    if (!Number.isInteger(rounds) || rounds < 1 || rounds > 100) return result(st, '请输入 1–100 之间的整数轮数。', 'error');
    stopPolling(st); st.sequence++;
    st.id = requestId(); st.starting = true; st.uncertain = false;
    st.job = { id: st.id, account, kind: 'quest_rewards', state: 'running', rounds, round: 0, requested: 0, completed: 0, succeeded: 0, failed: 0, noDetails: 0, results: [] };
    result(st, '正在提交任务，将先查询佣兵团任务列表…', 'running'); persist(st);
    try { updateJob(st, await timedRequest('/api/guild/quests/start', { account, rounds, requestId: st.id })); }
    catch (error) {
      if (error.httpStatus >= 400 && error.httpStatus < 500) {
        st.job = { ...st.job, state: 'failed' }; st.id = null; st.uncertain = false;
        result(st, `未启动领取：${message(error)}`, 'error');
      } else { st.uncertain = true; result(st, `提交结果尚未确认：${message(error)}\n正在核对任务，请勿重复提交。`, 'error'); }
    } finally { st.starting = false; persist(st); paint(st); schedule(st); }
  }
  async function cancel() {
    const account = activeAccount;
    if (!account) return;
    const st = getState(account);
    if (st.cancelling || st.starting || !st.id || !active.has(st.job.state)) return;
    st.cancelling = true; stopPolling(st); st.sequence++;
    result(st, '正在停止；已发出的这项领取请求将等待游戏返回。', 'running');
    try { updateJob(st, await timedRequest('/api/guild/quests/cancel', { account, id: st.id })); }
    catch (error) { st.uncertain = true; result(st, `停止结果尚未确认：${message(error)}\n请刷新进度核对。`, 'error'); }
    finally { st.cancelling = false; st.checking = false; persist(st); paint(st); schedule(st); }
  }
  async function resolveUnknown() {
    const account = activeAccount;
    if (!account) return;
    const st = getState(account);
    if (st.job.state !== 'unknown' || st.checking || st.recovering) return;
    st.recovering = true; stopPolling(st); paint(st);
    const confirmed = await openConfirmDialog({ title: '解除任务待确认状态', message: '目前查不到这项领取任务，可能是应用已重启。请先在游戏中核对任务奖励。解除后只查询最新任务，不会自动再次领取。', confirmLabel: '已核对，解除状态', returnFocus: $('guildQuestResolve') });
    st.recovering = false;
    if (!confirmed) { paint(st); schedule(st); return; }
    st.id = null; st.uncertain = false; st.checked = false; st.job = { state: 'idle' }; st.sequence++;
    persist(st); result(st, '正在查询最新任务状态…', 'running'); await check(st);
  }
  function changed() {
    for (const st of states.values()) stopPolling(st);
    if (!activeAccount) {
      const empty = getState('');
      empty.text = '请先登录并选择账号。'; empty.job = { state: 'idle' }; empty.queried = false; empty.questIds = [];
      paint({ ...empty, account: activeAccount });
      return;
    }
    const st = getState(activeAccount);
    $('guildQuestRounds').value = st.rounds; paint(st);
    if (isVisible(st)) check(st);
  }
  function init() {
    if (initialized || !$('guildQuestPanel')) return;
    initialized = true;
    $('guildQuestQuery').addEventListener('click', query);
    $('guildQuestStart').addEventListener('click', start);
    $('guildQuestStop').addEventListener('click', cancel);
    $('guildQuestResolve').addEventListener('click', resolveUnknown);
    $('guildQuestRefresh').addEventListener('click', () => { if (activeAccount) return check(getState(activeAccount)); });
    $('guildQuestRounds').addEventListener('input', () => { if (activeAccount) getState(activeAccount).rounds = $('guildQuestRounds').value; });
    window.addEventListener('lucima:account-change', changed);
    window.addEventListener('lucima:section-change', changed);
    document.addEventListener('visibilitychange', changed);
    changed();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
