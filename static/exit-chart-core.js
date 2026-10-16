// Pure chart calculations, shared by the canvas UI and offline tests.
(function(root){
  'use strict';
  const finite = v => typeof v==='number' && Number.isFinite(v);
  const validPrice = v => Number.isFinite(Number(v)) && Number(v)>0;
  function bars(input){
    const unique=new Map();
    for(const b of Array.isArray(input)?input:[]){
      if(!b || !/^\d{8}(\d{6})?$/.test(b.stamp) || !['open','high','low','close','volume'].every(k=>finite(b[k])))continue;
      if(b.low<=0 || b.low>Math.min(b.open,b.close) || b.high<Math.max(b.open,b.close) || b.volume<0)continue;
      unique.set(b.stamp,{...b});
    }
    return [...unique.values()].sort((a,b)=>a.stamp.localeCompare(b.stamp));
  }
  function lines(context){
    if(!context)return [];
    const values=[{kind:'average',label:'평단',price:context.average,color:'#c5d1e5'}];
    if(context.rules){
      const names={profit:'익절',protection:'이익보전',loss:'손절'}, colors={profit:'#ff7878',protection:'#f5c45d',loss:'#70aaff'};
      for(const r of context.rules){
        if(['done','closed'].includes(r.state))continue;
        values.push({kind:r.kind,label:names[r.kind]+' '+({gte:'이상',lte:'이하',lt:'미만'}[r.comparison])+(context.source==='draft'?'':r.enabled?'':' · 꺼짐'),price:r.price,color:colors[r.kind]});
      }
      return values.filter(l=>validPrice(l.price)).map(l=>({...l,price:Number(l.price)}));
    }
    if(context.protection)values.push({kind:'protection',label:'이익보전 미만',price:context.protection,color:'#6fe4ad'});
    if(context.profit)values.push({kind:'profit',label:'익절 이상',price:context.profit,color:'#ff7878'});
    if(context.loss)values.push({kind:'loss',label:'손절 이하',price:context.loss,color:'#70aaff'});
    return values.filter(l=>validPrice(l.price)).map(l=>({...l,price:Number(l.price)}));
  }
  function fromPlan(p){
    return {code:p.code,name:p.name,quantity:p.quantity,average:p.average,
      protection:p.profit_protection?.price,profit:p.take_profit_done?null:p.take_profit?.price,loss:p.stop_loss?.price,
      ...(p.conditions?{rules:p.conditions}:{}),source:'plan',status:p.watch_state||p.status};
  }
  function fromDraft(p,inputs){
    const target=(leg,sign)=>leg.enabled&&validPrice(leg.pct)?Number(p.average)*(1+sign*Number(leg.pct)/100):null;
    return {code:p.code,name:p.name,quantity:p.quantity,average:p.average,
      protection:inputs.pp.enabled&&validPrice(inputs.pp.price)?Number(inputs.pp.price):null,
      profit:target(inputs.tp,1),loss:target(inputs.sl,-1),source:'draft'};
  }
  function windowRange(total,count,offset){
    count=Math.max(20,Math.min(300,Math.round(count)));
    const end=Math.max(Math.min(count,total),total-Math.max(0,Math.round(offset)));
    return {start:Math.max(0,end-count),end};
  }
  function extent(rows,levels){
    const prices=[...rows.flatMap(b=>[b.low,b.high]),...levels.map(l=>l.price)].filter(validPrice).map(Number);
    if(!prices.length)return {low:0,high:1};
    const min=Math.min(...prices),max=Math.max(...prices),pad=Math.max((max-min)*.09,max*.001,1);
    return {low:Math.max(0,min-pad),high:max+pad};
  }
  const api={bars,lines,fromPlan,fromDraft,windowRange,extent};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.ExitChartCore=api;
})(typeof window!=='undefined'?window:globalThis);
