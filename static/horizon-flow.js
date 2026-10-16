/* Read-only stock detail panel. Never calls account, plan, or order endpoints. */
(() => {
  'use strict';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = (v, d=0) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('ko-KR', {maximumFractionDigits:d, minimumFractionDigits:d});
  const signed = (v, d=0) => (v > 0 ? '+' : '') + num(v,d);
  const tone = v => v > 0 ? 'hf-positive' : v < 0 ? 'hf-negative' : 'hf-neutral';
  const money = v => v == null ? '—' : signed(v / 1e8, 2) + '억';
  const date = d => /^\d{8}$/.test(d || '') ? d.slice(4,6)+'/'+d.slice(6,8) : '—';
  const stamp = s => s ? new Date(s).toLocaleString('ko-KR', {timeZone:'Asia/Seoul', month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '—';
  const hhmmss = s => /^\d{6}$/.test(s||'') ? s.slice(0,2)+':'+s.slice(2,4)+':'+s.slice(4) : '—';
  const root = document.createElement('section');
  root.className = 'horizon-flow'; root.id = 'horizonFlow'; root.setAttribute('aria-label','선택 종목 자리와 수급');
  let selected=null, key='', value=null, error='', request=null, lastFetch=0;
  const positive = v => `<b class="${tone(v)}">${signed(v)}</b>`;
  const warning = s => s ? `<p class="hf-warning">${esc(s)}</p>` : '';
  const empty = s => `<p class="hf-empty">${esc(s)}</p>`;
  function gap(p) {const price=selected?.price || value?.capture?.last_price;return price > 0 ? `조회가 대비 ${signed((p/price-1)*100,1)}%` : '조회가 미확인'}
  function paint() {
    if(!selected)return;
    const c=value?.capture||{}, b=value?.book||{}, pg=value?.program||{}, br=value?.brokers||{};
    const brokers=br.data, program=pg.data;
    const level=(r,label)=>`<li><span class="hf-dot ${r.net>0?'hf-bid':'hf-ask'}"></span>${label} <b>${num(r.price)}</b> <small>매수 ${num(r.buy)} / 매도 ${num(r.sell)}</small><span class="hf-break">순 ${positive(r.net)}주 <small>(${gap(r.price)})</small></span></li>`;
    const broker=(r,buy)=>`<li><span class="hf-dot ${buy?'hf-bid':'hf-ask'}"></span>${buy?'사 모은':'내다 판'} 창구 <b>${esc(r.name)}</b> ${positive(r.net)}주<small class="hf-break">매수 ${num(r.buy)} / 매도 ${num(r.sell)}</small></li>`;
    const stale=c.last_at && (Date.now()-new Date(c.last_at).getTime()>30000);
    root.innerHTML=`<header class="hf-header"><div><h3>자리 · 수급</h3><span>${esc(selected.name)} · ${esc(selected.code)}</span></div><span class="hf-badge">KRX · 참고 지표</span></header>
      <div class="hf-state" role="status">${error?esc(error):request?'수급 조회 중…':value?`조회 ${stamp(value.at)} · ${value.connected?'체결 연결':'체결 연결 대기'}`:'자료 수신 대기'}</div>
      <section class="hf-section"><h4>🧲 자리 <span>어디서 받고 어디서 털었나</span></h4>
      ${c.ticks?`<p>${date(c.date)} 수신 체결 <b>${num(c.ticks)}건</b> · 최다 거래 가격 <b>${num(c.poc)}</b></p>
        <ul>${(c.received||[]).map(r=>level(r,'받은 자리')).join('')}${(c.sold||[]).map(r=>level(r,'털린 자리')).join('')}</ul>
        <p class="hf-note">${stamp(c.first_at)}부터 ${stamp(c.last_at)}까지${c.partial?' · 부분 수집':''}${stale?' · 마지막 수신 자료':''}</p>`:empty('체결 수집 대기 · 수집된 체결로 가격대를 계산합니다.')}
      <div class="hf-divider"></div><h5>매도벽 · 연속 관측</h5>
      ${b.data?.rows?.length?`<ul>${b.data.rows.map(r=>`<li>🧱 <b>${num(r.price)}원</b> · 평균 <b>${num(r.average)}</b>주 <span class="hf-neutral">${num(r.samples)}회 관측</span><small class="hf-break">현재 잔량 ${num(r.quantity)}주 · ${gap(r.price)}</small></li>`).join('')}</ul><p class="hf-note">호가 수집 ${stamp(b.data.at)}</p>`:empty('정규장 최신 호가 수집 대기')}
      ${warning(b.error)}<p class="hf-note">15~20초 간격의 잔량 관측입니다. 같은 주문의 유지·체결 여부는 알 수 없습니다.</p></section>
      <section class="hf-section"><h4>💰 기준봉(${date(selected.d0)}) 이후 <span>누적 CVD</span></h4>
      ${c.daily?.length?`<p>기준봉 <b class="${tone(c.base_won)}">${money(c.base_won)}</b> → 수집분 누적 <b class="${tone(c.total_won)}">${money(c.total_won)}</b>${c.ratio_pct!=null?` <small>기준봉의 ${num(c.ratio_pct)}%</small>`:''}</p>
      <p class="hf-daily">${c.daily.map(r=>`<span title="${esc(stamp(r.first_at)+' ~ '+stamp(r.last_at))}">${date(r.date)} <b class="${tone(r.net_won)}">${money(r.net_won)}</b>${r.partial?'*':''}</span>`).join(' · ')}</p>`:empty('기준봉 이후 체결 수집 자료가 없습니다.')}
      ${warning(c.storage_error?'수집 자료 저장 실패 · 재시작 후 자료가 보존되지 않을 수 있습니다.':'')}
      <p class="hf-note">매수 체결대금 − 매도 체결대금. ${c.complete?'시작부터 연속 수신한 범위입니다.':'미수집 구간을 제외한 부분 합계입니다. * 부분 수집일'}<br>일봉으로 과거 CVD를 복원하지 않으며, 실제 남아 있는 자금을 뜻하지 않습니다.</p></section>
      <section class="hf-section"><h4>🤖 프로그램 매매 <span>${program?date(program.date)+' · '+hhmmss(program.time):'최근 거래일'}</span></h4>
      ${program?`<p>순매수 <strong class="${tone(program.net_won)}">${money(program.net_won)}</strong></p><p class="hf-note">매수 ${num(program.buy)}주 · 매도 ${num(program.sell)}주 · 순 ${signed(program.net)}주</p>`:empty('KRX 프로그램 자료 미확인')}
      ${warning(pg.error)}<p class="hf-note">키움 프로그램 집계 · 체결 방향 CVD와 별도 지표</p></section>
      <section class="hf-section"><h4>🏛 창구 <span>누가 사고 누가 파나${brokers?' · '+brokers.days.length+'거래일':''}</span></h4>
      ${brokers?`<p class="hf-note">${brokers.days.map(date).join(' ~ ')} · 조회된 상위 창구 · 갱신 ${stamp(br.fetched_at)}</p><ul>${brokers.bought.map(r=>broker(r,true)).join('')}${brokers.sold.map(r=>broker(r,false)).join('')}</ul>${!brokers.bought.length&&!brokers.sold.length?empty('조회된 순매수·순매도 창구가 없습니다.'):''}
      ${brokers.flips.map(r=>`<p class="hf-flip">↘ 돌아섰다 · <b>${esc(r.name)}</b><br>앞 거래일 ${positive(r.before)}주 → 최근 거래일 ${positive(r.now)}주</p>`).join('')}`:empty('최근 2거래일 창구 자료 미확인')}
      ${warning(br.error)}<p class="hf-note">상위 창구 조회 범위에서 비교합니다. 창구는 투자자의 신원을 뜻하지 않으며, 매도·매수를 맡긴 증권사입니다.</p></section>`;
  }
  async function refresh() {
    if(!selected || request || document.hidden)return;
    const ownKey=key, item={...selected}, controller=new AbortController();
    request=controller; paint();
    const timeout=setTimeout(()=>controller.abort(),42000);
    try {
      const response=await fetch(`/api/horizon-flow/${encodeURIComponent(item.code)}?since=${encodeURIComponent(item.d0)}`, {cache:'no-store',signal:controller.signal});
      if(!response.ok)throw Error('수급 조회 지연 · 다시 확인 중');
      const result=await response.json();
      if(key!==ownKey || request!==controller)return;
      if(result.code!==item.code || result.since!==item.d0)throw Error('선택 종목 확인 중');
      value=result; error='';
    } catch(e) {
      if(key===ownKey && request===controller)error='수급 조회 지연 · 이전 자료 표시';
    } finally {
      clearTimeout(timeout);
      if(key===ownKey && request===controller){request=null;lastFetch=Date.now();paint()}
    }
  }
  window.HorizonFlow={mount(slot,row){
    const next=row?row.code+':'+row.d0:'';
    if(next!==key){request?.abort();request=null;key=next;value=null;error='';lastFetch=0}
    selected=row?{code:row.code,d0:row.d0,name:row.name,price:row.price}:null;
    if(!selected){root.remove();return}
    if(slot && root.parentNode!==slot)slot.appendChild(root);
    paint();if(!lastFetch)refresh();
  }};
  setInterval(()=>{if(Date.now()-lastFetch>=20000)refresh()},2000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden && Date.now()-lastFetch>=20000)refresh()});
})();
