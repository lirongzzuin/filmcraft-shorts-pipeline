"""TTS 에 넣기 전 한국어 글 → 읽는 말 (숫자·단위·기호). 표준 라이브러리만.

TTS 모델은 '3번'을 '삼 번'으로, '2시'를 '이 시'로 읽는 등 고유어(하나·둘·셋)/한자어(일·이·삼) 구분이 서툴다.
그래서 자막 글은 그대로 두고, 읽을 때만 사람이 읽는 말로 바꿔서 넘긴다 (docs/FEEDBACK.md 2026-10-20).

규칙 (국립국어원 수사 용법 기준):
- 고유어 + 관형사형(한·두·세·네·스무): 개·명·마리·살·시(시각)·시간·번(횟수)·번째·달·잔·병·권·장·벌·가지 …
  단 100 이상은 한자어 ('120개' → 백이십 개)
- 한자어: 분·초·일·주·개월·년·월(유월·시월)·원·층·회·차·도·점·kg·g·cm·ml·% · 번(순서: '육아일지 7번', '7번 클립')
- 소수 '4.9' → 사 점 구, 시각 '14:02' → 열네 시 이 분, 'D+61' → 디 플러스 육십일
- 단위 없는 숫자는 한자어 ('120까지' → 백이십까지)
"""

from __future__ import annotations

import re

_SINO_DIGITS = "영일이삼사오육칠팔구"
_NATIVE_ONES = ["", "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉"]
_NATIVE_TENS = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]
# 단위 앞에서 줄어드는 꼴 (하나 개 → 한 개)
_PRENOUN = {"하나": "한", "둘": "두", "셋": "세", "넷": "네", "스물": "스무"}

NATIVE_COUNTERS = (
    "시간", "번째", "가지", "군데", "바퀴", "방울", "모금", "숟가락", "걸음", "마디", "켤레", "송이", "그루",
    "개", "명", "마리", "살", "시", "번", "달", "잔", "병", "권", "장", "벌", "대", "통", "판", "곡", "입", "밤", "끼",
)
SINO_COUNTERS = (
    "개월", "주년", "주째", "일째", "년째", "학년", "킬로그램", "그램", "센티미터", "센티", "밀리리터", "미리", "퍼센트",
    "분", "초", "일", "주", "년", "월", "원", "층", "회", "차", "도", "점", "위", "등", "호", "인분", "배",
)
UNITS = {"kg": "킬로그램", "g": "그램", "cm": "센티미터", "mm": "밀리미터", "ml": "밀리리터", "cc": "씨씨", "%": "퍼센트",
         "km": "킬로미터", "m": "미터", "l": "리터", "L": "리터"}
# '번'이 순서(한자어)로 읽히는 앞말: 육아일지 7번, 7번 클립, 제7번
_ORDINAL_BEFORE = ("육아일지", "일지", "No.", "No", "넘버", "제", "번호", "버스", "출구", "트랙", "채널", "에피소드")


def sino(n: int) -> str:
    """한자어 수: 120 → 백이십, 10 → 십, 10000 → 만, 0 → 영."""
    if n == 0:
        return "영"
    out = []
    for unit, name in ((10 ** 12, "조"), (10 ** 8, "억"), (10 ** 4, "만")):
        if n >= unit:
            head = n // unit
            out.append(("" if head == 1 and name == "만" else _under_10000(head)) + name)
            n %= unit
    if n:
        out.append(_under_10000(n))
    return "".join(out)


def _under_10000(n: int) -> str:
    s = ""
    for unit, name in ((1000, "천"), (100, "백"), (10, "십")):
        d = n // unit
        if d:
            s += ("" if d == 1 else _SINO_DIGITS[d]) + name
        n %= unit
    if n:
        s += _SINO_DIGITS[n]
    return s


def native(n: int, prenoun: bool = True) -> str:
    """고유어 수 (1~99): 3 → 세(단위 앞)/셋, 20 → 스무/스물, 21 → 스물한. 100 이상·0 은 한자어."""
    if not 0 < n < 100:
        return sino(n)
    tens, ones = _NATIVE_TENS[n // 10], _NATIVE_ONES[n % 10]
    if prenoun:
        if ones:
            ones = _PRENOUN.get(ones, ones)
        elif tens == "스물":
            tens = "스무"
    return tens + ones


def month(n: int) -> str:
    return {6: "유", 10: "시"}.get(n, sino(n)) + "월"


def _decimal(s: str) -> str:
    whole, _, frac = s.partition(".")
    return sino(int(whole)) + " 점 " + " ".join(_SINO_DIGITS[int(d)] for d in frac)


def _num(s: str) -> int:
    return int(s.replace(",", ""))


# 겹받침 대표음 (표준 발음법 10·11항): 말 끝·쉼 앞에서만. '잠듦'→[잠듬], '없'→[업], '닭'→[닥], '여덟'→[여덜]
# TTS 가 겹받침을 'ㄹ+ㅁ' 처럼 둘 다 내려다 끝음을 끈다 (2026-10-20: '잠듦'을 '잠들~'로 읽음)
_JONG_SIMPLE = {3: 1, 5: 4, 6: 4, 9: 1, 10: 16, 11: 8, 12: 8, 13: 8, 14: 17, 15: 8, 18: 17}


def pronounce_finals(text: str) -> str:
    out = list(text)
    for i, ch in enumerate(out):
        code = ord(ch) - 0xAC00
        if not 0 <= code < 11172 or code % 28 not in _JONG_SIMPLE:
            continue
        nxt = out[i + 1] if i + 1 < len(out) else ""
        if nxt and "가" <= nxt <= "힣":
            continue  # 뒤에 글자가 이어지면 연음 등으로 달라지므로 그대로 둔다 ('닭이' [달기])
        out[i] = chr(0xAC00 + code - code % 28 + _JONG_SIMPLE[code % 28])
    return "".join(out)


def spoken(text: str) -> str:
    """자막 글 → TTS 가 읽을 말. 숫자가 없는 문장은 기호만 정리한다."""
    t = text.replace("\n", " ")
    t = re.sub(r"[ㅋㅎㅠㅜ]{2,}", "", t)  # ㅋㅋ·ㅠㅠ 는 읽지 않는다
    t = re.sub(r"[~♡♥★☆]+", "", t)
    t = t.replace("…", ", ").replace("...", ", ").replace("·", ", ")

    # D+61 / D-3
    t = re.sub(r"\bD\s*([+-])\s*(\d+)", lambda m: f"디 {'플러스' if m[1] == '+' else '마이너스'} {sino(int(m[2]))}", t)
    # 시각 14:02 → 열네 시 이 분 (24시간제는 그대로 읽는다)
    t = re.sub(r"(\d{1,2}):(\d{2})", lambda m: f"{native(int(m[1]))} 시" + (f" {sino(int(m[2]))} 분" if int(m[2]) else ""), t)
    # 날짜: 12월 14일, 2024년
    t = re.sub(r"(\d{1,2})\s*월", lambda m: month(int(m[1])), t)
    # 소수 + 단위/기호
    t = re.sub(r"(\d+\.\d+)\s*(kg|g|cm|mm|ml|cc|km|%)?",
               lambda m: _decimal(m[1]) + (" " + UNITS[m[2]] if m[2] else ""), t)
    # 영문 단위·% (정수)
    t = re.sub(r"(\d[\d,]*)\s*(kg|cm|mm|ml|cc|km|%)(?![A-Za-z])", lambda m: f"{sino(_num(m[1]))} {UNITS[m[2]]}", t)
    # 'No.7', '#7' → 칠 번
    t = re.sub(r"(?:No\.?|#)\s*(\d+)", lambda m: f"{sino(int(m[1]))} 번", t)

    def counter(m: re.Match) -> str:
        before = m.string[max(0, m.start() - 12):m.start()].rstrip()
        n, unit = _num(m[1]), m[2]
        if unit == "번째":
            return ("첫" if n == 1 else native(n)) + " 번째"
        if (unit == "번" and before.endswith(_ORDINAL_BEFORE)) or (unit == "시" and n > 24) or n >= 100:
            return sino(n) + " " + unit
        return native(n) + " " + unit

    native_alt = "|".join(sorted(NATIVE_COUNTERS, key=len, reverse=True))
    sino_alt = "|".join(sorted(SINO_COUNTERS, key=len, reverse=True))
    # 고유어 단위를 먼저 ('시간'·'번째'처럼 긴 것부터). '분이'는 한자어 '분'과 겹치므로 고유어 목록에서 뺀다
    t = re.sub(rf"(\d[\d,]*)\s*({native_alt})(?!월|일|분|초|주|년)", counter, t)
    t = re.sub(rf"(\d[\d,]*)\s*({sino_alt})", lambda m: f"{sino(_num(m[1]))} {m[2]}", t)
    # 남은 숫자는 한자어
    t = re.sub(r"\d[\d,]*", lambda m: sino(_num(m[0])), t)
    t = pronounce_finals(t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    t = re.sub(r",(\s*,)+", ",", t)
    return t.strip(" ,")
