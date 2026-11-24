# WD My Cloud Backup for Home Assistant

[![CI](https://github.com/Jetschin/ha-wd-mycloud/actions/workflows/ci.yml/badge.svg)](https://github.com/Jetschin/ha-wd-mycloud/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Support this project](https://img.shields.io/badge/Support-this%20project-ffdd00.svg)](https://paypal.me/jetschin)

Store your Home Assistant backups on a **WD My Cloud Home**. After setup,
"WD My Cloud" shows up as a backup location in *Settings → System → Backups*,
next to "Local" and "Home Assistant Cloud". Uploading, listing, downloading,
restoring and deleting (retention rules) all go through Home Assistant's
standard backup system. No YAML, no rclone.

## Supported devices

- **WD My Cloud Home** via your WD account (email + password), using the private area of the device.
- Other My Cloud models: planned for a future release via local SMB.

## Installation (HACS)

Requires Home Assistant 2025.3 or newer.

1. In HACS, open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/Jetschin/ha-wd-mycloud` with category **Integration**.
3. Install **WD My Cloud Backup** and restart Home Assistant.

## Setup

1. Go to **Settings → Devices & Services → Add integration** and choose **WD My Cloud**.
2. Choose **WD account (My Cloud Home)**.
3. Enter your WD account email and password.
4. If your account has several devices, pick one.
5. Choose the target folder (default `HA_Backup`; created if missing, nested paths like `a/b/c` are allowed).
6. Go to **Settings → System → Backups** and enable **WD My Cloud** as a backup location.

## Privacy

- Your WD password is **never stored**. It is used once to log in; only a refresh token, the device ID and the folder are saved.
- Home Assistant encrypts backups by default (unless you disable encryption for a backup).
- Files are named the way Home Assistant names backups: `<name>_<date>_<time>.tar` plus a matching `.metadata.json`. The integration only modifies or deletes files it created; other files in the target folder are ignored and never touched.

## Disclaimer

This is an unofficial project and is not affiliated with Western Digital. It uses
the WD Off-Device cloud API with the public client ID of the WD apps (as known from
the open-source project [mchfuse](https://github.com/mchfuse/mchfuse)). **WD has marked
this API as deprecated and may switch it off at any time.** If that happens, this
integration stops working. Keep another backup location as well.

## Troubleshooting

- **Invalid credentials:** check email and password.
- **Cannot connect:** the WD service or your device may be offline. The integration tries the local address first and falls back to the WD cloud automatically.
- **Re-authentication requested:** your refresh token became invalid (for example after a password change). Re-authenticate from the integration card.
- **A `.tar` without `.metadata.json` in the folder:** an interrupted upload (for example a Home Assistant restart during the backup) can leave this behind. It is ignored by the integration and can be deleted manually.
- **Backup to this location failed:** only this location fails; other locations still complete. Check the Home Assistant log for `wd_mycloud`.

## Support this project ☕

If this integration saves you time, you can support its development:

[![Support this project](https://img.shields.io/badge/Support-this%20project-ffdd00.svg)](https://paypal.me/jetschin)

Donation link: [paypal.me/jetschin](https://paypal.me/jetschin)

## License

[MIT](LICENSE) © 2026 Marco Jetschin
