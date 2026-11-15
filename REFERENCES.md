R# Data sources, citations, and acknowledgements

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

## A. NASA fire data

Use the FIRMS archive reference together with each sensor product actually used.

1. NASA Fire Information for Resource Management System (FIRMS). (2026).
   _Archive Download: active fire/hotspot data and yearly summaries by country_
   [Data service]. NASA EOSDIS Land, Atmosphere Near real-time Capability for
   EOS (LANCE). https://firms.modaps.eosdis.nasa.gov/download/ (accessed
   2026-11-14).

2. NASA FIRMS. (2026). _FIRMS API: data availability endpoint_
   [Web service; MAP_KEY required]. Used by `app.py` for the connectivity check.
   https://firms.modaps.eosdis.nasa.gov/api/ (accessed 2026-11-14).

3. NASA MODIS Science Team. (2021). _MODIS/Aqua+Terra Thermal
   Anomalies/Fire Locations 1 km FIRMS Collection 6.1 NRT (MCD14DL)_ [Data
   set]. NASA LANCE MODIS at MODAPS.
   https://doi.org/10.5067/FIRMS/MODIS/MCD14DL.NRT.0061 (accessed 2026-11-14).

4. Giglio, L. (2024). _VIIRS/NPP Active Fires 6-Minute Level-2 Swath 375 m,
   Version 2 (VNP14IMG)_ [Data set]. NASA EOSDIS Land Processes Distributed
   Active Archive Center.
   https://doi.org/10.5067/VIIRS/VNP14IMG.002 (accessed 2026-11-14).

5. NASA VIIRS Land Science Team. (2020). _VIIRS (Suomi NPP) I-Band 375 m
   Active Fire Product NRT, Version 2 (VNP14IMGTDL_NRT)_ [Data set]. NASA
   LANCE MODIS at MODAPS.
   https://doi.org/10.5067/FIRMS/VIIRS/VNP14IMGT_NRT.002 (accessed
   2026-11-14).

6. Giglio, L. (2024). _VIIRS/JPSS-1 Active Fires 6-Minute Level-2 Swath 375 m,
   Version 2 (VJ114IMG)_ [Data set]. NASA EOSDIS Land Processes Distributed
   Active Archive Center.
   https://doi.org/10.5067/VIIRS/VJ114IMG.002 (accessed 2026-11-14).

7. NASA VIIRS Land Science Team. (2020). _VIIRS NOAA-20 (JPSS-1) 375 m,
   I-Band Active Fire Product NRT, Version 2 (VJ114IMGTDL_NRT)_ [Data set].
   NASA LANCE MODIS at MODAPS.
   https://doi.org/10.5067/FIRMS/VIIRS/VJ114IMGT_NRT.002 (accessed
   2026-11-14).

## B. NASA imagery and 3D assets

8. NASA Global Imagery Browse Services (GIBS). (2026). _Blue Marble Shaded
   Relief and Bathymetry_ (`BlueMarble_ShadedRelief_Bathymetry`, WMTS
   EPSG:3857, GoogleMapsCompatible_Level8) [Imagery service]. NASA ESDIS.
   https://www.earthdata.nasa.gov/data/tools/gibs. Used for the "NASA
   satellite" basemap in `frontend/maplibre-map.js`.

9. Stöckli, R., Vermote, E., Saleous, N., Simmon, R., & Herring, D. (2005).
   _The Blue Marble Next Generation: A true color Earth dataset including
   seasonal dynamics from MODIS_. NASA Earth Observatory.
   https://earthobservatory.nasa.gov/features/BlueMarble. Source of the bundled
   globe texture `frontend/assets/earth.jpg` and of the GIBS layer above.

10. NASA / Lopez, C. A. _Terra_ [3D model]. NASA Science 3D Resources.
    https://science.nasa.gov/3d-resources/terra/. Bundled as
    `frontend/assets/satellites/terra.glb` for flight mode. NASA. _Suomi
    National Polar-orbiting Partnership (Suomi NPP)_ [3D model]. NASA Science
    3D Resources. https://science.nasa.gov/3d-resources/. Bundled as
    `frontend/assets/satellites/snpp.glb` after decoding its Draco geometry
    and replacing each texture by its mean colour. NASA / Lane, K. K. _Aqua (A)_
    [3D model]. NASA 3D Resources GitHub repository.
    https://github.com/nasa/NASA-3D-Resources/tree/master/3D%20Models/Aqua%20%28A%29.
    Bundled as `frontend/assets/satellites/aqua.glb` after decoding its Draco
    geometry and converting its solar-cell textures to solid colours.

## C. Boundaries and geography

11. Natural Earth. _Admin 0 countries and Admin 1 states/provinces, 1:10m and
    1:50m_ [Vector data, public domain]. https://www.naturalearthdata.com/.
    Used for land, coastlines, international borders, country labels, and
    first-level boundaries for 154 countries.

12. Bostock, M. (2019). _world-atlas_ (v2.0.2) [TopoJSON of Natural Earth
    `countries-50m`, ISC License]. https://github.com/topojson/world-atlas.

13. Runfola, D., Anderson, A., Baier, H., Crittenden, M., Dowker, E., Fuhrig,
    S., et al. (2020). geoBoundaries: A global database of political
    administrative boundaries. _PLoS ONE, 15_(4), e0231866.
    https://doi.org/10.1371/journal.pone.0231866. gbOpen ADM1 (99 countries)
    and ADM2 (131 countries) from https://www.geoboundaries.org/. The original
    sources and licenses for each country are listed under `countries` in
    `frontend/assets/geography/index.json`. They include CC BY 3.0 IGO,
    CC BY 4.0, ODbL 1.0, public domain, and national open licenses.

14. OpenStreetMap contributors. (n.d.). _OpenStreetMap_ [Data, ODbL 1.0],
    via OSM Boundaries (Wambacher). https://www.openstreetmap.org/copyright.
    This is the upstream source for the geoBoundaries ADM2 files in
    `adm2/odbl/` (e.g. RUS, KAZ, IRN, TUR, CHL, FIN, POL). Credited on the map
    as "© OpenStreetMap contributors".

## D. Third-party map services and software in the browser

15. Google. _Maps JavaScript API: Photorealistic 3D Maps_ [Map service,
    optional, API key required]. Google Maps Platform.
    https://developers.google.com/maps/documentation/javascript/3d/overview.
    The API renders Google's own attribution on the map.

16. MapLibre contributors. _MapLibre GL JS_ (v5.6.2) [Software, BSD-3-Clause].
    https://maplibre.org/. Loaded from cdn.jsdelivr.net.

17. Bostock, M. (2019). _topojson-client_ (v3.1.0) [Software, ISC License].
    https://github.com/topojson/topojson-client.

18. Google. _Noto Sans_ (Regular, Bold, Italic; MapLibre glyph PBFs) and
    _Google Sans Flex_ [Fonts, SIL Open Font License 1.1].
    https://fonts.google.com/noto.

## E. Methods

19. Giglio, L., Schroeder, W., & Justice, C. O. (2016). The Collection 6 MODIS
    active fire detection algorithm and fire products. _Remote Sensing of
    Environment, 178_, 31-41. https://doi.org/10.1016/j.rse.2016.02.054.

20. Schroeder, W., Oliva, P., Giglio, L., & Csiszar, I. A. (2014). The New
    VIIRS 375 m active fire detection data product: Algorithm description and
    initial assessment. _Remote Sensing of Environment, 143_, 85-96.
    https://doi.org/10.1016/j.rse.2013.12.008.

21. Giglio, L., Schroeder, W., Hall, J. V., & Justice, C. O. (2021). _MODIS
    Collection 6 and Collection 6.1 Active Fire Product User's Guide, Version
    1.0_. NASA Earthdata.
    https://www.earthdata.nasa.gov/s3fs-public/2023-09/MODIS_C6_C6.1_Fire_User_Guide_1.0.pdf.

22. Giglio, L., Schroeder, W., Hall, J. V., & Justice, C. O. (2025).
    _Collection 2 VIIRS 375 m Active Fire Product User's Guide, Version 1.2_.
    NASA Earthdata.
    https://www.earthdata.nasa.gov/s3fs-public/2025-06/VIIRS_C2_AF-375m_User_Guide_1.2.pdf.

23. Hirsch, R. M., Slack, J. R., & Smith, R. A. (1982). Techniques of trend
    analysis for monthly water quality data. _Water Resources Research, 18_(1),
    107-121. https://doi.org/10.1029/WR018i001p00107. Seasonal Mann-Kendall
    test in `frontend/trend.js`.

24. Sen, P. K. (1968). Estimates of the regression coefficient based on
    Kendall's tau. _Journal of the American Statistical Association, 63_(324),
    1379-1389. https://doi.org/10.1080/01621459.1968.10480934. Seasonal Sen
    slope in `frontend/trend.js`.

25. Abramowitz, M., & Stegun, I. A. (1964). _Handbook of Mathematical
    Functions_ (NBS Applied Mathematics Series 55), eq. 26.2.17. Normal CDF
    approximation in `frontend/trend.js`.

26. Uber Technologies. _H3: Hexagonal hierarchical geospatial indexing system_
    (v4.5.0) [Software, Apache 2.0]. https://h3geo.org/docs/. Resolution 4/5
    cells and exact cell areas in `firms_pipeline/merge.py`.

## F. Development resources

The backend is written in Python and uses the versions recorded in
`requirements.txt`:

- DuckDB 2.0.0 for CSV ingestion, validation, aggregation, and analytical
  queries: https://duckdb.org/docs/.
- Apache Parquet, written through DuckDB/PyArrow, for partitioned columnar
  storage: https://parquet.apache.org/docs/.
- FastAPI 0.162.7, Pydantic, and Uvicorn 0.54.0 for the read-only web API:
  https://fastapi.tiangolo.com/ and https://www.uvicorn.org/.
- pandas 3.1.1 and PyArrow 26.0.1 for tabular and Arrow/Parquet support:
  https://pandas.pydata.org/docs/ and https://arrow.apache.org/docs/.
- Requests and HTTPX for HTTP access and API testing:
  https://requests.readthedocs.io/ and https://www.python-httpx.org/.

The browser interface uses HTML, CSS, JavaScript, WebGL, and SVG, with the
map libraries and services listed in section D.

## Acknowledgements

> We acknowledge the use of data from NASA's Fire Information for Resource
> Management System (FIRMS), part of NASA's Earth Science Data and Information
> System (ESDIS), and the MODIS and VIIRS science teams that produced the
> underlying active-fire products.

> We acknowledge the use of imagery provided by services from NASA's Global
> Imagery Browse Services (GIBS), part of NASA's Earth Science Data and
> Information System (ESDIS).

> Made with Natural Earth. Boundaries from geoBoundaries (Runfola et al., 2020) and their original providers. Contains information from
> OpenStreetMap, © OpenStreetMap contributors, made available under the Open
> Database License (ODbL).
