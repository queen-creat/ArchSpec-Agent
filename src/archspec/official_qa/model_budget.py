"""独立持久开发预算：先预留，后调用；不记录正文、密钥或模型回答。

每次预留0.10元是开发护栏，不是实际扣费。当前Flash已核验的最高峰价格
为输入2元/百万token、输出8元/百万token；16000字节输入与1024输出留有余量。
未来价格、token计算或服务行为变更必须重新核验，不能视作永久扣费保证。
"""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class DevelopmentBudgetGateway:
    """包装已有网关；同一个journal跨请求、实例和重启累计，不每日重置。"""

    def __init__(self, gateway, journal: Path):
        self.gateway = gateway
        self.journal = Path(journal)
        self._check_gateway()
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS development_budget (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                limit_cents INTEGER NOT NULL CHECK (limit_cents BETWEEN 0 AND 1200)
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS development_calls (
                id INTEGER PRIMARY KEY,
                request_digest TEXT NOT NULL,
                reserved_cents INTEGER NOT NULL CHECK (reserved_cents = 10),
                status TEXT NOT NULL CHECK (status IN ('RESERVED', 'COMPLETED', 'FAILED')),
                actual_model TEXT,
                actual_usage TEXT,
                request_id TEXT
            )""")
            # 旧账本上的限额不因新实例、日期或账户余额而提高。
            connection.execute("INSERT OR IGNORE INTO development_budget VALUES (1, 1200)")
            self._limit(connection)

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.journal, timeout=10)) as connection:
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _check_gateway(self):
        cap = getattr(self.gateway, "max_tokens", None)
        if (getattr(self.gateway, "model", None) != "deepseek-flash"
                or type(cap) is not int or not 64 <= cap <= 1024
                or getattr(self.gateway, "thinking_disabled", None) is not True):
            raise ValueError("开发预算仅允许deepseek-flash、明确关闭thinking、64–1024输出token")

    @staticmethod
    def _limit(connection):
        row = connection.execute("SELECT limit_cents FROM development_budget WHERE id = 1").fetchone()
        if row is None or type(row[0]) is not int or not 0 <= row[0] <= 1200:
            raise ValueError("开发预算账本限额无法核对")
        return row[0]

    def _reserve(self, digest):
        with self._connection() as connection:
            # 写事务锁住整个余额检查和预留；先提交再联网，不跨HTTP持有锁。
            connection.execute("BEGIN IMMEDIATE")
            limit = self._limit(connection)
            reserved = connection.execute(
                "SELECT COALESCE(SUM(reserved_cents), 0) FROM development_calls").fetchone()[0]
            if reserved + 10 > limit:
                raise ValueError("开发累计预算已用完；不会按账户余额扩大预算或自动重试")
            cursor = connection.execute("""INSERT INTO development_calls
                (request_digest, reserved_cents, status) VALUES (?, 10, 'RESERVED')""", (digest,))
            return cursor.lastrowid

    def _finish(self, call_id, status, turn=None):
        usage = self._usage(turn)
        with self._connection() as connection:
            connection.execute("""UPDATE development_calls SET
                status = ?, actual_model = ?, actual_usage = ?, request_id = ? WHERE id = ?""",
                (status, getattr(turn, "model", None),
                 json.dumps(usage, separators=(",", ":")) if usage is not None else None,
                 getattr(turn, "request_id", None), call_id))

    @staticmethod
    def _usage(turn):
        usage = getattr(turn, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        cached = getattr(usage, "cached_tokens", None)
        if (type(prompt) is not int or type(completion) is not int
                or prompt < 0 or completion < 0 or prompt + completion == 0
                or (cached is not None and (type(cached) is not int or not 0 <= cached <= prompt))):
            # 旧网关对缺失usage也可能返回0/0；不将其当实际零扣费或退款理由。
            return None
        return {"prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached}

    def complete(self, messages, tools):
        self._check_gateway()
        content = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(content) > 16000:
            raise ValueError("模型messages与tools合计不能超过16000 UTF-8字节")
        digest = hashlib.sha256(content).hexdigest()
        # 固定本次实际发送的JSON副本，不让调用方后续修改输入使摘要与发送内容不符。
        packet = json.loads(content)
        call_id = self._reserve(digest)
        try:
            turn = self.gateway.complete(packet["messages"], packet["tools"])
        except BaseException:
            # 超时/异常无法证明未扣费；只更新状态，预留永不退款，也不存异常原文。
            self._finish(call_id, "FAILED")
            raise
        self._finish(call_id, "COMPLETED", turn)
        return turn

    def metadata(self):
        with self._connection() as connection:
            connection.execute("BEGIN")
            limit = self._limit(connection)
            rows = connection.execute(
                "SELECT reserved_cents, actual_usage FROM development_calls").fetchall()
        known = [json.loads(row[1]) for row in rows if row[1] is not None]
        cached = [row["cached_tokens"] for row in known if row["cached_tokens"] is not None]
        return {
            "reserved_cny": sum(row[0] for row in rows) / 100,
            "limit_cny": limit / 100,
            "call_count": len(rows),
            "reservation_cny_per_call": 0.10,
            "budget_mode": "CUMULATIVE_DEVELOPMENT_RESERVATION",
            "actual_usage": {
                "reported_calls": len(known), "unknown_calls": len(rows) - len(known),
                "prompt_tokens": sum(row["prompt_tokens"] for row in known) if known else None,
                "completion_tokens": sum(row["completion_tokens"] for row in known) if known else None,
                "cached_tokens": sum(cached) if cached else None,
            },
            "actual_cost_cny": None,
            "notice": "预留上限不是实际扣费；未知用量和失败仍保留预留，未来费率变更须重新核验。",
        }
