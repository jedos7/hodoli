"""보조지표. 외부 라이브러리 없이 순수 파이썬."""
from __future__ import annotations


def ema(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def adx(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> list[float | None]:
    """와일더 ADX. 추세 강도(방향 무관). 25 이상이면 추세, 30 이상이면 강한 추세로 본다. 앞 2n 봉은 None."""
    m = len(closes)
    if m < 2 * n + 1:
        return [None] * m
    tr, pdm, mdm = [0.0] * m, [0.0] * m, [0.0] * m
    for i in range(1, m):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm[i] = up if up > dn and up > 0 else 0.0
        mdm[i] = dn if dn > up and dn > 0 else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    out: list[float | None] = [None] * m
    atr, spdm, smdm = sum(tr[1 : n + 1]), sum(pdm[1 : n + 1]), sum(mdm[1 : n + 1])
    dxs: list[float] = []
    adx_val: float | None = None
    for i in range(n + 1, m):
        atr = atr - atr / n + tr[i]
        spdm = spdm - spdm / n + pdm[i]
        smdm = smdm - smdm / n + mdm[i]
        pdi = 100 * spdm / atr if atr else 0.0
        mdi = 100 * smdm / atr if atr else 0.0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0
        if adx_val is None:
            dxs.append(dx)
            if len(dxs) == n:
                adx_val = sum(dxs) / n
        else:
            adx_val = (adx_val * (n - 1) + dx) / n
        out[i] = adx_val
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float], list[float]]:
    """(macd 선, 시그널 선, 히스토그램). 길이는 closes 와 같다."""
    if not closes:
        return [], [], []
    f, s = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(f, s)]
    sig = ema(line, signal)
    hist = [a - b for a, b in zip(line, sig)]
    return line, sig, hist
