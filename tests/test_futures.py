from app.kis.futures import extract_foreign_net


def test_extract_with_direct_field():
    body = {"rt_cd": "0", "output": [
        {"stck_bsop_date": "20260909", "frgn_ntby_qty": "-8107", "orgn_ntby_qty": "1200"},
        {"stck_bsop_date": "20260908", "frgn_ntby_qty": "3,420"},
    ]}
    r = extract_foreign_net(body)
    assert (r.today, r.prev) == (-8107, 3420) and r.today_date == "20260909" and r.error is None


def test_extract_from_buy_sell_volumes():
    body = {"output2": [{"frgn_shnu_vol": "10500", "frgn_seln_vol": "12000"}]}
    r = extract_foreign_net(body, field="nope")
    assert r.today == -1500 and r.prev is None


def test_extract_errors():
    assert extract_foreign_net({"output": []}).error
    assert "찾지 못했습니다" in extract_foreign_net({"output": [{"x": "1"}]}).error
