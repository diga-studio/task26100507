# equity-ledger

支持追溯更正的内存股权流水库（纯 Python 标准库，无外部依赖）。

## 功能

- 三种流水：向股东发行（`ISSUE`）、同类别转让（`TRANSFER`）、从股东注销（`CANCEL`）。
- 业务时刻为整数，允许补录早于已有记录的流水。
- `correct` 追加更正记录，完整替换事件当前版本；`None` 表示撤销，已撤销事件可恢复。
  更正不改变 `event_id` 与首次提交顺序号。
- 每次成功调用后，按（业务时刻, 首次提交顺序号）逐条回放校验：任意前缀内
  股东持股不得为负、各类别流通量不得超过授权上限；违反则整体拒绝，状态与审计序列不变。
- 全部原始记录与更正记录保留为只读审计序列，不原地改写。

## 用法

```python
from equity_ledger import EquityLedger, Event, EventKind, LedgerError

led = EquityLedger({"common": 1_000_000, "pref": 500_000})

e1 = led.append(Event(EventKind.ISSUE, "common", 100, 10, to_holder="A"))

# 追溯更正：完整替换 e1（可改种类/类别/参与方/股数/业务时刻）
led.correct(e1, Event(EventKind.ISSUE, "common", 120, 5, to_holder="A"))
# 撤销与恢复
led.correct(e1, None)
led.correct(e1, Event(EventKind.ISSUE, "common", 120, 5, to_holder="A"))

led.append(Event(EventKind.TRANSFER, "common", 40, 20,
                 from_holder="A", to_holder="B"))

holding, outstanding = led.snapshot("A", "common", 20)

for record in led.audit():      # 只读审计序列
    print(record.audit_seq, record.action, record.event_id, record.event)
```

非法操作抛出 `LedgerError`，且不留任何状态残留。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

## 文件

- `equity_ledger/ledger.py` — 库实现（`Event`、`EquityLedger`、`AuditRecord`、`LedgerError`）
- `tests/test_equity_ledger.py` — 自动化测试
