# RescueMesh Field Relay — Android

**Field Relay** is an optional native Android surface for RescueMesh. It is deliberately different from the judge Command Center: the Command Center is for coordinators, while Field Relay is for residents and volunteers operating at the edge of a disaster.

The app is **offline-first**. An incident report or community resource offer is written to a durable on-device outbox first. If the RescueMesh server is reachable it synchronizes immediately; if connectivity disappears, the report stays queued and is retried later using a caller-generated idempotency key so reconnect/retry does not create duplicate resources or incidents.

## Install the ready APK

The repository includes an installable competition build:

`mobile/releases/rescuemesh-field-relay-1.0.0.apk`

The committed APK is a non-debuggable release build signed with a competition-only demo certificate. It is intended for judging/sideloading and is not presented as a Play Store production release. Its SHA-256 is recorded in `mobile/releases/SHA256SUMS.txt`.

With Android platform tools installed:

```bash
adb install -r mobile/releases/rescuemesh-field-relay-1.0.0.apk
```

You can also copy the APK to an Android phone and open it there.

## Connect it to RescueMesh

Start the same Agent Kernel REST Command Center used by judges:

```bash
uv run python command_center.py
```

The server listens on port `8000`. In the app, set **RescueMesh server URL** to:

- Android emulator: `http://10.0.2.2:8000`
- physical phone on the same Wi-Fi: `http://<laptop-LAN-IP>:8000`

On Linux, `hostname -I` is a quick way to find the laptop LAN address.

## 90-second field demo

1. Open Field Relay and connect it to the running Command Center.
2. Disable Wi-Fi/mobile data on the phone.
3. Submit a flood incident and a boat/resource offer. The outbox count increases; nothing is lost.
4. Re-enable connectivity and tap **Sync queued**.
5. Open `http://localhost:8000/rescuemesh` on the laptop. Both entries appear in the same privacy-safe operational ledger and are considered by the network allocation planner.
6. Re-submit is safe: the API keeps the mobile request ID and treats a retry as an idempotent replay.

The app never performs dispatch. Allocation is still a dry-run proposal and confirmation remains a named human action in the Command Center.

## Build from source

Requirements: JDK 17, Android SDK 35, and `ANDROID_HOME` set.

```bash
cd mobile/android
./build-apk.sh
```

The build helper produces a normal debug APK from source; the repository also carries the prebuilt demo-signed release APK above. The project intentionally has no third-party Android runtime dependencies. It uses platform `HttpURLConnection`, `SharedPreferences`, and `org.json`, keeping the field client small and auditable.
