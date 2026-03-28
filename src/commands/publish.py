from typing import Optional

from src.publisher import publish_service
from src.publisher.publisher import PublisherError


def publish_command(
    service_ref: str,
    upload_id: Optional[str] = None,
    chunk_size_mb: Optional[int] = None,
):
    try:
        publish_service(service_ref=service_ref, upload_id=upload_id, chunk_size_mb=chunk_size_mb)
    except PublisherError as exc:
        print(f"Publish error: {exc}", flush=True)
