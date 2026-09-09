"""환경설정. .env 파일 또는 환경변수에서 읽는다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: os.getenv("KIS_ENV", "mock").lower())  # mock | vts | real
    app_key: str = field(default_factory=lambda: os.getenv("KIS_APP_KEY", ""))
    app_secret: str = field(default_factory=lambda: os.getenv("KIS_APP_SECRET", ""))
    account_no: str = field(default_factory=lambda: os.getenv("KIS_ACCOUNT_NO", ""))
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))

    # 테마 자동 수집 (네이버 금융). 0 이면 서버가 주기 수집을 하지 않는다 (버튼/스크립트로만).
    collect_minutes: int = field(default_factory=lambda: int(os.getenv("THEME_COLLECT_MINUTES", "0")))
    collect_top: int = field(default_factory=lambda: int(os.getenv("THEME_COLLECT_TOP", "12")))
    collect_per: int = field(default_factory=lambda: int(os.getenv("THEME_COLLECT_PER", "6")))

    # 야간 지표(야후 파이낸스) 수집 주기(분). 0 이면 서버가 수집하지 않는다.
    overnight_minutes: int = field(default_factory=lambda: int(os.getenv("OVERNIGHT_MINUTES", "5")))

    # 외인 선물 (코스피200 선물 외국인 순매수) 조회 설정. app/kis/futures.py 참고. 문서와 다르면 여기서 바꾼다.
    fut_path: str = field(default_factory=lambda: os.getenv("KIS_FUT_PATH", "/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market"))
    fut_tr_id: str = field(default_factory=lambda: os.getenv("KIS_FUT_TR_ID", "FHPTJ04040000"))
    fut_params: str = field(default_factory=lambda: os.getenv("KIS_FUT_PARAMS", '{"FID_INPUT_ISCD": "F001", "FID_INPUT_DATE_1": "{today}"}'))
    fut_field: str = field(default_factory=lambda: os.getenv("KIS_FUT_FIELD", "frgn_ntby_qty"))

    themes_file: Path = ROOT / "themes.json"
    data_dir: Path = ROOT / "data"
    static_dir: Path = ROOT / "static"

    @property
    def is_mock(self) -> bool:
        return self.env == "mock"

    @property
    def rest_base(self) -> str:
        if self.env == "real":
            return "https://openapi.koreainvestment.com:9443"
        return "https://openapivts.koreainvestment.com:29443"

    @property
    def ws_url(self) -> str:
        # 실시간 체결/호가는 실전 21000, 모의투자 31000 포트
        if self.env == "real":
            return "ws://ops.koreainvestment.com:21000"
        return "ws://ops.koreainvestment.com:31000"

    @property
    def token_cache(self) -> Path:
        return self.data_dir / f"kis_token_{self.env}.json"

    def validate(self) -> None:
        if self.is_mock:
            return
        missing = [k for k, v in (("KIS_APP_KEY", self.app_key), ("KIS_APP_SECRET", self.app_secret)) if not v]
        if missing:
            raise RuntimeError(f"KIS_ENV={self.env} 인데 {', '.join(missing)} 가 비어 있습니다. .env 를 확인하세요.")


settings = Settings()
