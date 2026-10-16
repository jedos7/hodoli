# 장중 접속 시 장 상태 대기 수정

2026-09-23. 연결은 정상이나 장 상태 알림(0s)이 없어 자동매도가 장 상태 대기 중인 경우를 처리한다.

## 판단 근거

키움 공식 API 명세의 주식체결(0B) FID 290은 1=장전 시간외, 2=장중, 3=장후 시간외다.
장시작시간(0s)은 장 시작/종료 등의 전환 알림이므로 장중 신규 연결에서 알림을 받지 못할 수 있다.

출처: https://raw.githubusercontent.com/Kiwoom-Securities/Kiwoom-REST-API/main/kiwoom/_data/kiwoom_api_spec.json

## 변경

- 당일 0s 알림이 없으면 KRX 실시간 0B의 290=2와 10초 이내 실제 체결을 함께 확인해 장중 상태를 제공한다.
- 주중 09:00~15:20 범위에서만 보완한다. 시계만으로 장을 열거나 종가 동시호가를 추정하지 않는다.
- 가격과 체결량이 양수인 KRX 종목만 인정한다. NXT/통합/다른 거래소, 누락된 장 구분, REST 가격, 차트 자료, 과거/미래 시세는 사용하지 않는다.
- 당일 0s가 있으면 그 값이 우선이다. 닫힘/장전/동시호가 알림을 체결로 뒤집지 않는다.
- 연결 종료·재접속 시 근거를 초기화하고 10초가 지난 보완값은 만료한다.
- /api/health의 realtime.marketSession에서 판단 값/시각/source를 확인할 수 있다.
- 감시 중단 사유가 주기적 확인 과정에서 일반적인 ‘꺼짐’ 문구로 덮이지 않도록 한다.
- 저장된 감시를 자동으로 켜지 않는다. 새 활성화는 사용자가 직접 최종 확인한다.

## 검증

가짜 시세/가짜 주문을 사용한 test_intraday_session, test_kiwoom_ws_safety,
test_exit_tickets, test_autoexit, test_auto_buy, test_custom_exit,
test_profit_protection, test_autosell: 343 passed.

85,900원 이하 340주 조건은 켠 경우 정확히 한 번 주문하고, 꺼진 경우에는 주문하지 않는 것을 검증했다.
실제 매도 체결 시험은 수행하지 않는다.
