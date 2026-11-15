"""Language extractors. Add a module here and register it in ``load_all`` to support a new language."""


def load_all() -> None:
    from ..extractor import register
    from .python import PythonExtractor
    from .typescript import TSExtractor

    register("python", PythonExtractor)
    register("typescript", lambda: TSExtractor("typescript"))
    register("tsx", lambda: TSExtractor("tsx"))
    register("javascript", lambda: TSExtractor("javascript"))
