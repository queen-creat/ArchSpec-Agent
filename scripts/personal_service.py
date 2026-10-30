"""本机个人服务管理；只管理本工程自身进程，不安装系统常驻项。"""
import argparse
import base64
import fcntl
import http.client
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'data/personal-runtime'
ACCESS = ROOT / 'data/local-access/personal-access.private.env'
STATE = RUNTIME / 'state.json'
PORT = 8010


class Refused(RuntimeError):
    """固定错误码不带提供方异常、凭据或请求正文。"""


def read_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(state):
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(STATE)


def credentials():
    from archspec.personal_access import read_access_file

    try:
        return read_access_file(ACCESS)
    except ValueError as error:
        raise Refused('ACCESS_CONFIGURATION_REQUIRED') from error


def get(path, *, authenticated=True):
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=3)
    try:
        headers = {}
        if authenticated:
            user, password = credentials()
            headers['Authorization'] = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def process(pid):
    if type(pid) is not int or pid < 2:
        return None
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat=', '-o', 'pgid=',
                             '-o', 'lstart=', '-o', 'command='],
                            capture_output=True, text=True, check=False)
    parts = result.stdout.strip().split(maxsplit=7)
    # 僵尸已经退出，命令显示可能改变；活进程仍完整核对原PID/组/启动时间/命令。
    if len(parts) != 8 or parts[0].startswith('Z'):
        return None
    return {'pgid': int(parts[1]), 'start_token': ' '.join(parts[2:7]), 'command': parts[7]}


def owned(record, kind):
    current = process(record.get('pid')) if isinstance(record, dict) else None
    if current is None:
        return False
    expected = str(ROOT / ('scripts/run_local.py' if kind == 'service' else 'data/tools/cloudflared'))
    if (current['pgid'] != record['pid'] or current['start_token'] != record.get('start_token')
            or expected not in current['command']):
        raise Refused('PROCESS_IDENTITY_MISMATCH')
    return True


def spawn(command, log_name, env=None):
    fd = os.open(RUNTIME / log_name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'ab') as log:
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(.15)
    current = process(child.pid)
    if current is None:
        raise Refused('PROCESS_START_FAILED')
    return {'pid': child.pid, 'pgid': current['pgid'], 'start_token': current['start_token'],
            'port': PORT, 'started_at': datetime.now(UTC).isoformat()}


def idle():
    # 只读本地持久业务状态；缺表/缺目录/损坏均视为未知，不能据此停止服务。
    try:
        with sqlite3.connect((ROOT / 'data/archspec.db').as_uri() + '?mode=ro', uri=True, timeout=2) as database:
            count = database.execute("SELECT count(*) FROM qa_tasks WHERE state IN ('CREATED','RUNNING')").fetchone()[0]
            count += database.execute("SELECT count(*) FROM qa_operations WHERE state='RUNNING'").fetchone()[0]
        for folder, active in [('official-acquisitions/jobs', {'QUEUED', 'DOWNLOADING', 'PROCESSING'}),
                               ('official-discovery/flows', {'RUNNING', 'WAITING_BODY'})]:
            directory = ROOT / 'data' / folder
            if not directory.is_dir():
                raise Refused('BUSINESS_STATE_UNKNOWN')
            count += sum(json.loads(path.read_text())['state'] in active for path in directory.glob('*.json'))
    except (OSError, sqlite3.Error, ValueError, KeyError) as error:
        raise Refused('BUSINESS_STATE_UNKNOWN') from error
    if count:
        raise Refused('BUSINESS_ACTIVE')


def terminate(state, kind):
    record = state.get(kind)
    if owned(record, kind):
        # 仅向已核自身独立进程组发一次中断，等待应用正常退出，不强杀未知工作。
        os.killpg(record['pid'], signal.SIGINT)
        for _ in range(40):
            if not owned(record, kind):
                break
            time.sleep(.2)
        else:
            raise Refused('STOP_PENDING_NO_FORCE_KILL')
    state.pop(kind, None)
    save(state)


def public_health(url):
    """只探测候选免费域名的公开健康页，不携带认证、代理或业务材料。"""
    if not re.fullmatch(r'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com', url or ''):
        return None
    try:
        return httpx.get(url + '/api/health', timeout=3, trust_env=False,
                         follow_redirects=False).status_code
    except httpx.HTTPError:
        return None


def external_status(state, *, check_public=True):
    """当前进程的日志和公共健康共同决定可用；进程存活或URL本身不代表READY。"""
    tunnel = state.get('tunnel') or {}
    running = owned(tunnel, 'tunnel')
    candidate = tunnel.get('candidate_url') or tunnel.get('share_url')
    latest = None
    if running:
        for name, offset in tunnel.get('log_offsets', {}).items():
            if name not in {'tunnel.log', 'tunnel-console.log'}:
                continue
            path = RUNTIME / name
            if not path.is_file():
                continue
            with path.open('rb') as stream:
                stream.seek(offset)
                for raw in stream:
                    line = raw.decode(errors='replace')
                    urls = re.findall(r'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com\b', line)
                    if urls:
                        candidate = urls[-1]
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            continue
                        stamp = datetime.fromisoformat(row['time'])
                        if stamp.tzinfo is None:
                            continue
                        message = str(row.get('message', '')) + ' ' + str(row.get('error', ''))
                        failed = 'Unauthorized: Tunnel not found' in message
                        registered = 'Registered tunnel connection' in message
                        if failed or registered:
                            event = (stamp.timestamp(), int(failed))
                            latest = event if latest is None else max(latest, event)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
    external, source, health_status = 'OFFLINE', 'PROCESS_STATE', None
    registered = latest is not None and latest[1] == 0
    if running:
        external, source = 'STARTING', 'CURRENT_TUNNEL_LOG'
        if latest is not None and latest[1] == 1:
            external = 'TUNNEL_NOT_FOUND'
        elif registered and candidate and check_public:
            health_status = public_health(candidate)  # 每次状态最多一次，无重试或重启。
            external = 'READY' if health_status == 200 else 'NETWORK_UNAVAILABLE'
            source = 'CURRENT_TUNNEL_LOG_AND_PUBLIC_HEALTH'
    ready = external == 'READY'
    if tunnel:
        tunnel.update(candidate_url=candidate, share_url=candidate if ready else None, external_state=external)
        save(state)
    return {'tunnel_running': running, 'external_state': external, 'external_state_source': source,
            'registration_observed': registered, 'public_health_status': health_status,
            'sharing': ready, 'candidate_url': candidate, 'share_url': candidate if ready else None,
            'reopen_required': external == 'TUNNEL_NOT_FOUND'}


def status(state, *, check_external=True):
    running = owned(state.get('service'), 'service')
    health, configured = False, False
    try:
        health = get('/api/health')[0] == 200
        code, body = get('/api/official-qa/model-status')
        configured = json.loads(body).get('configured') is True if code == 200 else False
    except (OSError, ValueError, http.client.HTTPException, Refused):
        pass
    return {'ok': True, 'running': running, 'pid': state.get('service', {}).get('pid'), 'port': PORT,
            'health': health, 'model_configured': configured,
            **external_status(state, check_public=check_external), 'share_url_changes_on_restart': True}


def execute(action):
    if action == 'prepare':
        from archspec.personal_access import prepare_access_file

        prepare_access_file(ACCESS)
        return {'ok': True, 'prepared': True}
    state = read_state()
    if action == 'start' and not owned(state.get('service'), 'service'):
        # 端口即使属于旧服务也不接管；由用户或受控原流程自行停止。
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', PORT)) == 0:
                raise Refused('PORT_ALREADY_IN_USE')
        user, password = credentials()
        environment = dict(os.environ, ARCHSPEC_PERSONAL_USER=user, ARCHSPEC_PERSONAL_PASSWORD=password)
        state['service'] = spawn(['/usr/bin/caffeinate', '-i', '-s', str(ROOT / '.venv/bin/python'),
            str(ROOT / 'scripts/run_local.py'), '--personal', '--port', str(PORT)], 'service.log', environment)
        save(state)
    elif action in {'stop', 'unshare'}:
        idle()
        terminate(state, 'tunnel')
        if action == 'stop':
            terminate(state, 'service')
    elif action == 'share':
        if not owned(state.get('service'), 'service'):
            raise Refused('MANAGED_SERVICE_REQUIRED')
        # 登录页本身可匿名访问；必须用业务API确认资料仍受认证保护。
        if get('/')[0] != 200 or get('/api/projects', authenticated=False)[0] != 401:
            raise Refused('AUTHENTICATION_GATE_NOT_VERIFIED')
        if not owned(state.get('tunnel'), 'tunnel'):
            tool = ROOT / 'data/tools/cloudflared'
            if not tool.is_file() or not os.access(tool, os.X_OK):
                raise Refused('TUNNEL_TOOL_REQUIRED')
            names = ('tunnel.log', 'tunnel-console.log')
            offsets = {name: (RUNTIME / name).stat().st_size if (RUNTIME / name).exists() else 0 for name in names}
            # 空配置不读取个人已有隧道凭据；不改Host、不传业务/模型密钥到命令行。
            state['tunnel'] = spawn([str(tool), 'tunnel', '--config', '/dev/null', '--no-autoupdate',
                '--protocol', 'http2', '--url', f'http://127.0.0.1:{PORT}', '--logfile', str(RUNTIME / 'tunnel.log')],
                'tunnel-console.log', {name: os.environ[name] for name in ('PATH', 'HOME', 'TMPDIR', 'LANG')
                                       if name in os.environ})
            state['tunnel']['log_offsets'] = offsets
            save(state)
        for _ in range(100):
            observed = external_status(state, check_public=False)
            if observed['reopen_required'] or (observed['registration_observed'] and observed['candidate_url']):
                break
            time.sleep(.25)
    # 准备和本地启动不依赖外网；只有显式status/share才探测公开健康页。
    return status(state, check_external=action in {'status', 'share'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'start', 'status', 'stop', 'share', 'unshare'))
    args = parser.parse_args()
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        RUNTIME.chmod(0o700)
        with (RUNTIME / 'manager.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(execute(args.action), ensure_ascii=False), flush=True)
    except Exception as error:  # noqa: BLE001 - CLI边界仅输出固定错误码，不回显异常原文或密钥。
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, Refused)
                          else 'PERSONAL_SERVICE_OPERATION_UNAVAILABLE'}), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
