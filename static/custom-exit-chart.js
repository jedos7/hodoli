(() => {
  'use strict';
  const $=id=>document.getElementById(id), core=window.ExitChartCore;
  const panel=$('exitChartPanel'), body=$('exitChartBody'), canvas=$('exitChartCanvas'), dialog=$('exitChartDialog');
  if(!panel||!core)return;
  const ctx=canvas.getContext('2d'), num=v=>Number(v).toLocaleString('ko-KR',{maximumFractionDigits:2});
  const states={watching:'감시 중',armed:'감시 대기',quote_wait:'시세 대기',session_wait:'장 상태 대기',
    connection_wait:'연결 확인 필요',prepared:'시작 전',paused:'감시 중지',completed:'체결 완료',
    checking:'매도 준비 중',ordered:'체결 확인 중',remainder_check:'잔고 확인 중'};
  let mode='custom',customContext=null,horizonContext=null;
  let context=null, plans=[], interval='1m', series=[], result=null, controller=null, sequence=0;
  let count=100, offset=0, hover=null, drag=null, loading=false, failure=false, geometry=null;
  function stamp(s){return s?`${s.slice(4,6)}/${s.slice(6,8)}${s.length>8?' '+s.slice(8,10)+':'+s.slice(10,12):''}`:'—';}
  function fetchTime(s){return s?new Date(s).toLocaleTimeString('ko-KR',{hour12:false,timeZone:'Asia/Seoul'}):'—';}
  function message(text){$('exitChartMessage').textContent=text;$('exitChartMessage').hidden=!text;}
  function metrics(){
    $('exitChartTitle').textContent=context?`${context.name} · ${context.code}`:'종목 차트';
    $('exitChartDialogTitle').textContent=context?`${context.name} · ${context.code} · 크게 보기`:'종목 차트 · 크게 보기';
    $('exitChartContext').textContent=!context?'잔고에서 종목을 선택하거나, 저장된 조건의 차트 보기를 누르세요.':
      `${context.method==='horizon'?'호라이즌':'직접 설정'} · ${num(context.quantity)}주 · `+(context.source==='draft'?'입력값 미리보기 · 실제 감시 조건은 등록한 조건 목록에서 확인':
      `저장된 조건 · ${states[context.status]||'감시 상태 확인 필요'}`);
    $('exitChartDialogContext').textContent=$('exitChartContext').textContent;
    const latest=series.at(-1), levels=core.lines(context);
    $('exitChartMetrics').replaceChildren();
    const items=[{label:'최근 봉 종가',value:latest?num(latest.close)+'원':'—',color:latest?(latest.close>=latest.open?'#ff7878':'#70aaff'):'#9aacc4'},
      ...levels.map(l=>({label:l.label,value:num(l.price)+'원',color:l.color}))];
    for(const item of items){const box=document.createElement('div'),label=document.createElement('span'),value=document.createElement('strong');
      label.textContent=item.label;value.textContent=item.value;value.style.color=item.color;box.append(label,value);$('exitChartMetrics').append(box);}
    $('exitChartSource').textContent=result?`${result.source} · 수신 ${fetchTime(result.fetched_at)} · 마지막 봉 ${stamp(result.last_bar_at)}`:'실제 시세를 조회하면 수신 시각이 표시됩니다.';
    $('exitChartSource').classList.toggle('chart-stale',failure);
    $('exitChartRefresh').disabled=!context||loading;
    $('exitChartPending').textContent=loading?'조회 중':failure?'갱신 실패 · 이전 자료':context?'20초마다 조회':'종목 선택 대기';
    $('exitChartPending').classList.toggle('chart-stale',failure);
    $('exitChartRange').textContent=series.length?`${Math.min(count,series.length)} / ${series.length}봉${offset?' · 과거 구간':''}`:'—';
  }
  function selection(next){
    const changed=next?.code!==context?.code;
    context=next;
    if(changed){sequence++;controller?.abort();series=[];result=null;offset=0;hover=null;failure=false;loading=false;}
    metrics();draw();
    if(changed&&context)load();
    if(!context)message('잔고 종목을 선택하면 캔들·거래량·매도 기준선이 표시됩니다.');
  }
  function customSelection(value){customContext=value;if(mode==='custom')selection(value);}
  window.ExitChart={
    setMode(value){mode=value;selection(mode==='horizon'?horizonContext:customContext);},
    setHorizon(value){horizonContext=value;if(mode==='horizon')selection(value);},
    setDraft(value){if(value)customSelection(core.fromDraft(value.position,value.inputs));
      else if(customContext?.source==='draft')customSelection(plans.length?core.fromPlan(plans[0]):null);},
    setTicketDraft(value){if(value)customSelection({...value.position,rules:[value.rule],source:'draft'});
      else if(customContext?.source==='draft')customSelection(plans.length?core.fromPlan(plans[0]):null);},
    setPlans(value){plans=value||[];if(!customContext&&plans.length)customSelection(core.fromPlan(plans[0]));
      else if(customContext?.source==='plan'){const p=plans.find(p=>p.code===customContext.code);customSelection(p?core.fromPlan(p):null);}},
    showPlan(code){const p=plans.find(p=>p.code===code);if(!p)return;window.ExitWorkspace?.setMode('custom');customSelection(core.fromPlan(p));panel.scrollIntoView({behavior:'smooth',block:'start'});}
  };
  async function load(){
    if(!context||loading)return;
    controller?.abort();controller=new AbortController();
    const request=++sequence, code=context.code, frame=interval;
    loading=true;failure=false;metrics();if(!series.length)message('키움 캔들 자료를 불러오는 중입니다…');
    const requestController=controller, timeout=setTimeout(()=>requestController.abort(),16000);
    try{
      const response=await fetch(`/api/price-chart/${encodeURIComponent(code)}?interval=${frame}&limit=600`,
        {cache:'no-store',signal:controller.signal});
      const data=await response.json();
      if(request!==sequence)return;
      if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'차트 조회 실패');
      if(data.code!==code||data.interval!==frame||!Array.isArray(data.bars))throw Error('차트 응답 종목·주기를 확인할 수 없습니다');
      const next=core.bars(data.bars);
      if(series.length&&offset){const previousLast=series.at(-1).stamp;offset+=next.filter(b=>b.stamp>previousLast).length;}
      series=next;result=data;hover=null;offset=Math.min(offset,Math.max(0,series.length-20));
      message(series.length?'':'이 종목의 해당 주기 자료가 없습니다.');
    }catch(e){if(request!==sequence)return;failure=true;message(`${e.name==='AbortError'?'차트 조회 시간 초과':e.message}${series.length?' · 이전 수신 자료입니다.':' · 새로고침으로 다시 조회하세요.'}`);}
    finally{clearTimeout(timeout);if(request===sequence){loading=false;metrics();draw();}}
  }
  function text(value,x,y,color='#8fa3bd',align='left',font='11px system-ui'){
    ctx.fillStyle=color;ctx.textAlign=align;ctx.font=font;ctx.fillText(value,x,y);
  }
  function line(x1,y1,x2,y2,color,dash=[]){ctx.beginPath();ctx.strokeStyle=color;ctx.lineWidth=1;ctx.setLineDash(dash);ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();ctx.setLineDash([]);}
  function draw(){
    const rect=canvas.getBoundingClientRect(), width=rect.width, height=rect.height;
    if(!width||!height||!ctx)return;
    const ratio=Math.min(window.devicePixelRatio||1,2);
    canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);ctx.setTransform(ratio,0,0,ratio,0,0);
    ctx.fillStyle='#101722';ctx.fillRect(0,0,width,height);geometry=null;
    if(!series.length){$('exitChartOHLC').textContent='캔들 위에 커서를 올리면 시가·고가·저가·종가·거래량을 볼 수 있습니다.';return;}
    const range=core.windowRange(series.length,count,offset), visible=series.slice(range.start,range.end);
    const levels=$('exitChartLines').checked?core.lines(context):[];
    const bounds=core.extent(visible,levels), left=10,right=width-82,top=24,bottom=height-112,volTop=height-84,volBottom=height-28;
    const pitch=(right-left)/visible.length, x=i=>left+(i+.5)*pitch;
    const y=value=>top+(bounds.high-value)/(bounds.high-bounds.low)*(bottom-top);
    geometry={visible,range,left,right,top,bottom,pitch};
    for(let n=0;n<=5;n++){const py=top+(bottom-top)*n/5;
      line(left,py,right,py,'#253142');text(num(bounds.high-(bounds.high-bounds.low)*n/5),right+7,py+4);}
    const maxVolume=Math.max(...visible.map(b=>b.volume),1);
    const step=Math.max(1,Math.floor(visible.length/(width<500?3:7)));
    visible.forEach((b,i)=>{
      const center=x(i), color=b.close>=b.open?'#ff716f':'#679efd', bodyWidth=Math.max(1,Math.min(12,pitch*.7));
      line(center,y(b.high),center,y(b.low),color);
      ctx.fillStyle=color;ctx.fillRect(center-bodyWidth/2,Math.min(y(b.open),y(b.close)),bodyWidth,Math.max(1,Math.abs(y(b.open)-y(b.close))));
      ctx.globalAlpha=.5;ctx.fillRect(center-bodyWidth/2,volBottom-b.volume/maxVolume*(volBottom-volTop),bodyWidth,b.volume/maxVolume*(volBottom-volTop));ctx.globalAlpha=1;
      if(i%step===0){line(center,top,center,volBottom,'#1d2939',[2,5]);text(stamp(b.stamp),Math.max(30,Math.min(right-25,center)),height-8,'#8196b2','center','10px system-ui');}
    });
    text('거래량',left,volTop-9);text(num(maxVolume),right+7,volTop+8,'#8196b2','left','10px system-ui');
    line(left,bottom+13,right,bottom+13,'#2c394c');
    // Spread label boxes without changing the actual horizontal line position.
    let labelBottom=-100;
    for(const l of [...levels].sort((a,b)=>b.price-a.price)){
      const py=y(l.price), ly=Math.min(bottom-4,Math.max(top+7,py,labelBottom+21));labelBottom=ly;
      line(left,py,right,py,l.color,context?.source==='draft'?[3,5]:l.kind==='protection'?[]:[6,4]);
      text(l.label,left+5,py-5,l.color,'left','600 10px system-ui');
      ctx.fillStyle=l.color;ctx.fillRect(right+1,ly-11,79,18);text(num(l.price),right+74,ly+2,'#0e1721','right','600 11px system-ui');
    }
    const last=visible.at(-1), py=y(last.close);
    line(left,py,right,py,last.close>=last.open?'#ff716f':'#679efd',[3,4]);
    const index=hover===null?visible.length-1:Math.max(0,Math.min(visible.length-1,hover)), b=visible[index];
    $('exitChartOHLC').textContent=`${stamp(b.stamp)} · 시 ${num(b.open)} · 고 ${num(b.high)} · 저 ${num(b.low)} · 종 ${num(b.close)} · 거래량 ${num(b.volume)}주`;
    if(hover!==null){line(x(index),top,x(index),volBottom,'#b5c5dc',[3,3]);
      ctx.beginPath();ctx.arc(x(index),y(b.close),3,0,2*Math.PI);ctx.fillStyle='#edf3ff';ctx.fill();}
  }
  document.querySelectorAll('[data-chart-interval]').forEach(button=>button.addEventListener('click',()=>{
    if(interval===button.dataset.chartInterval)return;interval=button.dataset.chartInterval;
    document.querySelectorAll('[data-chart-interval]').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',String(b===button));});
    sequence++;controller?.abort();loading=false;series=[];result=null;offset=0;hover=null;metrics();draw();load();
  }));
  $('exitChartRefresh').addEventListener('click',load);
  $('exitChartLines').addEventListener('change',draw);
  function zoom(factor){count=Math.min(300,Math.max(20,Math.round(count*factor)));hover=null;metrics();draw();}
  $('exitChartZoomIn').addEventListener('click',()=>zoom(.75));$('exitChartZoomOut').addEventListener('click',()=>zoom(1.35));
  $('exitChartLatest').addEventListener('click',()=>{offset=0;count=100;hover=null;metrics();draw();});
  canvas.addEventListener('wheel',e=>{if(!e.ctrlKey)return;e.preventDefault();zoom(e.deltaY>0?1.2:.8);},{passive:false});
  canvas.addEventListener('pointerdown',e=>{if(!geometry)return;drag={x:e.clientX,offset};canvas.setPointerCapture(e.pointerId);});
  canvas.addEventListener('pointermove',e=>{if(!geometry)return;const px=e.clientX-canvas.getBoundingClientRect().left;
    if(drag){offset=Math.max(0,Math.min(series.length-20,Math.round(drag.offset+(e.clientX-drag.x)/geometry.pitch)));hover=null;metrics();}
    else hover=Math.floor((px-geometry.left)/geometry.pitch);
    draw();});
  const release=()=>{drag=null;};canvas.addEventListener('pointerup',release);canvas.addEventListener('pointercancel',release);
  canvas.addEventListener('pointerleave',()=>{if(!drag){hover=null;draw();}});
  canvas.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();offset=Math.max(0,Math.min(series.length-20,offset+(e.key==='ArrowLeft'?10:-10)));metrics();draw();}});
  $('exitChartExpand').addEventListener('click',()=>{dialog.showModal();$('exitChartDialogSlot').append(body);draw();});
  $('exitChartClose').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{$('exitChartSlot').append(body);draw();});
  new ResizeObserver(draw).observe(canvas);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
  setInterval(()=>{if(!document.hidden)load();},20000);
  metrics();draw();message('잔고 종목을 선택하면 캔들·거래량·매도 기준선이 표시됩니다.');
})();
