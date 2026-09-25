"""Minimal agent-to-agent (A2A) messaging used by the L3A workflow.

Every message is correlated by ``case_id`` and carries only observable facts
(evidence refs, decision codes, structured findings), never free-form reasoning.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from .trace import TraceWriter

MAX_HOPS = 16


@dataclass(frozen=True)
class A2AMessage:
    message_id: str
    case_id: str
    sender: str
    recipient: str
    intent: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    hop: int = 0


class A2ABus:
    """In-process message bus. Emits observable trace events for each message."""

    def __init__(self, case_id: str, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.trace = trace
        self.log: list[A2AMessage] = []
        self._counter = itertools.count(1)

    def send(
        self,
        sender: str,
        recipient: str,
        intent: str,
        payload: dict[str, Any] | None = None,
        evidence_refs: list[str] | tuple[str, ...] = (),
    ) -> A2AMessage:
        hop = len(self.log)
        if hop >= MAX_HOPS:
            raise RuntimeError(f"{self.case_id}: A2A hop limit exceeded")
        message = A2AMessage(
            message_id=f"{self.case_id}-m{next(self._counter):02d}",
            case_id=self.case_id,
            sender=sender,
            recipient=recipient,
            intent=intent,
            payload=payload or {},
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            hop=hop,
        )
        self.log.append(message)
        event_type = "task_assigned" if intent.startswith("assign:") else "handoff"
        self.trace.emit(
            case_id=self.case_id,
            event_type=event_type,
            actor=sender,
            target=recipient,
            decision_code=intent,
            evidence_refs=list(message.evidence_refs) or None,
            attributes={"hop": hop},
        )
        return message
