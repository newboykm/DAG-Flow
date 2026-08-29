"""下载本地模型（hf-mirror 分段下载 + 断点续传）。

用法：
    python download_model.py                # 下载 embedding 模型 bge-small-zh-v1.5
    python download_model.py --reranker     # 下载 Reranker 模型 bge-reranker-base

说明：经由 hf-mirror.com 下载；大文件走 Range 分段 + 重试，断点续传（已存在且大小一致则跳过）。
"""
import os
import re
import sys
import time
import httpx

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SPECS = {
    # key: (hf_id, 目标目录, 文件清单)
    "embed": (
        "BAAI/bge-small-zh-v1.5",
        os.path.join(_ROOT, "models", "bge-small-zh-v1.5"),
        [
            "config.json",
            "model.safetensors",
            "tokenizer.json",
            "tokenizer_config.json",
            "modules.json",
            "sentence_bert_config.json",
            "special_tokens_map.json",
            "vocab.txt",
            "1_Pooling/config.json",
        ],
    ),
    "reranker": (
        "BAAI/bge-reranker-base",
        os.path.join(_ROOT, "models", "bge-reranker-base"),
        [
            "config.json",
            "model.safetensors",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "sentencepiece.bpe.model",
        ],
    ),
}

CHUNK = 8 * 1024 * 1024


def verify_weights(dest_dir) -> bool:
    """校验 model.safetensors 结构+数据可读、非空洞。损坏返回 False。"""
    wf = os.path.join(dest_dir, "model.safetensors")
    if not os.path.exists(wf):
        return False
    try:
        from safetensors import safe_open
        with safe_open(wf, framework="pt") as f:
            keys = list(f.keys())
            # 抽样读取若干段权重，触发完整 mmap 读取，检测空洞/损坏
            for k in keys[:: max(1, len(keys) // 8)]:
                f.get_tensor(k)
        return True
    except Exception:
        return False


def head_size(url):
    with httpx.Client(timeout=httpx.Timeout(60, connect=30), follow_redirects=True) as c:
        r = c.head(url)
        if r.status_code in (200, 206):
            return int(r.headers.get("content-length", 0))
        with c.stream("GET", url) as g:
            return int(g.headers.get("content-length", 0) or 0)


def fetch_range(url, start, end, dest):
    headers = {"Range": f"bytes={start}-{end}"}
    with httpx.Client(timeout=httpx.Timeout(180, connect=30), follow_redirects=True) as c:
        with c.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            with open(dest, "r+b") as f:
                f.seek(start)
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)


def download(name_key):
    hf_id, dest, files = SPECS[name_key]
    os.makedirs(dest, exist_ok=True)
    base = f"https://hf-mirror.com/{hf_id}/resolve/main"
    for name in files:
        url = f"{base}/{name}"
        dest_path = os.path.join(dest, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        try:
            size = head_size(url)
        except Exception as e:
            print("head fail", name, repr(e)[:120])
            continue
        print(f"== {name_key} {name} ({size})")
        if size <= 0 or (os.path.exists(dest_path) and os.path.getsize(dest_path) == size):
            if name == "model.safetensors" and os.path.exists(dest_path) and not verify_weights(dest):
                print("  existing model.safetensors corrupted (holes); re-download", name)
                os.remove(dest_path)
            else:
                print("skip", name)
                continue
        with open(dest_path, "wb") as f:
            f.truncate(size)
        start, attempt = 0, 0
        while start < size:
            end = min(start + CHUNK - 1, size - 1)
            try:
                fetch_range(url, start, end, dest_path)
                print(f"  ok {name} {start}-{end}")
                start = end + 1
                attempt = 0
            except Exception as e:
                attempt += 1
                if attempt > 10:
                    print("GIVE UP", name, repr(e)[:160])
                    break
                print(f"  retry {name} {start} ({attempt}): {repr(e)[:100]}")
                time.sleep(1.5 * attempt)
    print("DONE ->", dest)


if __name__ == "__main__":
    if "--reranker" in sys.argv:
        download("reranker")
    else:
        download("embed")
