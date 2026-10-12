"""An active flag cannot authorize a version different from the active pointer."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.application.index_registry import IndexRegistry, IndexRegistryError


@pytest.mark.parametrize("pointer", [None, "current-version"])
def test_explicit_version_requires_the_document_active_pointer(pointer):
    versions = Mock()
    versions.get.return_value = SimpleNamespace(
        index_version="stale-version", document_id="doc", status="active"
    )
    documents = Mock()
    documents.get.return_value = SimpleNamespace(status="available", active_index_version=pointer)
    registry = IndexRegistry(documents=documents, versions=versions, chunks=Mock())
    with pytest.raises(IndexRegistryError, match="index_version_not_active"):
        registry.resolve_explicit_version("stale-version", user_id="owner")
    documents.get.assert_called_once_with("doc", user_id="owner")
