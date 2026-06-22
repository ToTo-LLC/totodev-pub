#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
One-time helper: exchange a Zoho Self Client *grant code* for a permanent *refresh token*.

This automates step 3 of the Self Client OAuth flow documented in
`zoho_workdrive_sync.py`. You run it ONCE per account; afterwards the refresh token goes
into `ZOHO_WD_REFRESH_TOKEN` and the sync program mints access tokens from it forever.

## Prerequisites (do these in a browser first)

1. Zoho API Console (`https://api-console.zoho.<dc>`) -> Self Client -> note the
   **Client ID** and **Client Secret**.
2. "Generate Code" tab -> enter scopes (e.g.
   `WorkDrive.files.READ,WorkDrive.team.READ,WorkDrive.workspace.READ`), a description,
   and an expiry -> copy the one-time **grant code** (use it before it expires).

## Usage

    python zoho_workdrive_token_bootstrap.py \
        --client-id 1000.XXXX --client-secret YYYY --code 1000.ZZZZ --dc com

It POSTs the `authorization_code` exchange to `https://accounts.zoho.<dc>/oauth/v2/token`
and prints the `refresh_token` (and the `api_domain` Zoho associates with the account).

This is a developer convenience only; it stores nothing and has no side effects beyond
the single token-exchange request.
"""

from __future__ import annotations

import sys

import click


def exchange_code_for_refresh_token(
    client_id: str,
    client_secret: str,
    code: str,
    dc: str = "com",
) -> dict:
    """POST the one-time authorization_code exchange; return the parsed JSON response."""
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError(
            "The 'requests' package is required. Install with: "
            "pip install \"totodev-pub[connectors]\""
        ) from exc

    resp = requests.post(
        f"https://accounts.zoho.{dc}/oauth/v2/token",
        params={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
        timeout=30,
    )
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(f"Token endpoint returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}")


@click.command()
@click.option("--client-id", required=True, help="Self Client Client ID")
@click.option("--client-secret", required=True, help="Self Client Client Secret")
@click.option("--code", required=True, help="One-time grant code from the Generate Code tab")
@click.option("--dc", default="com", show_default=True, help="Data center / region (com/eu/in/com.au/...)")
def main(client_id: str, client_secret: str, code: str, dc: str) -> None:
    """Exchange a Zoho Self Client grant code for a permanent refresh token."""
    data = exchange_code_for_refresh_token(client_id, client_secret, code, dc)

    refresh_token = data.get("refresh_token")
    if not refresh_token:
        click.echo(f"No refresh_token in response: {data}", err=True)
        click.echo(
            "\nCommon causes: the grant code expired or was already used, the scopes/dc "
            "are wrong, or client id/secret mismatch. Generate a fresh code and retry.",
            err=True,
        )
        sys.exit(1)

    click.echo("Success! Save this as ZOHO_WD_REFRESH_TOKEN (it does not expire):\n")
    click.echo(f"  export ZOHO_WD_REFRESH_TOKEN='{refresh_token}'")
    if data.get("api_domain"):
        click.echo(f"\napi_domain for this account: {data['api_domain']}")
    click.echo(f"\n(access_token also issued, expires_in={data.get('expires_in')}s -- not needed; "
               "the sync program mints its own.)")


if __name__ == "__main__":
    main()
