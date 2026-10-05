"""Harvest and structure the PubMed literature on the exposome in routinely collected health data.

The package is deliberately stdlib-only apart from PyYAML, which is used solely
to read the human-curated vocabulary files under ``config/``.
"""

__version__ = "0.1.0"

USER_AGENT = (
    "exposome-ehr/{v} "
    "(+https://github.com/jeonglab-bcm/exposome-ehr-review; research use)"
).format(v=__version__)
