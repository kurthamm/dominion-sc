#!/usr/bin/env python3
import sqlite3
import json
import sys
from datetime import datetime, timezone, timedelta

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else 'ha_config/home-assistant_v2.db'
TARGET_DATE = '2026-04-01'
DAYS_BACK = 30
DAYS_FORWARD = 7

def ts(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())

start_dt = datetime.fromisoformat(TARGET_DATE + 'T00:00:00')
end_dt = start_dt + timedelta(days=1)
start_ts = ts(start_dt)
end_ts = ts(end_dt)

# configurable window for comparative analysis
window_start_dt = start_dt - timedelta(days=DAYS_BACK)
window_end_dt = start_dt + timedelta(days=DAYS_FORWARD + 1)
window_start_ts = ts(window_start_dt)
window_end_ts = ts(window_end_dt)

now_ts = int(datetime.now(timezone.utc).timestamp())

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row
cur = con.cursor()

print('\n--- sqlite_master tables (summary) ---')
for r in cur.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name").fetchall():
    print(dict(r))

def print_schema(table):
    try:
        print(f"\n--- schema for {table} ---")
        for col in cur.execute(f"PRAGMA table_info('{table}')").fetchall():
            print(dict(col))
    except Exception as e:
        print(f"Could not inspect schema for {table}: {e}")

print_schema('states')
print_schema('statistics')
print_schema('statistics_meta')

print('Using DB:', DB_PATH)
print('Target date:', TARGET_DATE)
print('Unix start/end (UTC):', start_ts, end_ts)
print('\n--- statistics_meta rows matching dominionsc ---')
cur.execute("SELECT statistic_id, unit_of_measurement, name FROM statistics_meta WHERE statistic_id LIKE '%dominionsc%' OR name LIKE '%dominionsc%'")
rows = cur.fetchall()
for r in rows:
    print(dict(r))

print('\n--- candidate statistic_ids (from meta) ---')
stat_ids = [r['statistic_id'] for r in rows]
print(stat_ids)

if not stat_ids:
    print('\nNo statistics_meta entries found matching dominionsc. Will also search statistics table for matching ids.')
    # If statistics_meta had no direct matches, search statistics_meta for likely keys
    print('\n--- scanning statistics_meta for likely dominionsc/electric/gas entries ---')
    cur.execute("SELECT id, statistic_id, name, unit_of_measurement FROM statistics_meta ORDER BY id DESC")
    meta_rows = cur.fetchall()
    likely_meta = []
    for m in meta_rows:
        text = (m['statistic_id'] or '') + ' ' + (m['name'] or '')
        low = text.lower()
        if 'domin' in low or 'electric' in low or 'energy' in low or 'gas' in low or 'cost' in low:
            likely_meta.append(m)

    print('Found', len(likely_meta), 'likely metadata rows')
    for m in likely_meta:
        print({'id': m['id'], 'statistic_id': m['statistic_id'], 'name': m['name'], 'unit': m['unit_of_measurement']})

    print('\n--- statistics rows for 2026-04-01 by metadata_id ---')
    for m in likely_meta:
        mid = m['id']
        cur.execute('SELECT * FROM statistics WHERE metadata_id=? AND start_ts>=? AND start_ts<? ORDER BY start_ts', (mid, start_ts, end_ts))
        srows = cur.fetchall()
        print('\nMETADATA_ID:', mid, 'statistic_id:', m['statistic_id'], ' rows:', len(srows))
        for s in srows:
            try:
                start_iso = datetime.fromtimestamp(int(s['start_ts']), tz=timezone.utc).isoformat()
            except Exception:
                start_iso = str(s['start_ts'])
            print({'start': start_iso, 'state': s['state'], 'sum': s['sum'], 'mean': s['mean']})

    print(f"\n--- Context: full window for each candidate series ({window_start_dt.date()} .. {window_end_dt.date()}) ---")
    for m in likely_meta:
        mid = m['id']
        sid = m['statistic_id']
        print('\nSeries:', sid, 'metadata_id:', mid)
        cur.execute('SELECT * FROM statistics WHERE metadata_id=? AND start_ts>=? AND start_ts<? ORDER BY start_ts', (mid, window_start_ts, window_end_ts))
        srows = cur.fetchall()
        # group rows by date (UTC)
        days = {}
        future_rows = []
        for s in srows:
            try:
                dt = datetime.fromtimestamp(int(s['start_ts']), tz=timezone.utc)
            except Exception:
                # fallback if start_ts not numeric
                try:
                    dt = datetime.fromisoformat(s['start'])
                except Exception:
                    continue
            date_key = dt.date().isoformat()
            days.setdefault(date_key, []).append((dt, s))
            if int(s['start_ts']) > now_ts:
                future_rows.append((dt, s))

        # summary per day
        anomalies = {'sum_equals_state': [], 'zero_sum_state_nonzero': [], 'no_midnight_row': []}
        for d in sorted(days.keys()):
            rows = sorted(days[d], key=lambda x: x[0])
            # prefer midnight (00:00) row if available
            chosen = None
            for dt, s in rows:
                if dt.hour == 0 and dt.minute == 0:
                    chosen = (dt, s)
                    break
            if not chosen:
                chosen = rows[0]
                anomalies['no_midnight_row'].append(d)
            dt, s = chosen
            state = s['state']
            sumv = s['sum']
            # checks
            try:
                if abs((sumv or 0) - (state or 0)) < 1e-6:
                    anomalies['sum_equals_state'].append((d, state, sumv))
                if (sumv or 0) == 0 and (state or 0) != 0:
                    anomalies['zero_sum_state_nonzero'].append((d, state, sumv))
            except Exception:
                pass

        # print brief summary
        print('total rows in window:', len(srows))
        print('future-dated rows in window:', len(future_rows))
        if future_rows:
            print('  sample future rows:')
            for dt, s in future_rows[:5]:
                print('   ', dt.isoformat(), {'state': s['state'], 'sum': s['sum']})
        print('anomaly counts:', {k: len(v) for k, v in anomalies.items()})
        if anomalies['sum_equals_state']:
            print('  sum_equals_state samples:', anomalies['sum_equals_state'][:5])
        if anomalies['zero_sum_state_nonzero']:
            print('  zero_sum_state_nonzero samples:', anomalies['zero_sum_state_nonzero'][:5])
        if anomalies['no_midnight_row']:
            print('  days lacking a 00:00 row (sample):', anomalies['no_midnight_row'][:5])

    print('\n--- latest state row (any time) for each candidate entity ---')
    for m in likely_meta:
        ent = m['statistic_id']
        cur.execute('SELECT state, last_updated, attributes FROM states WHERE entity_id=? ORDER BY last_updated_ts DESC LIMIT 1', (ent,))
        r = cur.fetchone()
        print({'entity_id': ent, 'row': dict(r) if r else None})

print('\n--- states table: entities containing dominionsc or sensor names of interest ---')
# look for common entity IDs from integration
candidates = [
    'sensor.electric_cumulative_consumption',
    'sensor.gas_cumulative_consumption',
    'sensor.electric_cumulative_cost',
    'sensor.gas_cumulative_cost'
]
# also any sensor with dominionsc in entity_id
cur.execute("SELECT DISTINCT entity_id FROM states WHERE entity_id LIKE 'sensor.%dominionsc%'")
rows = [r[0] for r in cur.fetchall()]
print('sensor.%dominionsc% entity_ids:', rows)

# also check any states where attributes mention dominionsc
cur.execute("SELECT DISTINCT entity_id FROM states WHERE attributes LIKE '%dominionsc%'")
rows2 = [r[0] for r in cur.fetchall()]
print('entity_ids with dominionsc in attributes:', rows2)

# merge
all_candidates = list(dict.fromkeys(candidates + rows + rows2))
print('\nCombined candidates to inspect:', all_candidates)

for ent in all_candidates:
    cur.execute('SELECT state, last_updated, last_changed, attributes FROM states WHERE entity_id=? AND last_updated>=? AND last_updated<? ORDER BY last_updated', (ent, start_dt.isoformat(), end_dt.isoformat()))
    ent_rows = cur.fetchall()
    print('\nEntity:', ent, ' rows:', len(ent_rows))
    for e in ent_rows:
        print({'state': e['state'], 'last_updated': e['last_updated'], 'last_changed': e['last_changed'], 'attributes_sample': (e['attributes'][:200] if e['attributes'] else None)})

print('\n--- smallest sums in statistics for 2026-04-01 (joined to meta) ---')
try:
    cur.execute('''
        SELECT sm.statistic_id, s.start_ts, s.sum, s.state
        FROM statistics s
        JOIN statistics_meta sm ON s.metadata_id = sm.id
        WHERE s.start_ts>=? AND s.start_ts<?
        ORDER BY s.sum ASC
        LIMIT 50
    ''', (start_ts, end_ts))
    bad = cur.fetchall()
    for b in bad:
        try:
            start_iso = datetime.fromtimestamp(int(b['start_ts']), tz=timezone.utc).isoformat()
        except Exception:
            start_iso = str(b['start_ts'])
        print({'statistic_id': b['statistic_id'], 'start': start_iso, 'sum': b['sum'], 'state': b['state']})
except Exception as e:
    print('Could not run joined statistics query:', e)

print('\n--- states with negative-looking state strings on 2026-04-01 ---')
cur.execute("SELECT entity_id, state, last_updated FROM states WHERE last_updated>=? AND last_updated<? AND state LIKE '-%' ORDER BY last_updated", (start_dt.isoformat(), end_dt.isoformat()))
neg_states = cur.fetchall()
for n in neg_states:
    print({'entity_id': n['entity_id'], 'state': n['state'], 'last_updated': n['last_updated']})

print('\n--- any series with negative sum on 2026-04-01 (joined to meta) ---')
try:
    cur.execute('''
        SELECT sm.statistic_id, s.start_ts, s.sum
        FROM statistics s
        JOIN statistics_meta sm ON s.metadata_id = sm.id
        WHERE s.start_ts>=? AND s.start_ts<? AND s.sum < 0
        ORDER BY s.sum ASC
    ''', (start_ts, end_ts))
    neg = cur.fetchall()
    if not neg:
        print('None')
    else:
        for n in neg:
            try:
                start_iso = datetime.fromtimestamp(int(n['start_ts']), tz=timezone.utc).isoformat()
            except Exception:
                start_iso = str(n['start_ts'])
            print({'statistic_id': n['statistic_id'], 'start': start_iso, 'sum': n['sum']})
except Exception as e:
    print('failed negative sum query:', e)

print('\n--- full scan: future-dated statistics rows (start_ts > now) ---')
try:
    cur.execute('''
        SELECT sm.statistic_id, s.start_ts, s.sum, s.state
        FROM statistics s
        JOIN statistics_meta sm ON s.metadata_id = sm.id
        WHERE s.start_ts > ?
        ORDER BY s.start_ts ASC
        LIMIT 200
    ''', (now_ts,))
    fut = cur.fetchall()
    if not fut:
        print('No future-dated statistics rows found (start_ts > now)')
    else:
        print('Found', len(fut), 'future-dated rows (showing up to 200):')
        for b in fut:
            try:
                start_iso = datetime.fromtimestamp(int(b['start_ts']), tz=timezone.utc).isoformat()
            except Exception:
                start_iso = str(b['start_ts'])
            print({'statistic_id': b['statistic_id'], 'start': start_iso, 'sum': b['sum'], 'state': b['state']})
except Exception as e:
    print('future-dated scan failed:', e)

con.close()
print('\nDone')
