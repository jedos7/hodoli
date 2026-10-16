(function(root){'use strict';
function calculate(levels,bars,min=-Infinity,max=Infinity){
 const estimated=!levels.length,prices=new Map();
 for(const row of (estimated?bars.map(b=>({price:(b.high+b.low+b.close)/3,buy:b.close>=b.open?b.volume:0,sell:b.close<b.open?b.volume:0})):levels)){
  if(![row.price,row.buy,row.sell].every(Number.isFinite)||row.price<=0||row.buy<0||row.sell<0)continue;
  const q=prices.get(row.price)||[0,0];q[0]+=row.buy;q[1]+=row.sell;prices.set(row.price,q);
 }
 const keys=[...prices.keys()].sort((a,b)=>a-b);if(!keys.length)return {zones:[],bins:[],step:1,total:0,estimated};
 const low=keys[0],high=keys.at(-1),grouped=keys.length>48,step=grouped?(high-low)/48:Math.min(...keys.slice(1).map((p,i)=>p-keys[i]))||1;
 const width=Number.isFinite(step)?step:1,buckets=new Map();
 for(const [p,q]of prices){const key=grouped?Math.min(47,Math.floor((p-low)/width)):p;let b=buckets.get(key);if(!b){b={price:grouped?low+(key+.5)*width:p,buy:0,sell:0};buckets.set(key,b);}b.buy+=q[0];b.sell+=q[1];}
 const bins=[...buckets.values()].map(b=>({...b,volume:b.buy+b.sell})).filter(b=>b.volume>0),ranked=[...bins].sort((a,b)=>b.volume-a.volume||a.price-b.price);
 const poc=ranked[0];return {bins,zones:ranked.filter(b=>b.price>=min&&b.price<=max).slice(0,2).map(b=>({...b,poc:b===poc})),total:bins.reduce((s,b)=>s+b.volume,0),step:width,estimated};
}
if(typeof module!=='undefined'&&module.exports)module.exports={calculate};else root.VolumeProfile={calculate};
})(typeof globalThis!=='undefined'?globalThis:this);
