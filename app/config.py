"""환경설정. .env 파일 또는 환경변수에서 읽는다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _broker_default() -> str:
    """BROKER 가 없으면 KIS_ENV 로 유추 (예전 설정 호환): mock → mock, vts/real → kis"""
    b = os.getenv("BROKER", "").lower()
    if b:
        return b
    return "mock" if os.getenv("KIS_ENV", "mock").lower() == "mock" else "kis"


@dataclass(frozen=True)
class Settings:
    broker: str = field(default_factory=_broker_default)  # mock | kis | kiwoom
    env: str = field(default_factory=lambda: os.getenv("KIS_ENV", "mock").lower())  # (KIS) mock | vts | real

    # ── 키움 REST API ──
    kiwoom_env: str = field(default_factory=lambda: os.getenv("KIWOOM_ENV", "paper").lower())  # paper(모의) | real
    kiwoom_app_key: str = field(default_factory=lambda: os.getenv("KIWOOM_APP_KEY", ""))
    kiwoom_app_secret: str = field(default_factory=lambda: os.getenv("KIWOOM_APP_SECRET", ""))
    kiwoom_rps: float = field(default_factory=lambda: float(os.getenv("KIWOOM_RPS", "4")))  # 초당 조회 수
    # 키움 거래대금 단위(원). 실시간 FID 14 · 일봉 trde_prica · 투자자 acc_trde_prica 모두 백만원 (문서·실응답으로 확인)
    kiwoom_amount_unit: int = field(default_factory=lambda: int(os.getenv("KIWOOM_AMOUNT_UNIT", "1000000")))
    app_key: str = field(default_factory=lambda: os.getenv("KIS_APP_KEY", ""))
    app_secret: str = field(default_factory=lambda: os.getenv("KIS_APP_SECRET", ""))
    account_no: str = field(default_factory=lambda: os.getenv("KIS_ACCOUNT_NO", ""))
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))

    # 테마 자동 수집 (네이버 금융). 0 이면 서버가 주기 수집을 하지 않는다 (버튼/스크립트로만).
    collect_minutes: int = field(default_factory=lambda: int(os.getenv("THEME_COLLECT_MINUTES", "0")))
    collect_top: int = field(default_factory=lambda: int(os.getenv("THEME_COLLECT_TOP", "12")))
    collect_per: int = field(default_factory=lambda: int(os.getenv("THEME_COLLECT_PER", "6")))

    # 리포트·뉴스 줄 갱신 주기(분). 0 이면 테마 수집 때만 채운다.
    news_minutes: int = field(default_factory=lambda: int(os.getenv("NEWS_MINUTES", "10")))
    report_days: int = field(default_factory=lambda: int(os.getenv("REPORT_DAYS", "7")))

    # 하루 한 번 자동 실행 (HH:MM, 비우면 끔). screener = 장 마감 후 고가놀이, collect = 장 전 테마 재수집+뉴스
    schedule_screener: str = field(default_factory=lambda: os.getenv("SCHEDULE_SCREENER", "15:45").strip())
    schedule_screener_universe: str = field(default_factory=lambda: os.getenv("SCHEDULE_SCREENER_UNIVERSE", "market").strip())
    schedule_collect: str = field(default_factory=lambda: os.getenv("SCHEDULE_COLLECT", "08:50").strip())

    # 야간 지표(야후 파이낸스) 수집 주기(분). 0 이면 서버가 수집하지 않는다.
    overnight_minutes: int = field(default_factory=lambda: int(os.getenv("OVERNIGHT_MINUTES", "5")))

    # 외인 선물 소스: naver (기본, 네이버 투자자별 매매동향·키 불필요) | kis (한국투자증권 API, 아래 설정 사용)
    fut_source: str = field(default_factory=lambda: os.getenv("FUT_SOURCE", "naver").lower())
    # 한국투자증권으로 조회할 때의 설정. app/kis/futures.py 참고. 문서와 다르면 여기서 바꾼다.
    fut_path: str = field(default_factory=lambda: os.getenv("KIS_FUT_PATH", "/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market"))
    fut_tr_id: str = field(default_factory=lambda: os.getenv("KIS_FUT_TR_ID", "FHPTJ04040000"))
    fut_params: str = field(default_factory=lambda: os.getenv("KIS_FUT_PARAMS", '{"FID_INPUT_ISCD": "F001", "FID_INPUT_DATE_1": "{today}"}'))
    fut_field: str = field(default_factory=lambda: os.getenv("KIS_FUT_FIELD", "frgn_ntby_qty"))

    themes_file: Path = ROOT / "themes.json"
    data_dir: Path = ROOT / "data"
    static_dir: Path = ROOT / "static"

    @property
    def is_mock(self) -> bool:
        return self.broker == "mock"

    @property
    def kiwoom_base(self) -> str:
        return "https://api.kiwoom.com" if self.kiwoom_env == "real" else "https://mockapi.kiwoom.com"

    @property
    def kiwoom_ws_url(self) -> str:
        host = "api.kiwoom.com" if self.kiwoom_env == "real" else "mockapi.kiwoom.com"
        return f"wss://{host}:10000/api/dostk/websocket"

    @property
    def kiwoom_token_cache(self) -> Path:
        return self.data_dir / f"kiwoom_token_{self.kiwoom_env}.json"

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
        if self.broker == "kiwoom":
            pairs = (("KIWOOM_APP_KEY", self.kiwoom_app_key), ("KIWOOM_APP_SECRET", self.kiwoom_app_secret))
        elif self.broker == "kis":
            pairs = (("KIS_APP_KEY", self.app_key), ("KIS_APP_SECRET", self.app_secret))
        else:
            raise RuntimeError(f"BROKER={self.broker} 는 모릅니다. mock | kis | kiwoom 중 하나여야 합니다.")
        missing = [k for k, v in pairs if not v]
        if missing:
            raise RuntimeError(f"BROKER={self.broker} 인데 {', '.join(missing)} 가 비어 있습니다. .env 를 확인하세요.")


settings = Settings()
