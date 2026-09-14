"""Live weather with Open-Meteo's non-commercial API and ten-minute cache."""
from datetime import datetime, timezone
import time
from urllib.parse import urlencode
import aiohttp
from athena.tools._http import PublicHTTP, PublicWebError
from athena.tools.models import ToolDefinition, ToolResult


class WeatherTool:
    definition = ToolDefinition(
        name="get_weather",
        description="Get live conditions, hourly forecasts and up to seven days of weather. Location is city with country, or 'latitude,longitude'. Ask which location if ambiguous; airport codes are not supported.",
        parameters={"type": "object", "properties": {
            "location": {"type": "string", "minLength": 2, "maxLength": 150},
            "days": {"type": "integer", "minimum": 1, "maximum": 7, "default": 3},
            "units": {"type": "string", "enum": ["metric", "imperial"], "default": "metric"},
        }, "required": ["location"], "additionalProperties": False}, timeout_seconds=35,
    )

    def __init__(self, http=None):
        self.http = http or PublicHTTP()
        self.cache = {}

    async def execute(self, arguments: dict) -> ToolResult:
        location = arguments["location"].strip()
        days, units = arguments.get("days", 3), arguments.get("units", "metric")
        key = (location.casefold(), days, units)
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            result = cached[1]
            return ToolResult(True, result.spoken_text, {**result.data, "cached": True})
        try:
            coords = None
            pieces = location.split(",")
            if len(pieces) == 2:
                try:
                    coords = [float(p.strip()) for p in pieces]
                except ValueError:
                    pass
            if coords is not None:
                lat, lon = coords
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    return ToolResult(False, "The latitude or longitude is invalid.")
                label = location
            else:
                geo = await self.http.json("https://geocoding-api.open-meteo.com/v1/search?" + urlencode({
                    "name": location, "count": 5, "language": "en", "format": "json"}))
                matches = geo.get("results", [])
                if not matches:
                    return ToolResult(False, "Location not found. Try a city and country, or coordinates.")
                if len(matches) > 1:
                    query_parts = [part.strip().casefold() for part in location.split(",") if part.strip()]
                    exact = [item for item in matches
                             if str(item.get("name", "")).casefold() == query_parts[0]
                             and all(any(part == str(item.get(field, "")).casefold()
                                             or part in str(item.get(field, "")).casefold()
                                             for field in ("country", "admin1", "admin2"))
                                     for part in query_parts[1:])]
                    ranked = sorted(exact, key=lambda item: item.get("population") or 0, reverse=True)
                    if ranked and ((ranked[0].get("population") or 0) >= 100_000
                                   and (len(ranked) == 1 or (ranked[0].get("population") or 0)
                                        >= 5 * (ranked[1].get("population") or 0))):
                        chosen = ranked[0]
                    else:
                        return ToolResult(False, "Which location do you mean?", {"candidates": [
                            {k: item.get(k) for k in ("name", "admin1", "country", "latitude", "longitude", "population")}
                            for item in matches]})
                else:
                    chosen = matches[0]
                lat, lon = chosen["latitude"], chosen["longitude"]
                label = ", ".join(str(chosen[k]) for k in ("name", "admin1", "country") if chosen.get(k))
            params = {
                "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": days,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "hourly": "temperature_2m,precipitation_probability",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "temperature_unit": "fahrenheit" if units == "imperial" else "celsius",
                "wind_speed_unit": "mph" if units == "imperial" else "kmh"}
            url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
            forecast = await self.http.json(url)
            current = forecast.get("current", {})
            if "temperature_2m" not in current:
                raise PublicWebError("No current temperature returned.")
            hourly = forecast.get("hourly", {})
            indexes = [i for i, stamp in enumerate(hourly.get("time", [])) if stamp >= current.get("time", "")][:24]
            result = ToolResult(True,
                f"It is {current['temperature_2m']} degrees {'Fahrenheit' if units == 'imperial' else 'Celsius'} in {label}.",
                {"location": label, "latitude": lat, "longitude": lon, "timezone": forecast.get("timezone"),
                 "current": current, "current_units": forecast.get("current_units"),
                 "daily": forecast.get("daily"), "daily_units": forecast.get("daily_units"),
                 "hourly": {k: [values[i] for i in indexes] for k, values in hourly.items()},
                 "hourly_units": forecast.get("hourly_units"), "source": url,
                 "attribution": "Weather data by Open-Meteo (CC BY 4.0)",
                 "retrieved_at": datetime.now(timezone.utc).isoformat(), "cached": False})
            if len(self.cache) >= 32:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic(), result)
            return result
        except (aiohttp.ClientError, ValueError, TimeoutError, KeyError, TypeError, IndexError):
            return ToolResult(False, "Weather is unavailable; I do not have a verified forecast.")


def create_tools():
    return [WeatherTool()]
