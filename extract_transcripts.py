#!/usr/bin/env python3
"""YouTube動画の字幕とメタ情報を抽出するスクリプト（yt-dlp版）。

- urls.txt（または引数）のURLを順に処理する（1行1URL、#と空行は無視）
- 字幕があれば transcripts/<video_id>.md に「メタ情報＋字幕」を保存
- 字幕がなければ skipped.md に「理由: 字幕なし」等で記録してスキップ
- 分析（要約・キーポイント抽出など）は Claude Code on the web 側で行う
  （このスクリプトは抽出だけを担当し、API課金は発生しない）

ポイント:
  YouTube はクラウドIPからの通常アクセスをボット判定で弾くが、yt-dlp の
  player_client=android_vr を使うと cookie なしでも字幕・メタ情報を取得できる。
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

# 字幕の優先言語（この順で探し、無ければ取得できた言語を使う）
PREFERRED_LANGS = ["ja", "en"]
# ボット判定を回避できるプレーヤークライアント
PLAYER_CLIENT = "android_vr"

ROOT = Path(__file__).resolve().parent
URLS_FILE = ROOT / "urls.txt"
OUT_DIR = ROOT / "transcripts"
SKIP_FILE = ROOT / "skipped.md"
# 任意: ログイン済みcookie（Netscape形式）を置くと429回避に強くなる
COOKIE_FILE = ROOT / "cookies.txt"


def extract_video_id(line: str) -> str | None:
    """1行から video_id を取り出す（きっちり1行1URL前提）。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if re.fullmatch(r"[\w-]{11}", line):
        return line
    patterns = [
        r"(?:v=|/watch\?.*v=)([\w-]{11})",
        r"youtu\.be/([\w-]{11})",
        r"/shorts/([\w-]{11})",
        r"/embed/([\w-]{11})",
        r"/live/([\w-]{11})",
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


def _ydl_opts(tmpdir: str) -> dict:
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,        # 自動生成字幕も対象
        "subtitleslangs": PREFERRED_LANGS,
        "subtitlesformat": "json3",       # タイムスタンプ付きで扱いやすい
        "extractor_args": {"youtube": {"player_client": [PLAYER_CLIENT]}},
        "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # --- 429（レート制限）対策の自己スロットリング ---
        "retries": 5,
        "extractor_retries": 3,
        "sleep_interval_requests": 2,     # リクエスト間に最低2秒空ける
        "sleep_interval": 1,
        "max_sleep_interval": 5,
    }
    # cookies.txt があれば使う（ログイン済みなら429に強い）
    if COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)
    return opts


def parse_json3(path: Path) -> list[tuple[float, str]]:
    """json3字幕を (開始秒, テキスト) のリストに変換。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[float, str]] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        out.append((event.get("tStartMs", 0) / 1000.0, text))
    return out


def fetch(url: str, tmpdir: str):
    """yt-dlpで字幕＋メタ情報を取得。

    返り値: (info_dict, segments | None, lang | None)
    segments が None なら字幕なし。
    """
    with yt_dlp.YoutubeDL(_ydl_opts(tmpdir)) as ydl:
        info = ydl.extract_info(url, download=True)

    requested = info.get("requested_subtitles") or {}
    if not requested:
        return info, None, None

    lang = next((l for l in PREFERRED_LANGS if l in requested), next(iter(requested)))
    sub = requested[lang]
    path = sub.get("filepath")
    if not path:
        cands = list(Path(tmpdir).glob(f"*.{lang}.json3"))
        path = str(cands[0]) if cands else None
    if not path or not Path(path).exists():
        return info, None, None

    return info, parse_json3(Path(path)), lang


def meta_lines(info: dict) -> list[str]:
    """メタ情報をMarkdownの箇条書きに整形。"""
    up = info.get("upload_date")  # YYYYMMDD
    up_fmt = f"{up[:4]}-{up[4:6]}-{up[6:]}" if up and len(up) == 8 else (up or "不明")
    dur = info.get("duration")
    dur_fmt = fmt_time(dur) if isinstance(dur, (int, float)) else "不明"
    views = info.get("view_count")
    return [
        f"- タイトル: {info.get('title', '不明')}",
        f"- チャンネル: {info.get('uploader', info.get('channel', '不明'))}",
        f"- 公開日: {up_fmt}",
        f"- 長さ: {dur_fmt}",
        f"- 再生数: {views:,}" if isinstance(views, int) else "- 再生数: 不明",
        f"- URL: {info.get('webpage_url', '')}",
    ]


def save_transcript(video_id: str, info: dict, segments: list[tuple[float, str]], lang: str) -> Path:
    """メタ情報＋タイムスタンプ付き字幕＋全文を保存。"""
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{video_id}.md"

    lines = [f"# {info.get('title', video_id)}", ""]
    lines += meta_lines(info)
    lines += [f"- 字幕言語: {lang}", f"- セグメント数: {len(segments)}", ""]

    desc = (info.get("description") or "").strip()
    if desc:
        lines += ["## 概要欄", "", desc, ""]

    lines += ["## 字幕（タイムスタンプ付き）", ""]
    plain_parts = []
    for start, text in segments:
        lines.append(f"[{fmt_time(start)}] {text}")
        plain_parts.append(text)

    lines += ["", "## 全文（連結）", "", " ".join(plain_parts), ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def classify_error(err: Exception) -> str:
    """yt-dlpのエラーをスキップ理由に変換。"""
    msg = str(err)
    low = msg.lower()
    if "drm" in low:
        return "DRM保護で取得不可"
    if "private video" in low:
        return "非公開動画"
    if "members-only" in low or "members only" in low:
        return "メンバー限定動画"
    if "removed" in low or "no longer available" in low or "unavailable" in low:
        return "動画が利用不可（削除/非公開など）"
    if "age" in low and "confirm" in low:
        return "年齢制限"
    if "not a bot" in low or "sign in to confirm" in low:
        return "YouTubeにブロックされた（ボット判定）"
    return f"取得失敗: {type(err).__name__}"


def read_urls() -> list[str]:
    if len(sys.argv) > 1:
        return sys.argv[1:]
    if URLS_FILE.exists():
        return URLS_FILE.read_text(encoding="utf-8").splitlines()
    print(f"URLが指定されていません。引数で渡すか {URLS_FILE.name} を作成してください。")
    sys.exit(1)


def main() -> None:
    urls = read_urls()
    saved: list[str] = []
    skipped: list[tuple[str, str]] = []

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
            with tempfile.TemporaryDirectory() as tmp:
                info, segments, lang = fetch(raw, tmp)
                if segments is None:
                    skipped.append((raw, "字幕なし"))
                    print(f"SKIP  {video_id}  -> 字幕なし")
                    continue
                path = save_transcript(video_id, info, segments, lang)
        except DownloadError as e:
            reason = classify_error(e)
            skipped.append((raw, reason))
            print(f"SKIP  {video_id}  -> {reason}")
            continue
        except Exception as e:  # 想定外も止めずに記録して継続
            skipped.append((raw, f"取得失敗: {type(e).__name__}: {e}"))
            print(f"SKIP  {video_id}  -> 取得失敗 ({type(e).__name__})")
            continue

        saved.append(video_id)
        print(f"OK    {video_id}  -> {path.relative_to(ROOT)} ({lang}, {len(segments)}セグメント)")

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
