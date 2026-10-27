"""Keisei (京成電鉄) + Shin-Keisei (新京成/松戸線) live train feed.

Source: zaisen.tid-keisei.jp - the Keisei app's web 在線 view, plain JSON,
no key, 30s polled:
    https://zaisen.tid-keisei.jp/data/traffic_info.json?ts=<ms>      main network
    https://zaisen.tid-keisei.jp/data/matsudo_train_info.json?ts=<ms> 松戸線

Main feed shape:
    UP[0].dt[0]{yy,mt,dy,hh,mm,ss}  -> data timestamp
    UP[0].st                         -> service status
    TS[]{id:"E001", tr[]{no,bs,sy,ik,dl,hk,sr}}
        no  = train number (suffix letter = operator/section, e.g. 15AE05 / 90F)
        bs  = block slot index on the line SVG
        sy  = train-type code  (syasyu.json)
        ik  = destination code (ikisaki.json)
        dl  = delay in MINUTES
        hk  = 0=上り(U) / 1=下り(D)
        sr  = display direction code (unused on the board)

The web app positions trains by matching TS.id+bs against <use id="E001_U1_..">
slots baked into per-line SVGs (svg/keisei.svg, e_narita.svg, access.svg,
oshiage.svg, kanamati.svg, chiba.svg, matsudo.svg). Which line a TS block lives
on, and its station, was extracted from those SVGs + /config/coordinate.json
(rs = rosen code) and is committed as BLOCK_TABLE below.

松戸線 trains that appear at junction blocks (E027/E062) in the main feed are
SKIPPED here - the dedicated matsudo_train_info.json (which also feeds the
新京成 running-position page) emits them instead, so the board shows each train
once. Matsudo-line destination codes (131-136, 221-237) are treated the same.
"""

from . import common

BASE = "https://zaisen.tid-keisei.jp"
TRAFFIC_URL = BASE + "/data/traffic_info.json?ts="
MATSUDO_URL = BASE + "/data/matsudo_train_info.json?ts="

DELAY_THRESHOLD_MIN = 1

# TS block id -> (rosen_code, railway, line_ja, station_ja)
#   rosen_code: 1=本線 2=東成田 3=スカイアクセス 4=押上 5=金町 6=千葉/千原 7=松戸
# Extracted from svg/{keisei,e_narita,access,oshiage,kanamati,chiba,matsudo}.svg
# block-slot <use> ids nearest-matched to config/coordinate.json (rs=rosen).
BLOCK_TABLE = {
    "E001": (1, "Keisei.Main", "京成本線", "京成上野"),
    "E003": (1, "Keisei.Main", "京成本線", "日暮里"),
    "E004": (1, "Keisei.Main", "京成本線", "新三河島"),
    "E005": (1, "Keisei.Main", "京成本線", "町屋"),
    "E006": (1, "Keisei.Main", "京成本線", "千住大橋"),
    "E007": (1, "Keisei.Main", "京成本線", "京成関屋"),
    "E008": (1, "Keisei.Main", "京成本線", "堀切菖蒲園"),
    "E009": (1, "Keisei.Main", "京成本線", "お花茶屋"),
    "E010": (1, "Keisei.Main", "京成本線", "青砥"),
    "E011": (1, "Keisei.Main", "京成本線", "京成高砂"),
    "E012": (1, "Keisei.Main", "京成本線", "京成小岩"),
    "E013": (1, "Keisei.Main", "京成本線", "江戸川"),
    "E014": (1, "Keisei.Main", "京成本線", "国府台"),
    "E015": (1, "Keisei.Main", "京成本線", "市川真間"),
    "E016": (1, "Keisei.Main", "京成本線", "菅野"),
    "E017": (1, "Keisei.Main", "京成本線", "京成八幡"),
    "E018": (1, "Keisei.Main", "京成本線", "鬼越"),
    "E019": (1, "Keisei.Main", "京成本線", "京成中山"),
    "E020": (1, "Keisei.Main", "京成本線", "東中山"),
    "E021": (1, "Keisei.Main", "京成本線", "京成西船"),
    "E022": (1, "Keisei.Main", "京成本線", "海神"),
    "E023": (1, "Keisei.Main", "京成本線", "京成船橋"),
    "E024": (1, "Keisei.Main", "京成本線", "大神宮下"),
    "E025": (1, "Keisei.Main", "京成本線", "船橋競馬場"),
    "E026": (1, "Keisei.Main", "京成本線", "谷津"),
    "E027": (1, "Keisei.Main", "京成本線", "京成津田沼"),
    "E028": (1, "Keisei.Main", "京成本線", "京成大久保"),
    "E029": (1, "Keisei.Main", "京成本線", "実籾"),
    "E030": (1, "Keisei.Main", "京成本線", "八千代台"),
    "E031": (1, "Keisei.Main", "京成本線", "京成大和田"),
    "E032": (1, "Keisei.Main", "京成本線", "勝田台"),
    "E033": (1, "Keisei.Main", "京成本線", "志津"),
    "E034": (1, "Keisei.Main", "京成本線", "ユーカリが丘"),
    "E035": (1, "Keisei.Main", "京成本線", "京成臼井"),
    "E036": (1, "Keisei.Main", "京成本線", "京成佐倉"),
    "E037": (1, "Keisei.Main", "京成本線", "大佐倉"),
    "E038": (1, "Keisei.Main", "京成本線", "京成酒々井"),
    "E039": (1, "Keisei.Main", "京成本線", "宗吾参道"),
    "E040": (1, "Keisei.Main", "京成本線", "公津の杜"),
    "E041": (1, "Keisei.Main", "京成本線", "京成成田"),
    "E044": (1, "Keisei.Main", "京成本線", "空港第２ビル"),
    "E045": (1, "Keisei.Main", "京成本線", "成田空港"),
    "E046": (2, "Keisei.HigashiNarita", "東成田線", "東成田"),
    "E047": (4, "Keisei.Oshiage", "押上線", "押上"),
    "E048": (4, "Keisei.Oshiage", "押上線", "京成曳舟"),
    "E049": (4, "Keisei.Oshiage", "押上線", "八広"),
    "E050": (4, "Keisei.Oshiage", "押上線", "四ツ木"),
    "E051": (4, "Keisei.Oshiage", "押上線", "京成立石"),
    "E052": (5, "Keisei.Kanamachi", "金町線", "柴又"),
    "E053": (5, "Keisei.Kanamachi", "金町線", "京成金町"),
    "E054": (6, "Keisei.Chiba", "千葉線", "京成幕張本郷"),
    "E055": (6, "Keisei.Chiba", "千葉線", "京成幕張"),
    "E056": (6, "Keisei.Chiba", "千葉線", "検見川"),
    "E057": (6, "Keisei.Chiba", "千葉線", "京成稲毛"),
    "E058": (6, "Keisei.Chiba", "千葉線", "みどり台"),
    "E059": (6, "Keisei.Chiba", "千葉線", "西登戸"),
    "E060": (6, "Keisei.Chiba", "千葉線", "新千葉"),
    "E061": (6, "Keisei.Chiba", "千葉線", "京成千葉"),
    "E062": (6, "Keisei.Chiba", "千葉線", "千葉中央"),
    "E080": (6, "Keisei.Chihara", "千原線", "大森台"),
    "E081": (6, "Keisei.Chihara", "千原線", "学園前"),
    "E082": (6, "Keisei.Chihara", "千原線", "おゆみ野"),
    "E083": (6, "Keisei.Chihara", "千原線", "ちはら台"),
    "E084": (6, "Keisei.Chihara", "千原線", "千葉寺"),
    "E130": (2, "Keisei.HigashiNarita", "東成田線", "芝山千代田"),
    "E131": (7, "Keisei.Matsudo", "松戸線", "新津田沼"),
    "E132": (7, "Keisei.Matsudo", "松戸線", "高根公団"),
    "E133": (7, "Keisei.Matsudo", "松戸線", "鎌ヶ谷大仏"),
    "E134": (7, "Keisei.Matsudo", "松戸線", "くぬぎ山"),
    "E135": (7, "Keisei.Matsudo", "松戸線", "八柱"),
    "E136": (7, "Keisei.Matsudo", "松戸線", "松戸"),
    "E140": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "矢切"),
    "E141": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "新鎌ヶ谷"),
    "E142": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "西白井"),
    "E143": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "千葉ニュータウン中央"),
    "E144": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "印西牧の原"),
    "E145": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "印旛日本医大"),
    "E146": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "成田湯川"),
    "E147": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "成田湯川"),
    "E148": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "東松戸"),
    "E149": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "小室"),
    "E211": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "新柴又"),
    "E212": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "北国分"),
    "E213": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "秋山"),
    "E214": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "松飛台"),
    "E215": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "大町"),
    "E216": (3, "Keisei.NaritaSkyAccess", "成田スカイアクセス線", "白井"),
    "E221": (7, "Keisei.Matsudo", "松戸線", "上本郷"),
    "E222": (7, "Keisei.Matsudo", "松戸線", "松戸新田"),
    "E223": (7, "Keisei.Matsudo", "松戸線", "みのり台"),
    "E224": (7, "Keisei.Matsudo", "松戸線", "常盤平"),
    "E225": (7, "Keisei.Matsudo", "松戸線", "五香"),
    "E226": (7, "Keisei.Matsudo", "松戸線", "元山"),
    "E227": (7, "Keisei.Matsudo", "松戸線", "北初富"),
    "E228": (7, "Keisei.Matsudo", "松戸線", "新鎌ヶ谷"),
    "E229": (7, "Keisei.Matsudo", "松戸線", "初富"),
    "E230": (7, "Keisei.Matsudo", "松戸線", "二和向台"),
    "E231": (7, "Keisei.Matsudo", "松戸線", "三咲"),
    "E232": (7, "Keisei.Matsudo", "松戸線", "滝不動"),
    "E233": (7, "Keisei.Matsudo", "松戸線", "高根木戸"),
    "E234": (7, "Keisei.Matsudo", "松戸線", "北習志野"),
    "E235": (7, "Keisei.Matsudo", "松戸線", "習志野"),
    "E236": (7, "Keisei.Matsudo", "松戸線", "薬園台"),
    "E237": (7, "Keisei.Matsudo", "松戸線", "前原"),
}

# matsudo_train_info: sectionid -> TS block (config/matsudo_id.json). The
# per-block station comes from BLOCK_TABLE above.
MATSUDO_SECTION = {
    1: "E027", 2: "E131", 3: "E131", 4: "E237", 5: "E237", 6: "E236",
    7: "E236", 8: "E235", 9: "E235", 10: "E234", 11: "E234", 12: "E233",
    13: "E233", 14: "E132", 15: "E132", 16: "E232", 17: "E232", 18: "E231",
    19: "E231", 20: "E230", 21: "E230", 22: "E133", 23: "E133", 24: "E229",
    25: "E229", 26: "E228", 27: "E228", 28: "E227", 29: "E227", 30: "E134",
    31: "E134", 32: "E226", 33: "E226", 34: "E225", 35: "E225", 36: "E224",
    37: "E224", 38: "E135", 39: "E135", 40: "E223", 41: "E223", 42: "E222",
    43: "E222", 44: "E221", 45: "E221", 46: "E136", 47: "E136",
    101: "E136", 102: "E221", 103: "E221", 104: "E222", 105: "E222",
    106: "E223", 107: "E223", 108: "E135", 109: "E135", 110: "E224",
    111: "E224", 112: "E225", 113: "E225", 114: "E226", 115: "E226",
    116: "E134", 117: "E134", 118: "E227", 119: "E227", 120: "E228",
    121: "E228", 122: "E229", 123: "E229", 124: "E133", 125: "E133",
    126: "E230", 127: "E230", 128: "E231", 129: "E231", 130: "E232",
    131: "E232", 132: "E132", 133: "E132", 134: "E233", 135: "E233",
    136: "E234", 137: "E234", 138: "E235", 139: "E235", 140: "E236",
    141: "E236", 142: "E237", 143: "E237", 144: "E131", 145: "E131",
    146: "E027", 147: "E027",
}

# ik destination codes (config/ikisaki.json)
IKISAKI = {
    1: "京成上野", 3: "日暮里", 4: "新三河島", 5: "町屋", 6: "千住大橋",
    7: "京成関屋", 8: "堀切菖蒲園", 9: "お花茶屋", 10: "青砥", 11: "京成高砂",
    12: "京成小岩", 13: "江戸川", 14: "国府台", 15: "市川真間", 16: "菅野",
    17: "京成八幡", 18: "鬼越", 19: "京成中山", 20: "東中山", 21: "京成西船",
    22: "海神", 23: "京成船橋", 24: "大神宮下", 25: "船橋競馬場", 26: "谷津",
    27: "京成津田沼", 28: "京成大久保", 29: "実籾", 30: "八千代台",
    31: "京成大和田", 32: "勝田台", 33: "志津", 34: "ユーカリが丘",
    35: "京成臼井", 36: "京成佐倉", 37: "大佐倉", 38: "京成酒々井",
    39: "宗吾参道", 40: "公津の杜", 41: "京成成田", 44: "空港第2ビル",
    45: "成田空港", 46: "東成田", 47: "押上", 48: "京成曳舟", 49: "八広",
    50: "四ツ木", 51: "京成立石", 52: "柴又", 53: "京成金町",
    54: "京成幕張本郷", 55: "京成幕張", 56: "検見川", 57: "京成稲毛",
    58: "みどり台", 59: "西登戸", 60: "新千葉", 61: "京成千葉", 62: "千葉中央",
    80: "大森台", 81: "学園前", 82: "おゆみ野", 83: "ちはら台", 84: "千葉寺",
    90: "西馬込", 91: "泉岳寺", 92: "浅草橋", 93: "蔵前", 94: "浅草",
    95: "本所吾妻橋", 100: "品川", 101: "京急川崎", 102: "神奈川新町",
    103: "金沢文庫", 104: "堀ノ内", 105: "浦賀", 106: "横浜",
    110: "京急久里浜", 111: "三浦海岸", 112: "三崎口", 120: "逗子・葉山",
    121: "羽田空港", 130: "芝山千代田",
    131: "新津田沼", 132: "高根公団", 133: "鎌ヶ谷大仏", 134: "くぬぎ山",
    135: "八柱", 136: "松戸", 140: "矢切", 141: "新鎌ヶ谷", 142: "西白井",
    143: "千葉ニュータウン中央", 144: "印西牧の原", 145: "印旛日本医大",
    146: "成田湯川", 148: "東松戸", 149: "小室",
    221: "上本郷", 222: "松戸新田", 223: "みのり台", 224: "常盤平",
    225: "五香", 226: "元山", 227: "北初富", 228: "新鎌ヶ谷", 229: "初富",
    230: "二和向台", 231: "三咲", 232: "滝不動", 233: "高根木戸",
    234: "北習志野", 235: "習志野", 236: "薬園台", 237: "前原",
}

# sy train-type codes (config/syasyu.json). 99 = 情報なし (deadhead-ish, filtered).
SYASYU = {
    0: "スカイライナー", 1: "モーニングライナー", 2: "イブニングライナー",
    3: "通勤特急", 4: "特急", 5: "急行", 6: "普通", 10: "快速", 11: "快速",
    13: "快速特急", 14: "快速特急", 15: "快速特急", 16: "シティライナー",
    17: "アクセス特急", 18: "アクセス特急", 99: "情報なし",
}

# Destination-code sets used to disambiguate junction blocks.
OSHIAGE_IK = {47, 90, 91, 92, 93, 94, 95, 100, 101, 102, 103, 104, 105, 106,
               110, 111, 112, 120, 121}
KANAMACHI_IK = {52, 53}
ACCESS_IK = {44, 45, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149}
CHIHARA_IK = {80, 81, 82, 83, 84}
CHIBA_IK = set(range(54, 63))
MATSUDO_IK = set(range(131, 137)) | set(range(221, 238))
ACCESS_SY = {0, 17, 18}


def _src_time(dt):
    """UP[0].dt[0] {yy,mt,dy,hh,mm,ss} -> JST iso."""
    try:
        from datetime import datetime

        d = datetime(int(dt["yy"]), int(dt["mt"]), int(dt["dy"]),
                     int(dt["hh"]), int(dt["mm"]), int(dt["ss"]),
                     tzinfo=common.JST)
        return d.isoformat()
    except Exception:
        return common.jst_now().isoformat()


def _resolve(block, ik, sy):
    """Return (railway, line_ja, station_ja) for one train, or None to skip.

    Junction blocks sit on more than one SVG/line; pick the line from the
    destination code / train type. Trains bound for 松戸線 are handed to the
    dedicated matsudo feed (return None) so they are not double counted.
    """
    base = BLOCK_TABLE.get(block)
    if not base:
        return None  # e.g. E094 - not on any line SVG (reserved/deadhead)
    code, railway, line_ja, station = base
    if code == 7:
        return None  # pure 松戸線 block -> matsudo feed owns it
    if ik in MATSUDO_IK:
        return None  # through 松戸線 train at a junction -> matsudo feed
    if block == "E010":  # 青砥: 押上線/浅草線直通 vs 本線
        if ik in OSHIAGE_IK:
            return "Keisei.Oshiage", "押上線", station
    elif block == "E011":  # 京成高砂: 本線/金町/アクセス
        if ik in KANAMACHI_IK:
            return "Keisei.Kanamachi", "金町線", station
        if ik in ACCESS_IK:
            return "Keisei.NaritaSkyAccess", "成田スカイアクセス線", station
    elif block == "E027":  # 京成津田沼: 本線/千葉/千原
        if ik in CHIHARA_IK:
            return "Keisei.Chihara", "千原線", station
        if ik in CHIBA_IK:
            return "Keisei.Chiba", "千葉線", station
    elif block == "E041":  # 京成成田: 本線 vs 東成田/芝山
        if ik in (46, 130):
            return "Keisei.HigashiNarita", "東成田線", station
    elif block in ("E044", "E045"):  # airport: 本線経由 vs スカイアクセス経由
        if sy in ACCESS_SY:
            return "Keisei.NaritaSkyAccess", "成田スカイアクセス線", station
    return railway, line_ja, station


def _fetch_traffic(traffic, fetched_at):
    body = []
    up = (traffic.get("UP") or [{}])[0]
    for blk in traffic.get("TS") or []:
        block = blk.get("id")
        for tr in blk.get("tr") or []:
            try:
                dl = int(tr.get("dl") or 0)
            except Exception:
                dl = 0
            if dl < DELAY_THRESHOLD_MIN:
                continue
            ik = int(tr.get("ik") or 0)
            sy = int(tr.get("sy") or 0)
            resolved = _resolve(block, ik, sy)
            if resolved is None:
                continue
            railway, line_ja, station = resolved
            number = str(tr.get("no") or "").strip()
            hk = str(tr.get("hk") or "")
            body.append({
                "owl:sameAs": f"Keisei:{block}.{number}",
                "odpt:railway": f"odpt.Railway:{railway}",
                "odpt:trainNumber": number or None,
                "odpt:delay": dl * 60,
                "odpt:trainType": SYASYU.get(sy),
                "_line_ja": line_ja,
                "_suki_line": line_ja,
                "_station": station,
                "_dest": IKISAKI.get(ik),
                "_direction": "上り" if hk == "0" else "下り",
                "_number_prefix": number or None,
            })
    return body


def _fetch_matsudo(matsudo):
    body = []
    if not isinstance(matsudo, list):
        return body
    for rec in matsudo:
        try:
            dl = int(rec.get("delayminute") or 0)
        except Exception:
            dl = 0
        if dl < DELAY_THRESHOLD_MIN:
            continue
        pk = rec.get("trainPositionInfoPK") or {}
        sectionid = pk.get("sectionid")
        block = MATSUDO_SECTION.get(int(sectionid)) if sectionid is not None else None
        station = BLOCK_TABLE.get(block, (0, "", "", ""))[3] if block else None
        number = str(rec.get("trainno") or "").strip()
        dest = rec.get("laststop") or None
        body.append({
            "owl:sameAs": f"Keisei-Matsudo:{sectionid}.{number}",
            "odpt:railway": "odpt.Railway:Keisei.Matsudo",
            "odpt:trainNumber": number or None,
            "odpt:delay": dl * 60,
            "odpt:trainType": "普通",
            "_line_ja": "松戸線",
            "_suki_line": "松戸線",
            "_station": station,
            "_dest": dest,
            "_direction": "松戸方面" if dest == "松戸" else "千葉方面",
            "_number_prefix": number or None,
        })
    return body


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            traffic = src.offline_json("keisei-traffic.json")
        except Exception:
            traffic = None
        try:
            matsudo = src.offline_json("keisei-matsudo.json")
        except Exception:
            matsudo = []
    else:
        ms = common.jst_now().strftime("%s") + "000"
        try:
            traffic = common.get_json(TRAFFIC_URL + ms)
        except Exception as exc:
            print(f"keisei traffic: {type(exc).__name__}: {str(exc)[:120]}")
            return None
        try:
            matsudo = common.get_json(MATSUDO_URL + ms)
        except Exception as exc:
            print(f"keisei matsudo: {type(exc).__name__}: {str(exc)[:120]}")
            matsudo = []
    if not isinstance(traffic, dict):
        return None
    dt = ((traffic.get("UP") or [{}])[0].get("dt") or [{}])[0]
    fetched_at = _src_time(dt)
    body = _fetch_traffic(traffic, fetched_at) + _fetch_matsudo(matsudo)
    return {
        "ok": True,
        "fetchedAt": fetched_at,
        "body": body,
        "source": "zaisen.tid-keisei.jp",
    }
