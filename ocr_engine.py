"""Windows OCR 어댑터 (winsdk 기반).

엔진 1회 초기화 후 재사용. ko-KR 언어팩 필요 (대부분의 한국어 Windows엔 기본 설치됨).

공용 API:
    ocr_text(img_np)   → str        : 모든 라인 텍스트 연결 (간단 용도)
    ocr_lines(img_np)  → list[OcrLine]: line 단위 텍스트 + bbox (행 매핑 용)
"""

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import List

import numpy as np
from PIL import Image

from winsdk.windows.globalization import Language
from winsdk.windows.graphics.imaging import BitmapDecoder
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

logger = logging.getLogger(__name__)

_ENGINE: OcrEngine | None = None


@dataclass
class OcrLine:
    text: str
    y_center: float   # bbox 세로 중심 (입력 이미지 픽셀 기준)
    height: float


def _get_engine() -> OcrEngine:
    global _ENGINE
    if _ENGINE is None:
        lang = Language("ko-KR")
        if not OcrEngine.is_language_supported(lang):
            raise RuntimeError(
                "Windows OCR: ko-KR 언어팩 미설치 — "
                "설정 → 시간 및 언어 → 한국어 언어팩 설치 필요"
            )
        engine = OcrEngine.try_create_from_language(lang)
        if engine is None:
            raise RuntimeError("Windows OCR: 엔진 생성 실패")
        _ENGINE = engine
        logger.info("[OCR] Windows OCR 엔진 초기화 (ko-KR)")
    return _ENGINE


async def _recognize_async(img_np: np.ndarray):
    # 그레이스케일 → RGB 3채널로 확장
    if img_np.ndim == 2:
        img_np = np.stack([img_np] * 3, axis=-1)
    pil = Image.fromarray(img_np)
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    data = buf.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(data)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    return await _get_engine().recognize_async(bitmap)


def warmup():
    """OCR 엔진 초기화 + 더미 이미지 1회 recognize.
    첫 실전 호출의 레이턴시(엔진 init + COM 준비)를 앱 시작 시점으로 이동.
    """
    import time as _time
    t0 = _time.perf_counter()
    try:
        _get_engine()  # 언어팩 확인 + 엔진 생성
        # 작은 검은 이미지 1회 recognize — 실제 호출 경로 전체를 데움
        dummy = np.zeros((40, 120, 3), dtype=np.uint8)
        asyncio.run(_recognize_async(dummy))
    except Exception as e:
        logger.warning("[OCR] 웜업 실패: %s", e)
        return
    logger.info("[OCR] 웜업 완료 (%dms)", int((_time.perf_counter() - t0) * 1000))


def warmup_async():
    """백그라운드 스레드에서 웜업 — GUI 블로킹 방지."""
    import threading
    threading.Thread(target=warmup, daemon=True, name="OcrWarmup").start()


def ocr_text(img_np: np.ndarray) -> str:
    """모든 라인 텍스트를 공백으로 이어붙여 반환. 실패 시 빈 문자열."""
    try:
        result = asyncio.run(_recognize_async(img_np))
    except Exception as e:
        logger.warning("[OCR] 호출 실패: %s", e)
        return ""
    return " ".join(line.text for line in result.lines)


def ocr_lines(img_np: np.ndarray) -> List[OcrLine]:
    """line 단위로 (text, y_center, height) 반환. line.words 의 bbox 평균으로 계산."""
    try:
        result = asyncio.run(_recognize_async(img_np))
    except Exception as e:
        logger.warning("[OCR] 호출 실패: %s", e)
        return []
    out: List[OcrLine] = []
    for line in result.lines:
        if not line.words:
            continue
        ys = [w.bounding_rect.y + w.bounding_rect.height / 2 for w in line.words]
        hs = [w.bounding_rect.height for w in line.words]
        out.append(OcrLine(
            text=line.text,
            y_center=sum(ys) / len(ys),
            height=sum(hs) / len(hs),
        ))
    return out
