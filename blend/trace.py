"""实物包谱系、分装/混合/外发与定向召回。

本模块在现有履约仓之上增加**实物层**追踪，与原有订单/占用/领料台账
（``wh_*`` 表）解耦，但复用同一个 SQLite 连接与写锁事务：

* 每一次 :meth:`blend.warehouse.Warehouse.issue` 会产出一个**首次实物包**
  （来源 = 该 lot，贡献即领出的克数）；旧库里没有实物包信息的 ``ISSUE``
  台账会在初始化时增量补登记为 ``来源未细分``（``UNREFINED``）。这种包
  不能冒认任何已知根 lot 去向，也不进入"在外"累计；
* 追溯操作 ``operate/split/mix`` 有稳定的业务编号 ``op_id``。一次操作
  **整组耗用父包**并生成带正整数克数的子包：

  - ``1 父 -> N 子``：等比例分装，子包根 lot 组成与父包一致，贡献按
    ``子克数 / 父克数`` 精确缩放；
  - ``N 父 -> 1 子``：均匀混合，子包贡献 = 各父包贡献之和。

  同一操作不能既分又混；操作前后总克数必须相等。根 lot 贡献一律用
  :class:`fractions.Fraction` 精确表示（如 ``12/5``）；
* 幂等与并发：以 ``op_id`` 为幂等键。同编号同内容重放**读回原收据**，
  异文直接失败；两个独立连接并发耗用同一父包时，SQLite 写锁串行化，
  失败方不会留下任何子包。失败操作整体回滚，父包保持原状态可继续使用；
* 外发把**一个完整活动包**交给一个收件方（:meth:`PhysicalTrace.ship`），
  部分外发必须先分包。退回运输包按**当前持有者的发运记录**交回同一个
  完整包（:meth:`PhysicalTrace.return_shipment`），收回后可重发；它不
  改变 ``wh_lots`` 库存，因此不同于原有的 ``return_material`` 退料入库；
* 召回以**记录位置**（journal 序号）为时间点：当时仍在外的包、收件方、
  指定根 lot 的暴露克数及可追到首次领料的谱系路径都可复现；
  :meth:`PhysicalTrace.contact_list` 按收件方累计当时所持包的贡献，达到
  门槛才列入待联系清单。历史位置在关闭重开后仍能复现。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from typing import TYPE_CHECKING, Mapping, Sequence

from .model import StateError, ValidationError, as_frac

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from .warehouse import Warehouse

__all__ = [
    "PhysicalTrace",
    "PackageView",
    "PackageContent",
    "OpReceipt",
    "ShipmentView",
    "PathStep",
    "RecallExposure",
    "ContactEntry",
    "ST_IN_STOCK",
    "ST_OUT",
    "ST_RETURNED",
    "ST_CONSUMED",
    "ST_UNREFINED",
]

# ---------------------------------------------------------------- 包状态
ST_IN_STOCK = "IN_STOCK"      # 仓内活动包，可继续分装/混合/外发
ST_OUT = "OUT"                # 已外发，收件方持有
ST_RETURNED = "RETURNED"      # 运输包退回仓内，可再次外发/加工
ST_CONSUMED = "CONSUMED"      # 已被追溯操作整包耗用
ST_UNREFINED = "UNREFINED"    # 旧库存量包：来源未细分，不可冒认去向

_AVAILABLE_STATES = (ST_IN_STOCK, ST_RETURNED)

# 首次包来源
SRC_ISSUE = "ISSUE"
SRC_UNREFINED = "UNREFINED"

_PT_SCHEMA = """
CREATE TABLE IF NOT EXISTS pt_counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pt_packages (
    package_id  TEXT PRIMARY KEY,
    grams       INTEGER NOT NULL,
    state       TEXT NOT NULL,
    source      TEXT NOT NULL,
    order_id    TEXT,
    lot_id      TEXT,
    ledger_id   INTEGER,
    op_id       TEXT,
    created_pos INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS pt_pkg_ledger
    ON pt_packages(ledger_id) WHERE ledger_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pt_pkg_op ON pt_packages(op_id);
CREATE TABLE IF NOT EXISTS pt_content (
    package_id TEXT NOT NULL,
    root_lot   TEXT NOT NULL,
    num        INTEGER NOT NULL,
    den        INTEGER NOT NULL,
    PRIMARY KEY (package_id, root_lot)
);
CREATE TABLE IF NOT EXISTS pt_ops (
    op_id      TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    receipt    TEXT NOT NULL,
    pos        INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pt_op_parents (
    op_id      TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    package_id TEXT NOT NULL,
    grams      INTEGER NOT NULL,
    PRIMARY KEY (op_id, seq)
);
CREATE TABLE IF NOT EXISTS pt_op_children (
    op_id      TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    package_id TEXT NOT NULL,
    grams      INTEGER NOT NULL,
    PRIMARY KEY (op_id, seq)
);
CREATE TABLE IF NOT EXISTS pt_shipments (
    shipment_id TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    shipped_pos INTEGER NOT NULL,
    returned_pos INTEGER,
    shipped_at  TEXT NOT NULL,
    returned_at TEXT
);
CREATE INDEX IF NOT EXISTS pt_ship_pkg ON pt_shipments(package_id);
CREATE TABLE IF NOT EXISTS pt_pkg_state (
    package_id TEXT NOT NULL,
    pos        INTEGER NOT NULL,
    state      TEXT NOT NULL,
    PRIMARY KEY (package_id, pos)
);
CREATE TABLE IF NOT EXISTS pt_journal (
    pos  INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref  TEXT NOT NULL,
    at   TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- 只读视图
@dataclass(frozen=True)
class PackageContent:
    """根 lot 对某实物包的精确贡献（单位克，精确有理数）。"""

    root_lot: str
    grams: Fraction


@dataclass(frozen=True)
class PackageView:
    package_id: str
    grams: int
    state: str
    source: str
    order_id: str | None
    lot_id: str | None
    created_pos: int
    op_id: str | None
    content: tuple[PackageContent, ...]


@dataclass(frozen=True)
class ShipmentView:
    shipment_id: str
    package_id: str
    recipient: str
    shipped_pos: int
    returned_pos: int | None
    grams: int

    @property
    def status(self) -> str:
        return ST_OUT if self.returned_pos is None else ST_RETURNED


@dataclass(frozen=True)
class PathStep:
    """谱系路径上的一个节点（从首次领料包到当前包）。"""

    package_id: str
    grams: int
    op_id: str | None
    kind: str
    parent_id: str | None = None
    order_id: str | None = None
    lot_id: str | None = None


@dataclass(frozen=True)
class OpReceipt:
    """一次追溯操作的稳定收据（幂等重放返回同一对象内容）。"""

    op_id: str
    kind: str
    position: int
    parents: tuple[tuple[str, int], ...]
    children: tuple[tuple[str, int], ...]
    child_content: Mapping[str, Mapping[str, Fraction]]

    def content_of(self, package_id: str) -> Mapping[str, Fraction]:
        return self.child_content[package_id]


@dataclass(frozen=True)
class RecallExposure:
    """某记录位置仍在外的单个实物包及其根 lot 暴露。"""

    package_id: str
    recipient: str
    shipment_id: str
    grams: int
    position: int
    exposure: Fraction
    contributions: tuple[PackageContent, ...]
    routes: tuple[tuple[PathStep, ...], ...]


@dataclass(frozen=True)
class ContactEntry:
    """按收件方累计达到门槛的待联系条目。"""

    recipient: str
    exposure: Fraction
    package_ids: tuple[str, ...]
    shipment_ids: tuple[str, ...]
    position: int


# ---------------------------------------------------------------- 内部工具
def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon_parents(parents: Sequence[str]) -> tuple[str, ...]:
    """父包集合与顺序无关：排序后作为幂等键的一部分。"""

    return tuple(sorted(parents))


class PhysicalTrace:
    """实物包层：在既有 SQLite 履约库上增量启用。

    Parameters
    ----------
    warehouse:
        已打开的 :class:`~blend.warehouse.Warehouse`。两者共享同一连接，
        所有实物操作都在 ``BEGIN IMMEDIATE`` 事务内完成。
    sync_issues:
        构造时是否立即把历史 ``ISSUE`` 台账补登记成实物包（默认是）。
        构造前已存在的领料没有实物包信息，统一登记为
        :data:`ST_UNREFINED`（来源未细分）；构造之后的新领料登记为已知
        根 lot 的首次实物包。
    """

    def __init__(self, warehouse: "Warehouse", sync_issues: bool = True) -> None:
        self.warehouse = warehouse
        self._conn: sqlite3.Connection = warehouse._conn  # 共享连接
        self._conn.executescript(_PT_SCHEMA)
        self._conn.commit()
        # 记录启用水位：<= 水位的既有 ISSUE 只能算来源未细分
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT value FROM pt_counters WHERE name='enable_ledger_max'"
            ).fetchone()
            if row is None:
                max_id = self._conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM wh_ledger"
                ).fetchone()[0]
                self._conn.execute(
                    "INSERT INTO pt_counters(name, value) VALUES('enable_ledger_max', ?)",
                    (int(max_id),),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        # 新领料即时生成实物包（同事务），避免依赖查询时补扫
        if getattr(warehouse, "_trace_hook", None) not in (None, self._on_issue):
            raise ValidationError("该仓库已绑定另一个 PhysicalTrace")
        warehouse._trace_hook = self._on_issue
        if sync_issues:
            self.enable()

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        """解除仓库钩子（不关闭共享连接）。"""

        if getattr(self.warehouse, "_trace_hook", None) == self._on_issue:
            self.warehouse._trace_hook = None

    def __enter__(self) -> "PhysicalTrace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 增量启用
    def enable(self) -> int:
        """把尚无实物包的 ``ISSUE`` 台账补登记，返回新登记数量。

        水位之前的历史领料登记为来源未细分（``UNREFINED``，无根 lot 组成，
        不进入在外累计、不能外发）；水位之后的新领料可从台账精确还原
        lot 与克数，登记为首次实物包。
        """

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            cutoff = int(
                conn.execute(
                    "SELECT value FROM pt_counters WHERE name='enable_ledger_max'"
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT l.id, l.order_id, l.lot_id, l.grams, l.created_at"
                " FROM wh_ledger l WHERE l.event='ISSUE' AND NOT EXISTS"
                " (SELECT 1 FROM pt_packages p WHERE p.ledger_id=l.id)"
                " ORDER BY l.id"
            ).fetchall()
            for ledger_id, order_id, lot_id, grams, created_at in rows:
                if ledger_id <= cutoff:
                    source, state, content = SRC_UNREFINED, ST_UNREFINED, {}
                else:
                    source = SRC_ISSUE
                    state = ST_IN_STOCK
                    content = {lot_id: Fraction(grams)}
                pos = self._journal(conn, "ENABLE_ISSUE", f"ledger:{ledger_id}", created_at)
                self._insert_package(
                    conn,
                    package_id=self._issue_package_id(ledger_id),
                    grams=int(grams),
                    state=state,
                    source=source,
                    order_id=order_id,
                    lot_id=lot_id,
                    ledger_id=ledger_id,
                    op_id=None,
                    pos=pos,
                    content=content,
                )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise

    def _on_issue(
        self,
        conn: sqlite3.Connection,
        order_id: str,
        lot_id: str,
        grams: int,
        ledger_id: int,
        now: str,
    ) -> None:
        """``Warehouse.issue`` 事务内钩子：生成已知根 lot 的首次实物包。"""

        pos = self._journal(conn, "ISSUE_PACKAGE", f"ledger:{ledger_id}", now)
        self._insert_package(
            conn,
            package_id=self._issue_package_id(ledger_id),
            grams=int(grams),
            state=ST_IN_STOCK,
            source=SRC_ISSUE,
            order_id=order_id,
            lot_id=lot_id,
            ledger_id=ledger_id,
            op_id=None,
            pos=pos,
            content={lot_id: Fraction(grams)},
        )

    @staticmethod
    def _issue_package_id(ledger_id: int) -> str:
        return f"PKG-ISSUE-{int(ledger_id)}"

    # ------------------------------------------------------------ 追溯操作
    def operate(
        self,
        op_id: str,
        kind: str,
        parents: Sequence[str],
        children: Sequence[int],
    ) -> OpReceipt:
        """执行一次分装/混合，返回稳定收据（同编号同内容重放读回原收据）。

        * ``split``：恰好 1 个父包、至少 1 个子包；子包按
          ``子克数 / 父克数`` 等比例继承父包组成；
        * ``mix``：至少 2 个父包、恰好 1 个子包；子包组成 = 父包贡献之和。

        父包被**整包**耗用（取其全部克数），操作前后总克数必须相等。
        一次操作只能分或只能混，不能既分又混。
        """

        if not isinstance(op_id, str) or not op_id.strip():
            raise ValidationError("op_id 必须是非空字符串")
        if kind not in ("split", "mix"):
            raise ValidationError("kind 只能是 'split' 或 'mix'")
        parents = tuple(parents)
        children = tuple(children)
        if not parents:
            raise ValidationError("至少需要一个父包")
        if not children:
            raise ValidationError("至少需要一个子包")
        for g in children:
            self._positive_int(g, "子包克数")
        if len(set(parents)) != len(parents):
            raise ValidationError("同一操作中父包不能重复")

        if kind == "split":
            if len(parents) != 1:
                raise ValidationError("分装（split）必须恰好 1 个父包，不能同时混合")
        else:  # mix
            if len(children) != 1:
                raise ValidationError("混合（mix）必须恰好 1 个子包，不能同时分装")
            if len(parents) < 2:
                raise ValidationError("混合（mix）至少需要 2 个父包")

        payload = json.dumps(
            {
                "kind": kind,
                "parents": list(_canon_parents(parents)),
                "children": list(children),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT payload, receipt FROM pt_ops WHERE op_id=?", (op_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] != payload:
                    raise StateError(f"op_id {op_id!r} 已存在且内容不同，拒绝异文重放")
                conn.commit()
                return self._decode_receipt(existing[1])

            parent_rows = [self._package_row(conn, pid) for pid in _canon_parents(parents)]
            for p in parent_rows:
                if p["state"] not in _AVAILABLE_STATES:
                    raise StateError(
                        f"父包 {p['package_id']} 状态为 {p['state']}，不能被耗用"
                    )
                if p["source"] == SRC_UNREFINED:
                    raise StateError(
                        f"父包 {p['package_id']} 来源未细分，不能参与追溯操作"
                    )
                # 组成守恒自检
                if sum(p["content"].values(), Fraction(0)) != p["grams"]:
                    raise StateError(f"父包 {p['package_id']} 组成与克数不守恒")

            parent_grams = sum(p["grams"] for p in parent_rows)
            if sum(children) != parent_grams:
                raise ValidationError(
                    f"操作前后总克数不等：父包 {parent_grams} g，子包 {sum(children)} g"
                )

            if kind == "split":
                parent = parent_rows[0]
                child_contents = [
                    {
                        root: frac * Fraction(g, parent["grams"])
                        for root, frac in parent["content"].items()
                    }
                    for g in children
                ]
            else:
                mixed: dict[str, Fraction] = {}
                for p in parent_rows:
                    for root, frac in p["content"].items():
                        mixed[root] = mixed.get(root, Fraction(0)) + frac
                child_contents = [mixed]

            now = _utcnow()
            pos = self._journal(conn, "OPERATE", op_id, now)
            # 整包耗用父包
            for p in parent_rows:
                self._set_state(conn, p["package_id"], ST_CONSUMED, pos)
            # 生成子包
            child_ids = [f"{op_id}#c{i}" for i in range(len(children))]
            for cid, grams, content in zip(child_ids, children, child_contents):
                if sum(content.values(), Fraction(0)) != grams:
                    raise StateError("子包组成与克数不守恒")
                self._insert_package(
                    conn,
                    package_id=cid,
                    grams=grams,
                    state=ST_IN_STOCK,
                    source="TRACE",
                    order_id=None,
                    lot_id=None,
                    ledger_id=None,
                    op_id=op_id,
                    pos=pos,
                    content=content,
                )
            for seq, (cid, grams) in enumerate(zip(child_ids, children)):
                conn.execute(
                    "INSERT INTO pt_op_children(op_id, seq, package_id, grams)"
                    " VALUES (?,?,?,?)",
                    (op_id, seq, cid, grams),
                )
            for seq, p in enumerate(parent_rows):
                conn.execute(
                    "INSERT INTO pt_op_parents(op_id, seq, package_id, grams)"
                    " VALUES (?,?,?,?)",
                    (op_id, seq, p["package_id"], p["grams"]),
                )

            receipt = {
                "op_id": op_id,
                "kind": kind,
                "position": pos,
                "parents": [[p["package_id"], p["grams"]] for p in parent_rows],
                "children": [[cid, g] for cid, g in zip(child_ids, children)],
                "child_content": {
                    cid: {root: self._frac_str(f) for root, f in sorted(content.items())}
                    for cid, content in zip(child_ids, child_contents)
                },
            }
            conn.execute(
                "INSERT INTO pt_ops(op_id, kind, payload, receipt, pos, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (op_id, kind, payload, json.dumps(receipt, ensure_ascii=False,
                 sort_keys=True), pos, now),
            )
            conn.commit()
            return self._decode_receipt(json.dumps(receipt))
        except Exception:
            conn.rollback()
            raise

    def split(self, op_id: str, parent: str, children: Sequence[int]) -> OpReceipt:
        return self.operate(op_id, "split", [parent], children)

    def mix(self, op_id: str, parents: Sequence[str], grams: int) -> OpReceipt:
        return self.operate(op_id, "mix", list(parents), [grams])

    def get_op(self, op_id: str) -> OpReceipt:
        row = self._conn.execute(
            "SELECT receipt FROM pt_ops WHERE op_id=?", (op_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"op_id {op_id!r} 不存在")
        return self._decode_receipt(row[0])

    # ------------------------------------------------------------ 外发/退回
    def ship(self, shipment_id: str, package_id: str, recipient: str) -> ShipmentView:
        """把**一个完整活动包**交给一个收件方；部分外发请先 :meth:`split`。"""

        if not isinstance(shipment_id, str) or not shipment_id.strip():
            raise ValidationError("shipment_id 必须是非空字符串")
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValidationError("recipient 必须是非空字符串")

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._shipment_row(conn, shipment_id, required=False)
            if existing is not None:
                if existing.package_id != package_id or existing.recipient != recipient:
                    raise StateError(f"shipment_id {shipment_id!r} 已存在且内容不同")
                if existing.returned_pos is not None:
                    raise StateError(
                        f"发运 {shipment_id!r} 已退回，重发请使用新的 shipment_id"
                    )
                conn.commit()
                return existing
            pkg = self._package_row(conn, package_id)
            if pkg["state"] not in _AVAILABLE_STATES:
                raise StateError(
                    f"包 {package_id} 状态为 {pkg['state']}，不能外发（只有完整活动包可外发）"
                )
            if pkg["source"] == SRC_UNREFINED:
                raise StateError(f"包 {package_id} 来源未细分，不能冒认去向外发")
            now = _utcnow()
            pos = self._journal(conn, "SHIP", shipment_id, now)
            conn.execute(
                "INSERT INTO pt_shipments(shipment_id, package_id, recipient,"
                " shipped_pos, shipped_at) VALUES (?,?,?,?,?)",
                (shipment_id, package_id, recipient, pos, now),
            )
            self._set_state(conn, package_id, ST_OUT, pos)
            conn.commit()
            return ShipmentView(shipment_id, package_id, recipient, pos, None, pkg["grams"])
        except Exception:
            conn.rollback()
            raise

    def return_shipment(self, shipment_id: str, recipient: str | None = None) -> ShipmentView:
        """退回运输包：须对应当前持有者的发运记录，交回同一个完整包。

        退回只把该运输包收回仓内（可再次外发），**不**改变 ``wh_lots``
        库存，因此不同于 :meth:`Warehouse.return_material` 的退料入库。
        """

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            sh = self._shipment_row(conn, shipment_id)
            if sh.returned_pos is not None:
                raise StateError(f"发运 {shipment_id!r} 已退回，不能重复退回")
            if recipient is not None and recipient != sh.recipient:
                raise StateError(
                    f"退回方 {recipient!r} 不是当前持有者 {sh.recipient!r}"
                )
            pkg = self._package_row(conn, sh.package_id)
            if pkg["state"] != ST_OUT:
                raise StateError(
                    f"包 {sh.package_id} 当前状态 {pkg['state']}，不是在外的运输包"
                )
            now = _utcnow()
            pos = self._journal(conn, "RETURN_SHIPMENT", shipment_id, now)
            conn.execute(
                "UPDATE pt_shipments SET returned_pos=?, returned_at=? WHERE shipment_id=?",
                (pos, now, shipment_id),
            )
            self._set_state(conn, sh.package_id, ST_RETURNED, pos)
            conn.commit()
            return ShipmentView(
                sh.shipment_id, sh.package_id, sh.recipient,
                sh.shipped_pos, pos, pkg["grams"],
            )
        except Exception:
            conn.rollback()
            raise

    def get_shipment(self, shipment_id: str) -> ShipmentView:
        return self._shipment_row(self._conn, shipment_id)

    # ------------------------------------------------------------ 召回
    def recall(
        self,
        root_lot: str,
        position: int | None = None,
    ) -> tuple[RecallExposure, ...]:
        """查询指定记录位置仍在外的包及其对指定根 lot 的暴露。

        只统计**当时仍在外**（``shipped_pos <= P`` 且未在 P 前退回）的
        完整包；仓内、已退回、已耗用与来源未细分的包都不进入在外累计。
        """

        if not isinstance(root_lot, str) or not root_lot:
            raise ValidationError("root_lot 必须是非空字符串")
        P = self.current_position() if position is None else int(position)
        if P < 0:
            raise ValidationError("记录位置必须是非负整数")

        rows = self._conn.execute(
            "SELECT shipment_id, package_id, recipient, shipped_pos"
            " FROM pt_shipments WHERE shipped_pos <= ?"
            " AND (returned_pos IS NULL OR returned_pos > ?)"
            " ORDER BY shipped_pos, shipment_id",
            (P, P),
        ).fetchall()
        out: list[RecallExposure] = []
        for shipment_id, package_id, recipient, shipped_pos in rows:
            pkg = self._package_row(self._conn, package_id)
            if pkg["created_pos"] > P:
                continue
            content = pkg["content"]
            exposure = content.get(root_lot, Fraction(0))
            if exposure <= 0:
                continue
            routes = self._routes_for_root(package_id, root_lot)
            out.append(
                RecallExposure(
                    package_id=package_id,
                    recipient=recipient,
                    shipment_id=shipment_id,
                    grams=pkg["grams"],
                    position=P,
                    exposure=exposure,
                    contributions=tuple(
                        PackageContent(k, v) for k, v in sorted(content.items())
                    ),
                    routes=routes,
                )
            )
        return tuple(out)

    def contact_list(
        self,
        root_lot: str,
        min_grams: int | Fraction | str,
        position: int | None = None,
    ) -> tuple[ContactEntry, ...]:
        """按收件方累计当前所持包对 root_lot 的贡献，达到门槛才联系。

        门槛 ``min_grams`` 使用精确有理数比较，累计值 >= 门槛即列入。
        """

        threshold = as_frac(min_grams)
        if threshold < 0:
            raise ValidationError("最小暴露克数不能为负")
        P = self.current_position() if position is None else int(position)
        agg: dict[str, dict] = {}
        for e in self.recall(root_lot, P):
            slot = agg.setdefault(
                e.recipient, {"exposure": Fraction(0), "packages": [], "shipments": []}
            )
            slot["exposure"] += e.exposure
            slot["packages"].append(e.package_id)
            slot["shipments"].append(e.shipment_id)
        entries = [
            ContactEntry(
                recipient=recip,
                exposure=slot["exposure"],
                package_ids=tuple(sorted(slot["packages"])),
                shipment_ids=tuple(sorted(slot["shipments"])),
                position=P,
            )
            for recip, slot in agg.items()
            if slot["exposure"] >= threshold
        ]
        entries.sort(key=lambda e: (-e.exposure, e.recipient))
        return tuple(entries)

    # ------------------------------------------------------------ 查询
    def current_position(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(pos),0) FROM pt_journal").fetchone()
        return int(row[0])

    def position_of(self, ref: str) -> int:
        """把 op_id / shipment_id / package_id 映射到其记录位置。"""

        row = self._conn.execute("SELECT pos FROM pt_ops WHERE op_id=?", (ref,)).fetchone()
        if row is not None:
            return int(row[0])
        row = self._conn.execute(
            "SELECT shipped_pos FROM pt_shipments WHERE shipment_id=?", (ref,)
        ).fetchone()
        if row is not None:
            return int(row[0])
        row = self._conn.execute(
            "SELECT created_pos FROM pt_packages WHERE package_id=?", (ref,)
        ).fetchone()
        if row is not None:
            return int(row[0])
        raise ValidationError(f"无法定位记录位置: {ref!r}")

    def journal(self) -> list[tuple[int, str, str, str]]:
        """完整记录流 ``(pos, kind, ref, at)``，用于选择召回时间点。"""

        return list(
            self._conn.execute("SELECT pos, kind, ref, at FROM pt_journal ORDER BY pos")
        )

    def package(self, package_id: str, position: int | None = None) -> PackageView:
        row = self._package_row(self._conn, package_id)
        P = self.current_position() if position is None else int(position)
        state = self._state_at(self._conn, package_id, P)
        return self._to_view(row, state or "NOT_YET", P)

    def packages(
        self,
        state: str | None = None,
        position: int | None = None,
    ) -> tuple[PackageView, ...]:
        """按位置与状态列出包（仓内/在外/已退回/已耗用/未细分均可查）。"""

        P = self.current_position() if position is None else int(position)
        rows = self._conn.execute(
            "SELECT package_id FROM pt_packages WHERE created_pos <= ? ORDER BY package_id",
            (P,),
        ).fetchall()
        views: list[PackageView] = []
        for (pid,) in rows:
            st = self._state_at(self._conn, pid, P)
            if st is None:
                continue
            if state is not None and st != state:
                continue
            views.append(self._to_view(self._package_row(self._conn, pid), st, P))
        return tuple(views)

    def package_content(self, package_id: str) -> tuple[PackageContent, ...]:
        row = self._package_row(self._conn, package_id)
        return tuple(PackageContent(k, v) for k, v in sorted(row["content"].items()))

    def package_content_dict(self, package_id: str) -> dict[str, Fraction]:
        """根 lot -> 精确贡献克数（便于断言与展示）。"""

        return {c.root_lot: c.grams for c in self.package_content(package_id)}

    def routes_for(
        self, package_id: str, root_lot: str
    ) -> tuple[tuple[PathStep, ...], ...]:
        """公共入口：包到指定根 lot 首次领料的全部谱系路径。"""

        return self._routes_for_root(package_id, root_lot)

    def explanation(self, package_id: str) -> str:
        """人读解释：当前状态 + 根 lot 组成 + 到首次领料的路径。"""

        view = self.package(package_id)
        lines = [
            f"包 {view.package_id}: {view.grams} g，状态 {view.state}，来源 {view.source}",
        ]
        if view.order_id:
            lines.append(f"  首次领料: 订单 {view.order_id} lot {view.lot_id}")
        if not view.content:
            lines.append("  组成: 来源未细分（无已知根 lot 贡献）")
        else:
            lines.append("  组成:")
            for c in view.content:
                lines.append(f"    - {c.root_lot}: {self._frac_str(c.grams)} g")
        for root in sorted({c.root_lot for c in view.content}):
            lines.append(f"  到 {root} 首次领料的路径:")
            for route in self._routes_for_root(package_id, root):
                chain = " -> ".join(
                    f"{s.package_id}({s.grams}g/{s.kind})" for s in route
                )
                lines.append(f"    * {chain}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 内部存取
    @staticmethod
    def _positive_int(grams, what: str) -> None:
        if not isinstance(grams, int) or grams <= 0:
            raise ValidationError(f"{what}必须是正整数")

    @staticmethod
    def _frac_str(x: Fraction) -> str:
        return f"{x.numerator}/{x.denominator}"

    def _journal(self, conn, kind: str, ref: str, at: str | None = None) -> int:
        cur = conn.execute(
            "INSERT INTO pt_journal(kind, ref, at) VALUES (?,?,?)",
            (kind, ref, at or _utcnow()),
        )
        return int(cur.lastrowid)

    def _insert_package(
        self,
        conn,
        *,
        package_id: str,
        grams: int,
        state: str,
        source: str,
        order_id: str | None,
        lot_id: str | None,
        ledger_id: int | None,
        op_id: str | None,
        pos: int,
        content: Mapping[str, Fraction],
    ) -> None:
        conn.execute(
            "INSERT INTO pt_packages(package_id, grams, state, source, order_id,"
            " lot_id, ledger_id, op_id, created_pos) VALUES (?,?,?,?,?,?,?,?,?)",
            (package_id, grams, state, source, order_id, lot_id, ledger_id, op_id, pos),
        )
        for root, frac in sorted(content.items()):
            conn.execute(
                "INSERT INTO pt_content(package_id, root_lot, num, den) VALUES (?,?,?,?)",
                (package_id, root, frac.numerator, frac.denominator),
            )
        conn.execute(
            "INSERT INTO pt_pkg_state(package_id, pos, state) VALUES (?,?,?)",
            (package_id, pos, state),
        )

    def _set_state(self, conn, package_id: str, state: str, pos: int) -> None:
        conn.execute(
            "INSERT INTO pt_pkg_state(package_id, pos, state) VALUES (?,?,?)",
            (package_id, pos, state),
        )
        conn.execute("UPDATE pt_packages SET state=? WHERE package_id=?", (state, package_id))

    def _package_row(self, conn, package_id: str) -> dict:
        row = conn.execute(
            "SELECT package_id, grams, state, source, order_id, lot_id, ledger_id,"
            " op_id, created_pos FROM pt_packages WHERE package_id=?",
            (package_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"实物包 {package_id!r} 不存在")
        content: dict[str, Fraction] = {}
        for root, num, den in conn.execute(
            "SELECT root_lot, num, den FROM pt_content WHERE package_id=?", (package_id,)
        ).fetchall():
            content[root] = Fraction(num, den)
        return {
            "package_id": row[0],
            "grams": row[1],
            "state": row[2],
            "source": row[3],
            "order_id": row[4],
            "lot_id": row[5],
            "ledger_id": row[6],
            "op_id": row[7],
            "created_pos": row[8],
            "content": content,
        }

    def _shipment_row(self, conn, shipment_id: str, required: bool = True) -> ShipmentView | None:
        row = conn.execute(
            "SELECT shipment_id, package_id, recipient, shipped_pos, returned_pos"
            " FROM pt_shipments WHERE shipment_id=?",
            (shipment_id,),
        ).fetchone()
        if row is None:
            if required:
                raise ValidationError(f"发运记录 {shipment_id!r} 不存在")
            return None
        grams = conn.execute(
            "SELECT grams FROM pt_packages WHERE package_id=?", (row[1],)
        ).fetchone()[0]
        return ShipmentView(row[0], row[1], row[2], row[3], row[4], int(grams))

    @staticmethod
    def _state_at(conn, package_id: str, pos: int) -> str | None:
        row = conn.execute(
            "SELECT state FROM pt_pkg_state WHERE package_id=? AND pos <= ?"
            " ORDER BY pos DESC LIMIT 1",
            (package_id, pos),
        ).fetchone()
        return None if row is None else row[0]

    def _to_view(self, row: dict, state: str, pos: int) -> PackageView:
        return PackageView(
            package_id=row["package_id"],
            grams=row["grams"],
            state=state,
            source=row["source"],
            order_id=row["order_id"],
            lot_id=row["lot_id"],
            created_pos=row["created_pos"],
            op_id=row["op_id"],
            content=tuple(PackageContent(k, v) for k, v in sorted(row["content"].items())),
        )

    def _decode_receipt(self, blob: str) -> OpReceipt:
        data = json.loads(blob)
        return OpReceipt(
            op_id=data["op_id"],
            kind=data["kind"],
            position=int(data["position"]),
            parents=tuple((p, int(g)) for p, g in data["parents"]),
            children=tuple((c, int(g)) for c, g in data["children"]),
            child_content={
                cid: {root: Fraction(v) for root, v in content.items()}
                for cid, content in data["child_content"].items()
            },
        )

    # ------------------------------------------------------------ 谱系路径
    def _routes_for_root(
        self, package_id: str, root_lot: str
    ) -> tuple[tuple[PathStep, ...], ...]:
        """包到指定根 lot 的首次领料包的全部谱系路径（自底向上回溯）。"""

        memo: dict[str, tuple[tuple[PathStep, ...], ...]] = {}

        def dfs(pid: str) -> tuple[tuple[PathStep, ...], ...]:
            if pid in memo:
                return memo[pid]
            row = self._package_row(self._conn, pid)
            if row["source"] == SRC_UNREFINED:
                memo[pid] = ()
                return ()
            if row["op_id"] is None:
                if row["lot_id"] == root_lot:
                    step = PathStep(
                        package_id=pid,
                        grams=row["grams"],
                        op_id=None,
                        kind="ISSUE",
                        parent_id=None,
                        order_id=row["order_id"],
                        lot_id=row["lot_id"],
                    )
                    memo[pid] = ((step,),)
                else:
                    memo[pid] = ()
                return memo[pid]
            op = self._conn.execute(
                "SELECT kind FROM pt_ops WHERE op_id=?", (row["op_id"],)
            ).fetchone()
            kind = op[0] if op else "?"
            parents = self._conn.execute(
                "SELECT package_id FROM pt_op_parents WHERE op_id=? ORDER BY seq",
                (row["op_id"],),
            ).fetchall()
            routes: list[tuple[PathStep, ...]] = []
            for (parent_id,) in parents:
                for sub in dfs(parent_id):
                    step = PathStep(
                        package_id=pid,
                        grams=row["grams"],
                        op_id=row["op_id"],
                        kind=kind,
                        parent_id=parent_id,
                        order_id=None,
                        lot_id=None,
                    )
                    routes.append(sub + (step,))
            memo[pid] = tuple(routes)
            return memo[pid]

        return dfs(package_id)

