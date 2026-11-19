# 受限混配方案与实际领料质量核账

纯 Python 3.11 标准库实现（SQLite 持久化 + `fractions.Fraction` 精确有理数 +
自研精确分支定界整数搜索），无第三方依赖。所有数据均为合成，不对应任何真实
食品或药品标准。

## 目录结构

```
blend/
  model.py      不可变数据模型、精确有理数、统一异常
  catalog.py    独立原料质量目录（SQLite，原子修订，质量边界产品）
  planner.py    整数混配规划器（精确分支定界）+ 独立全枚举核对器
  warehouse.py  履约仓（预览/确认事务锁、领料/退回/取消状态机、台账）
  trace.py      实物包谱系（分装/混合/外发/退回、幂等追溯、定向召回）
tests/          43 个 unittest 用例（含 300 例随机实例与全枚举一致性核对）
demo.py         九个端到端场景演示
```

## 运行

```bash
python3 -m unittest discover -s tests -v   # 全量测试
python3 demo.py                             # 端到端演示
```

## 模块入口

### 1. 质量目录 `blend.catalog.QualityCatalog`

- `replace_lots(lots, note)` —— 一次整体修订（区间与可用量同事务生效），
  失败整体回滚、修订号不动；历史修订永久保留，可按 rev 读取。
- `product(usage, rev=None)` —— 独立计算任意用量组合的每成分质量和上下界
  与分数上下界，产出 `UsageProduct`（不感知订单、不处理优先级）。
- `get_lots / get_lot / components / revisions` —— 修订与 lot 查询。
- 区间语义：同批每克的同一未知成分分数在 `[lo,hi]` 内任取，成分间不声明
  相关性；质量和界为各 lot 贡献区间的线性求和（独立区间算术）。

### 2. 规划器 `blend.planner.Planner`

```python
Planner(order, lots).solve()          # 无解抛 InfeasibleError
Planner(order, lots).solve_or_none()  # 无解返回 None
brute_force_best(order, lots)         # 独立第二条实现：笛卡尔积全枚举
```

- 决策变量本身就是整数克数（0 或步长倍数，截断到库存/M），**不做连续比例
  再舍入**；
- 稳健质量语义：所有 Fraction 端点统一放大成整数，稳健合格落地为整系数
  线性不等式 `Σu·lo ≥ L·M` 且 `Σu·hi ≤ H·M`，对目录允许的全部取值恒成立；
- 三级字典序目标：成本最小 → 正用量 lot 数最少 → 排序 lot_id 用量向量
  字典序最小（元组比较天然保证）；
- 剪枝：子集容量/步长 gcd/质量包络预筛 + DFS 后缀容量、gcd、质量可达包络、
  线性成本下界剪枝；规模上限（12 批、M≤300、步长 1..20、4 成分）下秒级完成。
- `evaluate_usage(lots, usage)` 不依赖目录即可算质量产品，仓库可消费外部产品。

### 3. 履约仓 `blend.warehouse.Warehouse`

| 入口 | 语义 |
| --- | --- |
| `register_lots` | 登记物料档案与初始库存 |
| `plan_order / preview_order` | 规划并保存预览，绑定目录 rev，**不占料** |
| `preview_external` | 消费外部 `PlanResult/UsageProduct`，仓库不重新解释质量 |
| `confirm_preview` | 单 `BEGIN IMMEDIATE` 事务：版本核对 → 库存核对 → 占料落账 |
| `issue` | 实际领料（PENDING → 已领），写 ISSUE 台账 |
| `return_material` | 已领份额登记退回入库，ISSUE/RETURN 双留痕 |
| `cancel_order` | 只释放尚未领出的份额；已领部分不复活 |
| `get_order / explain_order / inventory` | 查询与解释入口 |

- **并发争料**：确认在写锁事务内逐 lot 复核 `initial - allocated - issued
  + returned`，两个独立连接不可能双花同一份库存，失败方收到
  `ConflictError`（或锁等待超时）。
- **质量换代**：确认时若目录 rev 与预览绑定值不一致，事务整体回滚并抛
  `StalePreviewError`，预览标记 `REJECTED_STALE`，绝不把旧方案套新质量；
  已确认订单永久保存确认时的质量边界快照与来源 rev，旧确认结果保持来源。
- **状态机**：`PENDING → PARTIAL_ISSUED → ISSUED`；取消置 `RELEASED`；
  退回置 `PARTIAL_RETURNED/RETURNED`。文件库重开后全部状态完整保持。

## 演示场景（`demo.py`）

1. 目录独立质量产品与精确分数边界；
2. 中点可行而稳健不行（便宜宽区间料被规划器拒绝）；
3. 固定费改变最优方案；
4. 包装步长约束与步长 gcd 不可达的 INFEASIBLE；
5. 预览不占料 → 确认 → 部分领料 → 取消只放未领 → 退回留痕；
6. 两个独立线程/连接抢最后 10g 库存，恰好一个成功、审计无双花；
7. 质量换代：旧预览拒绝、按新质量重新规划、旧确认保持 rev1 来源；
8. 12 批/M=300/4 成分最大规模性能，以及小库存分支定界 vs 全枚举最优性核对；
9. 实物包谱系：分装/混合的精确分数贡献、外发/退回、按记录位置的定向召回。

## 实物包层 `blend.trace.PhysicalTrace`

在原履约库上**增量启用**（共享同一 SQLite 连接），不改变既有普通领料入口：

| 入口 | 语义 |
| --- | --- |
| `PhysicalTrace(warehouse)` | 启用实物层；构造时把历史 `ISSUE` 台账补登记 |
| `split(op_id, parent, children)` | 1 父 → N 子，等比例分装，精确有理数贡献 |
| `mix(op_id, parents, grams)` | N 父 → 1 子，均匀混合，贡献逐根 lot 求和 |
| `operate(op_id, kind, parents, children)` | 通用入口，一次只能分或只能混 |
| `ship(shipment_id, package_id, recipient)` | 外发**一个完整活动包**（部分外发须先 `split`） |
| `return_shipment(shipment_id, recipient=None)` | 按当前持有者的发运记录退回同一包，收回后可重发 |
| `recall(root_lot, position=None)` | 指定记录位置仍在外的包、收件方、暴露克数与谱系路径 |
| `contact_list(root_lot, min_grams, position=None)` | 按收件方累计贡献，达到门槛才列入待联系清单 |
| `package / packages / routes_for / journal` | 仓内、在外、已退回、已耗用中间包及路径查询 |

- **首次实物包**：`Warehouse.issue` 在同一事务内调用实物层钩子，生成
  `PKG-ISSUE-<ledger_id>`，来源为该 lot、贡献即领出克数。启用前已存在的
  领料登记为 `UNREFINED`（来源未细分），**无已知根 lot 贡献、不进入在外
  累计、不能外发/加工**，从而不会冒认已知去向。
- **守恒与精确有理数**：`split` 子包贡献 = 父包贡献 × 子克数/父克数；
  `mix` 子包贡献 = 各父贡献之和；操作前后总克数必须相等。三种根 lot
  贡献全程用 `Fraction`（如 `12/5`、`18/5`）。
- **整包耗用 + 幂等 + 并发**：父包被整包置为 `CONSUMED`。`op_id` 为幂等
  键：同编号同内容重放读回原收据，异文抛 `StateError`。耗用父包在
  `BEGIN IMMEDIATE` 写锁内复核状态，并发双耗只有一个成功，失败方整体
  回滚、不留任何子包，父包保持原状态可继续使用。
- **外发/退回**：`ship` 只接受完整活动包；`return_shipment` 把该运输包
  收回仓内并可重发，不改变 `wh_lots` 库存（区别于 `return_material`）。
- **召回时间点**：以自增 journal 的 `position` 表示记录位置。`recall` 只
  统计 `shipped_pos <= P` 且 `P` 前未退回的包；仓内、已退回、已耗用、来源
  未细分都不进入在外累计。历史位置在关闭重开后仍能复现同样的清单。

### 复现步骤（题面例子）

```python
from fractions import Fraction
from blend.catalog import QualityCatalog
from blend.model import PlannedLot, PlanResult
from blend.planner import evaluate_usage
from blend.trace import PhysicalTrace
from blend.warehouse import Warehouse
from tests.factories import make_lot, make_order

cat = QualityCatalog(":memory:")
lots = [make_lot("A", 500, {"c1": ("0", "1")}),
        make_lot("B", 500, {"c1": ("0", "1")})]
cat.replace_lots(lots)
wh = Warehouse(":memory:", cat); wh.register_lots(lots)
tr = PhysicalTrace(wh)                      # 既有库上增量启用

# 1) 按 A6+B4 确认并领料，生成首次实物包
usage = {"A": 6, "B": 4}
order = make_order("O1", 10, {"c1": ("0", "1")})
plan = PlanResult("O1", 10,
                  tuple(PlannedLot(l.lot_id, usage[l.lot_id], l.step_g,
                                   l.cost_per_g, l.fixed_fee) for l in lots),
                  evaluate_usage(lots, usage), 0, 2, "manual")
wh.confirm_preview(wh.preview_external(order, plan))
for lot, g in usage.items():
    wh.issue("O1", lot, g)
pk = {p.lot_id: p.package_id for p in tr.packages() if p.state == "IN_STOCK"}

# 2) 混合成 10g，再分 X 4g / Y 6g
mixed = tr.mix("MIX", [pk["A"], pk["B"]], 10)
split = tr.split("SPLIT", mixed.children[0][0], [4, 6])
X, Y = split.children[0][0], split.children[1][0]
assert tr.package_content_dict(X)["A"] == Fraction(12, 5)
assert tr.package_content_dict(Y)["A"] == Fraction(18, 5)

# 3) X、Y 均在甲处：5g 门槛命中；退回 X 后不再命中
tr.ship("S-X", X, "甲"); tr.ship("S-Y", Y, "甲")
p_both = tr.current_position()
assert [e.recipient for e in tr.contact_list("A", 5)] == ["甲"]   # 6g
tr.return_shipment("S-X", "甲")
assert tr.contact_list("A", 5) == ()

# 4) X 改发乙：当前乙 12/5、甲 18/5；旧时点仍找到甲当时的 6g
tr.ship("S-X2", X, "乙")
low = {e.recipient: e.exposure for e in tr.contact_list("A", 2)}
assert low == {"甲": Fraction(18, 5), "乙": Fraction(12, 5)}
assert tr.contact_list("A", 5, p_both)[0].exposure == Fraction(6)
```

> `demo.py` 场景 9 是可直接运行的完整版本。运行
> `python3 -m unittest tests.test_trace -v` 验证全部用例，其中包含
> 「失败后原包依旧可用」「并发双耗无残留」「关闭重开复现」等检查。

## 关键设计取舍

- **精确算术优先**：端点用 `Fraction`，搜索前按所有分母 LCM 放大为整数，
  全链路无浮点；压线可行/不可行判定可复现。
- **规划与目录解耦**：规划器只吃 `Lot` 列表，`evaluate_usage` 可对任意外部
  lot 集出产品；仓库通过 `preview_external` 消费外部质量产品。
- **SQLite 即锁即核**：不另建锁服务，`BEGIN IMMEDIATE` 拿库级写锁后在事务内
  读最新库存行复核，配合 `busy_timeout` 串行化确认。
- **台账不可变**：领料与退回各立 ledger 行，库存用
  `initial - allocated - issued + returned` 守恒口径推导，取消不抹历史。
- **剪枝只剪不可行支**：后缀质量包络是可达集的外包络，可能保守但绝不会
  剪掉可行解；正确性另由 `brute_force_best` 的 300 例随机对拍兜底。
