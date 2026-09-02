import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APK = ROOT / "mobile" / "releases" / "rescuemesh-field-relay-1.0.0.apk"


def test_mobile_apk_is_packaged_and_checksum_matches():
    assert APK.exists()
    assert APK.stat().st_size > 10_000
    assert APK.read_bytes()[:2] == b"PK"
    expected = (ROOT / "mobile" / "releases" / "SHA256SUMS.txt").read_text().split()[0]
    actual = hashlib.sha256(APK.read_bytes()).hexdigest()
    assert actual == expected


def test_mobile_client_requests_only_network_permissions():
    manifest = (ROOT / "mobile" / "android" / "app" / "src" / "main" / "AndroidManifest.xml").read_text()
    assert "android.permission.INTERNET" in manifest
    assert "android.permission.ACCESS_NETWORK_STATE" in manifest
    assert "ACCESS_FINE_LOCATION" not in manifest
    assert "READ_CONTACTS" not in manifest
