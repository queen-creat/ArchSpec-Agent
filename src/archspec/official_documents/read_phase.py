"""只暂存一次读取阶段的已验证内容；不保存许可、目录或原件校验结论。"""

import json
from copy import deepcopy


class _ValidatedJSONRecord:
    """原字节不可变；惰性内部对象与每个外部解析对象分别归属。"""

    __slots__ = ("borrowed_record", "content")

    def __init__(self, content):
        self.content = content
        self.borrowed_record = None

    def read(self, *, copy_record):
        if copy_record:
            # JSON原始值天然可重新解析；不返回浅引用或递归复制整本结构。
            return json.loads(self.content)
        if self.borrowed_record is None:
            # 仅两个既有权限历史入口可借用；首轮调用方对象从未存入这里。
            self.borrowed_record = json.loads(self.content)
        return self.borrowed_record


class ReadPhase:
    def __init__(self, *, max_records=128, max_bytes=32 * 1024 * 1024):
        if type(max_records) is not int or type(max_bytes) is not int or min(max_records, max_bytes) < 0:
            raise ValueError("读取暂存预算必须是非负整数")
        self.max_records, self.max_bytes = max_records, max_bytes
        self.records, self.byte_count = {}, 0
        # 原JSON仍沿用128条/32MiB预算；内部借用只保留当前一份展开图，
        # 避免多本带坐标的识别行按书数常驻。切换后仍可从已核原字节重建。
        self._borrowed_json_record = None

    def get(self, key, *, copy_record=True):
        # 调用者拿到的字典可改，暂存的原文不能跟着被改掉。
        # 仅权限历史的两处内部只读入口可借用原记录；不能把借用对象返回给网页或模型。
        record = self.records.get(key)
        if record is None:
            return None
        if isinstance(record, _ValidatedJSONRecord):
            if not copy_record and self._borrowed_json_record is not record:
                if self._borrowed_json_record is not None:
                    self._borrowed_json_record.borrowed_record = None
                self._borrowed_json_record = record
            return record.read(copy_record=copy_record)
        return deepcopy(record) if copy_record else record

    def put(self, key, record, size):
        # 保留通用对象原语；写入方与读取方仍不能污染内部对象。
        if len(self.records) < self.max_records and self.byte_count + size <= self.max_bytes:
            self.records[key] = deepcopy(record)
            self.byte_count += size

    def put_validated_json(self, key, content):
        """只接收调用方已核验的不可变原JSON字节；不在此保存可变首次对象。"""
        if type(content) is not bytes:
            raise TypeError("已验证JSON暂存只接受不可变原始bytes")
        size = len(content)
        # 与通用put共用原字节及条目上限。预算不足只不暂存，不替代完整校验。
        # 私有惰性解析对象有额外内存开销；不据其大小提高128条/32MiB上限。
        if len(self.records) < self.max_records and self.byte_count + size <= self.max_bytes:
            self.records[key] = _ValidatedJSONRecord(content)
            self.byte_count += size
