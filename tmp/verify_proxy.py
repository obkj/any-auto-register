import sys
from pathlib import Path
# Add project root to path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from core.proxy_utils import parse_proxy_url

def test_parsing():
    test_urls = [
        "vmess://eyJhZGQiOiIxLjIuMy40IiwicG9ydCI6IjQ0MyIsImlkIjoiZjc0YmY0OTktNTJmNC00YjI5LWI0MzEtZmI5Mjg4YzFkYWZjIiwiYWlkIjoiMCIsInNjeSI6ImF1dG8iLCJuZXQiOiJ3cyIsInR5cGUiOiJub25lIiwiaG9zdCI6ImV4YW1wbGUuY29tIiwicGF0aCI6Ii9ncmFwaHFsIiwidGxzIjoidGxzIiwic25pIjoiZXhhbXBsZS5jb20iLCJmcCI6ImNocm9tZSJ9",
        "vless://f74bf499-52f4-4b29-b431-fb9288c1dafc@1.2.3.4:443?type=tcp&security=reality&pbk=pubkey&sid=shortid&sni=example.com&fp=chrome&spx=%2F#test-node",
        "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ@1.2.3.4:8388#ss-node",
        "http://user:pass@1.2.3.4:8080",
        "1.2.3.4:1080"
    ]
    
    for url in test_urls:
        print(f"Testing: {url[:50]}...")
        result = parse_proxy_url(url)
        print(f"Result: {result}")
        print("-" * 20)

if __name__ == "__main__":
    test_parsing()
