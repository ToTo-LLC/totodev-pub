# LucidSpark Export Parser

Utilities for parsing, analyzing, and rendering LucidSpark CSV exports.

## Overview

The `lucidspark/` module converts LucidSpark CSV exports into structured Python objects that can be queried programmatically or rendered into formats such as Mermaid and GraphViz DOT.

Core capabilities include:

- tolerant CSV parsing with Pydantic models
- hierarchy and grouping analysis
- graph and tree representations
- Mermaid flowchart rendering
- GraphViz DOT rendering

## Intended Public Import Path

The public package target for this library is `totodev_pub`.

```python
from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection

collection = LucidSparkShapeCollection.from_csv("path/to/export.csv")
print(collection.connective_ratio())
print(collection.tags())
```

## Example Data

See `examples/ops_projects.csv` for a synthetic sample export used by the test suite.

## Development Notes

Typical validation work for this module includes:

```bash
pytest src/totodev_pub/tests/test_lucidspark_parser.py -v
```

If you are working directly from a source snapshot rather than a finalized public package layout, adapt the test path to match the local checkout.
