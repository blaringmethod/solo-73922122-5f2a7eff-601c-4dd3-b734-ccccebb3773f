"""
端到端试跑脚本：对运行中的 Flask 服务（默认 http://127.0.0.1:8000）
走一遍完整业务流，重点演示查价区间缓存：
  1) 日历 / 换月计划 / 报价录入
  2) 首次取数未命中并建缓存，网页与 CSV 共用
  3) 来回换基准日：缓存命中、不重读逐日原价
  4) 补改只影响区间末日之后换月的报价：缓存仍命中，按本次基准日与最新价差出数
  5) 区间内改价：缓存作废重建，结果与当前库一致
  6) 缺价提示规则保持不变
用法：python tests/e2e_http.py
"""
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
CAL = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
       "2026-01-09", "2026-01-12", "2026-01-13"]
# (code, date, price)
PRICES = [
    ("IF2601", "2026-01-05", 38000), ("IF2601", "2026-01-06", 38100),
    ("IF2601", "2026-01-07", 38200),
    ("IF2602", "2026-01-07", 38300), ("IF2602", "2026-01-08", 38400),
    ("IF2602", "2026-01-09", 38500),
    ("IF2603", "2026-01-09", 38750), ("IF2603", "2026-01-12", 38800),
    ("IF2603", "2026-01-13", 38900),
]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不自动跟随，让调用方直接看到 302


opener = urllib.request.build_opener(NoRedirect)


def post(path, data):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "ignore"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore"), e.headers


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read().decode("utf-8-sig"), r.headers


def main():
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    # 1) 交易日历
    st, _, _ = post("/calendar", {"dates": "\n".join(CAL)})
    check("保存交易日历", st == 302)

    # 2) 换月计划：初始 IF2601，01-08 切 IF2602，01-12 切 IF2603
    st, _, _ = post("/plan", {
        "initial_code": "IF2601",
        "switch_date": ["2026-01-08", "2026-01-12"],
        "switch_code": ["IF2602", "IF2603"],
    })
    check("登记换月计划", st == 302)

    # 取合约 id（从 /prices 页面解析 option value）
    st, html, _ = get("/prices")
    import re
    cid = {code: cid_ for cid_, code, _ in re.findall(
        r'value="(\d+)">([^（]+)（ord=(\d+)）', html)}
    assert len(cid) == 3, f"合约数异常: {cid}"

    # 3) 补录全部报价
    for code, day, px in PRICES:
        st, _, _ = post("/prices", {"contract_id": cid[code], "date": day, "price": px})
        assert st == 302, (code, day, st)
    check("补录 9 笔报价", True)

    # 4) 取数：基准日 01-13 —— 首次未命中，构建区间缓存
    q_full = "/query?start=2026-01-05&end=2026-01-13&base=2026-01-13"
    st, html, headers = get(q_full)
    check("首次取数 HTTP 200", st == 200)
    check("首次取数缓存未命中(miss)", headers.get("X-Cache-Status") == "miss",
          headers.get("X-Cache-Status"))
    # 首日连续价 = 38000 +(38300-38200)+(38750-38500)= 38000+100+250=38350
    check("首日连续价 38350", "38350" in html)
    check("末日连续价 38900", "38900" in html)

    # 5) 网页再查 + CSV 下载：共用同一缓存，均命中
    st, html, headers = get(q_full)
    check("网页二次查询命中缓存(hit)", headers.get("X-Cache-Status") == "hit",
          headers.get("X-Cache-Status"))
    check("二次查询数值不变 38350", "38350" in html)
    st, csv_text, headers = get(
        "/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    lines = csv_text.strip().splitlines()
    check("CSV 下载", st == 200 and len(lines) == 8 and lines[0].startswith("交易日"))
    check("CSV 与网页共用缓存(hit)", headers.get("X-Cache-Status") == "hit",
          headers.get("X-Cache-Status"))
    check("CSV 首行连续价 38350", lines[1].split(",")[3] == "38350")

    # 6) 来回换基准日（须在缓存仍是本区间时演示：单槽只保留最近一个查价区间）：
    #    区间不变，缓存持续命中，数值随基准日口径变化
    #    基准日不得早于结束日；取 end=01-09，在 base=01-09（roll2 不计入）
    #    与 base=01-13（roll2 切换日 01-12 已计入）之间来回切
    q_b09 = "/query?start=2026-01-05&end=2026-01-09&base=2026-01-09"
    st, html, headers = get(q_b09)
    check("先建 end=01-09 区间缓存(miss)",
          headers.get("X-Cache-Status") == "miss", headers.get("X-Cache-Status"))
    # 基准日 01-09：roll2(切换日 01-12) 在基准日之后，不计入 → 首日 = 38000+100 = 38100
    check("基准日 01-09 首日 38100", "38100" in html and "38350" not in html)
    q_b13 = "/query?start=2026-01-05&end=2026-01-09&base=2026-01-13"
    st, html, headers = get(q_b13)
    check("换基准日 01-13 仍命中", headers.get("X-Cache-Status") == "hit",
          headers.get("X-Cache-Status"))
    check("基准日 01-13 首日 38350", "38350" in html)
    st, html, headers = get(q_b09)  # 换回 01-09
    check("换回基准日 01-09 仍命中", headers.get("X-Cache-Status") == "hit",
          headers.get("X-Cache-Status"))
    check("换回后数值恢复 38100", "38100" in html and "38350" not in html)
    st, html, headers = get(q_b13)  # 再换回 01-13
    check("再换回基准日 01-13 仍命中", headers.get("X-Cache-Status") == "hit",
          headers.get("X-Cache-Status"))
    check("再换后数值恢复 38350", "38350" in html)

    # 7) 换一个更小的查价区间（单槽缓存随之切换）：end=01-08
    #    roll2 参考日 01-09 在 end 之后；改 IF2602@01-09 只联动 roll2 → 缓存须仍命中
    q08 = "/query?start=2026-01-05&end=2026-01-08&base=2026-01-13"
    st, _, headers = get(q08)
    check("小区间首次取数 miss（成为最近一个查价区间）",
          headers.get("X-Cache-Status") == "miss", headers.get("X-Cache-Status"))
    st, _, _ = post("/prices", {"contract_id": cid["IF2602"],
                                "date": "2026-01-09", "price": 38600})  # 38500 → 38600
    check("补改区间末日之后的报价", st == 302)
    st, html, headers = get(q08)
    check("区间外补改后仍命中(hit)", headers.get("X-Cache-Status") == "hit",
          headers.get("X-Cache-Status"))
    # 新 delta2 = 38750 - 38600 = 150；首日 = 38000 + 100 + 150 = 38250
    check("按最新价差出数 38250", "38250" in html)
    st, csv_text, headers = get(
        "/download?start=2026-01-05&end=2026-01-08&base=2026-01-13")
    check("区间外补改后 CSV 仍命中", headers.get("X-Cache-Status") == "hit",
          headers.get("X-Cache-Status"))
    check("CSV 同步 38250", "38250" in csv_text)

    # 8) 回到全区间：单槽已被 step6 的区间替换，故这里先 miss 重建；
    #    随后区间内改价 IF2602@01-07（roll1 参考日）38300 → 38360 → 缓存作废重建
    st, _, headers = get(q_full)
    check("切回全区间重新建缓存(miss)", headers.get("X-Cache-Status") == "miss",
          headers.get("X-Cache-Status"))
    st, _, _ = post("/prices", {"contract_id": cid["IF2602"],
                                "date": "2026-01-07", "price": 38360})
    check("区间内更正报价", st == 302)
    st, html, headers = get(q_full)
    check("区间内改价后缓存作废(miss)", headers.get("X-Cache-Status") == "miss",
          headers.get("X-Cache-Status"))
    # 新 delta1 = 160；roll2 在步骤 7 已变为 150：
    # 首日 = 38000 + 160(delta1) + 150(delta2) = 38310
    check("改价后按当前库出数 38310", "38310" in html)
    st, _, headers = get(q_full)
    check("重建后再次命中(hit)", headers.get("X-Cache-Status") == "hit",
          headers.get("X-Cache-Status"))
    st, csv_text, _ = get(
        "/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    check("改价后 CSV 同步 38310", "38310" in csv_text)

    # 9) 缺价场景：删除 IF2603 在 01-09 的报价（roll2 参考日新合约价）
    #    通过直接操作库模拟“尚未补录”，同时补一条 range 变更保持版本机制一致
    import sqlite3
    db_path = os.environ.get("CONT_DB_PATH", "data/continuous.db")
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM prices WHERE date='2026-01-09' "
                 "AND contract_id=(SELECT contract_id FROM contracts WHERE code='IF2603')")
    conn.execute("UPDATE rolls SET delta=NULL WHERE ord=2")
    conn.execute("INSERT INTO change_log(scope, change_day, reason) "
                 "VALUES ('range', '2026-01-09', 'simulated-missing-quote')")
    conn.commit()
    conn.close()
    st, html, _ = get(q_full)
    check("缺换月报价时留空", "待补报价：IF2603(2026-01-09)" in html)
    check("指出最早缺价换月 IF2602 → IF2603", "IF2602 → IF2603" in html)
    st, csv_text, _ = get(
        "/download?start=2026-01-12&end=2026-01-13&base=2026-01-13")
    rows = [l for l in csv_text.strip().splitlines()[1:]]
    check("换月后日期仍正常", rows[0].split(",")[3] == "38800")

    # 10) 基准日早于结束日应被拒（页面渲染错误提示，HTTP 200 + flash）
    st, html, _ = get("/query?start=2026-01-05&end=2026-01-13&base=2026-01-08")
    check("拒绝基准日早于结束日", st == 200 and "不能早于结束日" in html)

    print("\n===== 端到端试跑结果 =====")
    ok = True
    for name, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}"
              + (f"  （实际：{detail}）" if not passed and detail else ""))
        ok = ok and passed
    print("==========================")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
