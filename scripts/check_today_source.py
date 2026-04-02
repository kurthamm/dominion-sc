#!/usr/bin/env python3
"""Check whether today/yesterday statistics rows came from recorder compilation vs. external import."""
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else 'ha_config/home-assistant_v2.db'

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row
cur = con.cursor()

now = datetime.now(timezone.utc)
yesterday_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
tomorrow_end = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)

ys_ts = int(yesterday_start.timestamp())
te_ts = int(tomorrow_end.timestamp())

print(f"Checking statistics rows from {yesterday_start.date()} to {tomorrow_end.date()}")
print()

# Check BOTH statistics (recorder-compiled) and statistics_short_term
for table in ['statistics', 'statistics_short_term']:
    print(f"=== {table} ===")
    cur.execute(f'''
        SELECT sm.statistic_id, sm.source, s.start_ts, s.state, s.sum, s.mean
        FROM {table} s
        JOIN statistics_meta sm ON s.metadata_id = sm.id
        WHERE s.start_ts >= ? AND s.start_ts <= ?
          AND (sm.statistic_id LIKE '%dominionsc%'
               OR sm.statistic_id LIKE '%electric_cumulative%'
               OR sm.statistic_id LIKE '%gas_cumulative%')
        ORDER BY sm.statistic_id, s.start_ts
    ''', (ys_ts, te_ts))
    rows = cur.fetchall()
    if not rows:
        print("  No rows found")
    for r in rows:
        dt = datetime.fromtimestamp(r['start_ts'], tz=timezone.utc)
        print(f"  source={r['source']:10s}  id={r['statistic_id']:50s}  "
              f"start={dt.isoformat()}  state={r['state']}  sum={r['sum']}  mean={r['mean']}")
    print()

# Also check statistics_meta for source field
print("=== statistics_meta (source field) ===")
cur.execute('''
    SELECT statistic_id, source, unit_of_measurement
    FROM statistics_meta
    WHERE statistic_id LIKE '%dominionsc%'
       OR statistic_id LIKE '%electric_cumulative%'
       OR statistic_id LIKE '%gas_cumulative%'
    ORDER BY statistic_id
''')
for r in cur.fetchall():
    print(f"  source={r['source']:10s}  id={r['statistic_id']}  unit={r['unit_of_measurement']}")

con.close()