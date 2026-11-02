"""城市名→经纬度，Open-Meteo Geocoding 全局动态解析，支持全国地市"""
import requests
from functools import lru_cache


@lru_cache(maxsize=200)
def city_to_coords(city_name: str) -> tuple[float, float]:
    city_name = (city_name or "").strip().rstrip("市")
    if not city_name:
        return 22.82, 108.37
    try:
        url = "https://geocoding-api.open-meteo.com/v1/search"
        r = requests.get(
            url,
            params={"name": city_name, "count": 1, "language": "zh", "countryCode": "CN"},
            timeout=5,
        )
        j = r.json()
        if j.get("results"):
            res = j["results"][0]
            return res["latitude"], res["longitude"]
    except Exception:
        pass
    return 22.82, 108.37