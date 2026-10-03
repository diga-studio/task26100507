import unittest

from equity_ledger import (
    AuditRecord,
    EquityLedger,
    Event,
    EventKind,
    LedgerError,
)


def issue(class_id, amount, time, to):
    return Event(EventKind.ISSUE, class_id, amount, time, to_holder=to)


def transfer(class_id, amount, time, src, dst):
    return Event(
        EventKind.TRANSFER, class_id, amount, time,
        from_holder=src, to_holder=dst,
    )


def cancel(class_id, amount, time, src):
    return Event(EventKind.CANCEL, class_id, amount, time, from_holder=src)


class AppendAndSnapshotTest(unittest.TestCase):
    def test_basic_flow(self):
        led = EquityLedger({"common": 1000, "pref": 500})
        led.append(issue("common", 100, 10, "A"))
        led.append(transfer("common", 40, 20, "A", "B"))
        led.append(cancel("common", 10, 30, "A"))
        self.assertEqual(led.snapshot("A", "common", 30), (50, 90))
        self.assertEqual(led.snapshot("B", "common", 30), (40, 90))
        # 时刻边界：包含该时刻的全部有效事件
        self.assertEqual(led.snapshot("A", "common", 19), (100, 100))
        self.assertEqual(led.snapshot("A", "common", 20), (60, 100))
        # 类别之间互不影响
        self.assertEqual(led.snapshot("A", "pref", 30), (0, 0))

    def test_backdated_append(self):
        led = EquityLedger({"common": 1000})
        led.append(issue("common", 100, 50, "A"))
        # 补录更早的发行
        led.append(issue("common", 30, 10, "A"))
        self.assertEqual(led.snapshot("A", "common", 40), (30, 30))
        self.assertEqual(led.snapshot("A", "common", 50), (130, 130))

    def test_reject_overdraw_on_append(self):
        led = EquityLedger({"common": 1000})
        led.append(issue("common", 50, 10, "A"))
        with self.assertRaises(LedgerError):
            led.append(transfer("common", 60, 20, "A", "B"))
        self.assertEqual(led.snapshot("A", "common", 20), (50, 50))
        self.assertEqual(led.snapshot("B", "common", 20), (0, 50))
        self.assertEqual(len(led.audit()), 1)

    def test_reject_exceeding_authorized_cap(self):
        led = EquityLedger({"common": 100})
        led.append(issue("common", 100, 1, "A"))
        with self.assertRaises(LedgerError):
            led.append(issue("common", 1, 2, "B"))
        self.assertEqual(led.snapshot("B", "common", 2), (0, 100))
        self.assertEqual(len(led.audit()), 1)

    def test_reject_invalid_event_shape(self):
        led = EquityLedger({"common": 100})
        with self.assertRaises(LedgerError):
            led.append(issue("common", 0, 1, "A"))
        with self.assertRaises(LedgerError):
            led.append(issue("common", True, 1, "A"))
        with self.assertRaises(LedgerError):
            led.append(issue("unknown", 1, 1, "A"))
        with self.assertRaises(LedgerError):
            led.append(issue("common", 1, 1.5, "A"))
        with self.assertRaises(LedgerError):
            led.append(Event(EventKind.ISSUE, "common", 1, 1,
                             from_holder="A", to_holder="B"))
        self.assertEqual(len(led.audit()), 0)


class RetroactiveCorrectionTest(unittest.TestCase):
    def test_reduce_issue_breaking_later_transfer_is_rejected(self):
        led = EquityLedger({"common": 1000})
        g1 = led.append(issue("common", 100, 10, "A"))
        led.append(transfer("common", 80, 20, "A", "B"))
        led.append(issue("common", 50, 30, "A"))
        # 当前 A 最新余额 = 100-80+50 = 70
        # 追溯把首次发行减为 70：t=20 处 70-80=-10 透支，
        # 但最新余额 70-80+50=40 仍为正 -> 必须拒绝
        with self.assertRaises(LedgerError):
            led.correct(g1, issue("common", 70, 10, "A"))
        # 无状态残留
        self.assertEqual(led.snapshot("A", "common", 99), (70, 150))
        self.assertEqual(led.snapshot("B", "common", 99), (80, 150))
        self.assertEqual(len(led.audit()), 3)
        self.assertTrue(all(r.action == "append" for r in led.audit()))

    def test_failed_correction_leaves_no_residue(self):
        led = EquityLedger({"common": 1000})
        e1 = led.append(issue("common", 100, 10, "A"))
        led.append(transfer("common", 40, 20, "A", "B"))
        before_audit = led.audit()
        before_snap = led.snapshot("A", "common", 20)
        with self.assertRaises(LedgerError):
            led.correct(e1, issue("common", 30, 10, "A"))
        self.assertEqual(led.snapshot("A", "common", 20), before_snap)
        self.assertEqual(led.audit(), before_audit)

    def test_issue_increase_correction_not_falsely_rejected(self):
        # 若按"临时撤去旧版"判定，撤去发行瞬间转让透支会误拒；
        # 完整替换后的账本合法，必须接受。
        led = EquityLedger({"common": 1000})
        e1 = led.append(issue("common", 100, 10, "A"))
        led.append(transfer("common", 90, 20, "A", "B"))
        led.correct(e1, issue("common", 120, 10, "A"))
        self.assertEqual(led.snapshot("A", "common", 20), (30, 120))
        self.assertEqual(led.snapshot("B", "common", 20), (90, 120))

    def test_correction_can_change_everything_but_identity(self):
        led = EquityLedger({"common": 1000, "pref": 1000})
        e1 = led.append(issue("common", 100, 10, "A"))
        # 改变种类、类别、参与方、股数、业务时刻
        led.correct(e1, issue("pref", 50, 5, "B"))
        self.assertEqual(led.snapshot("A", "common", 99), (0, 0))
        self.assertEqual(led.snapshot("B", "pref", 99), (50, 50))
        # event_id 与首次提交顺序号不变
        rec = led.audit()[-1]
        self.assertEqual(rec.event_id, e1)
        self.assertEqual(rec.submission_seq, 1)
        self.assertEqual(rec.action, "correct")
        self.assertEqual(rec.replaces, issue("common", 100, 10, "A"))

    def test_correct_unknown_event_id(self):
        led = EquityLedger({"common": 100})
        with self.assertRaises(LedgerError):
            led.correct(999, issue("common", 1, 1, "A"))
        with self.assertRaises(LedgerError):
            led.correct(999, None)


class SameTimeOrderingTest(unittest.TestCase):
    def test_same_time_events_use_submission_order(self):
        led = EquityLedger({"common": 1000})
        # 先提交转让（此时非法）-> 拒绝
        with self.assertRaises(LedgerError):
            led.append(transfer("common", 10, 5, "A", "B"))
        # 先发行后转让，同一时刻，按提交顺序逐条满足
        led.append(issue("common", 100, 5, "A"))
        led.append(transfer("common", 10, 5, "A", "B"))
        self.assertEqual(led.snapshot("A", "common", 5), (90, 100))
        self.assertEqual(led.snapshot("B", "common", 5), (10, 100))

    def test_corrections_preserve_submission_order(self):
        led = EquityLedger({"common": 1000})
        e1 = led.append(issue("common", 100, 5, "A"))
        e2 = led.append(transfer("common", 60, 5, "A", "B"))
        original_audit = led.audit()
        # 连续更正
        led.correct(e1, issue("common", 100, 5, "A"))
        led.correct(e1, issue("common", 80, 5, "A"))
        # 撤销再恢复
        led.correct(e2, None)
        self.assertEqual(led.snapshot("B", "common", 5), (0, 80))
        led.correct(e2, transfer("common", 60, 5, "A", "B"))
        # 顺序未变：发行仍在转让之前，账本合法
        self.assertEqual(led.snapshot("A", "common", 5), (20, 80))
        self.assertEqual(led.snapshot("B", "common", 5), (60, 80))
        # 若顺序被更改编乱（转让先于发行），同刻逐条校验会透支，
        # 这里余额为正说明顺序保持 (submission_seq 1 < 2)。
        seqs = {r.event_id: r.submission_seq for r in led.audit()}
        self.assertEqual(seqs[e1], 1)
        self.assertEqual(seqs[e2], 2)
        # 原始审计记录未被原地改写
        self.assertEqual(led.audit()[: len(original_audit)], original_audit)
        for rec in original_audit:
            self.assertEqual(rec.action, "append")
            self.assertIsNotNone(rec.event)

    def test_revoke_and_restore_keeps_position(self):
        led = EquityLedger({"common": 1000})
        e1 = led.append(issue("common", 50, 5, "A"))
        led.append(issue("common", 50, 5, "A"))
        led.append(transfer("common", 50, 5, "A", "B"))
        led.correct(e1, None)
        # 撤销首笔发行后，剩余账本仍合法（第二笔发行支撑转让）
        self.assertEqual(led.snapshot("A", "common", 5), (0, 50))
        self.assertEqual(led.snapshot("B", "common", 5), (50, 50))
        # 恢复已撤销的事件
        led.correct(e1, issue("common", 50, 5, "A"))
        # 恢复后 e1 仍按首次提交顺序号排在最前
        self.assertEqual(led.snapshot("A", "common", 5), (50, 100))
        self.assertEqual(led.snapshot("B", "common", 5), (50, 100))

    def test_revoke_that_breaks_ledger_is_rejected(self):
        led = EquityLedger({"common": 1000})
        e1 = led.append(issue("common", 50, 5, "A"))
        led.append(transfer("common", 50, 6, "A", "B"))
        # 撤销发行会使转让透支 -> 整体拒绝
        with self.assertRaises(LedgerError):
            led.correct(e1, None)
        self.assertEqual(led.snapshot("A", "common", 6), (0, 50))
        self.assertEqual(len(led.audit()), 2)


class AuditTrailTest(unittest.TestCase):
    def test_audit_records_are_complete_and_readonly(self):
        led = EquityLedger({"common": 1000})
        e1 = led.append(issue("common", 100, 10, "A"))
        led.correct(e1, issue("common", 120, 10, "A"))
        led.correct(e1, None)
        led.correct(e1, issue("common", 90, 10, "A"))
        audit = led.audit()
        self.assertEqual([r.action for r in audit],
                         ["append", "correct", "revoke", "correct"])
        # 替代关系链完整
        self.assertIsNone(audit[0].replaces)
        self.assertEqual(audit[1].replaces, issue("common", 100, 10, "A"))
        self.assertEqual(audit[2].replaces, issue("common", 120, 10, "A"))
        self.assertIsNone(audit[2].event)
        self.assertEqual(audit[3].replaces, None)
        self.assertEqual(audit[3].event, issue("common", 90, 10, "A"))
        # 审计序号连续
        self.assertEqual([r.audit_seq for r in audit], [1, 2, 3, 4])
        # 只读：记录与事件均为冻结 dataclass
        with self.assertRaises(AttributeError):
            audit[0].event = None
        with self.assertRaises(AttributeError):
            audit[0].event.amount = 1
        # 返回的是副本，外部修改不影响内部
        self.assertIsInstance(audit, tuple)

    def test_audit_records_not_counted_as_events(self):
        led = EquityLedger({"common": 100})
        e1 = led.append(issue("common", 100, 1, "A"))
        led.correct(e1, issue("common", 60, 1, "A"))
        # 流通量只按当前版本计算一次
        self.assertEqual(led.snapshot("A", "common", 1), (60, 60))


if __name__ == "__main__":
    unittest.main()
