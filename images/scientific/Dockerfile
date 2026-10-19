# Open Terminal sandbox with the Python scientific stack and spreadsheet tooling.
#
#   docker build -t ghcr.io/felix920506/open-terminal-scientific:latest images/scientific
#
# Published by .github/workflows/scientific-image.yml.
#
# Builds on the full upstream image (numpy, scipy, pandas, matplotlib, seaborn, plotly,
# scikit-learn, jupyter, openpyxl, xlrd, xlsxwriter, LibreOffice) and adds what it lacks.
# Packages already in the base are pinned to their installed versions so Open Terminal's
# own dependencies are not upgraded underneath it.
ARG BASE_IMAGE=ghcr.io/open-webui/open-terminal:latest
FROM ${BASE_IMAGE}

USER root
RUN pip freeze > /tmp/base-constraints.txt \
    && pip install --no-cache-dir --constraint /tmp/base-constraints.txt \
        # Scientific / numerical
        sympy statsmodels networkx scikit-image numba numexpr bottleneck \
        pyarrow polars xarray netCDF4 h5py tables \
        bokeh altair \
        # Spreadsheets: pandas' optional Excel engines (.xlsx/.xlsm via openpyxl, fast reads
        # via calamine, .xlsb, .ods, legacy .xls), polars' Excel reader (fastexcel), formula
        # evaluation without Excel, password-protected workbooks and table rendering.
        "pandas[excel]" python-calamine pyxlsb odfpy fastexcel \
        formulas msoffcrypto-tool tabulate \
    && rm /tmp/base-constraints.txt

USER user
