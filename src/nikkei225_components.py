# Nikkei 225 Component Stocks
# Last updated: 2025-10-01
# Source: https://indexes.nikkei.co.jp/en/nkave/index/component
#
# Note: Nikkei 225 composition is reviewed in April and October each year.
# Update this list accordingly.

from __future__ import annotations

NIKKEI_225_TICKERS: list[dict[str, str]] = [
    # --- 水産 ---
    {"ticker": "1332.T", "name": "日本水産", "sector": "水産"},
    {"ticker": "1333.T", "name": "マルハニチロ", "sector": "水産"},
    # --- 鉱業 ---
    {"ticker": "1605.T", "name": "INPEX", "sector": "鉱業"},
    # --- 建設 ---
    {"ticker": "1721.T", "name": "コムシスホールディングス", "sector": "建設"},
    {"ticker": "1801.T", "name": "大成建設", "sector": "建設"},
    {"ticker": "1802.T", "name": "大林組", "sector": "建設"},
    {"ticker": "1803.T", "name": "清水建設", "sector": "建設"},
    {"ticker": "1808.T", "name": "長谷工コーポレーション", "sector": "建設"},
    {"ticker": "1812.T", "name": "鹿島建設", "sector": "建設"},
    # --- 食料品 ---
    {"ticker": "2002.T", "name": "日清製粉グループ本社", "sector": "食料品"},
    {"ticker": "2269.T", "name": "明治ホールディングス", "sector": "食料品"},
    {"ticker": "2282.T", "name": "日本ハム", "sector": "食料品"},
    {"ticker": "2501.T", "name": "サッポロホールディングス", "sector": "食料品"},
    {"ticker": "2502.T", "name": "アサヒグループホールディングス", "sector": "食料品"},
    {"ticker": "2503.T", "name": "キリンホールディングス", "sector": "食料品"},
    {"ticker": "2531.T", "name": "宝ホールディングス", "sector": "食料品"},
    {"ticker": "2801.T", "name": "キッコーマン", "sector": "食料品"},
    {"ticker": "2802.T", "name": "味の素", "sector": "食料品"},
    {"ticker": "2871.T", "name": "ニチレイ", "sector": "食料品"},
    {"ticker": "2914.T", "name": "日本たばこ産業", "sector": "食料品"},
    # --- 繊維 ---
    {"ticker": "3101.T", "name": "東洋紡", "sector": "繊維"},
    {"ticker": "3103.T", "name": "ユニチカ", "sector": "繊維"},
    {"ticker": "3401.T", "name": "帝人", "sector": "繊維"},
    {"ticker": "3402.T", "name": "東レ", "sector": "繊維"},
    # --- パルプ・紙 ---
    {"ticker": "3861.T", "name": "王子ホールディングス", "sector": "パルプ・紙"},
    {"ticker": "3863.T", "name": "日本製紙", "sector": "パルプ・紙"},
    # --- 化学 ---
    {"ticker": "3405.T", "name": "クラレ", "sector": "化学"},
    {"ticker": "3407.T", "name": "旭化成", "sector": "化学"},
    {"ticker": "4004.T", "name": "レゾナック・ホールディングス", "sector": "化学"},
    {"ticker": "4005.T", "name": "住友化学", "sector": "化学"},
    {"ticker": "4021.T", "name": "日産化学", "sector": "化学"},
    {"ticker": "4042.T", "name": "東ソー", "sector": "化学"},
    {"ticker": "4043.T", "name": "トクヤマ", "sector": "化学"},
    {"ticker": "4061.T", "name": "デンカ", "sector": "化学"},
    {"ticker": "4063.T", "name": "信越化学工業", "sector": "化学"},
    {"ticker": "4183.T", "name": "三井化学", "sector": "化学"},
    {"ticker": "4188.T", "name": "三菱ケミカルグループ", "sector": "化学"},
    {"ticker": "4208.T", "name": "UBE", "sector": "化学"},
    {"ticker": "4452.T", "name": "花王", "sector": "化学"},
    {"ticker": "4631.T", "name": "DIC", "sector": "化学"},
    {"ticker": "4901.T", "name": "富士フイルムホールディングス", "sector": "化学"},
    {"ticker": "4911.T", "name": "資生堂", "sector": "化学"},
    {"ticker": "6988.T", "name": "日東電工", "sector": "化学"},
    # --- 医薬品 ---
    {"ticker": "4151.T", "name": "協和キリン", "sector": "医薬品"},
    {"ticker": "4502.T", "name": "武田薬品工業", "sector": "医薬品"},
    {"ticker": "4503.T", "name": "アステラス製薬", "sector": "医薬品"},
    {"ticker": "4506.T", "name": "住友ファーマ", "sector": "医薬品"},
    {"ticker": "4507.T", "name": "塩野義製薬", "sector": "医薬品"},
    {"ticker": "4519.T", "name": "中外製薬", "sector": "医薬品"},
    {"ticker": "4523.T", "name": "エーザイ", "sector": "医薬品"},
    {"ticker": "4568.T", "name": "第一三共", "sector": "医薬品"},
    {"ticker": "4578.T", "name": "大塚ホールディングス", "sector": "医薬品"},
    # --- 石油 ---
    {"ticker": "5019.T", "name": "出光興産", "sector": "石油"},
    {"ticker": "5020.T", "name": "ENEOSホールディングス", "sector": "石油"},
    # --- ゴム ---
    {"ticker": "5101.T", "name": "横浜ゴム", "sector": "ゴム"},
    {"ticker": "5108.T", "name": "ブリヂストン", "sector": "ゴム"},
    # --- ガラス・土石 ---
    {"ticker": "5201.T", "name": "AGC", "sector": "ガラス・土石"},
    {"ticker": "5214.T", "name": "日本電気硝子", "sector": "ガラス・土石"},
    {"ticker": "5233.T", "name": "太平洋セメント", "sector": "ガラス・土石"},
    {"ticker": "5332.T", "name": "TOTO", "sector": "ガラス・土石"},
    {"ticker": "5333.T", "name": "日本ガイシ", "sector": "ガラス・土石"},
    {"ticker": "5401.T", "name": "日本製鉄", "sector": "鉄鋼"},
    # --- 鉄鋼 ---
    {"ticker": "5406.T", "name": "神戸製鋼所", "sector": "鉄鋼"},
    {"ticker": "5411.T", "name": "JFEホールディングス", "sector": "鉄鋼"},
    # --- 非鉄・金属 ---
    {"ticker": "3436.T", "name": "SUMCO", "sector": "非鉄・金属"},
    {"ticker": "5541.T", "name": "大平洋金属", "sector": "非鉄・金属"},
    {"ticker": "5703.T", "name": "日本軽金属ホールディングス", "sector": "非鉄・金属"},
    {"ticker": "5706.T", "name": "三井金属鉱業", "sector": "非鉄・金属"},
    {"ticker": "5707.T", "name": "東邦亜鉛", "sector": "非鉄・金属"},
    {"ticker": "5711.T", "name": "三菱マテリアル", "sector": "非鉄・金属"},
    {"ticker": "5713.T", "name": "住友金属鉱山", "sector": "非鉄・金属"},
    {"ticker": "5714.T", "name": "DOWAホールディングス", "sector": "非鉄・金属"},
    {"ticker": "5801.T", "name": "古河電気工業", "sector": "非鉄・金属"},
    {"ticker": "5802.T", "name": "住友電気工業", "sector": "非鉄・金属"},
    {"ticker": "5803.T", "name": "フジクラ", "sector": "非鉄・金属"},
    # --- 機械 ---
    {"ticker": "5631.T", "name": "日本製鋼所", "sector": "機械"},
    {"ticker": "6103.T", "name": "オークマ", "sector": "機械"},
    {"ticker": "6113.T", "name": "アマダ", "sector": "機械"},
    {"ticker": "6301.T", "name": "小松製作所", "sector": "機械"},
    {"ticker": "6302.T", "name": "住友重機械工業", "sector": "機械"},
    {"ticker": "6305.T", "name": "日立建機", "sector": "機械"},
    {"ticker": "6326.T", "name": "クボタ", "sector": "機械"},
    {"ticker": "6361.T", "name": "荏原製作所", "sector": "機械"},
    {"ticker": "6367.T", "name": "ダイキン工業", "sector": "機械"},
    {"ticker": "6471.T", "name": "日本精工", "sector": "機械"},
    {"ticker": "6472.T", "name": "NTN", "sector": "機械"},
    {"ticker": "6473.T", "name": "ジェイテクト", "sector": "機械"},
    {"ticker": "7004.T", "name": "日立造船", "sector": "機械"},
    {"ticker": "7011.T", "name": "三菱重工業", "sector": "機械"},
    {"ticker": "7013.T", "name": "IHI", "sector": "機械"},
    # --- 電気機器 ---
    {"ticker": "6479.T", "name": "ミネベアミツミ", "sector": "電気機器"},
    {"ticker": "6501.T", "name": "日立製作所", "sector": "電気機器"},
    {"ticker": "6503.T", "name": "三菱電機", "sector": "電気機器"},
    {"ticker": "6504.T", "name": "富士電機", "sector": "電気機器"},
    {"ticker": "6506.T", "name": "安川電機", "sector": "電気機器"},
    {"ticker": "6526.T", "name": "ソシオネクスト", "sector": "電気機器"},
    {"ticker": "6594.T", "name": "日本電産", "sector": "電気機器"},
    {"ticker": "6645.T", "name": "オムロン", "sector": "電気機器"},
    {"ticker": "6674.T", "name": "GSユアサコーポレーション", "sector": "電気機器"},
    {"ticker": "6701.T", "name": "NEC", "sector": "電気機器"},
    {"ticker": "6702.T", "name": "富士通", "sector": "電気機器"},
    {"ticker": "6723.T", "name": "ルネサスエレクトロニクス", "sector": "電気機器"},
    {"ticker": "6724.T", "name": "セイコーエプソン", "sector": "電気機器"},
    {"ticker": "6752.T", "name": "パナソニックホールディングス", "sector": "電気機器"},
    {"ticker": "6753.T", "name": "シャープ", "sector": "電気機器"},
    {"ticker": "6758.T", "name": "ソニーグループ", "sector": "電気機器"},
    {"ticker": "6762.T", "name": "TDK", "sector": "電気機器"},
    {"ticker": "6770.T", "name": "アルプスアルパイン", "sector": "電気機器"},
    {"ticker": "6841.T", "name": "横河電機", "sector": "電気機器"},
    {"ticker": "6857.T", "name": "アドバンテスト", "sector": "電気機器"},
    {"ticker": "6861.T", "name": "キーエンス", "sector": "電気機器"},
    {"ticker": "6902.T", "name": "デンソー", "sector": "電気機器"},
    {"ticker": "6920.T", "name": "レーザーテック", "sector": "電気機器"},
    {"ticker": "6952.T", "name": "カシオ計算機", "sector": "電気機器"},
    {"ticker": "6954.T", "name": "ファナック", "sector": "電気機器"},
    {"ticker": "6971.T", "name": "京セラ", "sector": "電気機器"},
    {"ticker": "6976.T", "name": "太陽誘電", "sector": "電気機器"},
    {"ticker": "6981.T", "name": "村田製作所", "sector": "電気機器"},
    {"ticker": "7735.T", "name": "SCREENホールディングス", "sector": "電気機器"},
    {"ticker": "7751.T", "name": "キヤノン", "sector": "電気機器"},
    {"ticker": "7752.T", "name": "リコー", "sector": "電気機器"},
    {"ticker": "8035.T", "name": "東京エレクトロン", "sector": "電気機器"},
    # --- 造船 ---
    {"ticker": "7012.T", "name": "川崎重工業", "sector": "造船"},
    # --- 自動車 ---
    {"ticker": "7201.T", "name": "日産自動車", "sector": "自動車"},
    {"ticker": "7202.T", "name": "いすゞ自動車", "sector": "自動車"},
    {"ticker": "7203.T", "name": "トヨタ自動車", "sector": "自動車"},
    {"ticker": "7205.T", "name": "日野自動車", "sector": "自動車"},
    {"ticker": "7211.T", "name": "三菱自動車工業", "sector": "自動車"},
    {"ticker": "7261.T", "name": "マツダ", "sector": "自動車"},
    {"ticker": "7267.T", "name": "本田技研工業", "sector": "自動車"},
    {"ticker": "7269.T", "name": "スズキ", "sector": "自動車"},
    {"ticker": "7270.T", "name": "SUBARU", "sector": "自動車"},
    {"ticker": "7272.T", "name": "ヤマハ発動機", "sector": "自動車"},
    # --- 精密機器 ---
    {"ticker": "4543.T", "name": "テルモ", "sector": "精密機器"},
    {"ticker": "7731.T", "name": "ニコン", "sector": "精密機器"},
    {"ticker": "7733.T", "name": "オリンパス", "sector": "精密機器"},
    {"ticker": "7741.T", "name": "HOYA", "sector": "精密機器"},
    {"ticker": "7762.T", "name": "シチズン時計", "sector": "精密機器"},
    # --- その他製造 ---
    {"ticker": "7832.T", "name": "バンダイナムコホールディングス", "sector": "その他製造"},
    {"ticker": "7911.T", "name": "凸版印刷", "sector": "その他製造"},
    {"ticker": "7912.T", "name": "大日本印刷", "sector": "その他製造"},
    {"ticker": "7951.T", "name": "ヤマハ", "sector": "その他製造"},
    # --- 商社 ---
    {"ticker": "2768.T", "name": "双日", "sector": "商社"},
    {"ticker": "8001.T", "name": "伊藤忠商事", "sector": "商社"},
    {"ticker": "8002.T", "name": "丸紅", "sector": "商社"},
    {"ticker": "8015.T", "name": "豊田通商", "sector": "商社"},
    {"ticker": "8031.T", "name": "三井物産", "sector": "商社"},
    {"ticker": "8053.T", "name": "住友商事", "sector": "商社"},
    {"ticker": "8058.T", "name": "三菱商事", "sector": "商社"},
    # --- 小売 ---
    {"ticker": "3086.T", "name": "J.フロントリテイリング", "sector": "小売"},
    {"ticker": "3092.T", "name": "ZOZO", "sector": "小売"},
    {"ticker": "3099.T", "name": "三越伊勢丹ホールディングス", "sector": "小売"},
    {"ticker": "3382.T", "name": "セブン&アイ・ホールディングス", "sector": "小売"},
    {"ticker": "8233.T", "name": "高島屋", "sector": "小売"},
    {"ticker": "8252.T", "name": "丸井グループ", "sector": "小売"},
    {"ticker": "8267.T", "name": "イオン", "sector": "小売"},
    {"ticker": "9843.T", "name": "ニトリホールディングス", "sector": "小売"},
    {"ticker": "9983.T", "name": "ファーストリテイリング", "sector": "小売"},
    # --- 銀行 ---
    {"ticker": "7186.T", "name": "コンコルディア・フィナンシャルグループ", "sector": "銀行"},
    {"ticker": "8304.T", "name": "あおぞら銀行", "sector": "銀行"},
    {"ticker": "8306.T", "name": "三菱UFJフィナンシャル・グループ", "sector": "銀行"},
    {"ticker": "8308.T", "name": "りそなホールディングス", "sector": "銀行"},
    {"ticker": "8309.T", "name": "三井住友トラスト・ホールディングス", "sector": "銀行"},
    {"ticker": "8316.T", "name": "三井住友フィナンシャルグループ", "sector": "銀行"},
    {"ticker": "8331.T", "name": "千葉銀行", "sector": "銀行"},
    {"ticker": "8354.T", "name": "ふくおかフィナンシャルグループ", "sector": "銀行"},
    {"ticker": "8411.T", "name": "みずほフィナンシャルグループ", "sector": "銀行"},
    # --- 証券 ---
    {"ticker": "8601.T", "name": "大和証券グループ本社", "sector": "証券"},
    {"ticker": "8604.T", "name": "野村ホールディングス", "sector": "証券"},
    # --- 保険 ---
    {"ticker": "8630.T", "name": "SOMPOホールディングス", "sector": "保険"},
    {"ticker": "8725.T", "name": "MS&ADインシュアランスグループホールディングス", "sector": "保険"},
    {"ticker": "8766.T", "name": "東京海上ホールディングス", "sector": "保険"},
    {"ticker": "8795.T", "name": "T&Dホールディングス", "sector": "保険"},
    # --- その他金融 ---
    {"ticker": "8253.T", "name": "クレディセゾン", "sector": "その他金融"},
    {"ticker": "8591.T", "name": "オリックス", "sector": "その他金融"},
    {"ticker": "8697.T", "name": "日本取引所グループ", "sector": "その他金融"},
    # --- 不動産 ---
    {"ticker": "3289.T", "name": "東急不動産ホールディングス", "sector": "不動産"},
    {"ticker": "8801.T", "name": "三井不動産", "sector": "不動産"},
    {"ticker": "8802.T", "name": "三菱地所", "sector": "不動産"},
    {"ticker": "8804.T", "name": "東京建物", "sector": "不動産"},
    {"ticker": "8830.T", "name": "住友不動産", "sector": "不動産"},
    # --- 鉄道・バス ---
    {"ticker": "9001.T", "name": "東武鉄道", "sector": "鉄道・バス"},
    {"ticker": "9005.T", "name": "東急", "sector": "鉄道・バス"},
    {"ticker": "9007.T", "name": "小田急電鉄", "sector": "鉄道・バス"},
    {"ticker": "9008.T", "name": "京王電鉄", "sector": "鉄道・バス"},
    {"ticker": "9009.T", "name": "京成電鉄", "sector": "鉄道・バス"},
    {"ticker": "9020.T", "name": "東日本旅客鉄道", "sector": "鉄道・バス"},
    {"ticker": "9021.T", "name": "西日本旅客鉄道", "sector": "鉄道・バス"},
    {"ticker": "9022.T", "name": "東海旅客鉄道", "sector": "鉄道・バス"},
    # --- 陸運 ---
    {"ticker": "9064.T", "name": "ヤマトホールディングス", "sector": "陸運"},
    {"ticker": "9147.T", "name": "NIPPON EXPRESSホールディングス", "sector": "陸運"},
    # --- 海運 ---
    {"ticker": "9101.T", "name": "日本郵船", "sector": "海運"},
    {"ticker": "9104.T", "name": "商船三井", "sector": "海運"},
    {"ticker": "9107.T", "name": "川崎汽船", "sector": "海運"},
    # --- 空運 ---
    {"ticker": "9201.T", "name": "日本航空", "sector": "空運"},
    {"ticker": "9202.T", "name": "ANAホールディングス", "sector": "空運"},
    # --- 倉庫 ---
    {"ticker": "9301.T", "name": "三菱倉庫", "sector": "倉庫"},
    # --- 情報・通信 ---
    {"ticker": "2413.T", "name": "エムスリー", "sector": "情報・通信"},
    {"ticker": "3659.T", "name": "ネクソン", "sector": "情報・通信"},
    {"ticker": "4307.T", "name": "野村総合研究所", "sector": "情報・通信"},
    {"ticker": "4324.T", "name": "電通グループ", "sector": "情報・通信"},
    {"ticker": "4385.T", "name": "メルカリ", "sector": "情報・通信"},
    {"ticker": "4661.T", "name": "オリエンタルランド", "sector": "情報・通信"},
    {"ticker": "4689.T", "name": "Zホールディングス", "sector": "情報・通信"},
    {"ticker": "4704.T", "name": "トレンドマイクロ", "sector": "情報・通信"},
    {"ticker": "4751.T", "name": "サイバーエージェント", "sector": "情報・通信"},
    {"ticker": "4755.T", "name": "楽天グループ", "sector": "情報・通信"},
    {"ticker": "9432.T", "name": "日本電信電話", "sector": "情報・通信"},
    {"ticker": "9433.T", "name": "KDDI", "sector": "情報・通信"},
    {"ticker": "9434.T", "name": "ソフトバンク", "sector": "情報・通信"},
    {"ticker": "9613.T", "name": "NTTデータグループ", "sector": "情報・通信"},
    {"ticker": "9984.T", "name": "ソフトバンクグループ", "sector": "情報・通信"},
    # --- 電力 ---
    {"ticker": "9501.T", "name": "東京電力ホールディングス", "sector": "電力"},
    {"ticker": "9502.T", "name": "中部電力", "sector": "電力"},
    {"ticker": "9503.T", "name": "関西電力", "sector": "電力"},
    # --- ガス ---
    {"ticker": "9531.T", "name": "東京ガス", "sector": "ガス"},
    {"ticker": "9532.T", "name": "大阪ガス", "sector": "ガス"},
    # --- サービス ---
    {"ticker": "2432.T", "name": "ディー・エヌ・エー", "sector": "サービス"},
    {"ticker": "4543.T", "name": "テルモ", "sector": "サービス"},
    {"ticker": "6098.T", "name": "リクルートホールディングス", "sector": "サービス"},
    {"ticker": "6178.T", "name": "日本郵政", "sector": "サービス"},
    {"ticker": "9602.T", "name": "東宝", "sector": "サービス"},
    {"ticker": "9735.T", "name": "セコム", "sector": "サービス"},
    {"ticker": "9766.T", "name": "コナミグループ", "sector": "サービス"},
]


def get_tickers() -> list[str]:
    """Return list of all Nikkei 225 ticker symbols (deduplicated)."""
    seen: set[str] = set()
    tickers: list[str] = []
    for entry in NIKKEI_225_TICKERS:
        t = entry["ticker"]
        if t not in seen:
            seen.add(t)
            tickers.append(t)
    return tickers


def get_by_sector(sector: str) -> list[dict[str, str]]:
    """Return tickers filtered by sector."""
    return [t for t in NIKKEI_225_TICKERS if t["sector"] == sector]


def get_sectors() -> list[str]:
    """Return list of unique sectors."""
    seen: set[str] = set()
    sectors: list[str] = []
    for entry in NIKKEI_225_TICKERS:
        s = entry["sector"]
        if s not in seen:
            seen.add(s)
            sectors.append(s)
    return sectors
