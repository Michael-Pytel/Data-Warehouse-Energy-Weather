"""Generates the Solar page (PBIR) from the visuals of the Weather & socio-economics page.

Run again after changes: it rewrites pages/a6000000000000000000 completely.
This also removes the page's guide overlay, so run gen_guide.py afterwards.
"""
import copy
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "powerbi" / "Data-Warehouse-Energy-Weather-Report.Report" / "definition"
PAGES = ROOT / "pages"
TEMPLATE = PAGES / "a4000000000000000000" / "visuals"
PAGE_ID = "a6000000000000000000"
PREFIX = "f6"


def load(name):
    return json.loads((TEMPLATE / name / "visual.json").read_text(encoding="utf-8"))


def lit(value):
    return {"expr": {"Literal": {"Value": value}}}


def text(value):
    return lit("'" + value.replace("'", "''") + "'")


def measure_ref(name):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": name}}


def column_ref(entity, name):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def proj_measure(name, display=None, active=None):
    p = {"field": measure_ref(name), "queryRef": f"_Measures.{name}", "nativeQueryRef": name}
    if display:
        p["displayName"] = display
    if active:
        p["active"] = True
    return p


def proj_column(entity, name, display=None, active=None):
    p = {"field": column_ref(entity, name), "queryRef": f"{entity}.{name}", "nativeQueryRef": name}
    if display:
        p["displayName"] = display
    if active:
        p["active"] = True
    return p


def place(v, n, x, y, w, h):
    name = f"{PREFIX}{n:018d}"
    v["name"] = name
    v["position"] = {"x": x, "y": y, "z": n * 1000, "height": h, "width": w, "tabOrder": n * 1000}
    return v


def set_title(v, title_measure=None, title_text=None, subtitle=None):
    vco = v["visual"].setdefault("visualContainerObjects", {})
    t = {"show": lit("true"), "fontSize": lit("15D")}
    t["text"] = {"expr": measure_ref(title_measure)} if title_measure else text(title_text)
    vco["title"] = [{"properties": t}]
    if subtitle is not None:
        vco["subTitle"] = [{"properties": {"show": lit("true"), "text": text(subtitle), "fontSize": lit("11D")}}]


def axis_titles(v, x_title, y_title):
    o = v["visual"]["objects"]
    o["categoryAxis"] = [{"properties": {"showAxisTitle": lit("true"), "titleText": text(x_title)}}]
    o["valueAxis"] = [{"properties": {"showAxisTitle": lit("true"), "titleText": text(y_title)}}]


def ref_line(measure, label):
    return {
        "properties": {
            "show": lit("true"),
            "displayName": text(label),
            "value": {"expr": measure_ref(measure)},
            "lineColor": {"solid": {"color": lit("'#5B6B82'")}},
            "style": lit("'dashed'"),
            "width": lit("1D"),
            "transparency": lit("0D"),
        },
        "selector": {"id": "1"},
    }



visuals = []

header = load("d4000000000000000001")
paras = header["visual"]["objects"]["general"][0]["properties"]["paragraphs"]
paras[0]["textRuns"][0]["value"] = "How much power does the sun deliver?"
paras[1]["textRuns"][0]["value"] = ("Sunshine and solar output per day and hour, how fast solar grows, and which countries "
                                    "could use more of their sun.")
visuals.append(place(header, 1, 0, 0, 1920, 134))
visuals.append(place(load("d4000000000000000002"), 2, 48, 118, 72, 4))
visuals.append(place(load("d4000000000000000003"), 3, 1180, 26, 330, 90))
visuals.append(place(load("d4000000000000000004"), 4, 1530, 26, 366, 90))

cards = ["Solar Generation (TWh)", "Solar Share %", "Solar Growth %", "Avg Solar Radiation (W/m²)",
         "Solar Output Correlation (r)"]
for i, m in enumerate(cards):
    c = load("d4000000000000000010")
    c["visual"]["query"]["queryState"]["Data"]["projections"] = [proj_measure(m)]
    visuals.append(place(c, 10 + i, 24 + i * 376, 150, 360, 135))

# (1) daily solar output vs radiation
s = load("d4000000000000000020")
season_colours = copy.deepcopy(s["visual"]["objects"]["dataPoint"])
qs = s["visual"]["query"]["queryState"]
qs["X"]["projections"] = [proj_measure("Avg Solar Radiation (W/m²)", active=True)]
qs["Y"]["projections"] = [proj_measure("Solar Generation (GWh)")]
axis_titles(s, "Average solar radiation (W/m², day and night)", "Solar output per day (GWh)")
set_title(s, title_measure="Title Solar Scatter",
          subtitle="One dot per day: average solar radiation at the countries' cities and solar output, coloured by season")
visuals.append(place(s, 20, 24, 300, 1240, 330))

# (2) sun vs solar use per country (quadrant)
q = load("d4000000000000000022")
qs = q["visual"]["query"]["queryState"]
qs["X"]["projections"] = [proj_measure("Avg Solar Radiation (W/m²)", active=True)]
qs["Y"]["projections"] = [proj_measure("Solar per Capita (kWh)")]
qs["Size"]["projections"] = [proj_measure("Population")]
axis_titles(q, "Average solar radiation (W/m²)", "Solar output per person (kWh)")
o = q["visual"]["objects"]
o["dataPoint"] = [{
    "properties": {"fill": {"solid": {"color": {"expr": measure_ref("Solar Potential Colour")}}}},
    "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]},
}]
o["xAxisReferenceLine"] = [ref_line("Radiation Median (W/m²)", "Median radiation")]
o["y1AxisReferenceLine"] = [ref_line("Solar per Capita Median (kWh)", "Median solar output per person")]
set_title(q, title_measure="Title Solar Potential",
          subtitle="One bubble per country, size = population. Dashed lines = medians. Orange = room to grow")
visuals.append(place(q, 21, 24, 645, 740, 330))

# (3) hourly profile by season
p = json.loads((PAGES / "a3000000000000000000" / "visuals" / "c3000000000000000022" / "visual.json").read_text(encoding="utf-8"))
p["visual"]["query"] = {
    "queryState": {
        "Category": {"projections": [proj_column("dim_time", "time_key", "Hour (UTC)", active=True)]},
        "Series": {"projections": [proj_column("dim_date", "season")]},
        "Y": {"projections": [proj_measure("Average Solar Generation (GW)")]},
    },
    "sortDefinition": {"sort": [{"field": column_ref("dim_time", "time_key"), "direction": "Ascending"}]},
}
po = p["visual"]["objects"]
# continuous axis: all 24 hours fit the narrow visual without a scrollbar
po["categoryAxis"] = [{"properties": {"axisType": lit("'Scalar'"), "start": lit("0D"), "end": lit("23D"), "showAxisTitle": lit("true"),
                                       "titleText": text("Hour of day (UTC)")}}]
po["valueAxis"] = [{"properties": {"showAxisTitle": lit("true"), "titleText": text("Average output (GW)")}}]
po["legend"] = [{"properties": {"show": lit("true"), "position": lit("'Top'")}}]
po["dataPoint"] = season_colours
set_title(p, title_measure="Title Solar Profile", subtitle="Average solar output per hour of day, by season")
visuals.append(place(p, 22, 780, 645, 484, 330))

# (4) country table
t = load("d4000000000000000023")
t["visual"]["query"]["queryState"]["Values"]["projections"] = [
    proj_column("dim_bidding_zone", "country_name", "Country"),
    proj_measure("Avg Solar Radiation (W/m²)", "Sun (W/m²)"),
    proj_measure("Solar Share %", "Share"),
    proj_measure("Solar per Capita (kWh)", "kWh/person"),
    proj_measure("Solar Growth %", "Growth"),
    proj_measure("Solar Output Correlation (r)", "r"),
    proj_measure("Solar Room to Grow", "Room to grow"),
]
t["visual"]["query"]["sortDefinition"] = {"sort": [{"field": measure_ref("Avg Solar Radiation (W/m²)"), "direction": "Descending"}]}
set_title(t, title_text="Solar by country",
          subtitle="Growth: first to last selected year (one year: vs the year before)")
visuals.append(place(t, 23, 1280, 300, 616, 675))

f = load("d4000000000000000030")
f["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"][0]["value"] = (
    "Sources: ENTSO-E (solar generation), Open-Meteo (solar radiation at each country's cities, day and night "
    "average), Eurostat (population). The Netherlands and Ireland report almost no solar to ENTSO-E (rooftop solar "
    "is missing), so their values are too low. Output per person stands in for installed capacity, so \"room to "
    "grow\" is a pointer, not a verdict. Only country-years with at least 95 % of days covered count (Cyprus: none). "
    "Bulgaria and Malta are excluded."
)
visuals.append(place(f, 30, 24, 990, 1872, 70))

page_dir = PAGES / PAGE_ID
if page_dir.exists():
    shutil.rmtree(page_dir)
(page_dir / "visuals").mkdir(parents=True)
page = json.loads((PAGES / "a4000000000000000000" / "page.json").read_text(encoding="utf-8"))
page["name"] = PAGE_ID
page["displayName"] = "Solar"
(page_dir / "page.json").write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")
for v in visuals:
    d = page_dir / "visuals" / v["name"]
    d.mkdir()
    (d / "visual.json").write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")

pages_file = PAGES / "pages.json"
pages = json.loads(pages_file.read_text(encoding="utf-8"))
if PAGE_ID not in pages["pageOrder"]:
    pages["pageOrder"].insert(pages["pageOrder"].index("a5000000000000000000") + 1, PAGE_ID)
pages["activePageName"] = PAGE_ID
pages_file.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"{len(visuals)} visuals written to {page_dir}")
