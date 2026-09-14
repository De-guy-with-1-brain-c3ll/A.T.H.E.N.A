import unittest
from unittest.mock import AsyncMock
from athena.tools.weather import WeatherTool


FORECAST = {"timezone": "Asia/Shanghai", "current": {"time": "2026-08-31T12:00", "temperature_2m": 27},
            "daily": {"time": ["2026-08-31"], "temperature_2m_max": [30]},
            "hourly": {"time": ["2026-08-31T11:00", "2026-08-31T13:00"], "temperature_2m": [26, 28]}}


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinates_forecast_cache_and_hourly(self):
        http = AsyncMock()
        http.json.return_value = FORECAST
        tool = WeatherTool(http)
        args = {"location": "31.23,121.47", "days": 3}
        result = await tool.execute(args)
        self.assertTrue(result.success)
        self.assertIn("27", result.spoken_text)
        self.assertEqual(result.data["hourly"]["temperature_2m"], [28])
        cached = await tool.execute(args)
        self.assertTrue(cached.data["cached"])
        self.assertEqual(http.json.await_count, 1)

    async def test_ambiguous_name_requests_clarification(self):
        http = AsyncMock()
        http.json.return_value = {"results": [{"name": "Paris", "country": "France"}, {"name": "Paris", "country": "USA"}]}
        result = await WeatherTool(http).execute({"location": "Paris"})
        self.assertFalse(result.success)
        self.assertEqual(len(result.data["candidates"]), 2)

    async def test_city_lookup_then_forecast(self):
        http = AsyncMock()
        http.json.side_effect = [{"results": [{"name": "Shanghai", "country": "China", "latitude": 31.23, "longitude": 121.47}]}, FORECAST]
        result = await WeatherTool(http).execute({"location": "Shanghai, China", "units": "imperial"})
        self.assertTrue(result.success)
        self.assertIn("Shanghai", result.data["location"])
        self.assertIn("fahrenheit", http.json.call_args.args[0])

    async def test_primary_named_city_is_selected_by_qualifier_and_population(self):
        http = AsyncMock()
        http.json.side_effect = [{"results": [
            {"name": "Shenzhen", "admin1": "Guangdong", "country": "China",
             "latitude": 22.54, "longitude": 114.06, "population": 17_000_000},
            {"name": "Shenzhen", "admin1": "Guangdong", "country": "China",
             "latitude": 22.18, "longitude": 111.11, "population": 20_000},
            {"name": "Shenzhen", "admin1": "Zhejiang", "country": "China",
             "latitude": 29.41, "longitude": 121.33, "population": 1_000},
        ]}, FORECAST]
        result = await WeatherTool(http).execute({"location": "Shenzhen, China", "days": 1})
        self.assertTrue(result.success)
        self.assertEqual(result.data['latitude'], 22.54)
        self.assertIn('Shenzhen, Guangdong, China', result.data['location'])

    async def test_invalid_coordinates_and_failure(self):
        tool = WeatherTool(AsyncMock(json=AsyncMock(side_effect=TimeoutError)))
        self.assertFalse((await tool.execute({"location": "100,200"})).success)
        self.assertFalse((await tool.execute({"location": "Shanghai"})).success)
