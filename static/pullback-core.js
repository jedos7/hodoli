(function(root){
  'use strict';
  const STOCKS=[['000660','SK하이닉스'],['009150','삼성전기'],['005380','현대차'],['233740','KODEX 코스닥150 레버리지']];
  const defaults={budget:1000000,parts:5,gap:2,tp1:3,tp2:6,trail:3,stop:8,pullMin:2,pullMax:8};
  function validate(c){
    for(const k of Object.keys(defaults))if(!Number.isFinite(c[k]))throw Error('모든 설정을 숫자로 입력하세요.');
    if(!Number.isInteger(c.budget)||c.budget<10000||c.budget>1e10)throw Error('가상 투자금은 1만~100억 원 사이의 정수로 입력하세요.');
    if(!Number.isInteger(c.parts)||c.parts<2||c.parts>40)throw Error('분할 횟수는 2~40회입니다.');
    for(const k of ['gap','tp1','tp2','trail','stop','pullMin','pullMax'])if(c[k]<=0||c[k]>=50)throw Error('비율은 0 초과 50 미만이어야 합니다.');
    if(c.tp2<=c.tp1||c.pullMax<=c.pullMin)throw Error('2차 익절률과 눌림 상한은 각각 앞의 값보다 커야 합니다.');
    return {...c};
  }
  function create(code,c={...defaults}){return {code,config:validate(c),running:false,cash:c.budget,quantity:0,cost:0,realized:0,round:0,stage:0,baseQuantity:0,lastBuy:0,high:0,peak:c.budget,maxDrawdown:0,trades:[],cycles:0,wins:0,cycleProfit:0,lastStamp:'',after:0,exitDay:'',resetSeen:true,note:'가상매매 시작 전'};}
  function stampMs(s){if(!/^\d{14}$/.test(s||''))return NaN;return Date.parse(`${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}T${s.slice(8,10)}:${s.slice(10,12)}:${s.slice(12,14)}+09:00`);}
  function day(at){return new Date(at+9*3600000).toISOString().slice(0,10).replaceAll('-','');}
  function signal(daily,price,at,c){
    const seen=new Map();
    for(const b of daily||[])if(/^\d{8}$/.test(b.stamp)&&b.stamp<day(at)&&['close','high','low','open'].every(k=>Number.isFinite(b[k])&&b[k]>0))seen.set(b.stamp,b);
    const rows=[...seen.values()].sort((a,b)=>a.stamp.localeCompare(b.stamp));
    if(rows.length<61)return {ready:false,entry:false,broken:false,label:'일봉 61개 이상 필요'};
    const avg=(n,end=rows.length)=>rows.slice(end-n,end).reduce((s,b)=>s+b.close,0)/n;
    const ma20=avg(20),ma60=avg(60),prev20=avg(20,rows.length-1),high=Math.max(...rows.slice(-20).map(b=>b.high));
    const pull=(high-price)/high*100,trend=ma20>ma60&&ma20>prev20&&price>=ma20;
    const entry=trend&&pull>=c.pullMin&&pull<=c.pullMax;
    const age=(at-Date.parse(rows.at(-1).stamp.replace(/(\d{4})(\d{2})(\d{2})/,'$1-$2-$3')+'T00:00:00+09:00'))/86400000;
    return {ready:age<=10,entry:age<=10&&entry,broken:price<ma60,ma20,ma60,pull,high,label:age>10?'일봉 갱신 필요':entry?'눌림목 진입 조건 충족':trend?'상승 추세 · 눌림 대기':'추세 회복 대기'};
  }
  function equity(s,p){return s.cash+s.quantity*p;}
  function record(s,side,price,qty,reason,stamp){
    if(!Number.isFinite(price)||price<=0||!Number.isInteger(qty)||qty<1)throw Error('가상 체결 값 오류');
    let profit=0;
    if(side==='buy'){if(qty*price>s.cash+1e-7)throw Error('가상 예산 초과');s.cash-=qty*price;s.cost+=qty*price;s.quantity+=qty;s.lastBuy=price;s.round++;}
    else{if(qty>s.quantity)throw Error('가상 보유 수량 초과');const avg=s.cost/s.quantity;profit=(price-avg)*qty;s.cash+=qty*price;s.cost-=avg*qty;s.quantity-=qty;s.realized+=profit;s.cycleProfit+=profit;}
    s.trades.push({id:s.trades.length+1,stamp,side,price,quantity:qty,reason,profit,config:{...s.config}});s.note=reason;
    if(side==='sell'&&!s.quantity){s.cost=0;s.cycles++;if(s.cycleProfit>0)s.wins++;s.cycleProfit=0;s.round=0;s.stage=0;s.high=0;s.baseQuantity=0;s.exitDay=stamp.slice(0,8);s.resetSeen=false;s.note='매도 완료 · 다음 날 이후 새로운 눌림 신호 대기';}
  }
  function start(s,now){s.running=true;s.after=now;s.lastPoll=now;s.note='새로운 KRX 1분봉 대기';}
  function pause(s,note='사용자가 중지함'){s.running=false;s.note=note;}
  function step(s,{bars,daily,now}){
    if(!s.running)return;
    if(now-s.lastPoll>120000||now<s.lastPoll){pause(s,'2분 이상 시세 확인 공백 · 확인 후 다시 시작');return;}
    s.lastPoll=now;
    const local=new Date(now+9*3600000),time=local.toISOString().slice(11,16),weekday=local.getUTCDay();
    if(!weekday||weekday===6||time<'09:00'||time>='15:20'){s.note='KRX 09:00~15:20 새 시세 대기';return;}
    const b=(bars||[]).filter(b=>Number.isFinite(b.close)&&b.close>0&&stampMs(b.stamp)+60000<=now).sort((a,b)=>a.stamp.localeCompare(b.stamp)).at(-1);
    if(!b||b.stamp.slice(0,8)!==day(now)||now-stampMs(b.stamp)>150000){s.note='최신 완료 1분봉 대기 · 가상 체결 없음';return;}
    if(b.stamp<=s.lastStamp||stampMs(b.stamp)<s.after)return;
    s.lastStamp=b.stamp;const p=b.close,c=s.config,g=signal(daily,p,now,c);s.lastPrice=p;s.lastSignal=g;
    if(!g.ready){s.note=g.label;return;}
    if(!g.entry&&b.stamp.slice(0,8)>s.exitDay)s.resetSeen=true;
    const avg=s.quantity?s.cost/s.quantity:0;
    // One decision per completed minute, using only its close (no assumed intrabar path).
    if(s.quantity){
      const stop=avg*(1-c.stop/100);
      if(p<=stop)record(s,'sell',stop,s.quantity,'손실 한도 매도',b.stamp);
      else if(g.broken)record(s,'sell',p,s.quantity,'60일선 이탈 · 추가매수 중단 및 정리',b.stamp);
      else if(s.stage===2){s.high=Math.max(s.high,p);const trail=s.high*(1-c.trail/100);if(p<=trail)record(s,'sell',trail,s.quantity,'고점 하락 · 잔량 추적매도',b.stamp);else s.note='잔량 추적매도 감시';}
      else if(s.stage===0&&p>=avg*(1+c.tp1/100)){s.baseQuantity=s.quantity;s.stage=1;record(s,'sell',avg*(1+c.tp1/100),Math.max(1,Math.floor(s.baseQuantity*.3)),'1차 익절 30% · 추가매수 종료',b.stamp);}
      else if(s.stage===1&&p>=avg*(1+c.tp2/100)){s.stage=2;s.high=p;record(s,'sell',avg*(1+c.tp2/100),Math.min(s.quantity,Math.max(1,Math.floor(s.baseQuantity*.3))),'2차 익절 30%',b.stamp);}
      else if(!s.stage&&s.round<c.parts&&p<=s.lastBuy*(1-c.gap/100)&&g.entry){const price=s.lastBuy*(1-c.gap/100),qty=Math.floor(Math.min(c.budget/c.parts,s.cash)/price);if(qty)record(s,'buy',price,qty,'눌림 분할 추가매수',b.stamp);else s.note='가상 예산으로 1주 매수 불가';}
      else s.note=s.stage?'다음 익절 목표 대기':g.entry?'다음 분할매수 가격 대기':g.label;
    }else if(g.entry&&s.resetSeen&&b.stamp.slice(0,8)>s.exitDay){const qty=Math.floor(Math.min(c.budget/c.parts,s.cash)/p);if(qty)record(s,'buy',p,qty,'눌림목 첫 매수',b.stamp);else s.note='회차별 가상 투자금으로 1주 매수 불가';}
    else s.note=s.exitDay&&!s.resetSeen?'재진입 잠금 · 조건 해제 후 새 신호 대기':g.label;
    const e=equity(s,p);s.peak=Math.max(s.peak,e);s.maxDrawdown=Math.max(s.maxDrawdown,(s.peak-e)/s.peak*100);
  }
  const api={STOCKS,defaults,validate,create,signal,step,start,pause,equity,stampMs};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.PullbackCore=api;
})(typeof window==='undefined'?globalThis:window);
