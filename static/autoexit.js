(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = n => Number(n).toLocaleString('ko-KR', {maximumFractionDigits:2});
  const names = {prepared:'확인 대기',armed:'감시 중',checking:'주문 준비',ordered:'주문 접수',paused:'꺼짐',completed:'청산 완료',
    submitting:'전송 중',accepted:'접수',partial:'일부 체결',filled:'전량 체결',unknown:'응답 불명',
    rejected:'거부',not_sent:'전송 안 됨',closed:'잔량 종료',attention:'미체결 확인 필요'};
  const reasons = {TAKE_PROFIT:'익절 +15%',STOP_LOSS:'손절 −10%',D2_CLOSE:'D+2 종가',D3_CLOSE:'D+3 종가 (이전 규칙)'};
  let token = '', positions = [], plan = null, busy = false, polling = false, lastOk = 0, statusFailed = false, statusRevision = 0;
  function error(message) { $('#error').textContent = message; $('#error').hidden = !message; }
  async function api(url, body) {
    const r = await fetch(url, {method:body === undefined ? 'GET':'POST',cache:'no-store',
      headers:body === undefined ? {} : {'Content-Type':'application/json','X-Autoexit-Token':token},
      body:body === undefined ? undefined : JSON.stringify(body),signal:AbortSignal.timeout(body === undefined ? 5000 : 30000)});
    const data = await r.json();
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '요청을 처리하지 못했습니다');
    return data;
  }
  async function action(fn) {
    if (busy) return;
    busy = true; error(''); $('#feedback').hidden=true;
    const buttons = [...document.querySelectorAll('button:not(.stop)')];
    buttons.forEach(b => b.disabled = true);
    try { await fn(); await refresh(); }
    catch(e) {error(e.message);}
    finally {busy=false;buttons.forEach(b => b.disabled=false);if($('#arm'))$('#arm').disabled=!$('#ack').checked;}
  }
  function planState(p,s) {
    if(p.status !== 'armed')return {label:names[p.status] || p.status, css:p.status};
    if(s.error || !s.connected)return {label:'연결 확인 필요',css:'paused'};
    if(!['2','3'].includes(s.market))return {label:'감시 대기',css:'prepared'};
    if((p.note || '').includes('최신 KRX 체결가 대기'))return {label:'시세 대기',css:'prepared'};
    return {label:'감시 중',css:'armed'};
  }
  function cancelControl(p){
    const active=['prepared','armed','checking'].includes(p.status);
    return `<div class="plan-actions">${active?`<button class="stop" data-pause="${esc(p.code)}">조건 취소 (감시 끄기)</button>`:`<span class="muted">${p.status==='completed'?'완료된 조건':'조건 감시 꺼짐'}</span>`}${p.status==='ordered'?'<small>접수된 주문은 키움에서 별도 확인·취소</small>':''}</div>`;
  }
  function render(s) {
    token=s.token || token;
    const env = s.environment === 'real' ? '실계좌' : '모의계좌';
    $('#environment').textContent=`키움 ${env}`;
    $('#status').textContent=s.enabled ? '자동매도 켜짐' : '자동매도 꺼짐';
    $('#status').className=s.enabled ? 'active' : '';
    $('#connection').textContent=s.connected ? '계좌 연결됨' : '계좌 감시 연결 필요';
    $('#market').textContent=({'3':'KRX 정규장','2':'KRX 마감 동시호가'})[s.market] || '장 상태 대기 / 정규장 아님';
    $('#updated').textContent=new Date(s.asof).toLocaleTimeString('ko-KR');
    if(s.error)error(s.error);
    const armed=s.plans.filter(p=>p.status==='armed').length, watching=s.plans.filter(p=>planState(p,s).label==='감시 중').length;
    $('#planCount').textContent=`${watching}종목 감시${armed>watching ? ` · ${armed-watching}종목 대기/확인 필요` : ''}`;
    $('#plans').innerHTML=s.plans.length ? s.plans.map(p=>{const state=planState(p,s);const removable=!s.error&&['paused','completed'].includes(p.status)&&!s.orders.some(o=>o.code===p.code&&['submitting','accepted','partial','unknown','attention'].includes(o.status));return `<article class="plan"><div class="info"><b>${esc(p.name)}</b> <small>${esc(p.code)}</small> <span class="pill ${esc(state.css)}">${esc(state.label)}</span><p>${num(p.quantity)}주 · 평단 ${num(p.average)}원 · 최초 매수 ${esc(p.bought_on)}</p><p>${esc(p.note)}</p></div><div><small>익절 <span class="up">${num(p.average*1.15)}</span> · 손절 <span class="down">${num(p.average*.9)}</span></small></div><div>${cancelControl(p)}${removable?`<button type="button" data-remove="${esc(p.code)}" data-plan-id="${esc(p.id)}" aria-label="${esc(p.name)} 감시 조건 삭제">조건 삭제</button>`:''}</div></article>`;}).join('') : '<p class="muted">자동매도를 켠 종목이 없습니다.</p>';
    $('#orders').innerHTML=s.orders.length ? [...s.orders].reverse().map(o=>`<tr><td>${esc(o.at.slice(5,19).replace('T',' '))}<small>${esc(o.name)}</small></td><td>${esc(reasons[o.reason] || o.reason)}</td><td>${esc(names[o.status] || o.status)}</td><td>${num(o.filled)} / ${num(o.quantity)}주</td><td>${esc(o.order_no || '미확정')}</td><td>${esc(o.note)}</td></tr>`).join('') : '<tr><td colspan="6" class="empty">전송한 자동매도 주문이 없습니다.</td></tr>';
  }
  function unknown(message) {
    statusFailed=true;error(message || '상태 갱신 지연 · 현재 감시 여부를 확인할 수 없습니다');
    $('#status').textContent='연결 확인 필요';$('#status').className='';
    $('#connection').textContent='계좌 감시 상태 미확인';$('#market').textContent='장 상태 미확인';
    $('#planCount').textContent='감시 상태 확인 필요';
    $('#plans').querySelectorAll('.pill').forEach(b=>{b.textContent='상태 미확인';b.className='pill paused';});
  }
  async function refresh(){if(polling)return;polling=true;const version=statusRevision;try{const s=await api('/api/autoexit');if(version!==statusRevision)return;if(statusFailed)error('');statusFailed=false;render(s);lastOk=Date.now();}catch(e){if(version===statusRevision)unknown(`연결 확인 실패 · 현재 감시 여부를 확인할 수 없습니다. ${e.message}`);}finally{polling=false;}}
  function holdings(s){positions=s.positions||[];const chosen=$('#code').value;$('#code').innerHTML='<option value="">종목 선택</option>'+positions.map(p=>`<option value="${esc(p.code)}">${esc(p.name)} · ${num(p.quantity)}주 · 평단 ${num(p.average)}</option>`).join('');if(positions.some(p=>p.code===chosen))$('#code').value=chosen;}
  function invalidate(){plan=null;$('#review').hidden=true;$('#review').innerHTML='';}
  $('#code').addEventListener('change',()=>{invalidate();$('#boughtOn').value=positions.find(p=>p.code===$('#code').value)?.bought_on || '';});
  $('#boughtOn').addEventListener('change',invalidate);
  $('#loadHoldings').addEventListener('click',()=>action(async()=>{holdings(await api('/api/autosell/start',{}));invalidate();}));
  $('#prepareForm').addEventListener('submit',e=>{e.preventDefault();action(async()=>{
    plan=await api('/api/autoexit/prepare',{code:$('#code').value,bought_on:$('#boughtOn').value});
    const env=plan.environment==='real'?'실계좌':'모의계좌';
    $('#review').innerHTML=`<h3>${esc(plan.name)} · ${num(plan.quantity)}주 전량</h3><div class="review-grid"><span>평균 매수가 <b>${num(plan.average)}원</b></span><span>최초 매수 ${esc(plan.bought_on)}</span><span class="up">익절 기준 ${num(plan.average*1.15)}원</span><span class="down">손절 기준 ${num(plan.average*.9)}원</span></div><p>매수 후 2거래일 뒤까지 남은 수량은 종가 매도합니다.</p><label><input id="ack" type="checkbox"> ${env}에서 조건 충족 시 <b>KRX 시장가 전량 매도</b>를 요청합니다. 급락·갭·유동성에 따라 체결가격이 달라지거나 미체결될 수 있음을 확인했습니다.</label><button id="arm" class="primary" disabled>${env} 자동매도 켜기</button><small>90초 이내 시작하세요. 켜는 즉시 조건에 해당하면 매도가 요청될 수 있습니다.</small>`;
    $('#review').hidden=false;
    $('#review').insertAdjacentHTML('beforeend',`<button type="button" class="stop review-cancel" data-pause="${esc(plan.code)}">이 조건 취소 (감시 끄기)</button>`);
    $('#ack').addEventListener('change',()=>{$('#arm').disabled=!$('#ack').checked||busy;});
    $('#arm').addEventListener('click',()=>action(async()=>{if(!plan||!$('#ack').checked)return;await api('/api/autoexit/arm',{plan_id:plan.id,environment:plan.environment,market_order:true});invalidate();}));
  });});
  async function pause(code){
    const version=++statusRevision;invalidate();error('');$('#feedback').hidden=true;
    try{
      const s=await api('/api/autoexit/pause',{code:code||null});if(version!==statusRevision)return;
      statusRevision++;
      if(!Array.isArray(s.plans)||!Array.isArray(s.orders)||s.error)throw Error(s.error||'중지 상태 응답 확인 필요');
      if(s.plans.some(p=>(!code||p.code===code)&&['prepared','armed','checking'].includes(p.status)))throw Error('감시가 아직 꺼지지 않았습니다');
      statusFailed=false;render(s);lastOk=Date.now();
      const pending=s.orders.some(o=>(!code||o.code===code)&&['submitting','accepted','partial','unknown','attention'].includes(o.status));
      $('#feedback').textContent=(code?`${code} 호라이즌 조건 감시를 껐습니다.`:'호라이즌 조건 감시를 모두 껐습니다.')+(pending?' 접수·미확정 주문은 취소되지 않았습니다. 키움 주문내역에서 확인하세요.':' 꺼진 조건은 기록으로 남습니다. 다시 사용하려면 조건을 확인하고 켜세요.');
      $('#feedback').hidden=false;
    }catch(e){if(version===statusRevision||version+1===statusRevision)unknown('취소 응답 미확인 · 감시 중지를 확인할 수 없습니다. '+e.message);}
  }
  $('#stopAll').addEventListener('click',()=>pause());
  $('#plans').addEventListener('click',e=>{
    const remove=e.target.closest('[data-remove]');
    if(remove){action(async()=>{
      const version=++statusRevision;
      const s=await api('/api/autoexit/remove',{code:remove.dataset.remove,plan_id:remove.dataset.planId});
      if(version!==statusRevision)return;
      statusRevision++;
      if(s.deleted_plan_id!==remove.dataset.planId||s.plans.some(p=>p.id===remove.dataset.planId))throw Error('삭제 완료를 확인하지 못했습니다');
      render(s);lastOk=Date.now();statusFailed=false;
      $('#feedback').textContent=`${s.deleted_code} 감시 조건을 저장 목록에서 삭제했습니다. 실제 잔고와 주문 기록은 유지됩니다.`;
      $('#feedback').hidden=false;
    });return;}
    const b=e.target.closest('[data-pause]');if(b)pause(b.dataset.pause);
  });
  $('#review').addEventListener('click',e=>{const b=e.target.closest('[data-pause]');if(b)pause(b.dataset.pause);});
  $('#reconcile').addEventListener('click',()=>action(()=>api('/api/autoexit/reconcile',{})));
  refresh();api('/api/autosell').then(holdings).catch(e=>error(e.message));setInterval(refresh,2000);
  setInterval(()=>{if(lastOk && Date.now()-lastOk>10000 && !statusFailed)unknown();},1000);
  window.addEventListener('offline',()=>unknown('인터넷 연결 중단 · 현재 감시 여부를 확인할 수 없습니다'));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){if(lastOk && Date.now()-lastOk>10000)unknown();refresh();}});
})();
