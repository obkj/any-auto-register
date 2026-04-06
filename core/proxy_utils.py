import base64
import json
import re
import time
from typing import Optional, Tuple, Dict, Any
from urllib.parse import unquote, urlsplit, urlunsplit, urlparse, parse_qs
from curl_cffi import requests as cffi_requests


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """将 socks5:// 规范化为 socks5h://，避免本地 DNS 泄漏。"""
    if proxy_url is None:
        return None

    value = str(proxy_url).strip()
    if not value:
        return None

    parts = urlsplit(value)
    if (parts.scheme or "").lower() == "socks5":
        parts = parts._replace(scheme="socks5h")
        return urlunsplit(parts)
    return value


def build_requests_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def build_playwright_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy_url:
        return None

    parts = urlsplit(proxy_url)
    if not parts.scheme or not parts.hostname or parts.port is None:
        return {"server": proxy_url}

    config = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        config["username"] = unquote(parts.username)
    if parts.password:
        config["password"] = unquote(parts.password)
    return config


def parse_proxy_url(raw: str) -> dict:
    """解析各种协议的代理 URL (http, socks5, ss, vmess, vless)"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    
    # 抽取可能的 URL (处理一些带前缀的格式)
    prefixes = ['http://', 'https://', 'socks5://', 'socks5h://', 'ss://', 'vmess://', 'vless://']
    positions = [raw.find(p) for p in prefixes if raw.find(p) != -1]
    if positions:
        raw = raw[min(positions):].strip()

    if raw.startswith(('http://', 'https://', 'socks5://', 'socks5h://')):
        u = urlparse(raw)
        ptype = 'socks5' if u.scheme.startswith('socks5') else 'http'
        return {
            'name': (u.hostname or raw)[:100],
            'type': ptype,
            'host': u.hostname or '',
            'port': u.port or 0,
            'username': unquote(u.username) if u.username else None,
            'password': unquote(u.password) if u.password else None,
            'raw_url': raw,
        }
    
    if raw.startswith('ss://'):
        return {
            'name': (unquote(raw.split('#')[-1]) if '#' in raw else 'ss-node')[:100],
            'type': 'ss',
            'host': '',
            'port': 0,
            'raw_url': raw,
        }

    if raw.startswith('vless://'):
        u = urlparse(raw)
        q = parse_qs(u.query)
        return {
            'name': (unquote(raw.split('#')[-1]) if '#' in raw else (u.hostname or 'vless-node'))[:100],
            'type': 'vless',
            'host': u.hostname or '',
            'port': u.port or 0,
            'username': unquote(u.username) if u.username else None,
            'raw_url': raw,
        }

    if raw.startswith('vmess://'):
        payload = raw[len('vmess://'):].split('#', 1)[0]
        padding = '=' * (-len(payload) % 4)
        try:
            data = json.loads(base64.b64decode(payload + padding).decode('utf-8'))
        except Exception:
            data = {}
        return {
            'name': (data.get('ps') or 'vmess-node')[:100],
            'type': 'vmess',
            'host': data.get('add', ''),
            'port': int(data.get('port', 0) or 0),
            'username': data.get('id'),
            'raw_url': raw,
        }

    # 兼容 ip:port 格式 (默认 socks5)
    if '://' not in raw and ':' in raw:
        host, port = raw.rsplit(':', 1)
        if port.isdigit():
            return {
                'name': raw[:100],
                'type': 'socks5',
                'host': host,
                'port': int(port),
                'raw_url': f'socks5://{raw}',
            }

    return {}


def check_ip_location(proxy_url: Optional[str] = None) -> Tuple[bool, str]:
    """
    检查 IP 地理位置及 OpenAI 适用性
    Returns: (is_supported, location_text)
    """
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        # 使用 curl_cffi 模拟 Chrome 以防被拦截
        resp = cffi_requests.get(
            "https://cp.cloudflare.com/cdn-cgi/trace",
            proxies=proxies,
            timeout=10,
            impersonate="chrome110"
        )
        trace_text = resp.text

        ip_match = re.search(r"ip=([^\n\r]+)", trace_text)
        loc_match = re.search(r"loc=([A-Z]+)", trace_text)
        
        ip = ip_match.group(1).strip() if ip_match else "unknown"
        loc = loc_match.group(1).strip() if loc_match else "unknown"

        location_text = f"IP={ip}, loc={loc}"
        
        # OpenAI 不支持的地区
        unsupported = ["CN", "HK", "MO", "TW", "RU", "IR", "KP", "SY", "CU", "BY", "VE"]
        if loc in unsupported:
            return False, location_text
        
        return True, location_text
    except Exception as e:
        return False, f"Check failed: {e}"


def test_proxy_connectivity(proxy_url: str, max_retries: int = 2) -> Dict[str, Any]:
    """综合测试代理连通性"""
    last_error = ""
    for attempt in range(max_retries + 1):
        start = time.time()
        is_supported, location = check_ip_location(proxy_url)
        elapsed = round((time.time() - start) * 1000)
        
        if "Check failed" not in location:
            return {
                "success": True,
                "is_supported": is_supported,
                "location": location,
                "response_time": elapsed,
                "message": f"Connect success. {'Supported' if is_supported else 'Unsupported region'}: {location}"
            }
        last_error = location
        if attempt < max_retries:
            time.sleep(1)
            
    return {
        "success": False,
        "is_supported": False,
        "message": f"Connect failed after {max_retries+1} attempts: {last_error}"
    }
