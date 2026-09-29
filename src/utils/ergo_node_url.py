"""The one check every reader of ``ledgers.ergo.NODE_URL`` makes before using it.

An empty value is not a network error and must not look like one: joined onto
``/info`` it became the relative URL ``/info``, and ``requests`` reported
``Invalid URL '/info': No scheme supplied`` from deep inside a balance check that
then answered "insufficient balance" (#441). Naming the key is the whole point.
"""
from typing import Any
from urllib.parse import urlparse

NODE_URL_KEY = "ledgers.ergo.NODE_URL"


class ErgoNodeUrlNotConfigured(ValueError):
    """``ledgers.ergo.NODE_URL`` is empty or is not an absolute http(s) URL."""


def ergo_node_url_problem(value: Any) -> str:
    """Why ``value`` cannot be used as the Ergo node URL, or ``""`` when it can."""
    url = str(value or "").strip()
    if not url:
        return (
            f"{NODE_URL_KEY} is not set; point it at an Ergo node's REST API "
            "(e.g. https://node.sigmaspace.io)"
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"{NODE_URL_KEY} is {url!r}, which is not an http:// or https:// URL"
    return ""


def is_valid_ergo_node_url(value: Any) -> bool:
    return not ergo_node_url_problem(value)


def require_ergo_node_url(value: Any) -> str:
    """``value`` stripped of whitespace and trailing slashes, or a clear config error."""
    problem = ergo_node_url_problem(value)
    if problem:
        raise ErgoNodeUrlNotConfigured(problem)
    return str(value).strip().rstrip("/")
