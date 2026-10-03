"""支持追溯更正的股权流水库（纯内存，仅标准库）。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple


class EventKind(Enum):
    ISSUE = "issue"        # 向股东发行
    TRANSFER = "transfer"  # 同类别股份转让
    CANCEL = "cancel"      # 从股东注销


@dataclass(frozen=True)
class Event:
    """一条经济事件的内容（不含 event_id / 提交顺序号）。"""
    kind: EventKind
    class_id: str
    quantity: int
    effective_time: int
    holder: str                 # 发行接收方 / 注销方 / 转让出让方
    counterparty: Optional[str] = None  # 仅转让使用：受让方


@dataclass(frozen=True)
class AuditRecord:
    """只读审计记录：每次成功提交的原始记录或更正记录。"""
    audit_seq: int              # 审计序列号（单调递增）
    event_id: int               # 事件 ID（稳定）
    submission_seq: int         # 首次提交顺序号（稳定）
    version_no: int             # 该事件的版本号，从 1 开始
    supersedes: Optional[int]   # 被替换版本的 version_no；原始记录为 None
    event: Optional[Event]      # 当前版本内容；None 表示撤销


class LedgerError(Exception):
    """校验失败等业务错误，操作被整体拒绝。"""


class Ledger:
    """股权流水库：状态仅驻内存。

    不变式：将各事件当前非空版本按 (业务时刻, 首次提交顺序号) 排序后，
    任意前缀内每个股东每个类别的持股数不为负，且每类流通总量不超过授权上限。
    """

    def __init__(self, authorized: Dict[str, int]) -> None:
        if not authorized:
            raise LedgerError("至少需要一个股份类别")
        for class_id, cap in authorized.items():
            if not isinstance(class_id, str) or not class_id:
                raise LedgerError("类别 ID 必须为非空字符串")
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise LedgerError("授权上限必须为正整数")
        self._authorized: Dict[str, int] = dict(authorized)
        self._current: Dict[int, Optional[Event]] = {}   # event_id -> 当前版本（None=已撤销）
        self._submission_seq: Dict[int, int] = {}        # event_id -> 首次提交顺序号
        self._version_no: Dict[int, int] = {}            # event_id -> 当前版本号
        self._next_event_id = 1
        self._next_submission_seq = 1
        self._audit: List[AuditRecord] = []

    # ---- 内部辅助 ----

    def _validate_event(self, event: Event) -> None:
        if not isinstance(event, Event):
            raise LedgerError("事件必须是 Event 实例")
        if not isinstance(event.kind, EventKind):
            raise LedgerError("未知的事件种类")
        if event.class_id not in self._authorized:
            raise LedgerError(f"未知股份类别: {event.class_id!r}")
        if (not isinstance(event.quantity, int)
                or isinstance(event.quantity, bool) or event.quantity <= 0):
            raise LedgerError("数量必须为正整数")
        if (not isinstance(event.effective_time, int)
                or isinstance(event.effective_time, bool)):
            raise LedgerError("业务时刻必须为整数")
        if not isinstance(event.holder, str) or not event.holder:
            raise LedgerError("股东 ID 必须为非空字符串")
        if event.kind is EventKind.TRANSFER:
            if not isinstance(event.counterparty, str) or not event.counterparty:
                raise LedgerError("转让必须指定受让方")

    def _ordered_events(self, current: Dict[int, Optional[Event]]):
        """当前非空版本按 (业务时刻, 首次提交顺序号) 排序。"""
        items = [
            (event.effective_time, self._submission_seq[event_id], event)
            for event_id, event in current.items() if event is not None
        ]
        items.sort(key=lambda item: (item[0], item[1]))
        return [event for _, _, event in items]

    def _check_invariants(self, current: Dict[int, Optional[Event]]) -> None:
        balances: Dict[Tuple[str, str], int] = {}
        outstanding: Dict[str, int] = {cid: 0 for cid in self._authorized}
        for event in self._ordered_events(current):
            cid = event.class_id
            if event.kind is EventKind.ISSUE:
                key = (event.holder, cid)
                balances[key] = balances.get(key, 0) + event.quantity
                outstanding[cid] += event.quantity
            elif event.kind is EventKind.CANCEL:
                key = (event.holder, cid)
                balances[key] = balances.get(key, 0) - event.quantity
                outstanding[cid] -= event.quantity
            else:  # TRANSFER
                src = (event.holder, cid)
                dst = (event.counterparty, cid)
                balances[src] = balances.get(src, 0) - event.quantity
                balances[dst] = balances.get(dst, 0) + event.quantity
            if outstanding[cid] > self._authorized[cid]:
                raise LedgerError(
                    f"类别 {cid!r} 流通总量超过授权上限")
            for key in ((event.holder, cid), (event.counterparty, cid)):
                if key[0] is not None and balances.get(key, 0) < 0:
                    raise LedgerError(
                        f"股东 {key[0]!r} 在类别 {key[1]!r} 持股为负")

    # ---- 公开操作 ----

    def append(self, event: Event) -> int:
        """新增一条经济事件，返回稳定的 event_id。"""
        self._validate_event(event)
        event_id = self._next_event_id
        candidate = dict(self._current)
        candidate[event_id] = event
        self._submission_seq[event_id] = self._next_submission_seq  # 试排顺序
        try:
            self._check_invariants(candidate)
        except LedgerError:
            del self._submission_seq[event_id]
            raise
        self._current = candidate
        self._version_no[event_id] = 1
        self._next_event_id += 1
        self._next_submission_seq += 1
        self._audit.append(AuditRecord(
            audit_seq=len(self._audit) + 1,
            event_id=event_id,
            submission_seq=self._submission_seq[event_id],
            version_no=1,
            supersedes=None,
            event=event,
        ))
        return event_id

    def correct(self, event_id: int, replacement: Optional[Event]) -> None:
        """追加更正记录；replacement 为 None 表示撤销。可反复更正、可恢复。"""
        if event_id not in self._current:
            raise LedgerError(f"未知事件: {event_id!r}")
        if replacement is not None:
            self._validate_event(replacement)
        candidate = dict(self._current)
        candidate[event_id] = replacement
        self._check_invariants(candidate)  # 按完整替换后的账本判定
        self._current = candidate
        old_version = self._version_no[event_id]
        self._version_no[event_id] = old_version + 1
        self._audit.append(AuditRecord(
            audit_seq=len(self._audit) + 1,
            event_id=event_id,
            submission_seq=self._submission_seq[event_id],
            version_no=old_version + 1,
            supersedes=old_version,
            event=replacement,
        ))

    def snapshot(self, stakeholder_id: str, class_id: str,
                 effective_time: int) -> Tuple[int, int]:
        """返回 (该股东在该类别该时刻的持股数, 该类别流通总量)。

        采用当前更正后的历史，包含该时刻的全部有效事件。
        """
        if class_id not in self._authorized:
            raise LedgerError(f"未知股份类别: {class_id!r}")
        holding = 0
        outstanding = 0
        for event in self._ordered_events(self._current):
            if event.effective_time > effective_time:
                break
            if event.class_id != class_id:
                continue
            if event.kind is EventKind.ISSUE:
                outstanding += event.quantity
                if event.holder == stakeholder_id:
                    holding += event.quantity
            elif event.kind is EventKind.CANCEL:
                outstanding -= event.quantity
                if event.holder == stakeholder_id:
                    holding -= event.quantity
            else:  # TRANSFER
                if event.holder == stakeholder_id:
                    holding -= event.quantity
                if event.counterparty == stakeholder_id:
                    holding += event.quantity
        return holding, outstanding

    def audit_trail(self) -> Tuple[AuditRecord, ...]:
        """返回完整只读审计序列（原始记录与更正记录，含替代关系）。"""
        return tuple(self._audit)
