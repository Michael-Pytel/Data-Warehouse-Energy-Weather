# Power BI page generators

Scripts that write the report's page files (PBIR, `powerbi/Data-Warehouse-Energy-Weather-Report.Report/definition`)
instead of building the pages by hand in Power BI Desktop. The measures they use are in the semantic model
(`_Measures.tmdl`); the scripts do not change the model.

| File | What it does |
|---|---|
| `gen_wind_page.py` | Rewrites the **Wind** page completely: header, slicers, cards, daily scatter, "room to grow" quadrant, wind rose (Deneb), country table, footer. Uses the Weather & socio-economics page as template |
| `gen_solar_page.py` | Rewrites the **Solar** page completely, the same way (hourly profile from the Generation mix line chart) |
| `gen_guide.py` | Adds the **Guide** to all pages: button, hidden overlay (fade layer, numbered badges, explanation panel, Close button) and the show/hide bookmarks. All guide texts are in this script. Also places the Year/Country slicers |
| `button_template.json` | A button made in Power BI Desktop; `gen_guide.py` copies its format, because hand-written button formatting is not applied by Desktop |
| `wind_rose.vl.json` | Vega-Lite spec of the wind rose, put into the Deneb visual by `gen_wind_page.py` |

## How to run

1. Close Power BI Desktop (otherwise saving in Desktop overwrites the files).
2. From the repository root:

   ```powershell
   python tools/powerbi/gen_wind_page.py    # only if the Wind page should be rebuilt
   python tools/powerbi/gen_solar_page.py   # only if the Solar page should be rebuilt
   python tools/powerbi/gen_guide.py        # always last: a page generator removes that page's guide
   ```

3. Open the `.pbip` and check the pages.

Changes made in Desktop to the Wind or Solar page are lost when its generator runs again.
