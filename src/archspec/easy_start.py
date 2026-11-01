"""本机首次启动窗口；凭据只显示给本机用户，不写入日志。"""

import os
import re
import subprocess
import sys
from pathlib import Path


def _private_folder(project_root):
    """拒绝链接和异常目标，避免将私人配置写到项目外。"""
    root = Path(project_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('项目目录无效或是符号链接')
    root = root.resolve(strict=True)
    folder = root
    for name in ('data', 'local-access'):
        folder = folder / name
        if folder.is_symlink():
            raise ValueError('配置目录不能是符号链接')
        folder.mkdir(mode=0o700, exist_ok=True)
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError('配置目录不可用')
    return folder


def _safe_file(folder, name):
    path = folder / name
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('配置文件不能是符号链接或目录')
    return path


def save_model_key_if_missing(project_root, key):
    """仅在配置不存在且用户输入密钥时独占创建；绝不读取或覆盖原配置。"""
    folder = _private_folder(project_root)
    path = _safe_file(folder, 'personal-model.private.env')
    if path.exists() or not key:
        return False
    # Key 应为单行可见字符，避免把额外配置注入 dotenv 文件。
    if len(key) > 512 or not re.fullmatch(r'[A-Za-z0-9._-]+', key):
        raise ValueError('Key 格式无效，请检查是否包含空格或换行')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        _safe_file(folder, path.name)
        return False
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write('DEEPSEEK_API_KEY=' + key + '\n')
    return True


def _open_editor(path):
    # 参数分别传递，文件路径不经过 shell 展开。
    if sys.platform == 'win32':
        subprocess.Popen(['notepad.exe', str(path)], close_fds=True)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-e', str(path)], close_fds=True)
    else:
        subprocess.Popen(['xdg-open', str(path)], close_fds=True)


def run_easy_start(project_root):
    """显示登录信息与可选 Key 设置；开始返回 True，取消返回 False。"""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print('无法打开设置窗口，请查看 README 中的手动启动步骤。', file=sys.stderr)
        return False
    try:
        folder = _private_folder(project_root)
        access_path = _safe_file(folder, 'personal-access.private.env')
        model_path = _safe_file(folder, 'personal-model.private.env')
        from archspec.personal_access import prepare_access_file, read_access_file

        prepare_access_file(access_path)
        username, password = read_access_file(access_path)
        window = tk.Tk()
    except (OSError, ValueError, RuntimeError, tk.TclError):
        print('无法打开设置窗口，请查看 README 中的手动启动步骤。', file=sys.stderr)
        return False

    window.title('ArchSpec 本机设置')
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=24)
    frame.grid(sticky='nsew')
    ttk.Label(frame, text='欢迎使用建筑规范查阅工作台', font=('TkDefaultFont', 15, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
    ttk.Label(frame, text='登录账号：').grid(row=1, column=0, sticky='w', pady=4)
    ttk.Label(frame, text=username).grid(row=1, column=1, sticky='w', pady=4)
    ttk.Label(frame, text='登录密码：').grid(row=2, column=0, sticky='w', pady=4)
    password_var = tk.StringVar(value=password)
    password_entry = ttk.Entry(frame, textvariable=password_var, show='●', width=36, state='readonly')
    password_entry.grid(row=2, column=1, sticky='ew', pady=4)

    def copy_password():
        window.clipboard_clear()
        window.clipboard_append(password_var.get())
        messagebox.showinfo('已复制', '登录密码已复制，请粘贴到工作台登录页。', parent=window)

    ttk.Button(frame, text='复制登录密码', command=copy_password).grid(row=3, column=1, sticky='w', pady=(0, 12))
    ttk.Separator(frame).grid(row=4, column=0, columnspan=2, sticky='ew', pady=8)
    ttk.Label(frame, text='DeepSeek Key（可稍后设置）').grid(row=5, column=0, columnspan=2, sticky='w', pady=(4, 6))
    key_var = tk.StringVar()
    if model_path.exists():
        ttk.Label(frame, text='已有模型配置文件，可按需打开编辑。').grid(row=6, column=0, columnspan=2, sticky='w')

        def edit_config():
            try:
                _open_editor(_safe_file(_private_folder(project_root), model_path.name))
            except (OSError, ValueError):
                messagebox.showerror('无法打开', '请在项目的 data/local-access 文件夹中打开模型配置。', parent=window)

        ttk.Button(frame, text='打开模型配置文件', command=edit_config).grid(row=7, column=0, columnspan=2, sticky='w', pady=8)
    else:
        ttk.Entry(frame, textvariable=key_var, show='●', width=36).grid(row=6, column=0, columnspan=2, sticky='ew')
        ttk.Label(frame, text='使用自己的 Key；留空可先查阅本地资料。').grid(row=7, column=0, columnspan=2, sticky='w', pady=8)
    result = {'started': False}

    def start():
        try:
            save_model_key_if_missing(project_root, key_var.get().strip())
        except (OSError, ValueError) as error:
            messagebox.showerror('无法保存设置', str(error) if isinstance(error, ValueError) else '请检查项目文件夹是否可写。', parent=window)
            return
        result['started'] = True
        window.destroy()

    ttk.Button(frame, text='开始使用', command=start).grid(row=8, column=1, sticky='e', pady=(12, 0))
    ttk.Button(frame, text='取消', command=window.destroy).grid(row=8, column=0, sticky='w', pady=(12, 0))
    window.protocol('WM_DELETE_WINDOW', window.destroy)
    window.mainloop()
    return result['started']
