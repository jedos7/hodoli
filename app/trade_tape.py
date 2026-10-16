"""Reference-only signed execution tape; never submits orders."""
import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta


class TradeTape:
    def __init__(self, root):
        self.root = root
        self.pending = defaultdict(list)
        self.lock = threading.RLock()
        self.cache = {}

    def add(self, day, code, stamp, price, signed):
        with self.lock:
            self.pending[(day, code)].append(dict(t=stamp, p=price, v=signed))

    def flush(self):
        with self.lock:
            for (day, code), rows in list(self.pending.items()):
                if not rows:
                    continue
                path = self.root / day / (code + '.jsonl')
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = ''.join(json.dumps(r, separators=(',', ':'))+'\n' for r in rows).encode()
                with path.open('ab+') as stream:
                    start = stream.tell()
                    try:
                        stream.write(payload)
                        stream.flush()
                    except OSError:
                        stream.truncate(start)
                        raise
                self.pending.pop((day, code))

    def read(self, code, start, end):
        with self.lock:
            self.flush()
            first = datetime.strptime(start[:8], '%Y%m%d')
            last = datetime.strptime(end[:8], '%Y%m%d')
            result = defaultdict(lambda: [0, 0])
            days = []
            for i in range((last-first).days+1):
                day = (first+timedelta(days=i)).strftime('%Y%m%d')
                path = self.root / day / (code+'.jsonl')
                if not path.exists():
                    continue
                size = path.stat().st_size
                key = (day, code)
                cached = self.cache.get(key)
                disk = path.with_suffix('.cache.json')
                if cached is None:
                    try:
                        cached = json.loads(disk.read_text('utf-8'))
                    except (OSError, ValueError):
                        cached = dict(offset=0, minutes={})
                if cached['offset'] > size:
                    cached = dict(offset=0, minutes={})
                if cached['offset'] != size:
                    with path.open('rb') as stream:
                        stream.seek(cached['offset'])
                        for line in stream:
                            row = json.loads(line)
                            levels = cached['minutes'].setdefault(row['t'][:4], {})
                            amounts = levels.setdefault(str(row['p']), [0, 0])
                            amounts[0 if row['v'] > 0 else 1] += abs(row['v'])
                        cached['offset'] = stream.tell()
                    temp = disk.with_suffix('.tmp')
                    temp.write_text(json.dumps(cached, separators=(',', ':')), 'utf-8')
                    temp.replace(disk)
                self.cache[key] = cached
                if len(self.cache) > 64:
                    self.cache.pop(next(iter(self.cache)))
                count = 0
                for minute, levels in cached['minutes'].items():
                    if start[:12] <= day+minute <= end[:12]:
                        for price, amounts in levels.items():
                            dest = result[int(price)]
                            dest[0] += amounts[0]
                            dest[1] += amounts[1]
                            count += sum(amounts)
                if count:
                    days.append(day)
            return dict(levels=[dict(price=p, buy=q[0], sell=q[1]) for p,q in sorted(result.items())],
                        days=days, source='실제 체결 · KRX 정규장 수신분만', partial=True)
