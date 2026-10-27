"""Match dokorail timetable legs onto jr.suki.ing's tmaps train keys.

jr.suki.ing identifies trains by its own dataset keys ("2549") with
{lines: [...], s_jas: [station names in order]}. dokorail/ODPT numbers
("346M") are a different namespace, so the join is semantic:

1. line compatibility: dokorail line title (minus its parenthetical
   variants, minus a leading "JR") against the candidate's lines, using
   lines_map.json aliases both ways;
2. station-sequence: the dokorail leg's ordered stops must appear as an
   ordered subsequence of s_jas (allows the suki train to run further at
   either end - through legs like 346M.3 start mid-route);
3. score = subsequence match, then stop overlap, then length proximity.
   The opposite direction fails the ordered-subsequence test naturally.

Validated live: 346M (紀伊内原->和歌山) -> key 10780 (御坊->和歌山), and the
reverse-direction trains of the same pattern are excluded.
"""

import json
import re
from pathlib import Path

PAREN = re.compile(r"\s*[(（].*$")
JR_PREFIX = re.compile(r"^JR")

# Line-name bridge between dokorail titles and jr.suki.ing line names.
# suki naming conventions (from its tmaps data): JR lines carry a lowercase
# "jr" prefix for some lines (jr奈良線, jr東西線, jr山口線, jr日光線), Toei
# subway lines are 都営地下鉄X線, Yokohama Municipal uses 号線 numbering, and
# Tobu keeps pre-2021 / official names (東上本線, 伊勢崎線, 野田線, 東武日光線).
#
# TITLE_ALIASES: unambiguous dokorail title -> suki line names.
TITLE_ALIASES = {
    # Tobu renames
    "東武スカイツリーライン": {"伊勢崎線", "東武伊勢崎線"},
    "東武アーバンパークライン": {"野田線", "東武野田線"},
    "東上線": {"東上本線", "東武東上線"},
    # JR-West
    "JR奈良線": {"jr奈良線"},
    "JR東西線": {"jr東西線"},
    "JR山口線": {"jr山口線"},
    "山口線": {"jr山口線"},
    "JR阪和線東羽衣支線": {"阪和線"},
    # JR-Shikoku: 瀬戸大橋直通列车在 suki 侧归在予讃線
    "本四備讃線": {"予讃線"},
    # JR-East service names over physical lines
    "中央線快速": {"中央本線", "中央線"},
    "中央・総武各駅停車": {"中央本線", "中央線", "総武本線"},
    "常磐線各駅停車": {"常磐線"},
    "常磐線快速": {"常磐線"},
    "京浜東北線・根岸線": {"東海道本線", "根岸線", "東北本線"},
    "埼京線・川越線": {"川越線", "山手線"},
    "湘南新宿ライン": {"東海道本線", "東北本線", "横須賀線", "高崎線"},
    "総武快速線": {"総武本線", "横須賀線"},
    "成田線我孫子支線": {"成田線"},
    "成田線空港支線": {"成田線"},
    "大崎支線": {"山手線"},
    "山手貨物線": {"山手線"},
    "東海道貨物線": {"東海道本線"},
    "相鉄直通線": {"相鉄本線", "相鉄新横浜線"},
    "水郡線常陸太田支線": {"水郡線"},
    # Toei / Yokohama Municipal
    "東京さくらトラム（都電荒川線）": {"荒川線", "都電荒川線"},
    "ブルーライン": {"横浜市営地下鉄1号線", "横浜市営地下鉄3号線"},
    "グリーンライン": {"横浜市営地下鉄4号線"},
    # JR-Hokkaido: their position service calls 札沼線 学園都市線
    "学園都市線": {"札沼線", "学園都市線"},
    # JR-Kyushu: 福北ゆたか線 is the nickname of 筑豊本線(+篠栗線)
    "福北ゆたか線": {"筑豊本線", "篠栗線", "福北ゆたか線"},
    # Kintetsu: their position pages use short names; suki keeps the 近鉄 prefix
    "奈良線": {"近鉄奈良線", "近畿日本鉄道奈良線"},
    "大阪線": {"近鉄大阪線", "近畿日本鉄道大阪線"},
    "名古屋線": {"近鉄名古屋線", "近畿日本鉄道名古屋線"},
    "南大阪線": {"近鉄南大阪線", "近畿日本鉄道南大阪線"},
    "橿原線": {"近鉄橿原線", "近畿日本鉄道橿原線"},
    "京都線": {"近鉄京都線", "近畿日本鉄道京都線"},
    "生駒線": {"近鉄生駒線", "近畿日本鉄道生駒線"},
    "田原本線": {"近鉄田原本線", "近畿日本鉄道田原本線"},
    "天理線": {"近鉄天理線", "近畿日本鉄道天理線"},
    "道明寺線": {"近鉄道明寺線", "近畿日本鉄道道明寺線"},
    "長野線": {"近鉄長野線", "近畿日本鉄道長野線"},
    "御所線": {"近鉄御所線", "近畿日本鉄道御所線"},
    "山田線": {"近鉄山田線", "近畿日本鉄道山田線"},
    "鳥羽線": {"近鉄鳥羽線", "近畿日本鉄道鳥羽線"},
    "志摩線": {"近鉄志摩線", "近畿日本鉄道志摩線"},
    "鈴鹿線": {"近鉄鈴鹿線", "近畿日本鉄道鈴鹿線"},
    "湯の山線": {"近鉄湯の山線"},
    "けいはんな線": {"けいはんな線", "近畿日本鉄道けいはんな線"},
    # 瀬戸大橋線 vs 本四備讃線: the same line is named 瀬戸大橋線 by
    # JR-West (through services) and 本四備讃線 by JR-Shikoku - suki data
    # uses the JR-Shikoku name
    "瀬戸大橋線": {"本四備讃線", "瀬戸大橋線"},
    # 仙台空港アクセス線 (formal name used by doko-train feeds) vs the
    # short name suki data tags it with
    "仙台空港アクセス線": {"仙台空港線", "仙台空港アクセス線"},
}

# RAILWAY_ALIASES: titles that collide across operators resolve by railway id.
RAILWAY_ALIASES = {
    "JR-East.Nikko": {"jr日光線"},
    "Tobu.Nikko": {"東武日光線"},
    "JR-East.Utsunomiya": {"東北本線"},
    "Tobu.Utsunomiya": {"東武宇都宮線"},
    "Keikyu.Daishi": {"京急大師線"},
    "Tobu.Daishi": {"東武大師線"},
    "Keikyu.Airport": {"京急空港線"},
    "Toei.Mita": {"都営地下鉄三田線"},
    "Toei.Shinjuku": {"都営地下鉄新宿線"},
    "JR-West.Nara": {"jr奈良線"},
    "Tobu.Kinugawa": {"東武鬼怒川線"},
}


def norm_station(name):
    name = PAREN.sub("", name or "").strip()
    # unify the small-ke morpheme connector: 霞ヶ関 vs 霞ケ関 across datasets
    return name.replace("ヶ", "ケ").replace("ｹ", "ケ")


def line_names_for(title):
    """dokorail line title -> set of acceptable jr.suki.ing line names."""
    base = PAREN.sub("", title or "").strip()
    base = JR_PREFIX.sub("", base).strip()
    return {base, title} if base != title else {base}


class SukiIndex:
    def __init__(self, tmaps, lines_map=None):
        self.aliases = {}
        if lines_map:
            for official, variants in lines_map.items():
                group = {official, *variants}
                for name in group:
                    self.aliases.setdefault(name, set()).update(group)
        self.trains = tmaps  # key -> {lines, s_jas}
        self._stops_norm = {
            k: [norm_station(s) for s in e["s_jas"]] for k, e in tmaps.items()
        }
        self._line_pool = {}
        for k, e in tmaps.items():
            for line in e["lines"]:
                self._line_pool.setdefault(line, []).append(k)

    @classmethod
    def load(cls, data_dir):
        root = Path(data_dir) / "suki"
        return cls(
            json.loads((root / "tmaps.json").read_text()),
            json.loads((root / "lines_map.json").read_text())
            if (root / "lines_map.json").exists()
            else None,
        )

    def _expand(self, names, railway=None):
        out = set()
        if railway:
            out |= RAILWAY_ALIASES.get(railway, set())
        for n in names:
            out |= self.aliases.get(n, set()) | {n}
            for base in line_names_for(n):
                out |= self.aliases.get(base, set()) | {base}
            out |= TITLE_ALIASES.get(n, set())
        return out

    @staticmethod
    def _has_pref_suffixed(hay, target):
        """True when a stop matches target up to a prefecture suffix: suki
        names come with county qualifiers (高松(香川県)) while live feeds
        report the bare station (高松). Only evaluated inside the line-filtered
        candidate pool, so 高松(石川県)/高松(東京都) can never be hit by an
        ula/Shikoku/other-line target."""
        if not target:
            return False
        for s in hay:
            if s.startswith(target):
                rest = s[len(target):]
                if rest and rest.endswith(("都", "道", "府", "県")):
                    return True
        return False

    def _is_subsequence(self, needle, hay):
        it = iter(hay)
        return all(any(s == h for h in it) for s in needle)

    @staticmethod
    def _lcs_len(a, b):
        """Longest common subsequence length (needle vs hay, both small)."""
        if not a:
            return 0
        prev = [0] * (len(b) + 1)
        for x in a:
            cur = [0]
            for j, y in enumerate(b):
                cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
            prev = cur
        return prev[-1]

    # north-end terminals per shinkansen line: an entry whose first stop is
    # the north terminal runs 下り (odd numbers), matching JR numbering parity
    NORTH_TERMINALS = {
        "東海道新幹線": {"東京"},
        "山陽新幹線": {"新大阪"},
        "九州新幹線": {"博多"},
    }

    # stopping-pattern size buckets by service type (approximate but
    # separates こだま from のぞみ cleanly; 車種 key-station 校验在
    # resolve_direct_by_time 里做, bucket 只做第一道粗筛)
    TYPE_STOP_BUCKETS = {
        # 上限必须覆盖全程车终点多出的站:のぞみ 東京⇄博多=12 站(含東京);
        # さくら/みずほ 新大阪→鹿児島中央=17 站(2456 实测);2026-10 实测
        # 8068=11 站(博多終点)仍在界内
        "のぞみ": (0, 12),
        "ひかり": (10, 13),
        "こだま": (12, 99),
        "みずほ": (0, 10),
        "さくら": (0, 20),
        # 在来線特急 (JR-East cloudrail nickname 实测站数分布 2026-10-27)
        "あずさ": (6, 14),   # 新宿→松本 大站停
        "かいじ": (5, 11),  # 新宿→甲府
        "踊り子": (5, 16),  # 東京→伊東/伊豆急下田
        "ひたち": (10, 17),  # 品川→仙台/いわき 大站停
        "ときわ": (14, 25),  # 品川→土浦/勝田 各停 pattern
        "あかぎ": (4, 10),  # 上野→前橋/高崎
        "草津・四万": (6, 14),  # 上野→長野原草津口
    }

    def match_direct(self, line_title, station, direction=None, train_type=None, top=3):
        """Direct suki matching for feeds whose lines have no dokorail
        timetable (shinkansen bundles are empty shells). Filters: line name,
        station membership, direction (feed "down"/"up" vs the entry's
        terminal order) and a stop-count bucket from the service type.
        Returns [{key, direct: True, overlap, size}] - callers should only
        trust a single-candidate result."""
        if not line_title or not station:
            return []
        want = self._expand(line_names_for(line_title))
        target = norm_station(station)
        # 跨线全程车(東京→博多 のぞみ 等)始发站可能是任一北端(東京/新大阪/博多),
        # 方向判断用全线北端并集,不能用本线路北端(否则漏掉跨线下行车)
        north = set().union(*self.NORTH_TERMINALS.values())
        lo, hi = self.TYPE_STOP_BUCKETS.get(train_type or "", (0, 99))
        scored = []
        for k in {kk for line in want for kk in self._line_pool.get(line, [])}:
            hay = self._stops_norm[k]
            if target not in hay and not self._has_pref_suffixed(hay, target):
                continue
            if direction == "down" and hay[0] not in north:
                continue
            if direction == "up" and hay[-1] not in north:
                continue
            if not (lo <= len(hay) <= hi):
                continue
            scored.append(
                {"key": k, "direct": True, "overlap": 1.0, "size": len(hay)}
            )
        scored.sort(key=lambda c: c["size"])
        return scored[:top]

    def match(self, stops, line_title=None, railway=None, top=3):
        """stops: ordered station names (ja). Returns ranked
        [{key, subseq, fuzzy, overlap, extra}] best first. line_title=None
        (e.g. JR-Hokkaido's all-line express group) searches every train,
        gated by containment quality. `fuzzy` = number of dokorail stops
        that break the ordered containment (stopping-pattern drift between
        the two datasets, usually 1-2 stations); 0 == exact subsequence."""
        if line_title:
            want_lines = self._expand(line_names_for(line_title), railway)
            keys = {k for line in want_lines for k in self._line_pool.get(line, [])}
        else:
            keys = set(self.trains)
        needle = [norm_station(s) for s in stops]
        needle_set = set(needle)
        scored = []
        for k in keys:
            hay = self._stops_norm[k]
            hay_set = set(hay)
            overlap = len(needle_set & hay_set) / max(1, len(needle_set))
            if overlap < 0.6:
                continue
            subseq = self._is_subsequence(needle, hay)
            fuzzy = 0 if subseq else len(needle) - self._lcs_len(needle, hay)
            extra = len(hay) - len(needle)
            scored.append(
                {
                    "key": k,
                    "subseq": subseq,
                    "fuzzy": fuzzy,
                    "overlap": round(overlap, 3),
                    "extra": extra,
                    "_rank": (
                        fuzzy,
                        -overlap,
                        abs(extra),
                    ),
                }
            )
        scored.sort(key=lambda c: c.pop("_rank"))
        return scored[:top]
