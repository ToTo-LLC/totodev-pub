# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
LucidSpark Collection - Shape Management and Analysis

## Purpose

This module provides the `LucidSparkShapeCollection` class, which manages collections of
LucidSpark shapes and provides powerful analysis and rendering capabilities. It serves as
the primary entry point for working with LucidSpark CSV exports, offering methods to load,
query, analyze, and render board data in various formats.

## About LucidSpark Exports

LucidSpark is a collaborative online whiteboard tool where teams create visual diagrams.
When exported to CSV, a LucidSpark board contains rows representing:

- **Shapes**: Individual elements (cards, notes, etc.)
- **Containers**: Frames and grouping structures that contain other shapes
- **Lines**: Connections between shapes showing relationships

The exported CSV includes columns like "Id", "Name", "Text Area 1", "Contained By",
"Line Source", "Line Destination", and "Tags", among others.

## Module Organization

This module is part of a three-file architecture:

- **lucidspark_models.py**: Primitive, serializable data models (`LucidSparkShapeRecord`)
- **lucidspark_collection.py** (this file): Collection management and analysis
- **mermaid_renderer.py**: Mermaid flowchart diagram generation (imported by this module)

## Key Components

### LucidSparkLeafRecord
A specialized wrapper for non-container, non-line shapes that includes their complete
parent container hierarchy. Not serializable due to object references.

### LucidSparkShapeCollection
The main collection class providing:
- CSV loading via `from_csv()` class method
- Analysis methods: `connective_ratio()`, `container_membership_ratio()`, `tags()`, etc.
- Graph representation: NetworkX `DiGraph` or `MultiDiGraph`
- Tree representation: treelib `Tree` for container hierarchies
- Rendering: `to_mermaid()` and `to_graphviz_dot()`

## Usage Examples

**Note:** The code samples below are verified by corresponding test cases in 
`test_lucidspark_parser.py::TestModuleDocstringExamples`.

### Basic Loading and Analysis

```python
from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection

# Load a CSV export
collection = LucidSparkShapeCollection.from_csv("board_export.csv")

# Analyze the board structure
print(f"Total shapes: {len(collection._shapes)}")
print(f"Connective ratio: {collection.connective_ratio():.2%}")
print(f"Container membership: {collection.container_membership_ratio():.2%}")
print(f"Has groups: {collection.has_groups()}")
print(f"Tags: {collection.tags()}")
```

### Working with Leaf Nodes

```python
# Get all leaf nodes with their parent hierarchies
leaves = collection.leaves()

for leaf in leaves:
    print(f"Leaf: {leaf.text_area_1}")
    if leaf.parent_containers:
        parent_names = [p.text_area_1 for p in leaf.parent_containers]
        print(f"  Parents: {' → '.join(parent_names)}")
```

### Graph and Tree Analysis

```python
# Access NetworkX graph for connectivity analysis
import networkx as nx
graph = collection.graph
print(f"Graph type: {type(graph).__name__}")
print(f"Nodes: {graph.number_of_nodes()}")
print(f"Edges: {graph.number_of_edges()}")

# Check if graph is strongly connected
if isinstance(graph, nx.DiGraph):
    connected = nx.is_strongly_connected(graph)
    print(f"Strongly connected: {connected}")

# Access treelib tree for hierarchy visualization
tree = collection.tree
tree.show()  # Print ASCII tree structure
```

### Rendering to Mermaid

```python
# Generate Mermaid flowchart
mermaid_code = collection.to_mermaid(
    direction="LR",      # Left-to-right layout
    wrap_width=40        # Wrap text at 40 characters
)

# Save to file
with open("diagram.mmd", "w") as f:
    f.write(mermaid_code)

# Or use in markdown
print(f"```mermaid\\n{mermaid_code}\\n```")
```

### Rendering to GraphViz DOT

```python
# Generate GraphViz DOT format
dot_code = collection.to_graphviz_dot()

# Save to file
with open("diagram.dot", "w") as f:
    f.write(dot_code)

# Convert to PNG using GraphViz (if installed)
import subprocess
subprocess.run(["dot", "-Tpng", "diagram.dot", "-o", "diagram.png"])
```

### Working with Groups

```python
# Access grouped shapes
groups = collection.groups

for group_id, shapes_in_group in groups.items():
    print(f"Group {group_id}:")
    for shape in shapes_in_group:
        print(f"  - {shape.text_area_1}")
```

### Advanced: Filtering and Custom Analysis

```python
# Get shapes by type
containers = [s for s in collection._shapes.values() if s.type() == "container"]
shapes = [s for s in collection._shapes.values() if s.type() == "shape"]
lines = [s for s in collection._shapes.values() if s.type() == "line"]

print(f"Containers: {len(containers)}")
print(f"Shapes: {len(shapes)}")
print(f"Lines: {len(lines)}")

# Find shapes with specific tags
tagged_shapes = [
    s for s in collection._shapes.values()
    if s.tags and "IntegrationPartner" in s.tags
]

# Calculate custom metrics
orphaned_shapes = [
    s for s in collection._shapes.values()
    if s.type() == "shape" and not s.contained_by
]
print(f"Orphaned shapes: {len(orphaned_shapes)}")
```

## Design Rationale

### Why Separate from Models?
The collection layer adds non-serializable functionality (NetworkX graphs, treelib trees)
and complex analysis methods that don't belong in primitive data models.

### Why Caching?
Graph, tree, groups, and leaves are computed lazily and cached because they're expensive
to build. Once computed, they're reused across multiple queries.

### Why Leaf Records?
Leaf records bundle shapes with their complete parent hierarchy, computed eagerly at
construction. This avoids repeated traversal and provides a convenient API for working
with the most common use case: analyzing end-node shapes.
"""

import csv
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional, Union

import networkx as nx
from treelib import Tree

from .lucidspark_models import LucidSparkShapeRecord


class LucidSparkLeafRecord:
    """
    Represents a leaf node (non-container, non-line shape) with its parent container hierarchy.
    
    This is NOT a Pydantic model because it contains non-serializable references to
    parent container objects.
    """
    
    def __init__(self, shape: LucidSparkShapeRecord, parent_containers: List[LucidSparkShapeRecord]):
        """
        Initialize a leaf record with its parent container hierarchy.
        
        Args:
            shape: The shape record
            parent_containers: Ordered list of ancestor containers (top-level first, immediate parent last)
        """
        self.shape = shape
        self.parent_containers = parent_containers
    
    def __getattr__(self, name):
        """Delegate attribute access to the underlying shape record."""
        return getattr(self.shape, name)
    
    def __hash__(self):
        """Hash based on shape ID."""
        return hash(self.shape.id)
    
    def __eq__(self, other):
        """Equality based on shape ID."""
        if not isinstance(other, LucidSparkLeafRecord):
            return False
        return self.shape.id == other.shape.id


class LucidSparkShapeCollection:
    """
    Represents a collection of LucidSpark shapes with query and traversal methods.
    
    Provides analysis capabilities and rendering to various output formats.
    """
    
    def __init__(self, shapes: Dict[str, LucidSparkShapeRecord]):
        """
        Initialize the collection with a dictionary of shapes keyed by ID.
        
        Args:
            shapes: Dictionary mapping shape IDs to LucidSparkShapeRecord instances
            
        Raises:
            ValueError: If duplicate IDs are detected
        """
        # Check for duplicates (shouldn't happen if dict is used, but validates input)
        if len(shapes) != len(set(shapes.keys())):
            raise ValueError("Duplicate IDs detected in shape collection")
        
        self._shapes = shapes
        self._groups_cache: Optional[Dict[str, List[LucidSparkShapeRecord]]] = None
        self._graph_cache: Optional[Union[nx.DiGraph, nx.MultiDiGraph]] = None
        self._tree_cache: Optional[Tree] = None
        self._leaves_cache: Optional[List[LucidSparkLeafRecord]] = None
    
    @classmethod
    def from_csv(cls, file_path: Union[str, Path]) -> "LucidSparkShapeCollection":
        """
        Load a LucidSpark CSV export file and create a collection.
        
        Args:
            file_path: Path to the CSV file
            
        Returns:
            A new LucidSparkShapeCollection instance
            
        Raises:
            ValueError: If duplicate IDs are found or parsing fails
        """
        file_path = Path(file_path)
        shapes = {}
        
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row_num, row in enumerate(reader, start=2):  # Start at 2 (1 is header)
                try:
                    # Create shape record using aliases
                    shape = LucidSparkShapeRecord(**row)
                    
                    # Check for duplicate IDs
                    if shape.id in shapes:
                        raise ValueError(
                            f"Duplicate ID '{shape.id}' found at row {row_num}"
                        )
                    
                    shapes[shape.id] = shape
                    
                except Exception as e:
                    raise ValueError(f"Error parsing row {row_num}: {str(e)}") from e
        
        return cls(shapes)
    
    @property
    def groups(self) -> Dict[str, List[LucidSparkShapeRecord]]:
        """
        Get unnamed groupings of shapes organized by group ID.
        
        Returns:
            Dictionary mapping group IDs to lists of shapes in that group
        """
        if self._groups_cache is None:
            self._groups_cache = {}
            for shape in self._shapes.values():
                if shape.group:
                    if shape.group not in self._groups_cache:
                        self._groups_cache[shape.group] = []
                    self._groups_cache[shape.group].append(shape)
        
        return self._groups_cache
    
    @property
    def graph(self) -> Union[nx.DiGraph, nx.MultiDiGraph]:
        """
        Get a NetworkX graph representation of the shapes and their connections.
        
        Returns DiGraph by default, MultiDiGraph if multiple edges exist between
        the same pair of nodes.
        
        Returns:
            NetworkX DiGraph or MultiDiGraph
        """
        if self._graph_cache is None:
            # First, check if we need a MultiDiGraph
            edge_counts: Dict[tuple, int] = {}
            for shape in self._shapes.values():
                if shape.type() == "line" and shape.line_source and shape.line_destination:
                    edge_key = (shape.line_source, shape.line_destination)
                    edge_counts[edge_key] = edge_counts.get(edge_key, 0) + 1
            
            # Use MultiDiGraph if any edge pair has more than one connection
            use_multi = any(count > 1 for count in edge_counts.values())
            
            # Create the appropriate graph type
            if use_multi:
                g = nx.MultiDiGraph()
            else:
                g = nx.DiGraph()
            
            # Add all non-line shapes as nodes
            for shape in self._shapes.values():
                if shape.type() != "line":
                    g.add_node(shape.id, shape=shape)
            
            # Add edges from line shapes
            for shape in self._shapes.values():
                if shape.type() == "line" and shape.line_source and shape.line_destination:
                    g.add_edge(shape.line_source, shape.line_destination, line=shape)
            
            self._graph_cache = g
        
        return self._graph_cache
    
    @property
    def tree(self) -> Tree:
        """
        Get a treelib Tree representation of the container/contained-by hierarchy.
        
        Auto-detects root containers (those with no 'Contained By' relationship).
        If multiple roots exist, they are all included in the tree.
        
        Returns:
            treelib Tree object
        """
        if self._tree_cache is None:
            tree = Tree()
            
            # Find all containers and non-line shapes
            containers = [s for s in self._shapes.values() if s.type() == "container"]
            all_non_lines = [s for s in self._shapes.values() if s.type() != "line"]
            
            # Find roots (containers with no parent)
            roots = [c for c in containers if not c.contained_by]
            
            # If no containers exist, create a virtual root
            if not containers:
                tree.create_node("Root", "root")
                for shape in all_non_lines:
                    tree.create_node(
                        shape.text_area_1 or shape.id,
                        shape.id,
                        parent="root",
                        data=shape
                    )
            else:
                # Create virtual root if multiple roots exist
                if len(roots) > 1:
                    tree.create_node("Root", "root")
                    root_parent = "root"
                else:
                    root_parent = None
                
                # Add all roots
                for root in roots:
                    tree.create_node(
                        root.text_area_1 or root.id,
                        root.id,
                        parent=root_parent,
                        data=root
                    )
                
                # Build the tree recursively
                added = set(r.id for r in roots)
                if root_parent:
                    added.add(root_parent)
                
                # Keep adding nodes until no more can be added
                max_iterations = len(all_non_lines) + 10
                iteration = 0
                while iteration < max_iterations:
                    iteration += 1
                    added_this_round = False
                    
                    for shape in all_non_lines:
                        if shape.id in added:
                            continue
                        
                        # Add if parent exists or if no parent (orphan)
                        parent_id = shape.contained_by if shape.contained_by else (root_parent or roots[0].id if roots else "root")
                        
                        if not shape.contained_by or parent_id in added:
                            try:
                                tree.create_node(
                                    shape.text_area_1 or shape.id,
                                    shape.id,
                                    parent=parent_id,
                                    data=shape
                                )
                                added.add(shape.id)
                                added_this_round = True
                            except:
                                # Parent doesn't exist yet, will try again
                                pass
                    
                    if not added_this_round:
                        break
            
            self._tree_cache = tree
        
        return self._tree_cache
    
    def connective_ratio(self) -> float:
        """
        Calculate the ratio of leaf nodes that are connected by lines.
        
        Returns the ratio of leaf nodes that are source or destination of a line
        to the total number of leaf nodes (expressed as float between 0.0 and 1.0).
        
        Returns:
            Float between 0.0 and 1.0
        """
        leaf_shapes = [s for s in self._shapes.values() if s.type() == "shape"]
        
        if not leaf_shapes:
            return 0.0
        
        # Find leaf IDs that are in connections
        connected_ids = set()
        for shape in self._shapes.values():
            if shape.type() == "line":
                if shape.line_source:
                    connected_ids.add(shape.line_source)
                if shape.line_destination:
                    connected_ids.add(shape.line_destination)
        
        # Count how many leaves are connected
        connected_leaves = sum(1 for s in leaf_shapes if s.id in connected_ids)
        
        return connected_leaves / len(leaf_shapes)
    
    def container_membership_ratio(self) -> float:
        """
        Calculate the ratio of non-line shapes that are containers or contained by another shape.
        
        Returns:
            Float between 0.0 and 1.0
        """
        non_line_shapes = [s for s in self._shapes.values() if s.type() != "line"]
        
        if not non_line_shapes:
            return 0.0
        
        # Count shapes that are containers or are contained
        member_count = sum(
            1 for s in non_line_shapes
            if s.type() == "container" or s.contained_by
        )
        
        return member_count / len(non_line_shapes)
    
    def has_groups(self) -> bool:
        """
        Check if any shapes use the Group field.
        
        Returns:
            True if any shape has a non-empty group field
        """
        return any(s.group for s in self._shapes.values())
    
    def tags(self) -> List[str]:
        """
        Get a list of all unique tags in the collection.
        
        Parses comma-separated or pipe-separated tag lists.
        
        Returns:
            Sorted list of unique tags, empty list if no tags present
        """
        all_tags = set()
        
        for shape in self._shapes.values():
            if shape.tags:
                # Split on both | and , to handle different formats
                tag_list = re.split(r'[|,]', shape.tags)
                for tag in tag_list:
                    tag = tag.strip()
                    if tag:
                        all_tags.add(tag)
        
        return sorted(all_tags)
    
    def leaves(self) -> List[LucidSparkLeafRecord]:
        """
        Get all leaf records (terminal shapes, non-containers, non-lines).
        
        Includes orphaned shapes (not in any container, not part of line connections).
        Each leaf record includes its parent container hierarchy.
        
        Returns:
            List of LucidSparkLeafRecord instances
        """
        if self._leaves_cache is None:
            self._leaves_cache = []
            
            # Get all leaf shapes
            leaf_shapes = [s for s in self._shapes.values() if s.type() == "shape"]
            
            # For each leaf, compute parent container hierarchy
            for leaf in leaf_shapes:
                parent_containers = self._get_parent_containers(leaf)
                leaf_record = LucidSparkLeafRecord(leaf, parent_containers)
                self._leaves_cache.append(leaf_record)
        
        return self._leaves_cache
    
    def _get_parent_containers(self, shape: LucidSparkShapeRecord) -> List[LucidSparkShapeRecord]:
        """Build parent container hierarchy by traversing up the contained_by chain."""
        parent_chain = []
        current_shape = shape
        
        # Walk up the containment hierarchy until we reach a root or missing parent
        while current_shape.contained_by:
            parent_shape_id = current_shape.contained_by
            
            # Stop if parent doesn't exist in collection
            if parent_shape_id not in self._shapes:
                break
            
            parent_shape = self._shapes[parent_shape_id]
            # Insert at start to maintain top-level-first ordering
            parent_chain.insert(0, parent_shape)
            current_shape = parent_shape
        
        return parent_chain
    
    def to_mermaid(self, direction: str = "TB", wrap_width: int = 50) -> str:
        """
        Render the collection as a Mermaid flowchart diagram.
        
        Args:
            direction: Diagram direction (TB, LR, RL, BT). Default: TB
            wrap_width: Maximum characters per line before wrapping. Default: 50
            
        Returns:
            Mermaid flowchart code as a string
        """
        from .mermaid_renderer import MermaidRenderer
        
        renderer = MermaidRenderer(self, direction=direction, wrap_width=wrap_width)
        return renderer.render()
    
    def to_graphviz_dot(self) -> str:
        """
        Render the collection as a GraphViz DOT diagram.
        
        Simple implementation with basic node and edge representation.
        
        Returns:
            GraphViz DOT code as a string
        """
        lines = ["digraph LucidSpark {"]
        lines.append("  rankdir=TB;")
        lines.append("  node [shape=box];")
        lines.append("")
        
        # Add nodes (non-line shapes)
        for shape in self._shapes.values():
            if shape.type() != "line":
                label = shape.text_area_1 or shape.id
                # Escape quotes
                label = label.replace('"', '\\"')
                lines.append(f'  "{shape.id}" [label="{label}"];')
        
        lines.append("")
        
        # Add edges (from line shapes)
        for shape in self._shapes.values():
            if shape.type() == "line" and shape.line_source and shape.line_destination:
                lines.append(f'  "{shape.line_source}" -> "{shape.line_destination}";')
        
        lines.append("}")
        
        return "\n".join(lines)

