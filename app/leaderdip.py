"""🗻 대장 눌림목 (변성환 규칙 문서 2026-10-19).

코스피 대형주가 장중에 크게 밀릴 때 F존에서 반씩 받아 다음 날 시초가에 판다.
매도(다음 날 09:00 시초가 시장가 전량)는 🌙 종가배팅과 같아 app/closebet.py 의 뼈대를 물려받는다.

  대상: 전날 종가 기준 **코스피 시총 상위 10종목** (문서: 20·30으로 넓히면 나빠졌음)
  기준선: 그날 **09:00부터의 15분봉**으로 고가라인을 잡는다 (문서: 08:00 NXT 부터 잡으면 전부 나빠졌음)
          F2 = 고가라인 × 0.90 · F3 = 고가라인 × 0.88
  매수: **10:00~14:59** 에 값이 F2 아래로 내려오면 그 순간 고가라인을 고정하고,
        한 칸의 반은 F2 지정가, 나머지 반은 F3 지정가로 **동시에** 걸어 둔다.
        F3 가 15:19 까지 안 체결되면 취소(반 칸만 들고 감).
        **몰린 날**: 그날 F2 에 닿은 대장주가 3종목 이상이면 반 칸만 쓴다.
        한 종목 하루 한 번 · 이미 들고 있는 종목은 안 삼.
  매도: 다음 날 09:00 시초가에 시장가 전량. 손절 없음.

원본에 수식이 없어 정한 것 (화면 규칙표에도 적는다):
  · **고가라인** = 09:00 이후 15분봉 중 '새 고가를 만든 봉' 또는 '저가에서 4% 넘게 되돌린(반등한) 봉' 의 고가 중
    **가장 최근 것**. 크게 빠진 뒤 반등했다가 다시 밀리면 그 반등 고점에서 다시 잰다는 뜻으로 읽었다.
  · 지금 값 = 키움 15분봉의 마지막(진행 중) 봉 종가. 1분마다 다시 받는다.
문서의 미수 2.5배는 쓰지 않고 현금으로만 산다 (총 투입 상한이 1,000만 원 미만이라 레버리지가 무의미하고 위험).
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import time

from app.autoexit import PENDING
from app.closebet import SKIP_NAME, CloseBet, in_window, plan_quantity
from app.collectors.naver_api import NaverApi, num
from app.fanout import fanout
from app.kiwoom.orders import NotSent, OrderRejected

MONITOR_WINDOW = (time(10, 0), time(14, 59))     # 문서: 10:00~14:59 에 닿은 것만
CANCEL_WINDOW = (time(15, 19), time(15, 25))     # F3 미체결 취소
FILL_WINDOW = (time(15, 31), time(15, 45))
POLL_SECONDS = 60
F2_RATE, F3_RATE = 0.90, 0.88
REBOUND_PCT = 4.0                                # '4% 넘게 반등한 봉'
CROWDED_COUNT = 3                                # 몰린 날 기준
TOTAL_CAP_MAX = 9_990_000
MAX_POSITIONS_MAX = 5
DEFAULT_SETTINGS = {'mode': 'dry', 'enabled': False, 'slot_amount': 1_000_000, 'max_positions': 2,
                    'total_cap': 3_000_000, 'top_n': 10, 'sell_time': '08:40'}


def normalize_settings(raw):
    s = DEFAULT_SETTINGS | {k: raw[k] for k in DEFAULT_SETTINGS if k in raw}
    if s['mode'] not in ('dry', 'live'):
        raise ValueError('mode 는 dry 또는 live')
    s['enabled'] = bool(s['enabled'])
    for key in ('slot_amount', 'max_positions', 'total_cap', 'top_n'):
        if type(s[key]) is not int or s[key] <= 0:
            raise ValueError(f'{key} 는 양의 정수')
    if s['total_cap'] > TOTAL_CAP_MAX:
        raise ValueError(f'총 투입 상한은 {TOTAL_CAP_MAX:,}원 이하 (1,000만 원 미만)')
    if s['max_positions'] > MAX_POSITIONS_MAX:
        raise ValueError(f'자리 수는 최대 {MAX_POSITIONS_MAX}')
    if not 3 <= s['top_n'] <= 30:
        raise ValueError('대장주 수는 3~30')
    if s['slot_amount'] > s['total_cap']:
        raise ValueError('한 칸 금액이 총 투입 상한보다 큽니다')
    from datetime import datetime
    try:
        t = datetime.strptime(s['sell_time'], '%H:%M').time()
    except ValueError:
        raise ValueError('sell_time 은 HH:MM')
    if not time(8, 30) <= t < time(9, 0):
        raise ValueError('매도 시각은 08:30~08:59 (장전 동시호가)')
    return s


def anchor_line(bars, rebound_pct=REBOUND_PCT):
    """09:00 이후 15분봉 → (고가라인, 그 봉 시각). 새 고가 봉 또는 4% 넘게 반등한 봉의 고가 중 가장 최근 것."""
    run_high, anchor, at = 0.0, 0.0, ''
    for stamp, o, h, l, c, v in bars:
        rebound = (c / l - 1) * 100 if l > 0 else 0.0   # 저가에서 되돌린 폭 = '반등'. 봉 전체 폭을 쓰면 급락 봉이 기준선을 잘못 내린다
        if h > run_high or rebound > rebound_pct:      # '새 고가' 는 더 높을 때만 (같은 값은 기준선을 옮기지 않는다)
            anchor, at = h, stamp
        run_high = max(run_high, h)
    return anchor, at


def zones(anchor):
    return round(anchor * F2_RATE), round(anchor * F3_RATE)


class LeaderDip(CloseBet):
    label, icon = '대장 눌림목', '🗻'
    gate_key = 'leaderdip'         # 🧭 전날 코스피가 0% 위면 그날은 쉼
    fill_window = FILL_WINDOW
    normalizer = staticmethod(normalize_settings)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setups, self.watch, self.universe_at = {}, {}, ''
        self.poll_task, self.polled_at = None, None

    # ── 저장: setups 도 같이 ──
    def open(self):
        extra = {}
        if self.path.exists():
            try:
                import json
                extra = json.loads(self.path.read_text('utf-8'))
            except (OSError, ValueError):
                extra = {}
        super().open()
        if self.owner and not self.error:
            self.setups = extra.get('setups') or {}
            self.save()

    def save(self):
        if not self.owner:
            raise RuntimeError(f'{self.label} 실행 잠금을 확보하지 못했습니다')
        import json
        import os
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'environment': self.env, 'plans': self.plans, 'orders': self.orders,
                       'settings': self.settings, 'positions': self.positions, 'scans': self.scans,
                       'days': self.days, 'setups': self.setups}, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ── 대장주 목록 (코스피 시총 상위 N) ──
    async def universe(self):
        today = self.today()
        if self.watch and self.universe_at == today:
            return self.watch
        api = NaverApi()
        try:
            d = await api.json('/stocks/marketValue/KOSPI', {'page': 1, 'pageSize': 40})
        finally:
            await api.close()
        out = {}
        for r in d.get('stocks') or []:
            code, name = str(r.get('itemCode') or ''), str(r.get('stockName') or '').strip()
            if len(code) != 6 or r.get('stockEndType', 'stock') != 'stock' or any(k in name for k in SKIP_NAME):
                continue
            stop = (r.get('tradeStopType') or {}).get('name')
            if stop and stop != 'TRADING':
                continue
            out[code] = {'code': code, 'name': name, 'cap_eok': num(r.get('marketValue'))}
            if len(out) >= self.settings['top_n']:
                break
        if len(out) < 3:
            raise RuntimeError('코스피 시총 상위 목록을 못 읽었습니다')
        self.watch, self.universe_at = out, today
        return out

    # ── 1초마다 ──
    async def tick(self):
        now = self.clock()
        if now.weekday() >= 5 or not self.owner or self.error:
            return
        d = self.day()
        if (in_window(now, MONITOR_WINDOW) and self.settings['enabled'] and not self.poll_task
                and (self.polled_at is None or (now - self.polled_at).total_seconds() >= POLL_SECONDS)):
            self.polled_at = now
            self.poll_task = asyncio.create_task(self._poll(now), name='leaderdip-poll')
            self.tasks.add(self.poll_task)
            self.poll_task.add_done_callback(self.tasks.discard)
        if in_window(now, CANCEL_WINDOW) and not d.get('cancelled') and self.open_buy_orders():
            d['cancelled'] = now.isoformat()
            self.persist()
            await self.cancel_unfilled(now)
        await self.tick_sell(now, d)

    def open_buy_orders(self):
        return [o for o in self.orders if o['side'] == 'buy' and not o.get('dry')
                and o['day'] == self.today() and o['status'] in PENDING and o['order_no']]

    # ── 1분마다: 15분봉으로 고가라인·F존 갱신, F2 아래면 주문 ──
    async def _poll(self, now):
        try:
            universe = await self.universe()
            broker = self.broker()
            ymd = self.today().replace('-', '')
            codes = list(universe)
            # 대장주 15분봉을 몇 개씩 겹쳐 받는다. 판단과 주문은 받은 뒤 **목록 순서대로** 한다 (자리 상한·몰린 날 셈이 어긋나지 않게).
            fetched = await fanout(codes, lambda c: broker.minutes(c, '15'))
            touched_today = {s['code'] for s in self.setups.values() if s['day'] == self.today()}
            resting = await self.resting()      # 🧭 맥락 문 · 쉬는 날이어도 화면 값은 계속 갱신한다
            if not resting:
                await self.read_sizing()        # 💧 배팅 크기
            for code, got in zip(codes, fetched):
                info = universe[code]
                if isinstance(got, Exception):
                    self.day().setdefault('poll_errors', {})[code] = str(got)[:60]
                    continue
                bars = [b for b in got if b[0][:8] == ymd]
                if not bars:
                    continue
                anchor, anchor_at = anchor_line(bars)
                last = bars[-1][4]
                f2, f3 = zones(anchor)
                info.update(anchor=anchor, anchorAt=anchor_at, f2=f2, f3=f3, last=last,
                            gap=round((last / anchor - 1) * 100, 2) if anchor else None, at=now.isoformat())
                key = f'{self.today()}:{code}'
                if resting or key in self.setups or anchor <= 0 or last >= f2:
                    continue
                if code in self.positions and self.positions[code]['status'] in ('held', 'selling'):
                    continue
                if len(self.held_codes()) >= self.settings['max_positions']:
                    self.day().setdefault('skipped', []).append(f"{info['name']}: 자리 상한")
                    continue
                if self.peer_busy(code):
                    self.day().setdefault('skipped', []).append(f"{info['name']}: 다른 자동매매에 걸린 종목")
                    continue
                crowded = len(touched_today | {code}) >= CROWDED_COUNT
                touched_today.add(code)
                await self.enter(info, f2, f3, anchor, crowded, now)
        except Exception as exc:
            self.day()['error'] = f'감시 실패: {str(exc)[:100]}'
            if self.owner and not self.error:
                self.persist()
        finally:
            self.poll_task = None

    async def enter(self, info, f2, f3, anchor, crowded, now):
        """F2 아래로 내려온 순간: 한 칸(몰린 날이면 반 칸)을 반씩 F2·F3 지정가로 동시에 건다."""
        code = info['code']
        key = f'{self.today()}:{code}'
        budget = self.slot_amount(self.settings['slot_amount']) // (2 if crowded else 1)
        setup = {'key': key, 'day': self.today(), 'code': code, 'name': info['name'], 'anchor': anchor,
                 'f2': f2, 'f3': f3, 'crowded': crowded, 'at': now.isoformat(), 'orders': [], 'note': ''}
        self.setups[key] = setup
        self.persist()
        placed = []
        for zone, price in (('F2', f2), ('F3', f3)):
            oid = await self.place(setup, zone, price, budget // 2, now)
            if oid:
                placed.append(zone)
        setup['note'] = ('건 주문: ' + ', '.join(placed)) if placed else '주문 못 함'
        self.persist()
        mode = '실전' if self.live() else '흉내'
        self.notify(f"{self.icon} {self.label}({mode}) {info['name']} F2 터치 · 고가라인 {anchor:,.0f} → "
                    f"F2 {f2:,} / F3 {f3:,}" + (' · 몰린 날 반 칸' if crowded else '') + f" · {setup['note']}")

    async def place(self, setup, zone, price, budget, now):
        code, epoch = setup['code'], self.epoch
        order = None
        try:
            async with self.lock:
                self.guard_live(epoch)
                broker = self.broker()
                remaining = self.settings['total_cap'] - self.committed()
                cap_qty = 10 ** 8
                if self.live():
                    await broker.stock(code, price or 1)
                    cap_qty = (await broker.capacity(code, price))['quantity'] if price else 0
                qty, why = plan_quantity(price, 10 ** 9, budget, remaining, 0, cap_qty)
                if qty <= 0:
                    raise NotSent(why)
                self.guard_live(epoch)
                order = {'id': secrets.token_hex(12), 'side': 'buy', 'code': code, 'name': setup['name'],
                         'quantity': qty, 'limit_price': price, 'reason': f'DIP_{zone}', 'zone': zone,
                         'setup': setup['key'], 'environment': self.env, 'day': now.date().isoformat(),
                         'at': now.isoformat(), 'status': 'submitting', 'order_no': '', 'filled': 0,
                         'remaining': qty, 'note': f'{zone} {price:,}원 · {why}', 'checked_at': None,
                         'dry': not self.live()}
                self.orders.append(order)
                setup['orders'].append(order['id'])
                self.persist()
                if self.live():
                    order['order_no'] = await broker.buy_once(code, qty, price, lambda: self.guard_live(epoch))
                    order['status'], order['note'] = 'accepted', f'{zone} 지정가 접수 · 15:19 미체결이면 취소'
                else:
                    order.update(status='dry', filled=qty, remaining=0, note=f'흉내: {zone} {price:,}원에 체결 가정')
                    p = self.positions.get(code)
                    if p and p.get('dry'):
                        total = p['quantity'] + qty
                        p['average'] = (p['average'] * p['quantity'] + price * qty) / total
                        p['quantity'] = total
                    else:
                        self.positions[code] = {'code': code, 'name': setup['name'], 'quantity': qty,
                                                'average': price, 'bought_on': now.date().isoformat(),
                                                'status': 'held', 'dry': True, 'buy_order': order['id'],
                                                'note': '흉내 보유'}
                self.persist()
                return order['id']
        except NotSent as exc:
            if order:
                order['status'], order['note'] = 'not_sent', str(exc)
            self.day().setdefault('skipped', []).append(f"{setup['name']} {zone}: {exc}")
        except OrderRejected as exc:
            order['status'], order['note'] = 'rejected', str(exc)
        except (Exception, asyncio.CancelledError):
            if order:
                order['status'], order['note'] = 'unknown', '전송/저장 결과 불명 · 재전송 차단 · 키움 주문내역 확인'
        if self.owner and not self.error:
            self.persist()
        return None

    # ── 15:19 F3(그리고 남은 F2) 미체결 취소 ──
    async def cancel_unfilled(self, now):
        cancelled = []
        for o in self.open_buy_orders():
            if o['filled'] >= o['quantity'] or o.get('cancel_sent'):
                continue
            epoch = self.epoch
            try:
                async with self.lock:
                    self.guard_sell(epoch)      # 취소는 비상 정지해도 막지 않는다 (미체결 매수를 남기는 쪽이 더 위험)
                    o['cancel_sent'] = now.isoformat()
                    self.persist()      # 취소 의도를 먼저 남긴다
                    await self.broker().cancel_once(o, lambda: self.guard_sell(epoch))
                    o['note'] = f"{o.get('zone', '')} 미체결 잔량 취소 요청 · 키움에서 확인"
                    cancelled.append(f"{o['name']} {o.get('zone', '')}")
            except Exception as exc:
                o['note'] = f'취소 요청 실패: {str(exc)[:60]} · 키움에서 직접 확인'
        if self.owner and not self.error:
            self.persist()
        if cancelled:
            self.notify(f"{self.icon} {self.label} 미체결 취소: " + ', '.join(cancelled))

    # ── 15:31 체결 확인 (한 종목에 F2·F3 두 주문이 있으므로 계좌 잔고를 기준으로 합친다) ──
    async def settle_buys(self):
        if not self.live():
            return
        try:
            account = await self.broker().positions()
        except Exception:
            account = {}
        today = self.today()
        codes = {o['code'] for o in self.orders if o['side'] == 'buy' and not o.get('dry')
                 and o['day'] == today and o['filled'] > 0}
        for code in codes:
            held = self.positions.get(code)
            if held and held['status'] in ('held', 'selling'):
                continue
            mine = [o for o in self.orders if o['code'] == code and o['side'] == 'buy' and o['day'] == today and o['filled'] > 0]
            filled = sum(o['filled'] for o in mine)
            spent = sum(o['filled'] * o['limit_price'] for o in mine)
            row = account.get(code)
            self.positions[code] = {
                'code': code, 'name': mine[0]['name'], 'quantity': row['quantity'] if row else filled,
                'average': row['average'] if row else (spent / filled if filled else 0),
                'bought_on': today, 'status': 'held', 'dry': False, 'buy_order': mine[-1]['id'],
                'zones': sorted({o.get('zone', '') for o in mine}),
                'note': '' if row else '계좌 잔고에 아직 안 보임 · 주문 체결수량 기준'}
        self.persist()
        held = [p for p in self.positions.values() if p['status'] == 'held' and p['bought_on'] == today and not p.get('dry')]
        if held:
            self.notify(f"{self.icon} {self.label} 체결 보유: " + ', '.join(
                f"{p['name']} {p['quantity']}주@{p['average']:,.0f}({'·'.join(p.get('zones') or [])})" for p in held)
                + f" · 내일 {self.settings['sell_time']} 시장가 매도 예정")

    def snapshot(self):
        base = super().snapshot()
        today = self.today()
        base.update(setups=[s for s in self.setups.values() if s['day'] == today],
                    watch=list(self.watch.values()), universeAt=self.universe_at,
                    polledAt=self.polled_at.isoformat() if self.polled_at else None,
                    zones={'f2': F2_RATE, 'f3': F3_RATE, 'rebound': REBOUND_PCT, 'crowded': CROWDED_COUNT},
                    windows={'monitor': '10:00~14:59', 'cancel': '15:19', 'fill': '15:31',
                             'sell': self.settings['sell_time']})
        return base
