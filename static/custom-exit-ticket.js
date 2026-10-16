(() => {
  'use strict';
  const $=id=>document.getElementById(id), num=v=>Number(v).toLocaleString('ko-KR',{maximumFractionDigits:4});
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const names={profit:'🎯 익절',protection:'🛡 이익보전',loss:'🛑 손절'}, comps={gte:'이상',lte:'이하',lt:'미만'};
  const labels={paused:'꺼짐',expired:'기한 종료',done:'실행 완료',closed:'주문 종료',pending:'체결 확인 중',checking:'주문 준비 중',
    watching:'감시 중',armed:'감시 대기',session_wait:'장 상태 대기',quote_wait:'시세 대기',connection_wait:'연결 확인 필요',
    remainder_check:'남은 잔고 확인 중',ordered:'체결 확인 중',prepared:'시작 전',completed:'완료',submitting:'전송 중',
    accepted:'접수',partial:'일부 체결',filled:'체결 완료',unknown:'응답 불명',rejected:'거부',not_sent:'전송 안 됨',attention:'미체결 확인 필요'};
  let planMarkup='',token='',positions=[],snapshot=null,kind='protection',editing=null,review=null,busy=false,polling=false,revision=0,statusVersion=0,lastOk=0,stale=false;
  const hoursName=r=>({off:'정규장만',KRX:'KRX 시간외',NXT:'NXT 시간외',both:'08:00~20:00 · 시장 자동 선택'})[r.after_hours||'off'];
  const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  function error(msg){$('error').textContent=msg;$('error').hidden=!msg;}
  function feedback(msg){$('feedback').textContent=msg;$('feedback').hidden=!msg;}
  function invalidate(){revision++;review=null;if($('reviewDialog').open)$('reviewDialog').close();$('review').replaceChildren();}
  async function api(path='',body){
    const response=await fetch('/api/custom-exit'+path,{method:body===undefined?'GET':'POST',cache:'no-store',
      headers:body===undefined?{}:{'Content-Type':'application/json','X-Custom-Exit-Token':token},
      body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(body===undefined?5000:30000)});
    const value=await response.json();if(!response.ok)throw Error(typeof value.detail==='string'?value.detail:'가격·수량·기간을 확인하세요');return value;
  }
  const selected=()=>positions.find(p=>p.code===$('code').value);
  function expiry(){return $('expiryMode').value==='none'?null:$('expiryMode').value==='today'?today():$('expiryDate').value;}
  function ruleText(r){return `${num(r.price)}원 ${comps[r.comparison]} → 매도 ${r.all_remaining?'남은 전량 ('+num(r.quantity)+'주)':num(r.quantity)+'주'} · ${r.order_type==='limit'?num(r.limit_price)+'원 지정가':'시장가'}`;}
  function until(r){return r.expires_on?r.expires_on+(r.after_hours&&r.after_hours!=='off'?' 20:00까지':' 15:20까지'):'직접 끌 때까지';}
  function draft(){const p=selected();return {code:$('code').value,kind,price:$('triggerPrice').value,comparison:$('comparison').value,
    quantity:$('allRemaining').checked&&p?p.quantity:Number($('sellQuantity').value),all_remaining:$('allRemaining').checked,
    order_type:$('orderType').value,limit_price:$('orderType').value==='limit'?Number($('limitPrice').value):null,
    expires_on:expiry(),rule_id:editing?.ruleId||null,after_hours:$('afterHours').value||'off',retry_unfilled:$('retryUnfilled').checked,empty_book:$('floorFallback').checked?'floor':'wait'};}
  function preview(){
    const ah=$('afterHours').value&&$('afterHours').value!=='off';
    $('retryUnfilled').disabled=!ah||kind==='profit'||busy;if(!ah||kind==='profit')$('retryUnfilled').checked=false;
    $('floorFallback').disabled=!ah||$('orderType').value!=='market'||busy;if(!ah||$('orderType').value!=='market')$('floorFallback').checked=false;
    const p=selected(),r=draft(),limit=r.order_type==='limit';
    $('average').textContent=p?num(p.average)+'원':'—';$('quantity').textContent=p?num(p.quantity)+'주':'—';$('available').textContent=p?num(p.available)+'주':'—';
    $('limitPrice').disabled=!limit||busy;$('limitPrice').required=limit;$('limitPrice').placeholder=limit?'주문 가격':'0';
    $('sellQuantity').disabled=r.all_remaining||busy;$('sellQuantity').max=p?.quantity||'';if(p&&r.all_remaining)$('sellQuantity').value=p.quantity;
    $('expiryDate').disabled=$('expiryMode').value!=='date'||busy;$('expiryDate').required=$('expiryMode').value==='date';
    $('expiryDate').min=today();
    $('priceHelp').textContent=limit?'지정가 · 호가단위 확인 · 미체결로 남을 수 있습니다.':'시장가 · 체결가는 기준 가격과 다를 수 있습니다.';
    const change=p?(Number(r.price)/p.average-1)*100:0;
    $('ticketPreview').innerHTML=p&&Number(r.price)>0?`${esc(ruleText(r))}<span class="${change>=0?'up':'down'}"> · 평단 ${num(p.average)} 대비 ${change>=0?'+':''}${num(Number(change.toFixed(2)))}%</span>`:'종목과 기준 가격을 선택하세요.';
    $('newCondition').hidden=!editing;
    if(document.activeElement?.id!=='exitChartCanvas')window.ExitChart?.setTicketDraft(p?{position:p,rule:r}:null);
  }
  function setKind(next,fill=true){
    kind=next;$('kindTitle').textContent=names[kind];$('kindTitle').className='ticket-kind '+kind;
    $('comparison').innerHTML=kind==='profit'?'<option value="gte">이 가격 이상 (≥)</option>':'<option value="lt">이 가격 미만 (&lt;)</option><option value="lte">이 가격 이하 (≤)</option>';
    if(kind==='loss')$('comparison').value='lte';
    document.querySelectorAll('[data-kind]').forEach(b=>{b.classList.toggle('selected',b.dataset.kind===kind);b.setAttribute('aria-pressed',String(b.dataset.kind===kind));});
    if(fill){$('afterHours').value='both';editing=null;$('percent').value=kind==='profit'?15:kind==='loss'?-10:'';$('triggerPrice').value='';
      if(kind!=='protection'&&selected())$('triggerPrice').value=String(Number((selected().average*(1+Number($('percent').value)/100)).toFixed(4)));}
    invalidate();preview();
  }
  async function action(fn){
    if(busy)return;busy=true;error('');feedback('');
    document.querySelectorAll('#customModePanel button:not(.stop),#plansSection button:not(.stop),#customOrders button,#reviewDialog button,#prepareForm input,#prepareForm select,#code,#loadHoldings,[data-exit-mode]').forEach(e=>e.disabled=true);
    try{await fn();}catch(e){error(e.message);}finally{busy=false;document.querySelectorAll('#customModePanel button:not(.stop),#plansSection button:not(.stop),#customOrders button,#reviewDialog button,#prepareForm input,#prepareForm select,#code,#loadHoldings,[data-exit-mode]').forEach(e=>e.disabled=false);preview();if($('arm'))$('arm').disabled=!$('ack').checked;await refresh();}
  }
  function statusOf(p,r,s){
    if(stale)return 'unknown';
    if(['pending','checking'].includes(r.state))return r.state;
    if(!r.enabled)return r.state==='watching'?'paused':r.state;
    if(s.error||!s.connected)return 'connection_wait';
    return r.watch_state||p.watch_state||p.status;
  }
  function render(s){
    window.ExitOrderTools?.update('custom',s);
    snapshot=s;token=s.token||token;window.ExitWorkspace?.customStatus(s);window.ExitChart?.setPlans(s.plans);
    $('environment').textContent=s.environment==='real'?'키움 실계좌':'키움 모의계좌';
    $('status').textContent=s.error?'상태 확인 필요':s.enabled?'내 조건 자동매도 켜짐':'내 조건 자동매도 꺼짐';
    $('statusDot').className=s.enabled&&s.connected&&!s.error?'active':'';
    $('connection').textContent=s.connected?(s.market==='3'?'계좌 연결 · KRX 연속매매':'계좌 연결 · 정규장 외'):'계좌 감시 연결 필요';
    $('updated').textContent=new Date(s.asof).toLocaleTimeString('ko-KR',{timeZone:'Asia/Seoul',hour12:false})+' 확인';
    if(s.error)error(s.error);
    let watching=0,count=0;
    const rows=s.plans.flatMap(p=>(p.conditions||[]).map(r=>{
      count++;const state=statusOf(p,r,s);if(state==='watching')watching++;
      const completed=['done','closed'].includes(r.state),pending=['pending','checking'].includes(r.state);
      return `<article class="condition-row ${esc(r.kind)}" data-code="${esc(p.code)}" data-rule="${esc(r.id)}" data-plan="${esc(p.id)}" tabindex="0" aria-label="${esc(p.name+' '+names[r.kind]+' '+ruleText(r))}">
        <div class="condition-line"><span class="condition-dot ${state==='watching'?'on':r.enabled||pending?'waiting':''}"></span><span class="condition-copy"><b>${esc(p.name)}</b> — ${esc(ruleText(r))} <span class="condition-kind">(${esc(names[r.kind])} · ${esc(hoursName(r))}${r.retry_unfilled?' · 재주문 ≤5회':''} · ${esc(r.expires_on?r.expires_on.slice(5).replace('-','/')+'까지':'직접 끌 때까지')})</span></span><span class="rule-state ${esc(state)}">${esc(labels[state]||state)}</span></div>
        <div class="condition-details"><span>${esc(p.code)} · 평단 ${num(p.average)}원</span>${p.note&&(p.status==='paused'||r.enabled&&state!=='watching'||pending)?`<span class="condition-note">${esc(p.note)}</span>`:''}
        <div class="condition-actions">${pending&&r.retry_unfilled?'<button type="button" data-action="stop-reprice" class="stop">재주문 중지</button>':''}<button type="button" data-action="chart">차트</button>${!completed&&!pending?`<button type="button" data-action="toggle" class="${r.enabled?'stop':'secondary'}">${r.enabled?(p.mode==='tickets'?'끄기':'묶음 끄기'):'확인 후 켜기'}</button>`:''}${!r.enabled&&!pending&&!completed?'<button type="button" data-action="edit">수정</button>':''}${!r.enabled&&!pending?'<button type="button" data-action="delete">삭제</button>':''}</div></div></article>`;
    }));
    $('planCount').textContent=`${count}개 · ${watching}개 감시 중`;
    // Keep stable rows when the returned markup is unchanged.
    const markup=rows.join('')||'<p class="empty">등록된 조건이 없습니다.<br>위에서 조건을 만들어 보세요.</p>';
    if(planMarkup!==markup){$('plans').innerHTML=markup;planMarkup=markup;}
    $('orders').innerHTML=s.orders.length?[...s.orders].reverse().map(o=>`<tr><td>${esc(o.at.slice(5,19).replace('T',' '))}<small>${esc(o.name)}</small></td><td>${esc(o.reason.startsWith('TICKET_')?names[o.reason.slice(7).toLowerCase()]:o.reason==='CUSTOM_PROFIT_PROTECTION'?'이익보전':o.reason==='CUSTOM_TAKE_PROFIT'?'익절':'손절')}<small>${o.order_type==='limit'?num(o.limit_price)+'원 지정가':'시장가'}</small></td><td>${num(o.filled)} / ${num(o.quantity)}주<small>미체결 ${o.remaining==null?'확인 중':num(o.remaining)+'주'}</small></td><td>${esc(labels[o.status]||o.status)}</td><td>${esc(o.order_no||'미확정')}<small>${esc(o.venue?o.venue+' · ':'')}${esc(o.note)}${o.manual_cancel_state?`<small class="cancel-result">${esc(o.manual_cancel_note)}${o.manual_cancel_no?' · 취소번호 '+esc(o.manual_cancel_no):''}</small>`:''}${o.after_hours&&o.chase_active?' · 재주문 감시 중':''}</small></td></tr>`).join(''):'<tr><td colspan="5" class="empty">전송한 주문이 없습니다.</td></tr>';
  }
  function unknown(msg){window.ExitWorkspace?.customStatus(null);planMarkup='';stale=true;error(msg||'상태 갱신 지연 · 현재 감시 여부를 확인할 수 없습니다');$('status').textContent='감시 상태 확인 필요';$('statusDot').className='';$('planCount').textContent='상태 미확인';document.querySelectorAll('.condition-dot').forEach(e=>e.className='condition-dot');document.querySelectorAll('.rule-state').forEach(e=>{e.className='rule-state unknown';e.textContent='상태 미확인';});}
  async function refresh(){if(polling)return;polling=true;const v=statusVersion;try{const s=await api();if(v!==statusVersion)return;if(!Array.isArray(s.plans)||!Array.isArray(s.orders))throw Error('상태 응답 확인 필요');if(stale)error('');stale=false;lastOk=Date.now();render(s);}catch(e){if(v===statusVersion)unknown('상태 조회 실패 · '+e.message);}finally{polling=false;}}
  async function holdings(code){
    const s=await api('/holdings',{});positions=s.positions||[];const selectedCode=code||$('code').value;
    $('code').innerHTML='<option value="">매도할 종목을 선택하세요</option>'+positions.map(p=>`<option value="${esc(p.code)}">${esc(p.name)} · ${num(p.quantity)}주</option>`).join('');
    $('code').value=positions.some(p=>p.code===selectedCode)?selectedCode:'';preview();window.ExitWorkspace?.positionsChanged();
  }
  async function prepare(body){
    invalidate();const v=revision;
    const p=await api('/tickets/prepare',body);if(v!==revision)throw Error('확인 중 입력·조건이 바뀌었습니다. 다시 확인하세요');
    review=p;const env=p.environment==='real'?'실계좌':'모의계좌';
    $('review').innerHTML=`<p class="eyebrow">조건 최종 확인 · ${env}</p><h2 id="reviewTitle">${esc(p.name)} · ${esc(names[p.rule.kind])}</h2><p class="review-summary">${esc(ruleText(p.rule))}</p><p>평단 ${num(p.average)}원 · 보유 ${num(p.quantity)}주<br>${esc(until(p.rule))} · ${esc(hoursName(p.rule))}</p>${p.rule.after_hours&&p.rule.after_hours!=='off'?`<p class="help">시간외 시장가는 최대 10단계 매수호가 중 가장 낮은 지정가로 변환합니다.<br>${p.rule.retry_unfilled?'손절·이익보전: 12초마다 확인 · 취소 완료 후 한 호가 아래 재주문 · 종목당 하루 최대 5회.':'자동 재주문 없음.'}<br>${p.rule.empty_book==='floor'?'매수호가가 비면 당일 하한가로 주문합니다.':'매수호가가 없으면 주문 대기합니다.'}</p>`:''}<p class="help">기준을 이미 충족했다면 감시를 켠 뒤 새 시세 수신 시 바로 주문할 수 있습니다. ${p.rule.order_type==='limit'?'지정가 주문은 미체결로 남을 수 있습니다.':'시장가 체결가격은 기준 가격과 다를 수 있습니다.'} 다른 켜진 조건도 잔고 확인 후 계속 감시합니다.</p><label class="review-ack"><input id="ack" type="checkbox"> 위 조건과 수량을 확인했습니다. 조건 충족 시 별도 확인 없이 매도 주문을 요청합니다.</label><button id="arm" type="button" class="primary" disabled>${env} · 이 조건 감시 켜기</button><button id="cancelReview" type="button" class="secondary">닫기 · 켜지 않기</button><small id="reviewTimer">90초 안에 확인하세요.</small>`;
    $('ack').addEventListener('change',()=>{$('arm').disabled=busy||!$('ack').checked;});
    $('cancelReview').addEventListener('click',invalidate);
    $('arm').addEventListener('click',()=>action(async()=>{
      if(!review||!$('ack').checked)return;const active=review;
      try{await api('/tickets/arm',{review_id:active.review_id,environment:active.environment,confirm_order:true});invalidate();editing=null;feedback('조건을 켰습니다. 아래 목록에서 감시 상태를 확인하세요.');}
      catch(e){invalidate();throw Error('시작 결과를 확인하세요. 목록 상태를 확인한 뒤 다시 설정하세요. '+e.message);}
    }));
    $('reviewDialog').showModal();
  }
  function findRule(row){const p=snapshot?.plans.find(p=>p.code===row.dataset.code&&p.id===row.dataset.plan);const r=p?.conditions.find(r=>r.id===row.dataset.rule);return {p,r};}
  async function pauseRule(p,r){
    ++statusVersion;invalidate();
    try{const s=await api('/tickets/pause',{code:p.code,plan_id:p.id,rule_id:r.id});++statusVersion;render(s);feedback('조건 감시를 껐습니다. 접수된 주문은 키움에서 별도 확인하세요.');}
    catch(e){unknown('중지 응답 미확인 · '+e.message);}await refresh();
  }
  async function toggle(p,r){
    if(r.enabled){await pauseRule(p,r);return;}
    if(['done','closed','pending','checking'].includes(r.state))return;
    await action(async()=>{await holdings(p.code);const position=selected();if(!position)throw Error('현재 보유 잔고를 확인하세요');await prepare({code:p.code,kind:r.kind,price:r.price,comparison:r.comparison,quantity:r.all_remaining?position.quantity:Math.min(r.quantity,position.quantity),all_remaining:r.all_remaining,order_type:r.order_type,limit_price:r.limit_price,expires_on:r.expires_on,rule_id:r.id,after_hours:r.after_hours||'off',retry_unfilled:!!r.retry_unfilled,empty_book:r.empty_book||'wait'});});
  }
  $('plans').addEventListener('click',e=>{
    const b=e.target.closest('[data-action]'),row=e.target.closest('.condition-row');if(!row)return;if(!b){row.focus();return;}
    const {p,r}=findRule(row);if(!p||!r)return;
    if(b.dataset.action==='stop-reprice'){pauseRule(p,r);return;}
    if(b.dataset.action==='chart'){window.ExitChart?.showPlan(p.code);return;}
    if(b.dataset.action==='toggle'){toggle(p,r);return;}
    if(b.dataset.action==='delete'){action(async()=>{invalidate();await api('/tickets/remove',{code:p.code,plan_id:p.id,rule_id:r.id});feedback('꺼진 조건을 삭제했습니다. 주문 기록은 유지됩니다.');});return;}
    if(b.dataset.action==='edit')action(async()=>{
      await holdings(p.code);setKind(r.kind,false);editing={ruleId:r.id,code:p.code};$('triggerPrice').value=r.price;$('comparison').value=r.comparison;
      $('percent').value='';$('orderType').value=r.order_type;$('limitPrice').value=r.limit_price||'';$('sellQuantity').value=r.quantity;$('allRemaining').checked=r.all_remaining;
      $('afterHours').value='both';$('retryUnfilled').checked=!!r.retry_unfilled;$('floorFallback').checked=r.empty_book==='floor';
      $('expiryMode').value=r.expires_on?'date':'none';$('expiryDate').value=r.expires_on||'';preview();$('prepareForm').scrollIntoView({behavior:'smooth',block:'center'});
    });
  });
  $('plans').addEventListener('dblclick',e=>{if(e.target.closest('button'))return;const row=e.target.closest('.condition-row');if(!row)return;const {p,r}=findRule(row);if(p&&r)toggle(p,r);});
  $('loadHoldings').addEventListener('click',()=>action(async()=>{invalidate();await holdings();}));
  document.querySelectorAll('[data-kind]').forEach(b=>b.addEventListener('click',()=>setKind(b.dataset.kind)));
  $('code').addEventListener('change',()=>{editing=null;$('triggerPrice').value='';$('percent').value='';invalidate();preview();});
  $('orderType').addEventListener('change',()=>{if($('orderType').value==='market')$('limitPrice').value='';});
  $('prepareForm').addEventListener('input',e=>{if(e.target.id==='triggerPrice')$('percent').value='';invalidate();preview();});$('prepareForm').addEventListener('change',()=>{invalidate();preview();});
  $('applyPercent').addEventListener('click',()=>{const p=selected(),pct=Number($('percent').value);if(!p||!$('percent').value||!Number.isFinite(pct)||pct<=-100||pct>1000){error('종목과 평단 대비 퍼센트를 입력하세요.');return;}$('triggerPrice').value=String(Number((p.average*(1+pct/100)).toFixed(4)));invalidate();preview();});
  $('newCondition').addEventListener('click',()=>{editing=null;setKind(kind);});
  $('prepareForm').addEventListener('submit',e=>{e.preventDefault();action(()=>prepare(draft()));});
  $('reviewDialog').addEventListener('cancel',()=>{review=null;revision++;});
  $('stopAll').addEventListener('click',async()=>{
    ++statusVersion;invalidate();try{const s=await api('/pause',{code:null});++statusVersion;render(s);feedback('내 조건 감시를 모두 껐습니다. 접수된 주문은 키움에서 확인하세요.');}catch(e){unknown('중지 응답 미확인 · '+e.message);}await refresh();
  });
  $('reconcile').addEventListener('click',()=>action(()=>api('/reconcile',{})));
  setInterval(()=>{
    if(lastOk&&Date.now()-lastOk>10000&&!stale)unknown();
    if(review){const remaining=Math.max(0,90-Math.floor((Date.now()-new Date(review.prepared_at).getTime())/1000));$('reviewTimer').textContent=remaining?`${remaining}초 안에 확인하세요.`:'확인 시간이 지났습니다. 닫고 다시 확인하세요.';if(!remaining)$('arm').disabled=true;}
  },1000);
  window.addEventListener('offline',()=>unknown('인터넷 연결 중단 · 감시 상태 확인 필요'));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  window.CustomExitUI={selected,positions:()=>positions,clearReview:invalidate,refreshChart:preview,isBusy:()=>busy};
  preview();refresh();setInterval(refresh,2000);
})();
