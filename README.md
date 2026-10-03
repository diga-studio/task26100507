# task26100507

支持追溯更正的股权流水库（纯 Python 标准库，状态仅驻内存）。

## 文件

- `equity_ledger.py` — 库本体，无第三方依赖
- `test_equity_ledger.py` — 自动化测试（`python3 -m unittest -v test_equity_ledger`）

## 快速上手

```python
from equity_ledger import Ledger, Event, EventKind

# 初始化：各类别授权上限
ledger = Ledger({"common": 1_000_000, "pref": 500_000})

# append：发行 / 转让 / 注销，返回稳定 event_id
e1 = ledger.append(Event(EventKind.ISSUE, "common", 100, 1, "alice"))
e2 = ledger.append(Event(EventKind.TRANSFER, "common", 40, 2, "alice", "bob"))
ledger.append(Event(EventKind.CANCEL, "common", 10, 3, "alice"))

# correct：完整替换（可改种类/类别/参与方/股数/时刻）；None 表示撤销；可反复更正、可恢复
ledger.correct(e2, Event(EventKind.TRANSFER, "common", 30, 2, "alice", "bob"))
ledger.correct(e2, None)                                   # 撤销
ledger.correct(e2, Event(EventKind.TRANSFER, "common", 30, 2, "alice", "bob"))  # 恢复

# snapshot：按当前更正后的历史查询，返回 (持股数, 类别流通总量)
holding, outstanding = ledger.snapshot("alice", "common", 3)

# audit_trail：只读审计序列（原始记录 + 更正记录 + 替代关系），不计入经济事件
for rec in ledger.audit_trail():
    print(rec)
```

## 语义要点

- 业务时刻为整数，允许补录早于已有记录的流水；同一时刻按**首次提交顺序号**逐条判定。
- 每次 `append`/`correct` 成功后，当前非空版本按 (业务时刻, 首次提交顺序号) 排序，
  任意前缀内每股东每类别持股不得为负、每类流通总量不得超过授权上限；
  违反则整体拒绝，状态与审计序列不变。更正按**完整替换后**的账本判定。
- `event_id` 与首次提交顺序号一旦分配即稳定，更正/撤销/恢复均不改变。
- 校验失败抛出 `LedgerError`。
