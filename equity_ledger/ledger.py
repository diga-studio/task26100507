"""支持追溯更正的内存股权流水库。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class EventKind(Enum):
    ISSUE = "issue"
    TRANSFER = "transfer"
    CANCEL = "cancel"


@dataclass(frozen=True)
class Event:
    """一条经济事件（不可变）。"""

    kind: EventKind
    class_id: str
    amount: int
    effective_time: int
    from_holder: Optional[str] = None
    to_holder: Optional[str] = None


@dataclass(frozen=True)
class AuditRecord:
    """审计序列中的一条只读记录。"""

    audit_seq: int
    event_id: int
    submission_seq: int
    action: str  # "append" | "correct" | "revoke"
    event: Optional[Event]
    replaces: Optional[Event]


class LedgerError(Exception):
    """非法操作（违反余额/授权上限等约束）。"""


class EquityLedger:
    """内存股权流水库。"""

    def __init__(self, authorized: Dict[str, int]) -> None:
        if not authorized:
            raise LedgerError("至少需要一个股份类别")
        for class_id, cap in authorized.items():
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise LedgerError(f"类别 {class_id!r} 的授权上限必须为正整数")
        self._authorized: Dict[str, int] = dict(authorized)
        # event_id -> 当前版本（None 表示已撤销）
        self._current: Dict[int, Optional[Event]] = {}
        # event_id -> 首次提交顺序号
        self._submission: Dict[int, int] = {}
        self._next_event_id = 1
        self._next_submission_seq = 1
        self._audit: List[AuditRecord] = []

    # ---- 内部工具 ----

    def _check_event(self, event: Event) -> None:
        if not isinstance(event, Event):
            raise LedgerError("事件必须是 Event 实例")
        if not isinstance(event.kind, EventKind):
            raise LedgerError("未知的事件种类")
        if event.class_id not in self._authorized:
            raise LedgerError(f"未知的股份类别: {event.class_id!r}")
        if (
            not isinstance(event.amount, int)
            or isinstance(event.amount, bool)
            or event.amount <= 0
        ):
            raise LedgerError("数量必须为正整数")
        if not isinstance(event.effective_time, int) or isinstance(
            event.effective_time, bool
        ):
            raise LedgerError("业务时刻必须为整数")
        if event.kind is EventKind.ISSUE:
            if event.to_holder is None or event.from_holder is not None:
                raise LedgerError("发行事件需要且仅需要 to_holder")
        elif event.kind is EventKind.CANCEL:
            if event.from_holder is None or event.to_holder is not None:
                raise LedgerError("注销事件需要且仅需要 from_holder")
        else:  # TRANSFER
            if event.from_holder is None or event.to_holder is None:
                raise LedgerError("转让事件需要 from_holder 与 to_holder")

    def _ordered_events(self) -> List[Event]:
        """当前非空版本按 (业务时刻, 首次提交顺序号) 排序。"""
        live = [
            (self._submission[eid], event)
            for eid, event in self._current.items()
            if event is not None
        ]
        live.sort(key=lambda item: (item[1].effective_time, item[0]))
        return [event for _, event in live]

    @staticmethod
    def _apply(
        event: Event,
        balances: Dict[Tuple[str, str], int],
        outstanding: Dict[str, int],
    ) -> None:
        if event.kind is EventKind.ISSUE:
            key = (event.to_holder, event.class_id)
            balances[key] = balances.get(key, 0) + event.amount
            outstanding[event.class_id] = (
                outstanding.get(event.class_id, 0) + event.amount
            )
        elif event.kind is EventKind.CANCEL:
            key = (event.from_holder, event.class_id)
            balances[key] = balances.get(key, 0) - event.amount
            outstanding[event.class_id] = (
                outstanding.get(event.class_id, 0) - event.amount
            )
        else:  # TRANSFER
            src = (event.from_holder, event.class_id)
            dst = (event.to_holder, event.class_id)
            balances[src] = balances.get(src, 0) - event.amount
            balances[dst] = balances.get(dst, 0) + event.amount

    def _validate_all_prefixes(self) -> None:
        """逐条回放，任一前缀不得出现负持股或超授权流通量。"""
        balances: Dict[Tuple[str, str], int] = {}
        outstanding: Dict[str, int] = {}
        for event in self._ordered_events():
            self._apply(event, balances, outstanding)
            for (holder, class_id), amount in balances.items():
                if amount < 0:
                    raise LedgerError(
                        f"股东 {holder!r} 在类别 {class_id!r} 的持股将为负"
                    )
            for class_id, total in outstanding.items():
                if total > self._authorized[class_id]:
                    raise LedgerError(
                        f"类别 {class_id!r} 的流通总量超过授权上限"
                    )

    # ---- 公开操作 ----

    def append(self, event: Event) -> int:
        self._check_event(event)
        event_id = self._next_event_id
        submission_seq = self._next_submission_seq
        self._current[event_id] = event
        self._submission[event_id] = submission_seq
        try:
            self._validate_all_prefixes()
        except LedgerError:
            del self._current[event_id]
            del self._submission[event_id]
            raise
        self._next_event_id += 1
        self._next_submission_seq += 1
        self._audit.append(
            AuditRecord(
                audit_seq=len(self._audit) + 1,
                event_id=event_id,
                submission_seq=submission_seq,
                action="append",
                event=event,
                replaces=None,
            )
        )
        return event_id

    def correct(self, event_id: int, replacement: Optional[Event]) -> None:
        if event_id not in self._current:
            raise LedgerError(f"未知的 event_id: {event_id!r}")
        if replacement is not None:
            self._check_event(replacement)
        old = self._current[event_id]
        self._current[event_id] = replacement
        try:
            self._validate_all_prefixes()
        except LedgerError:
            self._current[event_id] = old
            raise
        self._audit.append(
            AuditRecord(
                audit_seq=len(self._audit) + 1,
                event_id=event_id,
                submission_seq=self._submission[event_id],
                action="revoke" if replacement is None else "correct",
                event=replacement,
                replaces=old,
            )
        )

    def snapshot(
        self, stakeholder_id: str, class_id: str, effective_time: int
    ) -> Tuple[int, int]:
        if class_id not in self._authorized:
            raise LedgerError(f"未知的股份类别: {class_id!r}")
        holding = 0
        total = 0
        for event in self._ordered_events():
            if event.effective_time > effective_time:
                continue
            if event.class_id != class_id:
                continue
            if event.kind is EventKind.ISSUE:
                total += event.amount
                if event.to_holder == stakeholder_id:
                    holding += event.amount
            elif event.kind is EventKind.CANCEL:
                total -= event.amount
                if event.from_holder == stakeholder_id:
                    holding -= event.amount
            else:  # TRANSFER
                if event.from_holder == stakeholder_id:
                    holding -= event.amount
                if event.to_holder == stakeholder_id:
                    holding += event.amount
        return holding, total

    def audit(self) -> Tuple[AuditRecord, ...]:
        return tuple(self._audit)
