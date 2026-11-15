"""Build observed global calendar rows from the same filtered cell-days as the API."""

from __future__ import annotations

import json
from pathlib import Path

from firms_pipeline.api import app
from firms_pipeline.sensitivity import CELL_DAY_FILTER, CELL_X_SQL, CELL_Y_SQL


def main() -> None:
    repo = app.state.repository
    years = repo.rows(f"SELECT DISTINCT year(month_start) AS year FROM {repo.monthly} ORDER BY 1")
    series = {dataset: [] for dataset in ("all", "modis", "viirs_snpp", "viirs_noaa20")}
    for item in years:
        year = item["year"]
        rows = repo.rows(
            f"""
            WITH cell_days AS (
                SELECT acq_date, {CELL_Y_SQL} AS y, {CELL_X_SQL} AS x,
                    count_if(dataset = 'modis') AS modis,
                    count_if(dataset = 'viirs_snpp') AS viirs_snpp,
                    count_if(dataset = 'viirs_noaa20') AS viirs_noaa20
                FROM {repo.points}
                WHERE year = ? AND {CELL_DAY_FILTER}
                GROUP BY acq_date, y, x
            )
            SELECT date_trunc('month', acq_date)::DATE AS date,
                count(*) AS all_cells,
                count_if(modis > 0) AS modis_cells,
                count_if(viirs_snpp > 0) AS viirs_snpp_cells,
                count_if(viirs_noaa20 > 0) AS viirs_noaa20_cells,
                sum(modis) AS modis, sum(viirs_snpp) AS viirs_snpp, sum(viirs_noaa20) AS viirs_noaa20,
                count_if(modis > 0 AND (viirs_snpp > 0 OR viirs_noaa20 > 0)) AS shared_cell_days
            FROM cell_days
            GROUP BY 1 ORDER BY 1
            """,
            [year],
        )
        for row in rows:
            for dataset, output in series.items():
                if row[f"{dataset}_cells"]:
                    output.append({
                        "date": row["date"].isoformat(),
                        "active_cell_days": row[f"{dataset}_cells"],
                        **{key: row[key] if dataset in ("all", key) else 0 for key in series if key != "all"},
                        "shared_cell_days": row["shared_cell_days"] if dataset == "all" else 0,
                        "harmonized_cell_days": None,
                    })
        print(f"Built {year}: {len(rows)} months", flush=True)
    payload = {
        "schema_version": 1,
        "point_run_id": repo.point_manifest["run_id"],
        "series": series,
    }
    output = Path(__file__).resolve().parents[1] / "firms_pipeline" / "resources" / "global_history.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    temp.replace(output)


if __name__ == "__main__":
    main()
