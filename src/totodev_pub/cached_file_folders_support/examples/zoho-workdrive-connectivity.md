# Running the Zoho WorkDrive demo against a real account

This guide walks through connecting the
[`zoho_workdrive_sync.py`](./zoho_workdrive_sync.py)
example to a live Zoho WorkDrive account: where each credential comes from, how to find
the folder you want to mirror, and a recommended way to store it all in a single sourced
shell script.

> The example itself (offline behavior, retention policy, truncation, summarizing/indexing)
> is covered by its module docstring and the unit tests. This document is only about
> **actual connectivity** — getting real credentials and pointing the tool at real data.

---

## 0. What you'll end up with

A small, gitignored shell script you `source` before each run, plus one command:

```bash
source volatile/credentials/zoho_workdrive_env.sh
python -m totodev_pub.cached_file_folders_support.examples.zoho_workdrive_sync \
    --cache-root volatile/zoho_wd_sync/ --dir-key demo --max-files 5
```

Everything secret (and the folder to mirror) lives in that one script.

---

## 1. Install dependencies

```bash
pip install "totodev-pub[connectors]"      # installs requests
# optional, for nicer summaries of non-text files:
pip install pypandoc                        # plus the `pandoc` system binary
```

---

## 2. Acquire the OAuth credentials (Self Client)

The demo authenticates as a **Self Client** — Zoho's OAuth flow for an unattended,
single-account backend job. You do a one-time browser dance to get a permanent
**refresh token**; the program mints short-lived access tokens from it automatically.

### 2a. Client ID and Client Secret

1. Go to the Zoho API Console for your region: `https://api-console.zoho.<dc>`
   (e.g. `https://api-console.zoho.com`). Sign in with the account that owns the files.
2. **GET STARTED → Self Client → CREATE NOW**.
3. Copy the **Client ID** and **Client Secret**. These are the values for
   `ZOHO_WD_CLIENT_ID` and `ZOHO_WD_CLIENT_SECRET`.

### 2b. Generate a grant code (with the right scopes)

1. In the Self Client, open the **Generate Code** tab.
2. Enter the scopes (comma-separated). For full functionality including downloads of
   *non-native* files, use:

   ```
   WorkDrive.files.ALL,WorkDrive.team.READ,WorkDrive.workspace.READ,ZohoFiles.files.ALL
   ```

   (Read-only listing alone needs only `WorkDrive.files.READ`, but downloads of regular
   files require the broader `WorkDrive.files.ALL` + `ZohoFiles.files.ALL`.)
3. Set a description and an expiry, then **CREATE**. Copy the one-time **grant code**.
   ⚠️ It expires quickly (often 3–10 minutes) — do the next step right away.

### 2c. Exchange the grant code for a permanent refresh token

Use the bundled helper:

```bash
python -m totodev_pub.cached_file_folders_support.examples.zoho_workdrive_token_bootstrap \
    --client-id 1000.XXXX --client-secret YYYY --code 1000.ZZZZ --dc com
```

It prints a `refresh_token` (which does **not** expire) — that's `ZOHO_WD_REFRESH_TOKEN`.
If the code already expired, just generate a fresh one in 2b and retry.

### 2d. Data center / region (`ZOHO_WD_DC`)

This is the suffix in your Zoho URLs: `com`, `eu`, `in`, `com.au`, `jp`, etc. It must
match the account's region (the same `<dc>` you used for the API Console). If you're not
sure, check the domain when you're logged into WorkDrive in a browser.

---

## 3. Find the root folder id (what to mirror)

The tool descends recursively from **one folder id**. You provide it because mirroring
"everything" is rarely what you want — pick the subtree you care about.

To get the id, open the folder in WorkDrive in your browser and look at the URL. The id
is the **trailing segment after `/folders/`** (or the last id segment of a folder URL):

```
https://workdrive.zoho.com/.../ws/<workspace-id>/folders/<ROOT-FOLDER-ID>
                                                          ^^^^^^^^^^^^^^^^
```

- A **workspace/Team Folder root** id also works — that mirrors the whole workspace.
- The plain `.../folders/files` landing page is *not* a specific folder; navigate into an
  actual folder so a concrete id appears in the URL.

> **You don't have to extract the id by hand.** Both `--root-folder-id` and
> `ZOHO_WD_ROOT_FOLDER_ID` accept either a bare id *or* a pasted folder URL — the tool
> pulls the id out of the URL automatically (and if you paste the `.../folders/files`
> landing URL, it falls back to the workspace id). It prints which id it resolved.

This id is **not a secret** — it's configuration. You can pass it two ways:

- `--root-folder-id <id-or-url>` on the command line (handy for one-off runs), **or**
- `ZOHO_WD_ROOT_FOLDER_ID` in your env script (handy when you always sync the same tree).

Putting it in the shell script (next section) is the most convenient for repeated runs;
the CLI flag overrides the env var when you want to target a different folder ad hoc.

---

## 4. Create your credentials shell script

Store everything in a single gitignored script under your project's
`volatile/credentials/` folder (the `volatile/` tree is the conventional home for local,
throwaway, secret-bearing files). Create `volatile/credentials/zoho_workdrive_env.sh`:

```bash
#!/usr/bin/env bash
# Local Zoho WorkDrive credentials + target folder for the TRUNCATE demo.
# Source before running:   source volatile/credentials/zoho_workdrive_env.sh
# SECURITY: this file holds secrets. Keep it under volatile/ (gitignored). Rotate if leaked.

# --- OAuth (from the Self Client; see the connectivity guide, section 2) ---
export ZOHO_WD_CLIENT_ID="1000.XXXXXXXXXXXXXXXX"
export ZOHO_WD_CLIENT_SECRET="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export ZOHO_WD_REFRESH_TOKEN="1000.aaaaaaaa.bbbbbbbb"
export ZOHO_WD_DC="com"                       # data center / region (com/eu/in/com.au/jp/...)

# --- What to mirror (config, not a secret; section 3). Optional: omit to use --root-folder-id. ---
export ZOHO_WD_ROOT_FOLDER_ID="abcd1234your_folder_id"

# --- Optional explicit host overrides (otherwise derived from DC / token api_domain) ---
# export ZOHO_WD_API_HOST="https://www.zohoapis.com/workdrive/api/v1"
# export ZOHO_WD_DOWNLOAD_HOST="https://download.zoho.com/v1/workdrive"

echo "Zoho WorkDrive env loaded (DC=$ZOHO_WD_DC, client=${ZOHO_WD_CLIENT_ID})"
```

Make sure `volatile/` is gitignored (it is in this repo). Never commit real secrets.

---

## 5. Run it

```bash
source volatile/credentials/zoho_workdrive_env.sh

# Tiny smoke run (5 files), root folder taken from ZOHO_WD_ROOT_FOLDER_ID:
python -m totodev_pub.cached_file_folders_support.examples.zoho_workdrive_sync \
    --cache-root volatile/zoho_wd_sync/ --dir-key smoke --max-files 5 --debug

# Or target a different folder ad hoc (overrides the env var):
python -m totodev_pub.cached_file_folders_support.examples.zoho_workdrive_sync \
    --cache-root volatile/zoho_wd_sync/ --dir-key other \
    --root-folder-id <some-other-folder-id>
```

(If running from a source checkout rather than an installed package, prefix with
`PYTHONPATH=src`.)

---

## 6. Verify

- Files appear under `volatile/zoho_wd_sync/key-<dir_key>/`, mirroring the WorkDrive paths.
- Each file has a `*._slave/` directory with `metadata.yaml`, `summary.md`, `index.json`.
- Files over the truncate threshold have a zero-byte body + `_truncation_info.yaml`.
- Re-running with no changes reports `0 inserted, 0 updated, 0 deleted` (idempotent).

See the smoke-test checklist for the full set of things to confirm (truncation, native
docs, archives, deletes).

---

## 7. Security notes

- Treat `ZOHO_WD_CLIENT_SECRET` and `ZOHO_WD_REFRESH_TOKEN` like passwords. The refresh
  token does not expire — anyone holding it can read your files until you revoke it.
- Revoke/rotate from the Zoho API Console (delete the Self Client or regenerate) if a
  value leaks.
- Keep credential scripts under `volatile/` (gitignored). Do not paste secrets into code,
  commits, or chat.
- Prefer the narrowest scopes that work for your use case.
