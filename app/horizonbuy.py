"""🌅 호라이즌존 자동매수 (변성환 규칙 문서 2026-10-19).

관제탑(app/horizon.py)이 D+2 12:00 정각에 '정오 조건 충족'(status='ready')으로 올린 자리를 그 자리에서 산다.
판정 규칙은 관제탑 그대로 쓰고, 여기서는 **매수 쪽 거름망과 수량·주문**만 더한다.

  매수 거름망 (문서): 주가 1,000원↑ · 기준봉 대금 1,500억↑ · 등급 S·A·B
  수량 (문서): S 는 두 칸, A·B 는 한 칸. 한 칸 = 화면에서 정한 '한 자리 금액'
  매수 (문서에 주문 방식이 없어 정한 것): 12:00 정각에 매도1호가 +2틱 지정가, 한 번만. 수량은 호가 총잔량 안에서
  매도: 기존 호라이즌 자동매도(app/horizon_plan.py)에 그대로 넘긴다
        손절 −10% 전량 · 익절 +15%에 70% 지정가 → 남은 수량은 고점 −3% 트레일링 · 청산날(산 날 +2거래일) 본전 지정가 → 15:20 종가

문서와 다르게 한 것 (안전 때문):
  · 미수 2.5배를 쓰지 않고 **현금**으로만 산다. 총 투입 상한(기본 999만 원 미만)과 종목 수 상한을 그대로 지킨다.
  · 계좌를 2칸으로 나누는 대신 '한 자리 금액'을 화면에서 직접 정한다. S 등급이면 그 두 배를 쓴다.

안전장치는 종가배팅과 같다: 실전 전송은 mode='live' 일 때만, 전송 전 저널, 전송 1회(응답 불명은 재전송 안 함),
시간창 밖·연결 끊김·비상 정지면 전송 금지, 다른 엔진(종배·내 조건)에 걸린 종목은 건드리지 않음.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import date, datetime, time

from app.autoexit import PENDING, AutoExit
from app.closebet import buy_price, in_window, plan_quantity
from app.breakers import NO_BREAKERS
from app.kiwoom.orders import NotSent, OrderRejected

TOTAL_CAP_MAX = 9_990_000       # 총 투입 1,000만 원 미만 (종가배팅과 같은 상한)
MAX_POSITIONS_MAX = 5
BUY_WINDOW = (time(12, 0, 0), time(12, 1, 0))     # 관제탑이 '정오 조건 충족' 을 띄우는 그 1분
FILL_WINDOW = (time(12, 5), time(12, 30))         # 체결 확인 → 자동매도 연결
DEFAULT_SETTINGS = {'mode': 'dry', 'enabled': False, 'slot_amount': 1_000_000, 'max_positions': 2,
                    'total_cap': 3_000_000, 'min_base_eok': 1500, 'link_exit': True}


def normalize_settings(raw):
    s = DEFAULT_SETTINGS | {k: raw[k] for k in DEFAULT_SETTINGS if k in raw}
    if s['mode'] not in ('dry', 'live'):
        raise ValueError('mode 는 dry 또는 live')
    s['enabled'], s['link_exit'] = bool(s['enabled']), bool(s['link_exit'])
    for key in ('slot_amount', 'max_positions', 'total_cap', 'min_base_eok'):
        if type(s[key]) is not int or s[key] <= 0:
            raise ValueError(f'{key} 는 양의 정수')
    if s['total_cap'] > TOTAL_CAP_MAX:
        raise ValueError(f'총 투입 상한은 {TOTAL_CAP_MAX:,}원 이하 (1,000만 원 미만)')
    if s['max_positions'] > MAX_POSITIONS_MAX:
        raise ValueError(f'자리 수는 최대 {MAX_POSITIONS_MAX}')
    if s['slot_amount'] > s['total_cap']:
        raise ValueError('한 자리 금액이 총 투입 상한보다 큽니다')
    return s


def pick(rows, min_base_eok):
    """관제탑 행 목록 → 매수 후보. (후보, 걸러진 이유 목록)"""
    picks, skipped = [], []
    for r in rows:
        if r.get('status') != 'ready':
            continue
        name = r.get('name') or r.get('code')
        grade = r.get('grade')
        base_eok = (r.get('base') or {}).get('amountEok') or 0
        price = r.get('price') or 0
        if grade not in ('S', 'A', 'B'):
            skipped.append(f'{name}: 등급 {grade}')
        elif base_eok < min_base_eok:
            skipped.append(f'{name}: 기준봉 대금 {base_eok:,.0f}억 (문턱 {min_base_eok:,}억)')
        elif price < 1000:
            skipped.append(f'{name}: 주가 {price:,}원 (1,000원 미만)')
        else:
            picks.append({'code': r['code'], 'name': name, 'grade': grade, 'base_eok': base_eok,
                          'price': price, 'd0': r.get('d0'), 'slots': 2 if grade == 'S' else 1})
    picks.sort(key=lambda p: (-p['slots'], -p['base_eok']))     # S 먼저, 그다음 기준봉 대금 큰 순
    return picks, skipped


class HorizonBuy(AutoExit):
    """plans 는 쓰지 않는다. positions = 호라이즌존으로 산 보유, days = 날짜별 진행, settings."""

    def __init__(self, path, env, broker, connected, phase, monitor, clock,
                 candidates=None, exit_engine=None, peers=(), notify=None, breakers=None, sizing=None):
        super().__init__(path, env, broker, connected, phase, monitor, clock)
        self.candidates, self.exit_engine = candidates or (lambda: []), exit_engine
        self.peers, self.notify = peers, notify or (lambda text: None)
        self.breakers = breakers or NO_BREAKERS        # 차단기 (네 매매법이 같이 쓴다. app/breakers.py)
        self.sizing = sizing                           # 💧 시장 대금 배팅 크기 · 없으면 ×1
        self.size_mult = 1.0
        self.settings = dict(DEFAULT_SETTINGS)
        self.positions, self.days, self.last_pick = {}, {}, {}

    # ── 저장 (AutoExit 의 plans/orders 에 settings/positions/days 를 더한다) ──
    def open(self):
        extra = {}
        if self.path.exists():
            try:
                extra = json.loads(self.path.read_text('utf-8'))
            except (OSError, ValueError):
                extra = {}
        super().open()
        if self.owner and not self.error:
            self.settings = normalize_settings(extra.get('settings') or {})
            self.positions, self.days = extra.get('positions') or {}, extra.get('days') or {}
            self.days = dict(sorted(self.days.items())[-20:])
            self.save()

    def save(self):
        if not self.owner:
            raise RuntimeError('호라이즌 자동매수 실행 잠금을 확보하지 못했습니다')
        import os
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'environment': self.env, 'plans': self.plans, 'orders': self.orders,
                       'settings': self.settings, 'positions': self.positions, 'days': self.days},
                      f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def update_settings(self, raw):
        s = normalize_settings(raw)
        if s['mode'] == 'live' and self.env != 'real':
            raise ValueError('실전 전송(live)은 실전 계좌 환경(KIWOOM_ENV=real)에서만 켤 수 있습니다')
        self.settings = s
        self.persist()
        return s

    def stop(self, note='사용자가 비상 정지를 눌렀습니다'):
        """새 전송을 모두 막는다. 접수된 주문은 취소하지 않고, 보유 종목도 그대로 둔다(자동매도가 이어서 판다)."""
        self.epoch += 1
        self.settings['enabled'] = False
        self.day()['stopped'] = note
        if self.owner and not self.error:
            self.persist()

    def today(self):
        return self.clock().date().isoformat()

    def day(self):
        return self.days.setdefault(self.today(), {})

    def live(self):
        return self.settings['mode'] == 'live'

    def invested(self):
        return sum(p['quantity'] * p['average'] for p in self.positions.values()
                   if p['status'] in ('held', 'linked') and not p.get('dry'))

    def held_codes(self):
        return [c for c, p in self.positions.items() if p['status'] in ('held', 'linked')]

    def committed(self):
        """보유한 돈 + 아직 보유로 안 잡힌 접수·미확정 매수 주문 금액.
        실전에서는 체결 확인 전까지 보유가 안 잡히므로, 이걸 안 쓰면 같은 묶음 안에서 총액 상한이 뚫린다."""
        spent = self.invested()
        for o in self.orders:
            if o['side'] != 'buy' or o.get('dry') or o['status'] not in PENDING:
                continue
            p = self.positions.get(o['code'])
            if p and p.get('buy_order') == o['id'] and p['status'] in ('held', 'linked'):
                continue
            spent += o['quantity'] * o['limit_price']
        return spent


    def peer_busy(self, code):
        for peer in self.peers:
            if peer.plans.get(code, {}).get('status') in ('prepared', 'armed', 'checking', 'ordered', 'remainder_check') or peer.blocked(code):
                return True
            if code in getattr(peer, 'positions', {}) and peer.positions[code].get('status') in ('held', 'selling', 'linked'):
                return True
        return False

    def guard_live(self, epoch):
        if not self.live():
            return
        self.gate()
        if epoch != self.epoch or not self.settings['enabled']:
            raise NotSent('호라이즌 자동매수가 꺼졌습니다')

    # ── 1초마다 ──
    async def tick(self):
        now = self.clock()
        if now.weekday() >= 5 or not self.owner or self.error:
            return
        d = self.day()
        if in_window(now, BUY_WINDOW) and not d.get('bought') and self.settings['enabled']:
            d['bought'] = now.isoformat()
            self.persist()
            await self.buy_all(now)
        if in_window(now, FILL_WINDOW) and d.get('bought') and not d.get('settled'):
            d['settled'] = now.isoformat()
            self.persist()
            await self.reconcile()
            await self.settle(now)

    async def read_sizing(self):
        """💧 오늘 한 칸에 곱할 배수 (전날 시장 대금 ÷ 앞 20일 평균). 못 읽으면 ×1."""
        self.size_mult = 1.0
        if not self.sizing:
            return 1.0
        try:
            self.size_mult = float(await self.sizing())
        except Exception:
            self.size_mult = 1.0
        if self.size_mult != 1.0:
            self.day()['sizing'] = f'💧 시장 대금 배팅 크기 ×{self.size_mult:g}'
        return self.size_mult

    # ── 12:00 매수 ──
    async def buy_all(self, now):
        await self.read_sizing()
        try:
            rows = self.candidates()
        except Exception as exc:
            self.day()['error'] = f'관제탑 자리 조회 실패: {str(exc)[:80]}'
            self.persist()
            self.notify(f"🌅 호라이즌존 매수 못 함 — {self.day()['error']}")
            return
        picks, skipped = pick(rows, self.settings['min_base_eok'])
        self.last_pick = {'at': now.isoformat(), 'picks': picks, 'skipped': skipped}
        self.day()['skipped'] = skipped
        room = self.settings['max_positions'] - len(self.held_codes())
        bought = []
        for r in picks:
            if room <= 0:
                break
            if r['code'] in self.positions and self.positions[r['code']]['status'] in ('held', 'linked'):
                continue
            if self.peer_busy(r['code']):
                self.day().setdefault('skipped', []).append(f"{r['name']}: 다른 자동매매에 걸린 종목")
                continue
            if await self.buy_one(r, now):
                room -= 1
                bought.append(f"{r['name']}({r['grade']})")
        if self.owner and not self.error:
            self.persist()
        mode = '실전' if self.live() else '흉내'
        if bought:
            self.notify(f"🌅 호라이즌존 매수({mode}): " + ', '.join(bought))
        else:
            self.notify(f"🌅 호라이즌존 정오 매수 없음 — 조건 충족 {len(picks)}자리"
                        + (f" · 건너뜀 {len(self.day().get('skipped', []))}" if self.day().get('skipped') else ''))

    async def buy_one(self, r, now):
        code, epoch = r['code'], self.epoch
        order = None
        try:
            async with self.lock:
                self.guard_live(epoch)
                self.breakers.check_buy(code)          # 차단기: 멈춤·되풀이·건수 (흉내에서도 본다)
                broker = self.broker()
                book = await broker.book(code)
                price = buy_price(book['ask'])          # 매도1호가 + 2틱
                budget = int(self.settings['slot_amount'] * r['slots'] * self.size_mult)
                cap = self.breakers.stock_cap()          # 🧱 한 종목 ≤ 아침 예탁자산
                budget = min(budget, int(cap)) if cap else budget
                remaining = self.settings['total_cap'] - self.committed()
                cap_qty = 10 ** 8
                if self.live():
                    await broker.stock(code, price or 1)       # 거래정지·투자유의·호가단위 확인
                    cap_qty = (await broker.capacity(code, price))['quantity'] if price else 0
                    if code in (await broker.positions()):
                        raise NotSent('이미 계좌에 있는 종목 · 섞이지 않게 건너뜀')
                qty, why = plan_quantity(price, book['total_ask_qty'], budget, remaining, 0, cap_qty)
                if qty <= 0:
                    raise NotSent(why)
                self.guard_live(epoch)
                self.breakers.check_buy(code, qty * price)      # 차단기: 한 번 금액
                order = {'id': secrets.token_hex(12), 'side': 'buy', 'code': code, 'name': r['name'], 'quantity': qty,
                         'limit_price': price, 'reason': f"HORIZON_{r['grade']}", 'environment': self.env,
                         'day': now.date().isoformat(), 'at': now.isoformat(), 'status': 'submitting', 'order_no': '',
                         'filled': 0, 'remaining': qty, 'note': f"{r['grade']}등급 {r['slots']}자리 · {why}",
                         'checked_at': None, 'dry': not self.live()}
                self.orders.append(order)
                self.persist()
                if self.live():
                    order['order_no'] = await broker.buy_once(code, qty, price, lambda: self.guard_live(epoch))
                    order['status'], order['note'] = 'accepted', '지정가 매수 접수 · 12:05 체결 확인'
                else:
                    order.update(status='dry', filled=qty, remaining=0,
                                 note='흉내: 전송하지 않음 · 매도1호가+2틱에 전량 체결로 가정')
                    self.positions[code] = {'code': code, 'name': r['name'], 'quantity': qty, 'average': price,
                                            'grade': r['grade'], 'bought_on': now.date().isoformat(),
                                            'status': 'held', 'dry': True, 'buy_order': order['id'],
                                            'note': '흉내 보유 · 자동매도는 연결하지 않음'}
                self.breakers.record_buy(code, qty * price, '호라이즌존')
                self.persist()
                return True
        except NotSent as exc:
            if order:
                order['status'], order['note'] = 'not_sent', str(exc)
            self.day().setdefault('skipped', []).append(f"{r['name']}: {exc}")
        except OrderRejected as exc:
            order['status'], order['note'] = 'rejected', str(exc)
            self.breakers.record_reject(code, exc, '호라이즌존 매수')
        except (Exception, asyncio.CancelledError) as exc:
            if order:
                order['status'], order['note'] = 'unknown', '전송/저장 결과 불명 · 재전송 차단 · 키움 주문내역 확인'
            else:
                self.day().setdefault('skipped', []).append(f"{r['name']}: 조회 실패 {str(exc)[:60]}")
        return False

    # ── 12:05 체결 확인 → 보유 등록 → 자동매도 연결 ──
    async def settle(self, now):
        if not self.live():
            return
        try:
            account = await self.broker().positions()
        except Exception:
            account = {}
        for o in self.orders:
            if o['side'] != 'buy' or o.get('dry') or o['day'] != self.today() or o['filled'] <= 0:
                continue
            if o['code'] in self.positions and self.positions[o['code']]['status'] in ('held', 'linked'):
                continue
            row = account.get(o['code'])
            self.positions[o['code']] = {
                'code': o['code'], 'name': o['name'], 'quantity': min(o['filled'], row['quantity']) if row else o['filled'],
                'average': row['average'] if row else o['limit_price'], 'grade': o['reason'].replace('HORIZON_', ''),
                'bought_on': o['day'], 'status': 'held', 'dry': False, 'buy_order': o['id'],
                'note': '' if row else '계좌 잔고에 아직 안 보임 · 주문 체결수량 기준'}
        self.persist()
        for p in [x for x in self.positions.values() if x['status'] == 'held' and not x.get('dry') and x['bought_on'] == self.today()]:
            if self.settings['link_exit']:
                await self.link_exit(p)
            else:
                p['note'] = '자동매도 연결 안 함(설정) · 직접 켜세요'
        self.persist()
        held = [p for p in self.positions.values() if p['bought_on'] == self.today() and not p.get('dry')]
        if held:
            self.notify('🌅 호라이즌존 체결: ' + ', '.join(
                f"{p['name']} {p['quantity']}주@{p['average']:,.0f} · {p['note'] or '자동매도 연결됨'}" for p in held))

    async def link_exit(self, p):
        """체결 잔고를 기존 호라이즌 자동매도(+15% 70% · −10% · 트레일링 · 청산날)에 넘긴다."""
        engine = self.exit_engine() if self.exit_engine else None
        if engine is None:
            p['note'] = '자동매도 엔진 없음 · 직접 켜세요'
            return
        try:
            if engine.error or not engine.owner:
                raise NotSent('자동매도 저장 상태 확인 필요')
            if p['code'] in engine.plans and engine.plans[p['code']]['status'] in ('armed', 'checking', 'ordered'):
                p['status'], p['note'] = 'linked', '이미 자동매도가 켜져 있는 종목'
                return
            prepared = await engine.prepare(p['code'], date.fromisoformat(p['bought_on']))
            await engine.arm(prepared['id'], self.env, True)
            p['status'], p['note'], p['exit_plan_id'] = 'linked', '자동매도 연결 완료 (익절 70% · 손절 · 청산날)', prepared['id']
        except Exception as exc:
            p['note'] = f'자동매도 연결 실패: {str(exc)[:70]} · 자동매도 화면에서 직접 켜세요'
            self.notify(f"⚠️ 호라이즌존 {p['name']} 자동매도 연결 실패 — 직접 켜세요: {str(exc)[:60]}")

    async def _reconcile(self):
        await super()._reconcile()
        for p in self.positions.values():
            if p['status'] in ('held', 'linked') and self.exit_engine:
                engine = self.exit_engine()
                plan = engine.plans.get(p['code']) if engine else None
                if plan and plan.get('status') == 'completed':
                    p['status'], p['note'] = 'sold', '자동매도 완료'
        if self.owner and not self.error:
            self.persist()

    def snapshot(self):
        return {'environment': self.env, 'settings': self.settings, 'live': self.live(),
                'enabled': self.settings['enabled'], 'connected': bool(self.connected()), 'error': self.error,
                'asof': self.clock().isoformat(), 'invested': self.invested(),
                'positions': list(self.positions.values()), 'day': self.days.get(self.today(), {}),
                'lastPick': self.last_pick, 'orders': self.orders[-100:], 'breakers': self.breakers.snapshot(),
                'windows': {'buy': '12:00', 'settle': '12:05'}}
