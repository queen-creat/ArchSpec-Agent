"""只打包本项目代码；不读取或发送本机资料、密钥和虚拟环境。"""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".py", ".js", ".html", ".css", ".json"}
# 每个部署目标只打包各自需要的白名单文件；README.md 按目标换成对应的部署说明。
TARGETS = {
    "local": {
        "root_files": ("pyproject.toml", "uv.lock", ".gitignore", ".python-version", "docs/本机使用.md",
                       "scripts/run_local.py", "scripts/personal_service.py",
                       "scripts/build_server_release.py", "scripts/start_local.sh",
                       "启动个人版.command", "启动本机版.sh", "启动本机版.bat"),
        "overrides": {},
        "readme": "docs/本机使用.md",
        "package_name": "archspec-local-source.zip",
    },
    "public": {
        "root_files": ("pyproject.toml", "uv.lock", ".gitignore", ".python-version", "README.md",
                       "docs/本机使用.md", "scripts/run_local.py", "scripts/personal_service.py",
                       "scripts/build_server_release.py", "scripts/start_local.sh",
                       "启动个人版.command", "启动本机版.sh", "启动本机版.bat"),
        "overrides": {},
        "readme": "README.md",
        "package_name": "ArchSpec-Agent-source.zip",
    },
    "source-update": {
        "root_files": ("pyproject.toml", "uv.lock", "deploy/cloud-probe/SourceUpdate.Dockerfile",
                       "deploy/cloud-probe/SourceUpdate.Dockerfile.dockerignore",
                       "scripts/build_server_release.py", "scripts/verify_source_overlay.py",
                       "deploy/cloud-probe/verify-runtime-port.py",
                       ".github/workflows/build-private-images.yml"),
        "overrides": {},
        "readme": "deploy/README.md",
        "package_name": "archspec-source-update.zip",
    },
    "render": {
        "root_files": ("pyproject.toml", "uv.lock", "Dockerfile", ".dockerignore", ".gitignore",
                       "render.yaml"),
        "overrides": {},
        "readme": "deploy/README.md",
        "package_name": "archspec-render.zip",
    },
    "huggingface": {
        "root_files": ("pyproject.toml", "uv.lock", ".dockerignore"),
        "overrides": {"Dockerfile": "deploy/huggingface/Dockerfile"},
        "readme": "deploy/huggingface/README.md",
        "package_name": "archspec-huggingface.zip",
    },
}


def build_release(project, output, target="render"):
    if target not in TARGETS:
        raise ValueError(f"未登记的发布目标：{target}")
    spec = TARGETS[target]
    project, output = Path(project).resolve(), Path(output).resolve()
    if not output.is_relative_to(project):
        raise ValueError("发布包只能保存到本项目内")
    if output.exists():
        raise FileExistsError("发布包已存在，拒绝覆盖")
    selected = {name: project / name for name in spec["root_files"]}
    for name, source in spec["overrides"].items():
        selected[name] = project / source
    selected["README.md"] = project / spec["readme"]
    source = project / "src"
    if not source.is_dir():
        raise FileNotFoundError("源码目录缺失")
    for path in source.rglob("*"):
        if "__pycache__" in path.parts or any(part.startswith(".") for part in path.relative_to(source).parts):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError("源码含链接或越界文件，未生成发布包")
        if (target == "public" and path.parent == source / "archspec/official_evaluations/fixtures"):
            continue  # 开发者内部评测题库不读取、不随公开源码发布。
        if path.is_file() and path.suffix in SOURCE_SUFFIXES:
            if path.suffix == ".json" and path.parent != source / "archspec/official_evaluations/fixtures":
                raise ValueError("未审核的JSON资料不能随代码发布")
            selected[path.relative_to(project).as_posix()] = path
    # 完整预检与读取只涉及白名单；缺文件时不能留下貌似完整的包。
    contents = {}
    for name, path in selected.items():
        if path.is_symlink() or not path.resolve().is_relative_to(project):
            raise ValueError("发布文件链接越界")
        contents[name] = path.read_bytes()
    manifest = {"schema": "ARCHSPEC_CODE_ONLY_RELEASE_V1", "target": target,
                "sha256": {name: hashlib.sha256(value).hexdigest()
                           for name, value in sorted(contents.items())},
                "excluded": ["data", ".env*", ".venv", "tests", "personal project files"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for name, value in sorted(contents.items()):
            if target in {"local", "public"} and name in {
                    "启动个人版.command", "启动本机版.sh", "scripts/start_local.sh"}:
                # 默认writestr不保留执行位；Mac双击与Unix直接运行需要明确的文件权限。
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = 0o100755 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                package.writestr(info, value)
            else:
                package.writestr(name, value)
        package.writestr("release-manifest.json",
                         json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--target", choices=sorted(TARGETS), default="render",
                        help="代码包目标；local用于下载后本机运行，默认render")
    args = parser.parse_args()
    output = args.output or (ROOT / "data/deployment-releases" / str(uuid4())
                             / TARGETS[args.target]["package_name"])
    result = build_release(ROOT, output, target=args.target)
    print(json.dumps({"path": str(result), "target": args.target,
                      "sha256": hashlib.sha256(result.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
