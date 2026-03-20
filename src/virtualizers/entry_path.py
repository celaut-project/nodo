from typing import Iterable, List


def resolve_entrypoint_path(entry_path: Iterable[str]) -> str:
    raw_items = [str(item).strip() for item in entry_path]
    items = [item for item in raw_items if item]

    if not items:
        raise ValueError("container.init.entry_path is empty.")

    # Accept compact path form: ["/bin/service"] or ["service"].
    if len(items) == 1:
        candidate = items[0]
        if any(ch.isspace() for ch in candidate):
            raise ValueError(
                "container.init.entry_path must contain a single executable path without spaces."
            )
        return candidate if candidate.startswith("/") else f"/{candidate.lstrip('/')}"

    # Backward compatibility with segmented path form: ["bin", "service"].
    segments: List[str] = []
    for item in items:
        if any(ch.isspace() for ch in item):
            raise ValueError(
                "container.init.entry_path path segments must not contain spaces."
            )
        for part in item.split("/"):
            clean = part.strip()
            if not clean:
                continue
            if clean in {".", ".."}:
                raise ValueError(
                    "container.init.entry_path must not contain '.' or '..' segments."
                )
            if clean.startswith("-"):
                raise ValueError(
                    "container.init.entry_path must only define an executable path, not CLI arguments."
                )
            segments.append(clean)

    if not segments:
        raise ValueError("container.init.entry_path does not resolve to a valid executable path.")

    return "/" + "/".join(segments)
