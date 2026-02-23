#!/usr/bin/env python3
"""
Download Kibana AI Assistant knowledge base artifacts from the Elastic S3 bucket.

Cross-platform script — works on Windows, macOS, and Linux without changes.
Uses only the Python standard library (no pip install needed).

Usage:
    python download_elastic_artifacts.py                        # interactive version prompt
    python download_elastic_artifacts.py --version 9.3          # download all 9.3 artifacts
    python download_elastic_artifacts.py --version 9.3 --multilingual  # include multilingual variants
    python download_elastic_artifacts.py --list                 # list all available artifacts
    python download_elastic_artifacts.py --output ./my-folder   # custom output directory

Proxy support (optional):
    python download_elastic_artifacts.py --proxy http://proxy.corp:8080 --version 9.3
    HTTP_PROXY=http://proxy:8080 python download_elastic_artifacts.py --version 9.3
    HTTPS_PROXY=http://proxy:8080 python download_elastic_artifacts.py --version 9.3
"""

import argparse
import os
import platform
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import URLError
from urllib.request import (
    ProxyHandler,
    build_opener,
    install_opener,
    urlopen,
    urlretrieve,
)

BASE_URL = "https://kibana-knowledge-base-artifacts.elastic.co"
PRODUCTS = ["elasticsearch", "kibana", "observability", "security"]
DEFAULT_OUTPUT = str(Path("..", "elastic-artifacts"))


def setup_proxy(proxy_url: str | None = None) -> str | None:
    """Configure urllib to use an HTTP/HTTPS proxy.

    Priority: explicit --proxy flag > HTTPS_PROXY env > HTTP_PROXY env.
    Returns the proxy URL that was configured, or None.
    """
    url = (
        proxy_url
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    if url:
        proxy_handler = ProxyHandler({"http": url, "https": url})
        opener = build_opener(proxy_handler)
        install_opener(opener)
    return url


def fetch_artifact_list(base_url: str) -> list[dict]:
    """Fetch and parse the S3 bucket listing XML."""
    try:
        with urlopen(base_url, timeout=30) as resp:
            xml_data = resp.read()
    except URLError as e:
        print(f"Error fetching artifact index from {base_url}: {e}")
        sys.exit(1)

    root = ET.fromstring(xml_data)
    ns = {"s3": "http://doc.s3.amazonaws.com/2006-03-01"}

    artifacts = []
    for contents in root.findall("s3:Contents", ns):
        key = contents.findtext("s3:Key", "", ns)
        size = int(contents.findtext("s3:Size", "0", ns))
        etag = contents.findtext("s3:ETag", "", ns).strip('"')
        last_modified = contents.findtext("s3:LastModified", "", ns)
        artifacts.append(
            {"key": key, "size": size, "etag": etag, "last_modified": last_modified}
        )
    return artifacts


def filter_artifacts(
    artifacts: list[dict], version: str, multilingual: bool = False
) -> list[dict]:
    """Filter artifacts by Kibana version and multilingual preference."""
    matched = []
    for a in artifacts:
        key = a["key"]
        if f"-{version}." not in key and not key.endswith(f"-{version}.zip"):
            continue
        is_multilingual = "--." in key or "multilingual" in key
        if is_multilingual and not multilingual:
            continue
        matched.append(a)
    return matched


def human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def list_artifacts(artifacts: list[dict]) -> None:
    """Print all available artifacts."""
    print(f"\n{'Artifact Key':<75} {'Size':>10}  {'Last Modified'}")
    print("-" * 110)
    for a in artifacts:
        print(f"{a['key']:<75} {human_size(a['size']):>10}  {a['last_modified']}")
    print(f"\nTotal: {len(artifacts)} artifacts\n")


def download_artifacts(
    artifacts: list[dict], base_url: str, output_dir: str
) -> None:
    """Download a list of artifacts to the output directory."""
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    total = len(artifacts)
    total_size = sum(a["size"] for a in artifacts)
    print(f"\nDownloading {total} artifact(s) ({human_size(total_size)}) to: {out}\n")

    for i, a in enumerate(artifacts, 1):
        key = a["key"]
        url = f"{base_url}/{key}"
        dest = out / key
        print(f"  [{i}/{total}] {key} ({human_size(a['size'])})")
        try:
            urlretrieve(url, str(dest))
            print(f"         -> saved to {dest}")
        except URLError as e:
            print(f"         -> FAILED: {e}")

    print("\nDone.")


def get_available_versions(artifacts: list[dict]) -> list[str]:
    """Extract unique version strings from artifact keys."""
    versions = set()
    for a in artifacts:
        key = a["key"]
        if not key.startswith("kb-product-doc-"):
            continue
        for product in PRODUCTS:
            prefix = f"kb-product-doc-{product}-"
            if key.startswith(prefix):
                rest = key[len(prefix):]
                ver = rest.split(".zip")[0].split("--")[0]
                if ver and ver[0].isdigit():
                    versions.add(ver.rstrip("."))
    return sorted(versions, key=lambda v: list(map(int, v.split("."))))


def main():
    parser = argparse.ArgumentParser(
        description="Download Elastic AI Assistant knowledge base artifacts. "
        "Works on Windows, macOS, and Linux.",
    )
    parser.add_argument(
        "--version", "-v", help="Kibana version to download (e.g. 9.3)"
    )
    parser.add_argument(
        "--multilingual",
        "-m",
        action="store_true",
        help="Also download multilingual embedding variants",
    )
    parser.add_argument(
        "--list", "-l", action="store_true", help="List all available artifacts and exit"
    )
    parser.add_argument(
        "--output",
        "-o",
        default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--url",
        default=BASE_URL,
        help=f"Base repository URL (default: {BASE_URL})",
    )
    parser.add_argument(
        "--proxy",
        "-p",
        default=None,
        help="HTTP/HTTPS proxy URL (e.g. http://proxy:8080). "
        "Also reads HTTPS_PROXY / HTTP_PROXY env vars as fallback.",
    )
    args = parser.parse_args()

    # --- proxy setup ---
    active_proxy = setup_proxy(args.proxy)
    if active_proxy:
        print(f"Using proxy: {active_proxy}")

    print(f"Platform: {platform.system()} ({platform.platform()})")
    print(f"Fetching artifact index from {args.url} ...")
    artifacts = fetch_artifact_list(args.url)

    if args.list:
        list_artifacts(artifacts)
        return

    version = args.version
    if not version:
        versions = get_available_versions(artifacts)
        print(f"\nAvailable versions: {', '.join(versions)}")
        version = input("Enter the Kibana version to download: ").strip()
        if not version:
            print("No version specified. Exiting.")
            return

    matched = filter_artifacts(artifacts, version, args.multilingual)
    if not matched:
        print(f"\nNo artifacts found for version '{version}'.")
        print("Use --list to see all available artifacts.")
        return

    print(f"\nArtifacts for version {version}:")
    for a in matched:
        print(f"  - {a['key']} ({human_size(a['size'])})")

    download_artifacts(matched, args.url, args.output)


if __name__ == "__main__":
    main()
