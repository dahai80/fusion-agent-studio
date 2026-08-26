import logging
import time

from agent_runtime.errors import ErrorCode, raise_api_error

logger = logging.getLogger(__name__)


def _mask_key(key: str) -> str:
    # 审计 P0-4: 日志不记全量 api_key (明文泄露). 短 key 全挡, 长 key 首尾留 4.
    if not key or len(key) <= 8:
        return "***"
    return key[:4] + "..." + key[-4:]


class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self.last_used = time.monotonic()
        logger.debug("TokenBucket created: rate=%.2f capacity=%.2f", rate, capacity)

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        added = elapsed * self.rate
        self.tokens = min(self.capacity, self.tokens + added)
        self.last_refill = now
        logger.debug("Refilled %.4f tokens, total=%.4f", added, self.tokens)

    def consume(self, n: float = 1) -> bool:
        self._refill()
        self.last_used = time.monotonic()
        if self.tokens >= n:
            self.tokens -= n
            logger.debug("Consumed %.2f tokens, remaining=%.4f", n, self.tokens)
            return True
        logger.debug("Rate limited: need %.2f, have %.4f", n, self.tokens)
        return False

    def get_wait_time(self) -> float:
        self._refill()
        if self.tokens >= 1:
            return 0.0
        deficit = 1.0 - self.tokens
        wait = deficit / self.rate if self.rate > 0 else 0.0
        logger.debug("Wait time for next token: %.4fs", wait)
        return wait


class RateLimiter:
    _CLEANUP_THRESHOLD = 3600  # 1 hour

    def __init__(self):
        self._key_buckets: dict[str, TokenBucket] = {}
        self._agent_buckets: dict[str, TokenBucket] = {}
        logger.info("RateLimiter initialized")

    def check_key(self, key_id: str, rate: float = 10, capacity: float = 20) -> bool:
        if key_id not in self._key_buckets:
            self._key_buckets[key_id] = TokenBucket(rate=rate, capacity=capacity)
            logger.info(
                "Created key bucket: key_id=%s rate=%.2f capacity=%.2f",
                key_id,
                rate,
                capacity,
            )
        bucket = self._key_buckets[key_id]
        return bucket.consume()

    def check_agent(self, agent_id: str, rate: float = 0, capacity: float = 0) -> bool:
        if rate <= 0 or capacity <= 0:
            logger.debug(
                "No rate limit for agent_id=%s (rate=%.2f capacity=%.2f)",
                agent_id,
                rate,
                capacity,
            )
            return True
        if agent_id not in self._agent_buckets:
            self._agent_buckets[agent_id] = TokenBucket(rate=rate, capacity=capacity)
            logger.info(
                "Created agent bucket: agent_id=%s rate=%.2f capacity=%.2f",
                agent_id,
                rate,
                capacity,
            )
        bucket = self._agent_buckets[agent_id]
        return bucket.consume()

    def cleanup_expired(self):
        now = time.monotonic()
        expired_keys = [
            k
            for k, b in self._key_buckets.items()
            if (now - b.last_used) > self._CLEANUP_THRESHOLD
        ]
        expired_agents = [
            k
            for k, b in self._agent_buckets.items()
            if (now - b.last_used) > self._CLEANUP_THRESHOLD
        ]
        for k in expired_keys:
            del self._key_buckets[k]
        for k in expired_agents:
            del self._agent_buckets[k]
        if expired_keys or expired_agents:
            logger.info(
                "Cleaned up expired buckets: keys=%d agents=%d",
                len(expired_keys),
                len(expired_agents),
            )


class RateLimitMiddleware:
    def __init__(self, app, limiter: RateLimiter | None = None):
        self.app = app
        self.limiter = limiter or RateLimiter()
        logger.info("RateLimitMiddleware mounted")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope, receive)

        api_key = request.headers.get("x-api-key", "")
        path_parts = scope.get("path", "").split("/")
        agent_id = ""
        for i, part in enumerate(path_parts):
            if part == "agents" and i + 1 < len(path_parts):
                agent_id = path_parts[i + 1]
                break

        logger.debug(
            "RateLimitMiddleware: api_key=%s agent_id=%s path=%s",
            _mask_key(api_key),
            agent_id,
            scope.get("path"),
        )

        if api_key and not self.limiter.check_key(api_key):
            wait = 0.0
            bucket = self.limiter._key_buckets.get(api_key)
            if bucket:
                wait = bucket.get_wait_time()
            logger.warning("Key rate limited: api_key=%s wait=%.2fs", _mask_key(api_key), wait)
            response = JSONResponse(
                status_code=429,
                content=raise_api_error(
                    ErrorCode.RATE_LIMIT_REACHED,
                    f"Rate limit exceeded for API key, retry after {wait:.1f}s",
                ).body,
            )
            await response(scope, receive, send)
            return

        if agent_id and not self.limiter.check_agent(agent_id):
            wait = 0.0
            bucket = self.limiter._agent_buckets.get(agent_id)
            if bucket:
                wait = bucket.get_wait_time()
            logger.warning("Agent rate limited: agent_id=%s wait=%.2fs", agent_id, wait)
            response = JSONResponse(
                status_code=429,
                content=raise_api_error(
                    ErrorCode.RATE_LIMIT_REACHED,
                    f"Rate limit exceeded for agent, retry after {wait:.1f}s",
                ).body,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
