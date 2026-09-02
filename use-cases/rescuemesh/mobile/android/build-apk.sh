#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${ANDROID_HOME:-}" ]]; then
  echo "Set ANDROID_HOME to your Android SDK directory (SDK 35 recommended)." >&2
  exit 1
fi

./gradlew :app:assembleDebug
mkdir -p ../releases
cp app/build/outputs/apk/debug/app-debug.apk ../releases/rescuemesh-field-relay-debug.apk
sha256sum ../releases/rescuemesh-field-relay-debug.apk
