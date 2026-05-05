"""
보스 이동 자동화 대상 + 우선순위 설정.

동작:
  - AUTOMATION_TARGETS 에 없는 보스는 SOON이어도 자동화 스킵
    (TTS/Discord 알림은 그대로 전송됨)
  - 여러 보스가 동시 SOON이면 아래 2단계 정렬로 실행 순서 결정:
      1) REGION_PRIORITY 인덱스가 낮은 지역 먼저
      2) 같은 지역 내에선 AUTOMATION_TARGETS[region] 리스트 순서 (top=우선)

GUI에서 수정하면 automation_targets.json 에 저장되며, 다음 실행 시 로드됨.
파일을 삭제하면 아래 기본값으로 복원.
"""

import json
import logging

from models import BossConfig
from paths import DATA_DIR

logger = logging.getLogger(__name__)


# === 지역 우선순위 (위부터 높음) ===
REGION_PRIORITY: list[str] = [
    "아스",
    "무스펠",
    "알브",
    "니플",
    "니다벨",
    "요툰",
    # "미드",    # 비활성화
]


# === 지역별 자동화 대상 보스 (리스트 순서 = 우선순위) ===
# boss.config.name 기준으로 매칭 — boss_config.py 의 이름과 정확히 일치해야 함
AUTOMATION_TARGETS: dict[str, list[str]] = {
    "니플":   [],
    "아스":   ["발리","노트","샤무크","스칼드메르"],
    "무스펠": ["수르트", "우로보로스","엘드룬","탕그리스니르","헤르가름","신마라","메기르"],
    "알브":   ["오딘","굴베이그", "드라우그","모네가름", "두라스로르","스바르트"],
    "니다벨": ["토르","수드리","브륀힐드","라이노르", "라타토스크","스칼라니르","비요른", "헤르모드"],
    "요툰":   ["티르", "야른", "니드호그", "바우티", "페티", "파르바", "셀로비아", "흐니르"],
    # "미드":   ["그로아(미드)", "매트리악", "탕그뇨스트", "칼바람 하피", "레라드"],  # 비활성화
    "던전":   [],
}


# === 헬퍼 함수 ===

_MISSING = 9999  # 리스트에 없을 때 쓰는 큰 값


def is_automation_target(cfg: BossConfig) -> bool:
    """
    이 보스가 자동화 대상인지.
    - 지역이 REGION_PRIORITY 에 없으면 자동 제외
    - 지역이 있어도 AUTOMATION_TARGETS[region] 에 이름이 없으면 제외
    """
    if cfg.region not in REGION_PRIORITY:
        return False
    return cfg.name in AUTOMATION_TARGETS.get(cfg.region, [])


def priority_key(cfg: BossConfig) -> tuple[int, int]:
    """
    (region_idx, boss_idx) 반환 — 작을수록 높은 우선순위.
    지역이나 보스가 목록에 없으면 _MISSING(가장 낮은 우선순위).
    """
    region_idx = (REGION_PRIORITY.index(cfg.region)
                  if cfg.region in REGION_PRIORITY else _MISSING)
    boss_list = AUTOMATION_TARGETS.get(cfg.region, [])
    boss_idx = boss_list.index(cfg.name) if cfg.name in boss_list else _MISSING
    return (region_idx, boss_idx)


# === 런타임 편집 지원 (GUI에서 수정) ===

_TARGETS_JSON = DATA_DIR / "automation_targets.json"
_PRESETS_JSON = DATA_DIR / "automation_presets.json"


# === 스킬 프리셋 (보스별) ===
# boss_name → 프리셋 문자("A" 또는 "C"). 지정 없으면 "A" 로 간주.
# 도착 시 G+F 다음에 눌릴 키는 automator.SKILL_PRESET_KEYS 에서 매핑됨.
AUTOMATION_PRESETS: dict[str, str] = {}


def get_preset(boss_name: str) -> str:
    """보스의 스킬 프리셋 조회. 미지정 시 'A'."""
    return AUTOMATION_PRESETS.get(boss_name, "A")


def load_runtime_presets() -> bool:
    """automation_presets.json 이 있으면 AUTOMATION_PRESETS 를 덮어씀."""
    if not _PRESETS_JSON.exists():
        return False
    try:
        data = json.loads(_PRESETS_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON 루트가 dict 아님")
        AUTOMATION_PRESETS.clear()
        AUTOMATION_PRESETS.update(data)
        logger.info("[자동화] 프리셋 파일에서 로드 ← %s", _PRESETS_JSON.name)
        return True
    except Exception as e:
        logger.warning("[자동화] 프리셋 로드 실패 (%s): %s", _PRESETS_JSON.name, e)
        return False


def save_runtime_presets() -> bool:
    """현재 AUTOMATION_PRESETS 를 저장 (기본값 'A' 는 파일에서 생략)."""
    try:
        filtered = {k: v for k, v in AUTOMATION_PRESETS.items() if v != "A"}
        _PRESETS_JSON.write_text(
            json.dumps(filtered, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[자동화] 프리셋 저장 → %s (%d개)",
                    _PRESETS_JSON.name, len(filtered))
        return True
    except Exception as e:
        logger.error("[자동화] 프리셋 저장 실패: %s", e)
        return False


def update_presets(mapping: dict[str, str]) -> None:
    """GUI에서 편집된 프리셋 매핑으로 AUTOMATION_PRESETS 를 교체."""
    AUTOMATION_PRESETS.clear()
    AUTOMATION_PRESETS.update(mapping)


def load_runtime_targets() -> bool:
    """automation_targets.json 이 있으면 AUTOMATION_TARGETS 를 덮어씀.
    반환: 로드 성공 여부.
    """
    if not _TARGETS_JSON.exists():
        return False
    try:
        data = json.loads(_TARGETS_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON 루트가 dict 아님")
        AUTOMATION_TARGETS.clear()
        AUTOMATION_TARGETS.update(data)
        logger.info("[자동화] 우선순위 파일에서 로드 ← %s", _TARGETS_JSON.name)
        return True
    except Exception as e:
        logger.warning("[자동화] 우선순위 로드 실패 (%s): %s", _TARGETS_JSON.name, e)
        return False


def save_runtime_targets() -> bool:
    """현재 AUTOMATION_TARGETS 를 automation_targets.json 에 기록."""
    try:
        _TARGETS_JSON.write_text(
            json.dumps(AUTOMATION_TARGETS, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[자동화] 우선순위 저장 → %s", _TARGETS_JSON.name)
        return True
    except Exception as e:
        logger.error("[자동화] 우선순위 저장 실패: %s", e)
        return False


def update_region_targets(region: str, boss_names: list[str]) -> None:
    """GUI에서 특정 지역의 보스 순서를 업데이트 (dict 값 교체)."""
    AUTOMATION_TARGETS[region] = list(boss_names)
