# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
LucidSpark Sparkboard Export Interpreter

## Usage Example

```python
from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection

# Load CSV
collection = LucidSparkShapeCollection.from_csv("export.csv")

# Analyze
print(f"Connective ratio: {collection.connective_ratio()}")
print(f"Container membership ratio: {collection.container_membership_ratio()}")
print(f"Has groups: {collection.has_groups()}")
print(f"Tags: {collection.tags()}")
print(f"Number of leaves: {len(collection.leaves())}")

# Render to Mermaid
mermaid_code = collection.to_mermaid(direction="LR", wrap_width=40)
print(mermaid_code)

# Render to GraphViz DOT
dot_code = collection.to_graphviz_dot()
print(dot_code)

# Access individual shapes
for shape_id, shape in collection._shapes.items():
    print(f"{shape_id}: {shape.text_area_1} (type: {shape.type()})")

# Work with leaf records
for leaf in collection.leaves():
    print(f"Leaf: {leaf.text_area_1}")
    print(f"  Parents: {[p.text_area_1 for p in leaf.parent_containers]}")
```

---

## MODULE OVERVIEW

This module provides classes and functions to interpret and transform CSV exports from 
LucidSpark collaborative whiteboards into structured data formats suitable for 
programmatic use and LLM consumption.

### Purpose

LucidSpark is an online collaborative diagramming tool that allows users to create 
visual diagrams using shapes, containers (frames), and connecting lines. Users can 
export their boards as CSV files, which contain structured data about all elements 
but lack sufficient information to directly recreate the visual graph.

This module bridges that gap by:
- Parsing the CSV export into structured data models
- Analyzing and exposing the hierarchical and graph relationships
- Rendering the data into various output formats (Mermaid diagrams, YAML, JSON)

### Common Usage Patterns at Our Company

- **Container-based organization**: Creating frames/containers as visual groupings, 
  then placing shapes inside them to show membership (e.g., a "Future Features" 
  container holding multiple feature shapes)
- **Connected graphs**: Less common, but lines are sometimes used to connect shapes, 
  creating directed graphs showing relationships or workflows


## INPUT FORMAT SPECIFICATION

### CSV Export Structure

LucidSpark exports contain the following columns:

#### Complete Column List
- `Id` - Unique identifier for each shape/element
- `Name` - Indicates the **type of shape** (e.g., "LucidCardBlock", "SparkContainerBlock", "Line")
- `Shape Library` - Library the shape comes from
- `Page ID` - Page containing the shape
- `Contained By` - ID of container shape (enables nesting)
- `Group` - Manual grouping identifier
- `Line Source` - For lines: ID of source shape
- `Line Destination` - For lines: ID of destination shape
- `Source Arrow` - Arrow style at source end
- `Destination Arrow` - Arrow style at destination end
- `Tags` - Comma-separated tags for categorization
- `Status` - Status indicator
- `Text Area 1` - Primary text content (main label)
- `Text Area 2` - Secondary text content
- `Comments` - User comments (often JSON-encoded)
- `Assignee` - Assigned user(s)
- `Description` - Longer description text
- `End Date` - End date for tasks
- `Estimate` - Effort estimate
- `Start Date` - Start date for tasks
- `T-shirt size` - Size estimate (S/M/L/XL)
- `Title` - Alternative title field

#### Key Columns for Interpretation

The following columns are most critical for understanding structure:

- **`Id`** - Unique identifier (key field)
- **`Name`** - Type of shape/element. Broadly: shapes, containers, or lines
- **`Contained By`** - Enables multi-level nesting hierarchy
- **`Group`** - Manual grouping (note: groups don't have IDs or Text Area 1)
- **`Tags`** - Additional categorization mechanism
- **`Text Area 1`** - Primary display text (what humans see as the "name")
- **`Text Area 2`** - Secondary text (rare for containers)
- **`Line Source`** - Populated only for line-type records
- **`Line Destination`** - Populated only for line-type records

#### Shape Classification Rules

Elements fall into three categories:

1. **Lines**: Records with `Line Source` populated
2. **Containers**: Records with "Frame", "Container", or "Table" in the `Name` field
3. **Shapes**: All other non-line records (leaf nodes)

#### Human vs. Programmatic Perspective

When viewing LucidSpark boards, humans don't see IDs. They identify shapes by the 
content of `Text Area 1`. This module should use `Text Area 1` as the human-readable 
name/label while using `Id` for programmatic identification.


## DATA STRUCTURES AND CLASSES

### Core Classes

#### `LucidSparkShapeRecord`

Represents a single row from the CSV export.

**Type**: Pydantic v2 `BaseModel` with tolerant validation

**Features**:
- Keyed by `Id`
- Hashable based on `Id`
- Tolerant validation rules (handle missing/malformed data gracefully)

**Methods**:
- `type()` -> `str`
  - Returns: `"shape"`, `"container"`, or `"line"`
  - Classification logic:
    - `"line"`: `Line Source` column is populated
    - `"container"`: `Name` contains "Frame", "Container", or "Table"
    - `"shape"`: Everything else

**Data Integrity**:
- Duplicate IDs will raise an error (not allowed)
- Collections are always built from complete CSV (no incremental updates in this version)


#### `LucidSparkLeafRecord`

Represents a leaf node (non-container, non-line shape).

**Type**: Subclass of `LucidSparkShapeRecord`

**Additional Attributes**:
- `parent_containers: List[LucidSparkShapeRecord]`
  - Ordered list of ancestor containers
  - First element = top-level container
  - Last element = immediate parent
  - Computed eagerly at construction time

**Design Rationale**:
- Separate class (not a method) because leaf records are not serializable due to 
  containing links to parent container objects


#### `LucidSparkShapeCollection`

Represents the complete collection of shapes with query and traversal methods.

**Type**: Collection class keyed by `Id`

**Attributes**:
- `groups` - Unnamed groupings of shapes
  - Note: `Contained By` relationships take precedence over `Group` membership in tree 
    representations
  - Group membership only manifests as a subgraph when shapes share the same container
- `graph` - NetworkX `DiGraph` or `MultiDiGraph` object
  - Use `DiGraph` by default
  - Use `MultiDiGraph` if multiple edges exist between same node pair
- `tree` - Hierarchical representation using treelib (or similar)
  - Represents container/contained-by relationships
  - Leaf nodes are end shapes (non-containers)
  - Auto-detects root(s): containers with no `Contained By` relationship
  - Returns multiple roots as a list if present
- Note: The `Page ID` attribute is ignored in this version (meaning unclear)

**Methods**:

- `connective_ratio()` -> `float`
  - Returns the ratio of leaf nodes that are source or destination of a line to the 
    total number of leaf nodes (expressed as float between 0.0 and 1.0)
  - Only considers leaf nodes (non-containers, non-lines)
  - Use case: Detect if board represents a connected graph

- `container_membership_ratio()` -> `float`
  - Returns percentage of non-line shapes that are containers or contained by another shape
  - Use case: Detect if board represents a tree/hierarchy

- `has_groups()` -> `bool`
  - Returns `True` if any shapes use the `Group` field
  - Use case: Detect grouping-based organization

- `tags()` -> `List[str]`
  - Returns list of all unique tags in the collection
  - Returns empty list if no tags present

- `leaves()` -> `List[LucidSparkLeafRecord]`
  - Returns all leaf records (terminal shapes, non-containers, non-lines)
  - Orphaned shapes (not in any container, not part of line connections) are valid 
    and included


## OUTPUT FORMATS AND RENDERERS

### Mermaid Flowchart Renderer

#### Standard Flowchart Mode

Renders the LucidSpark data as a Mermaid flowchart diagram.

**Mapping Rules**:

- **Containers** → Mermaid subgraphs (nested if applicable)
- **Shapes** → Mermaid nodes
- **Lines** → Mermaid edges
- **Grouped leaf records** with same parent → Visual grouping in subgraph

**Node Labeling**:
- Use `Text Area 1` as the node label (ignore `Text Area 2`)
- Escape special characters that might break Mermaid syntax
- Wrap labels in quotation marks by default
- Text wrapping: Controlled by `wrap_width` parameter (default: 50 characters)
  - Only wrap on spaces (inject `<br>` tag for line breaks)

**Node Styling by Tags**:
- Distinct tag combinations → Unique background colors
- Use deterministic color assignment algorithm (same input → same output)
- If no tags present → No background color applied
- Color override NOT supported in this version (future feature)

**Node Shapes by Type**:

Default mappings based on the `Name` field:
- `"Sticky note"` → Rounded rectangle
- `"LucidCardBlock"` → Rectangle
- `"User Image"` → Document shape

For other shape types:
- Dynamically select Mermaid node shape based on `Name`
- Use deterministic algorithm (same input → same output)

**Renderer Parameters**:
- `direction: str` - Diagram direction (default: `"TB"` for top-to-bottom)
  - Supports Mermaid direction specifications: TB, LR, RL, BT
- `wrap_width: int` - Maximum characters per line before wrapping (default: 50)

**Legend Generation**:
- Generate a "Color-Tag Legend" subgraph showing tag-to-color mappings
- Included automatically in the output

**Requirements**:
- Deterministic output (same CSV always produces identical Mermaid code)
- Support for nested subgraphs
- Render everything (no filtering in this version; subtree rendering is future feature)


### GraphViz DOT Renderer

Provides export to GraphViz DOT format for alternative visualization.

**Implementation**:
- Simple, straightforward implementation (low priority feature)
- Basic node and edge representation
- Minimal styling

**Method**: `graphviz_dot()` on renderer or collection


### Future Output Formats

The module should be designed to support additional output formats over time:
- **YAML**: Hierarchical representation emphasizing structure
- **JSON**: Programmatic access to structured data


## OUT OF CURRENT SCOPE

The following features are explicitly excluded from this version but may be considered 
for future releases:

### Data Model Features Not Included

- **Incremental updates**: Collections must be built from complete CSV files; partial 
  updates are not supported
- **Page objects**: The `Page ID` field is ignored as its meaning is unclear
- **Lazy computation**: Parent containers are computed eagerly, not on-demand
- **Detailed CSV validation**: Parsing errors propagate naturally with line number 
  enhancement when possible, but detailed validation messages are not provided

### Collection and Analysis Features Not Included

- **Summary statistics**: No automatic generation of shape type counts, nesting depth, 
  or connectivity metrics
- **Orphan detection**: Orphaned shapes are valid; no special detection or handling

### Rendering Features Not Included

- **Color palette override**: Tag-to-color assignments are deterministic and 
  not customizable
- **Subtree/subgraph rendering**: Must render entire board; cannot filter to specific 
  containers or subtrees
- **Filtering by attributes**: Cannot filter collection by tags, status, assignee, 
  or other attributes before rendering
- **Individual container exports**: Cannot export containers as separate diagrams
- **Direction auto-detection**: Diagram direction must be explicitly specified

### Data Processing Features Not Included

- **Multi-file merging**: Cannot merge multiple CSV exports from different pages or boards
- **Version comparison**: No utilities to compare or diff two versions of the same 
  board export
- **Comments field parsing**: The `Comments` field (which may contain JSON) is not 
  parsed into structured data
- **Text Area 2 rendering**: Only `Text Area 1` is shown in visualizations

"""