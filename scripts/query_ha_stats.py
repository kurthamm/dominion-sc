import sqlite3
from datetime import datetime
import csv
import os

DB_PATH = r"ha_config\home-assistant_v2.db"
TARGET_IDS = [
    "sensor.dominion_sc_energy_electric_cumulative_consumption",
    "sensor.dominion_sc_energy_gas_cumulative_consumption",
]

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row
cur = con.cursor()
def table_info(table_name):
    cur.execute(f"PRAGMA table_info('{table_name}')")
    return [r[1] for r in cur.fetchall()]


print("DB tables and columns:\n")
for tbl in ("statistics_meta", "statistics"):
    try:
        cols = table_info(tbl)
        print(f"  {tbl}: {cols}")
    except Exception as err:
        print(f"  {tbl}: error - {err}")

for stat_id in TARGET_IDS:
    print("\n=== STATISTICS META for:", stat_id)
    # Older HA DBs may use different column names; try multiple fallbacks
    meta = None
    # Try statistic_id
    try:
        cur.execute("SELECT * FROM statistics_meta WHERE statistic_id = ?", (stat_id,))
        meta = cur.fetchone()
    except sqlite3.OperationalError:
        # Try name-based lookup
        try:
            cur.execute("SELECT * FROM statistics_meta WHERE name = ?", (stat_id,))
            meta = cur.fetchone()
        except Exception:
            meta = None

    if not meta:
        print("  NOT FOUND in statistics_meta")
        continue
    for k in meta.keys():
        print(f"  {k}: {meta[k]}")

    print("\n  Recent statistics (last 10):")
    # Determine how to filter statistics table: prefer statistic_id column if present
    stats_cols = table_info("statistics")
    where_clause = None
    if "statistic_id" in stats_cols:
        where_clause = "statistic_id = ?"
    elif "metadata_id" in stats_cols:
        # If metadata_id exists, find the meta id
        meta_id = meta["id"] if "id" in meta.keys() else None
        if meta_id is not None:
            where_clause = "metadata_id = ?"
        else:
            where_clause = None
    else:
        where_clause = None

    if where_clause is None:
        print("    Cannot query statistics table: no known identifying column")
        continue

    stat_filter = meta_id if where_clause == "metadata_id = ?" else stat_id
    cur.execute(f"SELECT * FROM statistics WHERE {where_clause} ORDER BY start DESC LIMIT 10", (stat_filter,))
    rows = cur.fetchall()
    if not rows:
        print("    NO STATISTICS ROWS")
        continue
    for r in rows:
        start = r["start"] if "start" in r.keys() else r[0]
        try:
            if isinstance(start, int) or (isinstance(start, str) and start.isdigit()):
                ts = int(start)
                if ts > 1_000_000_000_000:
                    ts_s = ts / 1_000_000
                else:
                    ts_s = ts
                dt = datetime.utcfromtimestamp(ts_s)
                start_s = dt.isoformat() + "Z"
            else:
                start_s = str(start)
        except Exception:
            start_s = str(start)
        mean = r["mean"] if "mean" in r.keys() else None
        sumv = r["sum"] if "sum" in r.keys() else None
        state = r["state"] if "state" in r.keys() else None
        print(f"    start={start_s} mean={mean} sum={sumv} state={state}")

    # Additional verification: check full series monotonicity and gaps
    try:
        meta_id = meta["id"]
        cur.execute(
            "SELECT start_ts, start, sum FROM statistics WHERE metadata_id = ? ORDER BY start_ts ASC",
            (meta_id,)
        )
        all_rows = cur.fetchall()
        if not all_rows:
            print("\n    No full-series rows to verify")
            continue
        prev = None
        decreases = []
        for ar in all_rows:
            s_sum = ar["sum"] if "sum" in ar.keys() else None
            if s_sum is None:
                continue
            try:
                s_val = float(s_sum)
            except Exception:
                continue
            if prev is not None and s_val < prev:
                decreases.append((prev, s_val))
            prev = s_val
        # Convert start_ts (stored as integer seconds or microseconds) to ISO
        first_ts = all_rows[0]['start_ts']
        last_ts = all_rows[-1]['start_ts']
        def _ts_to_iso(ts):
            try:
                t = int(ts)
                if t > 1_000_000_000_000:
                    t_s = t / 1_000_000
                else:
                    t_s = t
                return datetime.utcfromtimestamp(t_s).date().isoformat()
            except Exception:
                return str(ts)

        print(f"\n    Full-series points={len(all_rows)} first_sum={all_rows[0]['sum']} last_sum={all_rows[-1]['sum']} first_date={_ts_to_iso(first_ts)} last_date={_ts_to_iso(last_ts)}")
        if decreases:
            print(f"    WARNING: found {len(decreases)} decreasing steps in cumulative series: {decreases[:5]}")
        else:
            print("    OK: series is non-decreasing (monotonic)")
        # Compute per-day deltas and optionally export CSV for further analysis
        try:
            deltas = []
            prev_sum = None
            for ar in all_rows:
                # Determine date from start_ts or start
                if "start_ts" in ar.keys():
                    ts = ar["start_ts"]
                else:
                    ts = None
                if ts is None:
                    day = ar["start"] if "start" in ar.keys() else None
                else:
                    try:
                        t = int(ts)
                        if t > 1_000_000_000_000:
                            t_s = t / 1_000_000
                        else:
                            t_s = t
                        day = datetime.utcfromtimestamp(t_s).date().isoformat()
                    except Exception:
                        day = str(ar["start"]) if "start" in ar.keys() else str(ts)

                cum = float(ar["sum"] or 0.0) if "sum" in ar.keys() else 0.0
                if prev_sum is None:
                    delta = cum
                else:
                    delta = cum - prev_sum
                deltas.append({"date": day, "cumulative": cum, "delta": delta})
                prev_sum = cum

            # Print any anomalous deltas (negative or unusually large)
            negs = [d for d in deltas if d["delta"] < 0]
            large = []
            # Define large as > 10x median positive delta (if enough points)
            pos_deltas = sorted([d["delta"] for d in deltas if d["delta"] > 0])
            median = pos_deltas[len(pos_deltas)//2] if pos_deltas else None
            if median:
                threshold = median * 10
                large = [d for d in deltas if d["delta"] > threshold]

            if negs:
                print(f"\n    WARNING: {len(negs)} negative deltas found (possible regressions): {negs[:5]}")
            if large:
                print(f"\n    NOTICE: {len(large)} large jumps found (>{threshold}): {large[:5]}")

            # Export CSV into ha_config for easy download/inspection
            export_dir = os.path.join(os.path.dirname(DB_PATH), "dominionsc_exports")
            os.makedirs(export_dir, exist_ok=True)
            base_name = stat_id.split(".")[-1]
            csv_path = os.path.join(export_dir, f"{base_name}.csv")
            with open(csv_path, "w", newline='', encoding='utf-8') as fh:
                writer = csv.DictWriter(fh, fieldnames=["date", "cumulative", "delta"])
                writer.writeheader()
                for row in deltas:
                    writer.writerow(row)
            print(f"\n    Exported CSV to: {csv_path}")
        except Exception as err:
            print(f"\n    Could not compute/export deltas: {err}")
    except Exception as err:
        print(f"\n    Could not run full-series verification: {err}")

con.close()
print('\nDone')
