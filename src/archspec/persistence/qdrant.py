"""小型 Qdrant Cloud REST 适配器；不会生成向量或接入默认搜索。

调用方须先核对正文许可、来源及固定快照。这里的模型指纹与范围核对只隔离
向量存储身份；相似度不能证明规范事实、原文完整性或工程适用性。
"""

import copy
import json
import math
import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import httpx

_CLOUD_URL = re.compile(
    r"https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"cloud\.qdrant\.io(?::(?:443|6333))?/?",
    re.IGNORECASE,
)
_IDENTITY_KEYS = ("source_id", "snapshot_id", "chunk_id", "model_fingerprint", "region")


class QdrantUnavailable(RuntimeError):
    """连接、集合合同或远程响应不可用；消息不包含远程原始信息。"""

    def __init__(self, message, *, code="UNAVAILABLE"):
        super().__init__(message)
        self.code = code


def _identity(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 4096 and value == value.strip()


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("向量点编号必须是 UUID")  # noqa: TRY004 - 统一输入校验合同
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError("向量点编号必须是 UUID") from None


def _payload(value: Any) -> dict:
    if not isinstance(value, dict) or any(not _identity(value.get(key)) for key in _IDENTITY_KEYS):
        raise ValueError("向量载荷必须包含非空来源、快照、片段、模型指纹和地域身份")
    try:
        # 先完整校验，再深复制，禁止 NaN 与无法表示为 JSON 的附加字段。
        json.dumps(value, allow_nan=False)
        return copy.deepcopy(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("向量载荷必须是有限的 JSON 数据") from None


class QdrantStore:
    """同步 REST 存储；调用方注入的 HTTP 客户端仍由调用方管理生命周期。"""

    def __init__(
        self,
        url: str,
        api_key: str,
        collection: str,
        dimension: int,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(url, str) or not _CLOUD_URL.fullmatch(url) or len(url) > 300:
            raise ValueError("Qdrant 地址必须是 HTTPS Cloud 根地址，端口只能为 443 或 6333")
        if (
            not isinstance(api_key, str)
            or not 1 <= len(api_key) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in api_key)
        ):
            raise ValueError("Qdrant 密钥必须是非空且可用于请求头的文字")
        if not isinstance(collection, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", collection):
            raise ValueError("Qdrant 集合名称只能含字母、数字、下划线或短横线")
        if type(dimension) is not int or not 1 <= dimension <= 65536:
            raise ValueError("Qdrant 向量维度必须为 1 到 65536 的整数")
        self._url = url.rstrip("/")
        self._api_key = api_key
        self.collection = collection
        self.dimension = dimension
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.Client(
                timeout=10.0,
                follow_redirects=False,
                trust_env=False,
            )
        )

    def close(self) -> None:
        """只释放适配器自行创建的连接池，不关闭调用方的客户端。"""
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, suffix: str = "", *, body=None, allow_missing=False):
        try:
            response = self._client.request(
                method,
                self._url + f"/collections/{self.collection}" + suffix,
                headers={"api-key": self._api_key},
                json=body,
                timeout=10.0,
                follow_redirects=False,
                auth=None,
            )
            if allow_missing and response.status_code == 404:
                return None
            if response.status_code in {401, 403}:
                raise QdrantUnavailable('Qdrant访问权限未通过，请核对密钥和集合权限', code="ACCESS_DENIED")
            if not 200 <= response.status_code < 300:
                raise QdrantUnavailable("Qdrant 请求未成功，请检查服务状态与访问权限")
            document = response.json()
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError, RuntimeError) as error:
            if isinstance(error, QdrantUnavailable):
                raise
            # 断开异常链，超时/解析异常的 URL、密钥与响应不得进入日志。
            raise QdrantUnavailable("Qdrant 暂时不可用或返回了无法解析的数据") from None
        if not isinstance(document, dict) or document.get("status") != "ok":
            raise QdrantUnavailable("Qdrant 返回的数据合同不正确")
        return document

    def _collection(self, *, allow_missing=False) -> bool:
        document = self._request("GET", allow_missing=allow_missing)
        if document is None:
            return False
        try:
            vectors = document["result"]["config"]["params"]["vectors"]
            matches = (
                isinstance(vectors, dict)
                and type(vectors.get("size")) is int
                and vectors["size"] == self.dimension
                and vectors.get("distance") == "Cosine"
            )
        except (KeyError, TypeError):
            matches = False
        if not matches:
            raise QdrantUnavailable("Qdrant 集合必须使用同维度的单一 Cosine 向量，拒绝覆盖", code="INCOMPATIBLE")
        return True

    def health_check(self) -> None:
        """确认既有集合可用且向量合同一致，绝不创建或覆盖集合。"""
        self._collection()

    def probe_connection(self) -> dict[str, str]:
        """只读指定集合；空集群也能证明认证读取，但不声称索引已经迁移。

        官方exists接口必须明确返回布尔值；404、跳转和坏响应都不是不存在。
        不存在时只有一次GET，存在时第二次GET复用既有维度/Cosine合同。
        """
        document = self._request("GET", "/exists")
        result = document.get("result")
        exists = result.get("exists") if isinstance(result, dict) else None
        if type(exists) is not bool:
            raise QdrantUnavailable("Qdrant集合存在性返回的数据合同不正确")
        if not exists:
            return {"api_access": "VERIFIED", "collection": "MISSING"}
        self.health_check()
        return {"api_access": "VERIFIED", "collection": "READY"}

    def ensure_collection(self, *, prepare_filter_indexes=False) -> None:
        """仅明确 404 时创建；不删除、更新或覆盖错误配置的既有集合。"""
        if type(prepare_filter_indexes) is not bool:
            raise ValueError("过滤索引准备开关必须是布尔值")
        if self._collection(allow_missing=True):
            if prepare_filter_indexes:
                self._ensure_filter_indexes()
            return
        document = self._request(
            "PUT",
            body={
                "vectors": {"size": self.dimension, "distance": "Cosine", "on_disk": True},
            },
        )
        if document.get("result") is not True:
            raise QdrantUnavailable("Qdrant 未确认创建集合，请核对服务状态")
        self.health_check()
        if prepare_filter_indexes:
            self._ensure_filter_indexes()

    def _ensure_filter_indexes(self):
        """显式写入准备阶段补齐所有实际过滤键；不关闭Cloud严格模式或改已有类型。"""
        fields = ("source_id", "snapshot_id", "model_fingerprint", "region", "migration_cut_sha256")
        def schema():
            result = self._request("GET").get("result")
            value = result.get("payload_schema") if isinstance(result, dict) else None
            if not isinstance(value, dict):
                raise QdrantUnavailable("Qdrant未返回可核验的过滤字段索引")
            return value
        before = schema()
        # 先检查整组已有类型；发现冲突之前不创建任何新索引。
        for key in fields:
            if key in before and (not isinstance(before[key], dict) or before[key].get("data_type") != "keyword"):
                raise QdrantUnavailable("Qdrant已有过滤字段索引类型不符，拒绝覆盖", code="INCOMPATIBLE")
        missing = [key for key in fields if key not in before]
        for key in missing:
            result = self._request("PUT", "/index?wait=true", body={
                "field_name": key, "field_schema": "keyword"}).get("result")
            if not isinstance(result, dict) or result.get("status") != "completed":
                raise QdrantUnavailable("Qdrant未确认过滤字段索引创建完成")
        if missing:
            after = schema()
            if any(not isinstance(after.get(key), dict) or after[key].get("data_type") != "keyword" for key in fields):
                raise QdrantUnavailable("Qdrant过滤字段索引实际回读不完整")

    def _vector(self, vector: Any) -> list[float]:
        if not isinstance(vector, (list, tuple)) or len(vector) != self.dimension:
            raise ValueError("向量长度必须与已配置维度一致")
        try:
            finite = all(type(value) in (int, float) and math.isfinite(value) for value in vector)
            values = [float(value) for value in vector] if finite else []
            norm = math.hypot(*values)
        except (ValueError, OverflowError):
            norm = 0
        if not norm or not math.isfinite(norm):
            raise ValueError("向量必须由有限数值组成且范数有限、大于零")
        return values

    def upsert(self, points: Sequence[dict]) -> None:
        """一次提交最多 128 点；完整批次先校验，避免部分写入后才发现坏身份。"""
        if not isinstance(points, (list, tuple)) or len(points) > 128:
            raise ValueError("向量写入批次只能为最多 128 点的列表")
        prepared = []
        identifiers = set()
        for point in points:
            if not isinstance(point, dict):
                raise ValueError("向量点必须包含 UUID、向量和固定身份载荷")  # noqa: TRY004
            identifier = _uuid(point.get("id"))
            if identifier in identifiers:
                raise ValueError("同一写入批次不能包含重复 UUID")
            identifiers.add(identifier)
            prepared.append(
                {
                    "id": identifier,
                    "vector": self._vector(point.get("vector")),
                    "payload": _payload(point.get("payload")),
                }
            )
        if not prepared:
            return
        document = self._request("PUT", "/points?wait=true", body={"points": prepared})
        result = document.get("result")
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise QdrantUnavailable("Qdrant 未确认向量写入完成")

    def retrieve_points(self, identifiers):
        """按明确UUID批量回读全部向量与载荷，允许缺点由调用方判定未迁齐。"""
        if not isinstance(identifiers, (list, tuple)) or not 1 <= len(identifiers) <= 128:
            raise ValueError("向量回读必须明确指定1至128个UUID")
        ids = [_uuid(value) for value in identifiers]
        if len(set(ids)) != len(ids):
            raise ValueError("向量回读编号不能重复")
        document = self._request("POST", "/points", body={
            "ids": ids, "with_payload": True, "with_vector": True})
        rows = document.get("result")
        if not isinstance(rows, list) or len(rows) > len(ids):
            raise QdrantUnavailable("Qdrant向量回读格式或数量无效")
        seen = set()
        try:
            for row in rows:
                identifier = _uuid(row.get("id"))
                if identifier not in ids or identifier in seen:
                    raise ValueError
                self._vector(row.get("vector"))
                _payload(row.get("payload"))
                seen.add(identifier)
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise QdrantUnavailable("Qdrant向量回读身份或内容无法核对") from None
        return copy.deepcopy(rows)

    def count_points(self, *, migration_cut_sha256):
        """精确计数本迁移切面的全部点；不以Top K或集合估算数量代替。"""
        if not isinstance(migration_cut_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", migration_cut_sha256):
            raise ValueError("向量计数必须指定固定迁移SHA256")
        document = self._request("POST", "/points/count", body={
            "filter": {"must": [{"key": "migration_cut_sha256", "match": {"value": migration_cut_sha256}}]},
            "exact": True})
        result = document.get("result")
        count = result.get("count") if isinstance(result, dict) else None
        if type(count) is not int or count < 0:
            raise QdrantUnavailable("Qdrant向量精确计数无法核对")
        return count

    def query(
        self,
        vector: Sequence[float],
        *,
        scope_pairs: Sequence[tuple[str, str]],
        model_fingerprint: str,
        region: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """服务端先按来源/快照精确配对过滤；返回前再次核对每个命中身份。

        不能使用两个独立的 any 列表，否则来源与快照会发生交叉组合。
        返回相似度仅用于候选排序，调用方仍须核对固定原文及证据权限。
        """
        values = self._vector(vector)
        if not isinstance(scope_pairs, (list, tuple)) or not 1 <= len(scope_pairs) <= 128:
            raise ValueError("查询必须明确提供 1 到 128 个来源与快照配对，不能查询全库")
        pairs = []
        for pair in scope_pairs:
            if (
                not isinstance(pair, (list, tuple))
                or len(pair) != 2
                or any(not _identity(value) for value in pair)
            ):
                raise ValueError("查询范围必须是非空来源与快照的精确配对")
            if tuple(pair) not in pairs:
                pairs.append(tuple(pair))
        if not _identity(model_fingerprint) or (region is not None and not _identity(region)):
            raise ValueError("查询必须提供固定模型指纹，指定地域时地域也不能为空")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("查询结果上限必须为 1 到 100 的整数")
        must = [{"key": "model_fingerprint", "match": {"value": model_fingerprint}}]
        if region is not None:
            must.append({"key": "region", "match": {"value": region}})
        must.append(
            {
                "should": [
                    {
                        "must": [
                            {"key": "source_id", "match": {"value": source_id}},
                            {"key": "snapshot_id", "match": {"value": snapshot_id}},
                        ]
                    }
                    for source_id, snapshot_id in pairs
                ]
            }
        )
        document = self._request(
            "POST",
            "/points/query",
            body={
                "query": values,
                "limit": limit,
                "with_payload": True,
                "with_vector": False,
                "filter": {"must": must},
            },
        )
        result = document.get("result")
        hits = result.get("points") if isinstance(result, dict) else None
        if not isinstance(hits, list) or len(hits) > limit:
            raise QdrantUnavailable("Qdrant 查询结果格式或数量不正确")
        identifiers = set()
        for hit in hits:
            try:
                if not isinstance(hit, dict):
                    raise ValueError  # noqa: TRY004 - 远程坏类型统一转安全错误
                identifier = _uuid(hit.get("id"))
                payload = _payload(hit.get("payload"))
                score = hit.get("score")
                if (
                    identifier in identifiers
                    or type(score) not in (int, float)
                    or not math.isfinite(score)
                ):
                    raise ValueError
                if (
                    (payload["source_id"], payload["snapshot_id"]) not in pairs
                    or payload["model_fingerprint"] != model_fingerprint
                    or (region is not None and payload["region"] != region)
                ):
                    raise ValueError
                identifiers.add(identifier)
            except (ValueError, OverflowError):
                raise QdrantUnavailable("Qdrant 命中的固定身份或数值不符合本次查询范围") from None
        return copy.deepcopy(hits)
