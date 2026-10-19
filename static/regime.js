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
    const [name, cls] = CELL[row.cell] || CELL.unknown;
    el.className = 'regime ' + cls;
    el.hidden = false;
    if (row.cell === 'steady') {
      el.innerHTML = `<strong>오늘 장세 · ${esc(name)}</strong><p>${esc(row.what)} — ${esc(row.note)}</p>`;
      return;
    }
    const value = row.value === null || row.value === undefined ? '—' : `${row.value > 0 ? '+' : ''}${row.value}%`;
    const detail = row.error
      ? esc(row.error)
      : `${esc(row.what)} <b>${value}</b> (${esc(row.valueDay || '')}) · 좋은 칸 ${esc(row.goodAt || '')} ${esc(row.good)} · 나쁜 칸 ${esc(row.badAt || '')} ${esc(row.bad)}`;
    el.innerHTML = `<strong>오늘 장세 · ${esc(name)}</strong><p>${detail}`
      + (row.star ? ' · ★ 자료 앞뒤 반이 같은 방향' : '')
      + ' · <i>표시만 합니다 — 매수 여부는 바뀌지 않습니다</i></p>';
  }

  load();
  setInterval(load, 10 * 60 * 1000);
})();
