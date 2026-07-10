from src.commands.publisher import publish_service
from src.commands.publisher.publisher import PublisherError


def publish_command(
    service_ref: str
):
    try:
        publish_service(service_ref=service_ref)
    except PublisherError as exc:
        print(f"Publish error: {exc}", flush=True)
