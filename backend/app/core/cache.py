import redis
from typing import Optional
import os
import json

class RedisClient:
    _instance: Optional[redis.Redis] = None

    @classmethod
    def get_client(cls) -> redis.Redis:
        if cls._instance is None:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            cls._instance = redis.from_url(redis_url, decode_responses=True)
        return cls._instance

    @classmethod
    def close(cls):
        if cls._instance:
            cls._instance.close()
            cls._instance = None


def get_redis() -> redis.Redis:
    return RedisClient.get_client()


def cache_set(key: str, value: any, expire: int = 3600):
    client = get_redis()
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    client.setex(key, expire, value)


def cache_get(key: str) -> Optional[any]:
    client = get_redis()
    value = client.get(key)
    if value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return None


def cache_delete(key: str):
    client = get_redis()
    client.delete(key)


def cache_clear_pattern(pattern: str):
    client = get_redis()
    for key in client.scan_iter(match=pattern):
        client.delete(key)
