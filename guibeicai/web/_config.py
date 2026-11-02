# ========== 偏差补偿 ==========
PREDICTION_DEVIATION_THRESHOLD = 0.25

# ========== LLM multiplier 软边界 ==========
LLM_MULTIPLIER_MIN = 0.85
LLM_MULTIPLIER_MAX = 1.15

# ========== 天气规则修正 ==========
RULE_WEATHER_HOT_DEG = 30.0
RULE_WEATHER_HOT_PENALTY = -0.05
RULE_WEATHER_HOT_KEYWORDS = [
    "水煮", "鱼香", "麻辣", "香辣", "酸辣", "干锅", "火锅", "冒菜",
    "烧烤", "炸", "煲", "炖", "红烧", "糖醋", "宫保", "麻婆",
    "回锅", "辣子", "口水", "夫妻肺片", "毛血旺", "酸菜鱼",
]