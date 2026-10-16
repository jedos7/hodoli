(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = v => Number(v).toLocaleString('ko-KR');
  const pending = new Set(['submitting','accepted','partial','unknown','attention']);
  const names = {prepared:'확인 대기',armed:'매수 대기',watching:'가격 감시 중',checking:'주문 전 확인',
    ordered:'체결 확인 중',remainder_check:'매도 연결 확인 중',paused:'중지',completed:'완료',
    connection_wait:'연결 확인 대기',session_wait:'KRX 장 대기',quote_wait:'최신 가격 대기',
    submitting:'전송 중',accepted:'접수 · 체결 대기',partial:'일부 체결',filled:'전량 체결',
    closed:'잔량 종료',unknown:'응답 불명',attention:'주문 확인 필요',not_sent:'미전송',rejected:'거부'};
  let token='',state=null,review=null,revision=0,busy=false,polling=false,lastStatusAt=0,requestError='';
  const message = (id,text) => { $(id).textContent=text; $(id).hidden=!text; };
  function clearRequestError(){requestError='';message('error',state?.error||'');message('prepareError','');}
  function reportError(text,form=false){
    requestError=text;message('error',state?.error||requestError);
    const target=form?'prepareError':'error';message(target,text);
    $(target).scrollIntoView({behavior:'smooth',block:'nearest'});
  }
  async function api(path='',body) {
    const res=await fetch('/api/auto-buy'+path,{method:body===undefined?'GET':'POST',cache:'no-store',signal:AbortSignal.timeout(12000),
      headers:body===undefined?{}:{'Content-Type':'application/json','X-Auto-Buy-Token':token},
      ...(body===undefined?{}:{body:JSON.stringify(body)})});
    let data; try{data=await res.json();}catch{throw new Error('자동매수 서버 응답을 확인할 수 없습니다.');}
    if(!res.ok) throw new Error(typeof data.detail==='string'?data.detail:'입력값 또는 서버 연결을 확인하세요.');
    return data;
  }
  function linkText(p) {
    return [p.take_profit?`익절 +${p.take_profit.pct}%`:null,p.stop_loss?`손절 −${p.stop_loss.pct}%`:null].filter(Boolean).join(' · ') || '연결 안 함';
  }
  function render(s) {
    lastStatusAt=Date.now();
    state=s; if(s.token) token=s.token;
    $('status').textContent=s.error?'상태 확인 필요':s.enabled?'자동매수 진행 중':'자동매수 꺼짐';
    $('statusDot').classList.toggle('active',Boolean(s.enabled&&!s.error));
    $('environment').textContent=s.environment==='real'?'실전 계좌':'모의 계좌';
    $('connection').textContent=s.connected?'계좌 감시 연결됨':'계좌 감시 연결 전';
    $('updated').textContent='조회 '+s.asof.slice(11,19);
    $('prepare').disabled=busy||Boolean(s.error)||!token;
    $('stopAll').disabled=busy||!token;
    $('reconcile').disabled=busy||!token;
    // A healthy status poll does not mean the user's last request succeeded.
    message('error',s.error||requestError);
    $('plans').innerHTML=s.plans.length?s.plans.map(p=>{
      const blocked=s.orders.some(o=>o.code===p.code&&pending.has(o.status));
      const active=['prepared','armed','checking','ordered','remainder_check'].includes(p.status);
      return `<article class="plan"><div class="info"><b>${esc(p.name)} <small>${esc(p.code)}</small></b> <span class="pill ${esc(p.watch_state)}">${esc(names[p.watch_state]||p.status)}</span><p class="amount">${fmt(p.limit_price)}원 이하 · ${fmt(p.quantity)}주 · 원금 최대 ${fmt(p.limit_price*p.quantity)}원</p><p>${esc(p.expires_on)} 15:20까지 대기 · 매도 연결: ${esc(linkText(p))}</p><p>${esc(p.note)}</p>${p.linked_exit_id?'<a href="/custom-exit.html#plansSection">연결된 자동매도 확인 →</a>':''}</div><div class="plan-actions">${active?`<button class="stop" data-pause="${esc(p.code)}">매수 대기 중지</button>`:''}${!active&&!blocked?`<button data-remove="${esc(p.code)}" data-plan-id="${esc(p.id)}">계획 삭제</button>`:''}${blocked?'<small>접수된 매수 주문 있음<br>아래에서 잔량 취소 확인</small>':''}</div></article>`;
    }).join(''):'<p class="empty">등록한 자동매수 계획이 없습니다.</p>';
    $('orders').innerHTML=s.orders.length?[...s.orders].reverse().map(o=>`<tr><td><b>${esc(o.name)}</b><small>${esc(o.at.slice(0,19).replace('T',' '))}</small><small>주문 ${esc(o.order_no||'번호 확인 전')}</small></td><td>${fmt(o.limit_price)}원</td><td>${fmt(o.filled)} / ${fmt(o.quantity)}주<small>잔량 ${fmt(o.remaining)}주</small></td><td><span class="pill ${esc(o.status)}">${esc(names[o.status]||o.status)}</span>${o.cancel?`<small>취소 요청: ${esc(names[o.cancel.status]||o.cancel.status)}</small>`:''}</td><td>${esc(o.note)}${pending.has(o.status)&&o.order_no&&!o.cancel?`<p><button class="stop" data-cancel="${esc(o.id)}">미체결 잔량 취소…</button></p>`:''}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">자동매수 주문 기록이 없습니다.</td></tr>';
    if(review&&!s.plans.some(p=>p.id===review.id&&p.status==='prepared')) clearReview();
  }
  function clearReview(){review=null;$('review').hidden=true;$('review').innerHTML='';}
  function unknown(){
    $('status').textContent='연결 확인 필요 · 감시 상태 미확인';$('statusDot').classList.remove('active');
    $('plans').querySelectorAll('.pill').forEach(p=>{p.textContent='상태 미확인';p.className='pill unknown';});
    if($('arm'))$('arm').disabled=true;
  }
  async function refresh(){
    if(polling||busy)return; polling=true; const rev=revision;
    try{const s=await api();if(rev===revision)render(s);}
    catch(e){if(rev===revision){unknown();message('error',e.message);}}
    finally{polling=false;}
  }
  async function action(fn,form=false){
    if(busy)return;busy=true;revision++; document.querySelectorAll('button').forEach(b=>b.disabled=true);
    clearRequestError();message('feedback','');
    if(form){$('prepare').textContent='종목·금액 확인 중…';message('prepareProgress','종목 정보·보유 잔고·주문가능 금액을 확인하고 있습니다.');}
    try{await fn();}catch(e){reportError(e.message,form);}
    finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);$('prepare').disabled=!token||Boolean(state?.error);
      if(form){$('prepare').textContent='종목 · 금액 확인하기';message('prepareProgress','');}
      const arm=$('arm');if(arm)arm.disabled=!$('confirmLimit').checked||Boolean($('confirmExit')&&!$('confirmExit').checked);}
  }
  function amounts(){const amount=Number($('limitPrice').value)*Number($('quantity').value);$('total').textContent=Number.isSafeInteger(amount)&&amount>0?fmt(amount)+'원':'—';}
  function showReview(p){
    review=p; const linked=Boolean(p.take_profit||p.stop_loss);
    $('review').innerHTML=`<p class="eyebrow">02 · 최종 확인 (90초 이내)</p><h3>${esc(p.name)} · ${esc(p.code)}</h3><div class="review-line"><span>계좌</span><b>${p.environment==='real'?'실전 · 실제 매수 주문':'모의 계좌'}</b></div><div class="review-line"><span>지정가 / 수량</span><b>${fmt(p.limit_price)}원 이하 / ${fmt(p.quantity)}주</b></div><div class="review-line"><span>최대 원금</span><b>${fmt(p.limit_price*p.quantity)}원 + 수수료</b></div><div class="review-line"><span>대기 기한</span><b>${esc(p.expires_on)} 15:20</b></div><div class="review-line"><span>체결 후 자동매도</span><b>${esc(linkText(p))}</b></div><p class="help">현재 가격이 매수가보다 낮으면 켠 뒤 바로 주문될 수 있습니다. 지정가 주문은 미체결 또는 일부 체결될 수 있습니다.</p><label class="confirm-label"><input id="confirmLimit" type="checkbox">위 종목·가격·수량·금액·기한으로 실제 지정가 매수를 요청하는 데 동의합니다.</label>${linked?'<label class="confirm-label"><input id="confirmExit" type="checkbox">매수 잔량 종료 후 실제 평단·체결 수량으로 위 조건의 시장가 자동매도 감시를 연결합니다. 일부 체결 대기 중에는 감시되지 않음을 확인했습니다.</label>':''}<button id="arm" class="primary" disabled>이 계획 자동매수 켜기</button><button id="cancelPlan" class="secondary">계획 중지 · 다시 입력</button>`;
    $('review').hidden=false;
    const enable=()=>{$('arm').disabled=busy||!$('confirmLimit').checked||(linked&&!$('confirmExit').checked);};
    $('confirmLimit').addEventListener('change',enable);if(linked)$('confirmExit').addEventListener('change',enable);
    $('arm').onclick=()=>action(async()=>{const s=await api('/arm',{plan_id:p.id,environment:p.environment,confirm_limit:true,confirm_exit:linked});render(s);clearReview();message('feedback','매수 대기를 켰습니다. 아래 계획과 주문·체결 상태를 확인하세요.');});
    $('cancelPlan').onclick=()=>pause(p.code);
    $('review').scrollIntoView({behavior:'smooth',block:'center'});
  }
  async function pause(code){await action(async()=>{render(await api('/pause',{code}));if(review&&(!code||review.code===code))clearReview();message('feedback','매수 대기를 중지했습니다. 이미 접수된 주문과 연결 완료된 자동매도는 별도로 확인하세요.');});}
  $('prepareForm').addEventListener('submit',e=>{e.preventDefault();action(async()=>{
    const price=Number($('limitPrice').value),quantity=Number($('quantity').value),amount=price*quantity;
    if(!Number.isSafeInteger(price)||price<=0||!Number.isSafeInteger(quantity)||quantity<=0||!Number.isSafeInteger(amount))throw new Error('가격과 수량을 양의 정수로 입력하세요.');
    const linked=$('linkExit').checked,tp=linked&&$('tpPct').value!==''?Number($('tpPct').value):null,sl=linked&&$('slPct').value!==''?Number($('slPct').value):null;
    if(linked&&tp===null&&sl===null)throw new Error('연결할 익절 또는 손절 퍼센트를 입력하세요.');
    const p=await api('/prepare',{code:$('code').value.trim().toUpperCase(),limit_price:price,quantity,max_amount:amount,expires_on:$('expiresOn').value,take_profit_pct:tp,stop_loss_pct:sl});
    showReview(p);message('feedback','조회와 계획 저장만 완료했습니다. 아직 매수 대기를 켜지 않았습니다.');
  },true);});
  $('prepareForm').addEventListener('invalid',e=>{
    const titles={code:'종목코드',limitPrice:'매수가',quantity:'매수 수량',expiresOn:'매수 대기 마지막 날',tpPct:'익절 상승률',slPct:'손절 하락률'};
    reportError((titles[e.target.id]||'입력값')+': '+e.target.validationMessage,true);
  },true);
  $('prepareForm').addEventListener('input',()=>{clearRequestError();amounts();if(review){$('arm').disabled=true;clearReview();message('feedback','입력값이 변경되었습니다. 아래의 기존 확인 대기 계획을 중지한 뒤 다시 확인하세요.');}});
  $('linkExit').onchange=()=>{const linked=$('linkExit').checked;$('exitFields').hidden=!linked;$('tpPct').disabled=$('slPct').disabled=!linked;clearRequestError();};
  $('stopAll').onclick=()=>pause(null);
  $('reconcile').onclick=()=>action(async()=>render(await api('/reconcile',{})));
  $('plans').onclick=e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.pause)pause(b.dataset.pause);if(b.dataset.remove)action(async()=>{render(await api('/remove',{code:b.dataset.remove,plan_id:b.dataset.planId}));message('feedback','저장된 매수 계획을 삭제했습니다. 주문 기록은 유지됩니다.');});};
  $('orders').onclick=e=>{const b=e.target.closest('[data-cancel]');if(!b||busy)return;const o=state.orders.find(o=>o.id===b.dataset.cancel);if(!o)return;
    $('cancelReview').innerHTML=`<h3>${esc(o.name)} 미체결 잔량 취소</h3><p>원 매수 주문 ${esc(o.order_no)}의 남은 수량을 실제로 취소 요청합니다. 이미 체결된 주식은 그대로 보유합니다. 체결 후 자동매도 연결은 해제됩니다.</p><button id="confirmCancel" class="stop">이 주문 잔량 취소 요청</button> <button id="closeCancel">돌아가기</button>`;$('cancelReview').hidden=false;
    $('closeCancel').onclick=()=>{$('cancelReview').hidden=true;};
    $('confirmCancel').onclick=()=>action(async()=>{render(await api('/cancel',{order_id:o.id}));$('cancelReview').hidden=true;message('feedback','취소 처리 상태를 조회했습니다. 원 주문의 잔량 종료 여부를 확인하세요.');});
    $('cancelReview').scrollIntoView({behavior:'smooth',block:'center'});
  };
  $('searchButton').onclick=()=>action(async()=>{const q=$('search').value.trim();if(!q)throw new Error('찾을 종목명을 입력하세요.');const d=await api('/stocks?q='+encodeURIComponent(q));$('searchResults').innerHTML=d.stocks.length?d.stocks.map(s=>`<button type="button" data-stock="${esc(s.code)}" data-name="${esc(s.name)}">${esc(s.name)} · ${esc(s.code)}</button>`).join(''):'찾은 종목이 없습니다. 종목코드를 직접 입력할 수 있습니다.';});
  $('searchResults').onclick=e=>{const b=e.target.closest('[data-stock]');if(!b)return;$('code').value=b.dataset.stock;$('selectedStock').textContent=b.dataset.name+' · '+b.dataset.stock;clearReview();};
  $('search').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();$('searchButton').click();}};
  const today=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul'}).format(new Date());$('expiresOn').value=today;$('expiresOn').min=today;
  refresh();setInterval(refresh,3000);setInterval(()=>{if(lastStatusAt&&Date.now()-lastStatusAt>10000)unknown();},1000);
})();
