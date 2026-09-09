"""네이버 테마를 수집해 themes.json 을 갱신한다.

  py scripts/collect_themes.py                # 상위 12테마 × 6종목
  py scripts/collect_themes.py --top 8 --per 5
  py scripts/collect_themes.py --dry-run      # 파일에 쓰지 않고 결과만 출력
  py scripts/collect_themes.py --merge        # 기존 themes.json 의 수동 테마(id 가 nv 로 시작하지 않는 것)는 남긴다

서버가 떠 있으면 POST /api/collect 로 같은 일을 하고 곧바로 반영된다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.collectors.naver_theme import collect  # noqa: E402
from app.config import settings  # noqa: E402


def merge_manual(new: dict, path: Path) -> dict:
    if not path.exists():
        return new
    old = json.loads(path.read_text("utf-8"))
    manual = [t for t in old.get("themes", []) if not str(t.get("id", "")).startswith("nv")]
    taken = {s["code"] for t in new["themes"] for s in t["stocks"]}
    for t in manual:
        t["stocks"] = [s for s in t["stocks"] if s["code"] not in taken]
        if len(t["stocks"]) >= 2:
            new["themes"].append(t)
    return new


def write_themes(data: dict, path: Path) -> None:
    if path.exists():
        shutil.copy(path, path.with_suffix(".json.bak"))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=settings.collect_top)
    ap.add_argument("--per", type=int, default=settings.collect_per)
    ap.add_argument("--min-amount", type=float, default=10.0, help="종목 최소 거래대금(억)")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=str(settings.themes_file))
    args = ap.parse_args()

    data = await collect(top=args.top, per=args.per, min_amount_eok=args.min_amount)
    out = Path(args.out)
    if args.merge:
        data = merge_manual(data, out)
    for t in data["themes"]:
        print(f"{t['name']:<28} {t.get('chg', 0):+6.2f}%  {t['grade']}  " + ", ".join(s["name"] for s in t["stocks"]))
    print(f"\n{len(data['themes'])}개 테마 · {sum(len(t['stocks']) for t in data['themes'])}종목 · {data['collected_at']}")
    if args.dry_run:
        return
    write_themes(data, out)
    print(f"→ {out} (이전 파일은 {out.with_suffix('.json.bak').name})")


if __name__ == "__main__":
    asyncio.run(main())
