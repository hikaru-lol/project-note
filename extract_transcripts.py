#!/usr/bin/env python3
"""YouTube動画の字幕を抽出するスクリプト。

- urls.txt（または引数）のURLを順に処理する（1行1URL、#と空行は無視）
- 字幕があれば transcripts/<video_id>.md に保存
- 字幕がなければ skipped.md に「理由: 字幕なし」等で記録してスキップ
- 分析（要約・キーポイント抽出など）は Claude Code on the web 側で行う
  （このスクリプトはテキスト抽出だけを担当し、API課金は発生しない）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    InvalidVideoId,
    AgeRestricted,
    VideoUnplayable,
    IpBlocked,
    RequestBlocked,
)

# 字幕の優先言語（この順で探し、無ければ利用可能な任意の言語を使う）
PREFERRED_LANGS = ["ja", "en"]

ROOT = Path(__file__).resolve().parent
URLS_FILE = ROOT / "urls.txt"
OUT_DIR = ROOT / "transcripts"
SKIP_FILE = ROOT / "skipped.md"


def extract_video_id(line: str) -> str | None:
    """1行から video_id を取り出す（きっちり1行1URL前提）。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # 11文字のIDだけが渡された場合
    if re.fullmatch(r"[\w-]{11}", line):
        return line
    patterns = [
        r"(?:v=|/watch\?.*v=)([\w-]{11})",  # watch?v=ID
        r"youtu\.be/([\w-]{11})",            # youtu.be/ID
        r"/shorts/([\w-]{11})",              # /shorts/ID
        r"/embed/([\w-]{11})",               # /embed/ID
        r"/live/([\w-]{11})",                # /live/ID
    ]
    for pat in patterns:
        m = re.search(pat, line)
        if m:
            return m.group(1)
    return None


def fmt_time(seconds: float) -> str:
    """秒を [hh:mm:ss] / [mm:ss] に整形。"""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def fetch_transcript(api: YouTubeTranscriptApi, video_id: str):
    """利用可能な字幕を取得。優先言語→任意言語の順で試す。

    返り値: (FetchedTranscript, language_code)
    TranscriptsDisabled / NoTranscriptFound は「字幕なし」として扱う。
    """
    transcript_list = api.list(video_id)  # 字幕が一切無ければ TranscriptsDisabled
    available = [t.language_code for t in transcript_list]
    ordered = [l for l in PREFERRED_LANGS if l in available]
    ordered += [l for l in available if l not in ordered]
    if not ordered:
        raise NoTranscriptFound(video_id, PREFERRED_LANGS, transcript_list)
    fetched = api.fetch(video_id, languages=ordered)
    return fetched, ordered[0]


def save_transcript(video_id: str, url: str, fetched, lang: str) -> Path:
    """タイムスタンプ付き字幕と、分析用の連結全文を保存。"""
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{video_id}.md"

    lines = [
        f"# Transcript: {video_id}",
        "",
        f"- URL: {url}",
        f"- 言語: {lang}",
        f"- セグメント数: {len(fetched)}",
        "",
        "## 字幕（タイムスタンプ付き）",
        "",
    ]
    plain_parts = []
    for snippet in fetched:
        text = snippet.text.replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"[{fmt_time(snippet.start)}] {text}")
        plain_parts.append(text)

    lines += ["", "## 全文（連結）", "", " ".join(plain_parts), ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def read_urls() -> list[str]:
    """引数があればそれを、無ければ urls.txt を読む。"""
    if len(sys.argv) > 1:
        return sys.argv[1:]
    if URLS_FILE.exists():
        return URLS_FILE.read_text(encoding="utf-8").splitlines()
    print(f"URLが指定されていません。引数で渡すか {URLS_FILE.name} を作成してください。")
    sys.exit(1)


def main() -> None:
    urls = read_urls()
    api = YouTubeTranscriptApi()

    saved: list[str] = []
    skipped: list[tuple[str, str]] = []  # (url, 理由)

    for raw in urls:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        video_id = extract_video_id(raw)
        if not video_id:
            skipped.append((raw, "URL解析失敗（動画IDを取得できず）"))
            print(f"SKIP  {raw}  -> URL解析失敗")
            continue
        try:
            fetched, lang = fetch_transcript(api, video_id)
        except (TranscriptsDisabled, NoTranscriptFound):
            skipped.append((raw, "字幕なし"))
            print(f"SKIP  {video_id}  -> 字幕なし")
            continue
        except (VideoUnavailable, VideoUnplayable, AgeRestricted, InvalidVideoId) as e:
            skipped.append((raw, f"動画にアクセス不可: {type(e).__name__}"))
            print(f"SKIP  {video_id}  -> アクセス不可 ({type(e).__name__})")
            continue
        except (IpBlocked, RequestBlocked) as e:
            skipped.append((raw, f"YouTube側でブロック: {type(e).__name__}"))
            print(f"SKIP  {video_id}  -> ブロック ({type(e).__name__})")
            continue
        except Exception as e:  # 想定外も止めずに記録して継続
            skipped.append((raw, f"取得失敗: {type(e).__name__}: {e}"))
            print(f"SKIP  {video_id}  -> 取得失敗 ({type(e).__name__})")
            continue

        path = save_transcript(video_id, raw, fetched, lang)
        saved.append(video_id)
        print(f"OK    {video_id}  -> {path.relative_to(ROOT)} ({lang}, {len(fetched)}セグメント)")

    if skipped:
        skip_lines = ["# スキップした動画", ""]
        for url, reason in skipped:
            skip_lines.append(f"- {url}\n  - 理由: {reason}")
        SKIP_FILE.write_text("\n".join(skip_lines) + "\n", encoding="utf-8")

    print()
    print(f"完了: 保存 {len(saved)}件 / スキップ {len(skipped)}件")
    if skipped:
        print(f"スキップ詳細: {SKIP_FILE.name}")
    if saved:
        print(f"字幕は {OUT_DIR.name}/ に保存しました。これらを Claude Code が分析します。")


if __name__ == "__main__":
    main()
