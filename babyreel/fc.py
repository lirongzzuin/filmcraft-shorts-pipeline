"""FilmCraft MCP 클라이언트.

`filmcraft-cli mcp` 를 stdio 로 띄워 하나의 헤드리스 편집 세션을 유지한다.
명령 결과로 받은 아이템/클립 id 를 다음 명령에 그대로 넘길 수 있어서
jsonl 스크립트(`filmcraft-cli run`)보다 빌드 로직을 짜기 쉽다.

Claude Code 에 등록된 MCP 서버(.mcp.json)와 같은 서버·같은 도구를 쓴다.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TICKS_PER_SECOND = 254_016_000_000


def cli_path() -> Path:
    env = os.environ.get("FILMCRAFT_CLI")
    path = Path(env) if env else ROOT / "tools" / "bin" / "filmcraft-cli"
    if not path.exists():
        raise FileNotFoundError(
            f"filmcraft-cli 를 찾을 수 없습니다: {path}\n"
            "  ./scripts/setup.sh 를 실행하거나 FILMCRAFT_CLI 환경변수로 경로를 지정하세요."
        )
    return path


def seconds_to_ticks(seconds: float) -> int:
    return round(seconds * TICKS_PER_SECOND)


class FilmCraftError(RuntimeError):
    pass


class FilmCraft:
    """헤드리스 FilmCraft 세션. `with FilmCraft() as fc:` 로 사용한다."""

    def __init__(self, project: Path | None = None, verbose: bool = False):
        args = [str(cli_path()), "mcp"]
        if project:
            args += ["--project", str(project)]
        self.verbose = verbose
        self._id = 0
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if not verbose else None,
            text=True,
            encoding="utf-8",
        )
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "babyreel", "version": "1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    # --- JSON-RPC -----------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        assert self._proc.stdin
        self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        assert self._proc.stdout
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise FilmCraftError(f"FilmCraft MCP 서버가 종료되었습니다 ({method})")
            msg = json.loads(line)
            if msg.get("id") != self._id:
                continue  # progress 등 알림
            if "error" in msg:
                raise FilmCraftError(f"{method}: {msg['error'].get('message')}")
            return msg["result"]

    def tool(self, name: str, **arguments: Any) -> Any:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise FilmCraftError(f"{name} {json.dumps(arguments, ensure_ascii=False)[:300]}: {text}")
        try:
            return json.loads(text) if text else None
        except json.JSONDecodeError:
            return text

    # --- 편의 함수 ---------------------------------------------------------------------

    def run(self, command: str, **params: Any) -> Any:
        trace = os.environ.get("BABYREEL_TRACE")  # 회귀 확인용: 보낸 편집 명령을 jsonl 로 기록
        if trace:
            with open(trace, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": command, "params": params}, ensure_ascii=False, sort_keys=True) + "\n")
        if self.verbose:
            print(f"  · {command} {json.dumps(params, ensure_ascii=False)[:160]}")
        return self.tool("command_run", id=command, params=params)

    def inspect(self) -> dict:
        return self.tool("doc_inspect")

    def sequence(self) -> dict:
        return self.tool("sequence_inspect")

    def render_frame(self, seconds: float, path: Path) -> None:
        self.run("file.exportFrame", path=str(path), seconds=seconds)

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                assert self._proc.stdin
                self._proc.stdin.close()
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()

    def __enter__(self) -> "FilmCraft":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def rel(path: Path) -> str:
    """출력용 경로: 프로젝트 안이면 상대 경로.
    library/YYYY-MM 이 외장 SSD 로의 심볼릭 링크일 수 있어 링크를 풀지 않은 경로를 먼저 본다."""
    for p in (Path(os.path.abspath(path)), Path(path).resolve()):
        try:
            return str(p.relative_to(ROOT))
        except ValueError:
            pass
    return str(path)
