import textwrap

from sidecar.axis import PythonAxisExtractor
from sidecar.axis.container_kind import ContainerKindClassifier, NullGraphProbe


class _MarkerProbe(NullGraphProbe):
    def __init__(self, marker_kinds: set[str]) -> None:
        self._marker_kinds = set(marker_kinds)

    def library_marker_kinds(self, symbol_uid: str) -> set[str]:
        return set(self._marker_kinds)


def _profile(source: str, qualified_name: str):
    extraction = PythonAxisExtractor().extract(textwrap.dedent(source), "pkg/tasks.py")
    return extraction.profiles_by_qualified_name[qualified_name]


def _kinds(profile, probe=None) -> set[str]:
    classifier = ContainerKindClassifier(probe or NullGraphProbe())
    return {match.kind for match in classifier.classify(profile)}


def _matches(profile, probe=None):
    classifier = ContainerKindClassifier(probe or NullGraphProbe())
    return classifier.classify(profile)


def test_real_extractor_profiles_classify_data_model_and_config_carrier():
    model = _profile(
        """
        class Model:
            id: int
            name: str

        class Settings:
            host: str = "localhost"
            port: int = 8080
        """,
        "pkg.tasks.Model",
    )
    settings = _profile(
        """
        class Model:
            id: int
            name: str

        class Settings:
            host: str = "localhost"
            port: int = 8080
        """,
        "pkg.tasks.Settings",
    )

    assert _kinds(model) == {"data_model"}
    assert _kinds(settings) == {"config_carrier", "data_model"}

    config_match = next(match for match in _matches(settings) if match.kind == "config_carrier")
    assert config_match.payload["annotated_default_count"] == 2


def test_real_extractor_profile_classifies_metadata_carrier_key_identity():
    profile = _profile(
        """
        def configure(meta):
            meta["owner"] = "user"
            return meta["owner"]
        """,
        "pkg.tasks.configure",
    )

    assert "metadata_carrier" in _kinds(profile)
    metadata_match = next(match for match in _matches(profile) if match.kind == "metadata_carrier")
    assert metadata_match.payload["shared_keys"] == ["'owner'"]


def test_real_extractor_profile_classifies_middleware_chain_shape():
    profile = _profile(
        """
        def install(callbacks):
            def handler():
                pass

            callbacks.append(handler)
            for cb in callbacks:
                cb()
        """,
        "pkg.tasks.install",
    )

    assert _kinds(profile) == {"middleware_chain"}


def test_real_extractor_profile_classifies_di_container_from_call_default():
    profile = _profile(
        """
        def get_db():
            pass

        def endpoint(db=Depends(get_db)):
            return db
        """,
        "pkg.tasks.endpoint",
    )

    assert _kinds(profile) == {"di_container"}
    match = next(match for match in _matches(profile) if match.kind == "di_container")
    assert match.payload["call_default_parameters"] == ["db"]


def test_route_like_callable_table_stays_unclassified_without_marker():
    profile = _profile(
        """
        def build(table):
            def handler():
                pass

            table["/users"] = handler
            table["/items"] = handler
        """,
        "pkg.tasks.build",
    )

    assert "web_route_register" not in _kinds(profile)
    assert "web_route_register" in _kinds(profile, _MarkerProbe({"web_route_register"}))


def test_signal_shape_stays_middleware_chain_without_signal_marker():
    profile = _profile(
        """
        def install(callbacks):
            def receiver():
                pass

            callbacks.append(receiver)
            for callback in callbacks:
                callback()
        """,
        "pkg.tasks.install",
    )

    assert _kinds(profile) == {"middleware_chain"}
    assert {"middleware_chain", "signal_register"} <= _kinds(
        profile,
        _MarkerProbe({"signal_register"}),
    )
