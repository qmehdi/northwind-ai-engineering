---
title: Mobile App Support
doc_id: mobile-app-support
audience: customer
effective: 2024-08-20
supersedes: none
---

# Mobile App Support

This document explains what mobile platforms Northwind Cloud supports, how to report crashes, what offline mode does and does not do, how to troubleshoot push notifications, biometric login requirements, and what happens when a device falls outside our supported range. It applies to the Northwind Cloud mobile app on iOS and Android.

## Supported Operating System Versions

Northwind Cloud supports the last two major releases of iOS and the last two major releases of Android. As of the effective date of this policy, that means:

1. iOS: the two most recent major iOS versions.
2. Android: the two most recent major Android versions.

When a new major OS version ships, our support window shifts forward automatically and the oldest supported version is retired. We recommend keeping automatic OS updates enabled so your device stays within the supported range without manual action.

## Crash Reporting

If the app crashes, closes unexpectedly, or freezes, please report it so our engineering team can investigate.

1. Open the app and go to Settings, then Help, then Report a Problem.
2. Select Crash or Freeze as the issue type.
3. Add a short description of what you were doing when the crash occurred.
4. Tap Send Diagnostic Log. This attaches the last 7 days of cached app data (see Offline Mode below) along with device model, OS version, and app version.

When you contact support directly, please include:

1. Device model and OS version.
2. App version number, found under Settings, then About.
3. Approximate time the crash occurred.
4. Steps to reproduce, if known.
5. Screenshot of any error message shown on screen.

Diagnostic logs are retained for 30 days after submission and are used solely for troubleshooting.

## Offline Mode

The Northwind Cloud mobile app includes an offline mode for users who lose network connectivity while working.

1. Offline mode is read-only. You cannot create, edit, or delete records while offline.
2. The app caches up to 7 days of data for offline viewing. Data older than 7 days is not available offline and must be retrieved when connectivity is restored.
3. Any changes queued while offline (such as draft comments) are not saved until the device reconnects and the app syncs successfully.
4. Once connectivity returns, the app automatically syncs and refreshes the local cache. We recommend opening the app on a stable connection at least once every 7 days to keep cached data current.
5. Offline mode does not support attachments larger than 25 MB; these load only when online.

If cached data appears outdated after reconnecting, force a manual refresh from the Home screen by pulling down, or sign out and sign back in.

## Push Notification Troubleshooting

If you are not receiving push notifications, work through these steps in order:

1. Confirm notifications are enabled in your device's system settings for the Northwind Cloud app.
2. Confirm notifications are enabled inside the app under Settings, then Notifications.
3. Check that the device has an active internet connection, either Wi-Fi or cellular data.
4. Confirm the app is updated to the latest version available for your OS.
5. Restart the app, and if that does not resolve the issue, restart the device.
6. Sign out and sign back in to refresh your device's push token with our servers.
7. If notifications still do not arrive after these steps, contact support with your device model, OS version, and app version so we can check server-side delivery logs.

Note that devices in low power mode or with battery optimization enabled for the app may experience delayed notifications. We recommend excluding the Northwind Cloud app from aggressive battery optimization settings.

## Biometric Login

Biometric login (Face ID, Touch ID, or fingerprint unlock, depending on device) is optional and can be enabled under Settings, then Security.

1. Biometric login requires that biometric authentication is already set up and enabled at the device operating system level.
2. Biometric login requires an active Northwind Cloud session; it is used to unlock the app quickly, not to create a new session from scratch.
3. If biometric login fails three times in a row, the app falls back to requiring your standard email and password sign in.
4. Biometric login is disabled automatically if you sign out manually or if your session expires after 90 days of inactivity.
5. Biometric data itself is never stored or transmitted by Northwind Cloud; authentication is handled entirely by the device's operating system.

## When a Device Is Unsupported

A device is considered unsupported when its operating system version falls outside the last two major iOS or Android releases described above.

1. On unsupported devices, the app may continue to function but is not guaranteed to work correctly, and new features may not be available.
2. Support tickets related to bugs on unsupported devices may be closed with a recommendation to update the OS or device.
3. Security patches and performance improvements are only tested against supported OS versions.
4. If your device cannot be updated to a supported OS version, we recommend accessing Northwind Cloud through a supported web browser instead, where all core features remain available.

For further assistance with any topic in this document, contact Northwind Cloud support through the in-app Help menu or your usual support channel.
