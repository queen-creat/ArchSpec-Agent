"""受控一次性迁移入口；连接原AccessGate捕获/发布，不提供公开HTTP导出。"""
from __future__ import annotations

import argparse
import json
import os
import re
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path

from archspec.persistence import activation, evidence_manifest
from archspec.persistence.control_migration import _decode

_SCHEMA = "SERVER_CLOUD_MIGRATION_PLAN_V1"
_FIELDS = {"schema_version", "migration_id", "generation", "expected_active_sha256",
    "authorized_project_ids", "archive_private_files", "exclusive_source_confirmed", "expected_model_fingerprint"}
_SAFE = "迁移未确认；保留操作记录和原源数据，未自动切换或重新启用源写入"


class MigrationOperatorUnavailable(RuntimeError):
    """只携带安全阶段说明，不回显密钥、路径、SQL或原文。"""


def safe_diagnostic(failure):
    """诊断仅含固定ASCII代码标识与包内行号；绝不读取消息、局部量或SQL。"""
    package = Path(__file__).resolve().parents[1]
    chain, seen = [], set()
    def identifier(value, fallback):
        return value if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", value) else fallback
    while isinstance(failure, BaseException) and len(chain) < 5 and id(failure) not in seen:
        seen.add(id(failure))
        frames = []
        trace = failure.__traceback__
        while trace is not None and len(frames) < 24:
            code = trace.tb_frame.f_code
            try:
                path = Path(code.co_filename).resolve()
                internal = path.is_relative_to(package)
            except (OSError, ValueError):
                internal = False
            if internal:
                filename = path.name
                if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*\.py", filename):filename = "module.py"
                frames.append({"file": filename, "function": identifier(code.co_name, "anonymous"),
                               "line": trace.tb_lineno})
            trace = trace.tb_next
        record = {"exception": identifier(type(failure).__name__, "Exception"), "frames": frames}
        # 只读严格数值状态，不记录异常消息、响应正文、URL或局部变量。
        status = getattr(failure, "http_status", None)
        if type(status) is int and 100 <= status <= 599:
            record["http_status"] = status
        chain.append(record)
        # from None只控制Python显示，不抹去定位包装错误所需的原始context。
        failure = failure.__cause__ if failure.__cause__ is not None else failure.__context__
    return {"chain": chain}


def read_plan(path):
    """操作计划只有身份和授权，不接受目标地址、DSN或模型请求。"""
    path = evidence_manifest._root(path)
    if not path.is_file() or path.stat().st_size > 64 * 1024:
        raise MigrationOperatorUnavailable(_SAFE)
    plan = _decode(path.read_bytes())
    if (set(plan) != _FIELDS or plan["schema_version"] != _SCHEMA
            or plan["archive_private_files"] is not True or plan["exclusive_source_confirmed"] is not True
            or not isinstance(plan["authorized_project_ids"], list)
            or len(plan["authorized_project_ids"]) > 10_000
            or len(set(plan["authorized_project_ids"])) != len(plan["authorized_project_ids"])):
        raise MigrationOperatorUnavailable(_SAFE)
    activation._uuid(plan["migration_id"])
    activation._generation(plan["generation"])
    activation._digest(plan["expected_model_fingerprint"])
    if plan["expected_active_sha256"] is not None:activation._digest(plan["expected_active_sha256"])
    for project in plan["authorized_project_ids"]:activation._uuid(project)
    return plan


@contextmanager
def configured_targets(work):
    """仅环境配置建立目标，保留完整TLS校验；没有SQLite或公开桶降级。"""
    from archspec.persistence import config, postgres
    from archspec.persistence.chunked import ChunkedObjectStore
    pg = postgres.load_postgres_settings()
    storage = config.load_supabase_settings()
    index = config.load_qdrant_settings()
    if pg is None or storage is None or index is None:raise MigrationOperatorUnavailable(_SAFE)
    with ExitStack() as resources:
        engine = postgres.create_postgres_runtime_engine(pg)
        resources.callback(engine.dispose)
        # 正式工厂只产生PostgreSQL；SQLite分支供明确替换外部驱动的集成测试。
        schema = {"postgresql": "archspec_control", "sqlite": "main"}.get(engine.dialect.name)
        if schema is None:raise MigrationOperatorUnavailable(_SAFE)
        selected = engine.execution_options(schema_translate_map={None: schema})
        raw = config._supabase_objects(storage);resources.callback(raw.close)
        vectors = config._vectors(index);resources.callback(vectors.close)
        yield selected, ChunkedObjectStore(raw, work / "parts"), vectors


def migrate_server(server, plan, work, *, progress):
    """同一实际服务器对象准备固定语料并停写；成功/失败都不擅自恢复源。"""
    from archspec.embeddings.provider import PinnedFastEmbedProvider
    from archspec.official_documents.acquisition_router import get_runtime
    from archspec.official_documents.semantic_experiment import prepare_semantic_corpus
    from archspec.standard_governance.compatibility import ContentUseDenied
    progress("MODEL")
    directory = Path(os.getenv("ARCHSPEC_EMBEDDING_MODEL_DIR", "/opt/archspec-models/bge-small-zh-v1.5"))
    provider = PinnedFastEmbedProvider(directory)
    if provider.model_fingerprint != plan["expected_model_fingerprint"]:
        raise MigrationOperatorUnavailable(_SAFE)
    worker = get_runtime(server.application)
    documents = worker.documents
    progress("PREPARE")
    cached = os.environ.get("ARCHSPEC_MIGRATION_VECTOR_CACHE_DIR")
    cache_options = {}
    if cached is not None:
        # 一旦配置预编码目录，必须完整命中已有缓存；错误不能退回Free现场编码。
        if not cached.strip():raise MigrationOperatorUnavailable(_SAFE)
        directory = evidence_manifest._root(Path(cached))
        if not directory.is_dir():raise MigrationOperatorUnavailable(_SAFE)
        cache_options = {"index_dir": directory, "require_cache": True, "expected_dimension": 512}
    snapshots = []
    # 元信息准备也在原停写门内；不下载、不识别、不从公告猜正文。
    with worker.gate.write("METADATA"), documents.read_phase():
        for source in sorted(worker.registry.list(), key=lambda row: row["source_id"]):
            snapshot = documents._read_snapshot(source["source_id"])
            if snapshot is None or source["kind"] != "FULL_TEXT":continue
            try:
                for operation in ("LOCAL_SEARCH", "LOCAL_EXCERPT"):
                    documents._check_read(source["source_id"], snapshot["snapshot_id"], operation)
                documents._metadata_filter_profile(snapshot)
            except ContentUseDenied:continue
            snapshots.append(snapshot)
        corpus = prepare_semantic_corpus(snapshots, provider=provider, **cache_options)
    progress("CAPTURE")
    source = server.capture_source(work / "source", keep_frozen=True)
    # 复制件和所有控制状态由原对象再次核验；不能拿页面副本当捕获包。
    source.verify()
    progress("PUBLISH")
    with configured_targets(work) as (engine, objects, vectors):
        receipt = source.publish_cloud(engine, migration_id=plan["migration_id"], generation=plan["generation"],
            authorized_project_ids=plan["authorized_project_ids"], corpus=corpus,
            expected_model_fingerprint=plan["expected_model_fingerprint"], object_store=objects, vector_store=vectors,
            staging_root=work / "publication", expected_active_sha256=plan["expected_active_sha256"],
            archive_private_files=plan["archive_private_files"])
    return {"state": receipt.state, "active": asdict(receipt.active), "asset_count": receipt.asset_count,
            "vector_count": receipt.vector_count, "metadata_sha256": receipt.metadata_sha256,
            "source_manifest_sha256": source.manifest_sha256}



def captured_source_sha(records):
    """只读核捕获包；不反序列化内部签发对象，更不据此重新发布。"""
    from archspec.persistence.source_capture import _inventory
    root = evidence_manifest._root(records)
    content = evidence_manifest._root(root / "manifest.json").read_bytes()
    manifest = _decode(content)
    if manifest["schema_version"] != "SERVER_SOURCE_CAPTURE_V1":raise MigrationOperatorUnavailable(_SAFE)
    for name, key in (("main-control.json", "main_sha256"), ("budget-control.json", "budget_sha256"),
                      ("runtime-state.json", "runtime_sha256")):
        if evidence_manifest._sha(evidence_manifest._root(root / name)) != manifest[key]:
            raise MigrationOperatorUnavailable(_SAFE)
    if _inventory(root / "files") != manifest["files"]:raise MigrationOperatorUnavailable(_SAFE)
    import hashlib
    return hashlib.sha256(content).hexdigest()


def verify_published(plan, work, records):
    """未知提交只读复核全部对象、向量和当前控制关系，不回退旧任务或重放。"""
    from archspec.persistence.remote_startup import restore_published_generation
    captured = captured_source_sha(records)
    with configured_targets(work) as (engine, objects, vectors):
        objects.store.health_check()
        restored = restore_published_generation(engine, objects, vectors, parent_dir=work)
        try:
            metadata = restored.metadata
            if (metadata["migration_id"] != plan["migration_id"] or metadata["generation"] != plan["generation"]
                    or metadata["vector_index"]["model_fingerprint"] != plan["expected_model_fingerprint"]
                    or metadata["source_closure"]["source_manifest_sha256"] != captured
                    or sorted(metadata["source_identity"]["registration"]["authorized_project_ids"])
                        != sorted(plan["authorized_project_ids"])):
                raise MigrationOperatorUnavailable(_SAFE)
            # 与真实网站同一构造，包含后加租约/模型阶段、权限、预算及同库身份。
            restored.verified_runtime_bindings().validate_selection()
            # 合法运行后的任务/权限/预算以当前状态为准，不要求退回旧revision或额度。
            return {"state": "CLOUD_GENERATION_VERIFIED", "active": asdict(restored.active),
                "asset_count": len(metadata["asset_manifest"]["entries"]), "vector_count": metadata["vector_index"]["count"],
                "current_asset_count": sum(1 for path in restored.data_dir.rglob("*") if path.is_file()),
                "current_vector_count": restored.vector_count, "source_manifest_sha256": captured}
        finally:restored.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="独占完整本机源的一次性迁云操作；不启动网站或自动切换")
    parser.add_argument("--verify-published", action="store_true")
    parser.add_argument("--source-records", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    phase = "PRECHECK"
    work = None
    try:
        from archspec.deployment import create_server_app, load_backend_mode, load_server_settings
        plan = read_plan(args.plan)
        if args.verify_published:
            phase = "VERIFY"
            if args.source_records is None:raise MigrationOperatorUnavailable(_SAFE)
            source_root = evidence_manifest._root(args.source_records)
            if not source_root.is_dir():raise MigrationOperatorUnavailable(_SAFE)
        else:
            if args.source_records is not None:raise MigrationOperatorUnavailable(_SAFE)
            settings = load_server_settings()
            source_root = evidence_manifest._root(settings.data_dir)
            if (load_backend_mode() != "LOCAL" or any(not evidence_manifest._root(source_root / name).is_file()
                    for name in ("archspec.db", "model-budget.sqlite3"))):
                raise MigrationOperatorUnavailable(_SAFE)
        requested = evidence_manifest._root(args.output_root)
        if (requested.exists() or not requested.parent.is_dir()
                or requested.is_relative_to(source_root) or source_root.is_relative_to(requested)):
            raise MigrationOperatorUnavailable(_SAFE)
        requested.mkdir()
        work = requested
        def progress(value):
            nonlocal phase
            phase = value
            with (work / "progress.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"stage": value}, ensure_ascii=False) + "\n")
        if args.verify_published:
            progress("VERIFY")
            result = verify_published(plan, work, source_root)
        else:
            progress("PRECHECK")
            # 迁移命令绝不以演示种子补缺记录；必须先取得完整一致源且独占它。
            server = create_server_app(bootstrap_seed=False)
            result = migrate_server(server, plan, work, progress=progress)
        with (work / "result.json").open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as failure:  # noqa: BLE001 - 只输出脱敏代码位置，不回显上游消息。
        result = {"state": "FAILED_OR_UNKNOWN", "stage": phase, "message": _SAFE,
                  "diagnostic": safe_diagnostic(failure)}
        if work is not None:
            try:
                with (work / "failure.json").open("x", encoding="utf-8") as output:json.dump(result, output, ensure_ascii=False)
            except OSError:pass  # 保留首个失败；不覆盖原记录，不假称目标未提交。
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
