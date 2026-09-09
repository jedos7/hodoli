"""KIS 조회 API 원본 응답을 그대로 출력한다. 필드명·시장 코드를 맞출 때 쓴다.

  py scripts/probe_kis.py                                  # .env 의 KIS_FUT_* 설정으로 외인 선물 조회
  py scripts/probe_kis.py --tr FHPTJ04030000 --path /uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market \
                          --params '{"FID_INPUT_ISCD_1":"F001","FID_INPUT_ISCD_2":"0001"}'
  py scripts/probe_kis.py --quote 005930                    # 현재가 조회로 키·토큰이 정상인지 확인

KIS_ENV 가 mock 이면 실행할 수 없다 (.env 에 vts 또는 real 과 키가 필요).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import settings  # noqa: E402
from app.kis.auth import KisAuth  # noqa: E402
from app.kis.futures import KisFutures, extract_foreign_net  # noqa: E402
from app.kis.rest import KisRest  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tr")
    ap.add_argument("--path")
    ap.add_argument("--params", help="JSON 문자열")
    ap.add_argument("--quote", help="종목코드: 현재가 조회로 연결 점검")
    args = ap.parse_args()

    if settings.is_mock:
        print("KIS_ENV=mock 입니다. .env 에 vts/real 과 APP KEY·SECRET 을 넣으세요.")
        return
    settings.validate()
    rest = KisRest(settings, KisAuth(settings))
    try:
        if args.quote:
            print(json.dumps(asdict(await rest.quote(args.quote)), ensure_ascii=False, indent=1))
            return
        fut = KisFutures(rest, settings)
        path, tr = args.path or settings.fut_path, args.tr or settings.fut_tr_id
        params = json.loads(args.params) if args.params else fut.params()
        print(f"GET {path}  tr_id={tr}\nparams={json.dumps(params, ensure_ascii=False)}\n")
        body = await rest._get(path, tr, params)
        print(json.dumps(body, ensure_ascii=False, indent=1)[:6000])
        r = extract_foreign_net(body, settings.fut_field)
        print("\n→ 해석:", r)
    finally:
        await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
