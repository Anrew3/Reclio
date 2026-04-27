"""Recombee integration — item catalog, user interactions, recommendations.

Runs Recombee's synchronous Python SDK calls inside a thread executor so
they don't block the async event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def _safe_import():
    """Import the Recombee SDK with version tolerance.

    The exception class names changed between 4.x and 5.x:
      4.x exposed ApiException + ResponseException
      5.x renamed/dropped ApiException; ResponseException stayed
    Pinning a single version isn't enough — pip can resolve to a 5.x in
    fresh environments. Splitting the import lets the core SDK load even
    when only one of the exception classes exists; missing classes fall
    back to plain Exception (the catch-all already coerces both anyway).
    """
    # Step 1: hard requirement — the client + request modules
    try:
        from recombee_api_client.api_client import RecombeeClient
        from recombee_api_client import api_requests as rq
    except Exception as exc:  # noqa: BLE001
        logger.warning("Recombee SDK core unavailable: %s", exc)
        return None, None, None, Exception, Exception

    # Step 2: best-effort exception classes — fall back to Exception when
    # the SDK rev we got doesn't expose the exact name we want.
    try:
        from recombee_api_client.exceptions import ApiException
    except ImportError:
        try:
            # 5.x typically still exposes ApiTimeoutException; close enough
            # semantically to keep distinct error logging.
            from recombee_api_client.exceptions import ApiTimeoutException as ApiException
        except ImportError:
            ApiException = Exception
    try:
        from recombee_api_client.exceptions import ResponseException
    except ImportError:
        ResponseException = Exception

    # Step 3: optional Region enum — older SDKs don't expose it
    try:
        from recombee_api_client.api_client import Region
    except ImportError:
        Region = None

    return RecombeeClient, Region, rq, ApiException, ResponseException


def _resolve_region(Region, name: str):
    """Map a config string like 'us-west' to a Region enum member.

    Returns None when the SDK doesn't expose a Region enum at all (5.x
    in some packagings) or when the requested member doesn't exist.
    The caller MUST also try the string form below — passing region=None
    silently uses the SDK's default region, which broke prod.
    """
    if Region is None or not name:
        return None
    key = name.strip().upper().replace("-", "_")
    member = getattr(Region, key, None)
    if member is not None:
        return member
    aliases = {"US": "US_WEST", "EU": "EU_WEST", "AP": "AP_SE"}
    for alias, real in aliases.items():
        if key.startswith(alias):
            return getattr(Region, real, None)
    return None


def _make_recombee_client(client_cls, db_id: str, token: str,
                          region_str: str, region_enum: Any | None) -> Any | None:
    """Construct a RecombeeClient that survives SDK 4.x and 5.x signature drift.

    Tries, in order:
      1. region=Region.US_WEST  (4.x + 5.x with Region enum)
      2. region="us-west"       (5.x string form)
      3. options={"region": "us-west"}  (alternative dict form some SDKs use)
      4. (no region kwarg)      (SDK default — only as last resort)

    Most-specific first so we never silently fall through to the default
    region the way 1.6.x did. The `wrong_region` verdict in the v1.5
    health check was telling us this exact thing — items going to the
    wrong DB because Region enum lookup returned None and we then
    instantiated without any region arg at all.
    """
    last_exc: Exception | None = None

    if region_enum is not None:
        try:
            return client_cls(db_id, token, region=region_enum)
        except TypeError as exc:
            # 5.x can drop the `region` kwarg — fall through to string form
            last_exc = exc
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

    if region_str:
        try:
            return client_cls(db_id, token, region=region_str)
        except TypeError as exc:
            last_exc = exc
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

        try:
            return client_cls(db_id, token, options={"region": region_str})
        except TypeError as exc:
            last_exc = exc
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

    # Last resort — SDK default region. Logs a WARNING because this is
    # almost certainly wrong for any non-default user.
    try:
        client = client_cls(db_id, token)
        logger.warning(
            "Recombee client created without region argument — every SDK "
            "constructor signature was rejected. Items will go to the SDK's "
            "default region (likely AP_SE) instead of '%s'. "
            "This is the cause of the v1.5 health check's `wrong_region` "
            "verdict — please report which recombee-api-client version "
            "you have installed so we can wire its signature correctly.",
            region_str,
        )
        return client
    except Exception as exc:  # noqa: BLE001
        last_exc = exc

    if last_exc is not None:
        logger.warning("Recombee client init failed: %s", last_exc)
    return None


class RecombeeService:
    def __init__(
        self,
        database_id: str | None = None,
        private_token: str | None = None,
    ) -> None:
        settings = get_settings()
        self.database_id = database_id or settings.recombee_database_id
        self.private_token = private_token or settings.recombee_private_token

        RecombeeClient, Region, rq, ApiException, ResponseException = _safe_import()
        self._rq = rq
        self._exc_api = ApiException
        self._exc_resp = ResponseException

        self._client = None
        if RecombeeClient and self.database_id and self.private_token:
            region_enum = _resolve_region(Region, settings.recombee_region)
            self._client = _make_recombee_client(
                RecombeeClient,
                self.database_id,
                self.private_token,
                region_str=settings.recombee_region,
                region_enum=region_enum,
            )

        self._properties_initialized = False

    @property
    def available(self) -> bool:
        return self._client is not None

    async def _run(self, request: Any) -> Any:
        if not self._client:
            return None
        try:
            return await asyncio.to_thread(self._client.send, request)
        except (self._exc_resp, self._exc_api) as exc:  # type: ignore[misc]
            logger.debug("Recombee request failed: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee unexpected error: %s", exc)
            return None

    async def initialize_schema(self) -> None:
        """Ensure item properties exist. Safe to call every startup."""
        if not self._client or self._properties_initialized:
            return
        rq = self._rq
        # Item-level properties
        properties = [
            ("title", "string"),
            ("overview", "string"),
            ("genres", "set"),
            ("year", "int"),
            ("vote_average", "double"),
            ("popularity", "double"),
            ("media_type", "string"),
            ("cast", "set"),
            ("director", "string"),
        ]
        for name, ptype in properties:
            try:
                await asyncio.to_thread(
                    self._client.send, rq.AddItemProperty(name, ptype)
                )
            except self._exc_resp as exc:  # type: ignore[misc]
                # Property may already exist — ignore
                if "already exists" not in str(exc).lower():
                    logger.debug("Recombee AddItemProperty %s: %s", name, exc)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Recombee property init %s failed: %s", name, exc)
        self._properties_initialized = True

    # --- Items ---------------------------------------------------

    async def add_item(self, item_id: str, properties: dict[str, Any]) -> None:
        if not self._client:
            return
        rq = self._rq
        # Upsert item then set values
        try:
            await asyncio.to_thread(self._client.send, rq.AddItem(item_id))
        except self._exc_resp:  # type: ignore[misc]
            pass  # item already exists
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee AddItem %s failed: %s", item_id, exc)

        clean_props = {k: v for k, v in properties.items() if v is not None}
        if not clean_props:
            return
        try:
            await asyncio.to_thread(
                self._client.send,
                rq.SetItemValues(item_id, clean_props, cascade_create=True),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee SetItemValues %s failed: %s", item_id, exc)

    async def set_item_values(self, item_id: str, properties: dict[str, Any]) -> None:
        await self.add_item(item_id, properties)

    async def upsert_items_batch(
        self,
        items: list[tuple[str, dict[str, Any]]],
        chunk_size: int = 500,
    ) -> dict[str, Any]:
        """Bulk upsert items in batched Recombee requests.

        Args:
            items: list of (item_id, properties) tuples
            chunk_size: number of ops per batch (Recombee supports up to ~10k,
                        but 500 keeps response payloads sane and errors isolated)

        Returns stats {sent, succeeded, failed, failed_ids: set[str]}.
        Callers can use `failed_ids` to track per-item failures and avoid
        marking partially-failed catalog rows as synced.
        """
        stats: dict[str, Any] = {
            "sent": 0, "succeeded": 0, "failed": 0,
            "failed_ids": set(),
        }
        if not self._client or not items:
            return stats
        rq = self._rq

        # Build requests AND a parallel list of item_ids so we can attribute
        # per-request results back to the originating item.
        requests = []
        request_ids: list[str] = []
        for item_id, props in items:
            clean = {k: v for k, v in props.items() if v is not None}
            # SetItemValues with cascade_create=True creates the item if missing
            # AND sets values in one call — no need for separate AddItem.
            if clean:
                requests.append(rq.SetItemValues(item_id, clean, cascade_create=True))
                request_ids.append(item_id)

        for start in range(0, len(requests), chunk_size):
            chunk = requests[start : start + chunk_size]
            chunk_ids = request_ids[start : start + chunk_size]
            stats["sent"] += len(chunk)
            try:
                result = await asyncio.to_thread(
                    self._client.send, rq.Batch(chunk)
                )
                # Batch response is a list of per-request results with a "code"
                if isinstance(result, list):
                    for idx, entry in enumerate(result):
                        code = entry.get("code") if isinstance(entry, dict) else 200
                        if 200 <= int(code or 500) < 300:
                            stats["succeeded"] += 1
                        else:
                            stats["failed"] += 1
                            if idx < len(chunk_ids):
                                stats["failed_ids"].add(chunk_ids[idx])
                else:
                    stats["succeeded"] += len(chunk)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Recombee batch (items) chunk of %d failed: %s", len(chunk), exc
                )
                stats["failed"] += len(chunk)
                stats["failed_ids"].update(chunk_ids)

        return stats

    async def push_interactions_batch(
        self,
        interactions: list[dict[str, Any]],
        chunk_size: int = 500,
    ) -> dict[str, int]:
        """Bulk push user interactions.

        Each interaction is a dict:
            {"kind": "view"|"rating"|"bookmark",
             "user_id": str, "item_id": str,
             "rating": float (for rating), "timestamp": datetime|None}
        """
        stats = {"sent": 0, "succeeded": 0, "failed": 0}
        if not self._client or not interactions:
            return stats
        rq = self._rq

        requests = []
        for it in interactions:
            kind = it.get("kind")
            user_id = it.get("user_id")
            item_id = it.get("item_id")
            if not (kind and user_id and item_id):
                continue
            ts = it.get("timestamp")
            ts_value = ts.timestamp() if isinstance(ts, datetime) else ts

            if kind == "view":
                requests.append(
                    rq.AddDetailView(user_id, item_id, timestamp=ts_value, cascade_create=True)
                )
            elif kind == "rating":
                rating = max(-1.0, min(1.0, float(it.get("rating") or 0.0)))
                requests.append(
                    rq.AddRating(user_id, item_id, rating, timestamp=ts_value, cascade_create=True)
                )
            elif kind == "bookmark":
                requests.append(
                    rq.AddBookmark(user_id, item_id, timestamp=ts_value, cascade_create=True)
                )

        for start in range(0, len(requests), chunk_size):
            chunk = requests[start : start + chunk_size]
            stats["sent"] += len(chunk)
            try:
                result = await asyncio.to_thread(
                    self._client.send, rq.Batch(chunk)
                )
                if isinstance(result, list):
                    for entry in result:
                        code = entry.get("code") if isinstance(entry, dict) else 200
                        if 200 <= int(code or 500) < 300:
                            stats["succeeded"] += 1
                        else:
                            stats["failed"] += 1
                else:
                    stats["succeeded"] += len(chunk)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Recombee batch (interactions) chunk of %d failed: %s",
                    len(chunk), exc,
                )
                stats["failed"] += len(chunk)

        return stats

    # --- Users & interactions -----------------------------------

    async def add_user(self, user_id: str) -> None:
        if not self._client:
            return
        rq = self._rq
        try:
            await asyncio.to_thread(self._client.send, rq.AddUser(user_id))
        except self._exc_resp:  # type: ignore[misc]
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee AddUser %s failed: %s", user_id, exc)

    async def add_detail_view(
        self, user_id: str, item_id: str, timestamp: datetime | None = None
    ) -> None:
        if not self._client:
            return
        rq = self._rq
        ts = timestamp.timestamp() if timestamp else None
        try:
            await asyncio.to_thread(
                self._client.send,
                rq.AddDetailView(user_id, item_id, timestamp=ts, cascade_create=True),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee AddDetailView failed: %s", exc)

    async def add_rating(
        self,
        user_id: str,
        item_id: str,
        rating: float,
        timestamp: datetime | None = None,
    ) -> None:
        """rating is already normalized to [-1.0, 1.0] per Recombee spec."""
        if not self._client:
            return
        rq = self._rq
        ts = timestamp.timestamp() if timestamp else None
        try:
            await asyncio.to_thread(
                self._client.send,
                rq.AddRating(
                    user_id, item_id, max(-1.0, min(1.0, rating)),
                    timestamp=ts, cascade_create=True,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee AddRating failed: %s", exc)

    async def add_bookmark(self, user_id: str, item_id: str) -> None:
        if not self._client:
            return
        rq = self._rq
        try:
            await asyncio.to_thread(
                self._client.send,
                rq.AddBookmark(user_id, item_id, cascade_create=True),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee AddBookmark failed: %s", exc)

    # --- Recommendations ----------------------------------------

    async def get_recommendations(
        self,
        user_id: str,
        count: int = 50,
        filter_media_type: str | None = None,
    ) -> list[str]:
        if not self._client:
            return []
        rq = self._rq
        kwargs: dict[str, Any] = {"cascade_create": True}
        if filter_media_type:
            kwargs["filter"] = f"'media_type' == \"{filter_media_type}\""
        try:
            result = await asyncio.to_thread(
                self._client.send,
                rq.RecommendItemsToUser(user_id, count, **kwargs),
            )
            recoms = (result or {}).get("recomms", []) if isinstance(result, dict) else []
            return [r.get("id") for r in recoms if r.get("id")]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee RecommendItemsToUser failed: %s", exc)
            return []

    async def add_negative_interaction(self, user_id: str, item_id: str) -> None:
        """Record a "do not show me this" signal as a low rating.

        Recombee doesn't have a native "negative interaction" verb; the
        canonical pattern is to send AddRating with the lowest possible
        score, which both:
          - removes the item from future RecommendItemsToUser results for
            this user (interactions filter is automatic)
          - down-weights similar items in the collaborative model
        Failures are swallowed so chat-driven blocks never break the UX.
        """
        if not self._client:
            return
        rq = self._rq
        try:
            await asyncio.to_thread(
                self._client.send,
                rq.AddRating(user_id, item_id, -1.0, cascade_create=True),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee AddRating(-1) failed: %s", exc)

    async def list_items_count(self, max_count: int = 5) -> tuple[int | None, list[str]]:
        """Probe ListItems for diagnostics.

        Returns (count_or_None, sample_ids). count is None when the SDK
        request failed (Recombee unreachable, bad creds, wrong region).
        Use as a one-call "is Recombee actually receiving our writes?" probe.
        """
        if not self._client:
            return None, []
        rq = self._rq
        try:
            result = await asyncio.to_thread(
                self._client.send,
                rq.ListItems(count=max_count, return_properties=False),
            )
            if isinstance(result, list):
                return len(result), [str(x) for x in result[:max_count]]
            if isinstance(result, dict):
                items = result.get("items") or result.get("ids") or []
                return len(items), [str(i.get("itemId") if isinstance(i, dict) else i)
                                    for i in items[:max_count]]
            return 0, []
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee ListItems failed: %s", exc)
            return None, []

    async def get_item(self, item_id: str) -> dict | None:
        """Fetch a specific item's properties — used as a write-verify probe."""
        if not self._client:
            return None
        rq = self._rq
        try:
            return await asyncio.to_thread(
                self._client.send,
                rq.GetItemValues(item_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee GetItemValues(%s) failed: %s", item_id, exc)
            return None

    async def write_test_item(self) -> tuple[bool, str | None]:
        """Try to write a tiny test item, then read it back.

        Returns (success, error_message). Use to confirm the
        write+read path works against the configured database.
        """
        if not self._client:
            return False, "Recombee client unavailable (DB ID / token / SDK missing)"
        rq = self._rq
        test_id = "reclio_health_probe"
        try:
            await asyncio.to_thread(
                self._client.send,
                rq.SetItemValues(
                    test_id,
                    {"title": "Reclio Health Probe", "media_type": "movie"},
                    cascade_create=True,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"write failed: {exc}"
        try:
            result = await asyncio.to_thread(
                self._client.send, rq.GetItemValues(test_id),
            )
            if not result:
                return False, "wrote item but read-back returned empty"
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, f"read-back failed: {exc}"

    def config_dump(self) -> dict[str, Any]:
        """Non-sensitive config snapshot for diagnostics. Token NEVER included."""
        settings = get_settings()
        return {
            "available": self.available,
            "database_id": self.database_id,
            "region_setting": settings.recombee_region,
            "token_present": bool(self.private_token),
            "token_length": len(self.private_token) if self.private_token else 0,
            "sdk_loaded": self._rq is not None,
        }

    async def recommend_items_to_item(
        self,
        item_id: str,
        target_user_id: str,
        count: int = 25,
        filter_media_type: str | None = None,
    ) -> list[str]:
        """Items similar to `item_id` for `target_user_id`.

        Recombee filters out items the target user has already interacted
        with (watched / rated / bookmarked) when target_user_id is set, so
        the result is naturally watched-exclusive — exactly what BYW rows
        need. Returns [] if the request fails or Recombee is disabled.
        """
        if not self._client:
            return []
        rq = self._rq
        kwargs: dict[str, Any] = {
            "cascade_create": True,
            "target_user_id": target_user_id,
        }
        if filter_media_type:
            kwargs["filter"] = f"'media_type' == \"{filter_media_type}\""
        try:
            result = await asyncio.to_thread(
                self._client.send,
                rq.RecommendItemsToItem(item_id, target_user_id, count, **kwargs),
            )
            recoms = (result or {}).get("recomms", []) if isinstance(result, dict) else []
            return [r.get("id") for r in recoms if r.get("id")]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recombee RecommendItemsToItem failed: %s", exc)
            return []


_recombee: RecombeeService | None = None


def get_recombee() -> RecombeeService:
    global _recombee
    if _recombee is None:
        _recombee = RecombeeService()
    return _recombee
