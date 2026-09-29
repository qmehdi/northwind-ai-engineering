"""The serving tests reuse the trained fixtures of Projects 1 and 2: a real triage artifact and
a tiny exported semantic artifact, both built once per session."""

from tests.session02.conftest import ticket_file, ticket_rows, trained  # noqa: F401
from tests.session03.conftest import (  # noqa: F401
    config,
    exported_tiny,
    s3_file,
    s3_rows,
    spec,
    tiny_tokenizer,
    trained_tiny,
)
