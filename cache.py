import time

_response_cache: dict[str, dict] = {}
_CACHE_MAX_SIZE = 500


def _cache_evict():
    now = time.time()
    expired = [k for k, v in _response_cache.items() if v["expires"] <= now]
    for k in expired:
        del _response_cache[k]
    if len(_response_cache) > _CACHE_MAX_SIZE:
        sorted_keys = sorted(_response_cache, key=lambda k: _response_cache[k]["expires"])
        for k in sorted_keys[: len(_response_cache) - _CACHE_MAX_SIZE]:
            del _response_cache[k]


def cache_get(key: str):
    entry = _response_cache.get(key)
    if entry and entry["expires"] > time.time():
        return entry["data"]
    if entry:
        del _response_cache[key]
    return None


def cache_set(key: str, data, ttl: int = 60):
    if len(_response_cache) > _CACHE_MAX_SIZE:
        _cache_evict()
    _response_cache[key] = {"data": data, "expires": time.time() + ttl}


def cache_invalidate(key: str):
    _response_cache.pop(key, None)
