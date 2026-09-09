from app.kis.parse import parse_trades


def _rec(code="005930", price="71500", sign="2", chg="300", rate="0.42", vol="10", acc_vol="123456", acc_amt="8800000000", cttr="132.55"):
    f = [code, "093012", price, sign, chg, rate, "71234", "71200", "71800", "71000", "71600", "71500", vol, acc_vol, acc_amt]
    f += ["120", "150", "30", cttr, "60000", "63456", "1", "51.4"]  # 15~22: 체결건수·체결강도·총매도/매수·체결구분·매수비율
    f += ["0"] * 22  # 뒤쪽 필드는 쓰지 않는다
    return "^".join(f)


def test_cttr_and_totals():
    x = parse_trades("0|H0STCNT0|001|" + _rec(cttr="187.2"))[0]
    assert x.cttr == 187.2 and x.buy_total == 63456 and x.sell_total == 60000


def test_single_record():
    t = parse_trades("0|H0STCNT0|001|" + _rec())
    assert len(t) == 1
    x = t[0]
    assert x.code == "005930" and x.price == 71500 and x.change == 300 and x.change_rate == 0.42
    assert x.acc_amount == 8_800_000_000 and abs(x.acc_amount_eok - 88.0) < 1e-9


def test_multi_record_and_sign():
    raw = "0|H0STCNT0|002|" + _rec() + "^" + _rec(code="000660", price="180000", sign="5", chg="1500", rate="-0.83")
    t = parse_trades(raw)
    assert [x.code for x in t] == ["005930", "000660"]
    assert t[1].change == -1500


def test_ignores_other_tr():
    assert parse_trades("0|H0STASP0|001|005930^...") == []
    assert parse_trades('{"header":{"tr_id":"PINGPONG"}}') == []
