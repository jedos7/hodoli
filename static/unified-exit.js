/* Horizon controls inside the custom-exit workspace. Existing order APIs remain separate. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=v=>Number(v).toLocaleString('ko-KR',{maximumFractionDigits:2});
  const pending=['submitting','accepted','partial','unknown','attention'];
  const active=['prepared','armed','checking','ordered','remainder_check'];
  const names={prepared:'확인 대기',armed:'감시 중',checking:'주문 준비',ordered:'체결 확인 중',paused:'꺼짐',completed:'청산 완료',submitting:'전송 중',accepted:'접수',partial:'일부 체결',filled:'전량 체결',unknown:'응답 불명',rejected:'거부',not_sent:'전송 안 됨',closed:'잔량 종료',attention:'미체결 확인 필요'};
  const reasons={TAKE_PROFIT:'익절 +15%',TRAILING_STOP:'잔량 고점 대비 −3%',STOP_LOSS:'손절 −10%',D4_CLOSE:'D+4 종가 (이전 기록)',BREAKEVEN:'청산일 본전 지정가',D2_CLOSE:'매수 후 D+2 종가',D3_CLOSE:'D+3 종가 (이전 기록)'};
  let mode='custom',token='',state=null,customState=null,review=null,busy=false,polling=false,lastOk=0,stale=true,revision=0,statusRevision=0,lastCode='',dates={},chartPlan=null,planMarkup='';
  const position=()=>window.CustomExitUI?.selected();
  function error(msg){$('hzError').textContent=msg;$('hzError').hidden=!msg;}
  function feedback(msg){$('hzFeedback').textContent=msg;$('hzFeedback').hidden=!msg;}
  function invalidate(){revision++;review=null;if($('hzReviewDialog').open)$('hzReviewDialog').close();$('hzReview').replaceChildren();}
  function conflict(){
    const peer=mode==='horizon'?customState:state,code=$('code').value;
    const clash=code&&peer&&((peer.plans||[]).some(p=>p.code===code&&active.includes(p.status))||(peer.orders||[]).some(o=>o.code===code&&(pending.includes(o.status)||o.chase_active)));
    const unknown=code&&(!peer||peer.error||(mode==='custom'&&stale));
    $('modeConflict').hidden=!clash&&!unknown;
    $('modeConflict').textContent=clash?`${mode==='horizon'?'직접 설정':'호라이즌'}에 이 종목의 감시·확인 대기 조건 또는 미확정 주문이 있습니다. 아래 목록에서 먼저 감시를 끄고 주문내역을 확인하세요.`:unknown?'다른 방식의 상태를 확인 중입니다. 시작 전 서버에서 중복 감시·주문을 다시 확인합니다.':'';
  }
  function chart(){
    const p=chartPlan||position();
    window.ExitChart?.setHorizon(p?{...p,profit:Number(p.average)*1.15,loss:Number(p.average)*.9,source:chartPlan?'plan':'draft',method:'horizon',status:chartPlan?(stale?'connection_wait':stateLabel(p).key):undefined}:null);
  }
  function selection(){
    const code=$('code').value;
    if(code!==lastCode){lastCode=code;chartPlan=null;invalidate();$('hzBoughtOn').value=state?.plans.find(p=>p.code===code)?.bought_on||dates[code]||'';}
    chart();conflict();
  }
  function setMode(next,updateHash=true){
    if(!['custom','horizon'].includes(next)||busy||window.CustomExitUI?.isBusy())return false;
    if(next!==mode){invalidate();window.CustomExitUI?.clearReview();}
    mode=next;
    $('customModePanel').hidden=mode!=='custom';$('horizonModePanel').hidden=mode!=='horizon';$('customGuide').hidden=mode!=='custom';
    document.querySelectorAll('[data-exit-mode]').forEach(b=>{b.setAttribute('aria-pressed',String(b.dataset.exitMode===mode));});
    if(updateHash)history.replaceState(null,'','#'+mode);
    window.ExitChart?.setMode(mode);if(mode==='custom')window.CustomExitUI?.refreshChart();else selection();
    conflict();return true;
  }
  window.ExitWorkspace={setMode,positionsChanged:selection,customStatus(s){customState=s;conflict();}};
  async function api(path='',body){
    const r=await fetch('/api/autoexit'+path,{method:body===undefined?'GET':'POST',cache:'no-store',headers:body===undefined?{}:{'Content-Type':'application/json','X-Autoexit-Token':token},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(body===undefined?5000:30000)});
    const data=await r.json();if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'호라이즌 요청을 확인하세요');return data;
  }
  function stateLabel(p){
    if(stale)return {label:'상태 미확인',key:'connection_wait'};
    if(p.status!=='armed')return {label:names[p.status]||p.status,key:p.status};
    if(state?.error||!state?.connected)return {label:'연결 확인 필요',key:'connection_wait'};
    if(p.watch_state==='watching')return {label:'시간외 감시 중',key:'watching'};
    if(p.watch_state==='quote_wait')return {label:'시간외 시세·거래일 대기',key:'quote_wait'};
    if(!['2','3'].includes(state.market))return {label:'장 상태 대기',key:'session_wait'};
    if((p.note||'').includes('최신 KRX 체결가 대기'))return {label:'시세 대기',key:'quote_wait'};
    return {label:'감시 중',key:'watching'};
  }
  function render(s){
    window.ExitOrderTools?.update('horizon',s);
    state=s;token=s.token||token;
    $('hzEnvironment').textContent=s.environment==='real'?'키움 실계좌':'키움 모의계좌';
    $('hzStatus').textContent=stale?'호라이즌 상태 미확인':s.error?'호라이즌 확인 필요':s.enabled?'호라이즌 자동매도 켜짐':'호라이즌 자동매도 꺼짐';
    $('hzDot').className=!stale&&s.enabled&&s.connected&&!s.error?'active':'';
    $('hzConnection').textContent=stale?'계좌 감시 상태 미확인':!s.connected?'계좌 감시 연결 필요':({'3':'계좌 연결 · KRX 정규장','2':'계좌 연결 · 마감 동시호가'})[s.market]||'계좌 연결 · 정규장 외';
    $('hzUpdated').textContent=new Date(s.asof).toLocaleTimeString('ko-KR',{timeZone:'Asia/Seoul',hour12:false})+' 확인';
    if(s.error)error(s.error);
    const watching=s.plans.filter(p=>stateLabel(p).key==='watching').length;
    $('hzPlanCount').textContent=stale?'상태 미확인':`${s.plans.length}종목 · ${watching}종목 감시 중`;
    const markup=s.plans.map(p=>{
      const label=stateLabel(p),canRemove=!stale&&!s.error&&['paused','completed'].includes(p.status)&&!s.orders.some(o=>o.code===p.code&&(pending.includes(o.status)||o.chase_active));
      return `<article class="hz-plan" data-hz-code="${esc(p.code)}" data-hz-plan="${esc(p.id)}"><div class="hz-plan-heading"><b>${esc(p.name)}</b><span class="mode-tag">호라이즌</span><span class="hz-state ${esc(label.key)}">${esc(label.label)}</span></div><p>${num(p.quantity)}주 · 평단 ${num(p.average)}원 · 최초 매수 ${esc(p.bought_on||'미확인')}</p><p><span class="up">익절 ${num(p.average*1.15)}</span> · <span class="down">손절 ${num(p.average*.9)}</span> · D+${p.hold_sessions||2} 종가 · 익절 ${p.strategy_version>=2?'70% 지정가':'전량 (이전 계획)'}</p><p>${esc(p.note)}</p>${p.trailing_started_at?`<p>고점 추적 중 · 고점 ${num(p.trailing_high)}원 · 매도 기준 ${num(p.trailing_high*.97)}원 이하</p>`:p.strategy_version>=4?'<p>70% 익절 체결 후 잔량 고점 대비 −3% 추적</p>':''}<p>${p.after_hours&&p.after_hours!=='off'?`시간외 ${esc(p.after_hours==='both'?'08:00~20:00 시장 자동 선택':p.after_hours)} · 매수호가 기준${p.empty_book==='floor'?' · 호가 없을 때 하한가':''}${p.retry_unfilled?' · 손절 재주문 켜짐':''}`:'KRX 정규장만'}</p><div class="hz-plan-actions"><button type="button" data-hz-action="chart">차트</button>${(['prepared','armed','checking'].includes(p.status)||s.orders.some(o=>o.plan_id===p.id&&o.chase_active))?'<button type="button" class="stop" data-hz-action="pause">호라이즌 감시 끄기</button>':''}${canRemove?'<button type="button" data-hz-action="remove">조건 삭제</button>':''}</div></article>`;
    }).join('')||'<p class="empty">등록된 호라이즌 조건이 없습니다.</p>';
    if(markup!==planMarkup){$('hzPlans').innerHTML=markup;planMarkup=markup;}
    $('hzOrders').innerHTML=s.orders.length?[...s.orders].reverse().map(o=>`<tr><td>${esc(o.at?.slice(5,19).replace('T',' '))}<small>${esc(o.name)}</small></td><td>호라이즌 · ${esc(reasons[o.reason]||o.reason)}${o.after_hours?`<small>${esc(o.venue)} 지정가 ${num(o.limit_price)}원 · ${o.chase_active?'재주문 감시 중':'재주문 꺼짐'}</small>`:''}</td><td>${num(o.filled)} / ${num(o.quantity)}주<small>미체결 ${o.remaining==null?'확인 중':num(o.remaining)+'주'}</small></td><td>${esc(names[o.status]||o.status)}</td><td>${esc(o.order_no||'미확정')}<small>${esc(o.note)}${o.manual_cancel_state?`<small class="cancel-result">${esc(o.manual_cancel_note)}${o.manual_cancel_no?' · 취소번호 '+esc(o.manual_cancel_no):''}</small>`:''}</small></td></tr>`).join(''):'<tr><td colspan="5" class="empty">전송한 호라이즌 주문이 없습니다.</td></tr>';
    if(chartPlan){chartPlan=s.plans.find(p=>p.id===chartPlan.id)||null;chart();}
    conflict();
  }
  function unknown(msg){stale=true;error(msg||'호라이즌 상태 갱신 지연 · 감시 여부를 확인할 수 없습니다');if(state)render(state);else{$('hzStatus').textContent='호라이즌 상태 미확인';$('hzPlanCount').textContent='상태 미확인';}conflict();}
  async function refresh(){if(polling)return;polling=true;const v=statusRevision;try{const s=await api();if(v!==statusRevision)return;if(!Array.isArray(s.plans)||!Array.isArray(s.orders))throw Error('상태 응답 확인 필요');if(stale)error('');stale=false;lastOk=Date.now();render(s);}catch(e){if(v===statusRevision)unknown(e.message);}finally{polling=false;}}
  async function action(fn){
    if(busy||window.CustomExitUI?.isBusy())return;busy=true;error('');feedback('');
    const controls=[...document.querySelectorAll('#horizonModePanel button:not(.stop),#hzBoughtOn,#hzAfterHours,#hzRetryUnfilled,#hzFloorFallback,#code,#loadHoldings,[data-exit-mode],#hzReconcile')];
    const disabled=controls.map(e=>e.disabled);controls.forEach(e=>e.disabled=true);
    try{await fn();}catch(e){error(e.message);}finally{busy=false;controls.forEach((e,i)=>e.disabled=disabled[i]);if($('hzArm'))$('hzArm').disabled=!review||!$('hzAck').checked;await refresh();}
  }
  async function prepare(){
    if(mode!=='horizon')return;
    const p=position(),code=$('code').value,bought_on=$('hzBoughtOn').value;
    if(!p||p.code!==code)throw Error('잔고 읽기 후 종목을 선택하세요.');
    if(!bought_on)throw Error('실제 최초 매수일을 입력하세요.');
    if(stale||Date.now()-lastOk>10000)throw Error('호라이즌 상태 연결을 확인한 뒤 다시 시도하세요.');
    invalidate();const v=revision;++statusRevision;
    const after_hours=$('hzAfterHours').value, retry_unfilled=after_hours!=='off'&&$('hzRetryUnfilled').checked, empty_book=after_hours!=='off'&&$('hzFloorFallback').checked?'floor':'wait';
    const value=await api('/prepare',{code,bought_on,after_hours,retry_unfilled,empty_book});++statusRevision;
    if(v!==revision||mode!=='horizon'||$('code').value!==code||$('hzBoughtOn').value!==bought_on)throw Error('입력이 바뀌었습니다. 다시 확인하세요.');
    review={...value,reviewedAt:Date.now()};const env=review.environment==='real'?'실계좌':'모의계좌';
    const extended=value.after_hours&&value.after_hours!=='off'?`<p class="review-summary">시간외 ${esc(value.after_hours==='both'?'08:00~20:00 시장 자동 선택':value.after_hours)} · 매수호가 기준 지정가<br>호가 없음: ${value.empty_book==='floor'?'당일 하한가 허용':'대기'}<br>손절 미체결 재주문: ${value.retry_unfilled?'12초마다 · 최대 5회':'사용 안 함'}</p>`:'';
    $('hzReview').innerHTML=`<p class="eyebrow">호라이즌 · ${env}</p><h2 id="hzReviewTitle">${esc(value.name)} · ${num(value.quantity)}주 보유</h2><p>평단 ${num(value.average)}원 · 최초 매수 ${esc(value.bought_on)}</p><p class="review-summary">익절 ${num(value.profit_price)}원 (+15% 호가 올림) · ${num(value.profit_quantity)}주 지정가<br>손절 ${num(value.average*.9)}원 (−10%)<br>매수 후 2거래일 뒤 종가 잔량 청산 · KRX 시장가<br>청산일 08:00·09:00 첫 가격이 ${num(value.breakeven_trigger)}원 아래: ${num(value.breakeven_price)}원 잔량 지정가<br>15:18 미체결 취소 → 취소 완료 후 15:20 잔량 매도</p>${extended}<p class="help">켜는 즉시 조건에 해당하면 주문될 수 있습니다. 시장가 체결가격은 조건 가격과 다를 수 있습니다.</p><label class="review-ack"><input id="hzAck" type="checkbox"> ${env}에서 위 조건 충족 시 익절 70% 지정가·잔량 고점 대비 −3%·손절 잔량·D+2 본전 및 종가 청산 주문을 요청함을 확인했습니다.</label><button id="hzArm" type="button" class="primary" disabled>${env} · 호라이즌 감시 켜기</button><button id="hzCloseReview" type="button">닫기 · 켜지 않기</button><small id="hzReviewTimer">90초 안에 확인하세요.</small>`;
    $('hzAck').addEventListener('change',()=>{$('hzArm').disabled=busy||!review||!$('hzAck').checked;});
    $('hzCloseReview').addEventListener('click',invalidate);
    $('hzArm').addEventListener('click',()=>action(async()=>{
      const r=review;if(!r||!$('hzAck').checked||mode!=='horizon')return;
      if($('code').value!==r.code||$('hzBoughtOn').value!==r.bought_on||Date.now()-r.reviewedAt>=90000){invalidate();throw Error('입력 또는 확인 시간이 바뀌었습니다. 다시 확인하세요.');}
      ++statusRevision;
      try{await api('/arm',{plan_id:r.id,environment:r.environment,market_order:true});feedback('호라이즌 감시를 켰습니다. 아래 목록의 실제 상태를 확인하세요.');}
      catch(e){throw Error('시작 결과를 아래 목록에서 확인하세요. '+e.message);}
      finally{++statusRevision;invalidate();}
    }));
    $('hzReviewDialog').showModal();
  }
  async function pause(code=null){
    invalidate();const v=++statusRevision;error('');feedback('');
    try{const s=await api('/pause',{code});if(v!==statusRevision)return;++statusRevision;
      if(!Array.isArray(s.plans)||!Array.isArray(s.orders)||s.error||s.plans.some(p=>(!code||p.code===code)&&['prepared','armed','checking'].includes(p.status))||s.orders.some(o=>(!code||o.code===code)&&o.chase_active))throw Error('감시 중지를 확인할 수 없습니다');
      stale=false;lastOk=Date.now();render(s);feedback('호라이즌 감시를 껐습니다. 이미 접수된 주문은 취소되지 않습니다.');
    }catch(e){unknown('중지 응답 미확인 · '+e.message);}await refresh();
  }
  document.querySelectorAll('[data-exit-mode]').forEach(b=>b.addEventListener('click',()=>setMode(b.dataset.exitMode)));
  $('code').addEventListener('change',selection);
  $('hzBoughtOn').addEventListener('input',invalidate);
  $('hzAfterHours').addEventListener('change',()=>{invalidate();const off=$('hzAfterHours').value==='off';for(const id of ['hzRetryUnfilled','hzFloorFallback']){$(id).disabled=off;if(off)$(id).checked=false;}});
  for(const id of ['hzRetryUnfilled','hzFloorFallback'])$(id).addEventListener('change',invalidate);
  $('hzPrepareForm').addEventListener('submit',e=>{e.preventDefault();action(prepare);});
  $('hzStopAll').addEventListener('click',()=>pause());
  $('hzReconcile').addEventListener('click',()=>action(()=>api('/reconcile',{})));
  $('hzReviewDialog').addEventListener('cancel',invalidate);
  $('hzPlans').addEventListener('click',e=>{
    const button=e.target.closest('[data-hz-action]'),row=e.target.closest('[data-hz-plan]');if(!button||!row)return;
    const p=state?.plans.find(p=>p.id===row.dataset.hzPlan&&p.code===row.dataset.hzCode);if(!p)return;
    if(button.dataset.hzAction==='pause'){pause(p.code);return;}
    if(button.dataset.hzAction==='chart'){if(!setMode('horizon'))return;chartPlan=p;chart();return;}
    if(button.dataset.hzAction==='remove')action(async()=>{invalidate();++statusRevision;const s=await api('/remove',{code:p.code,plan_id:p.id});++statusRevision;if(s.deleted_plan_id!==p.id||s.plans.some(x=>x.id===p.id))throw Error('삭제 결과 확인 필요');stale=false;lastOk=Date.now();render(s);feedback('호라이즌 조건을 삭제했습니다. 잔고·주문 기록은 유지됩니다.');});
  });
  window.addEventListener('hashchange',()=>setMode(location.hash==='#horizon'?'horizon':'custom',false));
  window.addEventListener('offline',()=>unknown('인터넷 연결 중단 · 호라이즌 감시 상태 확인 필요'));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  setInterval(()=>{if(lastOk&&Date.now()-lastOk>10000&&!stale)unknown();if(review){const left=Math.max(0,90-Math.floor((Date.now()-review.reviewedAt)/1000));$('hzReviewTimer').textContent=left?`${left}초 안에 확인하세요.`:'확인 시간이 지났습니다. 다시 확인하세요.';if(!left)$('hzArm').disabled=true;}},1000);
  setMode(location.hash==='#horizon'?'horizon':'custom',false);refresh();setInterval(refresh,2000);
  fetch('/api/autosell',{cache:'no-store',signal:AbortSignal.timeout(5000)}).then(r=>r.ok?r.json():null).then(s=>{for(const p of s?.positions||[])if(p.bought_on)dates[p.code]=p.bought_on;if(!$('hzBoughtOn').value&&dates[$('code').value])$('hzBoughtOn').value=dates[$('code').value];}).catch(()=>{});
})();
