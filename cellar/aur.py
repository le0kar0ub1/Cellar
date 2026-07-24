"""Minimal AUR RPC v5 client (stdlib only).

Failure of any request raises AurError. Callers must treat that as
"hold everything": never fall back to allowing an upgrade because the
AUR could not be consulted.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

RPC_URL = "https://aur.archlinux.org/rpc/v5/info"
AUR_PACKAGE_URL = "https://aur.archlinux.org/packages/{name}"

# Keep request lines comfortably under aurweb's limit; batching also keeps
# the number of round-trips low (one request covers hundreds of packages).
_MAX_URL_LENGTH = 4400
_TIMEOUT_SECONDS = 30


class AurError(Exception):
    """The AUR could not be queried or returned an error."""


def fetch_info(names: list[str]) -> dict[str, dict]:
    """Return AUR metadata for every name found, keyed by package name.

    Names unknown to the AUR are simply absent from the result (locally
    built packages filter out here). Raises AurError on any failure.
    """
    results: dict[str, dict] = {}
    for batch in _batches(names):
        for pkg in _fetch_batch(batch):
            results[pkg["Name"]] = pkg
    return results


def _quoted_arg(name: str) -> str:
    return "arg%5B%5D=" + urllib.parse.quote(name, safe="")


def _batches(names: list[str]):
    """Split names into batches whose GET URL stays under _MAX_URL_LENGTH."""
    batch: list[str] = []
    length = len(RPC_URL) + 1  # trailing '?'
    for name in names:
        arg_length = len(_quoted_arg(name)) + 1  # joining '&'
        if batch and length + arg_length > _MAX_URL_LENGTH:
            yield batch
            batch = []
            length = len(RPC_URL) + 1
        batch.append(name)
        length += arg_length
    if batch:
        yield batch


def _fetch_batch(batch: list[str]) -> list[dict]:
    url = RPC_URL + "?" + "&".join(_quoted_arg(name) for name in batch)
    request = urllib.request.Request(
        url, headers={"User-Agent": f"cellar/{__version__}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise AurError(f"AUR RPC request failed: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("type") == "error":
        error = payload.get("error", "malformed response") if isinstance(payload, dict) else "malformed response"
        raise AurError(f"AUR RPC error: {error}")
    results = payload.get("results")
    if not isinstance(results, list):
        raise AurError("AUR RPC error: malformed response (no results array)")
    return results
