---
title: VPN setup with GlobalConnect
department: IT
classification: internal
groups: all-employees
updated: 2026-05-12
---
All remote access to the corporate network goes through GlobalConnect 6.2. Install the client from Self Service on macOS or Software Center on Windows, then connect to the gateway vpn.halcyon.example. Sign in with your Halcyon SSO account and approve the Okta Verify push on your phone.

The client connects automatically when you are off the office network. Split tunnelling is enabled, so video calls and public websites do not go through the tunnel; internal tools such as Jira, Vault and the build farm do.

Error GC-809 means the device certificate on your laptop has expired. Open the Device Enrollment portal, choose "Re-enroll this device" and restart the client. Error GC-512 means the gateway cannot reach your identity provider; wait five minutes and retry, then raise a ServiceDesk ticket if it persists.

If you still cannot reach internal systems from home, check that your laptop clock is correct and that no other VPN product is running.
