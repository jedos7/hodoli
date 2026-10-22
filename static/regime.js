/* 오늘 장세 맥락 한 줄 (네 매매 화면 공용). 이 화면의 매매법 것만 보여준다.
   표시만 하고 매수 여부는 바꾸지 않는다. 규칙과 출처는 app/regime.py */
(() => {
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const CELL = {
    good: ['좋은 칸', 'good'],
    bad: ['나쁜 칸', 'bad'],
    mid: ['가운데 칸', ''],
    steady: ['맥락을 덜 탐', ''],
    unknown: ['값 확인 안 됨', ''],
  };

  async function load() {
    const el = document.getElementById('regimeBar');
    if (!el) return;
    const key = el.dataset.key;
    let d;
    try {
      const r = await fetch('/api/regime', { headers: { Accept: 'application/json' } });
      if (!r.ok) throw new Error(r.status);
      d = await r.json();
    } catch (e) {
      el.hidden = true;
      return;
    }
    const row = (d.rows || []).find((x) => x.key === key);
    if (!row) { el.hidden = true; return; }
    const v = d.volume || {};
    const water = v.multiplier && v.multiplier !== 1
      ? ` · 💧 오늘 배팅 크기 <b>×${v.multiplier}</b> (전날 시장 대금 ${v.prevJo}조 · 평소의 ${v.ratio}배)`
      : (v.ratio ? ` · 💧 배팅 크기 ×1 (전날 시장 대금 ${v.prevJo}조 · 평소의 ${v.ratio}배)` : '');
    const [name, cls] = CELL[row.cell] || CELL.unknown;
    el.className = 'regime ' + cls;
    el.hidden = false;
    if (row.cell === 'steady') {
      el.innerHTML = `<strong>오늘 장세 · ${esc(name)}</strong><p>${esc(row.what)} — ${esc(row.note)}${water}</p>`;
      return;
    }
    const value = row.value === null || row.value === undefined ? '—' : `${row.value > 0 ? '+' : ''}${row.value}%`;
    if (row.rest) {
      // 🧭 맥락 문 — 제작자 프로그램의 실제 규칙. 새 매수만 멈춘다.
      el.className = 'regime rest';
      el.innerHTML = `<strong>🧭 오늘은 쉽니다</strong><p>${esc(row.restWhy)} (${esc(row.restRule)})`
        + ' · <b>새 매수만 멈춥니다</b> — 들고 있는 종목의 매도는 그대로 나갑니다</p>';
      return;
    }
    const detail = row.error
      ? esc(row.error)
      : `${esc(row.what)} <b>${value}</b> (${esc(row.valueDay || '')}) · 좋은 칸 ${esc(row.goodAt || '')} ${esc(row.good)} · 나쁜 칸 ${esc(row.badAt || '')} ${esc(row.bad)}`;
    el.innerHTML = `<strong>오늘 장세 · ${esc(name)}</strong><p>${detail}`
      + (row.star ? ' · ★ 자료 앞뒤 반이 같은 방향' : '')
      + (row.restRule ? ` · <i>맥락 문: ${esc(row.restRule)} — 오늘은 삽니다</i>` : '')
      + water + '</p>';
  }

  load();
  setInterval(load, 10 * 60 * 1000);
})();
