"""实物包谱系与定向召回测试。

覆盖题面给出的完整例子：A 6g + B 4g = 10g，分 X 4g / Y 6g，A 贡献
分别为 12/5 与 18/5；甲同时持有 X、Y 时 5g 门槛命中，退回 X 后不再
命中；X 改发乙后乙暴露 12/5，旧时点仍能找到甲当时的 6g。另含幂等重放、
异文失败、并发双耗无残留、旧库来源未细分、关闭重开后历史复现。
"""

import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path

from blend.catalog import QualityCatalog
from blend.model import PlanResult, PlannedLot, StateError, ValidationError
from blend.planner import evaluate_usage
from blend.trace import (
    ST_CONSUMED,
    ST_IN_STOCK,
    ST_OUT,
    ST_RETURNED,
    ST_UNREFINED,
    PhysicalTrace,
)
from blend.warehouse import Warehouse
from tests.factories import make_lot, make_order

F = Fraction


class TraceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "wh.db")
        self.cat_path = str(Path(self.tmp.name) / "cat.db")
        self.cat = QualityCatalog(self.cat_path)
        self.lots = [
            make_lot("A", 500, {"c1": ("0", "1")}, cost_per_g=1),
            make_lot("B", 500, {"c1": ("0", "1")}, cost_per_g=1),
        ]
        self.cat.replace_lots(self.lots)
        self.wh = Warehouse(self.db_path, self.cat)
        self.wh.register_lots(self.lots)
        self.tr = PhysicalTrace(self.wh)

    def tearDown(self):
        self.tr.close()
        self.wh.close()
        self.cat.close()
        self.tmp.cleanup()

    def _issue(self, order_id: str, usage: dict) -> dict:
        """按外部质量产品确认订单并逐 lot 领料，返回 lot -> 首次包 id。"""
        total = sum(usage.values())
        product = evaluate_usage(self.lots, usage, source="manual")
        planned = tuple(
            PlannedLot(l.lot_id, usage.get(l.lot_id, 0), l.step_g, l.cost_per_g, l.fixed_fee)
            for l in self.lots
        )
        order = make_order(order_id, total, {"c1": ("0", "1")}, max_lots=len(self.lots))
        pv = self.wh.preview_external(
            order, PlanResult(order_id, total, planned, product, 0, len(usage), "manual")
        )
        self.wh.confirm_preview(pv)
        for lot_id, grams in usage.items():
            self.wh.issue(order_id, lot_id, grams)
        return {
            p.lot_id: p.package_id
            for p in self.tr.packages()
            if p.order_id == order_id and p.state == ST_IN_STOCK
        }

    def _setup_XY(self):
        pkgs = self._issue("O1", {"A": 6, "B": 4})
        mixed = self.tr.mix("MIX", [pkgs["A"], pkgs["B"]], 10)
        split = self.tr.split("SPLIT", mixed.children[0][0], [4, 6])
        x = split.children[0][0]
        y = split.children[1][0]
        return x, y


class LineageMathTest(TraceBase):
    def test_split_and_mix_fractional_contributions(self):
        x, y = self._setup_XY()
        self.assertEqual(dict(self.tr.package_content_dict(x)), {"A": F(12, 5), "B": F(8, 5)})
        self.assertEqual(dict(self.tr.package_content_dict(y)), {"A": F(18, 5), "B": F(12, 5)})
        # 精确有理数，不产生浮点
        for content in (self.tr.package_content(x), self.tr.package_content(y)):
            for c in content:
                self.assertIsInstance(c.grams, Fraction)

    def test_conservation_and_shapes(self):
        pkgs = self._issue("O1", {"A": 6, "B": 4})
        mix = self.tr.mix("MIX", [pkgs["A"], pkgs["B"]], 10)
        # mix 不能多子包，split 不能多父包
        with self.assertRaises(ValidationError):
            self.tr.operate("BAD-MIX", "mix", [pkgs["A"]], [5, 5])
        with self.assertRaises(ValidationError):
            self.tr.operate("BAD-SPLIT", "split", [pkgs["A"], pkgs["B"]], [10])
        # 总克数不守恒
        with self.assertRaises(ValidationError):
            self.tr.split("BAD-SUM", mix.children[0][0], [3, 6])
        # 失败后父包仍可用
        self.assertEqual(self.tr.package(mix.children[0][0]).state, ST_IN_STOCK)

    def test_partial_external_requires_split(self):
        x, y = self._setup_XY()
        # X 完整外发
        self.tr.ship("S1", x, "甲")
        # 已在外包不能再外发
        with self.assertRaises(StateError):
            self.tr.ship("S2", x, "乙")


class RecallTest(TraceBase):
    def test_five_gram_threshold_and_return(self):
        x, y = self._setup_XY()
        self.tr.ship("S1", x, "甲")
        self.tr.ship("S2", y, "甲")
        entries = self.tr.contact_list("A", 5)
        self.assertEqual([e.recipient for e in entries], ["甲"])
        self.assertEqual(entries[0].exposure, F(6))  # 12/5 + 18/5

        self.tr.return_shipment("S1", "甲")
        self.assertEqual(self.tr.contact_list("A", 5), ())

    def test_resend_and_historical_position(self):
        x, y = self._setup_XY()
        self.tr.ship("S1", x, "甲")
        self.tr.ship("S2", y, "甲")
        p_both = self.tr.current_position()
        self.tr.return_shipment("S1", "甲")
        self.tr.ship("S3", x, "乙")

        # 当前甲仅剩 Y（18/5=3.6 < 5），乙仅 12/5=2.4 < 5，均不到门槛
        self.assertEqual(self.tr.contact_list("A", 5), ())
        # 旧时点：甲当时持有 X+Y = 6g，达到 5g
        old = {e.recipient: e.exposure for e in self.tr.contact_list("A", 5, p_both)}
        self.assertEqual(old, {"甲": F(6)})
        # 2g 门槛下，当前甲 18/5、乙 12/5 都列入
        low = {e.recipient: e.exposure for e in self.tr.contact_list("A", 2)}
        self.assertEqual(low, {"甲": F(18, 5), "乙": F(12, 5)})

    def test_internal_and_consumed_excluded_from_recall(self):
        pkgs = self._issue("O1", {"A": 6, "B": 4})
        # 尚未外发：仓内包不在召回
        self.assertEqual(self.tr.recall("A"), ())
        mix = self.tr.mix("MIX", [pkgs["A"], pkgs["B"]], 10)
        # 已耗用父包不再在外；中间包可单独查询
        self.assertEqual(self.tr.package(pkgs["A"]).state, ST_CONSUMED)
        self.assertEqual(self.tr.recall("A"), ())
        self.assertGreater(self.tr.package(mix.children[0][0]).content[0].grams, 0)

    def test_different_paths_reconverge_preserve_all_contributions(self):
        """同根 lot 经不同路径汇合，贡献相加而非去重。"""
        pkgs2 = self._issue("O2", {"A": 6, "B": 4})
        x, y = self._setup_XY()
        # X 与 O2 的 A 包混合成一份，A 贡献须为 12/5 + 6
        merged = self.tr.mix("MERGE", [x, pkgs2["A"]], 10)
        merged_id = merged.children[0][0]
        content = dict(self.tr.package_content_dict(merged_id))
        self.assertEqual(content["A"], F(12, 5) + 6)
        # 路径可追到两条不同订单的首次领料
        routes = self.tr.routes_for(merged_id, "A")
        self.assertEqual(len(routes), 2)
        order_ids = {route[0].order_id for route in routes}
        self.assertEqual(order_ids, {"O1", "O2"})


class IdempotencyAndConcurrencyTest(TraceBase):
    def test_replay_same_op_returns_same_receipt(self):
        pkgs = self._issue("O1", {"A": 6, "B": 4})
        r1 = self.tr.mix("MIX", [pkgs["A"], pkgs["B"]], 10)
        r2 = self.tr.mix("MIX", [pkgs["B"], pkgs["A"]], 10)  # 父包顺序无关
        self.assertEqual(r1, r2)
        # 异文失败且不新增任何包
        before = len(self.tr.packages())
        with self.assertRaises(StateError):
            self.tr.mix("MIX", [pkgs["A"], pkgs["B"]], 9)
        self.assertEqual(len(self.tr.packages()), before)

    def test_double_consume_fails_without_children(self):
        pkgs = self._issue("O1", {"A": 6, "B": 4})
        self.tr.mix("MIX", [pkgs["A"], pkgs["B"]], 10)
        before = len(self.tr.packages())
        with self.assertRaises(StateError):
            self.tr.split("AGAIN", pkgs["A"], [6])  # 父包已耗用
        self.assertEqual(len(self.tr.packages()), before)
        self.assertEqual(self.tr.package(pkgs["A"]).state, ST_CONSUMED)


class ConcurrencyDoubleSpendTest(unittest.TestCase):
    def test_concurrent_consume_one_wins(self):
        tmp = tempfile.TemporaryDirectory()
        db = str(Path(tmp.name) / "wh.db")
        cp = str(Path(tmp.name) / "cat.db")
        try:
            cat = QualityCatalog(cp)
            lots = [make_lot("A", 100, {"c1": ("0", "1")})]
            cat.replace_lots(lots)
            wh = Warehouse(db, cat)
            wh.register_lots(lots)
            tr = PhysicalTrace(wh)
            product = evaluate_usage(lots, {"A": 10})
            order = make_order("O1", 10, {"c1": ("0", "1")})
            pv = wh.preview_external(
                order,
                PlanResult("O1", 10, (PlannedLot("A", 10, 1, 0, 0),), product, 0, 1, "m"),
            )
            wh.confirm_preview(pv)
            wh.issue("O1", "A", 10)
            parent = next(p.package_id for p in tr.packages() if p.state == ST_IN_STOCK)
            tr.close(); wh.close(); cat.close()

            barrier = threading.Barrier(2)
            outcomes = []

            def worker(op_id, tag):
                c = QualityCatalog(cp)
                w = Warehouse(db, c)
                t = PhysicalTrace(w)
                try:
                    barrier.wait()
                    t.split(op_id, parent, [5, 5])
                    outcomes.append("ok")
                except StateError:
                    outcomes.append("blocked")
                finally:
                    w.close(); c.close()

            threads = [
                threading.Thread(target=worker, args=(f"OP-{tag}", tag)) for tag in "AB"
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(outcomes), ["blocked", "ok"])

            audit_w = Warehouse(db, QualityCatalog(cp))
            audit = PhysicalTrace(audit_w)
            op_children = [p for p in audit.packages() if p.op_id is not None]
            self.assertEqual(len(op_children), 2)  # 只有一个操作的子包
            self.assertEqual(audit.package(parent).state, ST_CONSUMED)
            audit.close(); audit_w.close()
        finally:
            tmp.cleanup()


class MigrationTest(unittest.TestCase):
    def test_legacy_issues_marked_unrefined(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = str(Path(tmp.name) / "wh.db")
            cp = str(Path(tmp.name) / "cat.db")
            cat = QualityCatalog(cp)
            lots = [make_lot("A", 100, {"c1": ("0", "1")})]
            cat.replace_lots(lots)
            wh = Warehouse(db, cat)
            wh.register_lots(lots)
            # 先领料，后启用实物层
            product = evaluate_usage(lots, {"A": 6})
            order = make_order("O1", 6, {"c1": ("0", "1")})
            pv = wh.preview_external(
                order, PlanResult("O1", 6, (PlannedLot("A", 6, 1, 0, 0),), product, 0, 1, "m")
            )
            wh.confirm_preview(pv)
            wh.issue("O1", "A", 6)

            tr = PhysicalTrace(wh)
            pkgs = tr.packages()
            self.assertEqual(len(pkgs), 1)
            self.assertEqual(pkgs[0].state, ST_UNREFINED)
            self.assertEqual(pkgs[0].content, ())
            self.assertEqual(tr.recall("A"), ())
            # 来源未细分不可加工、不可外发
            with self.assertRaises(StateError):
                tr.split("X", pkgs[0].package_id, [6])
            with self.assertRaises(StateError):
                tr.ship("S", pkgs[0].package_id, "甲")
            tr.close(); wh.close(); cat.close()
        finally:
            tmp.cleanup()


class PersistenceTest(unittest.TestCase):
    def test_history_reproducible_after_reopen(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = str(Path(tmp.name) / "wh.db")
            cp = str(Path(tmp.name) / "cat.db")
            cat = QualityCatalog(cp)
            lots = [
                make_lot("A", 100, {"c1": ("0", "1")}),
                make_lot("B", 100, {"c1": ("0", "1")}),
            ]
            cat.replace_lots(lots)
            wh = Warehouse(db, cat)
            wh.register_lots(lots)
            tr = PhysicalTrace(wh)
            product = evaluate_usage(lots, {"A": 6, "B": 4})
            order = make_order("O1", 10, {"c1": ("0", "1")})
            planned = (PlannedLot("A", 6, 1, 0, 0), PlannedLot("B", 4, 1, 0, 0))
            pv = wh.preview_external(
                order, PlanResult("O1", 10, planned, product, 0, 2, "m")
            )
            wh.confirm_preview(pv)
            wh.issue("O1", "A", 6)
            wh.issue("O1", "B", 4)
            pkgs = {p.lot_id: p.package_id for p in tr.packages() if p.state == ST_IN_STOCK}
            mixed = tr.mix("MIX", [pkgs["A"], pkgs["B"]], 10)
            split = tr.split("SPLIT", mixed.children[0][0], [4, 6])
            x, y = split.children[0][0], split.children[1][0]
            tr.ship("S1", x, "甲")
            tr.ship("S2", y, "甲")
            p_snapshot = tr.current_position()
            expected = [(e.recipient, str(e.exposure)) for e in tr.contact_list("A", 5)]
            tr.close(); wh.close(); cat.close()

            # 关闭重开：历史位置仍可复现同样的待联系清单
            wh2 = Warehouse(db, QualityCatalog(cp))
            tr2 = PhysicalTrace(wh2)
            self.assertEqual(tr2.current_position(), p_snapshot)
            got = [(e.recipient, str(e.exposure)) for e in tr2.contact_list("A", 5)]
            self.assertEqual(got, expected)
            # 再次重放同一操作仍读回原收据
            replay = tr2.mix("MIX", [pkgs["A"], pkgs["B"]], 10)
            self.assertEqual(replay.position, mixed.position)
            # 在 reopen 之后继续推进位置，旧清单不受影响
            tr2.return_shipment("S1", "甲")
            self.assertEqual(tr2.contact_list("A", 5), ())
            still_old = [
                (e.recipient, str(e.exposure)) for e in tr2.contact_list("A", 5, p_snapshot)
            ]
            self.assertEqual(still_old, expected)
            tr2.close(); wh2.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
