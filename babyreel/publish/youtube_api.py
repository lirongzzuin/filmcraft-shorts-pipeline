"""YouTube Data API v3 업로더: '비공개(임시저장)' 상태로 올린다.

YouTube API 에는 '초안(draft)' 상태가 없다. 그래서 비공개로 업로드해 두고
episodes/<회차>/<숏폼1·숏폼2·롱폼>/유튜브_업로드결과.txt 에 Studio 편집 링크를 남긴다.
사람은 링크를 열어 최종 확인 후 '공개' 또는 '예약'만 누르면 된다.

⚠ 실제 계정으로 아직 검증하지 않은 코드다. 켜기 전에 docs/AUTOMATION.md 의 순서대로
   비공개(private) 업로드 1건으로 먼저 시험할 것.

준비:
  pip install google-api-python-client google-auth-oauthlib
  Google Cloud 콘솔 → YouTube Data API v3 사용 설정 → OAuth 클라이언트(데스크톱 앱) 생성
  → JSON 을 secrets/youtube_client_secret.json 에 저장
  config/channel.toml [publish] youtube_shorts = "youtube_api" (또는 youtube_long)

참고: 미인증(감사 전) API 프로젝트로 올린 영상은 비공개로 잠긴다.
      공개 업로드를 하려면 Google 의 API 감사(audit)를 받아야 한다.
      기본 쿼터는 하루 10,000 유닛이고 업로드 1건에 약 1,600 유닛이 든다.
"""

from __future__ import annotations


from ..fc import ROOT
from .base import PublishResult, Publisher

SECRETS = ROOT / "secrets"
CLIENT_SECRET = SECRETS / "youtube_client_secret.json"
TOKEN = SECRETS / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"]


def _service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise RuntimeError("pip install google-api-python-client google-auth-oauthlib") from e

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise RuntimeError(f"{CLIENT_SECRET} 가 없습니다. docs/AUTOMATION.md 참고")
            creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES).run_local_server(port=0)
        SECRETS.mkdir(exist_ok=True)
        TOKEN.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


class YouTubePublisher(Publisher):
    name = "youtube_api"

    def publish(self, target: str, meta: dict) -> PublishResult:
        yt = _service()
        from googleapiclient.http import MediaFileUpload

        body = {
            "snippet": {
                "title": meta["title"],
                "description": meta["description"],
                "tags": meta["tags"],
                "categoryId": meta["category_id"],
                "defaultLanguage": meta["language"],
                "defaultAudioLanguage": meta["language"],
            },
            "status": {
                "privacyStatus": "private",  # 항상 비공개 = 임시저장. 공개는 사람이 Studio 에서
                "selfDeclaredMadeForKids": meta["made_for_kids"],
                "containsSyntheticMedia": meta["contains_synthetic_media"],
            },
        }
        if meta.get("publish_at"):  # ISO8601. 예약 공개는 privacy=private 이어야 함
            body["status"]["publishAt"] = meta["publish_at"]
        media = MediaFileUpload(str(ROOT / meta["file"]), chunksize=8 * 1024 * 1024, resumable=True,
                                mimetype="video/mp4")
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            status, resp = req.next_chunk()
            if status:
                print(f"    업로드 {int(status.progress() * 100)}%")
        vid = resp["id"]
        if meta.get("thumbnail") and target == "youtube_long":  # 쇼츠 썸네일은 API 로 지정할 수 없다 (앱에서)
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(ROOT / meta["thumbnail"]))).execute()
        url = f"https://youtube.com/shorts/{vid}" if target != "youtube_long" else f"https://youtu.be/{vid}"
        studio = f"https://studio.youtube.com/video/{vid}/edit"
        self._note(target, meta, url, studio)
        return PublishResult(target=target, status="draft", url=url, remote_id=vid, detail=studio)

    def _note(self, target: str, meta: dict, url: str, studio: str) -> None:
        from .. import deliver
        from ..plan import Plan

        from . import video_of
        from ..plan import TARGET_NAMES

        d = deliver.folder(Plan.load(self.episode_dir), "youtube", video_of(target))
        kind = TARGET_NAMES[video_of(target)]
        with (d / "유튜브_업로드결과.txt").open("a", encoding="utf-8") as f:
            f.write(
                f"[{kind}] 비공개(임시저장)로 업로드됨\n"
                f"  제목: {meta['title']}\n"
                f"  Studio에서 열기: {studio}\n"
                f"  → {meta.get('upload_date') or '올릴 날'}에 영상 확인 후 '공개 상태'를 공개로 바꾸고 저장\n"
                f"  공개 후 주소: {url}\n\n"
            )
