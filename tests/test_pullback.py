from app.kis.rest import Candle
from app.screener.pullback import find_pullbacks, sma


def _flat(n, px=10000, vol=100000, start=1):
    return [Candle(f"202601{start + i:02d}", px, px + 50, px - 50, px, vol, vol * px) for i in range(n)]


def _c(day, o, h, l, c, vol, amt_eok=200):
    return Candle(f"202602{day:02d}", o, h, l, c, vol, int(amt_eok * 1e8))


def test_sma():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_textbook_pullback_is_strict():
    cs = _flat(40)                                   # 20일선 = 10000 근처
    cs += [_c(1, 10000, 11300, 9950, 11200, 1_000_000)]   # 급등 +12%, 거래량 100만
    cs += [_c(2, 11200, 11600, 11100, 11500, 600_000)]    # 고점 11600
    cs += [_c(3, 11400, 11450, 11000, 11050, 400_000)]    # 눌림 1일: -4.7%, 거래량 0.4배 (안 줄음)
    cs += [_c(4, 11000, 11100, 10700, 10800, 150_000)]    # 눌림 2일: -6.9%, 평균 거래량 0.275배, 저점 10700 > 9950
    cs += _flat(5, 11300, 100000, start=10)               # 3일 뒤 수익률 계산용
    setups = find_pullbacks(cs, "000001", "테스트", "테마")
    by_day = {s.date: s for s in setups}
    assert "20260203" in by_day and "20260204" in by_day
    d3, d4 = by_day["20260203"], by_day["20260204"]
    assert any("거래량 안 줄음" in r for r in d3.reasons)
    assert d4.strict, d4.reasons
    assert d4.pull_days == 2 and round(d4.vol_ratio, 3) == 0.275 and d4.peak_high == 11600
    assert d4.fwd_ret is not None and d4.fwd_ret > 0 and 3 in d4.fwd and d4.low_hold


def test_long_pullback_is_flagged():
    cs = _flat(40)
    cs += [_c(1, 10000, 11300, 9950, 11200, 1_000_000), _c(2, 11200, 11600, 11100, 11500, 500_000)]
    for d in range(3, 8):                                          # 5일간 얕게 눌림, 거래량 0.1배
        cs += [_c(d, 11000, 11050, 10900, 10950, 100_000)]
    s = {x.date: x for x in find_pullbacks(cs, "000001", "테스트", "테마")}
    assert s["20260205"].strict                                    # 눌림 3일째
    assert any("눌림 길어짐" in r for r in s["20260207"].reasons)  # 5일째


def test_heavy_volume_and_ma_break_are_reasons():
    cs = _flat(40, px=12000)                                  # 20일선 12000
    cs += [_c(1, 12000, 13500, 11950, 13400, 1_000_000)]      # 급등 +11.7%
    cs += [_c(2, 13400, 13900, 13300, 13800, 900_000)]
    cs += [_c(3, 13700, 13750, 12200, 12300, 900_000)]        # -11.5% 눌림이지만 거래량 0.9배
    cs += [_c(4, 12200, 12300, 11700, 12050, 200_000)]        # 급등 시가(12000) 위지만 20일선(≈12175) 아래, 저점 11700 < 11950
    setups = {s.date: s for s in find_pullbacks(cs, "000001", "테스트", "테마")}
    assert any("거래량 안 줄음" in r for r in setups["20260203"].reasons)
    r4 = setups["20260204"].reasons
    assert "20일선 아래" in r4 and "급등봉 저점 이탈" in r4


def test_first_day_after_spike_is_skipped_and_breakdown_stops():
    cs = _flat(40)
    cs += [_c(1, 10000, 11300, 9950, 11200, 1_000_000), _c(2, 11100, 11150, 10500, 10600, 300_000),  # 급등 다음날 눌림 → 제외
           _c(3, 10500, 10600, 9800, 9900, 300_000)]                                                   # 급등 시가 아래 → 중단
    assert find_pullbacks(cs, "000001", "테스트", "테마") == []
