"""下载源码后的可选配置准备：不读密钥、不覆盖文件、不启动任何服务。"""

import os
from pathlib import Path

TEMPLATES = {
    'personal-model.private.env': '# 填入自己的模型Key后保存；主动提交问题后默认使用 DeepSeek，受累计预算限制。\nDEEPSEEK_API_KEY=\n',
    'personal-tavily.private.env': '# 填入自己的搜索Key；确认账号已关闭按量付费后才将下一项改为true。\n'
        'TAVILY_API_KEY=\nARCHSPEC_TAVILY_FREE_ONLY_CONFIRMED=false\n',
}


def prepare_optional_files(project_root):
    root = Path(project_root).resolve()
    data, folder = root / 'data', root / 'data/local-access'
    paths = [folder / name for name in TEMPLATES]
    # 检查目的地，不跟随链接去碰项目外的私有文件，也不读取已有文件内容。
    for path in (data, folder, *paths):
        if path.is_symlink():
            raise ValueError('配置目的地含符号链接，未准备模板')
        if path.exists() and (not path.is_dir() if path in (data, folder) else not path.is_file()):
            raise ValueError('配置目的地类型不符，未准备模板')
    data.mkdir(exist_ok=True)
    folder.mkdir(mode=0o700, exist_ok=True)
    created, preserved = [], []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path.exists():
            preserved.append(relative)
            continue
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            # 并发创建也不重开/改权限/读回已有凭据。
            if path.is_symlink() or not path.is_file():
                raise ValueError('配置目的地已变化，未覆盖已有文件') from None
            preserved.append(relative)
            continue
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(TEMPLATES[path.name])
        created.append(relative)
    return {'created': created, 'preserved': preserved, 'services_started': False}
