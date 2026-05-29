#!/usr/bin/env python3
"""Release a new version to the Godot Asset Store (store.godotengine.org).

Self-contained CLI used by the composite GitHub Action in this repo. It mirrors
the publish flow from godot-store-mcp:

1. Log in via the Keycloak OIDC authorization-code flow (pure httpx, no browser)
   to obtain the Flask ``session`` cookie.
2. Upload the version archive: request a pre-signed S3 URL, PUT the file there,
   then commit the upload via ``/version/create/``.
3. Optionally submit the asset for moderator review (``mark_public``).

The new store has no public JSON API; the manage pages are HTMX-rendered HTML,
so we scrape CSRF tokens and form errors out of them. Selectors/URLs may change
while the store is in beta.

Credentials are read from the environment so they never appear in argv / logs:
    GODOT_STORE_USERNAME, GODOT_STORE_PASSWORD
or, for CI where the interactive Keycloak flow is impractical, a pre-obtained
Flask session cookie:
    GODOT_STORE_SESSION
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://store.godotengine.org"
LOGIN_URL = f"{BASE_URL}/login"
STORE_HOST = "store.godotengine.org"
SSO_HOST = "sso.godotengine.org"
USER_AGENT = "godot-asset-store-release/1.0 (+https://github.com/NodotProject/godot-asset-store-release)"

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".zip": "application/zip",
}


class ReleaseError(RuntimeError):
    """Any failure during login, upload, or submit."""


# --------------------------------------------------------------------------- login


def _detect_interactive_block(html: str) -> str | None:
    low = html.lower()
    if "g-recaptcha" in low or "grecaptcha" in low or 'src="https://www.google.com/recaptcha' in low:
        return "Keycloak is showing a reCAPTCHA challenge."
    if 'name="otp"' in low or "one-time" in low or ("authenticator" in low and "code" in low):
        return "Two-factor / OTP code required."
    if "update password" in low or "configure-totp" in low:
        return "Keycloak is requiring a required-action step (password update / TOTP setup)."
    return None


def _detect_login_error(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    alert = soup.select_one(".alert-error, .kc-feedback-text, #input-error, .pf-c-form__helper-text")
    if alert:
        text = alert.get_text(" ", strip=True)
        if text:
            return text
    if "invalid username or password" in html.lower():
        return "Invalid username or password"
    return None


def login(client: httpx.Client, username: str, password: str) -> None:
    """Run the OIDC authorization-code flow; leave the ``session`` cookie in
    ``client``'s jar so subsequent manage calls are authenticated."""
    kc = client.get(LOGIN_URL)
    if kc.status_code >= 400:
        raise ReleaseError(f"Could not start login flow: HTTP {kc.status_code} at {kc.url}")

    parsed = urlparse(str(kc.url))
    # Already authenticated (a still-valid cookie short-circuits the form).
    if parsed.hostname == STORE_HOST and parsed.path not in ("/login", ""):
        if client.cookies.get("session"):
            return

    block = _detect_interactive_block(kc.text)
    if block:
        raise ReleaseError(f"{block} Cannot complete login headlessly in CI.")

    soup = BeautifulSoup(kc.text, "lxml")
    form = soup.find("form", id="kc-form-login") or soup.find("form")
    if form is None or not form.get("action"):
        raise ReleaseError("Could not locate the Keycloak login form (page layout may have changed).")
    action = form["action"]
    if action.startswith("/"):
        action = f"https://{parsed.hostname}{action}"

    resp = client.post(
        action,
        data={
            "username": username,
            "password": password,
            "credentialId": "",
            "login": "Sign In",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    final_host = urlparse(str(resp.url)).hostname
    if final_host == STORE_HOST:
        if not client.cookies.get("session"):
            raise ReleaseError(
                "Login appeared to succeed but no session cookie was issued; "
                "the store may have rejected the OIDC callback."
            )
        return

    if final_host == SSO_HOST:
        block = _detect_interactive_block(resp.text)
        if block:
            raise ReleaseError(f"{block} Cannot complete login headlessly in CI.")
        raise ReleaseError(_detect_login_error(resp.text) or "Login was rejected by Keycloak.")

    raise ReleaseError(f"Unexpected redirect after login: ended on {resp.url} (status {resp.status_code}).")


# --------------------------------------------------------------------------- manage helpers


def _form_errors(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    errors: list[str] = []
    for el in soup.select(".error, .errors li, .alert-error, .invalid-feedback, .request-error"):
        text = el.get_text(" ", strip=True)
        if text:
            errors.append(text)
    return errors


def fetch_manage(client: httpx.Client, publisher: str, slug: str) -> BeautifulSoup:
    url = urljoin(BASE_URL + "/", f"asset/{publisher}/{slug}/manage/")
    resp = client.get(url)
    if resp.status_code >= 400:
        raise ReleaseError(f"manage page HTTP {resp.status_code}: {resp.text[:200]}")
    final = urlparse(str(resp.url))
    if final.hostname == SSO_HOST or final.path.rstrip("/").endswith("/login"):
        raise ReleaseError(
            "Not authenticated: the store redirected to login. Check the username/password secrets."
        )
    if "/manage/" not in str(resp.url):
        raise ReleaseError(f"Not authorized to manage {publisher}/{slug}.")
    soup = BeautifulSoup(resp.text, "lxml")
    if not soup.find("input", {"name": "csrf_token"}):
        raise ReleaseError("No CSRF token on manage page; session may be expired.")
    return soup


def csrf_token(soup: BeautifulSoup) -> str:
    token = soup.find("input", {"name": "csrf_token"})
    if not token or not token.get("value"):
        raise ReleaseError("csrf_token input missing on manage page.")
    return token["value"]


def post_manage_form(
    client: httpx.Client,
    publisher: str,
    slug: str,
    endpoint: str,
    data: dict[str, str],
) -> None:
    url = urljoin(BASE_URL + "/", endpoint.lstrip("/"))
    referer = urljoin(BASE_URL + "/", f"asset/{publisher}/{slug}/manage/")
    resp = client.post(
        url,
        data=data,
        files={},  # HTMX manage forms expect a multipart body
        headers={"Referer": referer, "HX-Request": "true"},
        follow_redirects=False,
    )
    if not (200 <= resp.status_code < 300):
        raise ReleaseError("; ".join(_form_errors(resp.text)) or resp.text[:300] or f"HTTP {resp.status_code}")
    # The returned fragment IS the new tab content — surface inline errors on 200 too.
    errors = _form_errors(resp.text)
    if errors:
        raise ReleaseError("; ".join(errors))


# --------------------------------------------------------------------------- upload


def upload_version(
    client: httpx.Client,
    publisher: str,
    slug: str,
    *,
    file_path: Path,
    version_name: str,
    changelog: str,
    stable: bool,
    min_godot_version: str,
    max_godot_version: str,
    version_notes: str,
) -> dict:
    """Three-step upload: request pre-signed URL → PUT to S3 → commit."""
    soup = fetch_manage(client, publisher, slug)
    csrf = csrf_token(soup)

    body = file_path.read_bytes()
    checksum_b64 = base64.b64encode(hashlib.md5(body).digest()).decode()

    common = {
        "csrf_token": csrf,
        "name": version_name,
        "changelog": changelog,
        "min_godot_version": min_godot_version,
        "max_godot_version": max_godot_version,
        "version_notes": version_notes,
    }
    if stable:
        common["stable"] = "y"

    manage_url = urljoin(BASE_URL + "/", f"asset/{publisher}/{slug}/manage/")
    hx_headers = {"X-CSRFToken": csrf, "Referer": manage_url, "HX-Request": "true"}

    # Step 1 — request a pre-signed upload URL.
    step1 = client.post(
        urljoin(BASE_URL + "/", f"asset/{publisher}/{slug}/version/upload_url/"),
        data={**common, "filename": file_path.name, "checksum": checksum_b64},
        files={},
        headers=hx_headers,
        follow_redirects=False,
    )
    if step1.status_code >= 400:
        raise ReleaseError(f"version/upload_url rejected: {_json_error(step1)}")
    try:
        payload = step1.json()
    except ValueError as e:
        raise ReleaseError(f"version/upload_url returned non-JSON: {step1.text[:200]}") from e
    upload_url = payload.get("upload_url")
    queue_id = payload.get("queue_id")
    if not upload_url or not queue_id:
        raise ReleaseError(f"version/upload_url missing fields: {payload!r}")

    # Step 2 — PUT the archive straight to the pre-signed S3 URL. The URL embeds
    # its own auth, so this request must NOT carry the store session cookie.
    with httpx.Client(timeout=600.0) as raw:
        put_resp = raw.put(
            upload_url,
            content=body,
            headers={"Content-Type": "application/octet-stream"},
        )
    if put_resp.status_code >= 400:
        raise ReleaseError(f"S3 upload failed: HTTP {put_resp.status_code} {put_resp.text[:300]}")

    # Step 3 — commit the upload.
    step3 = client.post(
        urljoin(BASE_URL + "/", f"asset/{publisher}/{slug}/version/create/"),
        data={**common, "queue_id": str(queue_id)},
        files={},
        headers=hx_headers,
        follow_redirects=False,
    )
    if step3.status_code >= 400:
        raise ReleaseError(f"version/create rejected: {_json_error(step3)}")

    return {
        "queue_id": str(queue_id),
        "filename": file_path.name,
        "checksum": checksum_b64,
        "size": len(body),
        "version": version_name,
    }


def _json_error(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error") or resp.text[:300]
    except ValueError:
        return resp.text[:300]


def submit_for_review(client: httpx.Client, publisher: str, slug: str) -> None:
    soup = fetch_manage(client, publisher, slug)
    post_manage_form(
        client,
        publisher,
        slug,
        f"asset/{publisher}/{slug}/update_status/",
        {"csrf_token": csrf_token(soup), "action": "mark_public"},
    )


# --------------------------------------------------------------------------- cli


def _str2bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _write_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release a version to the Godot Asset Store.")
    parser.add_argument("--publisher", required=True, help="Publisher slug (store.godotengine.org/publisher/<slug>/).")
    parser.add_argument("--asset", required=True, help="Asset slug.")
    parser.add_argument("--file", required=True, help="Path to the version .zip archive.")
    parser.add_argument("--version", required=True, help="Version name, e.g. 1.2.3.")
    parser.add_argument("--changelog", default="", help="Changelog text for this version.")
    parser.add_argument("--version-notes", default="", help="Extra notes shown on the version.")
    parser.add_argument("--min-godot-version", default="Undefined", help='Minimum Godot version (e.g. "4.3").')
    parser.add_argument("--max-godot-version", default="Undefined", help='Maximum Godot version (e.g. "4.6").')
    parser.add_argument("--stable", default="true", help="Mark the version as stable (true/false).")
    parser.add_argument("--submit", default="false", help="Submit the asset for review after upload (true/false).")
    parser.add_argument("--username", default=os.environ.get("GODOT_STORE_USERNAME"), help="Store username (prefer GODOT_STORE_USERNAME env).")
    args = parser.parse_args(argv)

    username = args.username
    password = os.environ.get("GODOT_STORE_PASSWORD")
    session_cookie = os.environ.get("GODOT_STORE_SESSION")
    if not session_cookie and not (username and password):
        print(
            "::error::Provide either GODOT_STORE_SESSION (a session cookie) or "
            "both GODOT_STORE_USERNAME and GODOT_STORE_PASSWORD.",
            file=sys.stderr,
        )
        return 2

    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"::error::Archive not found: {file_path}", file=sys.stderr)
        return 2
    if file_path.suffix.lower() != ".zip":
        print(f"::warning::Expected a .zip archive, got {file_path.name}", file=sys.stderr)

    stable = _str2bool(args.stable)
    submit = _str2bool(args.submit)

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html, */*"}
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as client:
            if session_cookie:
                # Seed the pre-obtained Flask session cookie and skip the
                # interactive Keycloak flow. Flask rotates `session` on every
                # response; the client's cookie jar tracks the rotation.
                client.cookies.set("session", session_cookie, domain=STORE_HOST, path="/")
                print("Using GODOT_STORE_SESSION cookie (skipping interactive login).")
            else:
                print(f"Logging in as {username} …")
                login(client, username, password)
                print("Login OK.")

            print(f"Uploading {file_path.name} as version {args.version} to {args.publisher}/{args.asset} …")
            result = upload_version(
                client,
                args.publisher,
                args.asset,
                file_path=file_path,
                version_name=args.version,
                changelog=args.changelog,
                stable=stable,
                min_godot_version=args.min_godot_version,
                max_godot_version=args.max_godot_version,
                version_notes=args.version_notes,
            )
            print(f"Upload committed (queue_id={result['queue_id']}, {result['size']} bytes).")

            if submit:
                print("Submitting for review …")
                submit_for_review(client, args.publisher, args.asset)
                print("Submitted for review.")
    except ReleaseError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"::error::HTTP error: {e}", file=sys.stderr)
        return 1

    _write_output("queue-id", result["queue_id"])
    _write_output("checksum", result["checksum"])
    _write_output("size", str(result["size"]))
    _write_output("submitted", "true" if submit else "false")
    print(f"::notice::Released {args.publisher}/{args.asset} version {args.version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
