"""Build-time offline-renderer dependency; validate the official binary's checksum."""
import hashlib
import os
from pathlib import Path
import re
import urllib.request

version = os.environ["KUBECTL_VERSION"]
arch = os.environ["TARGETARCH"]
if not re.fullmatch(r"v\d+\.\d+\.\d+", version) or arch not in {"amd64", "arm64"}:
    raise ValueError("unsupported kubectl build arguments")
url = f"https://dl.k8s.io/release/{version}/bin/linux/{arch}/kubectl"
with urllib.request.urlopen(url + ".sha256", timeout=60) as response:
    expected = response.read(100).decode().strip()
if not re.fullmatch(r"[0-9a-f]{64}", expected):
    raise ValueError("invalid official kubectl checksum")
target = Path("/usr/local/bin/kubectl")
digest = hashlib.sha256()
with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as output:
    while chunk := response.read(1024 * 1024):
        digest.update(chunk)
        output.write(chunk)
if digest.hexdigest() != expected:
    target.unlink()
    raise ValueError("kubectl checksum mismatch")
target.chmod(0o755)
