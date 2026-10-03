#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ -n "${JAVA_HOME:-}" ]]; then
    export PATH="$JAVA_HOME/bin:$PATH"
fi
: "${BRAGI_ANDROID_KEYSTORE:?Set the release keystore path}"
: "${BRAGI_ANDROID_STORE_PASSWORD:?Set the keystore password}"
: "${BRAGI_ANDROID_KEY_ALIAS:?Set the signing alias}"
: "${BRAGI_ANDROID_KEY_PASSWORD:?Set the key password}"
if [[ ! -f "$BRAGI_ANDROID_KEYSTORE" ]]; then
    echo 'Release keystore does not exist.' >&2
    exit 1
fi
sdk_path="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [[ -z "$sdk_path" && -f local.properties ]]; then
    sdk_path=$(sed -n 's/^sdk.dir=//p' local.properties | head -n 1)
fi
apksigner_path="$sdk_path/build-tools/35.0.0/apksigner"
if [[ ! -x "$apksigner_path" ]]; then
    echo 'Set ANDROID_HOME to an SDK with build-tools 35.0.0.' >&2
    exit 1
fi
./gradlew :app:assembleRelease :app:testReleaseUnitTest :app:lintRelease --console=plain
"$apksigner_path" verify app/build/outputs/apk/release/app-release.apk
mkdir -p app/build/outputs/release
cp app/build/outputs/apk/release/app-release.apk app/build/outputs/release/BragiAndroid.apk
(cd app/build/outputs/release && sha256sum BragiAndroid.apk > BragiAndroid.apk.sha256)
echo 'Signed APK: app/build/outputs/release/BragiAndroid.apk'
