# malsim: synthetic malaria routine data for real DHIS2 facilities (v1)

## What it does

malsim takes the real list of health facilities from a DHIS2 instance and creates made-up but realistic monthly malaria data for each of them, as if every facility had reported its cases, tests, treatments, severe cases, deaths and stock levels every month.

It's meant for testing: the national malaria data repository, the SNT toolkit and data-quality services. You get data that looks like the real thing, without touching any real reported values. And because the data is simulated, the correct answer is always known.

There are two commands, both runnable locally or as the OpenHEXA pipeline's `mode`:
- **`inspect`** checks the inputs before a run and changes nothing. It reports how many facilities were found and how they're typed, which districts have population and for which years, whether every district has a boundary, and the range of incidence values.
- **`generate`** runs the simulation and writes all the outputs described below.

## What data it uses

- **The facility pyramid, from DHIS2:** every unit's name, its place in the hierarchy (country → region → district → facility) and its closing date, if any. Only the structure is used, never reported values.
- **Population per district and year:** a DHIS2 data element, chosen at run time, or a CSV. Missing years are projected from the nearest year with data at 3% growth a year.
- **Baseline malaria incidence per district:** cases per 1,000 people per year, taken from a Malaria Atlas Project (MAP) raster averaged over each district's boundary, or from a CSV.
- **District boundaries (polygons):** needed only when incidence comes from a raster. They're read from the org unit geometries in DHIS2, or from a GeoJSON file that carries each district's DHIS2 UID.
- **Optional facility types:** from a DHIS2 group set, a CSV, or rules on facility names (as for Côte d'Ivoire, e.g. "CSR…" = rural health centre).
- **Parameters** in a YAML config file (see `examples/tiny/config.yaml` for all of them).

No real malaria data values are read.

## How the numbers are made, and what that assumes

1. **Expected cases** per district and month = population × annual incidence ÷ 12 × the share of sick people who seek care (60%).
2. **Confirmed cases** are drawn at random around that expectation (a Poisson draw), which gives natural month-to-month variation.
3. **Cases are shared out among the district's facilities** at random, weighted by facility type. For example, a district hospital gets three times the share of a rural health centre. Opticians and dental practices get weight 0 and are left out.
4. **Every other number follows from the confirmed cases** at fixed rates:
   - 40% of cases are children under 5;
   - 45% of tests are positive, which gives the number tested;
   - 90% of suspected cases are tested, which gives the number suspected;
   - microscopy is 30% of tests in hospitals and 20% in clinics; all other tests are RDTs;
   - 95% of cases get an ACT;
   - 5% are severe, and 2% of severe cases die.
5. **RDT and ACT stocks** start at two months of expected use, and receive a delivery equal to the expected use every month.

The main assumptions and simplifications in this version:
- **No seasonality:** every month of a year has the same expected number of cases.
- **Incidence is fixed** at one baseline year (2023 for Côte d'Ivoire) for the whole period. Only population changes over time.
- **Every facility reports every month.** There are no missing reports, late reports or data-entry errors yet.
- **Stockouts have no effect:** a facility keeps testing and treating even when stock runs out, and the shortfall is only recorded. There's no presumptive treatment.
- **The facility-type weights and microscopy shares are working assumptions**, to be reviewed with the programme.
- **The numbers always add up:** under-5 plus 5 and over equals the total, and confirmed ≤ tested ≤ suspected.

The same config, inputs and random seed always give exactly the same data.

## What it produces

- **`datavalues.csv`, the dataset.** This is what DHIS2 would contain: one row per facility, month, data element and value, which is what a tool under test gets to see. With a DHIS2 mapping, `datavalues.json` holds the same values in a form DHIS2 can import.
- **`ground_truth.parquet`, the answer key.** It holds the true values behind the dataset, plus everything that's never reported:
  - per district: population, input incidence and expected cases;
  - per facility: its share of district cases, its expected cases, and its stock deliveries, expected use and unmet need.

  In this version the reported values equal the true ones, because no errors are added yet. Once later versions add missing reports and errors, the dataset will differ from the ground truth. Comparing a tool's output with the ground truth then shows how well it recovered what really happened: did a data-quality check flag the injected errors? Does an incidence estimate match the input?
- **`run_params.yaml`:** every setting used, the seed, the code version, and fingerprints (hashes) of the inputs, so a run can be traced and repeated.
- **`summary_report.md`, with CSV tables:** totals per year, simulated vs input incidence per district, results by facility type, how often months are zero, and stock shortfalls.
- **`inputs/`:** copies of the inputs as read (facilities, population, district boundaries, incidence per district).

### Indicators produced

One value per facility and month, in `datavalues.csv` (column `element`):

| Element | What it is | How it is derived |
|---|---|---|
| `confirmed` | Confirmed malaria cases | Facility's share of the district's Poisson-drawn cases |
| `confirmed_u5` | Confirmed cases, under 5 | Binomial(confirmed, `u5_share`) |
| `confirmed_5plus` | Confirmed cases, 5 and over | confirmed − confirmed_u5 |
| `tested_total` | Suspected cases tested | confirmed ÷ `positivity`, rounded |
| `tested_microscopy` | Tested by microscopy | Binomial(tested_total, microscopy share of the facility type) |
| `tested_rdt` | Tested by RDT | tested_total − tested_microscopy |
| `suspected` | Suspected malaria cases | tested_total ÷ `testing_rate`, rounded |
| `act_treated` | Confirmed cases treated with an ACT | Binomial(confirmed, `act_rate`) |
| `severe` | Severe malaria cases | Binomial(confirmed, `severe_rate`) |
| `deaths` | Malaria deaths | Binomial(severe, `cfr`) |
| `stock_rdt_end` | RDTs in stock at month end | max(0, opening stock + delivery − tested_rdt) |
| `stock_act_end` | ACT treatments in stock at month end | max(0, opening stock + delivery − act_treated) |

`datavalues.json` contains only the elements listed in the DHIS2 mapping file.

The sections below are the technical reference.

## Install and run

```bash
pip install -e ".[test]"
malsim generate --config examples/tiny/config.yaml --out output
pytest
```

`--seed` overrides `run.seed`, and `--population-de UID` sets the population data element.
Same config, seed and inputs give byte-identical data outputs.

Input rasters are not in the repository. For the Côte d'Ivoire example, download the MAP
*P. falciparum* incidence rate rasters for CIV from the Malaria Atlas Project data portal and put them in
`data/civ/MAP-rasters/` (the config uses `202608_Global_Pf_Incidence_Rate_CIV_2023.tiff`).

Before a real run, check the inputs (read-only):

```bash
malsim inspect --config examples/civ/config.yaml --population-de ep7dbOseL68
```

It lists the levels, group sets, facilities mapped and closed, population coverage per
year, polygon coverage and the range of raster incidence per unit.

## Inputs (all set in the YAML config)

| Input | Config | Format |
|---|---|---|
| Org unit pyramid | `pyramid.source` = `metadata` / `csv` / `api` | DHIS2 metadata JSON export (`organisationUnits` with `id, name, level, parent`), CSV (`id, name, level, parent`), or the Web API |
| Levels | `pyramid.facility_level`, `pyramid.population_level` | integers; facilities are the units at `facility_level` |
| Facility types (optional) | `pyramid.facility_type` | org unit group set UID (from the export or API) or CSV `ou_uid, type` |
| Population | `population.source` = `csv` / `dhis2` | CSV `ou_uid, year, pop`, or a data element. `dhis2_method: raw` (default) reads yearly values stored at the population units themselves; `analytics` uses analytics totals, which also sum values entered below the unit or for other periods. Missing years are projected from the nearest observed year with `run.pop_growth` |
| Baseline incidence | `incidence.source` = `csv` / `raster` | CSV `ou_uid, incidence_per_1000`, or a raster (`incidence.raster_path`) reduced to each population unit by zonal mean over its polygon (`all_touched`). Needs `pip install -e ".[raster]"`; `raster_scale` converts to per 1000/year (MAP rasters: 1000) |
| Population unit geometries (raster only) | `incidence.geometry_source` = `dhis2` / `file` | `dhis2`: org unit geometries from the Web API (Polygon / MultiPolygon only). `file`: GeoJSON FeatureCollection at `incidence.geometry_path`, with the org unit UID in the property named by `incidence.id_property` (default `id`, which also matches the feature's own `id`, as in DHIS2 GeoJSON exports). A population unit with facilities but no polygon gets no incidence, and the run stops with an error |
| Closed facilities | `pyramid.drop_closed_before_start` | facilities with a `closedDate` before `run.start` are dropped (default true) |
| DHIS2 mapping (optional) | `mapping` | YAML element -> `dataElement` / `categoryOptionCombo`; see `examples/tiny/mapping.yaml` |

DHIS2 credentials never go in the YAML. Locally they come from environment variables
(`DHIS2_USERNAME` / `DHIS2_PASSWORD` or `DHIS2_TOKEN`; the names are set under `dhis2`,
e.g. `CIV_NMDR_USERNAME` in `examples/civ/config.yaml`). On OpenHEXA they come from the
workspace DHIS2 connection, and the env vars and `dhis2.url` are not used.
`run_params.yaml` records the URL and the credential source, never the credentials.

API inputs are saved to `output/.../inputs/` (org units, facilities, observed population,
polygons, incidence per unit), with their SHA-256 in `run_params.yaml`, so two runs can be compared.

See `examples/tiny/config.yaml` for every parameter with its default.

## Model

Per population unit u and month m:

1. `expected_cases = pop[u, year] x incidence[u] / 1000 x month_weight(u, m) x care_seeking`, with `month_weight = 1/12` in v1.
2. `confirmed[u, m] ~ Poisson(expected_cases)`.
3. Multinomial allocation to the unit's facilities, weights by facility type (normalised within the unit).

Per facility, all elements derive from `confirmed` (see `model.py`), so the
invariants hold by construction and are also checked on every run
(`invariants.py`; a violation stops the run).

Stocks: start = `stock_start_months` x first month's expected use; delivery each
month = `delivery_factor` x that month's expected use (deterministic, tracks
population growth); `end = max(0, start + delivery - use)`. Stock does not
constrain use in v1; shortfalls are kept in the ground truth as `unmet_rdt` / `unmet_act`.

Each random step has its own RNG stream (`model.STREAMS`), so adding steps later
does not change the draws of existing ones.

Notes:
- Population units with no facilities are skipped; facilities with no ancestor at
  the population level are dropped. Both are listed in `run_params.yaml` and the report.
- Only units exactly at `facility_level` are facilities.

## Outputs (in `output.dir`)

- `datavalues.csv`: `ou_uid, period, element, value` (period `YYYYMM`).
- `datavalues.json`: DHIS2 `dataValueSets` payload, mapped elements only (when `mapping` is set).
- `ground_truth.parquet`: one row per population unit x month and facility x month
  (`role` column): population, incidence, month weight, expected cases, allocation
  weight, microscopy share, all elements, stock starts, deliveries, expected use, shortfalls.
- `run_params.yaml`: resolved config, seed, code version, SHA-256 of input files.
- `summary_report.md`, `summary_by_unit_year.csv`, `summary_zero_months.csv`:
  totals per unit/year, implied incidence vs input, share of zero months, stock shortfalls.

## OpenHEXA

1. In the workspace settings, create a DHIS2 connection for the instance.
2. In workspace Files, upload `examples/` and `data/` under one folder (e.g. `malsim/`),
   keeping the layout, so that relative paths in the config still resolve
   (`malsim/examples/civ/config.yaml` points to `malsim/data/civ/MAP-rasters/...`).
3. Build and push the pipeline (the malsim package is bundled with it):

   ```bash
   python scripts/build_openhexa.py
   openhexa pipelines push build/openhexa
   ```

4. Run it from the web interface: pick the connection, set `population_de`, and run
   `mode: inspect` first, then `mode: generate`. Outputs go to `output_dir` in workspace Files.

Notebook (same code, faster to iterate), with the bundle's `malsim/` folder uploaded next to the notebook:

```python
from openhexa.sdk import workspace
from malsim.dhis2 import Dhis2Client
from malsim.inspect import inspect
from malsim.run import generate, resolve_config

con = workspace.dhis2_connection("<connection-slug>")
client = Dhis2Client(con.url, username=con.username, password=con.password, source="openhexa_connection")
cfg_path = f"{workspace.files_path}/malsim/examples/civ/config.yaml"
inspect(resolve_config(cfg_path, population_de="ep7dbOseL68"), client)
generate(cfg_path, population_de="ep7dbOseL68", client=client, out_dir=f"{workspace.files_path}/malsim/output/civ")
```

The pipeline and the notebook snippet have not been run on OpenHEXA yet.

## Extending (later versions)

- Seasonality: replace `model.month_weight` (or pass another `weight_fn` to `month_weight_matrix`).
- Stockout feedback: make `split_test_method` / `treat` take available stock, and run the
  stock loop month by month.
- Missing reports / error injection: a post-processing step on `SimulationResult.facilities`
  before writing, with its own RNG stream.
