(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = value => Number(value).toLocaleString('ko-KR',{maximumFractionDigits:2});
  const labels = {prepared:'시작 전',watching:'감시 중',armed:'감시 대기',session_wait:'장 상태 대기',quote_wait:'시세 대기',
    connection_wait:'연결 확인 필요',checking:'매도 준비 중',remainder_check:'남은 잔고 확인 중',ordered:'체결 확인 중',
    paused:'감시 중지',completed:'설정 수량 체결 완료',submitting:'전송 중',accepted:'접수',partial:'일부 체결',
    filled:'설정 수량 체결 완료',unknown:'응답 불명',rejected:'거부',not_sent:'전송 안 됨',closed:'잔량 종료',attention:'미체결 확인 필요'};
  let token='', positions=[], plan=null, busy=false, polling=false, lastOk=0, stale=false, revision=0, statusRevision=0;
  function error(message){$('#error').textContent=message;$('#error').hidden=!message;}
  async function api(path='', body){
    const response=await fetch('/api/custom-exit'+path,{method:body===undefined?'GET':'POST',cache:'no-store',
      headers:body===undefined?{}:{'Content-Type':'application/json','X-Custom-Exit-Token':token},
      body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(body===undefined?5000:30000)});
    const value=await response.json();
    if(!response.ok)throw Error(typeof value.detail==='string'?value.detail:'입력한 퍼센트와 수량을 확인하세요');
    return value;
  }
  function invalidate(){revision++;plan=null;$('#review').hidden=true;$('#review').replaceChildren();}
  function selected(){return positions.find(p=>p.code===$('#code').value);}
  function preview(){
    const p=selected();
    $('#average').textContent=p?num(p.average)+'원':'—';$('#quantity').textContent=p?num(p.quantity)+'주':'—';
    $('#available').textContent=p?num(p.available)+'주':'—';
    for(const [prefix,sign] of [['tp',1],['sl',-1]]){
      const enabled=$('#'+prefix+'Enabled').checked, pct=Number($('#'+prefix+'Pct').value), qty=Number($('#'+prefix+'Qty').value);
      $('#'+prefix+'Pct').disabled=!enabled;$('#'+prefix+'Qty').disabled=!enabled;
      $('#'+prefix+'Pct').required=enabled;$('#'+prefix+'Qty').required=enabled;
      $('#'+prefix+'Qty').max=p?p.quantity:'';
      $('#'+prefix+'Enabled').closest('fieldset').classList.toggle('disabled',!enabled);
      $('#'+prefix+'Preview').textContent=!enabled?'이 조건은 사용하지 않습니다':p&&pct>0&&qty>0?
        `${num(p.average*(1+sign*pct/100))}원 ${sign>0?'이상':'이하'} → ${num(qty)}주`:'조건을 입력하면 기준 가격이 표시됩니다';
    }
    const pp=$('#ppEnabled').checked, price=Number($('#ppPrice').value);
    $('#ppPrice').disabled=!pp;$('#ppPrice').required=pp;
    $('#ppEnabled').closest('fieldset').classList.toggle('disabled',!pp);
    $('#ppPreview').textContent=!pp?'이익보전 조건은 사용하지 않습니다':p&&Number.isSafeInteger(price)&&price>0?
      `${num(price)}원 미만 → 보유 ${num(p.quantity)}주 전량 시장가 매도 (${num(price)}원에서는 매도하지 않음)`:
      '기준 가격을 입력하세요 · 선택한 종목의 보유 수량 전부';
    window.ExitChart?.setDraft(p?{position:p,inputs:{pp:{enabled:pp,price:price},tp:{enabled:$('#tpEnabled').checked,pct:$('#tpPct').value},sl:{enabled:$('#slEnabled').checked,pct:$('#slPct').value}}}:null);
  }
  async function action(fn){
    if(busy)return;busy=true;error('');$('#feedback').hidden=true;
    const buttons=[...document.querySelectorAll('button:not(.stop)')];buttons.forEach(b=>b.disabled=true);
    const inputs=[...document.querySelectorAll('#prepareForm input, #prepareForm select, #review input')].map(el=>[el,el.disabled]);
    inputs.forEach(([el])=>el.disabled=true);
    try{await fn();await refresh();}catch(e){error(e.message);}finally{busy=false;buttons.forEach(b=>b.disabled=false);inputs.forEach(([el,disabled])=>el.disabled=disabled);preview();if($('#arm'))$('#arm').disabled=!$('#ack').checked;}
  }
  function legText(leg,label,done=false){return leg?`<span class="${label==='익절'?'up':'down'} ${done?'done-leg':''}">${label} ${label==='익절'?'+':'−'}${num(leg.pct)}% · ${num(leg.quantity)}주 · ${num(leg.price)}원${done?' (실행 완료)':''}</span>`:`<span>${label} 미설정</span>`;}
  function protectionText(p){const leg=p.profit_protection;return leg?`<span class="protection-text">이익보전 ${num(leg.price)}원 미만 · 남은 ${num(leg.quantity)}주 전량</span>`:'';}
  function orderReason(o){return o.reason==='CUSTOM_PROFIT_PROTECTION'?`이익보전 ${num(o.trigger_price)}원 미만`:
    `${o.reason==='CUSTOM_TAKE_PROFIT'?'익절 +':'손절 −'}${num(o.trigger_pct)}%`;}
  function cancelControl(p){
    const active=['prepared','armed','checking','remainder_check'].includes(p.status)||(p.status==='ordered'&&p.continue_stop);
    return `<div class="plan-actions"><button type="button" class="secondary" data-chart-plan="${esc(p.code)}">차트 보기</button>${active?`<button class="stop" data-pause="${esc(p.code)}">조건 취소 (감시 끄기)</button>`:`<span class="muted">${p.status==='completed'?'완료된 조건':'조건 감시 꺼짐'}</span>`}${p.status==='ordered'?'<small>접수된 주문은 키움에서 별도 확인·취소</small>':''}</div>`;
  }
  function render(s){
    token=s.token||token;
    window.ExitChart?.setPlans(s.plans);
    $('#environment').textContent=s.environment==='real'?'키움 실계좌':'키움 모의계좌';
    const ready=!s.error&&s.connected;
    $('#status').textContent=s.error?'상태 확인 필요':s.enabled?'내 조건 자동매도 켜짐':'내 조건 자동매도 꺼짐';
    $('#statusDot').className=s.enabled&&ready?'active':'';
    $('#connection').textContent=s.connected?(s.market==='3'?'계좌 연결 · KRX 연속매매':'계좌 연결 · 장 상태 대기'):'계좌 감시 연결 필요';
    $('#updated').textContent=new Date(s.asof).toLocaleTimeString('ko-KR',{timeZone:'Asia/Seoul',hour12:false})+' 확인';
    if(s.error)error(s.error);
    const watching=s.plans.filter(p=>p.watch_state==='watching').length;
    $('#planCount').textContent=`${watching}종목 감시 중`;
    $('#plans').innerHTML=s.plans.length?s.plans.map(p=>{
      const state=(s.error||(!s.connected&&['armed','checking','remainder_check'].includes(p.status)))?'connection_wait':p.watch_state;
      return `<article class="plan"><div class="info"><b>${esc(p.name)}</b> <small>${esc(p.code)}</small> <span class="pill ${esc(state)}">${esc(labels[state]||state)}</span><p>확인한 잔고 ${num(p.quantity)}주 · 평단 ${num(p.average)}원</p><div class="conditions">${protectionText(p)}${legText(p.take_profit,'익절',p.take_profit_done)}${legText(p.stop_loss,'손절')}</div><p class="note">${esc(p.note)}</p></div>${cancelControl(p)}</article>`;
    }).join(''):'<p class="empty">아직 등록한 직접 설정 계획이 없습니다.</p>';
    $('#orders').innerHTML=s.orders.length?[...s.orders].reverse().map(o=>`<tr><td>${esc(o.at.slice(5,19).replace('T',' '))}<small>${esc(o.name)}</small></td><td>${orderReason(o)}</td><td>${num(o.filled)} / ${num(o.quantity)}주</td><td>${esc(labels[o.status]||o.status)}</td><td>${esc(o.order_no||'미확정')}<small>${esc(o.note)}</small></td></tr>`).join(''):'<tr><td colspan="5" class="empty">전송한 직접 설정 주문이 없습니다.</td></tr>';
  }
  function unknown(message){
    stale=true;error(message||'상태 갱신 지연 · 현재 감시 여부를 확인할 수 없습니다');
    $('#status').textContent='감시 상태 확인 필요';$('#statusDot').className='';
    $('#connection').textContent='연결 상태 미확인';$('#planCount').textContent='상태 미확인';
    $('#plans').querySelectorAll('.pill').forEach(p=>{p.className='pill unknown';p.textContent='상태 미확인';});
  }
  async function refresh(){
    if(polling)return;polling=true;const version=statusRevision;
    try{const s=await api();if(version!==statusRevision)return;if(!Array.isArray(s.plans)||!Array.isArray(s.orders))throw Error('상태 응답 확인 필요');if(stale)error('');stale=false;render(s);lastOk=Date.now();}
    catch(e){if(version===statusRevision)unknown('상태 조회 실패 · '+e.message);}finally{polling=false;}
  }
  function readLeg(prefix){
    if(!$('#'+prefix+'Enabled').checked)return [null,null];
    const pct=Number($('#'+prefix+'Pct').value),qty=Number($('#'+prefix+'Qty').value);
    if(!Number.isFinite(pct)||pct<=0||!Number.isSafeInteger(qty)||qty<=0)throw Error('사용할 조건의 퍼센트와 수량을 입력하세요');
    return [pct,qty];
  }
  function review(p){
    const env=p.environment==='real'?'실계좌':'모의계좌';
    const protection=p.profit_protection;
    const protectionHelp=protection?`<p class="protection-review"><b>${num(protection.price)}원 미만이면 남은 보유 수량 전부를 시장가 매도합니다.</b> 같은 가격에서는 주문하지 않습니다. 켜는 시점에 이미 기준가 미만이면 새 시세 수신 후 바로 매도할 수 있습니다. 손절 조건도 함께 충족하면 이익보전 전량 매도를 우선합니다.</p>`:'';
    const continuation=protection?'익절 후에도 남은 수량의 이익보전 감시를 계속합니다.':p.stop_loss?'익절 후 남은 수량의 손절 감시를 계속합니다.':'하락 시 매도 조건이 없어 익절 후 이 계획은 끝납니다.';
    $('#review').innerHTML=`<h3>${esc(p.name)} · 내 조건 확인</h3><p>평단 ${num(p.average)}원 · 보유 ${num(p.quantity)}주</p><div class="conditions">${protectionText(p)}${legText(p.take_profit,'익절')}${legText(p.stop_loss,'손절')}</div>${protectionHelp}<p class="help">${continuation}</p><label><input type="checkbox" id="ack"> 위 ${env} 조건과 수량을 확인했습니다. 조건에 도달하면 별도 확인 없이 <b>KRX 시장가 매도</b>를 요청합니다.</label><button type="button" id="arm" class="primary" disabled>${env} · 이 조건으로 감시 켜기</button><small>90초 안에 시작하세요. 시작 직후 조건을 충족하면 주문할 수 있습니다. 실제 체결가격은 기준 가격과 다를 수 있습니다.</small>`;
    $('#review').hidden=false;
    $('#review').insertAdjacentHTML('beforeend',`<button type="button" class="stop review-cancel" data-pause="${esc(p.code)}">이 조건 취소 (감시 끄기)</button>`);
    $('#ack').addEventListener('change',()=>{$('#arm').disabled=!$('#ack').checked||busy;});
    $('#arm').addEventListener('click',()=>action(async()=>{if(!plan||!$('#ack').checked)return;await api('/arm',{plan_id:plan.id,environment:plan.environment,market_order:true});invalidate();}));
  }
  $('#loadHoldings').addEventListener('click',()=>action(async()=>{
    const s=await api('/holdings',{});positions=s.positions||[];invalidate();
    const chosen=$('#code').value;$('#code').innerHTML='<option value="">매도할 종목을 선택하세요</option>'+positions.map(p=>`<option value="${esc(p.code)}">${esc(p.name)} · ${num(p.quantity)}주</option>`).join('');
    $('#code').value=positions.some(p=>p.code===chosen)?chosen:'';preview();
  }));
  $('#prepareForm').addEventListener('input',()=>{invalidate();preview();});
  $('#prepareForm').addEventListener('change',()=>{invalidate();preview();});
  document.querySelectorAll('[data-all]').forEach(b=>b.addEventListener('click',()=>{const p=selected();if(!p)return;$('#'+b.dataset.all).value=p.quantity;invalidate();preview();}));
  $('#prepareForm').addEventListener('submit',e=>{e.preventDefault();action(async()=>{
    invalidate();const version=revision;
    const [tpPct,tpQty]=readLeg('tp'),[slPct,slQty]=readLeg('sl');
    const ppPrice=$('#ppEnabled').checked?Number($('#ppPrice').value):null;
    if(ppPrice!==null&&(!Number.isSafeInteger(ppPrice)||ppPrice<=0||ppPrice>=1e9))throw Error('이익보전 기준가를 1원 이상 정수로 입력하세요');
    if(tpPct===null&&slPct===null&&ppPrice===null)throw Error('익절·손절·이익보전 중 하나 이상 선택하세요');
    const p=await api('/prepare',{code:$('#code').value,take_profit_pct:tpPct,take_profit_quantity:tpQty,stop_loss_pct:slPct,stop_loss_quantity:slQty,profit_protection_price:ppPrice});
    if(version!==revision)throw Error('확인 중 입력이 바뀌었습니다. 다시 확인하세요');
    plan=p;review(p);
  });});
  async function pause(code){
    const version=++statusRevision;invalidate();error('');$('#feedback').hidden=true;
    try{
      const s=await api('/pause',{code:code||null});if(version!==statusRevision)return;
      statusRevision++;
      if(!Array.isArray(s.plans)||!Array.isArray(s.orders)||s.error)throw Error(s.error||'중지 상태 응답 확인 필요');
      if(s.plans.some(p=>(!code||p.code===code)&&(['prepared','armed','checking','remainder_check'].includes(p.status)||(p.status==='ordered'&&p.continue_stop))))throw Error('감시가 아직 꺼지지 않았습니다');
      stale=false;render(s);lastOk=Date.now();
      const pending=s.orders.some(o=>(!code||o.code===code)&&['submitting','accepted','partial','unknown','attention'].includes(o.status));
      $('#feedback').textContent=(code?`${code} 조건 감시를 껐습니다.`:'내 조건 감시를 모두 껐습니다.')+(pending?' 접수·미확정 주문은 취소되지 않았습니다. 키움 주문내역에서 확인하세요.':' 꺼진 조건은 기록으로 남습니다. 다시 사용하려면 조건을 확인하고 켜세요.');
      $('#feedback').hidden=false;
    }catch(e){if(version===statusRevision||version+1===statusRevision)unknown('취소 응답 미확인 · 감시 중지를 확인할 수 없습니다. '+e.message);}
  }
  $('#stopAll').addEventListener('click',()=>pause());
  $('#plans').addEventListener('click',e=>{const b=e.target.closest('[data-pause]');if(b)pause(b.dataset.pause);const c=e.target.closest('[data-chart-plan]');if(c)window.ExitChart?.showPlan(c.dataset.chartPlan);});
  $('#review').addEventListener('click',e=>{const b=e.target.closest('[data-pause]');if(b)pause(b.dataset.pause);});
  $('#reconcile').addEventListener('click',()=>action(()=>api('/reconcile',{})));
  preview();refresh();setInterval(refresh,2000);
  setInterval(()=>{if(lastOk&&Date.now()-lastOk>10000&&!stale)unknown();},1000);
  window.addEventListener('offline',()=>unknown('인터넷 연결 중단 · 감시 상태를 확인할 수 없습니다'));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){if(lastOk&&Date.now()-lastOk>10000)unknown();refresh();}});
})();
