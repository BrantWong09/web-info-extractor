# v2rayN Subscription Auto-Update Support

## Date: 2026-09-04

## Background

`web-info-extractor` is a tool that automates proxy node subscription updates from a YouTube channel (<频道名>（隐去）). The current flow:

1. Scrapes the channel for the latest "stable node" video
2. Extracts a paste.to download link from the video description
3. Extracts a password from the video transcript
4. Decrypts the paste.to content
5. Extracts a Clash subscription link from the decrypted content
6. Downloads and filters the Clash config, updates Clash Party (mihomo-party)

v2rayN is already installed locally (paths configured in `config.yaml`). The codebase has an `update_v2rayn_sub()` function that is written but never called from the main flow.

## Goal

Enable automatic v2rayN subscription updates alongside the existing Clash Party updates, so both clients stay in sync with the latest nodes from the YouTube channel.

## Link Identification

The decrypted paste.to content contains two `dlink.host` links, both with `.jpg` extensions. After base64-decoding the path segment:

- **v2ray link**: decoded URL contains `/t/c/` (OneDrive shared text file)
- **Clash link**: decoded URL contains `/u/c/` (OneDrive shared file, different path)

Example:
- v2ray: `https://dlink.host/1drv/aHR0cHM6...s3.jpg` → decodes to `https://1drv.ms/t/c/cd5f084e...`
- Clash: `https://dlink.host/1drv/aHR0cHM6...RM.jpg` → decodes to `https://1drv.ms/u/c/cd5f084e...`

## Design

### Approach: Minimal change to `main_async()`

Modify the link extraction section of `main_async()` in `test_youtube.py` (approximately lines 435-458) to extract both links in a single pass through the URL list.

### Changes

**1. `main_async()` in `test_youtube.py`**

In the existing `for u in urls` loop that searches for Clash links:
- Also check for `/t/c/` in the base64-decoded string to identify v2ray links
- Store both `clash_url` and `v2ray_url`
- After the loop, if `v2ray_url` is found, call `await update_v2rayn_sub(v2ray_url)`
- The existing `update_clash_party_sub(clash_url)` call remains unchanged

**2. `update_v2rayn_sub()` (already implemented, no changes needed)**

The function at line 363-386 already:
- Copies the DB to a temp file
- Finds the `youtube` subscription item by remarks
- Updates the URL if different
- Copies back to the original location
- Handles PermissionError when v2rayN is running

**3. Constants (no changes needed)**

`V2RAYN_DB = "<v2rayN 安装目录>/guiConfigs/guiNDB.db"` already points to the correct location.

### Data Flow

```
YouTube video → transcript password → decrypt paste.to content
    → extract all dlink.host + jpg URLs
    → base64 decode each:
        /t/c/ → v2ray URL  → update_v2rayn_sub()  → update v2rayN DB
        /u/c/ → Clash URL  → update_clash_party_sub() → update Clash Party
```

### Error Handling

- If no v2ray link found: print message, continue to Clash update (non-blocking)
- If no Clash link found: print message, continue to v2ray update (non-blocking)
- `update_v2rayn_sub` already handles PermissionError when v2rayN is running

### Testing

Run `python test_youtube.py` and verify:
1. Console output shows both v2ray and Clash subscription URLs
2. v2rayN DB `SubItem` table `youtube` row has updated URL
3. Clash Party config updated (existing behavior unchanged)