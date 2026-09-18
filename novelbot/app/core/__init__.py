"""Core conversion engine: name pools, mapping registry and the replacer."""

from .names import NameRegistry, build_mapping, checksum, load_pools
from .discovery import augment_mapping, extract_candidates
from .converter import ConversionResult, NameConverter

__all__ = [
    "NameRegistry",
    "build_mapping",
    "load_pools",
    "checksum",
    "NameConverter",
    "ConversionResult",
    "augment_mapping",
    "extract_candidates",
]
