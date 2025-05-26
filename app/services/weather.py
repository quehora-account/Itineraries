import python_weather


class WeatherService:

    def __init__(self):
        self.client = python_weather.Client(unit=python_weather.IMPERIAL)

    async def get_weather(self) -> None:
        self.weather = await self.client.get("Paris")

        # Fetch the temperature for today.
        print(self.weather.temperature)

        # Fetch weather forecast for upcoming days.
        for daily in self.weather:
            print(daily)

            # Each daily forecast has their own hourly forecasts.
            for hourly in daily:
                print(f" --> {hourly!r}")

        return self.weather
