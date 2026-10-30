"""规范治理接口；所有写路径默认关闭，只接受服务端已有记录。"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from archspec.config import settings
from archspec.database import get_session
from archspec.standard_governance.auth import maintenance_status, require_standard_maintainer
from archspec.standard_governance.schemas import (
    Operation,
    RevokeVerificationRequest,
    SourceFromRecordRequest,
    SubjectKind,
    VerificationRequest,
    VersionRelationRequest,
)


class GovernanceRoute(APIRoute):
    """请求不合法时返回稳定错误码，不反射输入或可能混入的凭据。"""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError as error:
                return JSONResponse(status_code=422, content={"detail": {
                    "code": "INVALID_VERIFICATION_REQUEST", "message": "核验请求字段或范围不合法。",
                    "errors": [{"loc": list(item["loc"]), "type": item["type"]}
                               for item in error.errors()],
                }})
        return safe_handler


router = APIRouter(prefix="/api/standard-governance", tags=["规范来源与版本核验"],
                   route_class=GovernanceRoute)
DatabaseSession = Annotated[Session, Depends(get_session)]
Maintainer = Annotated[str, Depends(require_standard_maintainer)]
_WRITE_GUARD = [Depends(require_standard_maintainer)]


def _call(operation: Callable):
    # 内部服务也执行同一合同；HTTP 层只做错误转换，不替服务绕过事务检查。
    from archspec.standard_governance.models import GovernanceError

    try:
        return operation()
    except GovernanceError as error:
        raise HTTPException(status_code=error.status_code,
                            detail={"code": error.code, "message": error.message}) from error


def _service(session):
    from archspec.standard_governance.service import GovernanceService

    return GovernanceService(session)


@router.get("/status")
def governance_status(session: DatabaseSession):
    return {**_call(lambda: _service(session).status()), **maintenance_status(),
            "applicability_status": "NOT_REVIEWED", "shared_mode_supported": False}


@router.get("/standards")
def list_standards(session: DatabaseSession):
    return _call(lambda: _service(session).list_standards())


@router.get("/versions/{version_id}")
def get_version(version_id: str, session: DatabaseSession):
    return _call(lambda: _service(session).get_version(version_id))


@router.get("/sources/{source_id}")
def get_source(source_id: str, session: DatabaseSession):
    return _call(lambda: _service(session).get_source(source_id))


@router.get("/artifacts/{artifact_id}/admission")
def artifact_admission(artifact_id: str, session: DatabaseSession,
                       operation: Operation = "LOCAL_EXCERPT"):
    return _call(lambda: _current_artifact_admission(artifact_id, session, operation))


def _current_artifact_admission(artifact_id: str, session: Session, operation: str):
    """资格页面也重新检查原件/旧登记，不能只显示数据库里曾经通过的批准。"""
    from archspec.standard_governance.compatibility import LegacyAdmissionAdapter

    service = _service(session)
    operations = (operation,)
    if not service.repo.available():
        return service.admission.evaluate(artifact_id, operations=operations)
    artifact = service.get_artifact(artifact_id)
    adapter = LegacyAdmissionAdapter(session, settings.data_dir)
    if artifact["legacy_document_id"]:
        current = adapter.for_document(artifact["legacy_document_id"], operations)
    elif artifact["legacy_source_id"] and artifact["legacy_snapshot_id"]:
        current = adapter.for_snapshot(artifact["legacy_source_id"], artifact["legacy_snapshot_id"],
                                       operations)
    else:
        return service.admission.evaluate(artifact_id, operations=operations,
                                          runtime_issues=("FULL_TEXT_TRACE_UNRELIABLE",))
    if current["artifact_binding_id"] != artifact_id:
        # 新绑定已经核验，也不能把新绑定资格偷偷显示成旧 artifact 的批准。
        return service.admission.evaluate(artifact_id, operations=operations,
                                          runtime_issues=("MANIFEST_CHANGED", *current["reasons"]))
    return current


@router.get("/subjects/{kind}/{subject_id}/verifications")
def verification_history(kind: SubjectKind, subject_id: str, session: DatabaseSession):
    return _call(lambda: _service(session).history(kind, subject_id))


@router.post("/sources/from-record", status_code=201, dependencies=_WRITE_GUARD)
def source_from_record(payload: SourceFromRecordRequest, actor: Maintainer,
                       session: DatabaseSession):
    from archspec.standard_governance.imports import GovernanceImportService

    try:
        # actor 已由维护依赖核对。此接口只登记观察，不制造 VERIFY 事件。
        return _call(lambda: GovernanceImportService(session, settings.data_dir).from_record(
            payload.kind, payload.key, snapshot_id=payload.snapshot_id
        ))
    except LookupError as error:
        raise HTTPException(status_code=404, detail={
            "code": "GOVERNANCE_SUBJECT_NOT_FOUND", "message": "服务端没有指定的登记记录。"
        }) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_VERIFICATION_REQUEST", "message": "已有记录无法安全登记为规范来源。"
        }) from error


@router.post("/verifications", status_code=201, dependencies=_WRITE_GUARD)
def verify(payload: VerificationRequest, actor: Maintainer, session: DatabaseSession):
    return _call(lambda: _service(session).verify(actor, payload.model_dump(mode="json")))


@router.post("/verifications/{event_id}/revoke", status_code=201, dependencies=_WRITE_GUARD)
def revoke(event_id: str, payload: RevokeVerificationRequest, actor: Maintainer,
           session: DatabaseSession):
    return _call(lambda: _service(session).revoke(actor, event_id, payload.model_dump(mode="json")))


@router.post("/version-relations", status_code=201, dependencies=_WRITE_GUARD)
def record_relation(payload: VersionRelationRequest, actor: Maintainer, session: DatabaseSession):
    return _call(lambda: _service(session).record_relation(actor, payload.model_dump(mode="json")))
