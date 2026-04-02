from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

# Add repo root so package imports work when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.dominionsc.dominion_sc_client import DominionSCClient


STATE_PATH = Path(__file__).resolve().parent / ".test_integration_state.json"
DEFAULT_COOKIE_PATH = Path(__file__).resolve().parents[1] / "dominion_cookies.json"


@dataclass(frozen=True)
class BillingCycle:
    start: date
    end: date

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}|{self.end.isoformat()}"


class IntegrationRunner:
    """Stateful runner that behaves like integration first-run then scheduled runs."""

    def __init__(self, state_path: Path, now_dt: datetime) -> None:
        self.state_path = state_path
        self.now_dt = now_dt
        self.state = self._load_state()
        config = self.state["config"]
        self.client = DominionSCClient(
            log_requests=bool(config.get("log_requests", False)),
            verify_ssl=bool(config.get("verify_ssl", True)),
        )
        self._last_totals: dict[str, float] | None = None

    # ---------- Public flow ----------

    def run(self, smoke_mode: bool = False) -> None:
        if not self.state.get("initialized"):
            mode = "non-interactive" if smoke_mode else "interactive"
            print(f"[first-run] collecting configuration + login ({mode})")
            self._first_time_setup(non_interactive=smoke_mode)
            self._save_state()

        print("[scheduled] authenticating (reuse cookies/session if available)")
        self._authenticate_non_interactive()

        print("[scheduled] pulling interval usage")
        self._process_intervals(self.now_dt)

        print("[scheduled] running lag-safe daily reconcile window")
        self._daily_reconcile(self.now_dt.date())

        print("[scheduled] processing one backfill cycle")
        self.run_backfill_service(overwrite=False)

        self._assert_monotonic()
        self._save_state()
        self._print_summary()

    def run_backfill_service(self, overwrite: bool = False, cycle_key: str | None = None) -> None:
        cycle = self._pick_cycle_for_backfill(overwrite=overwrite, cycle_key=cycle_key)
        if cycle is None:
            print("[backfill] no remaining cycles")
            return

        rows = self._fetch_daily_rows(cycle.start, cycle.end)
        if not rows:
            print(f"[backfill] no rows returned for cycle {cycle.key}")
            return

        for row in rows:
            day_key = row["date"]
            self._upsert_daily(
                f"electric|{day_key}",
                row["electric_usage_kwh"],
                overwrite=overwrite,
                usage_total_key="electric_kwh_total",
            )
            self._upsert_daily(
                f"gas|{day_key}",
                row["gas_usage_ccf"] * 100.0,
                overwrite=overwrite,
                usage_total_key="gas_ft3_total",
            )
            self._upsert_daily_cost(
                f"electric|{day_key}",
                row["electric_cost"],
                overwrite=overwrite,
                cost_total_key="electric_cost_total",
            )
            self._upsert_daily_cost(
                f"gas|{day_key}",
                row["gas_cost"],
                overwrite=overwrite,
                cost_total_key="gas_cost_total",
            )

        backfill = self.state["backfill"]
        if cycle.key in backfill["missing_cycles"]:
            backfill["missing_cycles"].remove(cycle.key)
        if cycle.key not in backfill["completed_cycles"]:
            backfill["completed_cycles"].append(cycle.key)
            backfill["cycles_completed"] += 1

        print(f"[backfill] completed cycle={cycle.key} overwrite={overwrite}")

    # ---------- First run / auth ----------

    def _first_time_setup(self, non_interactive: bool = False) -> None:
        config = self.state["config"]

        def _env(key: str, fallback: str | None = None) -> str | None:
            return os.getenv(key, fallback)

        def _env_int(key: str, fallback: int) -> int:
            raw = _env(key)
            if not raw:
                return fallback
            return int(raw)

        def _env_bool(key: str, fallback: bool) -> bool:
            raw = _env(key)
            if raw is None:
                return fallback
            return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

        default_username = config.get("username") or ""
        if non_interactive:
            username = (
                _env("DOMINIONSC_TEST_USERNAME")
                or _env("DOMINION_USERNAME")
                or default_username
            )
        else:
            username = input(f"Dominion username [{default_username}]: ").strip() or default_username
        if not username:
            if non_interactive:
                raise RuntimeError(
                    "Username is required in smoke mode. Set DOMINIONSC_TEST_USERNAME (or DOMINION_USERNAME)."
                )
            raise RuntimeError("Username is required on first run")

        if non_interactive:
            password = _env("DOMINIONSC_TEST_PASSWORD") or _env("DOMINION_PASSWORD") or ""
        else:
            password = getpass.getpass("Dominion password: ").strip()

        poll_default = int(config.get("poll_minutes", 15))
        if non_interactive:
            poll_minutes = _env_int("DOMINIONSC_TEST_POLL_MINUTES", poll_default)
        else:
            poll_input = input(f"Polling frequency minutes [{poll_default}]: ").strip()
            poll_minutes = int(poll_input) if poll_input else poll_default

        lookback_default = int(config.get("daily_lookback_days", 5))
        if non_interactive:
            daily_lookback_days = _env_int("DOMINIONSC_TEST_DAILY_LOOKBACK_DAYS", lookback_default)
        else:
            lookback_input = input(f"Daily reconcile lookback days [{lookback_default}]: ").strip()
            daily_lookback_days = int(lookback_input) if lookback_input else lookback_default

        backfill_default = int(config.get("backfill_cycles_target", 6))
        if non_interactive:
            backfill_cycles_target = _env_int("DOMINIONSC_TEST_BACKFILL_CYCLES_TARGET", backfill_default)
        else:
            backfill_input = input(f"Backfill cycle target [{backfill_default}]: ").strip()
            backfill_cycles_target = int(backfill_input) if backfill_input else backfill_default

        cookie_default = config.get("cookie_path") or str(DEFAULT_COOKIE_PATH)
        if non_interactive:
            cookie_path = _env("DOMINIONSC_TEST_COOKIE_PATH") or cookie_default
        else:
            cookie_input = input(f"Cookie path [{cookie_default}]: ").strip()
            cookie_path = cookie_input or cookie_default

        verify_ssl = _env_bool("DOMINIONSC_TEST_VERIFY_SSL", bool(config.get("verify_ssl", False))) if non_interactive else bool(config.get("verify_ssl", False))
        log_requests = _env_bool("DOMINIONSC_TEST_LOG_REQUESTS", bool(config.get("log_requests", False))) if non_interactive else bool(config.get("log_requests", False))

        self.state["config"].update(
            {
                "username": username,
                "poll_minutes": max(poll_minutes, 1),
                "daily_lookback_days": max(daily_lookback_days, 1),
                "backfill_cycles_target": max(backfill_cycles_target, 1),
                "cookie_path": cookie_path,
                "verify_ssl": verify_ssl,
                "log_requests": log_requests,
            }
        )

        if cookie_path and self.client.load_cookies(cookie_path):
            print("[auth] restored valid session from cookie file during first run")
        else:
            if not password:
                mode = "smoke mode" if non_interactive else "first run"
                raise RuntimeError(
                    f"Password is required on {mode}. Set DOMINIONSC_TEST_PASSWORD (or DOMINION_PASSWORD) for non-interactive runs."
                )
            self._login(username=username, password=password, non_interactive=non_interactive)

        self._initialize_backfill_cycles()
        self.state["initialized"] = True

    def _login(self, username: str, password: str, non_interactive: bool) -> None:
        try:
            self.client.login(username, password)
            print("[auth] login successful without 2FA prompt")
        except Exception as exc:
            payload = exc.args[0] if exc.args else None
            if isinstance(payload, dict) and "2fa_required" in payload:
                if non_interactive:
                    raise RuntimeError(
                        "2FA is required but smoke mode is non-interactive. Run once interactively to cache cookies/token, then re-run smoke mode."
                    ) from exc
                options = payload["2fa_required"]
                print("[auth] 2FA required. Options:")
                for idx, option in enumerate(options):
                    print(f"  {idx + 1}. {option['method']} - {option['display_value']}")

                selection = input("Select option number [1]: ").strip() or "1"
                selected = options[max(int(selection) - 1, 0)]
                self.client.select_2fa_method(selected)

                code = input("Enter 2FA code: ").strip()
                remember = (input("Remember device/token? [Y/n]: ").strip().lower() or "y") in {"y", "yes"}
                if not self.client.verify_2fa_code(code, remember_device=remember):
                    raise RuntimeError("2FA verification failed")
                print("[auth] 2FA verification successful")
            else:
                raise RuntimeError(f"Login failed: {exc}") from exc

        self.client.save_cookies(self.state["config"]["cookie_path"])

    def _authenticate_non_interactive(self) -> None:
        cookie_path = self.state["config"].get("cookie_path")
        username = (
            self.state["config"].get("username")
            or os.getenv("DOMINIONSC_TEST_USERNAME")
            or os.getenv("DOMINION_USERNAME")
        )

        if cookie_path and self.client.load_cookies(cookie_path):
            print("[auth] restored valid session from cookie file")
            return

        password = (
            self.client._password  # loaded from cookie file if available
            or os.getenv("DOMINIONSC_TEST_PASSWORD")
            or os.getenv("DOMINION_PASSWORD")
        )
        if not password:
            raise RuntimeError("No reusable password found; rerun once and complete first-run login.")

        try:
            self.client.login(username, password)
        except Exception as exc:
            raise RuntimeError(f"Scheduled login failed non-interactively: {exc}") from exc

        self.client.save_cookies(cookie_path)
        print("[auth] login succeeded and cookies refreshed")

    # ---------- Data processing ----------

    def _process_intervals(self, now_dt: datetime) -> None:
        start = now_dt - timedelta(hours=2)
        electric_payload = self.client.get_hourly_usage(
            day_start=start,
            day_end=now_dt,
            measurement_type="ELECTRIC",
        )
        gas_payload = self.client.get_hourly_usage(
            day_start=start,
            day_end=now_dt,
            measurement_type="GAS",
        )
        rows = self._merge_usage_rows(
            electric_rows=self._parse_usage_rows(electric_payload, fuel="electric"),
            gas_rows=self._parse_usage_rows(gas_payload, fuel="gas"),
        )
        self.state["stats"]["raw_interval_rows"] += len(rows)

        for row in rows:
            interval_end = row["interval_end"]
            e_key = f"electric|{interval_end}"
            g_key = f"gas|{interval_end}"

            if e_key not in self.state["interval_ledger"]:
                self.state["interval_ledger"][e_key] = row["electric_usage_kwh"]
                self.state["totals"]["electric_kwh_total"] += row["electric_usage_kwh"]
                self.state["stats"]["accepted_interval_rows"] += 1

            if g_key not in self.state["interval_ledger"]:
                g_ft3 = row["gas_usage_ccf"] * 100.0
                self.state["interval_ledger"][g_key] = g_ft3
                self.state["totals"]["gas_ft3_total"] += g_ft3

            if e_key not in self.state["interval_cost_ledger"]:
                self.state["interval_cost_ledger"][e_key] = row["electric_cost"]
                self.state["totals"]["electric_cost_total"] += row["electric_cost"]

            if g_key not in self.state["interval_cost_ledger"]:
                self.state["interval_cost_ledger"][g_key] = row["gas_cost"]
                self.state["totals"]["gas_cost_total"] += row["gas_cost"]

        print(f"[intervals] fetched={len(rows)} accepted_total={self.state['stats']['accepted_interval_rows']}")

    def _daily_reconcile(self, now_date: date) -> None:
        lookback_days = int(self.state["config"]["daily_lookback_days"])
        start = now_date - timedelta(days=lookback_days)
        end = now_date - timedelta(days=1)
        rows = self._fetch_daily_rows(start, end)

        for row in rows:
            day_key = row["date"]
            self._upsert_daily(
                f"electric|{day_key}",
                row["electric_usage_kwh"],
                overwrite=True,
                usage_total_key="electric_kwh_total",
            )
            self._upsert_daily(
                f"gas|{day_key}",
                row["gas_usage_ccf"] * 100.0,
                overwrite=True,
                usage_total_key="gas_ft3_total",
            )
            self._upsert_daily_cost(
                f"electric|{day_key}",
                row["electric_cost"],
                overwrite=True,
                cost_total_key="electric_cost_total",
            )
            self._upsert_daily_cost(
                f"gas|{day_key}",
                row["gas_cost"],
                overwrite=True,
                cost_total_key="gas_cost_total",
            )

        print(f"[daily] reconciled rows={len(rows)} window={start.isoformat()}..{end.isoformat()}")

    def _fetch_daily_rows(self, start: date, end: date) -> list[dict[str, float | str]]:
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(end, time.max, tzinfo=UTC)
        electric_payload = self.client.get_daily_usage(
            start=start_dt,
            end=end_dt,
            measurement_type="ELECTRIC",
        )
        gas_payload = self.client.get_daily_usage(
            start=start_dt,
            end=end_dt,
            measurement_type="GAS",
        )
        return self._merge_usage_rows(
            electric_rows=self._parse_usage_rows(electric_payload, fuel="electric"),
            gas_rows=self._parse_usage_rows(gas_payload, fuel="gas"),
        )

    @staticmethod
    def _parse_usage_rows(payload: Any, fuel: str) -> list[dict[str, float | str]]:
        usage_rows = []
        chart_rows = []
        if isinstance(payload, dict):
            chart_rows = (payload.get("payload") or {}).get("usageChartDataList") or []

        for row in chart_rows:
            interval_end = row.get("intervalEndDate") or row.get("intervalEnd")
            interval_end_s = str(interval_end)
            day_key = interval_end_s.split(" ")[0]

            usage_rows.append(
                {
                    "interval_end": interval_end_s,
                    "date": day_key,
                    "electric_usage_kwh": max(float(row.get("consumption") or 0.0), 0.0) if fuel == "electric" else 0.0,
                    "gas_usage_ccf": max(float(row.get("consumption") or 0.0), 0.0) if fuel == "gas" else 0.0,
                    "electric_cost": max(float(row.get("cost") or 0.0), 0.0) if fuel == "electric" else 0.0,
                    "gas_cost": max(float(row.get("cost") or 0.0), 0.0) if fuel == "gas" else 0.0,
                }
            )

        return usage_rows

    @staticmethod
    def _merge_usage_rows(
        electric_rows: list[dict[str, float | str]],
        gas_rows: list[dict[str, float | str]],
    ) -> list[dict[str, float | str]]:
        merged: dict[str, dict[str, float | str]] = {}

        def _upsert(row: dict[str, float | str]) -> None:
            key = str(row["interval_end"])
            if key not in merged:
                merged[key] = {
                    "interval_end": row["interval_end"],
                    "date": row["date"],
                    "electric_usage_kwh": 0.0,
                    "gas_usage_ccf": 0.0,
                    "electric_cost": 0.0,
                    "gas_cost": 0.0,
                }

            merged_row = merged[key]
            merged_row["electric_usage_kwh"] = float(merged_row["electric_usage_kwh"]) + float(row["electric_usage_kwh"])
            merged_row["gas_usage_ccf"] = float(merged_row["gas_usage_ccf"]) + float(row["gas_usage_ccf"])
            merged_row["electric_cost"] = float(merged_row["electric_cost"]) + float(row["electric_cost"])
            merged_row["gas_cost"] = float(merged_row["gas_cost"]) + float(row["gas_cost"])

        for row in electric_rows:
            _upsert(row)
        for row in gas_rows:
            _upsert(row)

        return [merged[key] for key in sorted(merged.keys())]

    # ---------- Backfill ----------

    def _initialize_backfill_cycles(self) -> None:
        backfill = self.state["backfill"]
        if backfill["missing_cycles"]:
            return

        target = int(self.state["config"]["backfill_cycles_target"])
        cycles = self._build_recent_monthly_cycles(now_date=self.now_dt.date(), target=target)
        backfill["missing_cycles"] = [cycle.key for cycle in cycles]

    @staticmethod
    def _build_recent_monthly_cycles(now_date: date, target: int) -> list[BillingCycle]:
        cycles: list[BillingCycle] = []
        if target <= 0:
            return cycles

        month_start = date(now_date.year, now_date.month, 1)
        # Start with the most recently completed billing cycle (previous month).
        current = month_start - timedelta(days=1)

        while len(cycles) < target:
            cycle_start = date(current.year, current.month, 1)
            cycle_end = date(current.year, current.month, current.day)
            cycles.append(BillingCycle(start=cycle_start, end=cycle_end))
            current = cycle_start - timedelta(days=1)

        # Oldest first so one-cycle-per-run progresses forward in time.
        cycles.reverse()
        return cycles

    def _pick_cycle_for_backfill(self, overwrite: bool, cycle_key: str | None) -> BillingCycle | None:
        backfill = self.state["backfill"]

        if cycle_key:
            start_s, end_s = cycle_key.split("|")
            return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))

        if overwrite and backfill["completed_cycles"]:
            start_s, end_s = backfill["completed_cycles"][0].split("|")
            return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))

        if not backfill["missing_cycles"]:
            return None

        start_s, end_s = backfill["missing_cycles"][0].split("|")
        return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))

    # ---------- State / totals ----------

    def _upsert_daily(self, key: str, value: float, overwrite: bool, usage_total_key: str) -> None:
        value = max(float(value), 0.0)
        if key not in self.state["daily_ledger"]:
            self.state["daily_ledger"][key] = value
            self.state["totals"][usage_total_key] += value
            return
        if overwrite:
            old = float(self.state["daily_ledger"][key])
            self.state["daily_ledger"][key] = value
            self.state["totals"][usage_total_key] += value - old

    def _upsert_daily_cost(self, key: str, value: float, overwrite: bool, cost_total_key: str) -> None:
        value = max(float(value), 0.0)
        if key not in self.state["daily_cost_ledger"]:
            self.state["daily_cost_ledger"][key] = value
            self.state["totals"][cost_total_key] += value
            return
        if overwrite:
            old = float(self.state["daily_cost_ledger"][key])
            self.state["daily_cost_ledger"][key] = value
            self.state["totals"][cost_total_key] += value - old

    def _assert_monotonic(self) -> None:
        current = self.state["totals"]
        if self._last_totals is not None:
            for key in ("electric_kwh_total", "gas_ft3_total", "electric_cost_total", "gas_cost_total"):
                if current[key] < self._last_totals[key]:
                    raise RuntimeError(f"Non-monotonic total detected for {key}")
        self._last_totals = dict(current)

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))

        return {
            "initialized": False,
            "config": {
                "username": "",
                "poll_minutes": 15,
                "daily_lookback_days": 5,
                "backfill_cycles_target": 6,
                "cookie_path": str(DEFAULT_COOKIE_PATH),
                "verify_ssl": False,
                "log_requests": False,
            },
            "interval_ledger": {},
            "interval_cost_ledger": {},
            "daily_ledger": {},
            "daily_cost_ledger": {},
            "backfill": {
                "cycles_completed": 0,
                "missing_cycles": [],
                "completed_cycles": [],
            },
            "totals": {
                "electric_kwh_total": 0.0,
                "gas_ft3_total": 0.0,
                "electric_cost_total": 0.0,
                "gas_cost_total": 0.0,
            },
            "stats": {
                "raw_interval_rows": 0,
                "accepted_interval_rows": 0,
            },
        }

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def _print_summary(self) -> None:
        totals = self.state["totals"]
        print("\n=== Totals (synthetic cumulative) ===")
        print(f"electric_kwh_total : {totals['electric_kwh_total']:.3f} kWh")
        print(f"gas_ft3_total      : {totals['gas_ft3_total']:.3f} ft³")
        print(f"electric_cost_total: ${totals['electric_cost_total']:.3f}")
        print(f"gas_cost_total     : ${totals['gas_cost_total']:.3f}")
        print(f"interval keys      : {len(self.state['interval_ledger'])}")
        print(f"daily keys         : {len(self.state['daily_ledger'])}")
        print(f"backfill completed : {self.state['backfill']['cycles_completed']}")
        print(f"raw interval rows  : {self.state['stats']['raw_interval_rows']}")
        print(f"accepted intervals : {self.state['stats']['accepted_interval_rows']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Integration-behavior test runner for Dominion SC client.",
        epilog=(
            "When --smoke is enabled, variables are loaded from --env-file first (default .env), "
            "then process environment variables are read. "
            "Smoke mode env vars: DOMINIONSC_TEST_USERNAME, DOMINIONSC_TEST_PASSWORD, "
            "DOMINIONSC_TEST_POLL_MINUTES, DOMINIONSC_TEST_DAILY_LOOKBACK_DAYS, "
            "DOMINIONSC_TEST_BACKFILL_CYCLES_TARGET, "
            "DOMINIONSC_TEST_COOKIE_PATH, DOMINIONSC_TEST_VERIFY_SSL, DOMINIONSC_TEST_LOG_REQUESTS"
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete stored test state to force first-run interactive setup.",
    )
    parser.add_argument(
        "--overwrite-backfill",
        action="store_true",
        help="After scheduled run, execute one overwrite backfill cycle.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run non-interactively using environment variables; useful for CI/local smoke checks.",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=".env",
        help="Path to .env file used in --smoke mode (default: .env).",
    )
    return parser.parse_args()


def _load_env_file(env_file: Path) -> int:
    """Load .env key=value pairs into os.environ without overriding existing env vars."""
    if not env_file.exists() or not env_file.is_file():
        return 0

    loaded = 0
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def main() -> None:
    args = parse_args()
    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
        print(f"Deleted state file: {STATE_PATH}")

    if args.smoke:
        env_file = Path(args.env_file).expanduser()
        loaded_count = _load_env_file(env_file)
        if loaded_count:
            print(f"Loaded {loaded_count} variable(s) from {env_file}")
        elif env_file.exists():
            print(f"No new variables loaded from {env_file} (already set or no valid KEY=VALUE entries)")

    runner = IntegrationRunner(state_path=STATE_PATH, now_dt=datetime.now())
    runner.run(smoke_mode=args.smoke)

    if args.overwrite_backfill:
        print("\n[manual service] overwrite backfill")
        runner.run_backfill_service(overwrite=True)
        runner._save_state()
        runner._print_summary()

    print("\nSimulation completed successfully.")


if __name__ == "__main__":
    main()