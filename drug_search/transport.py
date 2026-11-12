import logging
from collections.abc import Callable

import requests

from .models import Candidates, State


logger = logging.getLogger(__name__)
MAX_DOWNLOAD = 16 * 1024 * 1024


class SourceError(Exception):
    pass


def fetch(url: str, params: dict | None = None, *, empty_404: bool = False) -> bytes:
    try:
        with requests.get(
            url, params=params, timeout=(4, 10), stream=True, allow_redirects=False,
            headers={"User-Agent": "DrugContraindicationsStudy/1.0"},
        ) as response:
            if response.status_code == 404 and empty_404:
                return b'{"results": []}'
            if response.status_code != 200:
                raise SourceError(f"HTTP {response.status_code}")
            content = bytearray()
            for chunk in response.iter_content(64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_DOWNLOAD:
                    raise SourceError("Response exceeds the download limit")
            return bytes(content)
    except requests.RequestException as exc:
        raise SourceError(type(exc).__name__) from exc


def source_search[T](operation: Callable[[], Candidates[T]]) -> Candidates[T]:
    try:
        return operation()
    except (SourceError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.warning("Search failed in %s (%s)", operation.__module__, type(exc).__name__)
        return Candidates(State.UNAVAILABLE)
