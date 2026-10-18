"""Every text a person reads, in English (en.py) and Korean (ko.py). config.LANGUAGE picks the catalog at call time.

Both catalogs expose the same keys (tests/test_i18n.py). Values are `str.format` templates: placeholders are named,
and a literal brace is written `{{` / `}}`.
"""

from agentic import config
from agentic.i18n import en, ko

CATALOGS = {"en": en.MESSAGES, "ko": ko.MESSAGES}
LANGUAGES = tuple(CATALOGS)


def t(key: str, **params: object) -> str:
    """The text `key` in the configured language, with `params` filled in."""
    return CATALOGS[config.LANGUAGE][key].format_map(params)
