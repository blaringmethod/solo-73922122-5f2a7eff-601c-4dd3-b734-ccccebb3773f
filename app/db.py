"""
连续合约结算价 —— 数据访问与计算层

核心模型
========
合约序列：初始合约 ord=0；第 k 次换月启用的合约 ord=k（k>=1），换月记录在 rolls(ord=k)。
某日 d 在用合约 = ord 满足：roll ord r 的 switch_date > d 的最小 r（即 roll r-1 已生效、r 未生效），
等价于 ord = count(rolls.switch_date <= d)，该 ord 即 rolls 中严格大于 d 的最小 ord 再减 1。

连续价（基准日 B）
==================
cont(d, B) = price(contract(d), d) + Σ { delta(r) | d < switch_date(r) <= B }
其中 delta(r) = price(new_r, pre_date_r) - price(old_r, pre_date_r)，
pre_date_r 为切换前一交易日；任一所需报价缺失则 delta 为 NULL。
缺价时连续价留空，并指出：
  - 当天原价缺失；或
  - 区间内最早一个 delta 为空的换月，以及该换月待补的合约报价。

复杂度保证
==========
- 换月价差以 Fenwick 树（bit_sums 表）维护：区间求和、单点更新均为 O(log n)，
  辅助记录数 ≤ n+1（n=换月次数），随换月次数对数级增长。
- 连续价不落地：单笔报价更新只写 prices 与至多一个 delta + Fenwick 单点，
  绝不逐日回写连续价。
- 单次取数在一个只读快照事务内完成（同一已提交状态），逐日用对数级查询。
- 查价区间缓存（range_cache，单槽，网页与 CSV 共用）：
  命中时不读 prices 逐日原价，整次取数读取的换月及索引记录数至多 O(log n)
  （2 次切换日索引定位、1 次部分索引 LIMIT 1、Fenwick 两个前缀和）；
  缓存行随区间天数线性增长，但那是缓存自身数据、不是逐日原价重读。
- 缓存失效只认 change_log 版本：区间内交易日的报价更正、日历/计划变更才作废；
  仅联动区间末日之后换月的补改报价不失效，命中时尾部按最新价差现算。

事务语义
========
- 报价更新（新增/更正）在单个 IMMEDIATE 事务内提交：原价、受影响价差、变更版本
  一起生效，不会出现“价已改、价差未改”的中间状态。
- 保存失败时事务整体回滚，调用方仍可拿到本次计算的预期结果用于页面展示。
- 缓存发布在独立写事务内复核版本：构建期间若有新变更提交则放弃发布，
  晚算完的旧缓存不会供新请求使用。
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from contextlib import contextmanager
from datetime import date as date_cls, datetime
from typing import Optional


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

ISO_FMT = "%Y-%m-%d"


def iso(d: date_cls) -> str:
    return d.strftime(ISO_FMT)


def parse_iso(s: str) -> date_cls:
    return datetime.strptime(s, ISO_FMT).date()


def valid_iso(s: str) -> bool:
    try:
        parse_iso(s)
        return True
    except (ValueError, TypeError):
        return False


class AppError(Exception):
    """面向用户的业务错误（中文消息可直接展示）。"""


# --------------------------------------------------------------------------- #
# 数据库连接
# --------------------------------------------------------------------------- #

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_db_path() -> str:
    return os.environ.get("CONT_DB_PATH", os.path.join(os.getcwd(), "data", "continuous.db"))


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())


@contextmanager
def snapshot(conn: sqlite3.Connection):
    """
    只读快照事务：整次取数只看到同一个已提交状态。
    SQLite 默认 DEFERRED 事务的首条 SELECT 即获取读快照，事务结束前不变。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """立即写事务：保证“原价 + 受影响价差 + 变更版本”原子提交，失败整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------- #
# 变更版本日志：缓存一致性的唯一依据
# --------------------------------------------------------------------------- #
# 每条写事务在同一 IMMEDIATE 事务内追加一条 change_log（ver 自增），
# 缓存发布时记下当时的 ver；命中前检查 (built_ver, 现在] 是否存在影响本区间的变更。
# 这样“建缓存期间行情员补改报价”时，旧缓存要么带着更新的 ver 发布（含新数据），
# 要么被随后提交的变更标记为过期——晚算完的旧缓存不可能供新请求使用。

def current_ver(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(ver), 0) AS v FROM change_log").fetchone()
    return row["v"]


def _record_change(conn: sqlite3.Connection, scope: str, reason: str,
                   change_day: Optional[str] = None) -> int:
    """在当前写事务内登记一条变更并返回新版本号（须已在 write_tx 中）。"""
    cur = conn.execute(
        "INSERT INTO change_log(scope, change_day, reason) VALUES (?, ?, ?)",
        (scope, change_day, reason),
    )
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# 基础配置维护：交易日历 / 合约 / 换月计划
# --------------------------------------------------------------------------- #

def list_calendar(conn: sqlite3.Connection) -> list[str]:
    return [r["date"] for r in conn.execute("SELECT date FROM calendar ORDER BY date")]


def replace_calendar(conn: sqlite3.Connection, dates: list[str]) -> int:
    """全量替换交易日历（去重、校验格式、排序）。"""
    cleaned = sorted({d.strip() for d in dates if d and d.strip()})
    bad = [d for d in cleaned if not valid_iso(d)]
    if bad:
        raise AppError(f"交易日日期格式非法：{bad[0]}，应为 YYYY-MM-DD")
    with write_tx(conn):
        conn.execute("DELETE FROM calendar")
        conn.executemany("INSERT INTO calendar(date) VALUES (?)", [(d,) for d in cleaned])
        # 日历变更后取数口径可能整体变化，全部缓存作废
        _record_change(conn, "config", "replace_calendar")
    return len(cleaned)


def prev_trade_date(conn: sqlite3.Connection, day: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM calendar WHERE date < ?", (day,)
    ).fetchone()
    return row["d"] if row and row["d"] else None


def is_trade_date(conn: sqlite3.Connection, day: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM calendar WHERE date = ?", (day,)
    ).fetchone() is not None


def list_contracts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT c.contract_id, c.code, c.ord, "
        "  (SELECT COUNT(*) FROM prices p WHERE p.contract_id = c.contract_id) AS price_count "
        "FROM contracts c ORDER BY c.ord"
    ))


def get_contract(conn: sqlite3.Connection, contract_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE contract_id = ?", (contract_id,)
    ).fetchone()


def contract_by_ord(conn: sqlite3.Connection, ord_: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE ord = ?", (ord_,)
    ).fetchone()


def list_rolls(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT r.ord, r.switch_date, r.pre_date, r.new_contract_id, r.delta, "
        "       nc.code AS new_code, oc.code AS old_code "
        "FROM rolls r "
        "JOIN contracts nc ON nc.contract_id = r.new_contract_id "
        "JOIN contracts oc ON oc.ord = r.ord - 1 "
        "ORDER BY r.ord"
    ))


def setup_plan(conn: sqlite3.Connection, initial_code: str,
               switches: list[tuple[str, str]]) -> None:
    """
    （重新）登记初始合约与固定换月计划。
    switches: 按时间顺序的 [(switch_date, new_contract_code), ...]
    仅允许在尚无任何报价/价差数据时整体重建，避免历史数据错配。
    """
    initial_code = (initial_code or "").strip()
    if not initial_code:
        raise AppError("初始合约代码不能为空")
    plan: list[tuple[str, str]] = []
    seen_dates: set[str] = set()
    prev = None
    for sd, code in switches:
        sd = (sd or "").strip()
        code = (code or "").strip()
        if not sd or not code:
            continue
        if not valid_iso(sd):
            raise AppError(f"换月切换日格式非法：{sd}")
        if sd in seen_dates:
            raise AppError(f"换月切换日重复：{sd}")
        if prev is not None and sd <= prev:
            raise AppError(f"换月切换日必须严格递增：{prev} 之后不能是 {sd}")
        if not is_trade_date(conn, sd):
            raise AppError(f"换月切换日 {sd} 不在交易日历中")
        seen_dates.add(sd)
        plan.append((sd, code))
        prev = sd

    with write_tx(conn):
        n_prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        n_rolls = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
        if n_prices or n_rolls:
            raise AppError("已存在报价或换月数据，不能重建计划；如需调整请先清空数据")
        conn.execute("DELETE FROM rolls")
        conn.execute("DELETE FROM contracts")
        conn.execute("DELETE FROM bit_sums")
        conn.execute("DELETE FROM range_cache_rows")
        conn.execute("DELETE FROM range_cache")

        cur = conn.execute(
            "INSERT INTO contracts(code, ord) VALUES (?, 0)", (initial_code,)
        )
        init_id = cur.lastrowid
        conn.execute("UPDATE meta SET initial_contract_id = ? WHERE id = 1", (init_id,))
        for k, (sd, code) in enumerate(plan, start=1):
            cur = conn.execute(
                "INSERT INTO contracts(code, ord) VALUES (?, ?)", (code, k)
            )
            new_id = cur.lastrowid
            pre = prev_trade_date(conn, sd)
            if pre is None:
                raise AppError(f"切换日 {sd} 之前没有交易日，无法取换月价差")
            conn.execute(
                "INSERT INTO rolls(ord, switch_date, pre_date, new_contract_id, delta) "
                "VALUES (?, ?, ?, ?, NULL)",
                (k, sd, pre, new_id),
            )
        # 计划重建后合约/换月口径变化，登记 config 变更使任何残留缓存作废
        _record_change(conn, "config", "setup_plan")


# --------------------------------------------------------------------------- #
# Fenwick 树：换月价差的对数级前缀和
# --------------------------------------------------------------------------- #

def _bit_add(conn: sqlite3.Connection, n: int, pos: int, v: int) -> None:
    i = pos
    while i <= n:
        row = conn.execute("SELECT val FROM bit_sums WHERE pos = ?", (i,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO bit_sums(pos, val) VALUES (?, ?)", (i, v)
            )
        else:
            conn.execute(
                "UPDATE bit_sums SET val = val + ? WHERE pos = ?", (v, i)
            )
        i += i & -i


def _bit_sum(conn: sqlite3.Connection, pos: int) -> int:
    total = 0
    i = pos
    while i > 0:
        row = conn.execute("SELECT val FROM bit_sums WHERE pos = ?", (i,)).fetchone()
        if row is not None:
            total += row["val"]
        i -= i & -i
    return total


def range_delta_sum(conn: sqlite3.Connection, lo_ord: int, hi_ord: int) -> int:
    """换月序号闭区间 [lo_ord, hi_ord] 内已知价差之和（调用前需确认区间内无 NULL）。"""
    if lo_ord > hi_ord:
        return 0
    return _bit_sum(conn, hi_ord) - _bit_sum(conn, lo_ord - 1)


# --------------------------------------------------------------------------- #
# 报价维护
# --------------------------------------------------------------------------- #

def get_price(conn: sqlite3.Connection, contract_id: int, day: str) -> Optional[int]:
    row = conn.execute(
        "SELECT price FROM prices WHERE contract_id = ? AND date = ?",
        (contract_id, day),
    ).fetchone()
    return row["price"] if row else None


def list_prices(conn: sqlite3.Connection, contract_id: Optional[int] = None,
                limit: int = 500) -> list[sqlite3.Row]:
    if contract_id is None:
        return list(conn.execute(
            "SELECT p.contract_id, c.code, p.date, p.price FROM prices p "
            "JOIN contracts c ON c.contract_id = p.contract_id "
            "ORDER BY p.date DESC, c.ord LIMIT ?", (limit,)
        ))
    return list(conn.execute(
        "SELECT p.contract_id, c.code, p.date, p.price FROM prices p "
        "JOIN contracts c ON c.contract_id = p.contract_id "
        "WHERE p.contract_id = ? ORDER BY p.date DESC LIMIT ?",
        (contract_id, limit),
    ))


def _recompute_roll_delta(conn: sqlite3.Connection, ord_: int) -> Optional[int]:
    """
    依据当前 prices 重算第 ord_ 次换月的价差并更新 rolls/bit_sums。
    返回新的 delta（缺报价为 None）。
    """
    roll = conn.execute("SELECT * FROM rolls WHERE ord = ?", (ord_,)).fetchone()
    if roll is None:
        return None
    n = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
    old = contract_by_ord(conn, ord_ - 1)
    new_id = roll["new_contract_id"]
    p_old = get_price(conn, old["contract_id"], roll["pre_date"])
    p_new = get_price(conn, new_id, roll["pre_date"])
    new_delta = (p_new - p_old) if (p_old is not None and p_new is not None) else None

    old_delta = roll["delta"]
    if new_delta != old_delta:
        conn.execute("UPDATE rolls SET delta = ? WHERE ord = ?", (new_delta, ord_))
        # Fenwick 只承载非空值：旧值撤下、新值加入，均为 O(log n)
        if old_delta is not None:
            _bit_add(conn, n, ord_, -old_delta)
        if new_delta is not None:
            _bit_add(conn, n, ord_, new_delta)
    return new_delta


def upsert_price(conn: sqlite3.Connection, contract_id: int, day: str,
                 price: int) -> dict:
    """
    新增/更正一笔整数分结算价。
    原子事务内完成：
      1) prices 写入（原价与价差一起生效）；
      2) 仅重算“价差参考日 = 该日”的换月（至多 1 个，因为 pre_date 唯一）；
      3) 不触碰任何连续价、不逐日回写。
    返回预览信息（即使提交失败，调用方也可展示计算结果）。
    """
    if not valid_iso(day):
        raise AppError(f"日期格式非法：{day}，应为 YYYY-MM-DD")
    if not isinstance(price, int) or isinstance(price, bool):
        raise AppError("结算价必须是整数（单位：分）")
    if not is_trade_date(conn, day):
        raise AppError(f"{day} 不在交易日历中，不能登记报价")

    contract = get_contract(conn, contract_id)
    if contract is None:
        raise AppError(f"合约不存在：id={contract_id}")

    affected_roll = conn.execute(
        "SELECT ord FROM rolls WHERE pre_date = ?", (day,)
    ).fetchone()
    before = get_price(conn, contract_id, day)
    preview = {
        "contract": contract["code"], "date": day, "price": price,
        "old_price": before, "is_correction": before is not None,
        "affected_roll_ord": affected_roll["ord"] if affected_roll else None,
        "committed": False,
    }

    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO prices(contract_id, date, price) VALUES (?, ?, ?) "
                "ON CONFLICT(contract_id, date) DO UPDATE SET price = excluded.price",
                (contract_id, day, price),
            )
            if affected_roll is not None:
                new_delta = _recompute_roll_delta(conn, affected_roll["ord"])
                preview["new_delta"] = new_delta
            # 与原价/价差同一事务登记变更：只影响“覆盖该交易日”的查价缓存。
            # 若该报价只联动区间末日之后的换月（pre_date > 区间末日），
            # 不含该交易日的缓存仍可命中，由命中逻辑按最新价差出数。
            new_ver = _record_change(conn, "range", "upsert_price", change_day=day)
            preview["ver"] = new_ver
        # 事务已由上下文管理器提交：原价与受影响价差同时生效
        preview["committed"] = True
    except sqlite3.Error as e:
        # 保存失败已整体回滚；预期结果仍随 preview 返回供页面展示
        preview["committed"] = False
        preview["error"] = str(e)
    return preview


# --------------------------------------------------------------------------- #
# 取数：逐日连续价 + 查价区间缓存（网页与 CSV 共用）
# --------------------------------------------------------------------------- #
#
# 缓存模型
# ========
# 单槽缓存 range_cache(1)，键为 (start, end)：只缓存“区间末日 end 及之前”的逐日
# 稳定部分——当日原价、在用合约、以及切换日落入 (d, end] 的稳定窗口价差和。
# end 之后到本次基准日 base 的“尾部换月”不进缓存：命中时按当前库的最新 rolls/
# bit_sums 现算（索引反向定位 + Fenwick，O(log n)），因此：
#   - 来回换基准日（base ≥ end）直接命中，prices 表零读取；
#   - 补改报价只联动 end 之后的换月（pre_date > end）时，缓存不失效，
#     按本次基准日和最新价差出数；
#   - 区间内交易日的报价更正、日历或计划变更会使缓存失效（change_log 判定）。
#
# 一致性
# ======
# 每次取数全程在同一个只读快照事务内：缓存命中判定、缓存行、尾部最新价差都来自
# 同一已提交状态。缓存发布在独立写事务中复核版本号，构建期间若有新变更提交则放弃
# 发布——晚算完的旧缓存不可能供新请求使用。

CACHE_ID = 1


def active_contract_ord(conn: sqlite3.Connection, day: str) -> int:
    """
    当日在用合约的 ord：切换日 <= day 的换月次数。
    走 switch_date 唯一索引的反向定位（LIMIT 1），单次 O(log n)，
    不做区间计数扫描；因 ord 稠密且与切换日同序，取到的最大 ord 即次数。
    """
    row = conn.execute(
        "SELECT ord FROM rolls WHERE switch_date <= ? "
        "ORDER BY switch_date DESC LIMIT 1",
        (day,),
    ).fetchone()
    return row["ord"] if row else 0


def _first_missing_roll(conn: sqlite3.Connection, lo_ord: int,
                        hi_ord: int) -> Optional[sqlite3.Row]:
    """区间 [lo_ord, hi_ord] 内最早一个缺价差的换月（部分索引 LIMIT 1）。"""
    if lo_ord > hi_ord:
        return None
    return conn.execute(
        "SELECT r.*, nc.code AS new_code, oc.code AS old_code "
        "FROM rolls r "
        "JOIN contracts nc ON nc.contract_id = r.new_contract_id "
        "JOIN contracts oc ON oc.ord = r.ord - 1 "
        "WHERE r.ord BETWEEN ? AND ? AND r.delta IS NULL "
        "ORDER BY r.ord LIMIT 1",
        (lo_ord, hi_ord),
    ).fetchone()


def _missing_quote_contracts(conn: sqlite3.Connection,
                             roll: sqlite3.Row) -> list[sqlite3.Row]:
    """某换月价差缺失时，具体缺 pre_date 上哪个合约的报价。"""
    missing = []
    old_c = contract_by_ord(conn, roll["ord"] - 1)
    new_c = get_contract(conn, roll["new_contract_id"])
    for c in (old_c, new_c):
        if get_price(conn, c["contract_id"], roll["pre_date"]) is None:
            missing.append(c)
    return missing


def _missing_roll_message(conn: sqlite3.Connection, roll: sqlite3.Row) -> str:
    """缺价差换月的统一提示文案（构建缓存与命中现算共用，沿用原提示规则）。"""
    miss_cs = _missing_quote_contracts(conn, roll)
    names = "、".join(
        f"{c['code']}({roll['pre_date']})" for c in miss_cs
    ) or "价差所需报价"
    return (
        f"换月 {roll['old_code']} → {roll['new_code']} "
        f"（切换日 {roll['switch_date']}）缺价差，待补报价：{names}"
    )


def _invalidation_ver(conn: sqlite3.Connection, since_ver: int,
                      start: str, end: str) -> Optional[int]:
    """
    版本 since_ver 之后是否存在使区间 [start, end] 缓存失效的已提交变更：
      - scope='config'（日历/计划重建）：任何缓存都作废；
      - scope='range'（报价补改）：仅当被改交易日落在 [start, end] 内才作废；
        只影响 end 之后换月的报价更正不失效，命中时走尾部最新价差。
    走 change_log 主键/索引聚合，不读逐日原价；返回首个失效版本，无则 None。
    """
    row = conn.execute(
        "SELECT MIN(ver) AS v FROM change_log "
        "WHERE ver > ? AND ("
        "  scope = 'config' OR (scope = 'range' AND change_day BETWEEN ? AND ?)"
        ")",
        (since_ver, start, end),
    ).fetchone()
    v = row["v"] if row else None
    return v if v else None


def _load_cache(conn: sqlite3.Connection, start: str, end: str
                ) -> Optional[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    meta = conn.execute(
        "SELECT * FROM range_cache WHERE cache_id = ? AND start_date = ? AND end_date = ?",
        (CACHE_ID, start, end),
    ).fetchone()
    if meta is None:
        return None
    rows = list(conn.execute(
        "SELECT * FROM range_cache_rows WHERE cache_id = ? ORDER BY day",
        (CACHE_ID,),
    ))
    return meta, rows


def _tail_state(conn: sqlite3.Connection, end: str, base: str) -> dict:
    """
    区间末日之后、基准日（含）以内的尾部换月窗口 (end_ord, base_ord] 现算。
    读取的换月/索引记录数与区间天数无关、至多 O(log n)：
      2 次 switch_date 唯一索引反向定位、1 次部分索引 LIMIT 1、
      Fenwick 两个前缀和（各 O(log n) 个 bit_sums 主键记录）。
    """
    base_row = conn.execute(
        "SELECT ord FROM rolls WHERE switch_date <= ? "
        "ORDER BY switch_date DESC LIMIT 1",
        (base,),
    ).fetchone()
    base_hi = base_row["ord"] if base_row else 0
    end_ord = active_contract_ord(conn, end)
    lo, hi = end_ord + 1, base_hi

    miss = _first_missing_roll(conn, lo, hi)
    miss_msg = _missing_roll_message(conn, miss) if miss is not None else None
    tail_sum = None if miss is not None else range_delta_sum(conn, lo, hi)
    return {"lo": lo, "hi": hi, "miss_message": miss_msg, "tail_sum": tail_sum}


def _build_cache_payload(conn: sqlite3.Connection, end: str,
                         days: list[str]) -> list[dict]:
    """
    缓存未命中：在调用方的只读快照内逐交易日构建稳定部分。
    稳定窗口 = 切换日落在 (d, end] 的换月 ord in (ord_d, end_ord]，
    其原价与价差在缓存有效期内不会变化（变化即失效）。
    """
    end_ord = active_contract_ord(conn, end)
    payload: list[dict] = []
    for day in days:
        ord_ = active_contract_ord(conn, day)
        contract = contract_by_ord(conn, ord_)
        raw = get_price(conn, contract["contract_id"], day)
        lo = ord_ + 1
        item = {
            "day": day, "contract": contract["code"], "ord": ord_,
            "lo_ord": lo, "raw_price": raw, "stable_adj": None,
            "status": "ok", "message": "",
        }
        if raw is None:
            item["status"] = "missing_raw"
            item["message"] = f"缺少原价：合约 {contract['code']} 在 {day} 的结算价待补"
            payload.append(item)
            continue
        miss = _first_missing_roll(conn, lo, end_ord)
        if miss is not None:
            item["status"] = "missing_roll"
            item["message"] = _missing_roll_message(conn, miss)
        else:
            item["stable_adj"] = range_delta_sum(conn, lo, end_ord)
        payload.append(item)
    return payload


def _assemble_rows(payload: list[dict], tail: dict) -> list[dict]:
    """
    稳定部分（缓存行或刚构建的 payload）+ 当前快照的尾部换月状态 → 出数行。
    构建与命中走同一组装逻辑，保证两条路径口径完全一致。
    """
    rows: list[dict] = []
    for c in payload:
        row = {
            "date": c["day"], "contract": c["contract"],
            "raw_price": c["raw_price"], "continuous": None,
            "status": c["status"], "message": c["message"],
        }
        if c["status"] != "ok":
            rows.append(row)  # 缺原价 / 稳定窗口缺换月报价：提示已随缓存固化
            continue
        if tail["miss_message"] is not None:
            # 尾部（end 之后）缺价差：对区间内每一天都成立，按最早缺价换月提示
            row["status"] = "missing_roll"
            row["message"] = tail["miss_message"]
        else:
            adj = c["stable_adj"] + tail["tail_sum"]
            row["adjustment"] = adj
            row["continuous"] = c["raw_price"] + adj
        rows.append(row)
    return rows


def _persist_cache(conn: sqlite3.Connection, start: str, end: str,
                   built_ver: int, payload: list[dict]) -> bool:
    """
    发布单槽缓存（替换旧区间）。发布前复核当前版本：构建期间若已有新变更提交，
    放弃发布，保证晚算完的旧缓存不会供新请求使用。失败不影响本次取数正确性。
    """
    try:
        with write_tx(conn):
            if current_ver(conn) != built_ver:
                return False
            conn.execute("DELETE FROM range_cache_rows")
            conn.execute("DELETE FROM range_cache")
            conn.execute(
                "INSERT INTO range_cache(cache_id, start_date, end_date, built_ver) "
                "VALUES (?, ?, ?, ?)",
                (CACHE_ID, start, end, built_ver),
            )
            conn.executemany(
                "INSERT INTO range_cache_rows(cache_id, day, contract, ord, lo_ord, "
                " raw_price, stable_adj, status, message) "
                "VALUES (1, :day, :contract, :ord, :lo_ord, :raw_price, "
                "        :stable_adj, :status, :message)",
                payload,
            )
        return True
    except sqlite3.Error:
        return False


def _read_range_once(conn: sqlite3.Connection, start: str, end: str,
                     base: str, days: list[str]) -> tuple[list[dict], str, int, list[dict]]:
    """在一个只读快照事务内完成一次取数，返回 (rows, status, ver_now, payload)。"""
    cache_status = "miss"
    with snapshot(conn):
        # 快照内首读即固定版本：缓存判定、缓存行、尾部价差都来自同一已提交状态
        ver_now = current_ver(conn)
        tail = _tail_state(conn, end, base)
        found = _load_cache(conn, start, end)
        payload: list[dict]
        if found is not None:
            meta, cached_rows = found
            if (len(cached_rows) == len(days)
                    and _invalidation_ver(conn, meta["built_ver"], start, end) is None):
                payload = [dict(r) for r in cached_rows]
                cache_status = "hit"
        if cache_status != "hit":
            payload = _build_cache_payload(conn, end, days)
        rows = _assemble_rows(payload, tail)
    return rows, cache_status, ver_now, payload


def fetch_range_meta(conn: sqlite3.Connection, start: str, end: str,
                     base: str) -> tuple[list[dict], dict]:
    """
    取 [start, end] 交易日的连续价，基准日 base（不早于 end）。
    返回 (rows, meta)，meta 标注本次是缓存命中 hit、未命中 miss，
    还是构建期间数据已变更而重取 miss_stale。
    整次取数在一个只读快照事务内完成，只看到同一已提交状态（SQLite 读快照）。
    命中时不读 prices 逐日原价，读取的换月/索引记录数至多 O(log n)（n=换月次数）。
    """
    for label, v in (("开始日", start), ("结束日", end), ("基准日", base)):
        if not valid_iso(v):
            raise AppError(f"{label}格式非法：{v}，应为 YYYY-MM-DD")
    if start > end:
        raise AppError("开始日不能晚于结束日")
    if base < end:
        raise AppError(f"基准日 {base} 不能早于结束日 {end}")

    # 日历与取数参数校验在任何快照之外完成
    with snapshot(conn):
        days = [r["date"] for r in conn.execute(
            "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        )]
    if not days:
        raise AppError(f"{start} 至 {end} 之间没有交易日")

    # 至多重试一次：发布复核发现构建期间有新提交时，丢弃旧快照结果，
    # 在新快照上按当前库重取——晚算完的旧缓存/旧结果都不会供新请求使用
    rows, status, ver_now, payload = _read_range_once(conn, start, end, base, days)
    if status == "miss" and not _persist_cache(conn, start, end, ver_now, payload):
        # 构建期间数据已变更：旧结果作废，按当前库重新取数并再试一次发布
        rows, _, ver_now, payload = _read_range_once(conn, start, end, base, days)
        _persist_cache(conn, start, end, ver_now, payload)
        status = "miss_stale"  # 本次返回的已是当前库状态的重取结果
    return rows, {"cache": status, "built_ver": ver_now}


def fetch_range(conn: sqlite3.Connection, start: str, end: str,
                base: str) -> list[dict]:
    """兼容旧接口：只返回逐日行（meta 由 fetch_range_meta 提供）。"""
    rows, _ = fetch_range_meta(conn, start, end, base)
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["交易日", "在用合约", "原价(分)", "连续价(分)", "状态", "说明"])
    status_cn = {"ok": "正常", "missing_raw": "缺原价", "missing_roll": "缺换月报价"}
    for r in rows:
        writer.writerow([
            r["date"], r["contract"],
            "" if r["raw_price"] is None else r["raw_price"],
            "" if r["continuous"] is None else r["continuous"],
            status_cn.get(r["status"], r["status"]),
            r["message"],
        ])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 状态总览
# --------------------------------------------------------------------------- #

def overview(conn: sqlite3.Connection) -> dict:
    n_contracts = conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"]
    n_rolls = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
    n_missing_delta = conn.execute(
        "SELECT COUNT(*) AS n FROM rolls WHERE delta IS NULL"
    ).fetchone()["n"]
    n_prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
    n_days = conn.execute("SELECT COUNT(*) AS n FROM calendar").fetchone()["n"]
    cal = list_calendar(conn)
    return {
        "n_contracts": n_contracts,
        "n_rolls": n_rolls,
        "n_missing_delta": n_missing_delta,
        "n_prices": n_prices,
        "n_days": n_days,
        "cal_min": cal[0] if cal else None,
        "cal_max": cal[-1] if cal else None,
    }
