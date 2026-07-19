"""Durable, task-scoped controller leases for automatic orchestration."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .contracts import AuditEventType, utc_now
from .ledger import EvidenceLedger
from .task_lifecycle import TaskLifecycleError


MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTROLLER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_EVENT_FIELDS = {
    "schema_version",
    "task_id",
    "action",
    "controller_id",
    "lease_id",
    "idempotency_key",
    "request_sha256",
    "request",
    "response",
}
_LEASE_FIELDS = {
    "task_id",
    "controller_id",
    "lease_id",
    "acquired_at",
    "expires_at",
    "status",
}


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise TaskLifecycleError(
            "invalid_controller_lease", "controller lease timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise TaskLifecycleError(
            "invalid_controller_lease", "controller lease timestamp lacks a timezone"
        )
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ControllerLease:
    task_id: str
    controller_id: str
    lease_id: str
    acquired_at: str
    expires_at: str
    status: str = "active"

    def to_dict(self, *, now: datetime | None = None) -> dict[str, str]:
        status = self.status
        if status == "active" and _parse_time(self.expires_at) <= (
            now or datetime.now(timezone.utc)
        ):
            status = "expired"
        return {
            "task_id": self.task_id,
            "controller_id": self.controller_id,
            "lease_id": self.lease_id,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "status": status,
        }


class ControllerLeaseStore:
    """Project one active controller from append-only ledger events."""

    def __init__(self, ledger: EvidenceLedger):
        self.ledger = ledger

    @staticmethod
    def _stream_id(task_id: str) -> str:
        return f"controller:{task_id}"

    def _events(self, task_id: str):
        return [
            event
            for event in self.ledger.events_for_contract(
                self._stream_id(task_id), all_segments=True
            )
            if event.event_type == AuditEventType.TASK_CONTROLLER_LEASE.value
        ]

    def _replay(
        self, task_id: str
    ) -> tuple[
        ControllerLease | None,
        dict[str, tuple[str, dict[str, Any], str]],
        bool,
    ]:
        current: ControllerLease | None = None
        idempotency: dict[str, tuple[str, dict[str, Any], str]] = {}
        events = self._events(task_id)
        for event in events:
            payload = event.payload
            if (
                set(payload) != _EVENT_FIELDS
                or payload.get("schema_version") != 1
                or payload.get("task_id") != task_id
            ):
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease envelope is invalid"
                )
            action = payload.get("action")
            controller_id = payload.get("controller_id")
            lease_id = payload.get("lease_id")
            key = payload.get("idempotency_key")
            request_hash = payload.get("request_sha256")
            request = payload.get("request")
            response = payload.get("response")
            if (
                action not in {"acquire", "renew", "release"}
                or not isinstance(controller_id, str)
                or not _CONTROLLER_ID.fullmatch(controller_id)
                or not isinstance(lease_id, str)
                or not isinstance(key, str)
                or not _IDEMPOTENCY_KEY.fullmatch(key)
                or not isinstance(request_hash, str)
                or not _HASH.fullmatch(request_hash)
                or not isinstance(request, dict)
                or not isinstance(response, dict)
            ):
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease event is invalid"
                )
            try:
                UUID(lease_id)
            except (ValueError, AttributeError) as exc:
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease_id is invalid"
                ) from exc
            expected_request = {
                "task_id": task_id,
                "controller_id": controller_id,
                "action": action,
                "ttl_seconds": request.get("ttl_seconds"),
                "lease_id": request.get("lease_id"),
            }
            ttl = request.get("ttl_seconds")
            valid_ttl = (
                action in {"acquire", "renew"}
                and not isinstance(ttl, bool)
                and isinstance(ttl, int)
                and MIN_LEASE_SECONDS <= ttl <= MAX_LEASE_SECONDS
            ) or (action == "release" and ttl is None)
            if (
                request != expected_request
                or _digest(request) != request_hash
                or not valid_ttl
            ):
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease request digest is invalid"
                )
            previous = idempotency.get(key)
            if previous is not None:
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease idempotency history conflicts"
                )
            idempotency[key] = (request_hash, response, event.entry_hash)
            lease = response.get("lease")
            if (
                set(response) != {"lease"}
                or not isinstance(lease, dict)
                or set(lease) != _LEASE_FIELDS
                or lease.get("task_id") != task_id
                or lease.get("controller_id") != controller_id
                or lease.get("lease_id") != lease_id
                or lease.get("status")
                != ("released" if action == "release" else "active")
            ):
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease response is invalid"
                )
            try:
                candidate = ControllerLease(**lease)
                acquired = _parse_time(candidate.acquired_at)
                expires = _parse_time(candidate.expires_at)
                event_time = _parse_time(event.timestamp)
            except (TypeError, TaskLifecycleError) as exc:
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease response fields are invalid"
                ) from exc
            if acquired > expires or acquired > event_time:
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease time order is invalid"
                )
            prior = current.to_dict(now=event_time) if current is not None else None
            if action == "acquire":
                expected_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"causality:{task_id}:{controller_id}:{key}",
                    )
                )
                if (
                    request.get("lease_id") is not None
                    or lease_id != expected_id
                    or prior
                    and prior["status"] == "active"
                    or abs((event_time - acquired).total_seconds()) > 2
                ):
                    raise TaskLifecycleError(
                        "invalid_controller_lease", "controller acquire transition is invalid"
                    )
            elif (
                request.get("lease_id") != lease_id
                or prior is None
                or prior["status"] != "active"
                or prior["controller_id"] != controller_id
                or prior["lease_id"] != lease_id
            ):
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease transition is invalid"
                )
            if action in {"renew", "release"} and current is not None:
                if candidate.acquired_at != current.acquired_at:
                    raise TaskLifecycleError(
                        "invalid_controller_lease",
                        "controller lease acquisition time changed",
                    )
            expected_expiry = event_time if action == "release" else event_time + timedelta(
                seconds=ttl
            )
            if abs((expires - expected_expiry).total_seconds()) > 2:
                raise TaskLifecycleError(
                    "invalid_controller_lease", "controller lease expiry is invalid"
                )
            current = candidate
        return current, idempotency, bool(events)

    def state(self, task_id: str) -> dict[str, str] | None:
        lease, _, managed = self._replay(task_id)
        if not managed or lease is None:
            return None
        return lease.to_dict()

    def mutate(
        self,
        task_id: str,
        *,
        controller_id: str,
        action: str,
        idempotency_key: str,
        ttl_seconds: int | None = None,
        lease_id: str | None = None,
    ) -> tuple[dict[str, str], bool, str]:
        if not isinstance(controller_id, str) or not _CONTROLLER_ID.fullmatch(
            controller_id
        ):
            raise TaskLifecycleError(
                "validation_error", "controller_id has an invalid format"
            )
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
            idempotency_key
        ):
            raise TaskLifecycleError(
                "validation_error", "idempotency_key has an invalid format"
            )
        if action not in {"acquire", "renew", "release"}:
            raise TaskLifecycleError(
                "validation_error", "lease action must be acquire, renew, or release"
            )
        if action in {"acquire", "renew"} and (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not MIN_LEASE_SECONDS <= ttl_seconds <= MAX_LEASE_SECONDS
        ):
            raise TaskLifecycleError(
                "validation_error",
                f"ttl_seconds must be {MIN_LEASE_SECONDS}..{MAX_LEASE_SECONDS}",
            )
        if action == "release":
            ttl_seconds = None
        request = {
            "task_id": task_id,
            "controller_id": controller_id,
            "action": action,
            "ttl_seconds": ttl_seconds,
            "lease_id": lease_id,
        }
        request_hash = _digest(request)
        current, idempotency, managed = self._replay(task_id)
        replay = idempotency.get(idempotency_key)
        if replay is not None:
            if replay[0] != request_hash:
                raise TaskLifecycleError(
                    "idempotency_conflict", "lease key was used with another request"
                )
            recorded = dict(replay[1]["lease"])
            current_state = current.to_dict() if current is not None else None
            if action in {"acquire", "renew"} and current_state != recorded:
                raise TaskLifecycleError(
                    "controller_lease_stale",
                    "the replayed lease is no longer the current task lease",
                )
            return recorded, True, replay[2]

        now = datetime.now(timezone.utc)
        current_state = current.to_dict(now=now) if current is not None else None
        active = current_state is not None and current_state["status"] == "active"
        if action == "acquire":
            if active:
                raise TaskLifecycleError(
                    "controller_lease_conflict",
                    "task already has an active controller",
                    details={"controller_id": current_state["controller_id"]},
                )
            issued_lease_id = str(
                uuid5(NAMESPACE_URL, f"causality:{task_id}:{controller_id}:{idempotency_key}")
            )
            acquired_at = utc_now()
        else:
            if not managed or current_state is None or current_state["status"] != "active":
                code = (
                    "controller_lease_expired"
                    if current_state and current_state["status"] == "expired"
                    else "controller_lease_required"
                )
                raise TaskLifecycleError(code, "task has no active controller lease")
            if (
                current_state["controller_id"] != controller_id
                or current_state["lease_id"] != lease_id
            ):
                raise TaskLifecycleError(
                    "controller_lease_conflict", "controller lease identity does not match"
                )
            issued_lease_id = current_state["lease_id"]
            acquired_at = current_state["acquired_at"]

        status = "released" if action == "release" else "active"
        expires_at = (
            now.isoformat()
            if action == "release"
            else (now + timedelta(seconds=ttl_seconds or 0)).isoformat()
        )
        lease = ControllerLease(
            task_id=task_id,
            controller_id=controller_id,
            lease_id=issued_lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
            status=status,
        )
        response = {"lease": lease.to_dict(now=now)}
        event = self.ledger.append(
            AuditEventType.TASK_CONTROLLER_LEASE,
            {
                "schema_version": 1,
                "task_id": task_id,
                "action": action,
                "controller_id": controller_id,
                "lease_id": issued_lease_id,
                "idempotency_key": idempotency_key,
                "request_sha256": request_hash,
                "request": request,
                "response": response,
            },
            contract_id=self._stream_id(task_id),
        )
        result = lease.to_dict(now=now)
        return result, False, event.entry_hash

    def assert_mutation(
        self,
        task_id: str,
        *,
        controller_id: str | None,
        lease_id: str | None,
    ) -> None:
        current, _, managed = self._replay(task_id)
        if not managed:
            if controller_id is not None or lease_id is not None:
                raise TaskLifecycleError(
                    "controller_lease_required", "task has not been claimed"
                )
            return
        state = current.to_dict() if current is not None else None
        if state is None or state["status"] != "active":
            code = (
                "controller_lease_expired"
                if state and state["status"] == "expired"
                else "controller_lease_required"
            )
            raise TaskLifecycleError(code, "task requires an active controller lease")
        if controller_id is None or lease_id is None:
            raise TaskLifecycleError(
                "controller_lease_required", "controller_id and lease_id are required"
            )
        if state["controller_id"] != controller_id or state["lease_id"] != lease_id:
            raise TaskLifecycleError(
                "controller_lease_conflict", "controller lease identity does not match"
            )
