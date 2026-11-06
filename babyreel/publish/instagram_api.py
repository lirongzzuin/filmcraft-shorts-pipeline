"""Instagram Graph API 릴스 업로더 (자동화 전환용).

⚠ 실제 계정으로 아직 검증하지 않은 코드다. docs/AUTOMATION.md 순서대로 먼저 시험할 것.

Instagram 콘텐츠 게시 API 의 제약:
  - 계정이 '프로페셔널(비즈니스/크리에이터)' 이고 Meta 앱 + instagram_content_publish 권한 필요
  - 영상은 로컬 파일이 아니라 **공개 URL** 로 넘겨야 한다 (Meta 서버가 가져감)
    → 결과 mp4 를 잠깐 올려둘 곳이 필요 (예: Cloudflare R2/S3 presigned URL, Vercel Blob)
  - 24시간에 게시물 API 발행 수 제한이 있다 (계정당 50건 수준)

환경변수:
  IG_USER_ID       Instagram 비즈니스 계정 id
  IG_ACCESS_TOKEN  장기 액세스 토큰
설정: config/channel.toml [publish] instagram_reels = "instagram_api"
      [instagram] public_base_url = "https://..."   (out/ 파일이 이 주소 아래에서 보여야 함)
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from .base import PublishResult, Publisher

GRAPH = "https://graph.facebook.com/v21.0"


def _call(method: str, path: str, **params: str) -> dict:
    data = urllib.parse.urlencode(params).encode()
    url = f"{GRAPH}/{path}"
    if method == "GET":
        url, data = f"{url}?{data.decode()}", None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=60) as r:
        return json.loads(r.read())


class InstagramPublisher(Publisher):
    name = "instagram_api"

    def publish(self, target: str, meta: dict) -> PublishResult:
        from .. import config

        user, token = os.environ.get("IG_USER_ID"), os.environ.get("IG_ACCESS_TOKEN")
        base = config.load().get("instagram", {}).get("public_base_url")
        if not (user and token and base):
            raise RuntimeError("IG_USER_ID / IG_ACCESS_TOKEN 환경변수와 [instagram] public_base_url 이 필요합니다")
        video_url = f"{base.rstrip('/')}/{meta['file']}"
        container = _call("POST", f"{user}/media", media_type="REELS", video_url=video_url,
                          caption=meta["caption"], share_to_feed=str(meta.get("share_to_feed", True)).lower(),
                          access_token=token)["id"]
        for _ in range(60):  # 최대 10분 대기
            status = _call("GET", container, fields="status_code,status", access_token=token)
            if status.get("status_code") == "FINISHED":
                break
            if status.get("status_code") == "ERROR":
                return PublishResult(target=target, status="failed", detail=json.dumps(status))
            time.sleep(10)
        else:
            return PublishResult(target=target, status="failed", detail="처리 시간 초과")
        media_id = _call("POST", f"{user}/media_publish", creation_id=container, access_token=token)["id"]
        link = _call("GET", media_id, fields="permalink", access_token=token).get("permalink")
        return PublishResult(target=target, status="uploaded", url=link, remote_id=media_id)
