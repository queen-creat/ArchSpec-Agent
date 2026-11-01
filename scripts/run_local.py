"""在 VS Code 终端启动本机开发服务；不默默后台运行或对外共享。"""

import argparse
import json
import os
import sys
from pathlib import Path


def configure_development_mode(project_root):
    """免费本地工作台的正式入口；只改当前进程，不改用户配置或累计预算。"""
    os.environ.update({
        "ARCHSPEC_DATA_DIR": str(Path(project_root).resolve() / "data"),
        "ARCHSPEC_PERSISTENCE_BACKEND": "LOCAL",
        "DEEPSEEK_API_KEY": "",
        "ARCHSPEC_TAVILY_ENABLED": "false",
        "ARCHSPEC_FREE_SEARCH_ENABLED": "false",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    })
    os.environ.pop("ARCHSPEC_COMPUTE_URL", None)

    def protect_private_configuration(event, args):
        # 在任何业务模块导入之前阻断两个私有配置文件，不读取或回显内容。
        if (event == "open" and isinstance(args[0], (str, bytes))
                and Path(os.fsdecode(args[0])).name in {".env.local", "live-access.local.env"}):
            raise RuntimeError("免费本地开发模式不读取私有配置文件")
    sys.addaudithook(protect_private_configuration)
    return {"persistence": "LOCAL", "model_api": "DISABLED", "external_search": "DISABLED",
            "cloud_compute": "DISABLED", "private_configuration": "BLOCKED"}



def _configure_personal_tavily(root):
    """只读独立本机搜索密钥；空文件沿免费路线，不继承旧环境或修改其它配置。"""
    from dotenv import dotenv_values

    from archspec.official_discovery.tavily import load_tavily_provider

    os.environ["ARCHSPEC_TAVILY_ENABLED"] = "false"
    os.environ.pop("TAVILY_API_KEY", None)
    os.environ.pop("ARCHSPEC_TAVILY_FREE_ONLY_CONFIRMED", None)
    path = root / "data/local-access/personal-tavily.private.env"
    try:
        if path.is_symlink():
            raise ValueError
        if not path.exists():
            return False
        if not path.is_file() or path.stat().st_size > 4096 or path.stat().st_mode & 0o077:
            raise ValueError
        values = dotenv_values(path, interpolate=False)
        if set(values) - {"TAVILY_API_KEY", "ARCHSPEC_TAVILY_FREE_ONLY_CONFIRMED"}:
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        raise ValueError("个人Tavily配置无法核对，须使用独立私有文件") from None
    key = values.get("TAVILY_API_KEY")
    if not key:
        return False
    selected = {"ARCHSPEC_TAVILY_ENABLED": "true", "TAVILY_API_KEY": key,
                "ARCHSPEC_TAVILY_FREE_ONLY_CONFIRMED": values.get("ARCHSPEC_TAVILY_FREE_ONLY_CONFIRMED", "")}
    # 复用原工厂的免费/PAYG及密钥格式门；此步不发请求，不把Key放入状态或日志。
    load_tavily_provider(selected)
    os.environ.update(selected)
    return True


def configure_personal_mode(project_root):
    """个人免费基础设施入口；只给官方QA局部配置模型，不启用其它收费服务。"""
    root = Path(project_root).resolve()
    # 继续复用原资料和累计账本，不迁移、不创建新预算。
    os.environ.update({
        "ARCHSPEC_DATA_DIR": str(root / "data"),
        "ARCHSPEC_PERSISTENCE_BACKEND": "LOCAL",
        "ARCHSPEC_TAVILY_ENABLED": "false",
        "ARCHSPEC_OFFICIAL_HTTP_MODE": "DIRECT",
        "ARCHSPEC_OFFICIAL_FETCH_MODE": "DIRECT",
        "ARCHSPEC_QA_MODEL_CONFIG_FILE": str(root / "data/local-access/personal-model.private.env"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    })
    # 模型密钥仅由QA局部读取；普通资料/Agent入口不继承全局密钥。
    for name in ("DEEPSEEK_API_KEY", "ARCHSPEC_COMPUTE_URL"):
        os.environ.pop(name, None)

    def protect_private_configuration(event, args):
        # 即使配置误指旧文件，业务代码也不能读取此前禁止访问的私有文件。
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0]))
            protected = {".env.local", "live-access.local.env"}
            if path.name in protected or path.resolve().name in protected:
                raise RuntimeError("个人模式不读取旧私有配置文件")
    sys.addaudithook(protect_private_configuration)
    # 搜索配置独立于模型/登录凭据，只读这个固定非密钥文件，不读取旧env。
    search_path = root / "data/local-access/personal-search.local.json"
    search = {"enabled": False}
    if search_path.exists():
        if search_path.is_symlink() or search_path.stat().st_size > 4096:
            raise ValueError("个人搜索配置无法核对")
        search = json.loads(search_path.read_text())
        if (not isinstance(search, dict) or set(search) - {"enabled", "proxy_url"}
                or type(search.get("enabled")) is not bool):
            raise ValueError("个人搜索配置须明确enabled及可选本机代理")
    from archspec.official_discovery.free_search import validate_search_proxy

    proxy = validate_search_proxy(search.get("proxy_url"))
    os.environ["ARCHSPEC_FREE_SEARCH_ENABLED"] = str(search["enabled"]).lower()
    if proxy:
        os.environ["ARCHSPEC_FREE_SEARCH_PROXY"] = proxy
    else:
        os.environ.pop("ARCHSPEC_FREE_SEARCH_PROXY", None)
    tavily = _configure_personal_tavily(root)
    return {"persistence": "LOCAL", "model_api": "QA_WITH_EXISTING_BUDGET",
            "external_search": "TAVILY_FREE_ONLY" if tavily else ("DUCKDUCKGO_FREE" if search["enabled"] else "DISABLED"),
            "cloud_compute": "DISABLED",
            "private_configuration": "OLD_FILES_BLOCKED", "authentication": "REQUIRED"}


def configure_local_processing(project_root, model_dir=None):
    """复用已下载并逐件校验的固定模型；不下载、不改云端处理策略。"""
    from archspec.embeddings.provider import PinnedFastEmbedProvider
    from archspec.official_documents.semantic_experiment import MODEL_COMMIT

    directory = (Path(model_dir).expanduser().resolve() if model_dir else
        Path(project_root) / "data/models/models--Qdrant--bge-small-zh-v1.5/snapshots" / MODEL_COMMIT)
    provider = PinnedFastEmbedProvider(directory)
    # 先完整校验固定五件套再启用；缺失或损坏即失败，不能悄悄联网下模型。
    os.environ["ARCHSPEC_EMBEDDING_MODEL_DIR"] = str(directory)
    os.environ["ARCHSPEC_RUNTIME_PUBLICATION_COMPUTE"] = "native"
    os.environ["ARCHSPEC_PDF_NATIVE_TEXT"] = "1"
    return {"pdf_mode": "NATIVE_TEXT_IMAGES_UNRECOGNIZED_V1",
            "embedding_model_fingerprint": provider.model_fingerprint}


def configure_personal_processing(project_root, model_dir=None):
    """个人入口的PDF识别与可选语义模型分开；不让模型缺失阻断扫描正文。"""
    from archspec.official_documents.semantic_experiment import MODEL_COMMIT

    default = (Path(project_root) / "data/models/models--Qdrant--bge-small-zh-v1.5/snapshots"
               / MODEL_COMMIT)
    if model_dir is not None or default.exists() or default.is_symlink():
        processing = {**configure_local_processing(project_root, model_dir),
                      "semantic_search": "AVAILABLE"}
    else:
        # 仅默认可选资源完全缺失时走关键词路径；坏模型仍由原校验拒绝。
        os.environ.pop("ARCHSPEC_EMBEDDING_MODEL_DIR", None)
        os.environ["ARCHSPEC_RUNTIME_PUBLICATION_COMPUTE"] = "native"
        processing = {"semantic_search": "UNAVAILABLE", "embedding_model_fingerprint": None}
    # 个人本机复用已安装RapidOCR；图像在本机处理，不发送到模型API。
    # OCR开关/单页像素与失败保原件合同保持；本机页数策略独立选择，不改旧固定快照。
    os.environ.pop("ARCHSPEC_PDF_NATIVE_TEXT", None)
    os.environ["ARCHSPEC_OFFICIAL_RESOURCE_PROFILE"] = "LOCAL"
    return {**processing, "pdf_mode": "LOCAL_OCR_REVIEW_REQUIRED"}


def configure_personal_access(project_root):
    """前台入口复用原本机账号配置，不依赖后台管理器的macOS进程命令。"""
    if os.environ.get("ARCHSPEC_PERSONAL_USER") or os.environ.get("ARCHSPEC_PERSONAL_PASSWORD"):
        # 管理器已注入时不读写账号文件；缺一项仍交原访问门明确拒绝。
        return {"authentication": "ENVIRONMENT"}
    from archspec.personal_access import prepare_access_file, read_access_file

    root = Path(project_root).resolve()
    path = root / "data/local-access/personal-access.private.env"
    # 前台账号准备不导入后台管理器，也不读取其进程状态或使用Unix锁。
    prepare_access_file(path)
    username, password = read_access_file(path)
    os.environ.update(ARCHSPEC_PERSONAL_USER=username, ARCHSPEC_PERSONAL_PASSWORD=password)
    return {"authentication": "LOCAL_PRIVATE_FILE",
            "credentials_file": str(root / "data/local-access/personal-access.private.env")}


def open_browser_when_ready(server, host, port):
    """只打开本进程成功监听的服务，避免误开占用同端口的其他程序。"""
    import time
    import webbrowser
    deadline = time.monotonic() + 90
    while not server.started and not server.should_exit and time.monotonic() < deadline:
        time.sleep(0.1)
    if server.started:
        display_host = "[::1]" if host == "::1" else host
        webbrowser.open(f"http://{display_host}:{port}/")


def main():
    parser = argparse.ArgumentParser(description="ArchSpec 仅本机启动工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--check", action="store_true", help="只检查配置，不启动服务")
    parser.add_argument("--easy-start", action="store_true", help="使用本机图形向导设置账号和模型")
    parser.add_argument("--open-browser", action="store_true", help="服务就绪后自动打开本机页面")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare-config", action="store_true", help="只创建空的可选模型/搜索私有配置，已有文件不读不覆盖")
    modes.add_argument("--development", action="store_true", help="免费本地开发：关闭模型API、外部搜索及云计算，保留官方联网")
    modes.add_argument("--personal", action="store_true", help="个人使用：本机存储处理、受保护入口、只允许预算内QA模型")
    parser.add_argument("--direct-network", action="store_true", help="仅本程序直连官方来源，不使用系统代理")
    parser.add_argument("--local-processing", action="store_true", help="使用已有固定模型与PDF文字层在本机处理")
    parser.add_argument("--embedding-model-dir", type=Path, help="已有固定五件套目录；仅配合--local-processing")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("当前原型仅本机使用，拒绝非本机绑定；尚未完成共享身份与权限")
    if not 1 <= args.port <= 65535:
        parser.error("端口须为1—65535")
    if args.embedding_model_dir and not (args.local_processing or args.personal):
        parser.error("模型目录只用于--local-processing")
    if args.easy_start and (not args.personal or args.check):
        parser.error("图形向导仅用于正常启动个人版")
    if args.prepare_config and args.check:
        parser.error("--prepare-config会创建空文件，请单独使用；不能与--check合用")
    if args.prepare_config:
        from archspec.local_setup import prepare_optional_files

        print(json.dumps(prepare_optional_files(Path(__file__).resolve().parents[1]), ensure_ascii=False))
        return
    if args.easy_start:
        from archspec.easy_start import run_easy_start
        if not run_easy_start(Path(__file__).resolve().parents[1]):
            raise SystemExit(1)
    development = configure_development_mode(Path(__file__).resolve().parents[1]) if args.development else None
    personal = configure_personal_mode(Path(__file__).resolve().parents[1]) if args.personal else None
    root = Path(__file__).resolve().parents[1]
    processing = (configure_personal_processing(root, args.embedding_model_dir)
                  if args.personal and not args.local_processing
                  else configure_local_processing(root, args.embedding_model_dir)
                  if args.local_processing else None)
    if args.direct_network:
        os.environ["ARCHSPEC_OFFICIAL_HTTP_MODE"] = "DIRECT"
    from archspec.official_http import official_http_mode

    if args.check:
        print(json.dumps({"host": args.host, "port": args.port, "local_only": True, "official_http_mode": official_http_mode(), "local_processing": processing, "development": development, "personal": personal}))
        return
    if args.personal:
        access = configure_personal_access(root)
        print(f"本机工作台：http://{args.host}:{args.port}/", flush=True)
        if access.get("credentials_file"):
            print("登录账号和密码保存在本项目 data/local-access/personal-access.private.env；请勿公开。", flush=True)
    import uvicorn
    # 无需记住运行目录；默认资料仍在项目原来的 data。
    os.chdir(Path(__file__).resolve().parents[1])
    target = "archspec.personal:create_personal_app" if args.personal else "archspec.main:app"
    if not args.open_browser:
        uvicorn.run(target, factory=args.personal, host=args.host, port=args.port, proxy_headers=False)
        return
    # 仅在本次服务启动成功后打开浏览器；端口被占用时不误打开其他程序。
    import threading
    server = uvicorn.Server(uvicorn.Config(target, factory=args.personal, host=args.host,
                                           port=args.port, proxy_headers=False))
    threading.Thread(target=open_browser_when_ready, args=(server, args.host, args.port),
                     daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
