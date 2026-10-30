"""仅按已签收清单恢复固定原页；不重新获取官网、渲染或扩展来源许可。"""
from threading import RLock

from archspec.official_documents.page_images import PageImageRepository, _identity
from archspec.persistence import evidence_manifest


class CloudPageImageRepository(PageImageRepository):
    """正文/原件仍在启动完整核验；只将原页及描述传输延后到实际读取。"""

    def __init__(self, generation):
        from archspec.persistence.remote_startup import RestoredCloudGeneration, _ISSUER
        if type(generation) is not RestoredCloudGeneration or generation._issuer is not _ISSUER:
            raise ValueError("原页恢复必须绑定真实云端启动")
        super().__init__(generation.data_dir / "official-cache")
        self.generation = generation
        self._entries = {entry["path"]: dict(entry) for entry in generation.deferred_page_assets}
        self._verified = set()
        # 同一仓库最多一张原页在途，避免同时下载同页或累积大量PNG字节。
        self._lock = RLock()

    def _check_binding(self):
        if (self.generation.closed or self.generation.closing
                or self.cache_dir != (self.generation.data_dir / "official-cache").resolve()):
            raise OSError("原页恢复已停止或当前资料根不一致")

    def recovery_status(self):
        self._check_binding()
        with self._lock:
            return {"policy": "PAGE_IMAGES_ON_READ_V1", "deferred_at_startup": sum(path.endswith(".png") for path in self._entries),
                    "deferred_asset_files_at_startup": len(self._entries),
                    "deferred_bytes_at_startup": sum(e["size_bytes"] for e in self._entries.values()),
                    "verified_on_read": len(self._verified)}

    def read(self, source_id, snapshot_id, document_sha256, page_number):
        self._check_binding()
        identity = _identity(source_id, snapshot_id, document_sha256, page_number)
        png, descriptor = self._paths(identity)
        relative = png.relative_to(self.generation.data_dir).as_posix()
        with self._lock:
            for path in (descriptor, png):
                entry = self._entries.get(path.relative_to(self.generation.data_dir).as_posix())
                if entry is None:
                    continue  # 本次启动后的新发布页沿原有本机准备/签收闭包。
                if not path.exists():
                    try:
                        self.generation._objects.download_file(entry["object_key"], path, entry["sha256"],
                                                               expected_size=entry["size_bytes"])
                    except (RuntimeError, OSError, ValueError, LookupError):
                        raise OSError("固定原页尚未完整从私有云恢复，请稍后读取") from None
                # 描述和PNG都与签收清单逐一核SHA；坏文件保留，不覆盖。
                if (not path.is_file() or path.is_symlink() or path.stat().st_size != entry["size_bytes"]
                        or evidence_manifest._sha(path) != entry["sha256"]):
                    raise ValueError("固定原页与已签收清单不符")
            # 继续执行原描述、来源/快照/文档指纹、尺寸与完整PNG解码检查。
            content = super().read(source_id, snapshot_id, document_sha256, page_number)
            if relative in self._entries:
                self._verified.add(relative)
            return content
