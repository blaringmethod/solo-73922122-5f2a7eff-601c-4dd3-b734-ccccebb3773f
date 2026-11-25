"""
核心逻辑测试：
  连续价口径、缺价提示、改价联动、原子提交、快照隔离、Fenwick 对数复杂度、CSV、Web 冒烟、
  查价区间缓存（命中/失效/版本作废/对数级读取）。
运行：PYTHONPATH=. python -m unittest discover -s tests -v
"""
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from app import db


CAL = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
       "2026-01-09", "2026-01-12", "2026-01-13"]

# (合约, 日期, 价格)  单位：分
PRICES = [
    ("C0", "2026-01-05", 1000), ("C0", "2026-01-06", 1010),
    ("C0", "2026-01-07", 1020),
    ("C1", "2026-01-07", 1030), ("C1", "2026-01-08", 1040),
    ("C1", "2026-01-09", 1050),
    ("C2", "2026-01-09", 1075), ("C2", "2026-01-12", 1080),
    ("C2", "2026-01-13", 1090),
]


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        db.replace_calendar(self.conn, CAL)
        db.setup_plan(self.conn, "C0",
                      [("2026-01-08", "C1"), ("2026-01-12", "C2")])
        cid = {r["code"]: r["contract_id"] for r in db.list_contracts(self.conn)}
        for code, day, px in PRICES:
            db.upsert_price(self.conn, cid[code], day, px)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def fetch(self, start, end, base):
        return db.fetch_range(self.conn, start, end, base)


class TestContinuousPrice(BaseCase):
    def test_delta_values(self):
        rolls = {r["ord"]: r["delta"] for r in db.list_rolls(self.conn)}
        self.assertEqual(rolls[1], 1030 - 1020)   # +10
        self.assertEqual(rolls[2], 1075 - 1050)   # +25

    def test_continuous_full_range_base_latest(self):
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        got = {r["date"]: r["continuous"] for r in rows}
        expected = {
            "2026-01-05": 1000 + 10 + 25,   # 两次换月都在该日之后
            "2026-01-06": 1010 + 10 + 25,
            "2026-01-07": 1020 + 10 + 25,
            "2026-01-08": 1040 + 25,        # roll1 当日已生效，只加 roll2
            "2026-01-09": 1050 + 25,
            "2026-01-12": 1080,             # 基准日前最后一次换月当日起不再调整
            "2026-01-13": 1090,
        }
        self.assertEqual(got, expected)
        self.assertTrue(all(r["status"] == "ok" for r in rows))

    def test_continuous_base_inside_range(self):
        # 基准日 = 结束日 01-09：roll2(01-12) 在基准日之后，不计入
        rows = self.fetch("2026-01-05", "2026-01-09", "2026-01-09")
        got = {r["date"]: r["continuous"] for r in rows}
        self.assertEqual(got["2026-01-05"], 1010)   # 1000 + 10
        self.assertEqual(got["2026-01-08"], 1040)
        self.assertEqual(got["2026-01-09"], 1050)

    def test_base_before_end_rejected(self):
        with self.assertRaises(db.AppError):
            self.fetch("2026-01-05", "2026-01-13", "2026-01-08")

    def test_missing_raw_price(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-06'",
                          (cid,))
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        row = next(r for r in rows if r["date"] == "2026-01-06")
        self.assertIsNone(row["continuous"])
        self.assertEqual(row["status"], "missing_raw")
        self.assertIn("C0", row["message"])
        self.assertIn("2026-01-06", row["message"])

    def test_missing_roll_quote_points_earliest_and_contracts(self):
        # 删掉 roll1 参考日(01-07) 的新合约报价 → delta1 置空
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-07'",
                          (cid,))
        # 直接重算 roll1 模拟该报价缺失下的状态
        with db.write_tx(self.conn):
            db._recompute_roll_delta(self.conn, 1)
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        early = next(r for r in rows if r["date"] == "2026-01-05")
        self.assertIsNone(early["continuous"])
        self.assertEqual(early["status"], "missing_roll")
        # 指出最早缺价换月（ord=1）及待补的具体合约报价
        self.assertIn("C0 → C1", early["message"])
        self.assertIn("C1(2026-01-07)", early["message"])
        # roll1 之后的日子不受该换月缺失影响（01-12 仍正常）
        last = next(r for r in rows if r["date"] == "2026-01-12")
        self.assertEqual(last["continuous"], 1080)

    def test_correction_recomputes_delta_and_continuous(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        preview = db.upsert_price(self.conn, cid, "2026-01-07", 1035)  # 1030 → 1035
        self.assertTrue(preview["committed"])
        self.assertTrue(preview["is_correction"])
        self.assertEqual(preview["new_delta"], 15)
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(rows[0]["continuous"], 1000 + 15 + 25)
        # 连续价不落地：库中不存在任何连续价表/列
        tables = {r["name"] for r in
                  self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("continuous", tables)

    def test_non_trade_date_rejected(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        with self.assertRaises(db.AppError):
            db.upsert_price(self.conn, cid, "2026-01-10", 999)  # 周六，非交易日

    def test_integer_price_required(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        with self.assertRaises(db.AppError):
            db.upsert_price(self.conn, cid, "2026-01-05", 1000.5)


class TestAtomicityAndSnapshot(BaseCase):
    def test_save_failure_rolls_back_but_returns_preview(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        before = db.get_price(self.conn, cid, "2026-01-07")

        # 让 Fenwick 更新在事务中途失败（模拟保存失败）
        orig = db._bit_add
        def boom(conn, n, pos, v):
            raise sqlite3.OperationalError("simulated disk failure")
        db._bit_add = boom
        try:
            preview = db.upsert_price(self.conn, cid, "2026-01-07", 1111)
        finally:
            db._bit_add = orig

        self.assertFalse(preview["committed"])          # 仍返回预期结果
        self.assertEqual(preview["price"], 1111)
        self.assertEqual(db.get_price(self.conn, cid, "2026-01-07"), before)  # 已回滚
        roll = self.conn.execute("SELECT delta FROM rolls WHERE ord=1").fetchone()
        self.assertEqual(roll["delta"], 10)             # 价差也保持旧值

    def test_snapshot_sees_one_committed_state(self):
        other = db.connect(self.db_path)
        cid = next(r["contract_id"] for r in db.list_contracts(other)
                   if r["code"] == "C0")
        with db.snapshot(self.conn):
            seen_before = db.get_price(self.conn, cid, "2026-01-05")
            db.upsert_price(other, cid, "2026-01-05", 2222)   # 另一连接已提交
            seen_in_snap = db.get_price(self.conn, cid, "2026-01-05")
        seen_after = db.get_price(self.conn, cid, "2026-01-05")
        self.assertEqual(seen_before, 1000)
        self.assertEqual(seen_in_snap, 1000)   # 快照内不被外部提交影响
        self.assertEqual(seen_after, 2222)
        other.close()


class TestFenwickLogGrowth(unittest.TestCase):
    def _build(self, n_rolls):
        tmp = tempfile.TemporaryDirectory()
        conn = db.connect(os.path.join(tmp.name, "b.db"))
        db.init_db(conn)
        # 跨月生成连续自然日，避免硬编码月份天数
        start = date(2026, 1, 5)
        days = [(start + timedelta(days=i)).isoformat()
                for i in range(2 + (n_rolls + 1) * 2)]
        db.replace_calendar(conn, days)
        switches = [(days[2 * k + 1], f"K{k+1}") for k in range(n_rolls)]
        db.setup_plan(conn, "K0", switches)
        return tmp, conn, days

    def test_matches_brute_force_and_aux_size(self):
        n = 20
        tmp, conn, days = self._build(n)
        try:
            cid = {r["code"]: r["contract_id"] for r in db.list_contracts(conn)}
            # 每个交易日给所有合约报价，价差取确定性序列
            for k in range(n + 1):
                for di, day in enumerate(days):
                    db.upsert_price(conn, cid[f"K{k}"], day, 100 + k * 7 + di)
            # 辅助表行数 ≤ 换月次数（稀疏 Fenwick，上界 n）
            bit_rows = conn.execute("SELECT COUNT(*) AS c FROM bit_sums").fetchone()["c"]
            self.assertLessEqual(bit_rows, n)

            rows = db.fetch_range(conn, days[0], days[-1], days[-1])
            # 暴力重算对照
            rolls = db.list_rolls(conn)
            for r in rows:
                day = r["date"]
                ord_ = db.active_contract_ord(conn, day)
                code = db.contract_by_ord(conn, ord_)["code"]
                raw = db.get_price(conn, cid[code], day)
                s = 0
                for roll in rolls:
                    if day < roll["switch_date"] <= days[-1]:
                        s += roll["delta"]
                self.assertEqual(r["continuous"], raw + s, msg=day)
        finally:
            conn.close()
            tmp.cleanup()

    def test_query_cost_is_log_steps(self):
        # Fenwick 前缀和的节点访问数 = 二进制置位数 ≤ log2(n)+1
        n = 64
        tmp, conn, days = self._build(n)
        try:
            # 填入报价使所有 delta 非空、Fenwick 节点被实际写入
            cid = {r["code"]: r["contract_id"] for r in db.list_contracts(conn)}
            for k in range(n + 1):
                for di, day in enumerate(days):
                    db.upsert_price(conn, cid[f"K{k}"], day, 100 + k * 7 + di)

            steps = 0
            orig_bit_sum = db._bit_sum

            def counting_bit_sum(c, pos):
                nonlocal steps
                steps += 1
                return orig_bit_sum(c, pos)

            db._bit_sum = counting_bit_sum
            try:
                db.range_delta_sum(conn, 1, n)  # 两个前缀和，各 O(log n)
            finally:
                db._bit_sum = orig_bit_sum
            self.assertLessEqual(steps, 2 * (n.bit_length()))
        finally:
            conn.close()
            tmp.cleanup()


class TestPlanValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "v.db"))
        db.init_db(self.conn)
        db.replace_calendar(self.conn, CAL)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_switch_must_be_trade_day(self):
        with self.assertRaises(db.AppError):
            db.setup_plan(self.conn, "C0", [("2026-01-10", "C1")])

    def test_switch_dates_strictly_increasing(self):
        with self.assertRaises(db.AppError):
            db.setup_plan(self.conn, "C0",
                          [("2026-01-08", "C1"), ("2026-01-08", "C2")])

    def test_rebuild_blocked_after_prices(self):
        db.setup_plan(self.conn, "C0", [("2026-01-08", "C1")])
        cid = db.list_contracts(self.conn)[0]["contract_id"]
        db.upsert_price(self.conn, cid, "2026-01-05", 100)
        with self.assertRaises(db.AppError):
            db.setup_plan(self.conn, "C0", [("2026-01-09", "X1")])


class _CountingConn:
    """
    连接代理：统计 SELECT 语句对各类表的访问次数。
    本系统中 rolls/bit_sums 的读取均为索引定位/LIMIT 1/主键点查（每次至多 1 行），
    因此语句数即换月及索引记录读取数的上界，可用来证明 O(log n) 且与区间天数无关。
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "counters", {
            "prices_reads": 0, "roll_index_reads": 0,
            "cache_reads": 0, "other_reads": 0, "writes": 0,
        })

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name in ("_real", "counters"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)

    def execute(self, sql, params=()):
        s = sql.lstrip().upper()
        if s.startswith("SELECT"):
            if re.search(r"\b(?:FROM|JOIN)\s+PRICES\b", sql, re.I):
                self.counters["prices_reads"] += 1
            elif re.search(r"\b(?:FROM|JOIN)\s+(ROLLS|BIT_SUMS)\b", sql, re.I):
                self.counters["roll_index_reads"] += 1
            elif re.search(r"\b(?:FROM|JOIN)\s+(RANGE_CACHE|RANGE_CACHE_ROWS|CHANGE_LOG)\b",
                           sql, re.I):
                self.counters["cache_reads"] += 1
            else:
                self.counters["other_reads"] += 1
        else:
            self.counters["writes"] += 1
        return self._real.execute(sql, params)

    def close(self):
        return self._real.close()


class TestRangeCache(BaseCase):
    def fetch_meta(self, start, end, base):
        return db.fetch_range_meta(self.conn, start, end, base)

    def test_first_miss_then_hit_and_equal(self):
        r1, m1 = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m1["cache"], "miss")
        r2, m2 = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m2["cache"], "hit")
        self.assertEqual(r1, r2)  # 命中路径与构建路径出数完全一致

    def test_switch_base_day_keeps_hit(self):
        # 同一查价区间（start/end 不变），仅换基准日
        _, m1 = self.fetch_meta("2026-01-05", "2026-01-09", "2026-01-09")
        self.assertEqual(m1["cache"], "miss")
        rows, m2 = self.fetch_meta("2026-01-05", "2026-01-09", "2026-01-13")
        self.assertEqual(m2["cache"], "hit")
        got = {r["date"]: r["continuous"] for r in rows}
        # 基准日 01-13：roll2 计入 → 首日 = 1000+10+25
        self.assertEqual(got["2026-01-05"], 1035)
        # 再换回基准日 01-09：roll2 不计入 → 首日 = 1000+10
        rows, m3 = self.fetch_meta("2026-01-05", "2026-01-09", "2026-01-09")
        self.assertEqual(m3["cache"], "hit")
        self.assertEqual(rows[0]["continuous"], 1010)

    def test_web_and_csv_share_one_cache(self):
        os.environ["CONT_DB_PATH"] = self.db_path
        from app.app import create_app
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        q = "/query?start=2026-01-05&end=2026-01-13&base=2026-01-13"
        self.assertEqual(client.get(q).headers["X-Cache-Status"], "miss")
        # 网页再查与 CSV 下载都命中同一个缓存
        self.assertEqual(client.get(q).headers["X-Cache-Status"], "hit")
        dl = "/download?start=2026-01-05&end=2026-01-13&base=2026-01-13"
        self.assertEqual(client.get(dl).headers["X-Cache-Status"], "hit")

    def test_correction_only_affecting_roll_after_end_keeps_hit(self):
        # 区间末日 01-08；roll2 参考日 01-09 在末日之后
        _, m1 = self.fetch_meta("2026-01-05", "2026-01-08", "2026-01-13")
        self.assertEqual(m1["cache"], "miss")
        before = {r["date"]: r["continuous"] for r in
                  self.fetch_meta("2026-01-05", "2026-01-08", "2026-01-13")[0]}
        self.assertEqual(before["2026-01-05"], 1035)  # 1000 + 10 + 25

        # 改 01-09 旧合约 C1 的价：1050 → 1060，只联动 roll2（pre_date=01-09 > end）
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        db.upsert_price(self.conn, cid, "2026-01-09", 1060)

        rows, m2 = self.fetch_meta("2026-01-05", "2026-01-08", "2026-01-13")
        self.assertEqual(m2["cache"], "hit")  # 补改不影响区间内数据，缓存仍命中
        # 新 delta2 = 1075 - 1060 = 15；按本次基准日与最新价差出数
        self.assertEqual(rows[0]["continuous"], 1000 + 10 + 15)
        # 区间内 01-08 当天原价未受影响
        self.assertEqual(next(r for r in rows if r["date"] == "2026-01-08")
                         ["continuous"], 1040 + 15)

    def test_correction_inside_range_invalidates_cache(self):
        self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        db.upsert_price(self.conn, cid, "2026-01-07", 1035)  # 1030 → 1035
        rows, m = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m["cache"], "miss")          # 区间内报价更正 → 作废重建
        self.assertEqual(rows[0]["continuous"], 1000 + 15 + 25)
        rows2, m2 = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m2["cache"], "hit")          # 重建后再次命中
        self.assertEqual(rows2, rows)

    def test_missing_price_hints_identical_on_hit(self):
        # 缺原价场景
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-06'",
                          (cid,))
        r1, m1 = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        r2, m2 = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m1["cache"], "miss")
        self.assertEqual(m2["cache"], "hit")
        self.assertEqual(r1, r2)  # 缺价留空与提示规则命中时保持一致
        row = next(r for r in r2 if r["date"] == "2026-01-06")
        self.assertEqual(row["status"], "missing_raw")
        self.assertIn("C0", row["message"])

    def test_missing_roll_hint_identical_on_hit(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-07'",
                          (cid,))
        with db.write_tx(self.conn):
            db._recompute_roll_delta(self.conn, 1)
        r1, _ = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        r2, m2 = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m2["cache"], "hit")
        self.assertEqual(r1, r2)
        self.assertIn("C0 → C1", r2[0]["message"])
        self.assertIn("C1(2026-01-07)", r2[0]["message"])

    def test_calendar_change_invalidates_cache(self):
        self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        db.replace_calendar(self.conn, CAL)  # 内容相同也会登记 config 变更
        _, m = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m["cache"], "miss")

    def test_config_scope_change_always_invalidates(self):
        self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        with db.write_tx(self.conn):
            db._record_change(self.conn, "config", "test-config-change")
        _, m = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m["cache"], "miss")

    def test_stale_cache_built_at_old_version_not_served(self):
        # 先建缓存，再手工把缓存标记为更旧的版本（模拟晚算完的旧缓存）
        self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.conn.execute("UPDATE range_cache SET built_ver = 0 WHERE cache_id = 1")
        rows, m = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m["cache"], "miss")          # 旧缓存不供新请求
        rows2, m2 = self.fetch_meta("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(m2["cache"], "hit")
        self.assertEqual(rows, rows2)

    def test_persist_rejects_when_version_moved_during_build(self):
        payload = [{
            "day": "2026-01-05", "contract": "C0", "ord": 0, "lo_ord": 1,
            "raw_price": 1000, "stable_adj": 0, "status": "ok", "message": "",
        }]
        ver = db.current_ver(self.conn)
        # 构建期间行情员补改了一笔报价（版本前进）
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        db.upsert_price(self.conn, cid, "2026-01-05", 1111)
        published = db._persist_cache(self.conn, "2026-01-05", "2026-01-05",
                                      ver, payload)
        self.assertFalse(published)  # 版本复核失败，放弃发布旧缓存
        self.assertIsNone(
            self.conn.execute("SELECT * FROM range_cache WHERE cache_id=1").fetchone())

    def test_fetch_retries_with_fresh_snapshot_when_build_goes_stale(self):
        # 端到端模拟：读快照固定 → 期间他人提交更正 → 发布复核失败 → 自动重取
        reader = db.connect(self.db_path)
        try:
            reader.execute("BEGIN")
            ver = db.current_ver(reader)
            days = [r["date"] for r in reader.execute(
                "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
                ("2026-01-05", "2026-01-13"))]
            stale_payload = db._build_cache_payload(reader, "2026-01-13", days)
            other = db.connect(self.db_path)
            cid = next(r["contract_id"] for r in db.list_contracts(other)
                       if r["code"] == "C1")
            db.upsert_price(other, cid, "2026-01-07", 1035)  # delta1: 10 → 15
            other.close()
            reader.execute("COMMIT")
            # 直接发布旧 payload 必被拒
            self.assertFalse(db._persist_cache(reader, "2026-01-05", "2026-01-13",
                                               ver, stale_payload))
        finally:
            reader.close()
        # 正常取数：内部重取后返回的是更正后的当前库结果
        rows, meta = db.fetch_range_meta(self.conn, "2026-01-05",
                                         "2026-01-13", "2026-01-13")
        self.assertIn(meta["cache"], ("miss", "miss_stale"))
        self.assertEqual(rows[0]["continuous"], 1000 + 15 + 25)
        # 再查必命中新缓存
        rows2, meta2 = db.fetch_range_meta(self.conn, "2026-01-05",
                                           "2026-01-13", "2026-01-13")
        self.assertEqual(meta2["cache"], "hit")
        self.assertEqual(rows2, rows)

    def test_hit_does_not_read_prices_and_roll_reads_are_log_n(self):
        # 64 次换月，取数只覆盖前半段，基准日放到最后 → 命中时尾部含大量换月
        n = 64
        tmp = tempfile.TemporaryDirectory()
        big_path = os.path.join(tmp.name, "big.db")
        big = db.connect(big_path)
        db.init_db(big)
        start0 = date(2026, 1, 5)
        days = [(start0 + timedelta(days=i)).isoformat()
                for i in range(2 + (n + 1) * 2)]
        db.replace_calendar(big, days)
        switches = [(days[2 * k + 1], f"K{k+1}") for k in range(n)]
        db.setup_plan(big, "K0", switches)
        cid = {r["code"]: r["contract_id"] for r in db.list_contracts(big)}
        for k in range(n + 1):
            for di, day in enumerate(days):
                db.upsert_price(big, cid[f"K{k}"], day, 100 + k * 7 + di)

        q_start, q_end, q_base = days[2], days[40], days[-1]
        # 首次未命中：构建缓存（会逐日读 prices）
        r_miss, m_miss = db.fetch_range_meta(big, q_start, q_end, q_base)
        self.assertEqual(m_miss["cache"], "miss")

        # 另开连接做计数代理，读已提交的缓存
        counter = _CountingConn(db.connect(big_path))
        try:
            r_hit, m_hit = db.fetch_range_meta(counter, q_start, q_end, q_base)
            self.assertEqual(m_hit["cache"], "hit")
            self.assertEqual(r_hit, r_miss)
            # 命中时完全不重读逐日原价
            self.assertEqual(counter.counters["prices_reads"], 0)
            # 换月/索引记录读取数只随换月次数对数增长，与区间天数无关：
            # 2 次切换日索引定位 + 1 次缺价部分索引 + 两个 Fenwick 前缀和（各 ≤ bit_length）
            bound = 3 + 2 * n.bit_length()
            self.assertLessEqual(counter.counters["roll_index_reads"], bound)
            # 缓存行数 = 区间交易日数（缓存自身数据，不计入换月/索引读取）
            n_cached = counter.execute(
                "SELECT COUNT(*) AS c FROM range_cache_rows").fetchone()["c"]
            self.assertEqual(n_cached, len(r_hit))
        finally:
            counter.close()
            big.close()
            tmp.cleanup()

    def test_build_then_concurrent_correction_serves_only_fresh_state(self):
        # 模拟取数进行到一半时行情员补改报价：
        # 取数连接的读快照固定在改价前，发布时复核到版本已前进 → 放弃发布；
        # 随后的新请求不命中旧缓存，按当前库出数。
        reader = db.connect(self.db_path)
        try:
            reader.execute("BEGIN")  # 读快照固定在改价前
            ver = db.current_ver(reader)
            days = [r["date"] for r in reader.execute(
                "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
                ("2026-01-05", "2026-01-13"))]
            payload = db._build_cache_payload(reader, "2026-01-13", days)
            # 另一连接在取数期间提交更正（区间内报价）
            other = db.connect(self.db_path)
            cid = next(r["contract_id"] for r in db.list_contracts(other)
                       if r["code"] == "C1")
            db.upsert_price(other, cid, "2026-01-07", 1035)
            other.close()
            reader.execute("COMMIT")

            published = db._persist_cache(reader, "2026-01-05", "2026-01-13",
                                          ver, payload)
            self.assertFalse(published)  # 晚算完的旧缓存不得发布
            rows, m = db.fetch_range_meta(self.conn, "2026-01-05",
                                          "2026-01-13", "2026-01-13")
            self.assertEqual(m["cache"], "miss")      # 新请求按当前库重新取数
            self.assertEqual(rows[0]["continuous"], 1000 + 15 + 25)
        finally:
            reader.close()

    def test_hit_matches_brute_force_after_corrections(self):
        # 交错补改后，命中结果必须与按当前库暴力重算一致
        self.fetch_meta("2026-01-05", "2026-01-08", "2026-01-13")  # 建缓存
        c1 = next(r["contract_id"] for r in db.list_contracts(self.conn)
                  if r["code"] == "C1")
        db.upsert_price(self.conn, c1, "2026-01-09", 1060)   # 仅影响 end 之后换月 → 仍命中
        rows, m = self.fetch_meta("2026-01-05", "2026-01-08", "2026-01-13")
        self.assertEqual(m["cache"], "hit")

        rolls = db.list_rolls(self.conn)
        for r in rows:
            day = r["date"]
            ord_ = db.active_contract_ord(self.conn, day)
            code = db.contract_by_ord(self.conn, ord_)["code"]
            cidv = next(c["contract_id"] for c in db.list_contracts(self.conn)
                        if c["code"] == code)
            raw = db.get_price(self.conn, cidv, day)
            s = sum(rl["delta"] for rl in rolls
                    if day < rl["switch_date"] <= "2026-01-13")
            self.assertEqual(r["continuous"], raw + s, msg=day)


class TestCSVAndWeb(BaseCase):
    def test_csv_columns_and_blank_for_missing(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-06'",
                          (cid,))
        rows = self.fetch("2026-01-05", "2026-01-08", "2026-01-13")
        text = db.to_csv(rows)
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "交易日,在用合约,原价(分),连续价(分),状态,说明")
        blank = next(l for l in lines if l.startswith("2026-01-06"))
        self.assertIn(",,", blank)          # 连续价留空
        self.assertIn("缺原价", blank)

    def test_flask_routes_and_download(self):
        os.environ["CONT_DB_PATH"] = self.db_path  # 让 Web 请求落到本用例的临时库
        from app.app import create_app
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        for path in ("/", "/calendar", "/plan", "/prices", "/query"):
            self.assertEqual(client.get(path).status_code, 200)

        # Web 表单更正一笔报价
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        resp = client.post("/prices", data={
            "contract_id": cid, "date": "2026-01-07", "price": 1038})
        self.assertEqual(resp.status_code, 302)

        resp = client.get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["Content-Type"])
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        body = resp.data.decode("utf-8-sig")
        self.assertIn("2026-01-05", body)
        # 1038 → delta1 = 18，首日连续价 = 1000+18+25 = 1043
        self.assertIn("1043", body)


if __name__ == "__main__":
    unittest.main()
