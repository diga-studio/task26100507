import unittest

from equity_ledger import (
    AuditRecord, Event, EventKind, Ledger, LedgerError,
)


def issue(class_id, qty, t, holder):
    return Event(EventKind.ISSUE, class_id, qty, t, holder)


def transfer(class_id, qty, t, src, dst):
    return Event(EventKind.TRANSFER, class_id, qty, t, src, dst)


def cancel(class_id, qty, t, holder):
    return Event(EventKind.CANCEL, class_id, qty, t, holder)


class AppendAndSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger({"common": 1000, "pref": 500})

    def test_issue_transfer_cancel_snapshot(self):
        self.ledger.append(issue("common", 100, 1, "alice"))
        self.ledger.append(transfer("common", 40, 2, "alice", "bob"))
        self.ledger.append(cancel("common", 10, 3, "alice"))
        self.assertEqual(self.ledger.snapshot("alice", "common", 3), (50, 90))
        self.assertEqual(self.ledger.snapshot("bob", "common", 3), (40, 90))
        self.assertEqual(self.ledger.snapshot("carol", "common", 3), (0, 90))

    def test_snapshot_is_point_in_time(self):
        self.ledger.append(issue("common", 100, 5, "alice"))
        self.assertEqual(self.ledger.snapshot("alice", "common", 4), (0, 0))
        self.assertEqual(self.ledger.snapshot("alice", "common", 5), (100, 100))

    def test_backdated_append_allowed(self):
        self.ledger.append(issue("common", 100, 10, "alice"))
        self.ledger.append(issue("common", 50, 1, "alice"))  # 补录更早时刻
        self.assertEqual(self.ledger.snapshot("alice", "common", 5), (50, 50))
        self.assertEqual(self.ledger.snapshot("alice", "common", 10), (150, 150))

    def test_overdraft_rejected_without_residue(self):
        self.ledger.append(issue("common", 100, 1, "alice"))
        before = self.ledger.audit_trail()
        with self.assertRaises(LedgerError):
            self.ledger.append(transfer("common", 101, 2, "alice", "bob"))
        self.assertEqual(self.ledger.snapshot("alice", "common", 2), (100, 100))
        self.assertEqual(self.ledger.snapshot("bob", "common", 2), (0, 100))
        self.assertEqual(self.ledger.audit_trail(), before)

    def test_authorized_cap_enforced(self):
        self.ledger.append(issue("pref", 500, 1, "alice"))
        with self.assertRaises(LedgerError):
            self.ledger.append(issue("pref", 1, 2, "bob"))
        self.assertEqual(self.ledger.snapshot("bob", "pref", 2), (0, 500))

    def test_invalid_events_rejected(self):
        bad = [
            Event(EventKind.ISSUE, "nope", 1, 1, "alice"),      # 未知类别
            Event(EventKind.ISSUE, "common", 0, 1, "alice"),    # 非正数量
            Event(EventKind.ISSUE, "common", -5, 1, "alice"),
            Event(EventKind.ISSUE, "common", 1.5, 1, "alice"),
            Event(EventKind.TRANSFER, "common", 1, 1, "alice"), # 缺受让方
        ]
        for event in bad:
            with self.assertRaises(LedgerError, msg=repr(event)):
                self.ledger.append(event)
        self.assertEqual(self.ledger.audit_trail(), ())

    def test_classes_are_independent(self):
        self.ledger.append(issue("common", 100, 1, "alice"))
        self.ledger.append(issue("pref", 200, 1, "alice"))
        self.assertEqual(self.ledger.snapshot("alice", "common", 1), (100, 100))
        self.assertEqual(self.ledger.snapshot("alice", "pref", 1), (200, 200))


class RetroactiveCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger({"common": 1000})

    def test_retroactive_reduction_overdrawing_transfer_rejected(self):
        """追溯减少发行导致中间转让透支（最新余额仍为正）→ 拒绝且无残留。"""
        e1 = self.ledger.append(issue("common", 100, 1, "alice"))
        self.ledger.append(transfer("common", 80, 2, "alice", "bob"))
        self.ledger.append(issue("common", 50, 3, "alice"))
        # 最新余额: alice=70, bob=80, 流通=150
        before_audit = self.ledger.audit_trail()
        before_snap = self.ledger.snapshot("alice", "common", 3)
        # 把首次发行从 100 减到 30：时刻2 alice 只有 30，转让 80 透支；
        # 尽管最终余额 30-80+50=0 仍非负，也必须拒绝。
        with self.assertRaises(LedgerError):
            self.ledger.correct(e1, issue("common", 30, 1, "alice"))
        self.assertEqual(self.ledger.snapshot("alice", "common", 3), before_snap)
        self.assertEqual(self.ledger.snapshot("alice", "common", 3), (70, 150))
        self.assertEqual(self.ledger.audit_trail(), before_audit)

    def test_legitimate_increase_not_rejected_by_temporary_removal(self):
        """发行增额更正：若按'先撤旧版再插新版'判定会被误拒，完整替换必须接受。"""
        e1 = self.ledger.append(issue("common", 100, 1, "alice"))
        self.ledger.append(transfer("common", 90, 2, "alice", "bob"))
        # 临时撤去 e1 时，时刻2 的转让 90 无发行支撑会透支；
        # 但完整替换为 150 后所有前缀合法，必须接受。
        self.ledger.correct(e1, issue("common", 150, 1, "alice"))
        self.assertEqual(self.ledger.snapshot("alice", "common", 2), (60, 150))
        self.assertEqual(self.ledger.snapshot("bob", "common", 2), (90, 150))

    def test_correction_can_change_everything_but_identity(self):
        ledger = Ledger({"common": 1000, "pref": 500})
        e1 = ledger.append(issue("common", 100, 1, "alice"))
        ledger.append(issue("pref", 200, 2, "alice"))
        e3 = ledger.append(issue("common", 50, 3, "alice"))
        # 种类、类别、参与方、股数、业务时刻全部改变，仍合法
        ledger.correct(e3, transfer("pref", 60, 4, "alice", "bob"))
        self.assertEqual(ledger.snapshot("alice", "common", 4), (100, 100))
        self.assertEqual(ledger.snapshot("alice", "pref", 4), (140, 200))
        self.assertEqual(ledger.snapshot("bob", "pref", 4), (60, 200))
        # event_id 与首次提交顺序号不变
        rec = ledger.audit_trail()[-1]
        self.assertEqual(rec.event_id, e3)
        self.assertEqual(rec.submission_seq, ledger.audit_trail()[2].submission_seq)

    def test_revoke_and_restore(self):
        e1 = self.ledger.append(issue("common", 100, 1, "alice"))
        e2 = self.ledger.append(transfer("common", 40, 2, "alice", "bob"))
        self.ledger.correct(e2, None)  # 撤销转让
        self.assertEqual(self.ledger.snapshot("bob", "common", 2), (0, 100))
        # 撤销发行会导致转让透支？转让已撤销，故可撤销发行
        self.ledger.correct(e1, None)
        self.assertEqual(self.ledger.snapshot("alice", "common", 2), (0, 0))
        # 恢复（已撤销事件也可恢复）
        self.ledger.correct(e1, issue("common", 100, 1, "alice"))
        self.ledger.correct(e2, transfer("common", 40, 2, "alice", "bob"))
        self.assertEqual(self.ledger.snapshot("alice", "common", 2), (60, 100))
        self.assertEqual(self.ledger.snapshot("bob", "common", 2), (40, 100))

    def test_revoke_causing_overdraft_rejected(self):
        e1 = self.ledger.append(issue("common", 100, 1, "alice"))
        self.ledger.append(transfer("common", 40, 2, "alice", "bob"))
        before = self.ledger.snapshot("alice", "common", 2)
        with self.assertRaises(LedgerError):
            self.ledger.correct(e1, None)  # 撤销发行后转让透支
        self.assertEqual(self.ledger.snapshot("alice", "common", 2), before)

    def test_correct_unknown_event_rejected(self):
        with self.assertRaises(LedgerError):
            self.ledger.correct(999, issue("common", 1, 1, "alice"))


class SubmissionOrderTest(unittest.TestCase):
    """同一时刻的事件按首次提交顺序号逐条判定；更正/撤销/恢复不改变顺序。"""

    def test_same_time_ordering_and_stable_seq(self):
        ledger = Ledger({"common": 1000})
        e1 = ledger.append(issue("common", 100, 5, "alice"))       # seq 1
        e2 = ledger.append(transfer("common", 60, 5, "alice", "bob"))  # seq 2
        # 同刻第三条：alice 此刻余额 100-60=40，再转 40 恰好归零。
        # 若顺序不是按首次提交号（如转让先于发行），前缀即透支。
        e3 = ledger.append(transfer("common", 40, 5, "alice", "bob"))  # seq 3
        self.assertEqual(ledger.snapshot("alice", "common", 5), (0, 100))
        self.assertEqual(ledger.snapshot("bob", "common", 5), (100, 100))

        original_audit = ledger.audit_trail()
        original_snaps = [ledger.snapshot(h, "common", 5)
                          for h in ("alice", "bob")]

        # 连续更正、撤销、恢复，均不得改变首次提交顺序
        ledger.correct(e1, issue("common", 100, 5, "alice"))
        ledger.correct(e2, None)
        ledger.correct(e2, transfer("common", 60, 5, "alice", "bob"))
        ledger.correct(e3, transfer("common", 40, 5, "alice", "bob"))
        self.assertEqual([ledger.snapshot(h, "common", 5)
                          for h in ("alice", "bob")], original_snaps)

        # 顺序号稳定
        seqs = {rec.event_id: rec.submission_seq
                for rec in ledger.audit_trail()}
        self.assertEqual(seqs[e1], 1)
        self.assertEqual(seqs[e2], 2)
        self.assertEqual(seqs[e3], 3)

        # 原始审计记录未被原地改写
        current = ledger.audit_trail()
        self.assertEqual(current[:len(original_audit)], original_audit)
        self.assertEqual(current[0].event, issue("common", 100, 5, "alice"))
        self.assertIsNone(current[0].supersedes)
        self.assertEqual(current[0].version_no, 1)

    def test_backdated_same_time_event_sorts_after_existing(self):
        ledger = Ledger({"common": 1000})
        ledger.append(issue("common", 50, 5, "alice"))        # seq 1
        ledger.append(transfer("common", 50, 5, "alice", "bob"))  # seq 2
        # 补录同刻发行：顺序号更靠后，不能'拯救'排在它之前的转让
        with self.assertRaises(LedgerError):
            ledger.append(transfer("common", 1, 5, "alice", "bob"))
        ledger.append(issue("common", 10, 5, "alice"))  # 合法补录
        self.assertEqual(ledger.snapshot("alice", "common", 5), (10, 60))


class AuditTrailTest(unittest.TestCase):
    def test_audit_records_and_supersede_chain(self):
        ledger = Ledger({"common": 1000})
        e1 = ledger.append(issue("common", 100, 1, "alice"))
        ledger.correct(e1, issue("common", 120, 1, "alice"))
        ledger.correct(e1, None)
        trail = ledger.audit_trail()
        self.assertEqual(len(trail), 3)
        self.assertEqual([r.audit_seq for r in trail], [1, 2, 3])
        self.assertEqual([r.version_no for r in trail], [1, 2, 3])
        self.assertEqual([r.supersedes for r in trail], [None, 1, 2])
        self.assertTrue(all(r.event_id == e1 for r in trail))
        self.assertIsNone(trail[2].event)  # 撤销记录
        # 审计记录为只读值对象
        self.assertIsInstance(trail[0], AuditRecord)
        with self.assertRaises(AttributeError):
            trail[0].event = None
        # 审计序列不重复计入经济事件：余额只按当前版本计算一次
        self.assertEqual(ledger.snapshot("alice", "common", 1), (0, 0))

    def test_audit_trail_returned_copy(self):
        ledger = Ledger({"common": 1000})
        ledger.append(issue("common", 100, 1, "alice"))
        trail = ledger.audit_trail()
        self.assertIsInstance(trail, tuple)
        ledger.append(issue("common", 1, 2, "alice"))
        self.assertEqual(len(trail), 1)  # 旧快照不随后续操作变化
        self.assertEqual(len(ledger.audit_trail()), 2)


if __name__ == "__main__":
    unittest.main()
