from app.supply import SupplyInputs, score


def test_neutral_when_no_signals():
    assert score(SupplyInputs(cttr=None, net_investor_eok=None, turnover_eok=0)) == (50, 0)


def test_strong_buying_everywhere_maxes_out():
    s, flow = score(SupplyInputs(cttr=250, net_investor_eok=150, turnover_eok=1000, five=200, prev_five=100))
    assert s == 100 and flow == 1


def test_strong_selling_floors():
    s, flow = score(SupplyInputs(cttr=0.1, net_investor_eok=-200, turnover_eok=1000, five=20, prev_five=100))
    assert s == 0 and flow == -1


def test_partial_signals_are_linear():
    # 체결강도 150 → +10, 순매수 5% → +10, 가속 없음
    s, flow = score(SupplyInputs(cttr=150, net_investor_eok=50, turnover_eok=1000))
    assert s == 70 and flow == 1


def test_flow_deadband():
    _, flow = score(SupplyInputs(cttr=None, net_investor_eok=3, turnover_eok=1000))  # 0.3% → 중립
    assert flow == 0
