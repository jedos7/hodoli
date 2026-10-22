"""다섯 매매법이 같이 쓰는 지갑 (app/wallet.py). 합쳐서 999만 원을 넘지 않는다."""
from types import SimpleNamespace

from app.wallet import GLOBAL_CAP, NO_WALLET, Wallet


def engine(cap, spent):
    return SimpleNamespace(settings={'total_cap': cap}, committed=lambda: spent)


def test_room_is_the_smaller_of_own_cap_and_the_shared_cap():
    a, b = engine(3_000_000, 1_000_000), engine(3_000_000, 2_000_000)
    w = Wallet(cap=4_000_000).register(a, b)
    assert w.spent() == 3_000_000
    assert w.room(a) == 1_000_000       # 제 상한은 200만 남았지만 전체는 100만뿐
    assert w.room(b) == 1_000_000


def test_own_cap_still_binds_when_the_shared_cap_is_loose():
    a = engine(1_000_000, 900_000)
    w = Wallet(cap=GLOBAL_CAP).register(a)
    assert w.room(a) == 100_000


def test_five_engines_cannot_exceed_the_ten_million_rule():
    """3백만짜리 다섯이 동시에 차도 합은 999만을 넘지 않는다 (예전에는 1,500만까지 갈 수 있었다)."""
    engines = [engine(3_000_000, 2_000_000) for _ in range(5)]
    w = Wallet().register(*engines)
    assert w.spent() == 10_000_000
    assert all(w.room(e) == 0 for e in engines)


def test_room_is_never_negative():
    a = engine(3_000_000, 5_000_000)
    assert Wallet().register(a).room(a) == 0


def test_a_broken_engine_does_not_hide_the_others():
    def boom():
        raise RuntimeError('저장 상태 확인 필요')
    bad = SimpleNamespace(settings={'total_cap': 1}, committed=boom)
    good = engine(3_000_000, 1_000_000)
    w = Wallet().register(bad, good)
    assert w.spent() == 1_000_000       # 읽을 수 있는 것만 세고 멈추지 않는다


def test_no_wallet_only_looks_at_the_engines_own_cap():
    a = engine(3_000_000, 1_000_000)
    assert NO_WALLET.room(a) == 2_000_000 and NO_WALLET.spent() == 0
