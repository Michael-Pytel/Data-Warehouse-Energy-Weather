"""Generates the Wind page (PBIR) from the visuals of the Weather & socio-economics page.

Run again after changes: it rewrites pages/a5000000000000000000 completely.
This also removes the page's guide overlay, so run gen_guide.py afterwards.
"""
import copy
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "powerbi" / "Data-Warehouse-Energy-Weather-Report.Report" / "definition"
PAGES = ROOT / "pages"
TEMPLATE = PAGES / "a4000000000000000000" / "visuals"
PAGE_ID = "a5000000000000000000"
PREFIX = "e5"
DENEB = "deneb7E15AEF80B9E4D4F8E12924291ECE89A"
SPEC = Path(__file__).parent / "wind_rose.vl.json"


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

# --- header band, gold bar, slicers -------------------------------------------------------------
header = load("d4000000000000000001")
paras = header["visual"]["objects"]["general"][0]["properties"]["paragraphs"]
paras[0]["textRuns"][0]["value"] = "How much power does the wind deliver?"
paras[1]["textRuns"][0]["value"] = ("Wind speed and wind output per day, which countries could use more of their wind, "
                                    "and where the wind comes from.")
visuals.append(place(header, 1, 0, 0, 1920, 134))
visuals.append(place(load("d4000000000000000002"), 2, 48, 118, 72, 4))
visuals.append(place(load("d4000000000000000003"), 3, 1180, 26, 330, 90))
visuals.append(place(load("d4000000000000000004"), 4, 1530, 26, 366, 90))

# --- cards ----------------------------------------------------------------------------------------
cards = ["Wind Generation (TWh)", "Wind Share %", "Avg Wind Speed (km/h)", "Wind Output Correlation (r)",
         "Average Wind Direction"]
for i, m in enumerate(cards):
    c = load("d4000000000000000010")
    c["visual"]["query"]["queryState"]["Data"]["projections"] = [proj_measure(m)]
    visuals.append(place(c, 10 + i, 24 + i * 376, 150, 360, 135))

# --- (1) daily wind output vs wind speed ----------------------------------------------------------------
s = load("d4000000000000000020")
qs = s["visual"]["query"]["queryState"]
qs["X"]["projections"] = [proj_measure("Avg Wind Speed (km/h)", active=True)]
qs["Y"]["projections"] = [proj_measure("Wind Generation (GWh)")]
axis_titles(s, "Average wind speed (km/h)", "Wind output per day (GWh)")
set_title(s, title_measure="Title Wind Scatter",
          subtitle="One dot per day: average wind speed (10 m above ground at the countries' cities) and onshore + "
                   "offshore wind output, coloured by season")
visuals.append(place(s, 20, 24, 300, 1240, 330))

# --- (2) wind resource vs wind use per country (quadrant) ---------------------------------------
q = load("d4000000000000000022")
qs = q["visual"]["query"]["queryState"]
qs["X"]["projections"] = [proj_measure("Avg Wind Speed (km/h)", active=True)]
qs["Y"]["projections"] = [proj_measure("Wind per Capita (kWh)")]
qs["Size"]["projections"] = [proj_measure("Population")]
axis_titles(q, "Average wind speed (km/h)", "Wind output per person (kWh)")
o = q["visual"]["objects"]
o["dataPoint"] = [{
    "properties": {"fill": {"solid": {"color": {"expr": measure_ref("Wind Potential Colour")}}}},
    "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]},
}]
o["xAxisReferenceLine"] = [ref_line("Wind Speed Median (km/h)", "Median wind speed")]
o["y1AxisReferenceLine"] = [ref_line("Wind per Capita Median (kWh)", "Median wind output per person")]
set_title(q, title_measure="Title Wind Potential",
          subtitle="One bubble per country, size = population. Dashed lines = medians. Orange = room to grow")
visuals.append(place(q, 21, 24, 645, 740, 330))

# --- (3) wind rose (Deneb) ----------------------------------------------------------------------------
spec = json.loads(SPEC.read_text(encoding="utf-8"))
spec.pop("width", None)   # Deneb sizes the view to the visual
spec.pop("height", None)
config = spec.pop("config", {})
config["autosize"] = {"type": "fit", "contains": "padding"}
rose = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.14.0/schema.json",
    "name": "",
    "position": {},
    "visual": {
        "visualType": DENEB,
        "query": {"queryState": {"dataset": {"projections": [
            proj_column("fact_load", "Wind Sector No", active=True),
            proj_column("fact_load", "Wind Sector"),
            proj_column("fact_load", "Wind Speed Band No"),
            proj_column("fact_load", "Wind Speed Band"),
            proj_measure("Wind Hours"),
        ]}}},
        "objects": {"vega": [{"properties": {
            "provider": text("vegaLite"),
            "jsonSpec": text(json.dumps(spec, ensure_ascii=False, indent=2)),
            "jsonConfig": text(json.dumps(config, ensure_ascii=False, indent=2)),
            "renderMode": text("svg"),
            "enableTooltips": lit("true"),
            "enableContextMenu": lit("true"),
            "enableSelection": lit("false"),
            "enableHighlight": lit("false"),
        }}]},
        "drillFilterOtherVisuals": True,
    },
}
set_title(rose, title_measure="Title Wind Rose",
          subtitle="Share of hours by wind direction and speed")
visuals.append(place(rose, 22, 780, 645, 484, 330))

# --- (4) country table --------------------------------------------------------------------------------
t = load("d4000000000000000023")
t["visual"]["query"]["queryState"]["Values"]["projections"] = [
    proj_column("dim_bidding_zone", "country_name", "Country"),
    proj_measure("Avg Wind Speed (km/h)", "Wind (km/h)"),
    proj_measure("Wind Share %", "Share"),
    proj_measure("Wind per Capita (kWh)", "kWh/person"),
    proj_measure("Offshore Share of Wind %", "Offshore"),
    proj_measure("Wind Output Correlation (r)", "r"),
    proj_measure("Wind Room to Grow", "Room to grow"),
]
t["visual"]["query"]["sortDefinition"] = {"sort": [{"field": measure_ref("Avg Wind Speed (km/h)"), "direction": "Descending"}]}
set_title(t, title_text="Wind by country",
          subtitle="r = how closely daily wind output follows daily wind speed (1 = fully)")
visuals.append(place(t, 23, 1280, 300, 616, 675))

# --- footer ----------------------------------------------------------------------------------------------
f = load("d4000000000000000030")
f["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"][0]["value"] = (
    "Sources: ENTSO-E (wind onshore + offshore generation), Open-Meteo (wind 10 m above ground at each country's "
    "cities), Eurostat (population). City wind shows how windy a country is, not wind at turbine height or offshore. "
    "Output per person stands in for installed capacity (not in the warehouse), so \"room to grow\" is a pointer, not a "
    "verdict. Only country-years with at least 95 % of days covered count (Cyprus: none). Ireland = all-island market. "
    "Bulgaria and Malta are excluded."
)
visuals.append(place(f, 30, 24, 990, 1872, 70))

# --- write -------------------------------------------------------------------------------------------------
page_dir = PAGES / PAGE_ID
if page_dir.exists():
    shutil.rmtree(page_dir)
(page_dir / "visuals").mkdir(parents=True)
page = json.loads((PAGES / "a4000000000000000000" / "page.json").read_text(encoding="utf-8"))
page["name"] = PAGE_ID
page["displayName"] = "Wind"
(page_dir / "page.json").write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")
for v in visuals:
    d = page_dir / "visuals" / v["name"]
    d.mkdir()
    (d / "visual.json").write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")

pages_file = PAGES / "pages.json"
pages = json.loads(pages_file.read_text(encoding="utf-8"))
if PAGE_ID not in pages["pageOrder"]:
    pages["pageOrder"].insert(pages["pageOrder"].index("a3000000000000000000") + 1, PAGE_ID)
pages["activePageName"] = PAGE_ID
pages_file.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")

report_file = ROOT / "report.json"
report = json.loads(report_file.read_text(encoding="utf-8"))
if DENEB not in report.get("publicCustomVisuals", []):
    report.setdefault("publicCustomVisuals", []).append(DENEB)
report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"{len(visuals)} visuals written to {page_dir}")
