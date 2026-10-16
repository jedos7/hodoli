(() => {
  'use strict';
  const C=window.PullbackCore,$=id=>document.getElementById(id),KEY='pullback-lab-v1';
  const fmt=n=>Number(n).toLocaleString('ko-KR',{maximumFractionDigits:0});
  const labels={budget:'가상 투자금 (원)',parts:'분할 횟수 (회)',gap:'추가매수 간격 (%)',tp1:'1차 익절 (%)',tp2:'2차 익절 (%)',trail:'잔량 고점 하락 (%)',stop:'평단 대비 손실 한도 (%)',pullMin:'고점 대비 눌림 최소 (%)',pullMax:'고점 대비 눌림 최대 (%)'};
  let selected=C.STOCKS[0][0],states={},data={},busy=false,owner=false,storageFailed=false;
  const err=text=>{$('error').textContent=text;$('error').hidden=!text;};
  function load(){
    const raw=localStorage.getItem(KEY),saved=raw?JSON.parse(raw):null;
    if(saved&&saved.version!==1)throw Error('저장된 가상매매 기록의 버전을 확인하세요.');
    for(const [code] of C.STOCKS){const s=saved?.states?.[code]||C.create(code);C.validate(s.config);
      if(s.code!==code||!Array.isArray(s.trades)||!Number.isFinite(s.cash)||s.cash<-.01||!Number.isInteger(s.quantity)||s.quantity<0||!Number.isFinite(s.cost)||s.cost<-.01)throw Error('저장된 가상매매 기록을 확인하세요. 원본은 덮어쓰지 않았습니다.');
      C.pause(s,'화면을 다시 열었습니다 · 설정 확인 후 시작');states[code]=s;}
  }
  function save(){
    if(!owner||storageFailed)return false;
    try{localStorage.setItem(KEY,JSON.stringify({version:1,states}));return true;}
    catch(e){storageFailed=true;for(const s of Object.values(states))C.pause(s,'기록 저장 실패 · 가상매매 중지');err('브라우저에 기록을 저장할 수 없습니다. 기록 저장 버튼으로 내려받은 뒤 저장 공간을 확인하세요.');return false;}
  }
  function price(code){const d=data[code];return d?.minute?.bars?.at(-1)?.close||d?.daily?.bars?.at(-1)?.close||states[code]?.lastPrice||0;}
  function html(el,text,tag='span',cls=''){const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;el.append(n);return n;}
  function render(){
    $('cards').replaceChildren();
    for(const [code,name] of C.STOCKS){const s=states[code],d=data[code],p=price(code),card=html($('cards'),'','button','stock'+(code===selected?' selected':''));card.type='button';card.onclick=()=>{selected=code;fill();render();};card.setAttribute('aria-pressed',code===selected);
      html(card,name,'b');html(card,code+(s.running?' · 가상매매 진행 중':' · 중지'),'small');html(card,p?fmt(p)+'원':'—','div','price');html(card,d?.failure?'갱신 실패 · 이전 가격':d?.minute?.last_bar_at?'1분봉 '+displayStamp(d.minute.last_bar_at):d?.daily?.last_bar_at?'일봉 '+displayStamp(d.daily.last_bar_at):'시세 수신 대기','small');
      const sig=C.signal(d?.daily?.bars,p,Date.now(),s.config);const fresh=d?.minute?.last_bar_at&&Date.now()-C.stampMs(d.minute.last_bar_at)<150000;html(card,d?.failure?'시세 확인 필요':(fresh?'':'최근 봉 참고 · ')+sig.label,'div','signal');html(card,s.note,'div','state');const total=p?C.equity(s,p)-s.config.budget:s.realized;html(card,'가상손익 '+(total>=0?'+':'')+fmt(total)+'원','div','pnl '+(total>=0?'up':'down'));}
    const s=states[selected],p=price(selected),avg=s.quantity?s.cost/s.quantity:0;
    $('mode').textContent=s.running?'가상매매 진행 중':'중지';$('selectedName').textContent=C.STOCKS.find(x=>x[0]===selected)[1];
    $('settings').querySelectorAll('input,button').forEach(x=>x.disabled=!owner||storageFailed||s.running||s.quantity>0);
    $('start').disabled=!owner||storageFailed||s.running;$('pause').disabled=!owner||!s.running;$('pauseAll').disabled=!owner;$('leveraged').hidden=selected!=='233740';
    $('plan').textContent=(!owner?'다른 창이 가상매매를 관리하고 있습니다. 이 창은 조회 전용입니다. ':s.quantity?'보유 중에는 설정을 바꿀 수 없습니다. 중지는 보유분을 매도하지 않습니다. ':'')+`회차당 ${fmt(s.config.budget/s.config.parts)}원${p?' · 최근 가격 기준 '+Math.floor(s.config.budget/s.config.parts/p)+'주 가능':''} · 현재 ${s.round}/${s.config.parts}회 매수`+(s.quantity?` · 평단 ${fmt(avg)}원 · 다음 ${s.stage===2?'추적매도 '+fmt(s.high*(1-s.config.trail/100)):s.stage===1?'2차 익절 '+fmt(avg*(1+s.config.tp2/100)):'추가매수 '+fmt(s.lastBuy*(1-s.config.gap/100))}원`:'');
    $('metrics').replaceChildren();
    const items=[['가상 보유',fmt(s.quantity)+'주'],['남은 가상 현금',fmt(s.cash)+'원'],['실현손익',fmt(s.realized)+'원'],['평가손익',fmt(s.quantity*p-s.cost)+'원'],['관측 최대 낙폭',s.maxDrawdown.toFixed(2)+'%'],['완료 / 수익 회차',`${s.cycles} / ${s.wins}`]];
    for(const [label,value] of items){const div=html($('metrics'),'','div');html(div,label,'small');html(div,value,'b');}
    $('trades').replaceChildren();for(const t of s.trades.slice(-100).reverse()){const row=html($('trades'),'','tr');[displayStamp(t.stamp),t.side==='buy'?'가상 매수':'가상 매도',fmt(t.price)+'원',fmt(t.quantity)+'주',t.reason,t.side==='sell'?fmt(t.profit)+'원':'—'].forEach((v,i)=>html(row,v,'td',i===1?(t.side==='buy'?'up':'down'):''));}
    if(!s.trades.length){const row=html($('trades'),'','tr');html(row,'아직 가상 체결이 없습니다. 시작 후 새 시세가 조건에 도달하면 기록됩니다.','td').colSpan=6;}
    draw();
  }
  function displayStamp(s){return s?`${s.slice(0,4)}.${s.slice(4,6)}.${s.slice(6,8)}${s.length>8?' '+s.slice(8,10)+':'+s.slice(10,12):''}`:'—';}
  function fill(){const s=states[selected];$('fields').replaceChildren();for(const [k,label] of Object.entries(labels)){const l=html($('fields'),label,'label'),input=html(l,'','input');input.type='number';input.id='setting-'+k;input.name=k;input.value=s.config[k];input.required=true;input.min=k==='budget'?10000:k==='parts'?2:.1;input.max=k==='budget'?1e10:k==='parts'?40:49.9;input.step=['budget','parts'].includes(k)?1:.1;}}
  async function get(code,interval){const r=await fetch(`/api/price-chart/${code}?interval=${interval}&limit=160`,{cache:'no-store',signal:AbortSignal.timeout(16000)});if(!r.ok)throw Error('키움 시세 연결을 확인하세요.');const d=await r.json();if(d.code!==code||d.interval!==interval||!Array.isArray(d.bars))throw Error('시세 응답이 올바르지 않습니다.');return d;}
  async function refresh(){
    if(busy)return;busy=true;$('refresh').disabled=true;
    let failures=0;
    // Sequence requests to respect the existing broker quote-rate limiter.
    for(const [code] of C.STOCKS){try{const d=data[code]||(data[code]={});if(!d.daily||Date.now()-(d.dailyAt||0)>300000){d.daily=await get(code,'1d');d.dailyAt=Date.now();}
        d.minute=await get(code,'1m');d.failure=false;
        if(owner&&!storageFailed){C.step(states[code],{bars:d.minute.bars,daily:d.daily.bars,now:Date.now()});save();}
      }catch(e){failures++;(data[code]||(data[code]={})).failure=true;if(owner&&states[code].running){C.pause(states[code],'시세 조회 실패 · 확인 후 다시 시작');save();}}
    }
    $('connection').textContent=failures?`${failures}종목 시세 조회 실패 · 체결 계산 중지`:'키움 KRX · 조회 '+new Date().toLocaleTimeString('ko-KR',{hour12:false});busy=false;$('refresh').disabled=false;render();
  }
  function draw(){
    const d=data[selected],bars=d?.daily?.bars||[],canvas=$('chart'),box=canvas.getBoundingClientRect(),w=box.width,h=box.height,dpr=window.devicePixelRatio||1;
    canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
    $('chartTitle').textContent=C.STOCKS.find(x=>x[0]===selected)[1]+' · 일봉';$('chartSource').textContent=d?.daily?'KRX · '+displayStamp(d.daily.last_bar_at):'시세 대기';
    const g=C.signal(bars,price(selected),Date.now(),states[selected].config);$('chartInfo').textContent=d?.failure?'조회 실패 · 차트는 이전 자료입니다.':g.ready?`최근 수신 가격 기준 · ${g.label} · 20일선 ${fmt(g.ma20)}원 · 고점 대비 ${g.pull.toFixed(2)}% 조정`:g.label;
    if(bars.length<2){ctx.fillStyle='#9aaac0';ctx.font='14px sans-serif';ctx.fillText('실제 일봉 자료를 기다리고 있습니다.',20,h/2);return;}
    const show=bars.slice(-80),ma=(n,i)=>i+1<n?null:bars.slice(i-n+1,i+1).reduce((a,b)=>a+b.close,0)/n;
    const values=show.flatMap(b=>[b.high,b.low]);for(let i=bars.length-show.length;i<bars.length;i++)for(const n of [20,60]){const v=ma(n,i);if(v)values.push(v);}
    let lo=Math.min(...values),hi=Math.max(...values),pad=(hi-lo)*.08||1;lo-=pad;hi+=pad;const left=8,right=w-75,top=15,bottom=h-30,x=i=>left+(i+.5)*(right-left)/show.length,y=p=>bottom-(p-lo)/(hi-lo)*(bottom-top);
    ctx.font='11px sans-serif';for(let i=0;i<=4;i++){const p=lo+(hi-lo)*i/4;ctx.strokeStyle='#263447';ctx.beginPath();ctx.moveTo(left,y(p));ctx.lineTo(right,y(p));ctx.stroke();ctx.fillStyle='#9aaac0';ctx.fillText(fmt(p),right+8,y(p)+4);}
    show.forEach((b,i)=>{ctx.strokeStyle=ctx.fillStyle=b.close>=b.open?'#ff777b':'#75a7ff';ctx.beginPath();ctx.moveTo(x(i),y(b.high));ctx.lineTo(x(i),y(b.low));ctx.stroke();const bw=Math.max(2,(right-left)/show.length*.65);ctx.fillRect(x(i)-bw/2,Math.min(y(b.open),y(b.close)),bw,Math.max(1,Math.abs(y(b.open)-y(b.close))));});
    for(const [n,color] of [[20,'#ffc765'],[60,'#c49cff']]){ctx.strokeStyle=color;ctx.lineWidth=1.5;ctx.beginPath();let began=false;show.forEach((b,i)=>{const v=ma(n,bars.length-show.length+i);if(v){if(!began)ctx.moveTo(x(i),y(v));else ctx.lineTo(x(i),y(v));began=true;}});ctx.stroke();}ctx.lineWidth=1;ctx.fillStyle='#9aaac0';ctx.fillText(displayStamp(show[0].stamp),left,h-8);ctx.fillText(displayStamp(show.at(-1).stamp),Math.max(left,right-90),h-8);
  }
  $('settings').onsubmit=e=>{e.preventDefault();if(!owner||storageFailed)return;const s=states[selected];if(s.running||s.quantity)return;try{const c=C.validate(Object.fromEntries(Object.keys(labels).map(k=>[k,Number($('setting-'+k).value)])));if(s.trades.length&&c.budget!==s.config.budget)throw Error('기록이 있는 종목의 초기 투자금은 변경할 수 없습니다. 다른 매매 비율은 중지·전량 매도 상태에서 변경할 수 있습니다.');s.config=c;if(!s.trades.length)s.cash=s.peak=c.budget;s.note='설정 저장 완료 · 시작 버튼으로 검증';save();err('');render();}catch(e){err(e.message);}};
  $('start').onclick=()=>{if(!owner||storageFailed)return;const s=states[selected];if(Object.keys(labels).some(k=>Number($('setting-'+k).value)!==s.config[k])){err('변경한 설정을 먼저 저장하세요.');return;}C.start(s,Date.now());save();render();};
  $('pause').onclick=()=>{if(owner){C.pause(states[selected]);save();render();}};$('pauseAll').onclick=()=>{if(owner){Object.values(states).forEach(s=>C.pause(s));save();render();}};
  $('refresh').onclick=refresh;$('export').onclick=()=>{const blob=new Blob([JSON.stringify({version:1,exported_at:new Date().toISOString(),description:'비용 제외 가상매매 · 실제 주문 아님',states},null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='4종목-가상매매-'+new Date().toISOString().slice(0,10)+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  window.addEventListener('resize',draw);
  async function init(){try{load();fill();render();if(location.hash==='#sell'){location.replace('/account-setup.html#sell');return;}
      if(!navigator.locks){err('이 브라우저는 중복 실행 방지를 지원하지 않습니다. 최신 Chrome 또는 Edge에서 열어주세요.');await refresh();return;}
      navigator.locks.request(KEY,{ifAvailable:true},async lock=>{if(!lock){err('다른 창에서 가상매매를 관리 중입니다. 그 창을 사용하거나 닫은 뒤 이 창을 새로고침하세요.');return;}load();owner=true;save();fill();render();await new Promise(resolve=>window.addEventListener('pagehide',resolve,{once:true}));owner=false;});
      await refresh();setInterval(refresh,20000);
      window.addEventListener('storage',e=>{if(e.key===KEY&&!owner){try{load();render();}catch(e){err(e.message);}}});
    }catch(e){storageFailed=true;err(e.message);$('start').disabled=true;$('settings').querySelectorAll('input,button').forEach(x=>x.disabled=true);}}
  init();
})();
