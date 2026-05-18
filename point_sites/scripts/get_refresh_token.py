"""One-shot OAuth flow to obtain a Gmail API refresh token.

Run locally exactly once after creating a Desktop-app OAuth Client ID in
Google Cloud Console. Outputs the three values that need to be set as
GitHub Secrets: GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN.

Usage:
    cd point_sites
    uv run python scripts/get_refresh_token.py ~/Downloads/client_secret_xxx.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# Read-only scope is enough for click-mail discovery. No send / no modify.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main(credentials_path: Path) -> None:
    if not credentials_path.exists():
        sys.exit(f"client_secret JSON not found: {credentials_path}")

    with credentials_path.open() as f:
        client_config = json.load(f)
    # Desktop app JSON has top-level key "installed"; web app uses "web".
    installed = client_config.get("installed") or client_config.get("web")
    if installed is None:
        sys.exit("Unexpected client_secret JSON shape (no 'installed' or 'web' key)")

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    # prompt='consent' forces the consent screen and guarantees Google returns a
    # refresh_token even on subsequent runs (otherwise only the first run gets one).
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\n" + "=" * 70)
    print("OAuth flow succeeded. Set these three values as GitHub Secrets:")
    print("=" * 70)
    print(f"GMAIL_CLIENT_ID     = {installed['client_id']}")
    print(f"GMAIL_CLIENT_SECRET = {installed['client_secret']}")
    print(f"GMAIL_REFRESH_TOKEN = {creds.refresh_token}")
    print("=" * 70)
    print("\nRegister via either:")
    print(
        "  gh secret set GMAIL_CLIENT_ID --body '<value>'\n"
        "  gh secret set GMAIL_CLIENT_SECRET --body '<value>'\n"
        "  gh secret set GMAIL_REFRESH_TOKEN --body '<value>'"
    )
    print("or paste them into GitHub Settings → Secrets and variables → Actions.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: uv run python scripts/get_refresh_token.py <path-to-client_secret.json>")
    main(Path(sys.argv[1]).expanduser())
