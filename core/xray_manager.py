"""
共享 Xray Runtime 管理器
从 codex-reg 移植

目标：
- 用一个 Xray 进程统一管理 http / socks5 / ss / vmess / vless
- 为每条代理/节点分配一个本地 HTTP 入站端口
- 通过 routing 规则将入站绑定到对应 outbound
"""

import json
import os
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs, unquote
import base64
import platform
import zipfile
import io
import urllib.request
import shutil

# Resolve project root dynamically: core/xray_manager.py -> project_root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Runtime dir under current project
RUNTIME_DIR = (_PROJECT_ROOT / 'data' / 'xray_shared')
# Bin dir for xray
XRAY_BIN_DIR = (_PROJECT_ROOT / 'xray-bin')

def get_xray_bin_path() -> Path:
    system = platform.system().lower()
    arch = platform.machine().lower()

    # Normalize system
    if system == 'windows':
        sys_name = 'windows'
        ext = '.exe'
    elif system == 'linux':
        sys_name = 'linux'
        ext = ''
    elif system == 'darwin':
        sys_name = 'macos'
        ext = ''
    else:
        sys_name = system
        ext = ''

    # Normalize arch
    arch_map = {
        'amd64': '64',
        'x86_64': '64',
        'x86': '32',
        'i386': '32',
        'i686': '32',
        'arm64': 'arm64-v8a',
        'aarch64': 'arm64-v8a',
    }
    arch_name = arch_map.get(arch, arch)

    # 1. Structured path: xray-bin/<sys>/<arch>/xray
    bin_path = XRAY_BIN_DIR / sys_name / arch_name / f'xray{ext}'
    if bin_path.exists():
        return bin_path

    # 2. Legacy path: xray-bin/xray
    legacy_bin = XRAY_BIN_DIR / f'xray{ext}'
    if legacy_bin.exists():
        return legacy_bin

    # 3. Root path fallback
    root_bin = _PROJECT_ROOT / f'xray{ext}'
    if root_bin.exists():
        return root_bin

    # 默认返回期望路径
    return bin_path

XRAY_BIN = get_xray_bin_path()
CONFIG_PATH = RUNTIME_DIR / 'config.json'
PID_PATH = RUNTIME_DIR / 'xray.pid'
LOG_PATH = RUNTIME_DIR / 'xray.log'
BASE_PORT = 24000


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def pick_free_port(start: int = BASE_PORT, used: Optional[set[int]] = None) -> int:
    used = used or set()
    port = start
    while True:
        if port in used:
            port += 1
            continue
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                port += 1


def is_xray_running() -> bool:
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def stop_xray() -> bool:
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
    except Exception:
        return False
    try:
        # On Windows, killpg is not available
        if platform.system().lower() == 'windows':
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    try:
        PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return True


def ensure_xray_binary() -> Dict[str, Any]:
    """确保 Xray 二进制文件存在，不存在则下载"""
    global XRAY_BIN
    XRAY_BIN = get_xray_bin_path()
    if XRAY_BIN.exists():
        return {'success': True, 'message': f'Xray 已就绪: {XRAY_BIN}'}

    XRAY_BIN_DIR.mkdir(parents=True, exist_ok=True)
    
    system = platform.system().lower()
    arch = platform.machine().lower()
    
    # 映射系统名
    if system == 'windows':
        sys_name = 'windows'
        asset_prefix = 'Xray-windows'
    elif system == 'linux':
        sys_name = 'linux'
        asset_prefix = 'Xray-linux'
    elif system == 'darwin':
        sys_name = 'macos'
        asset_prefix = 'Xray-macos'
    else:
        return {'success': False, 'message': f'不支持的系统: {system}'}
    
    # 映射架构名
    arch_map = {
        'amd64': '64',
        'x86_64': '64',
        'x86': '32',
        'i386': '32',
        'i686': '32',
        'arm64': 'arm64-v8a',
        'aarch64': 'arm64-v8a',
    }
    current_arch = arch_map.get(arch, arch)
    
    # 构建下载文件名
    asset_name = f"{asset_prefix}-{current_arch}.zip"
    target_extract_dir = XRAY_BIN_DIR / sys_name / current_arch

    download_url = f"https://github.com/XTLS/Xray-core/releases/latest/download/{asset_name}"
    
    print(f"[Xray] 正在从 {download_url} 下载内核...")
    try:
        # 使用 urllib 下载
        headers = {'User-Agent': 'Mozilla/5.0'}
        req = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(req) as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as z:
                target_extract_dir.mkdir(parents=True, exist_ok=True)
                # 只从中提取内核文件，减小磁盘占用
                binary_name = 'xray.exe' if system == 'windows' else 'xray'
                if binary_name in z.namelist():
                    z.extract(binary_name, target_extract_dir)
                else:
                    # 如果没找到，退而求其次全量解开
                    z.extractall(target_extract_dir)
        
        # 赋予执行权限 (Linux/macOS)
        if system != 'windows':
            xray_file = target_extract_dir / 'xray'
            if xray_file.exists():
                xray_file.chmod(0o755)
        
        # 重新刷新路径
        XRAY_BIN = get_xray_bin_path()
        if XRAY_BIN.exists():
            return {'success': True, 'message': f'Xray 下载成功: {XRAY_BIN}'}
        else:
            return {'success': False, 'message': '下载完成但未找到内核文件'}
            
    except Exception as e:
        return {'success': False, 'message': f'下载 Xray 失败: {e}'}


def start_xray() -> Dict[str, Any]:
    ensure_runtime_dir()
    global XRAY_BIN
    XRAY_BIN = get_xray_bin_path()
    if not XRAY_BIN.exists():
        # 尝试自动下载
        download_res = ensure_xray_binary()
        if not download_res['success']:
            return download_res
    
    if not CONFIG_PATH.exists():
        return {'success': False, 'message': '共享 Xray 配置不存在'}
    if is_xray_running():
        return {'success': True, 'message': 'Xray 已在运行'}
    
    kwargs = {}
    if platform.system().lower() != 'windows':
        kwargs['start_new_session'] = True
        
    with LOG_PATH.open('ab') as logf:
        proc = subprocess.Popen(
            [str(XRAY_BIN), 'run', '-c', str(CONFIG_PATH)],
            stdout=logf,
            stderr=subprocess.STDOUT,
            **kwargs
        )
    PID_PATH.write_text(str(proc.pid), encoding='utf-8')
    return {'success': True, 'message': f'Xray 已启动，PID={proc.pid}'}


def restart_xray() -> Dict[str, Any]:
    stop_xray()
    return start_xray()


def _vless_outbound(proxy: Dict[str, Any]) -> Dict[str, Any]:
    raw = proxy.get('raw_url', '')
    u = urlparse(raw)
    q = parse_qs(u.query)
    stream = {
        'network': q.get('type', ['tcp'])[0],
        'security': q.get('security', ['none'])[0],
    }
    if stream['security'] == 'reality':
        stream['realitySettings'] = {
            'publicKey': q.get('pbk', [''])[0],
            'shortId': q.get('sid', [''])[0],
            'serverName': q.get('sni', [''])[0],
            'fingerprint': q.get('fp', ['chrome'])[0],
            'spiderX': q.get('spx', [''])[0],
        }
    elif stream['security'] == 'tls':
        stream['tlsSettings'] = {
            'serverName': q.get('sni', [''])[0],
            'fingerprint': q.get('fp', ['chrome'])[0],
            'allowInsecure': q.get('allowInsecure', ['0'])[0] in ('1', 'true', 'True'),
        }
    if stream['network'] == 'ws':
        stream['wsSettings'] = {'path': q.get('path', ['/'])[0], 'headers': {}}
        if q.get('host', [''])[0]:
            stream['wsSettings']['headers']['Host'] = q.get('host', [''])[0]
    elif stream['network'] == 'grpc':
        stream['grpcSettings'] = {'serviceName': q.get('serviceName', [''])[0], 'multiMode': False}
    return {
        'tag': f"proxy-{proxy['id']}-out",
        'protocol': 'vless',
        'settings': {
            'vnext': [{
                'address': u.hostname,
                'port': u.port,
                'users': [{
                    'id': u.username,
                    'encryption': q.get('encryption', ['none'])[0],
                    'level': 0,
                }],
            }]
        },
        'streamSettings': stream,
    }


def _vmess_outbound(proxy: Dict[str, Any]) -> Dict[str, Any]:
    raw = proxy.get('raw_url', '')
    payload = raw[len('vmess://'):]
    padding = '=' * (-len(payload) % 4)
    data = json.loads(base64.b64decode(payload + padding).decode('utf-8'))
    stream = {
        'network': data.get('net', 'tcp'),
        'security': 'tls' if data.get('tls') and data.get('tls') != 'none' else 'none',
    }
    if stream['security'] == 'tls':
        stream['tlsSettings'] = {
            'serverName': data.get('sni') or data.get('host') or '',
            'fingerprint': data.get('fp') or 'chrome',
        }
    if stream['network'] == 'ws':
        stream['wsSettings'] = {'path': data.get('path') or '/', 'headers': {}}
        if data.get('host'):
            stream['wsSettings']['headers']['Host'] = data.get('host')
    return {
        'tag': f"proxy-{proxy['id']}-out",
        'protocol': 'vmess',
        'settings': {
            'vnext': [{
                'address': data.get('add'),
                'port': int(data.get('port', 0)),
                'users': [{
                    'id': data.get('id'),
                    'alterId': int(data.get('aid', 0)),
                    'security': data.get('scy', 'auto'),
                }],
            }]
        },
        'streamSettings': stream,
    }


def _http_or_socks_outbound(proxy: Dict[str, Any]) -> Dict[str, Any]:
    protocol = 'http' if proxy.get('type') == 'http' else 'socks'
    server = {
        'address': proxy.get('host', ''),
        'port': int(proxy.get('port', 0)),
    }
    if proxy.get('username') and proxy.get('password'):
        server['users'] = [{'user': proxy['username'], 'pass': proxy['password']}]
        
    return {
        'tag': f"proxy-{proxy['id']}-out",
        'protocol': protocol,
        'settings': {'servers': [server]},
    }


def _ss_outbound(proxy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = str(proxy.get('raw_url') or '').strip()
    if not raw.startswith('ss://'):
        return None
    body = raw[len('ss://'):].split('#', 1)[0]
    method = password = host = None
    port = None
    try:
        if '@' in body:
            userinfo, server = body.rsplit('@', 1)
            if ':' in userinfo and not userinfo.endswith('='):
                method, password = userinfo.split(':', 1)
            else:
                padding = '=' * (-len(userinfo) % 4)
                decoded = base64.urlsafe_b64decode(userinfo + padding).decode('utf-8')
                method, password = decoded.split(':', 1)
            host, port_s = server.rsplit(':', 1)
            port = int(port_s)
        else:
            padding = '=' * (-len(body) % 4)
            decoded = base64.urlsafe_b64decode(body + padding).decode('utf-8')
            creds, server = decoded.rsplit('@', 1)
            method, password = creds.split(':', 1)
            host, port_s = server.rsplit(':', 1)
            port = int(port_s)
        return {
            'tag': f"proxy-{proxy['id']}-out",
            'protocol': 'shadowsocks',
            'settings': {'servers': [{
                'address': host,
                'port': port,
                'method': method,
                'password': password,
                'level': 0,
            }]},
        }
    except Exception:
        return None


def build_outbound(proxy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ptype = proxy.get('type')
    if ptype == 'vless':
        return _vless_outbound(proxy)
    if ptype == 'vmess':
        return _vmess_outbound(proxy)
    if ptype in ('http', 'socks5'):
        return _http_or_socks_outbound(proxy)
    if ptype == 'ss':
        return _ss_outbound(proxy)
    return None


def build_shared_config(proxies: List[Dict[str, Any]]) -> Dict[str, Any]:
    used_ports = {int(p['local_http_port']) for p in proxies if p.get('local_http_port')}
    inbounds = []
    outbounds = []
    rules = []

    for proxy in proxies:
        if not proxy.get('enabled', True):
            continue
        outbound = build_outbound(proxy)
        if not outbound:
            continue
        port = proxy.get('local_http_port') or pick_free_port(used=used_ports)
        used_ports.add(int(port))
        proxy['local_http_port'] = int(port)
        inbound_tag = f"proxy-{proxy['id']}-in"
        outbound_tag = outbound['tag']
        inbounds.append({
            'tag': inbound_tag,
            'port': int(port),
            'listen': '127.0.0.1',
            'protocol': 'http',
            'settings': {},
        })
        outbounds.append(outbound)
        rules.append({
            'type': 'field',
            'inboundTag': [inbound_tag],
            'outboundTag': outbound_tag,
        })

    outbounds.append({'protocol': 'freedom', 'tag': 'direct'})
    return {
        'log': {'loglevel': 'warning'},
        'inbounds': inbounds,
        'outbounds': outbounds,
        'routing': {'domainStrategy': 'AsIs', 'rules': rules},
    }


def save_shared_config(config: Dict[str, Any]) -> Path:
    ensure_runtime_dir()
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    return CONFIG_PATH


def sync_shared_xray(proxies: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rebuild shared Xray config from proxies and ensure single shared process is running/stopped."""
    eligible = []
    for proxy in proxies:
        if not proxy.get('enabled', True):
            continue
        # For simplicity, we assign a temporary ID if not present
        if 'id' not in proxy:
            proxy['id'] = str(hash(proxy.get('raw_url', '')))
        
        outbound = build_outbound(proxy)
        if outbound:
            eligible.append(proxy)
    if not eligible:
        stop_xray()
        return {'success': True, 'message': '无可用 Xray 节点，已停止共享 Xray', 'proxies': proxies, 'running': False}
    
    config = build_shared_config(eligible)
    save_shared_config(config)
    result = restart_xray()
    result['proxies'] = proxies
    result['running'] = result.get('success', False)
    return result
