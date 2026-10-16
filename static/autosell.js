/* 계좌 읽기 및 매도 신호 검증 UI. 실제 주문 기능은 없다. */
(()=>{
  const $=s=>document.querySelector(s),modal=$('#sellModal'),status=$('#sellStatus');
  let polling=null,busy=false;
  const reasons={TAKE_PROFIT:'익절 +15%',STOP_LOSS:'손절 −10%',D2_CLOSE:'D+2 종가',D3_CLOSE:'D+3 종가 (이전 규칙)',OVERDUE_CLOSE:'보유기간 초과 종가'};
  const fmt=n=>n==null?'–':n.toLocaleString('ko-KR',{maximumFractionDigits:2});
  async function api(path='',body){
    const response=await fetch('/api/autosell'+path,{method:body===undefined?'GET':'POST',cache:'no-store',
      headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
    if(response.status===404)throw Error('새 기능을 불러오려면 테마 레이더 서버를 다시 실행하세요.');
    const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'요청을 확인해주세요');return data;
  }
  function render(data){
    status.textContent=(data.enabled?'검증 중':'검증 중지')+' · 실제 주문 미전송'+(data.error?' · '+data.error:'')+(data.calendar_error?' · '+data.calendar_error:'');
    $('#sellStart').disabled=busy||data.enabled;$('#sellStop').disabled=busy||!data.enabled;
    const body=$('#sellRows');body.replaceChildren();
    for(const p of data.positions){
      const tr=document.createElement('tr');
      for(const text of [p.name+' ('+p.code+') / '+fmt(p.quantity)+'주',fmt(p.average)+' / 익절 '+fmt(p.take_profit)+' · 손절 '+fmt(p.stop_loss),p.bought_on||'매수일 미확인',p.days_held==null?'거래일 확인 대기':'D+'+p.days_held,p.signal?(reasons[p.signal.reason]||p.signal.reason)+' 신호 · 미전송':'조건 대기']){
        const td=document.createElement('td');td.textContent=text;td.style.padding='10px 5px';tr.append(td);
      }
      const button=document.createElement('button');button.className='btn';button.textContent='매수일 확인';
      button.addEventListener('click',async()=>{const value=prompt(p.name+'의 실제 최초 매수일 (YYYY-MM-DD)',p.bought_on||'');if(!value)return;
        try{render(await api('/buy-date',{code:p.code,bought_on:value}))}catch(e){status.textContent=e.message}});
      tr.children[2].append(document.createElement('br'),button);body.append(tr);
    }
    if(!data.positions.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=5;td.textContent='계좌 연결 후 현금 주식 보유분을 표시합니다.';tr.append(td);body.append(tr)}
  }
  async function load(){if(busy)return;try{render(await api())}catch(e){status.textContent=e.message}}
  $('#sellBtn').addEventListener('click',()=>{modal.hidden=false;load();clearInterval(polling);polling=setInterval(load,3000)});
  function close(){modal.hidden=true;clearInterval(polling)}
  $('#sellClose').addEventListener('click',close);
  modal.addEventListener('click',e=>{if(e.target===modal)close()});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')close()});
  for(const [id,path] of [['#sellStart','/start'],['#sellStop','/stop']])$(id).addEventListener('click',async()=>{
    if(busy)return;busy=true;$('#sellStart').disabled=$('#sellStop').disabled=true;status.textContent='계좌 연결 상태 확인 중…';
    try{const data=await api(path,{});busy=false;render(data)}catch(e){busy=false;status.textContent=e.message;$('#sellStart').disabled=$('#sellStop').disabled=false}
  });
})();
