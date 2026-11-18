# 01 Data sources

[← README](../README.md) · **Data sources** · [ETL pipeline →](02-etl-pipeline.md)

The platform combines three public APIs with different grains: electricity per bidding zone and
quarter-hour, weather per city and hour, and economic figures per country and year. All three are free;
only ENTSO-E needs a (free) personal token.

| Source | What is extracted | Grain | Access |
|---|---|---|---|
| [ENTSO-E Transparency Platform](https://transparency.entsoe.eu) | actual total load, day-ahead load forecast, actual generation per production type | bidding zone, 15–60 min | REST API (XML), token |
| [Open-Meteo](https://open-meteo.com) historical weather | temperature, humidity, precipitation, wind speed and direction, pressure, cloud cover, solar radiation, WMO weather code | city, hour | REST API (JSON), rate limits |
| [Eurostat](https://ec.europa.eu/eurostat) | population, GDP per person, household electricity price, energy intensity, unemployment rate | country, year | JSON-stat API |

## Countries and bidding zones

ENTSO-E reports per **bidding zone**, a market area with one electricity price. In the EU, every country
is one zone except Denmark (DK1 west, DK2 east), which gives **27 countries and 28 zones**.

The list of countries, zones, their ENTSO-E codes (EIC), time zones and the Eurostat country codes lives
in one Python module, [`etl/zones.py`](../etl/zones.py). Every script imports it; no script has its own
list. The EIC codes were checked against the mapping of the `entsoe-py` library. Eurostat uses `EL` for
Greece instead of the ISO code `GR`; the module translates.

## ENTSO-E Transparency Platform

The European Network of Transmission System Operators for Electricity publishes the data that grid
operators are legally required to report.

| Data | Document type | Process type |
|---|---|---|
| actual total load | A65 | A16 (realised) |
| day-ahead total load forecast | A65 | A01 (day-ahead) |
| actual generation per production type | A75 | A16 (realised) |

**How the client works** ([`etl/entsoe_client.py`](../etl/entsoe_client.py)):

- **Chunking and parallelism.** A request takes about 14 seconds on the server whether it asks for one
  month or three, and a request may cover at most one year. The client asks for 3-month chunks,
  12 requests in parallel: 1,008 requests for the full period, about 30 minutes.
- **Rate limit.** ENTSO-E allows 400 requests per minute per token and blocks the token for 10 minutes
  when that is exceeded. The client stays at or below 300 per minute and retries failed requests up to
  5 times.
- **"No data" is not an error.** When a zone has no data for a period, ENTSO-E answers HTTP 200 with an
  acknowledgement document ("No matching data found"). The client records it as an empty result; any
  other failure fails the step.
- **Production vs consumption series.** Generation documents also contain consumption series (for
  example pumped storage pumping water up). Only production series are kept.
- **Compressed curves.** Some responses use curve type A03, which leaves out repeated values. The parser
  fills them forward.
- **Hourly grain.** Values for 15 or 30 minutes are averaged to the hour (MW averaged over an hour equals
  MWh for that hour). All times are UTC.

## Open-Meteo

Real historical weather from the Open-Meteo **archive API** (based on reanalysis of weather
observations). The separate climate API of Open-Meteo is a climate-model simulation and was deliberately
not used.

- **89 representative cities**, 3 per country and 4 for the largest (France, Germany, Italy, Netherlands,
  Poland, Romania, Spain, Sweden), so a country's weather is not a single point.
- **Nine hourly variables** at 2 m (temperature, humidity) or 10 m (wind) above ground: temperature,
  relative humidity, precipitation, wind speed, wind direction, sea-level pressure, cloud cover, shortwave
  solar radiation, WMO weather code.
- **Rate limits.** The free tier allows 600 calls per minute, 5,000 per hour and 10,000 per day, where a
  long request counts as several calls (one per 14 days). The client asks per city and year, 4 requests in
  parallel (more gives "too many concurrent requests"), and paces itself to 500 per minute and 4,500 per
  hour. The full 3-year period needs about 7,000 weighted calls, so it takes about one hour and can run
  once a day.
- The WMO weather code is mapped to nine readable conditions (clear, partly cloudy, overcast, fog, rain,
  heavy rain, snow, heavy snow, storm), each with a severity.

## Eurostat

Five yearly indicators per country, each from one dataset with filters that leave exactly one value per
country and period:

| Indicator | Dataset | Unit |
|---|---|---|
| population on 1 January | `demo_pjan` | persons |
| GDP per person | `nama_10_pc` | EUR per inhabitant, current prices |
| household electricity price | `nrg_pc_204` | EUR per kWh excl. taxes, 2,500–4,999 kWh per year, half-yearly |
| energy intensity of the economy | `sdg_07_30` | EUR per kg of oil equivalent |
| unemployment rate | `une_rt_a` | % of active population aged 15–74 |

There are no fallbacks to other units or datasets: a query that fails or returns nothing fails the step,
so a wrong number cannot slip in quietly. Eurostat publishes with a delay and revises figures later; how
the warehouse handles that is described in [Data model](03-data-model.md#yearly-figures-and-history).

## Public holidays

Public holidays per country come from the Python [`holidays`](https://pypi.org/project/holidays/)
package, so weekend and holiday effects on demand can be compared country by country.

---

[← README](../README.md) · [ETL pipeline →](02-etl-pipeline.md)
