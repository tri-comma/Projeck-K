import os
import re
import unicodedata
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from mangum import Mangum
from ytmusicapi import YTMusic

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration ---

# 「ボーカルなし（カラオケ）」とみなすチャンネル (White List)
TRUSTED_KARAOKE_CHANNELS = [
    "カラオケ歌っちゃ王",
    "歌っちゃ王",
    "JOYSOUND CHANNEL",
    "JOYSOUND",
    "カラオケDAM公式チャンネル",
    "Karaoke",
    "J-POP Karaoke",
    "GARAOKE",
    "TJ KARAOKE",
    "カラオケまねきねこ",
    "Victor Entertainment",
    "Sony Music",
    "UNIVERSAL MUSIC",
    "ニコカラチャンネル",
    "On-boc",
    "EdKara" 
]

# 「ボーカルなし（カラオケ）」とみなすキーワード
OFF_VOCAL_KEYWORDS = [
    "instrumental", "off vocal", "backing track", "karaoke", 
    "インスト", "オフボーカル", "カラオケ", "ガイド"
]

# 検索結果から除外するキーワード (Negative Filter)
NEGATIVE_KEYWORDS = [
    "歌ってみ", "×(修正版あり)",
    "bgm", "orgel", "オルゴール", "medley", "メドレー",
    "reaction", "リアクション", "切り抜き"
]

ytmusic = YTMusic(language='ja', location='JP')

# --- Models ---

class SongItem(BaseModel):
    video_id: str
    title: str
    artist: str
    original_title: str
    duration: str
    channel: str
    result_type: str # 'song' or 'video'
    is_no_guide: bool = False
    has_vocal: bool = True
    key: Optional[str] = None
    type: str # フロントエンドのアイコン判定用に追加 (v2.2)

# --- Utilities ---

def clean_text(text: str):
    if not text: return ""
    return text.replace("　", " ").strip()

def normalize_for_comparison(text: str):
    if not text: return ""
    normalized = unicodedata.normalize('NFKC', text).lower()
    cleaned = re.sub(r'[!！?？、。.,・･~～\-−_＿\s「」『』()（）【】\[\]/／]', '', normalized)
    return cleaned

def determine_attributes(title: str, channel: str):
    title_lower = title.lower()
    is_no_guide = any(k in title_lower for k in ["ガイドなし", "ガイド無し", "no guide", "ガイドメロディなし", "ガイドメロディ無し"])
    
    has_vocal = True
    if any(c in channel for c in TRUSTED_KARAOKE_CHANNELS):
        has_vocal = False
    elif any(k in title_lower for k in OFF_VOCAL_KEYWORDS):
        has_vocal = False

    return is_no_guide, has_vocal

def parse_metadata(original_title: str, artist_from_api: str, channel_name: str, result_type: str):
    title = original_title
    artist = artist_from_api
    key = None
    
    clean_t = original_title.replace("（カラオケ）", "").replace("(Karaoke)", "").replace("(Official)", "")
    clean_t = re.sub(r"[\[\(]ガイド.*?(?:無し|なし)カラオケ[\]\)]", "", clean_t)

    # --- NicoKara Logic ---
    if "ニコカラ" in original_title or "ニコカラ" in channel_name:
        def nico_clean(t):
            if not t: return ""
            t = re.sub(r"【(?:ニコカラ|カラオケ).*?】", "", t)
            t = re.sub(r"(?i)[【\[\(]?(?:off\s*vocal|オフボーカル|karaoke|カラオケ|instrumental|インスト|guide|ガイド).*?[】\]\)]?", "", t)
            t = t.replace("ニコカラ", "")
            return t.strip()

        match_brackets = re.search(r"(.*?)\s*《(.*?)》", original_title)
        is_bracket_valid = False
        if match_brackets:
            inner_text = match_brackets.group(2)
            noise_check = ["off vocal", "offvocal", "key", "キー", "guide", "ガイド", "karaoke", "カラオケ"]
            if not any(n in inner_text.lower() for n in noise_check):
                is_bracket_valid = True
        
        if is_bracket_valid:
            title = match_brackets.group(1)
            artist = match_brackets.group(2)
        elif re.search(r"[/／]", original_title):
            parts = re.split(r"[/／]", original_title, 1)
            title = parts[0]
            artist = parts[1]
        else:
            title = original_title
            artist = ""

        title = nico_clean(title)
        artist = nico_clean(artist)

    # --- Videoの場合 ---
    elif result_type == "video":
        artist = ""
        if channel_name == "JOYSOUND CHANNEL":
            if "【合唱練習用】" in original_title:
                match_chorus = re.search(r"「(.*?)」", original_title)
                title = match_chorus.group(1) if match_chorus else original_title.replace("【合唱練習用】", "").strip()
                artist = "合唱練習用"
            else:
                temp = original_title.replace("【karaoke】", "").replace("【JOYSOUND】", "").strip()
                if "/" in temp:
                    parts = temp.split("/", 1)
                    def extract_jp(text):
                        if text.endswith(")") and "(" in text:
                            last_open = text.rfind("(")
                            if last_open != -1: return text[last_open+1:-1]
                        return text
                    title = extract_jp(parts[0].strip())
                    artist = extract_jp(parts[1].strip())
                else:
                    title = temp

        elif "歌っちゃ王" in channel_name:
            temp_title = re.sub(r"【.*?】", "", original_title).strip()
            temp_title = temp_title.replace("（カラオケ）", "").replace("(Karaoke)", "").replace("(Official)", "")
            temp_title = re.sub(r"[\[\(]ガイド.*?(?:無し|なし)カラオケ[\]\)]", "", temp_title)
            
            re_prefix = r"(?i)(?:Key|キー)\s*[:：]?\s*([+＋\-−–—ー－]?\d+)"
            re_suffix = r"(?i)([+＋\-−–—ー－]?\d+)\s*(?:Key|キー)"
            match_p = re.search(re_prefix, temp_title)
            match_s = re.search(re_suffix, temp_title)
            raw_key = None
            if match_p:
                raw_key = match_p.group(1); temp_title = re.sub(re_prefix, "", temp_title)
            elif match_s:
                raw_key = match_s.group(1); temp_title = re.sub(re_suffix, "", temp_title)
            if raw_key:
                key_val = raw_key.replace("＋", "+").translate(str.maketrans("−–—ー－", "-----"))
                key = f"{key_val}KEY"

            match_artist = re.search(r"[\[\(\【［（](?:原曲歌手|オリジナルアーティスト|オリジナル歌手)[:：](.*?)[\]\)\】］）]", temp_title)
            if match_artist:
                artist = match_artist.group(1).strip()
                temp_title = re.sub(r"[\[\(\【［（](?:原曲歌手|オリジナルアーティスト|オリジナル歌手)[:：].*?[\]\)\】］）]", "", temp_title)
                title = temp_title.strip()
            else:
                if " / " in temp_title:
                    parts = temp_title.split(" / ", 1); title = parts[0].strip(); artist = parts[1].replace("[カラオケ]", "").strip()
                elif "/" in temp_title:
                    parts = temp_title.split("/", 1); title = parts[0].strip(); artist = parts[1].replace("[カラオケ]", "").strip()
                else:
                    title = temp_title.strip()
        
        elif "EdKara" in channel_name:
            temp_title = re.sub(r"^(?:練習用)?(?:Karaoke|カラオケ)[♬♪]*\s*", "", original_title, flags=re.IGNORECASE).strip()
            match_performed = re.search(r"[\(\[\【](?:Originally Performed by|Original Artist)[:\s]+(.*?)[\]\)\】]", temp_title, re.IGNORECASE)
            if match_performed:
                artist = match_performed.group(1).strip()
                title_part = temp_title.split(match_performed.group(0))[0]
                title = re.sub(r"[\[\(\【].*?[\]\)\】]", "", title_part).strip()
            elif " - " in temp_title:
                parts = temp_title.split(" - ", 1); title = parts[0].strip(); artist_candidate = parts[1].strip()
                cutoff_pattern = r"(?:【|\[|\(|Instrumental|Off Vocal)"
                split_match = re.search(cutoff_pattern, artist_candidate, re.IGNORECASE)
                if split_match: artist_candidate = artist_candidate[:split_match.start()]
                artist = re.sub(r"(?i)Karaoke[♬♪]*", "", artist_candidate).strip()
            elif "/" in temp_title:
                parts = temp_title.split("/", 1); title = parts[0].strip(); artist = parts[1].strip()
            else:
                title = re.sub(r"【.*?】", "", temp_title).strip()
        else:
            if "/" in clean_t:
                parts = clean_t.split("/", 1); title = parts[0]; artist = parts[1].replace("[カラオケ]", "").strip()
            elif " - " in clean_t:
                parts = clean_t.split(" - ", 1); title = parts[0]; artist = parts[1]
            else:
                title = clean_t
                if any(k in channel_name for k in ["Official", "Music", "Records"]): artist = channel_name

    # --- Songの場合 ---
    elif result_type == "song":
        if "まねきねこ" in artist_from_api or "Manekineko" in artist_from_api:
            temp_title = original_title
            match_key = re.search(r"([+-]?\d+KEY)", temp_title)
            if match_key:
                key = match_key.group(1); temp_title = re.sub(r"[+-]?\d+KEY", "", temp_title)
            match_artist_long = re.search(r"\[Originally Performed By (.*?)\]", temp_title, re.IGNORECASE)
            match_artist_short = re.search(r"\[(.*?)\]$", temp_title)
            if match_artist_long:
                artist = match_artist_long.group(1).strip()
                temp_title = re.sub(r"\[Originally Performed By .*?\]", "", temp_title, flags=re.IGNORECASE)
            elif match_artist_short:
                artist = match_artist_short.group(1).strip()
                temp_title = re.sub(r"\[.*?\]$", "", temp_title)
            title = temp_title.replace("（カラオケ）", "").replace("(カラオケ)", "").strip()

        elif "歌っちゃ王" in artist_from_api:
            temp_title = original_title
            re_prefix = r"(?i)(?:Key|キー)\s*[:：]?\s*([+＋\-−–—ー－]?\d+)"
            re_suffix = r"(?i)([+＋\-−–—ー－]?\d+)\s*(?:Key|キー)"
            match_p = re.search(re_prefix, temp_title); match_s = re.search(re_suffix, temp_title)
            raw_key = None
            if match_p:
                raw_key = match_p.group(1); temp_title = re.sub(re_prefix, "", temp_title)
            elif match_s:
                raw_key = match_s.group(1); temp_title = re.sub(re_suffix, "", temp_title)
            if raw_key:
                key_val = raw_key.replace("＋", "+").translate(str.maketrans("−–—ー－", "-----"))
                key = f"{key_val}KEY"
            match_artist = re.search(r"[\[\(\【［（](?:原曲歌手|オリジナルアーティスト|オリジナル歌手)[:：](.*?)[\]\)\】］）]", temp_title)
            if match_artist:
                artist = match_artist.group(1).strip()
                temp_title = re.sub(r"[\[\(\【［（](?:原曲歌手|オリジナルアーティスト|オリジナル歌手)[:：].*?[\]\)\】］）]", "", temp_title)
            temp_title = temp_title.replace("（カラオケ）", "").replace("(カラオケ)", "")
            title = re.sub(r"[\[\(]ガイド.*?(?:無し|なし)カラオケ[\]\)]", "", temp_title).strip()

        elif "カラオケ" in artist_from_api or "Karaoke" in artist_from_api:
             if "/" in original_title:
                parts = original_title.split("/", 1); title = parts[0]; artist = parts[1].replace("【カラオケ音源】", "").strip()

    return clean_text(title), clean_text(artist), key

def calculate_relevance_score(query: str, title: str, artist: str, original_title: str):
    if not query: return 0
    score = 0
    query_parts = query.split()
    target_text_raw = f"{title} {artist} {original_title}"
    target_norm = normalize_for_comparison(target_text_raw)
    for part in query_parts:
        part_norm = normalize_for_comparison(part)
        if not part_norm: continue
        if part_norm in target_norm: score += 20000 
    return score

# --- API Endpoints ---

@app.get("/api/search")
async def search(q: Optional[str] = None):
    if not q:
        return {"results": [], "next_page_token": None}

    search_query = f"{q} カラオケ"
    temp_results = []
    seen_ids = set()

    try:
        song_results = ytmusic.search(search_query, filter="songs", limit=20)
        video_results = ytmusic.search(search_query, filter="videos", limit=40)
        
        all_items = []
        for item in song_results:
            item['_type'] = 'song'
            all_items.append(item)
        for item in video_results:
            item['_type'] = 'video'
            all_items.append(item)

        for item in all_items:
            vid = item.get("videoId")
            if not vid or vid in seen_ids: continue

            original_title = item.get("title", "")
            title_lower = original_title.lower()
            if any(ng in title_lower for ng in NEGATIVE_KEYWORDS): continue
            
            artists = item.get("artists", [])
            api_artist_name = artists[0]["name"] if artists else ""
            channel_name = api_artist_name if api_artist_name else "YouTube Music"

            is_no_guide, has_vocal = determine_attributes(original_title, channel_name)
            parsed_title, parsed_artist, key = parse_metadata(original_title, api_artist_name, channel_name, item['_type'])

            # SongItemの作成 (typeフィールドを v2.2 用にセット)
            song_obj = SongItem(
                video_id=vid,
                title=parsed_title,
                artist=parsed_artist,
                original_title=original_title,
                duration=item.get("duration") or "00:00",
                channel=channel_name,
                result_type=item['_type'],
                is_no_guide=is_no_guide,
                has_vocal=has_vocal,
                key=key,
                type=item['_type'] # アイコン表示用に 'song' または 'video' をそのまま代入
            )

            score = 0
            relevance = calculate_relevance_score(q, parsed_title, parsed_artist, original_title)
            score += relevance
            if not has_vocal: score += 5000 
            if any(c in channel_name for c in TRUSTED_KARAOKE_CHANNELS): score += 3000
            if item['_type'] == 'song': score += 100

            temp_results.append({ "data": song_obj, "score": score })
            seen_ids.add(vid)

    except Exception as e:
        print(f"Search Error: {e}")
        return {"results": [], "next_page_token": None}

    temp_results.sort(key=lambda x: x["score"], reverse=True)
    final_results = [x["data"] for x in temp_results]

    return {"results": final_results, "next_page_token": None}

handler = Mangum(app)