from app.kis.rest import Candle
from app.screener.hoga_play import backtest, find_setups


def _flat(n, px=10000, start_day=1):
    out = []
    for i in range(n):
        d = f"202601{start_day + i:02d}"
        out.append(Candle(d, px, px + 50, px - 50, px, 100000, 100000 * px))
    return out


def _c(day, o, h, l, c, amt_eok=200):
    return Candle(f"202602{day:02d}", o, h, l, c, 1000, int(amt_eok * 1e8))


def test_spike_then_tight_consolidation_passes():
    cs = _flat(40)  # MACD 워밍업
    cs += [_c(1, 10000, 11300, 9950, 11200)]      # 급등봉 +12%
    cs += [_c(2, 11150, 11350, 11000, 11250)]     # 1일 횡보, 폭 3%, 저점 상승
    cs += [_c(3, 11250, 11400, 11050, 11300)]     # 2일 횡보
    cs += _flat(5, 11500, start_day=10)          # 3일 뒤 수익률 계산용
    setups = find_setups(cs, "000001", "테스트", "테마")
    assert [s.hold_days for s in setups] == [1, 2, 3]  # 뒤의 평탄 봉도 횡보로 이어진다
    s1 = setups[0]
    assert s1.spike_pct > 11 and s1.box_pct < 10 and s1.strict, s1.reasons
    assert s1.fwd_ret is not None and s1.fwd_ret > 0


def test_close_bet_flag_and_gap():
    cs = _flat(40) + [_c(1, 10000, 11300, 9950, 11200), _c(2, 11150, 11350, 11000, 11250), _c(3, 11400, 11450, 11100, 11300)]
    cs += _flat(5, 11500, start_day=10)
    setups = find_setups(cs, "000001", "테스트", "테마")
    d1 = next(s for s in setups if s.hold_days == 1)
    assert d1.strict and d1.close_bet and round(d1.gap, 2) == round((11400 / 11250 - 1) * 100, 2)   # 다음 날 시가 11400
    d2 = next(s for s in setups if s.hold_days == 2)
    assert d2.strict and not d2.close_bet
    assert d1.as_dict()["closeBet"] is True and d1.as_dict()["gapPct"] == round(d1.gap, 2)


def test_wide_box_and_falling_low_are_reasons():
    cs = _flat(40)
    cs += [_c(1, 10000, 11300, 9950, 11200)]
    cs += [_c(2, 11200, 11600, 10200, 11000)]     # 폭 12.5%, 저가가 급등봉 저가 위이긴 함
    cs += [_c(3, 11000, 11100, 9900, 10900)]      # 저점이 첫날보다 내려감
    setups = find_setups(cs, "000001", "테스트", "테마")
    assert any("횡보 폭 넓음" in r for r in setups[0].reasons)
    assert "저점이 안 올라옴" in setups[1].reasons


def test_break_below_spike_open_kills_setup():
    cs = _flat(40)
    cs += [_c(1, 10000, 11300, 9950, 11200), _c(2, 11000, 11100, 9800, 9900)]  # 급등 시가 아래로 종가
    assert find_setups(cs, "000001", "테스트", "테마") == []


def test_backtest_stats():
    cs = _flat(40) + [_c(1, 10000, 11300, 9950, 11200), _c(2, 11150, 11350, 11000, 11250)] + _flat(5, 11500, start_day=10)
    one_day = [s for s in find_setups(cs, "000001", "테스트", "테마") if s.hold_days == 1]
    st = backtest(one_day, baseline_ret=0.5)
    assert st["n"] == 1 and st["win"] == 100.0 and st["vsBase"] == round(st["avg"] - 0.5, 2)
