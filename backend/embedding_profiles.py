"""Immutable embedding prompt regimes. No model loading or implicit upgrades."""
from dataclasses import asdict, dataclass
import hashlib
import json


def _digest(value):
    # PostgreSQL jsonb array text uses these separators and literal Unicode too.
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class EmbeddingProfile:
    model_id: str
    revision: str
    dimension: int
    query_prefix: str = ''
    document_prefix: str = ''
    normalization: str = 'l2'

    def __post_init__(self):
        if not isinstance(self.dimension, int) or isinstance(self.dimension, bool) or self.dimension <= 0:
            raise ValueError('dimension must be a positive integer matching the existing vector columns')
        if not self.model_id or not self.revision:
            raise ValueError('model_id and immutable revision are required; do not guess legacy identity')
        if self.normalization not in {'l2', 'none'}:
            raise ValueError('normalization must be l2 or none')
        for value in (self.model_id, self.revision, self.query_prefix, self.document_prefix):
            if not isinstance(value, str) or '\x00' in value:
                raise ValueError('profile text fields must be strings without NUL')

    @property
    def fingerprint(self):
        return _digest([self.model_id, self.revision, self.dimension, self.query_prefix,
                        self.document_prefix, self.normalization])

    def cache_key(self, kind, text):
        if kind not in {'query', 'document'}:
            raise ValueError('kind must be query or document')
        return _digest([self.fingerprint, kind, text])

    def to_dict(self):
        return asdict(self)


def resolve_profile(model_id, dimension, *, revision, query_prefix=None, document_prefix=None):
    known = model_id == 'google/embeddinggemma-300m'
    return EmbeddingProfile(model_id, revision, dimension,
                            ('task: search result | query: ' if known else '') if query_prefix is None else query_prefix,
                            ('title: none | text: ' if known else '') if document_prefix is None else document_prefix)


def validate_transition(source, target):
    if any(getattr(source, field) != getattr(target, field)
           for field in ('model_id', 'revision', 'dimension', 'normalization')):
        raise ValueError('v0.3 supports same-model prompt migration only (same revision, normalization and dimension); '
                         'retain the current profile or use a separately designed migration')
