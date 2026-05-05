"""
EasyOCR vs Windows OCR 정확도/속도 비교.

사전 준비:
    1. 게임 실행 + 로그인 상태
    2. 스케줄 패널은 닫힌 상태여도 됨 (스크립트가 열고 닫음)
    3. pip install winsdk  # Windows OCR 바인딩

실행:
    python ocr_benchmark.py

출력:
    각 탭의 각 보스 행에 대해:
        [보스명]
          EasyOCR  : "텍스트" → 파싱결과  (Xms)
          WinOCR   : "텍스트" → 파싱결과  (Xms)
          [일치/불일치 마커]
    최종 요약: 총 N행 중 일치 M행, 각 엔진 평균 처리시간.
"""

import asyncio
import io
import logging
import re
import sys
import time
from datetime import timedelta

import easyocr
import mss
import numpy as np
from PIL import Image

# Windows OCR (winsdk)
from winsdk.windows.globalization import Language
from winsdk.windows.graphics.imaging import BitmapDecoder
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

from boss_config import TAB_BOSS_MAP, TAB_TIME_COORDS
from game_coords import (
    BASE_W, BASE_H,
    find_game_window,
    scale_xy, scale_w, scale_h,
    click_game,
)
from screen_sync import (
    SCHEDULE_ICON, SCHEDULE_CLOSE, TAB_COORDS,
    TIME_CROP_W, TIME_CROP_H,
    _TIME_PATTERNS, _SPAWNED_PATTERN, SPAWNED,
)

import ctypes

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


# ─── 파싱 (screen_sync 복제) ────────────────────────────────
def parse_time(text: str):
    if _SPAWNED_PATTERN.search(text):
        return SPAWNED
    for pattern, converter in _TIME_PATTERNS:
        m = pattern.search(text)
        if m:
            return converter(m)
    return None


def fmt_parsed(p) -> str:
    if p == SPAWNED:
        return "출현중"
    if isinstance(p, timedelta):
        total_s = int(p.total_seconds())
        h, r = divmod(total_s, 3600)
        m, s = divmod(r, 60)
        return f"{h}h{m}m{s}s"
    return "—"


# ─── Windows OCR 어댑터 ──────────────────────────────────────
_WIN_ENGINE = None


def _win_engine():
    global _WIN_ENGINE
    if _WIN_ENGINE is None:
        lang = Language("ko-KR")
        if not OcrEngine.is_language_supported(lang):
            raise RuntimeError("Windows OCR: ko-KR 미지원. 설정 > 시간 및 언어 > 한국어 언어팩 필요")
        _WIN_ENGINE = OcrEngine.try_create_from_language(lang)
        if _WIN_ENGINE is None:
            raise RuntimeError("Windows OCR: 엔진 생성 실패")
    return _WIN_ENGINE


async def _win_ocr_async(img_np: np.ndarray) -> str:
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

    result = await _win_engine().recognize_async(bitmap)
    # line 단위 결합 (OCR 결과가 여러 줄로 쪼개질 수 있음)
    return " ".join(line.text for line in result.lines)


def win_ocr(img_np: np.ndarray) -> str:
    return asyncio.run(_win_ocr_async(img_np))


# ─── EasyOCR 어댑터 ──────────────────────────────────────
_EASY_READER: easyocr.Reader | None = None


def easy_reader() -> easyocr.Reader:
    global _EASY_READER
    if _EASY_READER is None:
        print("[웜업] EasyOCR 모델 로딩 중...", flush=True)
        _EASY_READER = easyocr.Reader(["ko"], gpu=False)
        # 웜업 1회
        _EASY_READER.readtext(
            np.zeros((60, 300, 3), dtype=np.uint8), detail=0,
            allowlist="0123456789일시간분초남음출현중 ",
        )
        print("[웜업] EasyOCR 완료", flush=True)
    return _EASY_READER


def easy_ocr(img_np: np.ndarray) -> str:
    results = easy_reader().readtext(
        img_np, detail=1,
        allowlist="0123456789일시간분초남음출현중 ",
    )
    return " ".join(text for _, text, conf in results if conf > 0.3)


# ─── 캡처 파이프라인 ──────────────────────────────────────
def capture_row(sct, bx, by, cx, cy, cw, ch) -> np.ndarray:
    """보스 행 하나를 크롭해서 반환."""
    crop_w = scale_w(TIME_CROP_W, cw)
    crop_h = scale_h(TIME_CROP_H, ch)
    gx, gy = scale_xy((cx, cy), cw, ch)
    region = {
        "left": bx + gx - crop_w // 2,
        "top": by + gy - crop_h // 2,
        "width": crop_w,
        "height": crop_h,
    }
    shot = sct.grab(region)
    img = np.array(Image.frombytes("RGB", (shot.width, shot.height), shot.rgb))
    return img


# ─── 메인 ──────────────────────────────────────
def main():
    # 엔진 초기화
    _win_engine()
    easy_reader()

    hwnd, bx, by, cw, ch = find_game_window()
    print(f"[게임] 클라이언트 {cw}x{ch} @ ({bx},{by})")

    # 스케줄 열기
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    sx, sy = scale_xy(SCHEDULE_ICON, cw, ch)
    click_game(sx, sy, bx, by)
    time.sleep(1.5)

    rows = []  # {'tab', 'boss', 'img'}
    with mss.mss() as sct:
        for tab_name, tab_xy in TAB_COORDS.items():
            tx, ty = scale_xy(tab_xy, cw, ch)
            click_game(tx, ty, bx, by)
            time.sleep(1.0)

            bosses = TAB_BOSS_MAP.get(tab_name, [])
            coords = TAB_TIME_COORDS.get(tab_name, [])
            for boss_cfg, (cx, cy) in zip(bosses, coords):
                img = capture_row(sct, bx, by, cx, cy, cw, ch)
                rows.append({
                    "tab": tab_name,
                    "boss": boss_cfg.name,
                    "img": img,
                })

    # 스케줄 닫기
    sx, sy = scale_xy(SCHEDULE_CLOSE, cw, ch)
    click_game(sx, sy, bx, by)
    time.sleep(0.3)

    print(f"\n[캡처] {len(rows)} 행 수집 완료. OCR 비교 시작...\n")

    # 비교
    match = 0
    mismatch = 0
    easy_total_ms = 0
    win_total_ms = 0

    print(f"{'탭':8} {'보스':14} | {'EasyOCR':40} | {'Windows OCR':40} | ")
    print("─" * 140)

    for row in rows:
        img = row["img"]

        t0 = time.perf_counter()
        e_text = easy_ocr(img)
        e_ms = (time.perf_counter() - t0) * 1000
        e_parsed = parse_time(e_text)
        easy_total_ms += e_ms

        t0 = time.perf_counter()
        try:
            w_text = win_ocr(img)
        except Exception as ex:
            w_text = f"<ERR:{ex}>"
        w_ms = (time.perf_counter() - t0) * 1000
        w_parsed = parse_time(w_text)
        win_total_ms += w_ms

        agree = (e_parsed == w_parsed) if not (e_parsed is None and w_parsed is None) else False
        if e_parsed == w_parsed and e_parsed is not None:
            match += 1
            mark = "✓"
        elif e_parsed is None and w_parsed is None:
            mark = "?"  # 둘 다 실패
        else:
            mismatch += 1
            mark = "✗"

        e_summary = f'"{e_text[:25]:25}" {fmt_parsed(e_parsed):10} {int(e_ms):>4}ms'
        w_summary = f'"{w_text[:25]:25}" {fmt_parsed(w_parsed):10} {int(w_ms):>4}ms'
        print(f"{row['tab'][:8]:8} {row['boss'][:14]:14} | {e_summary} | {w_summary} | {mark}")

    n = len(rows)
    print("─" * 140)
    print(f"\n[요약]")
    print(f"  총 {n}행 — 일치 {match}, 불일치 {mismatch}, 둘 다 실패 {n - match - mismatch}")
    print(f"  EasyOCR 평균: {easy_total_ms/n:.0f}ms")
    print(f"  Windows OCR 평균: {win_total_ms/n:.0f}ms")
    print(f"  속도 차이: {easy_total_ms/win_total_ms:.2f}x")


if __name__ == "__main__":
    sys.exit(main())
