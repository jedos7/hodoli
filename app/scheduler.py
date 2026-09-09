"""하루 한 번 정해진 시각에 작업을 돌리는 간단한 스케줄러. 서버가 떠 있는 동안만 동작한다.

  작업          기본 시각   하는 일
  screener      15:45      장 마감 후 확정 일봉으로 고가놀이 다시 찾기 (증권사 일봉, 시장 전체)
  collect       08:50      장 시작 전 네이버 테마 재수집 + 리포트·뉴스 갱신

규칙
  - 평일만. 시각이 지났는데 오늘 아직 안 돌았으면 돈다 (서버를 늦게 켜도 따라잡는다).
  - 마지막 실행일을 data/schedule.json 에 적어 두어 재시작해도 같은 날 두 번 돌지 않는다.
  - 시각은 .env 의 SCHEDULE_SCREENER / SCHEDULE_COLLECT (HH:MM, 비우면 끔).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    at: str                                  # "HH:MM", "" 이면 끔
    run: Callable[[], Awaitable[dict]]
    weekdays_only: bool = True
    last_date: str = ""                      # YYYY-MM-DD
    last_result: dict = field(default_factory=dict)
    running: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.at)


def is_due(job: Job, now: datetime) -> bool:
    if not job.enabled or job.running:
        return False
    if job.weekdays_only and now.weekday() >= 5:
        return False
    today = now.strftime("%Y-%m-%d")
    if job.last_date == today:
        return False
    return now.strftime("%H:%M") >= job.at


class Scheduler:
    def __init__(self, jobs: list[Job], state_file: Path, interval: float = 30.0):
        self.jobs = jobs
        self.state_file = state_file
        self.interval = interval
        self._load()

    def _load(self) -> None:
        try:
            d = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for j in self.jobs:
            s = d.get(j.name) or {}
            j.last_date, j.last_result = s.get("last_date", ""), s.get("last_result", {})

    def _save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({j.name: {"last_date": j.last_date, "last_result": j.last_result} for j in self.jobs},
                                                  ensure_ascii=False, indent=1), "utf-8")
        except OSError as e:
            log.warning("스케줄 상태 저장 실패: %s", e)

    async def run_job(self, job: Job, force: bool = False) -> dict:
        if job.running:
            return {"ok": False, "error": "이미 실행 중"}
        job.running = True
        started = datetime.now()
        try:
            r = await job.run()
            job.last_result = {"ok": True, "at": started.isoformat(timespec="seconds"), "took": round((datetime.now() - started).total_seconds()), **r}
        except Exception as e:
            log.exception("스케줄 작업 실패: %s", job.name)
            job.last_result = {"ok": False, "at": started.isoformat(timespec="seconds"), "error": str(e)}
        finally:
            job.running = False
            job.last_date = started.strftime("%Y-%m-%d")
            self._save()
        log.info("스케줄 %s 완료: %s", job.name, job.last_result)
        return job.last_result

    async def loop(self) -> None:
        log.info("스케줄: %s", ", ".join(f"{j.name} {j.at}" for j in self.jobs if j.enabled) or "없음")
        while True:
            now = datetime.now()
            for j in self.jobs:
                if is_due(j, now):
                    log.info("스케줄 %s 시작 (%s)", j.name, j.at)
                    await self.run_job(j)
            await asyncio.sleep(self.interval)

    def status(self) -> dict:
        now = datetime.now()
        return {j.name: {"at": j.at or None, "enabled": j.enabled, "running": j.running, "lastDate": j.last_date,
                         "lastResult": j.last_result, "dueToday": is_due(j, now)} for j in self.jobs}
