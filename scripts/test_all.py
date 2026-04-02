"""
Script to test DominionSCcliententicator cliententication logic interactively.

Prompts for username, password, and handles 2FA if required.
"""
import argparse
import sys
import getpass
from pathlib import Path
import json
from datetime import datetime

# Add repo root so package imports work (allows importing the dominionsc package)
sys.path.insert(0, str(Path(__file__).parent.parent))
from custom_components.dominionsc.dominion_sc_client import DominionSCClient

output_dir = Path(__file__).resolve().parents[1] / "reference/json_dumps"
output_dir.mkdir(parents=True, exist_ok=True)

def write_output(name, payload):
    path = output_dir / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {path}")

JSON_OUTPUT_HANDLERS = [
    ("get_account_summary", lambda client, start, end: client.get_account_summary()),
    ("get_current_daily_usage", lambda client, start, end: client.get_current_daily_usage()),
    ("get_ami_meter", lambda client, start, end: client.get_ami_meter()),
    ("get_energy_analyzer_flag", lambda client, start, end: client.get_energy_analyzer_flag()),
    (
        "get_daily_usage",
        lambda client, start, end: client.get_daily_usage(start=start, end=end),
    ),
    (
        "get_hourly_usage",
        lambda client, start, end: client.get_hourly_usage(day_start=start, day_end=end),
    ),
    (
        "get_bill_itemization",
        lambda client, start, end: client.get_bill_itemization(start=start, end=end),
    ),
    ("get_bill_projection", lambda client, start, end: client.get_bill_projection()),
    ("get_monthly_summary_widget_data", lambda client, start, end: client.get_monthly_summary_widget_data()),
    ("get_ui_configs", lambda client, start, end: client.get_ui_configs()),
]

def main():
    parser = argparse.ArgumentParser(description="Run Dominion/Bidgely integration gestures and optionally dump selected JSON outputs.")
    parser.add_argument(
        "-f",
        "--functions",
        nargs="+",
        choices=[name for name, _ in JSON_OUTPUT_HANDLERS],
        help="JSON-output functions to run (defaults to all)",
    )
    parser.add_argument(
        "--start",
        type=str,
        help="Optional start datetime (ISO format or 'YYYY-MM-DD HH:MM:SS') for functions that accept it",
    )
    parser.add_argument(
        "--end",
        type=str,
        help="Optional end datetime (ISO format or 'YYYY-MM-DD HH:MM:SS') for functions that accept it",
    )
    args = parser.parse_args()
    selected_functions = set(args.functions) if args.functions else {name for name, _ in JSON_OUTPUT_HANDLERS}
    def parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            # allow space-separated format by converting to ISO
            iso_value = value.replace(" ", "T") if " " in value else value
            return datetime.fromisoformat(iso_value)
        except ValueError:
            raise ValueError(
                "Datetimes must be ISO formatted (e.g. 2026-04-01T00:00:00) or have a space between date/time."
            )
    start_dt = parse_dt(args.start)
    end_dt = parse_dt(args.end)

    print("DominionSCClient cliententication Test")
    cookie_path = "dominion_cookies.json"
    client = DominionSCClient(log_requests=True, verify_ssl=False)
    # Try to load cookies and credentials
    loaded = client.load_cookies(cookie_path)
    if loaded and client._username and client._password:
        print("Loaded credentials from cookie file.")
        username = client._username
        password = client._password
    else:
        username = input("Username: ")
        password = getpass.getpass("Password: ")
        client._username = username
        client._password = password
    try:
        result = client.login(username, password)
        print("Login successful (no 2FA required).")
    except Exception as e:
        # Check for 2FA required (our class raises Exception with dict)
        if hasattr(e, 'args') and e.args and isinstance(e.args[0], dict) and '2fa_required' in e.args[0]:
            options = e.args[0]['2fa_required']
            print("2FA required. Available options:")
            for idx, opt in enumerate(options):
                print(f"  {idx+1}: {opt['method']} - {opt['display_value']}")
            sel = int(input("Select 2FA method (number): ")) - 1
            selected = options[sel]
            if client.select_2fa_method(selected):
                code = input("Enter 2FA code received: ")
                try:
                    if client.verify_2fa_code(code, remember_device=True):
                        print("2FA verification successful. Logged in!")
                except Exception as ve:
                    print(f"2FA verification failed: {ve}")
                    sys.exit(1)
        else:
            print(f"Login failed: {e}")
            sys.exit(1)
    print("Session is logged in:", client.is_logged_in)
    # Always save cookies and credentials after login
    client.save_cookies(cookie_path)
    print(f"Cookies and credentials saved to {cookie_path}")

    # get_account_listing
    accounts = client.get_account_listing()
    if(len(accounts) == 0):
        print("Single Account Found")
    else:
        print(f"Multiple Accounts Found: {accounts}")

    print(f"\n**** Running Dominion SC + Bidgely JSON helpers and saving output to {output_dir} ****")

    for name, handler in JSON_OUTPUT_HANDLERS:
        if name not in selected_functions:
            continue
        recs = handler(client, start_dt, end_dt)
        write_output(name, recs)

if __name__ == "__main__":
    main()
