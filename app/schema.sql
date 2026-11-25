-- 连续合约结算价库结构
-- 设计要点：
--   contracts.ord = 0 为初始合约；第 k 次换月启用的合约 ord = k（k >= 1）
--   rolls 记录每次换月；delta 为换月价差（新合约价 - 旧合约价，取切换前一交易日）
--   bit_sums 是树状数组（Fenwick tree）辅助表，前缀和/单点更新均为 O(log n)
--   连续价不落地存储，取数时实时计算，改价不逐日回写

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS contracts (
    contract_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    ord  INTEGER NOT NULL UNIQUE      -- 0=初始合约，k=第 k 次换月启用的合约
);

CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_contract_id INTEGER REFERENCES contracts(contract_id)
);
INSERT OR IGNORE INTO meta (id) VALUES (1);

CREATE TABLE IF NOT EXISTS rolls (
    ord              INTEGER PRIMARY KEY,          -- 第几次换月，1..n 稠密
    switch_date      TEXT NOT NULL UNIQUE,         -- 切换日（当日起启用新合约）
    pre_date         TEXT NOT NULL,                 -- 切换前一交易日
    new_contract_id  INTEGER NOT NULL REFERENCES contracts(contract_id),
    delta            INTEGER                        -- 新合约价 - 旧合约价，缺报价时为 NULL
);

-- Fenwick 树：pos 为树状数组下标，val 为区间累加值；仅覆盖非空 delta
CREATE TABLE IF NOT EXISTS bit_sums (
    pos INTEGER PRIMARY KEY,
    val INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar (
    date TEXT PRIMARY KEY      -- 仅登记交易日，ISO 格式 YYYY-MM-DD
);

CREATE TABLE IF NOT EXISTS prices (
    contract_id INTEGER NOT NULL REFERENCES contracts(contract_id),
    date        TEXT NOT NULL,
    price       INTEGER NOT NULL,                    -- 整数分结算价
    PRIMARY KEY (contract_id, date)
) WITHOUT ROWID;

-- 连续价取数缓存（网页查询与 CSV 下载共用，单槽：最近一个查价区间）
-- 缓存只存“区间末日及之前”的数据状态；末日之后的换月价差异动不使缓存失效，
-- 命中时按本次基准日读取最新 roll/bit_sums（O(log n)）出数。
CREATE TABLE IF NOT EXISTS range_cache (
    cache_id     INTEGER PRIMARY KEY CHECK (cache_id = 1),
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    -- 构建该缓存时已看到的最新变更版本；命中要求版本之后没有影响本区间的变更
    built_ver    INTEGER NOT NULL,
    built_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS range_cache_rows (
    cache_id     INTEGER NOT NULL REFERENCES range_cache(cache_id) ON DELETE CASCADE,
    day          TEXT NOT NULL,
    contract     TEXT NOT NULL,          -- 构建时在用合约代码（计划变更必作废缓存）
    ord          INTEGER NOT NULL,       -- 当日在用合约 ord
    lo_ord       INTEGER NOT NULL,       -- 该日之后首个换月 ord = ord + 1
    raw_price    INTEGER,                -- 当日原价，缺价为 NULL
    stable_adj   INTEGER,                -- 切换日 <= 区间末日部分的价差和（稳定窗口）
    status       TEXT NOT NULL,          -- ok / missing_raw / missing_roll
    message      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (cache_id, day)
) WITHOUT ROWID;

-- 数据变更日志：每条写事务提交一条，版本严格递增。
-- scope='range' 仅影响含该交易日的查价区间；scope='config' 使全部缓存作废
-- （日历/计划变更后取数必须与按当前库直接查询一致）。
CREATE TABLE IF NOT EXISTS change_log (
    ver          INTEGER PRIMARY KEY,
    scope        TEXT NOT NULL CHECK (scope IN ('range', 'config')),
    change_day   TEXT,                   -- scope='range' 时被改报价所在交易日
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_change_day ON change_log(change_day);

CREATE INDEX IF NOT EXISTS idx_rolls_switch  ON rolls(switch_date);
CREATE INDEX IF NOT EXISTS idx_rolls_predate ON rolls(pre_date);
-- 部分索引：快速定位区间内最早缺价差的换月
CREATE INDEX IF NOT EXISTS idx_rolls_delta_null ON rolls(ord) WHERE delta IS NULL;
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);
