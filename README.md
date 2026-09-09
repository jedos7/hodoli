# 테마 레이더 — 백엔드 뼈대

한국투자증권 Open API(REST + WebSocket)로 시세를 받아 테마 활동도·수급을 계산하고,
`static/index.html` 프론트에 1초마다 방송하는 FastAPI 서버입니다.
API 키 없이도 `KIS_ENV=mock` 으로 모의 시세가 돌아갑니다.

## 구조

```
theme-radar/
├─ app/
│  ├─ config.py          .env → Settings (mock / vts / real 전환, 서버 주소)
│  ├─ state.py           장중 상태: 종목 시세 → 테마 지표(활동도·5분 유입·대장주·집중도)
│  ├─ supply.py          수급 점수: 체결강도 + 외인·기관 순매수 + 대금 가속
│  ├─ scheduler.py       하루 한 번 자동 실행 (장 마감 후 스크리너, 장 전 테마 수집)
│  ├─ collectors/
│  │  ├─ naver_theme.py  네이버 금융 테마 목록·구성 종목·편입 사유 수집
│  │  ├─ naver_daily.py  네이버 일봉 · 코스피/코스닥 종목 목록 (스크리너용)
│  │  ├─ naver_news.py   종목별 증권사 리포트 건수 · 최신 뉴스 (카드의 리포트 줄·뉴스 줄)
│  │  ├─ naver_futures.py 코스피200 선물 투자자별 매매동향 (외인 선물)
│  │  └─ overnight.py    야간 지표: 전일 20:05 대비 해외 지수·유가·환율 (야후 파이낸스)
│  ├─ feeds.py           MockFeed(랜덤워크) / KiwoomFeed / KisFeed (REST 초기값 + WebSocket 체결)
│  ├─ kiwoom/
│  │  ├─ auth.py         키움 접근토큰 (파일 캐시)
│  │  ├─ rest.py         ka10001 기본정보 · ka10081 일봉 · ka10059 투자자(현재가·거래대금·순매수)
│  │  ├─ ws.py           실시간 0B 체결 구독, LOGIN/REG/PING, 재접속
│  │  └─ parse.py        부호 붙은 숫자, 실시간 FID → Trade, 일봉·투자자 응답 파서
│  ├─ main.py            FastAPI: /api/*, /ws/stream, 정적 파일
│  ├─ kis/
│  │  ├─ auth.py         접근토큰(24h, 파일 캐시) · 웹소켓 접속키
│  │  ├─ rest.py         현재가 · 일봉 · 지수 조회 (호출 간격 제한 포함)
│  │  ├─ ws.py           실시간 체결 구독, PINGPONG, 재접속, 41종목 제한 분할
│  │  ├─ futures.py      코스피200 선물 외국인 순매수 (설정으로 거래 ID·필드 조정)
│  │  └─ parse.py        '0|H0STCNT0|n|a^b^c…' 텍스트 → Trade
│  └─ screener/
│     ├─ indicators.py   EMA · MACD
│     ├─ hoga_play.py    고가놀이 판정 + 3일 보유 백테스트
│     ├─ pullback.py     눌림목 판정 (눌림 폭·거래량 감소·20일선·저점 유지)
│     └─ runner.py       소스(naver/kis/mock)·범위(themes/market) 골라 실행 → data/hoga.json
├─ scripts/fetch_daily.py  장 마감 후 일봉 수집 → 스크리너 → data/hoga.json
├─ scripts/collect_themes.py  네이버 테마 수집 → themes.json
├─ scripts/fetch_overnight.py 야간 지표 1회 수집 → data/overnight.json
├─ scripts/probe_kis.py       KIS 조회 API 원본 응답 확인 (필드·코드 맞출 때)
├─ scripts/sweep_pullback.py  눌림목 조건 조합 비교 (저장된 일봉으로 승률·기준선 대비 표)
├─ static/index.html       프론트 (모의 피드 내장, 서버에서 열면 WebSocket 으로 전환)
├─ themes.json             테마 → 종목 매핑
└─ tests/                  파서 · 스크리너 단위 테스트
```

## 시작하기

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # 기본 KIS_ENV=mock
py -m pytest -q
py scripts\fetch_daily.py       # 고가놀이 결과 생성 (네이버 실제 일봉, 테마 종목)
uvicorn app.main:app --reload   # http://127.0.0.1:8000
```

브라우저에서 `http://127.0.0.1:8000` 을 열면 프론트가 `/ws/stream` 에 붙어 서버 계산값을 그립니다.
연결이 안 되면 프론트는 자체 모의 피드로 돌아갑니다 (상단 배지에 표시).

## 증권사 연결

`.env` 의 `BROKER` 로 고릅니다. `mock` 은 키 없이 가짜 시세, `kiwoom` 은 키움 REST API, `kis` 는 한국투자증권입니다.
시세를 읽기만 하며 주문 기능은 없습니다.

### 키움 REST API (BROKER=kiwoom) — 실제 접속으로 검증됨

1. https://openapi.kiwoom.com 로그인 → **IP 등록**: 서버를 돌릴 PC 의 현재 IP 를 추가합니다. 등록된 IP 에서만 인증이 됩니다 (집 인터넷은 IP 가 바뀔 수 있으니 인증 오류가 나면 먼저 확인).
2. 계좌를 API 사용 신청하고 App Key / App Secret 을 내려받습니다. **한 번만 내려받을 수 있고** 실전용·모의투자용이 따로입니다. 서로 바꿔 쓰면 오류 8030 이 납니다.
3. `.env` 에 `BROKER=kiwoom`, `KIWOOM_ENV=real`(실전 키) 또는 `paper`(모의투자 키), 키 두 줄을 넣고 서버를 재시작합니다.
4. 시작 시 종목마다 `ka10059` 한 번으로 현재가·전일종가·누적거래대금·외국인/기관 순매수(당일 잠정치)를 채우고, 이후 체결은 WebSocket `0B` 로 받습니다. 순매수는 2분마다 다시 받습니다.

| 항목 | 값 |
|---|---|
| REST | `https://api.kiwoom.com` (실전) · `https://mockapi.kiwoom.com` (모의) |
| WebSocket | `wss://api.kiwoom.com:10000/api/dostk/websocket` (모의는 mockapi) |
| 거래대금 단위 | 백만원 (실시간 FID 14 · 일봉 `trde_prica` · 투자자 `acc_trde_prica`) → `KIWOOM_AMOUNT_UNIT=1000000` |
| 체결강도 | 실시간 FID 228 |
| 일봉 | `ka10081` (스크리너 `--source kiwoom`) |

공식 명세·예제: https://github.com/Kiwoom-Securities/Kiwoom-REST-API (`kiwoom/_data/kiwoom_api_spec.json` 에 전체 항목 정의).
외인 선물 한 칸은 키움에 해당 API 가 없어 네이버 금융에서 받습니다 (아래 '외인 선물' 참고).

### 한국투자증권 (BROKER=kis)

1. https://apiportal.koreainvestment.com 에서 앱 등록 → APP KEY / SECRET 발급.
2. `.env` 에 `KIS_ENV=vts`(모의투자) 또는 `real`, 키 두 개 입력.
3. 서버 재시작. 시작 시 REST 로 전일 종가·현재가를 채우고, 이후 체결은 WebSocket 으로 옵니다.

| 항목 | 실전 | 모의투자 |
|---|---|---|
| REST | `https://openapi.koreainvestment.com:9443` | `https://openapivts.koreainvestment.com:29443` |
| WebSocket | `ws://ops.koreainvestment.com:21000` | `ws://ops.koreainvestment.com:31000` |
| 호출 제한 | 초당 20건 | 초당 2건 |
| 실시간 구독 | 접속당 약 41건 | 동일 |

접근토큰은 하루 1개가 원칙이라 `data/kis_token_*.json` 에 캐시합니다. 지우면 재발급합니다.

## 고가놀이 스크리너 (app/screener/)

"터뜨리고 버티며 저점을 높이는 자리". 완성된 일봉으로만 판정합니다 (`hoga_play.py`).

1. 급등봉: 종가 기준 +8% 이상, 거래대금 50억 이상.
2. 이후 1~3일 횡보: 종가가 급등봉 시가 아래로 무너지지 않고, 급등봉 고가 대비 -15% 이내.
3. 엄선 조건 (하나라도 어기면 탈락 사유로 기록): 횡보 폭 10% 미만, 저점 상승, MACD(12,26,9) 0선 위.
4. 3일 보유 수익률로 승률·건당 수익·기준선 대비를 냅니다.

**실제 일봉 소스와 범위** (`runner.py`)

| 옵션 | 값 | 설명 |
|---|---|---|
| `--source` | `naver` (기본) | 네이버 일봉. 키 불필요. 거래대금은 거래량×종가 근사 |
| | `kiwoom` | 키움 일봉 `ka10081` (정확한 거래대금, 65종목 17초) — 실접속 검증됨 |
| | `kis` | 한국투자증권 REST 일봉 (`KIS_ENV=vts/real`) |
| | `auto` | 증권사 피드가 켜져 있으면 그 증권사 일봉, 모의 모드면 네이버. 화면의 '다시 찾기' 가 이걸 쓴다 |
| | `mock` | 합성 일봉 (UI 확인용) |
| `--universe` | `themes` (기본) | themes.json 의 종목만. 수 초 |
| | `market` | 코스피+코스닥 중 오늘 거래대금 30억 이상 전 종목 (우선주·스팩·ETF 제외). 1~3분 |

```powershell
py scripts\fetch_daily.py                       # 네이버 일봉, 테마 종목
py scripts\fetch_daily.py --universe market     # 시장 전체
```

서버가 떠 있으면 고가놀이 창 오른쪽 위 **다시 찾기** 버튼(범위 선택 가능)으로 같은 일을 백그라운드로 돌리고, 진행률이 표시된 뒤 표가 갱신됩니다.
API 로는 `POST /api/screener/run?source=naver&universe=market`, 진행은 `GET /api/screener/status`.
장 마감 후 하루 한 번 돌리는 것이 맞고, 장중에 돌리면 오늘 봉이 미완성인 채로 들어가므로 '자리 잡은 날' 판정에 오늘은 쓰지 않는 게 안전합니다.

## 눌림목 스크리너 (app/screener/pullback.py)

"크게 오른 뒤 거래량 줄며 얕게 쉬는 자리". 고가놀이와 같은 일봉으로 한 번에 같이 계산되어 `data/pullback.json` 에 저장되고, 고가놀이 창의 **눌림목** 탭에서 봅니다.

1. 급등봉: 종가 +8% 이상, 거래대금 50억 이상.
2. 급등 2일 뒤부터 10일 안에서, 급등 이후 고점 대비 3% 이상 내려온 날이 자리 후보. 급등봉 시가 아래로 무너지면 끝.
3. 엄선 조건 (하나라도 어기면 탈락 사유 기록): 눌림 폭 고점 대비 -3 ~ -10%, 눌림 3일 이내, 눌리는 동안 평균 거래량이 급등일의 0.3배 이하, 종가가 20일선 위, 자리 날 저가가 급등봉 저가 위.
4. 3일 보유 수익률로 승률·건당·기준선 대비를 냅니다.

급등 첫날 눌림은 위험해서 후보에서 뺐습니다.

**조건 값의 근거 — 조합 비교 (scripts/sweep_pullback.py).** 일봉을 `data/candles_{source}_{universe}.json` 에 저장해 두고
눌림 폭·거래량 비율·눌림 일수·20일선·저점·급등 크기 값을 768개 조합으로 돌려 3일 보유 승률·건당·기준선 대비를 표로 냅니다.
2026-09-09 시장 전체 491종목 250일 결과: 얕게(-3~-10%) 짧게(1~3일) 거래량이 확 줄어든(0.3배 이하) 자리가 승률 48%, 건당 +1.9%,
기준선 대비 +1.0%p 로 전반·후반 모두 가장 좋았고, 깊은 눌림(-8~-20%)이나 거래량 조건이 없는 조합은 기준선을 밑돌았습니다.
조건 하나씩 보면 거래량 감소가 가장 크게 갈랐습니다 (0.3배 이하 +0.88% vs 나머지 -0.03%).

```powershell
py scripts\sweep_pullback.py                   # 캐시가 있으면 몇 초, 없으면 일봉을 받아 저장한 뒤 실행
py scripts\sweep_pullback.py --hold 5          # 5일 보유 기준
py scripts\fetch_daily.py --use-cache          # 저장된 일봉으로 스크리너만 다시 (조건을 고친 뒤)
```

시장이 바뀌면 결과도 바뀝니다. 분기에 한 번쯤 다시 돌려 값을 확인하는 것이 좋습니다.

## 하루 한 번 자동 실행 (app/scheduler.py)

서버가 떠 있는 동안 평일에 정해진 시각에 돌립니다. 시각은 `.env` 로 바꾸고, 비우면 끕니다.

| 작업 | 기본 | 하는 일 |
|---|---|---|
| `SCHEDULE_SCREENER` | 15:45 | 장 마감 후 확정 일봉으로 고가놀이 다시 찾기. 범위는 `SCHEDULE_SCREENER_UNIVERSE` (기본 market) |
| `SCHEDULE_COLLECT` | 08:50 | 장 시작 전 네이버 테마 재수집 + 리포트·뉴스 갱신 → 화면 즉시 반영 |

- 서버를 시각 뒤에 켜도 그날 아직 안 돌았으면 바로 따라잡습니다. 마지막 실행일은 `data/schedule.json` 에 남아 같은 날 두 번 돌지 않습니다.
- 상태는 `GET /api/schedule`, 화면 맨 아래 줄에도 "마지막 09/09 완료" 식으로 보입니다. 지금 바로 돌리려면 `POST /api/schedule/run/screener` 또는 `.../collect`.
- 서버를 켜 두지 않는 날은 돌지 않습니다. PC 를 켜 둘 수 없다면 Windows 작업 스케줄러에 `py scripts\fetch_daily.py --source kiwoom --universe market` 을 등록하는 방법도 있습니다.
- 공휴일에는 일봉이 새로 생기지 않으니 스크리너 결과가 전날과 같게 나옵니다. 오류는 아닙니다.

## 수급 점수 (app/supply.py)

0~100, 50 이 중립. 세 신호를 더하고, 가중치는 `WEIGHTS` 에서 바꿉니다.

| 신호 | 출처 | 만점 기준 | 가중치 |
|---|---|---|---|
| 체결강도 | 실시간 체결 `CTTR` (매수체결량/매도체결량×100) | 200 이상 +, 0 이면 − | ±20 |
| 외인·기관 순매수 | 장중 `HHPTJ04160200` 추정가집계, 안 되면 `FHKST01010900` 전일 확정치 | 순매수 / 당일 거래대금 = ±10% | ±20 |
| 대금 가속 | 테마 5분 유입 / 직전 5분 유입 | 1.5배 이상 +, 0.5배 이하 − | ±10 |

수급 방향(▲▼)은 외인+기관 순매수 부호이며, 거래대금의 0.5% 미만이면 중립입니다.
프론트에서 점수 칸에 마우스를 올리면 근거(체결강도, 외인·기관 금액, 출처)가 보입니다.
추정가집계는 모의투자 서버에서 지원되지 않을 수 있어, 첫 실패 시 자동으로 전일 확정치로 내려갑니다 (출처 "전일" 표시).

## 테마 자동 수집 (app/collectors/naver_theme.py)

네이버 금융 테마 페이지에서 등락률 상위 테마와 구성 종목을 읽어 `themes.json` 을 만듭니다.

- 테마는 등락률 순으로 훑어, 거래대금 10억 이상 종목이 3개 이상인 것만 채택합니다 (기본 12테마 × 6종목).
- 종목은 거래대금 순이며, 같은 종목이 여러 테마에 걸리면 순위가 높은 테마에만 넣습니다.
- 각 종목의 "테마 편입 사유"를 같이 저장해 화면에서 종목명에 마우스를 올리면 보입니다.
- 재료 등급은 등락률 4% 이상(강함)과 상승 종목 비율 70% 이상(넓음)의 조합으로 A/B/C 를 매깁니다.

실행 방법 세 가지.

```powershell
py scripts\collect_themes.py --dry-run          # 결과만 보기
py scripts\collect_themes.py                    # themes.json 갱신 (이전 파일은 themes.json.bak)
py scripts\collect_themes.py --merge            # 손으로 넣은 테마(id 가 nv 로 시작하지 않는 것)는 남기고 갱신
```

서버가 떠 있으면 화면의 **테마 수집** 버튼이나 `POST /api/collect` 로 같은 일을 하고, 피드를 다시 붙여 곧바로 화면에 반영됩니다.
`.env` 의 `THEME_COLLECT_MINUTES` 를 0 보다 크게 두면 평일 08:50~15:30 사이에 그 주기로 자동 수집합니다.
손으로 `themes.json` 을 고쳤을 때는 `POST /api/themes/reload` 로 다시 읽습니다.

비공식 페이지를 읽는 것이라 구조가 바뀌면 `parse_theme_list` / `parse_theme_detail` 만 고치면 됩니다. 실패하면 이전 `themes.json` 이 그대로 유지됩니다.

## 리포트 줄 · 뉴스 줄 (app/collectors/naver_news.py)

테마 카드의 두 줄을 실제 데이터로 채웁니다.

- **리포트 줄**: 테마 종목 전체의 최근 `REPORT_DAYS`(기본 7)일 증권사 리포트를 네이버 금융 리서치 목록에서 세어 "리포트 7일 3건 · 증권사 2곳 · 최근 미래에셋 09/07" 형식으로 씁니다. 없으면 "리포트 7일 없음".
- **뉴스 줄**: 등락률 1위 종목(없으면 다음 종목)의 최신 기사 제목을 네이버 종목 뉴스에서 가져와 "종목명 — 제목" 으로 쓰고, 클릭하면 기사로 갑니다. 마우스를 올리면 기사 시각이 보입니다.
  테마 종목 전체의 최근 6건씩을 모아 점수를 매깁니다. 제목에 종목명 +2, 테마 핵심어 하나당 +2(최대 +4), 시황·마감 패턴(`MARKET_WRAP`: 시황, 코스피, 증시, 마감, 이 시각 등)은 제외.
  테마 핵심어는 테마명 토큰(예: 광통신, 광케이블, 광섬유)과 편입 사유에서 2종목 이상 겹치는 낱말입니다 (`theme_keywords`).
  3일(`FRESH_DAYS`) 안의 '관련' 기사만 후보입니다. 관련 = 핵심어가 제목에 낱말로 있거나, 종목명과 재료 단서(급등·수주·계약·목표가·진출 등 `CUE`)가 함께 있는 것.
  점수가 같으면 대장주 → 다음 종목, 그다음 최신 순이고, 후보가 없으면 "테마 관련 기사 없음" 으로 표시합니다 (종목명만 있는 봉사활동·인사 기사는 쓰지 않습니다).
- 테마 수집 때 함께 채우고, 서버가 `NEWS_MINUTES`(기본 10분) 주기로 다시 갱신해 화면과 `themes.json` 에 반영합니다. 즉시 갱신은 `POST /api/news/refresh`.
- 테마당 요청은 종목 수 + 1 이라 12테마 기준 70~80건, 15초 안팎입니다.

## 야간 지표 (app/collectors/overnight.py)

"전일 20:05 (NXT 마감) 대비" 표입니다. 나스닥, 코스피200, WTI, 브렌트, 환율, SK하이닉스 GDR, VIX, 달러지수, 금,
필라델피아 반도체, SMH, 마이크론, TSMC, 키옥시아, 샌디스크, 대만 가권을 야후 파이낸스 차트 API 에서 받습니다.

- 기준값은 저장해 두지 않고, 15분봉 이력에서 "가장 최근 20:05 KST 이전 마지막 봉"을 뽑습니다. 서버가 20:05 에 떠 있지 않아도 되고 재시작해도 같은 값입니다.
- 미국 프리장·애프터장 봉을 포함하므로(`includePrePost`) 장 시작 전에도 나스닥·개별주가 움직입니다.
- 장이 닫힌 지수(코스피200, 대만 가권, 키옥시아)는 기준값과 지금이 같은 종가라 0.00% 로 나옵니다.
- SK하이닉스 미국 ADR 은 야후에 없어 슈투트가르트 GDR(유로) 을 씁니다.
- 서버는 `OVERNIGHT_MINUTES`(기본 5분) 주기로 갱신하고 `data/overnight.json` 에도 씁니다. `POST /api/overnight/refresh` 로 즉시 갱신, `py scripts\fetch_overnight.py` 로 단독 실행.
- 프론트는 서버 피드일 때 1분마다 `/api/overnight` 를 읽고, 표 머리에 기준 시각과 갱신 시각을 보여줍니다.

### 외인 선물 (app/collectors/naver_futures.py)

코스피200 선물 외국인 순매수(계약 수)입니다. 표에는 전일 값(확정), 당일 값(장중 잠정), 차이(계약)가 나옵니다.
기본 소스는 네이버 금융 '투자자별 매매동향 · 선물' 페이지라 증권사·키와 무관하게 동작합니다. 키움 REST API 명세에는 선물·옵션 분류가 없어 키움에서는 받을 수 없습니다.
`FUT_SOURCE=kis` 로 바꾸면 한국투자증권 API(`app/kis/futures.py`)로 조회하며, 그 설정은 **추정치**라 아래로 원본 응답을 보고
`.env` 의 `KIS_FUT_PATH / KIS_FUT_TR_ID / KIS_FUT_PARAMS / KIS_FUT_FIELD` 를 맞추세요.

```powershell
py scripts\probe_kis.py --quote 005930     # 키·토큰 점검
py scripts\probe_kis.py                    # 현재 설정으로 외인 선물 조회, 원본 응답 출력
```

응답 목록의 첫 행을 당일, 둘째 행을 전일로 해석하며, 필드가 없으면 `frgn_shnu_vol - frgn_seln_vol` 로 계산합니다. 실패하면 표에 오류 사유가 툴팁으로 남고 다른 지표는 정상 갱신됩니다.

## 아직 비어 있는 것

- **주문**: 의도적으로 넣지 않았습니다. 필요하면 `app/kis/order.py` 로 분리하고 `KIS_ACCOUNT_NO` 를 씁니다.

## 필드 참고 (H0STCNT0 실시간 체결)

인덱스 0 종목코드 · 1 체결시간 · 2 현재가 · 3 전일대비부호 · 4 전일대비 · 5 등락률 · 7 시가 · 8 고가 · 9 저가 · 12 체결량 · 13 누적거래량 · 14 누적거래대금(원).
필드 순서는 KIS 포털의 최신 문서로 한 번 더 확인하세요.
