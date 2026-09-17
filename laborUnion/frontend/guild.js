/* Guild management reuses api() and the selected account's existing login. */
(() => {
  const $ = id => document.getElementById(id);
  const states = new Map();
  const defaults = { overview: '点击查询，查看当前账号的工会信息。', captain: '选择一项工会商品，购买后查看结果。' };
  let initialized = false;
  function state(account) {
    if (!states.has(account)) states.set(account, { account, pending: {}, results: Object.fromEntries(Object.entries(defaults).map(([key,text]) => [key, {text, tone:'idle', raw:null}])) });
    return states.get(account);
  }
  function errorText(error) {
    const text = error?.message || String(error || '未知错误');
    if (/OverCount/i.test(text)) return '已达到游戏当前允许的次数（OverCount）。';
    if (/CantCheckIn/i.test(text)) return '游戏未允许本次签到（CantCheckIn），请查询工会状态。';
    if (/E0001/i.test(text)) return `游戏接口返回异常，操作未确认成功。\n${text}`;
    return text;
  }
  const safeDetails = value => JSON.stringify(value, (key,val) => /^(aid|sessionid|token|password|authorization|accesstoken|refreshtoken)$/.test(key.replace(/[_-]/g,'').toLowerCase()) ? '[已隐藏]' : val, 2);
  function paint(st) {
    if (st.account !== activeAccount) return;
    for (const [key, value] of Object.entries(st.results)) {
      const prefix = key === 'overview' ? 'guildOverview' : 'guildCaptain';
      $(prefix).textContent = value.text; $(prefix).dataset.tone = value.tone;
      $(`${prefix}Details`).classList.toggle('hidden', value.raw == null);
      $(`${prefix}Raw`).textContent = value.raw == null ? '' : safeDetails(value.raw);
    }
    ['guildRefresh','guildCheckin','guildAttendance'].forEach(id => { $(id).disabled = !st.account || Boolean(st.pending.overview); });
    document.querySelectorAll('.guild-buy').forEach(button => { button.disabled = !st.account || Boolean(st.pending.captain); });
  }
  function result(st,key,text,tone='idle',raw=null) { st.results[key] = {text,tone,raw}; paint(st); }
  function responseError(value) {
    if (!value || typeof value !== 'object') return null;
    if (value.error) return String(value.error);
    return value.ok === false ? String(value.message || value.detail || '操作未完成') : null;
  }
  function overview(value) {
    const errors = Object.entries(value || {}).filter(([,part]) => responseError(part)).map(([key,part]) => `${key}：${errorText(responseError(part))}`);
    const data = value?.full?.GuildData || value?.full?.data?.GuildData || value?.guild?.GuildData || value?.GuildData;
    const name = data?.Info?.Name || data?.GuildName || data?.Name;
    const level = data?.Info?.GuildLV ?? data?.GuildLV ?? data?.Level;
    const members = data?.Info?.MemberCount ?? data?.MemberCount ?? (Array.isArray(data?.MemberList) ? data.MemberList.length : null);
    const lines = name ? [`工会：${name}`,level != null ? `等级：${level}` : '',members != null ? `成员：${members}` : ''].filter(Boolean) : ['工会查询已完成，可展开查看返回详情。'];
    return {text:lines.concat(errors).join('\n'),tone:errors.length?'error':'ok'};
  }
  async function run(key,label,action,extra={}) {
    const account = activeAccount;
    if (!account) return;
    const st = state(account);
    if (st.pending[key]) return;
    st.pending[key] = true; result(st,key,`${label}中，请等待游戏服务器返回…`,'running');
    try {
      const value = action === 'overview' ? await api('/api/guild/overview',{account}) : await api('/api/guild/action',{account,action,...extra});
      const error = responseError(value);
      if (error) result(st,key,errorText(error),'error',value);
      else if (action === 'overview') { const view=overview(value); result(st,key,view.text,view.tone,value); }
      else result(st,key,`${label}请求已成功返回。`,'ok',value);
    } catch(error) { result(st,key,`${label}未完成：${errorText(error)}`,'error'); }
    finally { st.pending[key]=false; paint(st); }
  }
  function changed() {
    $('guildAccount').textContent = activeAccount || '未选择账号';
    paint(state(activeAccount));
  }
  function init() {
    if(initialized || !$('section-guild')) return;
    initialized=true; SECTION_TITLES.guild='工会';
    $('guildRefresh').addEventListener('click',()=>run('overview','查询工会','overview'));
    $('guildCheckin').addEventListener('click',()=>run('overview','工会签到','checkin'));
    $('guildAttendance').addEventListener('click',()=>run('overview','领取日奖励','attendance'));
    document.querySelectorAll('.guild-buy').forEach(button=>button.addEventListener('click',()=>run('captain',button.querySelector('strong').textContent,'buy',{itemId:button.dataset.item,count:1})));
    window.addEventListener('lucima:account-change',changed);
    changed();
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',init,{once:true});
  else init();
})();
