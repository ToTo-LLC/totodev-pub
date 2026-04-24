# Briefing 5: Metadata & Cache Queries – Curating Context

## Summary

Briefing 4 showed how event logs capture the full timeline of a document's processing. This briefing focuses on *summaries*: small, intentional pieces of metadata that make it easy to answer “What’s important about this cached document right now?” We’ll also walk through the query tools (`files()`, `filtered_map()`, etc.) that turn the cache into a searchable catalogue, so teams can build reports, dashboards, and follow-up jobs without bolting on a database.

```text
cached_file.pdf
cached_file.pdf._slave/
  ├── events/
  │   ├── e001_ENTER_STATE@NEW.yaml
  │   └── e002_ENTER_STATE@RETRIEVE.yaml
  ├── metadata.yaml          # ← only created when you call metadata().write()
  └── artifacts/             # OCR text, analysis outputs, etc.
```

---

## Why Curate Metadata?

Every cached file already has a slave directory—from earlier briefings you’ve seen it store events, outputs, and temporary assets. The `metadata()` file in that directory is your space to stash quick answers about the document: page counts, language classification, validation flags, tags, quality scores, anything that helps downstream automation move faster.

Think of metadata as the *summary card* for the document. Events still hold the whole story, but metadata:

- Gives you fast lookups without walking event history.
- Records the “current best facts” in a place every worker can read.
- Keeps human operators and dashboards synced with the pipeline’s understanding.
- Lets developers choose what matters—no schema police, just consistent fields per project.
- Costs nothing until you use it—the `metadata.yaml` file is only written when you call `metadata().write()`, so caches that don’t need it stay lean.

The pattern works best when teams agree on a small set of fields that answer the questions they ask most often (e.g., `processing_stage`, `category`, `quality_score`, `last_validated_at`, `tags`).

---

## Metadata Basics

Access metadata through any `CachedFileRef`:

```python
from datetime import datetime

cached_ref = cache_grouping.one("client1/legal/contract.pdf")
meta = cached_ref.metadata()

# Update a few summary fields
meta.update(
    processing_stage="VALIDATION_PENDING",
    page_count=12,
    category="Contract",
    last_touched=datetime.utcnow().isoformat()
)
meta.write()

# Later…
info = cached_ref.metadata().data
if info.get("processing_stage") == "VALIDATION_PENDING":
    kick_off_validation(cached_ref)
```

Notes:

- The underlying storage is YAML (or JSON). Keys and values are whatever you need—stick to simple JSON-serializable data for portability.
- If you want stronger typing, you can wrap the metadata dict in a Pydantic model *you* define. We recommend that for critical pipelines, but it’s a standalone topic; keep it in mind if you need validation or autocompletion.
- Metadata updates are cheap—write as soon as you have new facts so readers never need to re-derive them from events.

---

## Common Metadata Fields (Pick What Helps You)

You don’t have to adopt all of these—use the ones that make downstream decisions faster:

- `processing_stage`: mirrors the latest `ENTER_STATE` event so dashboards can fetch it without scanning events.
- `final_status`: clear yes/no or enumerated result (`SUCCESS`, `FAILED_RETRYING`, …).
- `quality_score`: numeric or letter grade for ranking.
- `category` / `tags`: taxonomy for reports or routing.
- `ocr_page_count`, `language_detected`, `has_attachments`: whatever you’d otherwise recompute repeatedly.
- `human_review_required`, `reviewer_id`, `review_deadline`: coordination hooks for mixed human/automation systems.

Keep the payload lightweight—metadata should stay quick to read. If you need megabytes of context, store it as a separate artefact and reference it.

---

## Querying the Cache

Once metadata is populated, queries become straightforward. Start simple:

```python
def docs_needing_validation(cache_grouping):
    for cached_ref in cache_grouping.files():
        meta = cached_ref.metadata().data
        if meta.get("processing_stage") == "VALIDATION_PENDING":
            yield cached_ref
```

This works, but for larger caches you’ll quickly want richer tooling. That’s where `filtered_map()` comes in—it lets you filter, transform, and aggregate in one pass.

### `filtered_map()` in Plain English

- **Filters** decide which cached files to keep.
- **Mapper** builds the output you want (could be a dict, a custom object, even `None` if you’re counting).
- **Include options** (`include_metadata`, `include_slave`, etc.) control what’s pre-loaded for each entry so you don’t hit the filesystem twice.

For very small projects or ad-hoc scripts, iterating `cache_grouping.files()` (or `cached_folder.files()`) is perfectly fine—you can read metadata, run your checks, and keep whatever you need. Reach for `filtered_map()` when you want to express filters and projections declaratively, especially if you’re composing several rules or handing the results off to other tooling.

### Example: Group Documents by Category

Suppose each file’s metadata includes `category` and `processing_stage`. We want a quick “group by category” summary that lists documents waiting for validation.

```python
from collections import defaultdict
from totodev_pub.cached_file_folders_support.cache_grouping import filtered_map

def validation_queue_by_category(cache_grouping):
    # First collect the items we care about
    pending = filtered_map(
        cache_grouping,
        include_metadata=True,
        filters=[
            lambda entry: entry.metadata.data.get("processing_stage") == "VALIDATION_PENDING"
        ],
        mapper=lambda entry: {
            "category": entry.metadata.data.get("category", "Uncategorized"),
            "ref": entry.cached_ref,
            "page_count": entry.metadata.data.get("page_count")
        }
    )

    # Now group them
    grouped = defaultdict(list)
    for item in pending:
        grouped[item["category"]].append(item)
    return grouped
```

Use it like:

```python
groups = validation_queue_by_category(cache_grouping)
for category, docs in groups.items():
    total_pages = sum(doc["page_count"] or 0 for doc in docs)
    print(f"{category}: {len(docs)} docs pending ({total_pages} pages total)")
```

This pattern is approachable even if you’ve never used `itertools`: treat `filtered_map()` as “gather the items I care about” and then post-process with familiar Python tools.

### Example: Dashboard Snapshot

```python
def dashboard_rows(cache_grouping):
    return filtered_map(
        cache_grouping,
        include_metadata=True,
        mapper=lambda entry: {
            "path": str(entry.cached_ref.file_path),
            "stage": entry.metadata.data.get("processing_stage"),
            "last_error": entry.event_log.latest_values().get("ERROR_AT_STATE"),
            "quality": entry.metadata.data.get("quality_score"),
            "tags": entry.metadata.data.get("tags", [])
        }
    )
```

Feed the results straight into a template, API response, or Slack alert.

---

## Performance & Best Practices

- **Write metadata early and often.** After each major stage, update the metadata so future workers don’t redo that work.
- **Keep metadata small.** Aim for quick JSON-like snapshots. Use separate artefacts (files, event payloads) for heavy data.
- **Normalize field names.** Agree on shared keys (`processing_stage`, `category`, etc.) across teams—consistency makes cross-project tooling much easier.
- **Cache invalidation:** if you derive metadata from event history, treat events as the source of truth and metadata as a digest. When in doubt, regenerate the digest.
- **Combine with events.** Event logs remain the audit trail. Metadata complements them by providing instant answers.

---

## Key Takeaways

1. **Metadata is your summary card.** Populate it with the facts your team reaches for most—stage, quality, categories, deadlines—so nobody has to replay the entire event history.
2. **Queries become simple.** Use `filtered_map()` (or plain iteration) to build reports, validation queues, and dashboards that merge metadata with current event states.
3. **Keep it intentional.** Lightweight snapshots plus consistent fields let multiple workers, services, and humans stay in sync with minimal filesystem work.

Next up, we’ll look at how to build richer cache insights—rolling up metrics across folders, automating clean-up, and surfacing trends that make large deployments manageable.
