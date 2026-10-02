"""Windows 便携包的配置、后台启动、状态检查与停止入口。"""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx
import psutil
import redis
from cryptography.fernet import Fernet
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'data' / 'processes.json'


def initialize():
    os.chdir(ROOT)
    for folder in ('data', 'logs', 'data/redis'):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    config_path = ROOT / 'launcher.json'
    if not config_path.exists():
        config_path.write_text(json.dumps({'web_port': 3000, 'redis_port': 16379}, indent=2))
    config = json.loads(config_path.read_text())
    for key in ('web_port', 'redis_port'):
        if not isinstance(config[key], int) or not 1024 <= config[key] <= 65535:
            raise RuntimeError(f'Invalid {key} in launcher.json')
    if config['web_port'] == config['redis_port']:
        raise RuntimeError('Web and Redis ports must differ.')
    if not (ROOT / '.env').exists():
        password = secrets.token_hex(24)
        values = {
            'APIBRIDGE_ENV': 'production',
            'APIBRIDGE_ADMIN_TOKEN': secrets.token_urlsafe(32),
            'APIBRIDGE_ENCRYPT_KEY': Fernet.generate_key().decode(),
            'APIBRIDGE_DATABASE_URL': 'sqlite+aiosqlite:///./data/apibridge.db',
            'APIBRIDGE_REDIS_URL': f'redis://:{password}@127.0.0.1:{config["redis_port"]}/0',
            'APIBRIDGE_SCHEDULER_ENABLED': 'false',
            'APIBRIDGE_ORACLE_CLIENT_MODE': 'thick',
            'APIBRIDGE_ORACLE_CLIENT_LIB_DIR': './oracle/instantclient_19_32',
            'APIBRIDGE_PROBE_ALLOWED_HOSTS': '',
        }
        (ROOT / '.env').write_text(''.join(f'{k}={v}\n' for k, v in values.items()), encoding='utf-8')
    values = dotenv_values(ROOT / '.env')
    from urllib.parse import urlparse
    redis_url = urlparse(values.get('APIBRIDGE_REDIS_URL', ''))
    if redis_url.hostname != '127.0.0.1' or redis_url.port != config['redis_port'] or not redis_url.password:
        raise RuntimeError('Keep .env Redis URL and launcher.json redis_port consistent; a password is required.')
    # 密码由初始化生成；禁止在 Redis 配置中注入新指令。
    if not redis_url.password.isalnum():
        raise RuntimeError('Use an alphanumeric Redis password in .env.')
    redis_config = ROOT / 'data' / 'redis.conf'
    redis_config.write_text(
        f'bind 127.0.0.1\nport {config["redis_port"]}\nprotected-mode yes\n'
        f'requirepass {redis_url.password}\ndir .\nappendonly yes\n', encoding='utf-8')
    return config, values


def identity(pid):
    process = psutil.Process(pid)
    return {'pid': pid, 'created': process.create_time(), 'exe': process.exe()}


def verified(record):
    try:
        process = psutil.Process(record['pid'])
        if abs(process.create_time() - record['created']) > 0.01:
            return None
        if os.path.normcase(process.exe()) != os.path.normcase(record['exe']):
            return None
        return process
    except psutil.NoSuchProcess:
        return None


def write_state(records):
    STATE.write_text(json.dumps(records, indent=2), encoding='utf-8')


def spawn(name, command, cwd, env):
    with (ROOT / 'logs' / f'{name}.out.log').open('ab') as out, (ROOT / 'logs' / f'{name}.err.log').open('ab') as err:
        child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=err, close_fds=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    return identity(child.pid)


def wait_ready(check, record, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not verified(record):
            raise RuntimeError('A service exited during startup. See logs/.')
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError('Service startup timed out. See logs/.')


def stop(records, values):
    # 只操作本目录启动并记录了创建时间的进程，不按程序名批量结束。
    for name in ('backend', 'redis'):
        if name not in records:
            continue
        process = verified(records[name])
        if process is not None:
            if name == 'redis':
                client = redis.Redis.from_url(values['APIBRIDGE_REDIS_URL'], socket_timeout=5)
                client.shutdown(save=True)
                process.wait(timeout=15)
            else:
                children = process.children(recursive=True)
                for child in reversed(children):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                process.terminate()
                _, alive = psutil.wait_procs(children + [process], timeout=10)
                if alive:
                    raise RuntimeError('Some APIBridge processes are still running.')
        records.pop(name)
        write_state(records)
        print(f'{name}: stopped')
    STATE.unlink(missing_ok=True)


def main():
    config, values = initialize()
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    records = json.loads(STATE.read_text()) if STATE.exists() else {}
    if action == 'init':
        print('Configuration ready. Keep .env with your database; it contains the encryption key.')
        return
    if action == 'migrate':
        if any(verified(record) for record in records.values()):
            raise RuntimeError('Stop all services and back up the database before migration.')
        env = {k: v for k, v in os.environ.items() if not k.startswith('APIBRIDGE_')}
        env.update({k: v for k, v in values.items() if v is not None})
        result = subprocess.run([sys.executable, '-m', 'app.migrations'], cwd=ROOT, env=env)
        if result.returncode:
            raise RuntimeError('Database migration failed; inspect the output and restore backup if needed.')
        return
    if action == 'stop':
        stop(records, values)
        return
    if action == 'status':
        for name in ('backend', 'redis'):
            print(f'{name}: {"running" if name in records and verified(records[name]) else "stopped"}')
        return
    if action != 'start':
        raise RuntimeError('Usage: manage.py start|stop|status|init')
    if any(verified(record) for record in records.values()):
        raise RuntimeError('APIBridge is already running; use stop.cmd first.')
    for port in (config['web_port'], config['redis_port']):
        with socket.socket() as check:
            if check.connect_ex(('127.0.0.1', port)) == 0:
                raise RuntimeError(f'Port {port} is in use. See launcher.json and README.md.')
    env = {k: v for k, v in os.environ.items() if not k.startswith('APIBRIDGE_')}
    env.update({k: v for k, v in values.items() if v is not None})
    env['PYTHONIOENCODING'] = 'utf-8'
    if values.get('APIBRIDGE_ORACLE_CLIENT_MODE') == 'thick':
        import oracledb
        try:
            oracledb.init_oracle_client(lib_dir=values['APIBRIDGE_ORACLE_CLIENT_LIB_DIR'])
        except Exception:
            raise RuntimeError('Oracle client could not load. Extract to an English-only path such as D:\\APIBridge and install prerequisites/VC_redist.x64.exe. Oracle 12.1+ can alternatively use thin mode.') from None
    records = {}
    try:
        records['redis'] = spawn('redis', [str(ROOT / 'redis/redis-server.exe'), '../redis.conf'], ROOT / 'data/redis', env)
        write_state(records)
        redis_client = redis.Redis.from_url(values['APIBRIDGE_REDIS_URL'], socket_timeout=1)
        wait_ready(redis_client.ping, records['redis'])
        records['backend'] = spawn('backend', [sys.executable, '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', str(config['web_port'])], ROOT, env)
        write_state(records)
        with httpx.Client(trust_env=False, timeout=2) as client:
            wait_ready(lambda: client.get(f'http://127.0.0.1:{config["web_port"]}/health').status_code == 200, records['backend'])
    except Exception:
        stop(records, values)
        raise
    print(f'APIBridge: http://127.0.0.1:{config["web_port"]}')
    print('Enter the APIBRIDGE_ADMIN_TOKEN from .env in the top-right Token panel.')
    print('Logs: logs/   Stop: stop.cmd')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
