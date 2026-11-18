"""Adds the "? Guide" overlay to every page (PBIR): a Guide button in the header, a hidden overlay (fade layer,
numbered badges, explanation panel with three columns, Close button) and two bookmarks per page (show / hide)
that only touch the overlay visuals and capture no data, so slicer selections stay as they are.

Rerunnable: removes earlier guide visuals and bookmarks first. Also moves the Year/Country slicers right to make
room for the button.
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "powerbi" / "Data-Warehouse-Energy-Weather-Report.Report" / "definition"
PAGES = ROOT / "pages"
BOOKMARKS = ROOT / "bookmarks"
VC_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.14.0/schema.json"
BM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmark/1.4.0/schema.json"
BMM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmarksMetadata/1.0.0/schema.json"

NAVY, GOLD, GREY, TEXT = "#0E2A5C", "#FFCC00", "#5B6B82", "#252423"
NUM = "①②③④⑤"
Z0 = 900000  # overlay above everything on the page


def lit(v):
    return {"expr": {"Literal": {"Value": v}}}


def s(v):
    return lit("'" + v.replace("'", "''") + "'")


def colour(c):
    return {"solid": {"color": lit(f"'{c}'")}}


def run(value, size=12, bold=False, col=TEXT, italic=False):
    st = {"fontSize": f"{size}pt", "color": col}
    if bold:
        st["fontWeight"] = "bold"
    if italic:
        st["fontStyle"] = "italic"
    return {"value": value, "textStyle": st}


def container(name, x, y, w, h, z, visual, hidden=True):
    v = {"$schema": VC_SCHEMA, "name": name,
         "position": {"x": x, "y": y, "z": z, "height": h, "width": w, "tabOrder": z}, "visual": visual}
    if hidden:
        v["isHidden"] = True
    return v


def box_objects(bg=None, transparency=0, border=None, radius=0, pad=(0, 0, 0, 0), shadow=False):
    o = {
        "background": [{"properties": {"show": lit("true" if bg else "false"),
                                       **({"color": colour(bg), "transparency": lit(f"{transparency}D")} if bg else {})}}],
        "border": [{"properties": {"show": lit("true" if border else "false"),
                                   **({"color": colour(border), "width": lit("1D")} if border else {}),
                                   "radius": lit(f"{radius}D")}}],
        "padding": [{"properties": {"top": lit(f"{pad[0]}D"), "bottom": lit(f"{pad[1]}D"),
                                    "left": lit(f"{pad[2]}D"), "right": lit(f"{pad[3]}D")}}],
        "title": [{"properties": {"show": lit("false")}}],
        "visualHeader": [{"properties": {"show": lit("false")}}],
    }
    if shadow:
        o["dropShadow"] = [{"properties": {"show": lit("true"), "preset": lit("'Custom'"), "transparency": lit("80D"),
                                           "shadowBlur": lit("12D"), "shadowDistance": lit("2D"), "angle": lit("90D"),
                                           "shadowSpread": lit("0D")}}]
    return o


def textbox(paragraphs, **box):
    return {"visualType": "textbox",
            "objects": {"general": [{"properties": {"paragraphs": paragraphs}}]},
            "visualContainerObjects": box_objects(**box),
            "drillFilterOtherVisuals": True}


TEMPLATE_BUTTON = Path(__file__).parent / "button_template.json"


def button(bookmark, tooltip, label, icon):
    """Button that applies a bookmark, copied from a button made in Power BI Desktop (style preset "Icon & Text",
    navy fill, white text), so the formatting is in Desktop's own format. Hand-written button formatting was not
    applied correctly. Added: the bookmark action, and no visual background/border (the theme draws white cards)."""
    import copy
    v = copy.deepcopy(json.loads(TEMPLATE_BUTTON.read_text(encoding="utf-8"))["visual"])
    o = v["objects"]
    o["icon"][0]["properties"]["shapeType"] = s(icon)
    o["text"][1]["properties"]["text"] = s(label)
    vco = v.setdefault("visualContainerObjects", {})
    vco["visualLink"] = [{"properties": {"show": lit("true"), "type": lit("'Bookmark'"), "bookmark": s(bookmark),
                                         "tooltip": s(tooltip)}}]
    vco["background"] = [{"properties": {"show": lit("false")}}]
    vco["border"] = [{"properties": {"show": lit("false")}}]
    vco["dropShadow"] = [{"properties": {"show": lit("false")}}]
    vco["title"] = [{"properties": {"show": lit("false")}}]
    vco["visualHeader"] = [{"properties": {"show": lit("false")}}]
    vco["padding"] = [{"properties": {"top": lit("0D"), "bottom": lit("0D"), "left": lit("0D"), "right": lit("0D")}}]
    return v


def column(heading, items):
    """items: list of (lead, text) – lead in bold navy (a number or a term)."""
    paras = [{"textRuns": [run(heading, 14, True, NAVY)]}, {"textRuns": [run(" ", 6)]}]
    for lead, txt in items:
        paras.append({"textRuns": [run(lead + "  ", 11, True, NAVY), run(txt, 11)]})
        paras.append({"textRuns": [run(" ", 4)]})
    return paras


# --- content per page --------------------------------------------------------------------------------------
# badges: (x, y) of numbers ②..⑤ (① is always at the slicers); read: ①..⑤ texts; terms: (term, meaning); look: text
COMMON_1 = ("Year and Country filter every card and chart on this page. They are synced: the selection follows you "
            "to the other pages.")
GUIDES = {
    "b2aba78fa0206e47a030": {
        "title": "Overview, explained",
        "badges": [(900, 310), (1852, 310), (1852, 655)],
        "read": [COMMON_1,
                 "Map: each country is coloured by the share of its generation that comes from renewable sources. "
                 "Hover a country for its demand and demand per person.",
                 "Monthly chart: columns = generation by source, line = demand.",
                 "Mix by year: share of renewable, nuclear, fossil and other generation in each year."],
        "terms": [("TWh", "terawatt-hours: an amount of energy (1 TWh = 1 million MWh)."),
                  ("MW / GW", "power at one moment. Peak load is the highest single hour."),
                  ("Demand", "electricity used, as measured by the grid operators (\"actual load\")."),
                  ("Renewable", "wind, solar, hydro (without pumped storage), biomass, geothermal, marine.")],
        "look": "Which countries are dark green on the map, and does the renewable share rise from year to year? "
                "In the monthly chart, demand is highest in winter while solar peaks in summer.",
    },
    "a2000000000000000000": {
        "title": "Load & forecast, explained",
        "badges": [(1000, 310), (1852, 310), (821, 656), (1220, 655)],
        "read": [COMMON_1,
                 "Forecast quality per month. Switch between Error (how far off) and Bias (too high or too low) "
                 "with the buttons in the chart.",
                 "Forecast error per country, from most to least accurate.",
                 "Heatmap: average demand by weekday and hour (UTC). Darker = higher demand.",
                 "Demand on weekends and public holidays compared with working days in the same zone."],
        "terms": [("Day-ahead forecast", "the demand the grid operators expected, published the day before."),
                  ("Error", "Σ |forecast − actual| ÷ Σ actual: 3 % = off by 3 % of demand on average."),
                  ("Bias", "Σ (forecast − actual) ÷ Σ actual: positive = forecasts too high, negative = too low."),
                  ("UTC", "all hours are UTC; local time is 0–3 hours later.")],
        "look": "Which countries forecast worst, and are their forecasts too high or too low (switch to Bias)? "
                "In the heatmap, look for the working-day peaks and how much demand drops at weekends.",
    },
    "a3000000000000000000": {
        "title": "Generation mix, explained",
        "badges": [(1220, 310), (600, 655), (1220, 655), (1853, 311)],
        "read": [COMMON_1,
                 "Average generation by hour of day (UTC), stacked by category: solar fills the middle of the day.",
                 "The 10 largest production types and their share of all generation.",
                 "Renewable share of generation per month.",
                 "Mix per country, sorted by renewable share. Hover a bar for the exact values."],
        "terms": [("GW", "average power in an hour (1 GW = 1,000 MW)."),
                  ("Category", "Renewable, Nuclear, Fossil, Storage (pumped hydro, batteries) or Other."),
                  ("Intermittent", "depends on the weather: wind, solar, run-of-river hydro, marine."),
                  ("Share", "part of all generation reported to ENTSO-E for the selection.")],
        "look": "Which months have the highest renewable share, and which source drives it? Which countries still "
                "rely mostly on fossil fuels, and which on nuclear?",
    },
    "a5000000000000000000": {
        "title": "Wind, explained",
        "badges": [(1220, 310), (720, 655), (1220, 655), (1852, 310)],
        "read": [COMMON_1,
                 "One dot per day: the windier the day, the more wind power.",
                 "Countries by wind (right = windier) and wind power per person (up = more). Orange = room to grow.",
                 "Wind rose: the longer a wedge, the more hours the wind came from that direction; colours = speed.",
                 "All countries, sorted by wind speed."],
        "terms": [("r", "correlation from −1 to 1. Above 0.7: daily wind output follows the wind closely."),
                  ("kWh per person", "wind generation per inhabitant and year; stands in for the wind capacity "
                                     "a country has built."),
                  ("Room to grow", "windier than the median country, but less wind power per person than the median."),
                  ("Wind speed", "measured 10 m above ground at the countries' cities; turbines stand higher, "
                                 "where it is windier.")],
        "look": "Orange bubbles in the lower right are windy but use little wind power. Select one country to see "
                "its own wind rose and how closely its output follows the wind.",
    },
    "a6000000000000000000": {
        "title": "Solar, explained",
        "badges": [(1220, 310), (720, 655), (1220, 655), (1852, 310)],
        "read": [COMMON_1,
                 "One dot per day: the sunnier the day, the more solar power.",
                 "Countries by sunshine (right = sunnier) and solar power per person (up = more). "
                 "Orange = room to grow.",
                 "Average solar output by hour of day (UTC) for each season.",
                 "All countries, sorted by sunshine, with growth over the selected years."],
        "terms": [("W/m²", "solar radiation reaching the ground, averaged over day and night."),
                  ("r", "correlation from −1 to 1. Above 0.7: daily solar output follows the sunshine closely."),
                  ("Growth", "change from the first to the last selected year (one year: against the year before)."),
                  ("Room to grow", "sunnier than the median country, but less solar power per person than the "
                                   "median.")],
        "look": "Sunny countries in orange produce little solar power for their sunshine. Compare their growth in "
                "the table: some are catching up fast. In the hourly chart, summer middays deliver more than twice "
                "the winter output.",
    },
    "a4000000000000000000": {
        "title": "Weather & socio-economics, explained",
        "badges": [(1220, 310), (600, 655), (1220, 655), (1852, 311)],
        "read": [COMMON_1,
                 "One dot per day: average temperature against total demand, coloured by season.",
                 "Heating and cooling degree days per year for each country.",
                 "Countries by GDP per person and electricity demand per person; bubble size = population.",
                 "Eurostat profile of every country for the selected years."],
        "terms": [("HDD / CDD", "heating / cooling degree days: how far, and for how long, the temperature was "
                                "below / above 18 °C."),
                  ("r", "correlation from −1 to 1; negative = colder days, higher demand."),
                  ("Demand per person", "yearly demand per inhabitant; only country-years with at least 95 % of "
                                        "hours covered."),
                  ("Price", "household electricity price excluding taxes (Eurostat).")],
        "look": "Cold countries with many heating degree days, such as Finland and Sweden, also use the most "
                "electricity per person. Is demand per person linked to GDP?",
    },
}


def page_title(pid):
    return json.loads((PAGES / pid / "page.json").read_text(encoding="utf-8"))["displayName"]


def write_visual(pid, v):
    d = PAGES / pid / "visuals" / v["name"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "visual.json").write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")


def bookmark(name, display, pid, overlay, show):
    containers = {}
    for v in overlay:
        sv = {"visualType": v["visual"]["visualType"], "objects": {}}
        if not show:
            sv["display"] = {"mode": "hidden"}
        containers[v["name"]] = {"singleVisual": sv}
    return {
        "$schema": BM_SCHEMA,
        "displayName": display,
        "name": name,
        "options": {"targetVisualNames": [v["name"] for v in overlay], "applyOnlyToTargetVisuals": True,
                    "suppressData": True},
        "explorationState": {"version": "1.3", "activeSection": pid,
                             "sections": {pid: {"visualContainers": containers}}},
    }


# --- clean up earlier runs -----------------------------------------------------------------------------------
for pid in GUIDES:
    for d in (PAGES / pid / "visuals").glob("guide_*"):
        shutil.rmtree(d)
if BOOKMARKS.exists():
    for f in BOOKMARKS.glob("Guide*.bookmark.json"):
        f.unlink()

order = json.loads((PAGES / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
bookmark_names = []
for pid in order:
    g = GUIDES[pid]
    key = pid[:6]
    show_bm, hide_bm = f"GuideShow_{key}", f"GuideHide_{key}"

    # move the slicers right, make room for the button
    for vdir in (PAGES / pid / "visuals").iterdir():
        f = vdir / "visual.json"
        v = json.loads(f.read_text(encoding="utf-8"))
        if v["visual"]["visualType"] == "slicer" and v["position"]["y"] < 134:
            field = v["visual"]["query"]["queryState"]["Values"]["projections"][0]["queryRef"]
            if field == "dim_date.year":
                v["position"].update(x=1300, width=250)
            elif field == "dim_bidding_zone.country_name":
                v["position"].update(x=1566, width=330)
            f.write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")

    # Guide button (always visible)
    write_visual(pid, container(f"guide_{key}_button", 1160, 60, 120, 40, Z0,
                                button(show_bm, "Show the guide to this page", "Guide", "help"), hidden=False))

    overlay = []
    z = Z0 + 1000
    # fade layer over the page body
    overlay.append(container(f"guide_{key}_veil", 0, 134, 1920, 946, z,
                             textbox([{"textRuns": [run(" ", 6)]}], bg="#FFFFFF", transparency=25)))
    # explanation panel
    overlay.append(container(f"guide_{key}_panel", 40, 690, 1840, 372, z + 1000,
                             textbox([{"textRuns": [run(" ", 6)]}], bg="#FFFFFF", border=NAVY, radius=4, shadow=True)))
    overlay.append(container(f"guide_{key}_title", 64, 702, 1500, 40, z + 2000,
                             textbox([{"textRuns": [run(g["title"], 18, True, NAVY)]}], pad=(4, 0, 0, 0))))
    read = column("How to read this page", list(zip(NUM, g["read"])))
    terms = column("What the numbers mean", g["terms"])
    look = [{"textRuns": [run("What to look for", 14, True, NAVY)]}, {"textRuns": [run(" ", 6)]},
            {"textRuns": [run(g["look"], 11, italic=True)]}]
    for i, paras in enumerate([read, terms, look]):
        overlay.append(container(f"guide_{key}_col{i + 1}", 64 + i * 608, 748, 584, 304, z + 3000 + i,
                                 textbox(paras, pad=(0, 0, 0, 8))))
    overlay.append(container(f"guide_{key}_close", 1740, 702, 120, 40, z + 4000,
                             button(hide_bm, "Hide the guide", "Close  ✕", "blank")))
    # numbered badges: ① at the slicers, ②.. at the visuals
    for i, (x, y) in enumerate([(1350, 24)] + g["badges"]):
        overlay.append(container(f"guide_{key}_badge{i + 1}", x, y, 30, 30, z + 5000 + i,
                                 textbox([{"textRuns": [run(str(i + 1), 13, True, NAVY)],
                                           "horizontalTextAlignment": "center"}],
                                         bg=GOLD, radius=15, pad=(3, 0, 0, 0))))
    for v in overlay:
        write_visual(pid, v)

    BOOKMARKS.mkdir(exist_ok=True)
    name = page_title(pid)
    for bm, show in ((show_bm, True), (hide_bm, False)):
        b = bookmark(bm, f"Guide – {name} ({'show' if show else 'hide'})", pid, overlay, show)
        (BOOKMARKS / f"{bm}.bookmark.json").write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
        bookmark_names.append(bm)

meta_file = BOOKMARKS / "bookmarks.json"
meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {"$schema": BMM_SCHEMA, "items": []}
meta["items"] = [i for i in meta["items"] if i["name"] != "Guide"]
meta["items"].append({"name": "Guide", "displayName": "Guide", "children": bookmark_names})
meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"guide added to {len(order)} pages, {len(bookmark_names)} bookmarks")
