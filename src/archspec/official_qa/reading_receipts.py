"""任务各次操作的不可变出处位置，不保存旧回答、正文或读取授权。"""

import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from archspec.official_documents.processing_cache import publish_json
from archspec.persistence.business_files import BusinessFilesUnavailable

UUID_PATTERN = r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'


class ReadingPosition(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source_id: str = Field(pattern=r'^(gb55031|official-[a-f0-9]{32})$')
    snapshot_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    chunk_id: str = Field(max_length=180)
    snapshot_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    title: str = Field(max_length=300)
    standard_code: str = Field(max_length=120)
    clause_number: str | None = Field(default=None, max_length=120)
    page_start: StrictInt = Field(ge=1, le=200)
    page_end: StrictInt = Field(ge=1, le=200)
    role: Literal['MATCH', 'CONTINUATION', 'RELATED_CONTEXT']

    @model_validator(mode='after')
    def fixed_identity(self):
        prefix = self.source_id+':'+self.snapshot_id+':'
        index = self.chunk_id.removeprefix(prefix)
        if (not self.chunk_id.startswith(prefix) or not index.isascii() or not index.isdecimal()
                or str(int(index)) != index or int(index) > 999999 or self.page_end < self.page_start):
            raise ValueError('出处位置身份无法核对')
        return self


class ReadingReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['QA_READING_POSITIONS_V1'] = 'QA_READING_POSITIONS_V1'
    task_id: str = Field(pattern=UUID_PATTERN)
    operation_id: str = Field(pattern=UUID_PATTERN)
    revision: StrictInt = Field(ge=1)
    project_id: str | None = Field(default=None, max_length=100)
    mode: Literal['LOCAL', 'MODEL']
    positions: list[ReadingPosition] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def unique_positions(self):
        if len({row.chunk_id for row in self.positions}) != len(self.positions):
            raise ValueError('出处位置不能重复')
        return self


def receipt_path(root, task_id, operation_id):
    task, operation = str(UUID(task_id)), str(UUID(operation_id))
    return Path(root) / 'official-qa' / 'readings' / task / f'{operation}.json'


def validate_receipt_bytes(content):
    """共享给私有业务文件签收/回读；有限结构拒绝正文、原问或任意附加字段。"""
    if not isinstance(content, bytes) or not 0 < len(content) <= 32768:
        raise ValueError('出处位置记录大小无法核对')
    return ReadingReceipt.model_validate_json(content)


def capture_positions(service, task, metadata, result):
    """在任务完成提交前保存操作位置；孤立文件不能替代数据库已完成操作收据。"""
    from archspec.persistence.business_files import write_runtime_file

    if result.get('status') not in {'REFERENCE_BRIEF', 'READING_ONLY'}:
        return 'NONE'
    positions, seen = [], set()
    try:
        for row in (result.get('evidence') or [])[:8]:
            fields = ReadingPosition.model_fields
            position = ReadingPosition.model_validate({key: row[key] for key in fields if key in row})
            if position.chunk_id not in seen:
                positions.append(position)
                seen.add(position.chunk_id)
        if not positions:
            return 'NONE'
        receipt = ReadingReceipt(task_id=task.task_id, operation_id=metadata.operation_id,
            revision=task.revision+1, project_id=task.project_id, mode=task.mode, positions=positions)
        value = receipt.model_dump(mode='json')
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')
        validate_receipt_bytes(content)
        path = receipt_path(service.documents.cache_dir.parent, task.task_id, metadata.operation_id)
        if not write_runtime_file(path, content):
            publish_json(path, value)
        return 'RECORDED'
    except (OSError, ValueError, TypeError, KeyError, AttributeError, BusinessFilesUnavailable):
        # 附加位置记录失败不重放已完成的模型或阻断原回答；恢复端诚实说明尚无记录。
        return 'UNAVAILABLE'


def load_positions(root, task, operation):
    from archspec.persistence.business_files import read_runtime_file

    base = {'schema_version': 'QA_TASK_READING_V1', 'task_id': task.task_id,
            'revision': task.revision, 'project_id': task.project_id, 'positions': []}
    if operation is None:
        return {**base, 'state': 'NOT_RECORDED'}
    path = receipt_path(root, task.task_id, operation.operation_id)
    managed, content = read_runtime_file(path, missing_ok=True)
    if not managed:
        if not path.exists():
            return {**base, 'state': 'NOT_RECORDED'}
        if path.stat().st_size > 32768:
            raise ValueError('出处位置记录大小无法核对')
        content = path.read_bytes()
    if content is None:
        return {**base, 'state': 'NOT_RECORDED'}
    receipt = validate_receipt_bytes(content)
    if (receipt.task_id != task.task_id or receipt.operation_id != operation.operation_id
            or receipt.revision != task.revision or receipt.revision != operation.result_revision
            or receipt.project_id != task.project_id or receipt.mode != task.mode):
        raise ValueError('出处位置不属于本次已提交任务操作')
    return {**base, 'state': 'AVAILABLE', 'positions': [row.model_dump(mode='json') for row in receipt.positions]}
