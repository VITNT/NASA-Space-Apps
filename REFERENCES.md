# Data sources, citations, and acknowledgements

## Suggested project citation text

> Active-fire and thermal-anomaly observations were obtained from NASA's Fire
> Information for Resource Management System (FIRMS), part of NASA's Earth
> Observing System Data and Information System (EOSDIS). The project used FIRMS
> yearly country-summary archives and archive/near-real-time (NRT) CSV exports
> for MODIS Collection 6.1 on Terra and Aqua (nominal 1 km resolution) and
> VIIRS Collection 2 on Suomi NPP and NOAA-20/JPSS-1 (nominal 375 m resolution).
> Data were accessed on 14 November 2026 from the NASA FIRMS Archive Download
> service: https://firms.modaps.eosdis.nasa.gov/download/.

The yearly archives used in the validated historical dataset cover MODIS for
2000-2025, VIIRS Suomi NPP for 2012-2025, and VIIRS NOAA-20/JPSS-1 for
2018-2025. The merged dataset gives those yearly archives priority through 2025
and uses the separately downloaded archive/NRT exports from 2026 onward. These
are active-fire or thermal-anomaly detections, not burned-area measurements or
confirmed wildfire perimeters.

## Dataset references

Use the FIRMS archive reference together with each sensor product actually used.

1. NASA Fire Information for Resource Management System (FIRMS). (2026).
   *Archive Download: active fire/hotspot data and yearly summaries by country*
   [Data service]. NASA EOSDIS Land, Atmosphere Near real-time Capability for
   EOS (LANCE). https://firms.modaps.eosdis.nasa.gov/download/ (accessed
   2026-11-14).

2. NASA MODIS Science Team. (2021). *MODIS/Aqua+Terra Thermal
   Anomalies/Fire Locations 1 km FIRMS Collection 6.1 NRT (MCD14DL)* [Data
   set]. NASA LANCE MODIS at MODAPS.
   https://doi.org/10.5067/FIRMS/MODIS/MCD14DL.NRT.0061 (accessed 2026-11-14).

3. Giglio, L. (2024). *VIIRS/NPP Active Fires 6-Minute Level-2 Swath 375 m,
   Version 2 (VNP14IMG)* [Data set]. NASA EOSDIS Land Processes Distributed
   Active Archive Center.
   https://doi.org/10.5067/VIIRS/VNP14IMG.002 (accessed 2026-11-14).

4. NASA VIIRS Land Science Team. (2020). *VIIRS (Suomi NPP) I-Band 375 m
   Active Fire Product NRT, Version 2 (VNP14IMGTDL_NRT)* [Data set]. NASA
   LANCE MODIS at MODAPS.
   https://doi.org/10.5067/FIRMS/VIIRS/VNP14IMGT_NRT.002 (accessed
   2026-11-14).

5. Giglio, L. (2024). *VIIRS/JPSS-1 Active Fires 6-Minute Level-2 Swath 375 m,
   Version 2 (VJ114IMG)* [Data set]. NASA EOSDIS Land Processes Distributed
   Active Archive Center.
   https://doi.org/10.5067/VIIRS/VJ114IMG.002 (accessed 2026-11-14).

6. NASA VIIRS Land Science Team. (2020). *VIIRS NOAA-20 (JPSS-1) 375 m,
   I-Band Active Fire Product NRT, Version 2 (VJ114IMGTDL_NRT)* [Data set].
   NASA LANCE MODIS at MODAPS.
   https://doi.org/10.5067/FIRMS/VIIRS/VJ114IMGT_NRT.002 (accessed
   2026-11-14).

## Product documentation and methods

7. Giglio, L., Schroeder, W., & Justice, C. O. (2016). The Collection 6 MODIS
   active fire detection algorithm and fire products. *Remote Sensing of
   Environment, 178*, 31-41. https://doi.org/10.1016/j.rse.2016.02.054.

8. Schroeder, W., Oliva, P., Giglio, L., & Csiszar, I. A. (2014). The New
   VIIRS 375 m active fire detection data product: Algorithm description and
   initial assessment. *Remote Sensing of Environment, 143*, 85-96.
   https://doi.org/10.1016/j.rse.2013.12.008.

9. Giglio, L., Schroeder, W., Hall, J. V., & Justice, C. O. (2021). *MODIS
   Collection 6 and Collection 6.1 Active Fire Product User's Guide, Version
   1.0*. NASA Earthdata.
   https://www.earthdata.nasa.gov/s3fs-public/2023-09/MODIS_C6_C6.1_Fire_User_Guide_1.0.pdf.

10. Giglio, L., Schroeder, W., Hall, J. V., & Justice, C. O. (2025).
    *Collection 2 VIIRS 375 m Active Fire Product User's Guide, Version 1.2*.
    NASA Earthdata.
    https://www.earthdata.nasa.gov/s3fs-public/2025-06/VIIRS_C2_AF-375m_User_Guide_1.2.pdf.

## Development resources

The application was developed in Python and uses the versions recorded in
`requirements.txt`. Its principal technical resources are:

- DuckDB 2.0.0 for CSV ingestion, validation, aggregation, and analytical
  queries: https://duckdb.org/docs/.
- Apache Parquet, written through DuckDB/PyArrow, for partitioned columnar
  storage: https://parquet.apache.org/docs/.
- H3 4.5.0 for hierarchical spatial indexing and exact cell-area metrics:
  https://h3geo.org/docs/.
- FastAPI 0.162.7, Pydantic, and Uvicorn 0.54.0 for the read-only web API:
  https://fastapi.tiangolo.com/ and https://www.uvicorn.org/.
- pandas 3.1.1 and PyArrow 26.0.1 for tabular and Arrow/Parquet support:
  https://pandas.pydata.org/docs/ and https://arrow.apache.org/docs/.
- Requests and HTTPX for HTTP access and API testing:
  https://requests.readthedocs.io/ and https://www.python-httpx.org/.
- The browser interface uses standard HTML, CSS, JavaScript, and SVG. It does
  not use an external basemap or third-party map tiles.

## Acknowledgement

> We acknowledge the use of data from NASA's Fire Information for Resource
> Management System (FIRMS), part of NASA's Earth Science Data and Information
> System (ESDIS), and the MODIS and VIIRS science teams that produced the
> underlying active-fire products.

