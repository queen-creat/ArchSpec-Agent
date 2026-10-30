"""公开官方资产的不可变清单；隔离恢复不会激活来源、许可或预算。

此层只传输调用方明确选定的官方资产，不能证明整库完整、公开转载许可，
也不保存项目资料或事务状态。服务启动接入须另验来源目录、权限和预算。
"""
import hashlib
import json
import re
import shutil
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from itertools import islice
from pathlib import Path, PurePosixPath
from uuid import uuid4

SCHEMA = "OFFICIAL_ASSET_BUNDLE_V1"
SERVER_SCHEMA = "SERVER_BUSINESS_FILES_V1"
SERVER_FOLDERS = frozenset({"official-qa", "official-cache", "official-acquisitions", "official-discovery",
    "official-evaluations", "uploads", "qa-task-backups", "standard-governance-backups"})
MAX_FILES = 20_000
MAX_OBJECT_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
_DIGEST = r"[a-f0-9]{64}"
_SOURCE = r"(?:gb55031|official-[a-f0-9]{32})"
_PATH = re.compile(
    rf"official-cache/(?:{_SOURCE}/(?:{_DIGEST}\.(?:pdf|json)|latest\.json)"
    rf"|content-store/(?:blobs/{_DIGEST}\.(?:pdf|html)|(?:extractions|snapshots)/{_DIGEST}\.json)"
    rf"|page-images/{_SOURCE}/{_DIGEST}/{_DIGEST}/{_DIGEST}/[1-9][0-9]{{0,3}}\.(?:png|json)"
    rf"|source-metadata/(?:observations/{_DIGEST}\.json"
    rf"|bindings/{_SOURCE}/{_DIGEST}\.json))"
)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _root(value):
    path = Path(value).absolute()
    # 检查resolve前的路径链；否则链接会隐藏实际的外部目标。
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("资产目录不能通过符号链接指向其他资料")
    return path.resolve()


def _relative(value, *, schema=SCHEMA):
    if schema == SERVER_SCHEMA:
        # 私有业务备份采用独立合同；不能借公开官方包路径外传项目或凭据。
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ValueError("业务文件路径无效")
        parts = PurePosixPath(value)
        if (parts.is_absolute() or len(parts.parts) < 2 or str(parts) != value
                or any(p in {".", ".."} for p in value.split("/"))
                or parts.parts[0] not in SERVER_FOLDERS
                or any(ord(c) < 32 or c in '\\:<>"|?*' for c in value)
                or any(p.endswith((" ", ".")) for p in parts.parts)
                or parts.suffix.lower() in {".key", ".pem", ".env"}
                or any(p.startswith(".env") for p in parts.parts)):
            raise ValueError("业务文件路径或凭据隔离无法核对")
        return value
    if schema != SCHEMA or not isinstance(value, str) or not _PATH.fullmatch(value):
        raise ValueError("仅允许明确选定的官方固定资产路径，禁止项目资料与控制状态")
    return value


def _file(root, relative, *, schema=SCHEMA):
    path = root / _relative(relative, schema=schema)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("资产文件或父目录不能是符号链接")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise FileNotFoundError("选定官方资产不存在或不是普通文件")
    return path


def _check_manifest(manifest):
    if (not isinstance(manifest, dict) or set(manifest) != {
            "schema_version", "entries", "manifest_id"} or manifest["schema_version"] not in {SCHEMA, SERVER_SCHEMA}
            or not isinstance(manifest["manifest_id"], str)
            or not re.fullmatch(_DIGEST, manifest["manifest_id"])):
        raise ValueError("固定资产清单格式无效")
    entries = manifest["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError("固定资产清单必须包含预算内的完整选定文件列表")
    seen = set()
    identities = {}
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
                "path", "sha256", "size_bytes", "object_key"}:
            raise ValueError("资产条目字段无效")
        relative = _relative(entry["path"], schema=manifest["schema_version"])
        digest, size = entry["sha256"], entry["size_bytes"]
        if (relative in seen or not isinstance(digest, str) or not re.fullmatch(_DIGEST, digest)
                or type(size) is not int or not 0 <= size <= MAX_OBJECT_BYTES
                or entry["object_key"] != "evidence/blobs/" + digest):
            raise ValueError("资产身份、重复路径或预算无法核对")
        identity = (digest, size)
        previous = identities.setdefault(entry["object_key"], identity)
        if previous != identity:
            raise ValueError("同一对象键的指纹或长度冲突，未去重或开始传输")
        seen.add(relative)
        total += size
    if total > MAX_TOTAL_BYTES or [item["path"] for item in entries] != sorted(seen):
        raise ValueError("资产总量超限或清单顺序无效")
    identity = {"schema_version": manifest["schema_version"], "entries": entries}
    if hashlib.sha256(_canonical(identity)).hexdigest() != manifest["manifest_id"]:
        raise ValueError("固定资产清单内容指纹不一致")
    return entries


def build_manifest(root, relative_paths, *, schema=SCHEMA):
    """流式记录真实字节；不遍历整个data，也不把其他资料自动加入公开包。"""
    root = _root(root)
    if not isinstance(relative_paths, (list, tuple)) or not 1 <= len(relative_paths) <= MAX_FILES:
        raise ValueError("必须明确选择预算内的官方资产")
    paths = [_relative(item, schema=schema) for item in relative_paths]
    if len(set(paths)) != len(paths):
        raise ValueError("固定资产路径不能重复")
    entries = []
    for relative in sorted(paths):
        path = _file(root, relative, schema=schema)
        size = path.stat().st_size
        if size > MAX_OBJECT_BYTES:
            raise ValueError("选定资产超过单文件预算")
        digest = _sha(path)
        # 同时核大小，防止读取过程中明显变化的资产被当成固定输入。
        if path.stat().st_size != size:
            raise ValueError("选定资产在建立清单期间改变")
        entries.append({"path": relative, "sha256": digest, "size_bytes": size,
                        "object_key": "evidence/blobs/" + digest})
    manifest = {"schema_version": schema, "entries": entries}
    manifest["manifest_id"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    _check_manifest(manifest)
    return manifest


def _worker_count(workers):
    if type(workers) is not int or not 1 <= workers <= 2:
        raise ValueError("资产传输并行数只能为1或2，不能使用布尔值")
    return workers


def _transfer_batches(entries, operation, workers):
    """SQL事务外的独立资产任务；最多一批在途，异常先收回工人再让调用方清理。"""
    if workers == 1:
        # 明确回滚入口沿用调用线程，不把同线程的旧行为转成单个后台工人。
        for entry in entries:
            operation(entry)
        return
    iterator = iter(entries)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="archspec-asset") as executor:
        while batch := list(islice(iterator, workers)):
            futures = [executor.submit(operation, entry) for entry in batch]
            completed, unfinished = wait(futures, return_when=FIRST_EXCEPTION)
            failed = next((future for future in futures
                           if future in completed and future.exception() is not None), None)
            if failed is not None:
                for future in unfinished:
                    future.cancel()
                # executor退出等待已开始的工作；不能让工人稍后写回已清理候选。
                failed.result()
            for future in futures:
                future.result()


def publish_manifest(root, manifest, object_store, *, workers=2):
    """先完整预检，再有界上传；全部确认后发布清单，完整读回仍由restore完成。"""
    workers = _worker_count(workers)
    entries = _check_manifest(manifest)
    root = _root(root)
    # 所有本地输入先核齐，再开始网络写入，不能靠旧清单上传已变化的文件。
    for entry in entries:
        path = _file(root, entry["path"], schema=manifest["schema_version"])
        if path.stat().st_size != entry["size_bytes"] or _sha(path) != entry["sha256"]:
            raise ValueError("资产已改变，未发布清单")
    unique = {}
    for entry in entries:
        # _check_manifest已核同键身份一致；保留原顺序的首条，不并发重复上传同键。
        unique.setdefault(entry["object_key"], entry)
    def upload(entry):
        object_store.put_file(entry["object_key"], _file(root, entry["path"], schema=manifest["schema_version"]),
                              entry["sha256"])
    _transfer_batches(unique.values(), upload, workers)
    # 清单临时文件也是专属排他新建，不写入已有原件目录。
    temporary = root / (".asset-manifest-" + uuid4().hex + ".tmp")
    content = _canonical(manifest)
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
        key = "evidence/manifests/" + manifest["manifest_id"] + ".json"
        object_store.put_file(key, temporary, hashlib.sha256(content).hexdigest(),
                              content_type="application/json")
        return key
    finally:
        temporary.unlink(missing_ok=True)


def restore_manifest(manifest, staging_root, object_store, *, workers=2, defer_page_images=False):
    """默认完整恢复；按需模式只延后固定原页，逐资产核SHA且不激活资料。"""
    workers = _worker_count(workers)
    entries = _check_manifest(manifest)
    if type(defer_page_images) is not bool:
        raise ValueError("原页恢复策略必须由服务器明确选择")
    # 仅延后已签收的原页PNG及配对描述；正文、原件及私有上传仍完整恢复。
    page_assets = re.compile(rf"official-cache/page-images/{_SOURCE}/{_DIGEST}/{_DIGEST}/{_DIGEST}/[1-9][0-9]{{0,3}}[.](?:png|json)")
    deferred = [dict(entry) for entry in entries
                if defer_page_images and page_assets.fullmatch(entry["path"])]
    required = [entry for entry in entries if not (defer_page_images and page_assets.fullmatch(entry["path"]))]
    root = _root(staging_root)
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / (".asset-candidate-" + uuid4().hex)
    candidate.mkdir()
    try:
        def restore(entry):
            destination = candidate / entry["path"]
            object_store.download_file(entry["object_key"], destination, entry["sha256"],
                                       expected_size=entry["size_bytes"])
            if (not destination.is_file() or destination.is_symlink()
                    or destination.stat().st_size != entry["size_bytes"]
                    or _sha(destination) != entry["sha256"]):
                raise ValueError("恢复的官方资产无法核对，未激活")
        # 清单可含旧路径与内容库两份相同原件。对象身份已由_check_manifest
        # 证明同键同SHA同长度；按对象分组下载，不能按路径重复消耗云流量。
        objects = {}
        for entry in required:
            objects.setdefault(entry["object_key"], []).append(entry)

        def restore_object(group):
            first, *aliases = group
            restore(first)
            source = candidate / first["path"]
            for alias in aliases:
                destination = candidate / alias["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                # 独立复制，避免硬链接使一个路径的后续损坏传播到旧快照。
                shutil.copyfile(source, destination)
                # 原有每路径完整校验不省略；不将下载成功当作文件身份已核。
                if (not destination.is_file() or destination.is_symlink()
                        or destination.stat().st_size != alias["size_bytes"]
                        or _sha(destination) != alias["sha256"]):
                    raise ValueError("恢复的官方资产副本无法核对，未激活")

        _transfer_batches(objects.values(), restore_object, workers)
        result = {"state": "ASSETS_RESTORED_UNPUBLISHED", "manifest_id": manifest["manifest_id"],
                  "directory": str(candidate), "files": len(required)}
        if deferred:
            result.update(state="CORE_ASSETS_RESTORED_UNPUBLISHED", deferred_page_assets=deferred)
        return result
    except BaseException:
        # 只清理本函数新建且仍在指定目录内的候选；既有资料从不删除或移动。
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
            raise RuntimeError("候选目录发生异常改变，保留现场，未清理其他路径") from None
        shutil.rmtree(candidate)
        raise
