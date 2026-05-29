# godot-asset-store-release

A GitHub Action that publishes a new version archive to the **Godot Asset
Store** — <https://store.godotengine.org/> — the new beta marketplace, **not**
the old [asset library](https://godotengine.org/asset-library/).

It logs in via Keycloak SSO, uploads your `.zip` through the store's pre-signed
S3 flow, and (optionally) submits the asset for moderator review — all from CI.

> [!WARNING]
> The new asset store is in beta and has no public JSON API. This action drives
> the HTMX-rendered manage pages and scrapes CSRF tokens out of the HTML, so an
> upstream selector or URL change can break it until the script is updated. The
> publish flow is ported from
> [godot-store-mcp](https://github.com/NodotProject/godot-store-mcp).

## What it does

1. **Log in** — runs the Keycloak OIDC authorization-code flow with pure
   `httpx` (no browser) to obtain the store `session` cookie. If Keycloak ever
   demands reCAPTCHA / 2FA, the run fails with a clear message (those can't be
   completed headlessly in CI).
2. **Upload the version** — requests a pre-signed upload URL, `PUT`s the archive
   straight to S3, then commits the upload via `/version/create/`.
3. **Submit for review** *(optional)* — marks the asset public
   (`submit-for-review: true`), equivalent to clicking **Publish**.

## Prerequisites

- The asset must already exist on the store (create it once via the web UI or
  the `store_create_asset` MCP tool). This action publishes **versions** to an
  existing asset; it does not create the listing.
- Store credentials saved as repository secrets:
  - `GODOT_STORE_USERNAME`
  - `GODOT_STORE_PASSWORD`

## Usage

```yaml
- name: Publish to the Godot Asset Store
  uses: NodotProject/godot-asset-store-release@v1
  with:
    username: ${{ secrets.GODOT_STORE_USERNAME }}
    password: ${{ secrets.GODOT_STORE_PASSWORD }}
    publisher: your-publisher-slug
    asset-slug: your-asset-slug
    file: release.zip
    version: 1.2.3
    changelog: "Bug fixes and a new demo scene."
    min-godot-version: "4.3"
    max-godot-version: "4.6"
    stable: "true"
    submit-for-review: "false"
```

A complete tag-triggered workflow lives in
[`.github/workflows/release.yml`](.github/workflows/release.yml) — copy it into
your own project and adjust the build step + asset coordinates.

The asset coordinates come from its URL:
`https://store.godotengine.org/asset/<publisher>/<asset-slug>/`.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `username` | ✅ | — | Store account username (use a secret). |
| `password` | ✅ | — | Store account password (use a secret). |
| `publisher` | ✅ | — | Publisher slug. |
| `asset-slug` | ✅ | — | Asset slug. |
| `file` | ✅ | — | Path to the version `.zip`. |
| `version` | ✅ | — | Version name, e.g. `1.2.3`. |
| `changelog` | | `""` | Changelog text for this version. |
| `version-notes` | | `""` | Extra notes shown on the version. |
| `min-godot-version` | | `Undefined` | Minimum Godot version, e.g. `4.3`. |
| `max-godot-version` | | `Undefined` | Maximum Godot version, e.g. `4.6`. |
| `stable` | | `"true"` | Mark the version as stable. |
| `submit-for-review` | | `"false"` | Submit the asset for review after upload. |
| `python-version` | | `"3.12"` | Python version used to run the script. |

## Outputs

| Output | Description |
| --- | --- |
| `queue-id` | Upload queue id returned by the store. |
| `checksum` | Base64-encoded MD5 of the uploaded archive. |
| `size` | Uploaded archive size in bytes. |
| `submitted` | Whether the asset was submitted for review. |

## Local testing

The release script runs standalone — handy for verifying credentials before
wiring up CI:

```bash
pip install -r requirements.txt
export GODOT_STORE_USERNAME=you
export GODOT_STORE_PASSWORD=secret
python release.py \
  --publisher your-publisher-slug \
  --asset your-asset-slug \
  --file release.zip \
  --version 1.2.3 \
  --changelog "Test upload" \
  --stable true
# add --submit true to publish for review
```

Credentials are read from the environment so they never appear in `argv` or
the workflow logs.

## License

MIT — see [LICENSE](LICENSE).
