"""构建时准备公开固定模型；运行期只读取产物，不自动下载或使用私有凭据。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from archspec.official_documents import semantic_experiment as semantic

# 固定上游提交与已审五件套共用同一SHA合同，不请求main或下载整库。
MODEL_REPOSITORY = "Qdrant/bge-small-zh-v1.5"
_MAX_FILE_BYTES = 128 * 1024 * 1024


def prepare_model(directory: Path, *, client: httpx.Client | None = None):
    """全部文件校验成功才发布目录；已有模型只核对，绝不覆盖损坏版本。"""
    directory = Path(directory).absolute()
    if directory.is_symlink():
        raise ValueError("固定模型目录不能是符号链接")
    if directory.exists():
        return semantic.offline_model_manifest(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    if client is None:
        # trust_env关闭，构建不会带入代理、netrc或本机服务凭据。
        with httpx.Client(timeout=120, follow_redirects=True, trust_env=False) as owned:
            return prepare_model(directory, client=owned)
    with TemporaryDirectory(prefix=".archspec-model-", dir=directory.parent) as temporary:
        staging = Path(temporary) / "ready"
        staging.mkdir()
        for name, expected in semantic.MODEL_FILES.items():
            url = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{semantic.MODEL_COMMIT}/{name}"
            digest = hashlib.sha256()
            size = 0
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with (staging / name).open("xb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > _MAX_FILE_BYTES:
                            raise ValueError("固定模型文件超过构建上限")
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"固定模型文件哈希不匹配：{name}")
        manifest = semantic.offline_model_manifest(staging)
        # 并发构建若已发布非空目录，重核该目录，不删除或覆盖它。
        try:
            staging.rename(directory)
        except OSError:
            if directory.exists() and not directory.is_symlink():
                return semantic.offline_model_manifest(directory)
            raise
        return manifest


def main():
    parser = argparse.ArgumentParser(description="准备固定公开检索模型；不调用付费API")
    parser.add_argument("--prepare", action="store_true", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare_model(args.directory)
    print(json.dumps({"model_name": manifest["model_name"], "commit": manifest["commit"],
                      "dimension": manifest["dimension"], "files_verified": len(manifest["files"])},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
