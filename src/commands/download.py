from typing import Optional

from src.commands.publisher import download_from_manifest_url
from src.commands.publisher.publisher import PublisherError


def download_command(url: str, output_dir: Optional[str] = None):
    try:
        download_from_manifest_url(manifest_url=url, output_dir=output_dir)
    except PublisherError as exc:
        print(f"Download error: {exc}", flush=True)
