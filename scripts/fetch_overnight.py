"""야간 지표를 한 번 수집해 data/overnight.json 에 쓴다. 서버가 떠 있으면 주기 수집을 알아서 한다.

  py scripts/fetch_overnight.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.collectors.overnight import collect  # noqa: E402
from app.config import settings  # noqa: E402


async def main() -> None:
    d = await collect()
    print(f"기준 {d['base_at']} · 수집 {d['asof']}")
    for r in d["rows"]:
        if r["chg"] is None:
            print(f"  {r['name']:<12} –  ({r['error']})")
        else:
            f = f"{{:,.{r['digits']}f}}"
            print(f"  {r['name']:<12} {f.format(r['prev']):>12} → {f.format(r['now']):>12}  {r['chg']:+.2f}%")
    for n in d["notes"]:
        print("  ·", n)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    out = settings.data_dir / "overnight.json"
    out.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
