"""图片 OCR（RapidOCR：纯 pip、内置中英文 onnx 模型，无需系统 tesseract）。

对外暴露 ocr_image(path_or_bytes) -> str，返回识别出的文本。
模型懒加载：首次调用时初始化 onnxruntime + RapidOCR。
"""
from __future__ import annotations

_ocr = None
_OCR_TRY = False


def _get_ocr():
    global _ocr, _OCR_TRY
    if _OCR_TRY:
        return _ocr
    _OCR_TRY = True
    try:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    except Exception:
        _ocr = None
    return _ocr


def ocr_image(path_or_bytes) -> str:
    """对本地图片文件识别文本。支持路径字符串或 bytes。失败返回 ""。"""
    ocr = _get_ocr()
    if ocr is None:
        return ""
    try:
        result, _ = ocr(path_or_bytes)
    except Exception:
        return ""
    if not result:
        return ""
    lines = []
    for it in result:
        try:
            txt = it[1] if len(it) > 1 else ""
            if isinstance(txt, str) and txt.strip():
                lines.append(txt.strip())
        except Exception:
            continue
    return "\n".join(lines)
