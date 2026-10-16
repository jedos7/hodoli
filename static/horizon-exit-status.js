/* Read-only order-engine status for the Horizon plan card. Never starts or stops trading. */
(() => {
  'use strict';
  const panel = document.getElementById('planMonitor');
  if (!panel) return;
  const heading = document.getElementById('planMonitorHeading');
  const rows = document.getElementById('planMonitorRows');
  const updated = document.getElementById('planMonitorUpdated');
  const footer = document.getElementById('autoexitState');
  let busy = false, lastOk = 0, previous = [], outdated = false, signature = '';
  const shortTime = value => new Date(value).toLocaleTimeString('ko-KR', {timeZone:'Asia/Seoul',hour12:false});

  function stateFor(plan, state) {
    const name = plan.name || plan.code;
    if (state.error || (['armed','checking'].includes(plan.status) && (!state.connected || !state.enabled)))
      return {name, tone:'warning', label:'상태 확인 필요', detail:state.error || '계좌 감시 연결을 확인하세요'};
    if (plan.status === 'armed') {
      if (plan.watch_state === 'watching')
        return {name, tone:'watching', label:'시간외 감시 중', detail:plan.note};
      if (plan.watch_state === 'quote_wait')
        return {name, tone:'waiting', label:'시간외 시세·거래일 대기', detail:plan.note};
      if (!['2','3'].includes(state.market))
        return {name, tone:'waiting', label:'감시 대기', detail:'자동매도 켜짐 · 장 상태 수신 대기'};
      if ((plan.note || '').includes('최신 KRX 체결가 대기'))
        return {name, tone:'waiting', label:'시세 대기', detail:'자동매도 켜짐 · 최신 체결가 확인 중'};
      return {name, tone:'watching', label:'감시 중', detail:plan.note || '익절·손절·D+2 종가 감시'};
    }
    const labels = {prepared:'시작 전', paused:'감시 중지', checking:'매도 준비 중',
      ordered:'주문 체결 확인 중', completed:'청산 완료'};
    return {name, tone:['checking','ordered'].includes(plan.status)?'waiting':'stopped',
      label:labels[plan.status] || '상태 확인 필요', detail:plan.note || '자동매도 설정에서 확인하세요'};
  }

  function draw(items) {
    const next = JSON.stringify(items);
    if (signature === next) return;
    signature = next;
    rows.replaceChildren();
    for (const item of items) {
      const row = document.createElement('div'); row.className = `plan-monitor-row ${item.tone}`;
      const line = document.createElement('div'); line.className = 'plan-monitor-line';
      const name = document.createElement('b'); name.textContent = item.name;
      const badge = document.createElement('span'); badge.className = 'plan-monitor-badge'; badge.textContent = item.label;
      const detail = document.createElement('small'); detail.textContent = item.detail;
      line.append(name, badge); row.append(line, detail); rows.append(row);
    }
  }

  function unknown() {
    outdated = true;
    panel.dataset.state = 'warning';
    heading.textContent = '자동매도 상태 확인 필요';
    footer.textContent = '상태 확인 필요';
    draw(previous.length ? previous.map(p => ({name:p.name || p.code, tone:'warning', label:'연결 확인 필요',
      detail:'현재 감시 여부를 확인할 수 없습니다'})) : [{name:'연결 확인 필요',tone:'warning',label:'',detail:'자동매도 상태를 다시 확인하고 있습니다'}]);
    updated.textContent = lastOk ? `마지막 확인 ${shortTime(lastOk)} · 갱신 지연` : '상태 조회 연결 대기';
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/autoexit', {cache:'no-store', signal:AbortSignal.timeout(5000)});
      if (!response.ok) throw Error('상태 조회 실패');
      const state = await response.json();
      if (!Array.isArray(state.plans) || typeof state.enabled !== 'boolean') throw Error('상태 응답 확인 필요');
      previous = state.plans;
      lastOk = Date.now(); outdated = false;
      const unready = state.error || (state.enabled && !state.connected);
      heading.textContent = unready ? '자동매도 상태 확인 필요' : state.enabled ? '자동매도 켜짐' : '자동매도 꺼짐';
      panel.dataset.state = unready ? 'warning' : state.enabled ? 'enabled' : 'stopped';
      footer.textContent = unready ? '확인 필요' : state.enabled ? '켜짐' : '꺼짐';
      draw(previous.length ? previous.map(p => stateFor(p, state)) : [{name:'등록된 종목 없음',tone:'stopped',label:'',
        detail:'자동매도 설정에서 종목을 선택하세요'}]);
      updated.textContent = `${shortTime(lastOk)} 확인 · 3초마다 갱신`;
    } catch {
      unknown();
    } finally { busy = false; }
  }
  refresh();
  setInterval(refresh,3000);
  setInterval(() => {if (lastOk && Date.now()-lastOk>10000 && !outdated) unknown();},1000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){if(lastOk && Date.now()-lastOk>10000)unknown();refresh();}});
  window.addEventListener('online',refresh);
  window.addEventListener('offline',unknown);
})();
