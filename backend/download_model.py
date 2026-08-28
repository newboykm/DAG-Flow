import os
import time
import httpx

BASE = "https://hf-mirror.com/BAAI/bge-small-zh-v1.5/resolve/main"
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "bge-small-zh-v1.5")
os.makedirs(DEST, exist_ok=True)

FILES = [
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "vocab.txt",
    "1_Pooling/config.json",
]

CHUNK = 8 * 1024 * 1024

def head_size(url):
    with httpx.Client(timeout=httpx.Timeout(60, connect=30), follow_redirects=True) as c:
        r = c.head(url)
        if r.status_code in (200, 206):
            return int(r.headers.get("content-length", 0))
        # HEAD 可能失败，退回 GET 读取 content-length
        with c.stream("GET", url) as g:
            return int(g.headers.get("content-length", 0) or 0)

def fetch_range(url, start, end, dest):
    headers = {"Range": f"bytes={start}-{end}"}
    with httpx.Client(timeout=httpx.Timeout(120, connect=30), follow_redirects=True) as c:
        with c.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            with open(dest, "r+b") as f:
                f.seek(start)
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)

for name in FILES:
    url = f"{BASE}/{name}"
    dest = os.path.join(DEST, name.replace("/", os.sep))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        size = head_size(url)
    except Exception as e:
        print("head fail", name, repr(e)[:120]); continue
    print("== size", name, size)
    if size <= 0 or (os.path.exists(dest) and os.path.getsize(dest) == size):
        print("skip", name); continue
    with open(dest, "wb") as f:
        f.truncate(size)
    start = 0
    attempt = 0
    while start < size:
        end = min(start + CHUNK - 1, size - 1)
        try:
            fetch_range(url, start, end, dest)
            print(f"  ok {name} {start}-{end}")
            start = end + 1
            attempt = 0
        except Exception as e:
            attempt += 1
            if attempt > 8:
                print("GIVE UP", name, repr(e)[:160]); break
            print(f"  retry {name} {start} ({attempt}): {repr(e)[:100]}")
            time.sleep(1.5 * attempt)

print("DONE ->", DEST)
