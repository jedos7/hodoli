from app.kiwoom.parse import inum, num, parse_basic, parse_daily, parse_investor, parse_real


def test_signed_numbers():
    assert num("+71,500") == 71500 and num("-0.42") == -0.42 and num("--1200") == -1200 and num("") == 0 and num(None, 5) == 5
    assert inum("+300") == 300 and inum("-300") == -300


def test_parse_real_0b():
    msg = {"trnm": "REAL", "data": [{"type": "0B", "item": "005930", "values": {
        "10": "+71500", "11": "+300", "12": "+0.42", "13": "123456", "14": "8800", "15": "-10",
        "16": "71200", "17": "71800", "18": "71000", "20": "093012", "228": "132.55"}},
        {"type": "0D", "item": "005930", "values": {}}]}
    ts = parse_real(msg, amount_unit=1_000_000)
    assert len(ts) == 1
    t = ts[0]
    assert t.code == "005930" and t.price == 71500 and t.change == 300 and t.change_rate == 0.42
    assert t.volume == 10 and t.acc_volume == 123456 and t.acc_amount == 8800 * 1_000_000 and t.cttr == 132.55
    assert t.time == "093012" and t.open == 71200 and t.high == 71800 and t.low == 71000
    assert parse_real({"trnm": "PING"}) == []


def test_parse_basic_and_prev_close():
    b = parse_basic({"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "-71500", "pred_pre": "-300", "flu_rt": "-0.42",
                     "trde_qty": "123456", "open_pric": "+71200", "high_pric": "+71800", "low_pric": "-71000"})
    assert b.price == 71500 and b.change == -300 and b.prev_close == 71800 and b.change_rate == -0.42 and b.amount == 0


def test_parse_daily_sorted_oldest_first():
    body = {"stk_dt_pole_chart_qry": [
        {"dt": "20260909", "cur_prc": "71500", "open_pric": "71200", "high_pric": "71800", "low_pric": "71000", "trde_qty": "100", "trde_prica": "7"},
        {"dt": "20260908", "cur_prc": "71000", "open_pric": "70000", "high_pric": "71500", "low_pric": "69900", "trde_qty": "90", "trde_prica": "6"},
    ]}
    cs = parse_daily(body, amount_unit=1_000_000)
    assert [c.date for c in cs] == ["20260908", "20260909"] and cs[1].close == 71500 and cs[1].amount == 7_000_000


def test_parse_investor_real_shape():
    # 2026-09-09 실응답: 금액 단위 백만원
    body = {"stk_invsr_orgn": [{"dt": "20260909", "cur_prc": "-268500", "pre_sig": "5", "pred_pre": "-1000", "flu_rt": "-37",
                                "acc_trde_qty": "13451782", "acc_trde_prica": "3648859", "ind_invsr": "0", "frgnr_invsr": "136412", "orgn": "211366"}]}
    r = parse_investor(body, amount_unit=1_000_000)
    assert r["date"] == "20260909" and r["price"] == 268500 and r["prev_close"] == 269500
    assert r["acc_amount"] == 3_648_859_000_000 and r["frgn"] == 136_412_000_000 and r["orgn"] == 211_366_000_000 and r["prsn"] == 0
    assert parse_investor({"stk_invsr_orgn": []}) is None
