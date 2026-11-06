"""아빠 목소리 TTS 작업자 (.venv-voice 의 파이썬으로 실행, babyreel/voice.py 가 띄운다).

mlx-audio(Qwen3-TTS Base)는 의존성이 커서 메인 .venv 와 분리했다. 모델을 한 번 올려 두고
표준입력으로 한 줄씩 받은 JSON 요청을 처리한 뒤, 표준출력으로 한 줄 JSON 결과를 돌려준다.

요청: {"text", "out", "ref_audio", "ref_text", "seed", (선택) "temperature", "top_p", "top_k", "repetition_penalty"}
결과: {"ok": true, "seconds": 3.2} 또는 {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    model_id = sys.argv[1]
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model

    model = load_model(model_id)
    print(json.dumps({"ready": True, "sample_rate": model.sample_rate}), flush=True)
    for raw in sys.stdin:
        if not raw.strip():
            continue
        try:
            req = json.loads(raw)
            mx.random.seed(int(req.get("seed", 0)))
            chunks = [np.array(r.audio) for r in model.generate(
                text=req["text"], ref_audio=req["ref_audio"], ref_text=req["ref_text"],
                lang_code="korean", split_pattern="\n",
                **{k: req[k] for k in ("temperature", "top_p", "top_k", "repetition_penalty") if k in req})]
            audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
            sf.write(req["out"], audio, model.sample_rate, subtype="PCM_16")
            print(json.dumps({"ok": True, "seconds": len(audio) / model.sample_rate}), flush=True)
        except Exception as e:  # noqa: BLE001 - 요청 하나가 실패해도 작업자는 계속 산다
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), flush=True)


if __name__ == "__main__":
    main()
