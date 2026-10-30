# Redis Persistence

## Wiring

`GuardAgentHandler.initialize_redis(redis_handler)` stores the handler and calls `EventBuffer.initialize_redis`, which only attaches the handler: it loads nothing. Each buffer persists under its own instance id (`EventBuffer.instance_id`, logged at start), so a worker never loads what another worker still holds. The handler accepts any object implementing `RedisHandlerProtocol` (guard-core's `redis_handler` is the canonical implementation).

## Write Path

On `add_event` / `add_metric`, after appending to the in-memory deque, the buffer persists the item to Redis:

* Key: `event_<time_ns()>_<uuid4 hex[:8]>` (events) or `metric_<time_ns()>_<uuid4 hex[:8]>` (metrics), namespaced per buffer instance under `agent_events:<instance_id>` / `agent_metrics:<instance_id>`.
* Value: `safe_json_serialize(item.model_dump())`.
* TTL: `3600` seconds.
* The buffer records the short key on the item via `id(item)` for later confirmation.

A persist failure increments `redis_persist_failures` and logs a WARNING; the item still lives in the in-memory buffer (durability degraded, not lost).

## Load Path (Explicit Recovery Only)

The buffer never loads items another instance persisted. A dead instance's items stay in Redis under its own namespace until their 3600 s TTL expires, unless you recover them:

* `list_persisted_instances()` (on `EventBuffer`, `GuardAgentHandler` and `SyncGuardAgentHandler`) returns `{instance_id: {"events": n, "metrics": m}}` for every other instance on that Redis. It changes nothing. A live worker is listed too, so you decide which id is dead (the id is logged when each instance starts).
* `recover_orphaned_instance(instance_id)` moves that instance's events and metrics into this buffer: each is written under this instance, deleted from the dead one, appended to the in-memory deque and tracked for confirmation. It returns the adopted `(events, metrics)`; this instance's own id adopts nothing.

Listing goes through the handler's `keys()`, limited to one instance's namespace; guard-core's handler implements `keys()` with SCAN, and the agent never issues KEYS itself. Keys written by agent v3.2.4 and earlier (`agent_events:<key>`, no instance id) are not recovered; they expire after one hour.

## Delete Path (Success Only)

`flush_events_with_keys` / `flush_metrics_with_keys` pop the items and their keys out of the buffer and return both. The handler calls `transport.send_events` / `send_metrics`:

* `True` (accepted or permanently dropped) → `confirm_event_redis_keys` / `confirm_metric_redis_keys` deletes the Redis keys. The items are gone everywhere.
* `False` (transient failure) → `requeue_events_in_memory` / `requeue_metrics_in_memory` pushes the items back to the front of the deque and re-associates the Redis keys. The Redis keys are retained for the next retry or for explicit recovery.

Redis keys are never deleted on failure. The 3600s TTL is the only failure-case expiry, bounding how long an un-flushed item survives a downed SaaS.

## Clear

`clear_buffer()` wipes both in-memory deques and deletes the `agent_events:<instance_id>:*` / `agent_metrics:<instance_id>:*` keys of this instance only; another worker's persisted items are untouched. Use only when you intend to lose pending telemetry.
