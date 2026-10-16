/* Shared order feedback and explicit cancel review. Never auto-submits cancellation. */
(()=>{
'use strict';
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>Number(v).toLocaleString('ko-KR'),sources={horizon:{path:'/api/autoexit',header:'X-Autoexit-Token',name:'호라이즌'},custom:{path:'/api/custom-exit',header:'X-Custom-Exit-Token',name:'직접 설정'}};
const states={},seen={},ready=new Set();let reviews=null,busy=false;
const err=msg=>{$('cancelError').textContent=msg;$('cancelError').hidden=!msg;};
function notify(text){const row=document.createElement('div');row.className='order-toast';row.textContent=text;const close=document.createElement('button');close.type='button';close.textContent='닫기';close.addEventListener('click',()=>row.remove());row.append(close);$('orderNotifications').prepend(row);while($('orderNotifications').children.length>5)$('orderNotifications').lastElementChild.remove();}
function update(source,s){
 if(!sources[source]||!s)return;
 states[source]={...s,received:Date.now()};
 for(const o of s.orders||[]){
  const key=source+':'+o.id,old=seen[key],filled=Number(o.filled)||0;
  if(ready.has(source)&&((old&&filled>old.filled)||(!old&&filled>0)))notify(`${sources[source].name} · ${o.name} · ${o.status==='filled'?'전량 체결':'체결 확인'} ${num(filled)}주 / ${num(o.quantity)}주 · 미체결 ${o.remaining==null?'확인 중':num(o.remaining)+'주'}`);
  if(ready.has(source)&&o.manual_cancel_state==='confirmed'&&old?.cancel!=='confirmed')notify(`${sources[source].name} · ${o.name} · 잔량 ${num(o.manual_cancel_quantity)}주 취소 완료 확인`);
  seen[key]={filled,cancel:o.manual_cancel_state};
 }
 ready.add(source);
}
window.ExitOrderTools={update};
async function api(source,path,body){
 const conf=sources[source],state=states[source];
 if(!state?.token||Date.now()-state.received>10000)throw Error(conf.name+' 상태를 다시 확인하세요');
 const r=await fetch(conf.path+path,{method:'POST',headers:{'Content-Type':'application/json',[conf.header]:state.token},body:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
 const s=await r.json();if(!r.ok)throw Error(typeof s.detail==='string'?s.detail:'취소 결과 확인 필요');return s;
}
function close(){reviews=null;$('cancelReviewDialog').close();}
$('cancelReviewClose').addEventListener('click',close);
$('cancelReviewDialog').addEventListener('cancel',()=>{reviews=null;});
$('cancelAck').addEventListener('change',()=>{$('cancelConfirm').disabled=busy||!reviews||!$('cancelAck').checked;});
$('reviewSellCancel').addEventListener('click',async()=>{
 if(busy)return;busy=true;reviews=null;err('');$('cancelAck').checked=false;$('cancelAck').disabled=true;$('cancelConfirm').disabled=true;
 $('cancelTargets').textContent='증권사 원주문·미체결 수량 조회 중…';$('cancelReviewDialog').showModal();
 try{
  const result=await Promise.allSettled(Object.keys(sources).map(async source=>({source,data:await api(source,'/cancel/prepare',{})})));
  const success=result.filter(r=>r.status==='fulfilled').map(r=>r.value),failures=result.filter(r=>r.status==='rejected').map(r=>r.reason.message);
  if(!$('cancelReviewDialog').open)return;
  reviews=success.filter(r=>r.data.items.length).map(r=>({...r,received:Date.now()}));
  $('cancelTargets').innerHTML=success.map(({source,data})=>`<h3>${esc(sources[source].name)} · ${data.environment==='real'?'실계좌':'모의계좌'}</h3>${data.items.length?data.items.map(i=>`<p>${esc(i.name)} · ${esc(i.venue)} · 주문 ${esc(i.order_no)}<br><b>미체결 ${num(i.remaining)}주 취소</b> · 체결 ${num(i.filled)} / 주문 ${num(i.quantity)}주</p>`).join(''):'<p>취소 가능한 미체결 매도 없음</p>'}${data.excluded.map(i=>`<p class="cancel-excluded">제외: ${esc(i.name)} · ${esc(i.note)}</p>`).join('')}`).join('');
  if(failures.length)err(failures.join(' · '));
  if(!reviews.length)reviews=null;
 }catch(e){err(e.message);}finally{busy=false;$('cancelAck').checked=false;$('cancelAck').disabled=!reviews;$('cancelConfirm').disabled=true;}
});
$('cancelConfirm').addEventListener('click',async()=>{
 if(busy||!reviews||!$('cancelAck').checked)return;
 const selected=reviews;reviews=null;busy=true;$('cancelConfirm').disabled=true;err('');
 try{
  if(selected.some(r=>Date.now()-r.received>=60000))throw Error('확인 시간이 지났습니다. 닫고 다시 조회하세요.');
  const messages=[];
  for(const {source,data} of selected){
   try{const r=await api(source,'/cancel/confirm',{review_id:data.review_id,environment:data.environment,confirm:true});
    messages.push(sources[source].name+': '+r.results.map(x=>x.note).join(' · '));
   }catch(e){messages.push(sources[source].name+': '+e.message);}
  }
  $('cancelTargets').textContent=messages.join('\n');
  err('취소 접수는 완료가 아닙니다. 아래 주문내역에서 증권사 취소 완료를 확인하세요. 해당 종목의 감시·재주문은 다시 켜지 않습니다.');
 }catch(e){err(e.message);}finally{busy=false;}
});
setInterval(()=>{if(reviews&&reviews.some(r=>Date.now()-r.received>=60000)){reviews=null;$('cancelConfirm').disabled=true;err('확인 시간이 지났습니다. 닫고 다시 조회하세요.');}},1000);
})();
