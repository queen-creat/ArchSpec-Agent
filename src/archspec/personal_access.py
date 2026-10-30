"""前台与后台共用的本机账号文件逻辑，不依赖平台进程管理模块。"""
import os
import re
import secrets
from pathlib import Path

from dotenv import dotenv_values


def prepare_access_file(path):
    """首次独占创建，已有账号保持原值；不向日志返回密码。"""
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.parent.chmod(0o700)
    if not path.exists():
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as output:
            output.write('ARCHSPEC_PERSONAL_USER=archspec\nARCHSPEC_PERSONAL_PASSWORD='
                         + secrets.token_urlsafe(32)+'\n')
    path.chmod(0o600)


def read_access_file(path):
    """只读取原固定字段，解析错误不携带文件内容或凭据。"""
    try:
        values=dotenv_values(path,interpolate=False)
    except (OSError,UnicodeError):
        raise ValueError('ACCESS_CONFIGURATION_REQUIRED') from None
    user,password=values.get('ARCHSPEC_PERSONAL_USER'),values.get('ARCHSPEC_PERSONAL_PASSWORD')
    if (not isinstance(user,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',user)
            or not isinstance(password,str) or not 16<=len(password)<=128):
        raise ValueError('ACCESS_CONFIGURATION_REQUIRED')
    return user,password
