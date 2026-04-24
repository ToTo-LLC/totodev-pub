# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Mermaid Flowchart Renderer - Convert LucidSpark to Mermaid Diagrams

## Purpose

This module provides the `MermaidRenderer` class that transforms LucidSpark shape
collections into Mermaid flowchart syntax. Mermaid is a markdown-compatible diagramming
language that renders in browsers, documentation sites, and many modern tools. This
renderer enables automated diagram generation from LucidSpark whiteboard exports.

## About LucidSpark and Mermaid

**LucidSpark** is a collaborative whiteboard tool where users create visual diagrams
with shapes, containers (frames), and connecting lines. When exported to CSV, these
elements can be programmatically processed.

**Mermaid** is a text-based diagramming language that generates diagrams from simple
markdown-like syntax. It's widely supported in GitHub, GitLab, documentation sites,
and can be rendered to images via CLI tools.

## Module Organization

This module is part of a three-file architecture:

- **lucidspark_models.py**: Primitive, serializable data models (`LucidSparkShapeRecord`)
- **lucidspark_collection.py**: Collection management and analysis
- **mermaid_renderer.py** (this file): Mermaid flowchart diagram generation

## Rendering Strategy

The renderer transforms LucidSpark structures to Mermaid equivalents:

| LucidSpark Element | Mermaid Equivalent | Notes |
|--------------------|-------------------|-------|
| Container (Frame)  | Subgraph          | Nested subgraphs for hierarchy |
| Shape (Card/Note)  | Node              | Various node shapes based on type |
| Line (Connection)  | Edge              | Directed arrows between nodes |
| Tags               | CSS Styling       | Deterministic color assignment |
| Text Area 1        | Node Label        | Escaped and wrapped |

## Key Features

- **Nested subgraphs**: Containers render as nested Mermaid subgraphs
- **Shape variety**: Different node shapes for different LucidSpark shape types
- **Tag-based coloring**: Unique colors for each tag combination (deterministic)
- **Text handling**: Automatic escaping and wrapping at configurable width
- **Legend generation**: Automatic color-tag legend in diagram
- **Deterministic output**: Same input always produces identical output

## Usage Examples

**Note:** The code samples below are verified by corresponding test cases in 
`test_lucidspark_parser.py::TestModuleDocstringExamples`.

### Command-Line Interface

For quick conversions, use the built-in CLI:

```bash
# Basic usage (requires PYTHONPATH=src)
PYTHONPATH=src python -m totodev_pub.lucidspark.mermaid_renderer diagram.csv > output.mmd

# With options
PYTHONPATH=src python -m totodev_pub.lucidspark.mermaid_renderer -d LR -w 40 diagram.csv
```

### Basic Usage (via Collection)

The most common way to use this renderer is through `LucidSparkShapeCollection`:

```python
from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection

# Load and render
collection = LucidSparkShapeCollection.from_csv("export.csv")
mermaid_code = collection.to_mermaid(direction="TB", wrap_width=50)

# Save to file
with open("diagram.mmd", "w") as f:
    f.write(mermaid_code)
```

### Direct Renderer Usage

For more control, you can use the renderer directly:

```python
from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
from totodev_pub.lucidspark.mermaid_renderer import MermaidRenderer

# Load collection
collection = LucidSparkShapeCollection.from_csv("export.csv")

# Create renderer with custom settings
renderer = MermaidRenderer(
    collection=collection,
    direction="LR",      # Left-to-right layout
    wrap_width=40        # Wrap text at 40 characters
)

# Render to Mermaid code
mermaid_code = renderer.render()
print(mermaid_code)
```

### Different Layout Directions

```python
# Top-to-bottom (default)
mermaid_tb = collection.to_mermaid(direction="TB")

# Left-to-right (good for wide diagrams)
mermaid_lr = collection.to_mermaid(direction="LR")

# Right-to-left
mermaid_rl = collection.to_mermaid(direction="RL")

# Bottom-to-top
mermaid_bt = collection.to_mermaid(direction="BT")
```

### Controlling Text Wrapping

```python
# No wrapping (may produce very wide nodes)
mermaid_no_wrap = collection.to_mermaid(wrap_width=1000)

# Narrow wrapping (good for mobile/narrow displays)
mermaid_narrow = collection.to_mermaid(wrap_width=30)

# Default balanced wrapping
mermaid_default = collection.to_mermaid(wrap_width=50)
```

### Using Rendered Output

#### In Markdown Files

```python
mermaid_code = collection.to_mermaid()

markdown_content = f'''
# My Diagram

```mermaid
{mermaid_code}
```
'''

with open("README.md", "w") as f:
    f.write(markdown_content)
```

#### Converting to Images

Using Mermaid CLI (requires `npm install -g @mermaid-js/mermaid-cli`):

```python
import subprocess

mermaid_code = collection.to_mermaid()

# Save Mermaid code
with open("diagram.mmd", "w") as f:
    f.write(mermaid_code)

# Convert to PNG
subprocess.run(["mmdc", "-i", "diagram.mmd", "-o", "diagram.png"])

# Convert to SVG (scalable)
subprocess.run(["mmdc", "-i", "diagram.mmd", "-o", "diagram.svg"])

# Convert to PDF
subprocess.run(["mmdc", "-i", "diagram.mmd", "-o", "diagram.pdf"])
```

#### In Jupyter Notebooks

```python
from IPython.display import Markdown, display

mermaid_code = collection.to_mermaid()
display(Markdown(f"```mermaid\\n{mermaid_code}\\n```"))
```

## Mermaid Output Structure

A typical rendered diagram includes:

1. **Header**: `flowchart TB` (or chosen direction)
2. **Orphan nodes**: Shapes not in any container
3. **Container subgraphs**: Nested hierarchies with contained shapes
4. **Edges**: Connections between shapes (from Line records)
5. **CSS styling**: Color classes for tag combinations
6. **Style assignments**: Applying colors to tagged nodes
7. **Legend**: "Color-Tag Legend" subgraph showing tag-color mappings

Example output:
```
flowchart TB

  n1["Orphan Shape"]

  subgraph n2["Container A"]
    n3["Shape 1"]
    n4["Shape 2"]
  end

  n3 --> n4

  classDef Tag1-Tag2 fill:#FFB3BA
  class n3 Tag1-Tag2

  subgraph legend["Color-Tag Legend"]
    legend0["Tag1, Tag2"]
    class legend0 Tag1-Tag2
  end
```

## Design Decisions

### Why Deterministic Colors?
MD5 hashing ensures the same tag combination always gets the same color across
multiple renders, making diagrams consistent and predictable.

### Why Escape Text?
Mermaid uses special characters (`"`, `#`, `[`, `]`, etc.) for syntax. These must be
escaped in user text to avoid breaking diagram parsing.

### Why Wrap on Spaces Only?
Wrapping mid-word creates awkward line breaks. Wrapping on spaces maintains readability
while fitting text into configured width limits.

### Why Generate Legend?
With deterministic colors but no user control, the legend helps readers understand
which colors represent which tag combinations.

## Limitations

- No custom color palettes (deterministic assignment only)
- All arrow styles default to standard arrows (no customization by arrow type)
- No filtering (renders entire board)
- Text Area 2 is ignored (only Text Area 1 used for labels)

See module documentation in `sparkboard_export_interpreter.py` for complete feature
scope and planned enhancements.
"""

import hashlib
import re
from typing import Dict, List, Set, Tuple, TYPE_CHECKING

import click

if TYPE_CHECKING:
    from .lucidspark_collection import LucidSparkShapeCollection
    from .lucidspark_models import LucidSparkShapeRecord


class MermaidRenderer:
    """Renders a LucidSpark collection as a Mermaid flowchart."""
    
    # Mermaid node shapes for different LucidSpark shape types
    SHAPE_MAPPINGS = {
        "sticky note": "round",  # (text)
        "lucidcardblock": "rect",  # [text]
        "user image": "doc",  # {{text}}
    }
    
    # Available Mermaid node shapes for deterministic assignment
    AVAILABLE_SHAPES = ["rect", "round", "stadium", "subroutine", "cylindrical", "circle", "asymmetric", "rhombus", "hexagon", "parallelogram", "trapezoid"]
    
    # Color palette for tag combinations (deterministic)
    COLOR_PALETTE = [
        "#FFB3BA", "#FFDFBA", "#FFFFBA", "#BAFFC9", "#BAE1FF",
        "#FFB3E6", "#E6B3FF", "#B3D9FF", "#B3FFB3", "#FFD9B3",
        "#FFC9DE", "#C9E4FF", "#E4FFC9", "#FFC9C9", "#C9FFC9"
    ]
    
    def __init__(self, collection: "LucidSparkShapeCollection", direction: str = "TB", wrap_width: int = 50):
        """
        Initialize the Mermaid renderer.
        
        Args:
            collection: The LucidSpark shape collection to render
            direction: Diagram direction (TB, LR, RL, BT)
            wrap_width: Maximum characters per line before wrapping
        """
        self.collection = collection
        self.direction = direction
        self.wrap_width = wrap_width
        self._node_id_map: Dict[str, str] = {}
        self._tag_color_map: Dict[Tuple[str, ...], str] = {}
        self._tag_class_name_map: Dict[Tuple[str, ...], str] = {}
    
    def render(self) -> str:
        """
        Render the collection as a Mermaid flowchart.
        
        Returns:
            Mermaid flowchart code as a string
        """
        lines = [f"flowchart {self.direction}"]
        lines.append("")
        
        # Build node ID sanitization map
        self._build_node_id_map()
        
        # Build tag-to-color mappings
        self._build_tag_color_map()
        
        # Render containers as subgraphs
        containers = [s for s in self.collection._shapes.values() if s.type() == "container"]
        root_containers = [c for c in containers if not c.contained_by]
        
        # Render shapes not in containers (orphans)
        orphans = [
            s for s in self.collection._shapes.values()
            if s.type() == "shape" and not s.contained_by
        ]
        
        for orphan in orphans:
            lines.append(self._render_shape(orphan))
        
        if orphans:
            lines.append("")
        
        # Render container hierarchies
        for root in root_containers:
            lines.extend(self._render_container(root, indent_level=1))
            lines.append("")
        
        # Render edges (from line shapes)
        line_shapes = [s for s in self.collection._shapes.values() if s.type() == "line"]
        if line_shapes:
            for line in line_shapes:
                if line.line_source and line.line_destination:
                    src_id = self._get_node_id(line.line_source)
                    dst_id = self._get_node_id(line.line_destination)
                    arrow = self._get_arrow_style(line.destination_arrow)
                    lines.append(f"  {src_id} {arrow} {dst_id}")
            lines.append("")
        
        # Render styling for tags
        if self._tag_color_map:
            lines.extend(self._render_tag_styling())
            lines.append("")
        
        # Render color-tag legend
        if self._tag_color_map:
            lines.extend(self._render_legend())
        
        return "\n".join(lines)
    
    def _build_node_id_map(self):
        """Create sanitized Mermaid-safe node IDs for all non-line shapes."""
        for shape in self.collection._shapes.values():
            if shape.type() != "line":
                # Replace non-alphanumeric chars with underscores (Mermaid requirement)
                sanitized_id = re.sub(r'[^a-zA-Z0-9_]', '_', str(shape.id))
                
                # Mermaid IDs must start with a letter, prefix with 'n' if needed
                if not sanitized_id[0].isalpha():
                    sanitized_id = 'n' + sanitized_id
                
                self._node_id_map[shape.id] = sanitized_id
    
    def _get_node_id(self, original_shape_id: str) -> str:
        """Look up sanitized Mermaid ID, falling back to prefixed original if not found."""
        return self._node_id_map.get(original_shape_id, f"n{original_shape_id}")
    
    def _build_tag_color_map(self):
        """Map each unique tag combination to a deterministic color and semantic class name."""
        # Collect all unique tag combinations from shapes
        unique_tag_combinations = set()
        for shape in self.collection._shapes.values():
            if shape.tags:
                # Split on pipe or comma, normalize whitespace, sort for consistency
                normalized_tags = tuple(sorted(
                    tag.strip() 
                    for tag in re.split(r'[|,]', shape.tags) 
                    if tag.strip()
                ))
                if normalized_tags:
                    unique_tag_combinations.add(normalized_tags)
        
        # Assign colors and class names using deterministic methods
        for tag_combination in sorted(unique_tag_combinations):
            # Hash the tag tuple to get consistent color index
            tag_hash = int(hashlib.md5(str(tag_combination).encode()).hexdigest(), 16)
            color_index = tag_hash % len(self.COLOR_PALETTE)
            self._tag_color_map[tag_combination] = self.COLOR_PALETTE[color_index]
            
            # Generate semantic class name from tags
            self._tag_class_name_map[tag_combination] = self._generate_semantic_class_name(tag_combination)
    
    def _get_tag_combo(self, shape: "LucidSparkShapeRecord") -> Tuple[str, ...]:
        """Extract and normalize shape's tags into sorted tuple (empty if no tags)."""
        if not shape.tags:
            return tuple()
        
        # Parse tags same way as _build_tag_color_map for consistency
        normalized_tags = tuple(sorted(
            tag.strip() 
            for tag in re.split(r'[|,]', shape.tags) 
            if tag.strip()
        ))
        return normalized_tags
    
    def _sanitize_tag_for_css(self, tag: str) -> str:
        """
        Sanitize a single tag name for use in CSS class names.
        
        Converts tag to PascalCase and replaces invalid CSS characters.
        Examples:
            "Souban Houn" -> "SoubanHoun"
            "Sprint #42" -> "Sprint_42"
            "2023 Release" -> "2023Release"
        """
        # Split on spaces and capitalize each word (PascalCase)
        words = tag.split()
        pascal_case = ''.join(word.capitalize() for word in words)
        
        # Replace any non-alphanumeric/hyphen/underscore with underscore
        sanitized = re.sub(r'[^a-zA-Z0-9\-_]', '_', pascal_case)
        
        # Remove consecutive underscores
        sanitized = re.sub(r'_+', '_', sanitized)
        
        # Remove leading/trailing underscores
        sanitized = sanitized.strip('_')
        
        return sanitized if sanitized else 'tag'
    
    def _generate_semantic_class_name(self, tag_combination: Tuple[str, ...]) -> str:
        """
        Generate a semantic CSS class name from tag combination.
        
        Joins sanitized tags with hyphens, prefixes with 'tag_' if starts with digit.
        Truncates to 100 chars if needed, breaking on tag boundaries.
        
        Examples:
            ("Souban Houn", "Major Release") -> "SoubanHoun-MajorRelease"
            ("Bug Fix", "Sprint #42") -> "BugFix-Sprint_42"
            ("2023", "Q1") -> "tag_2023-Q1"
        """
        if not tag_combination:
            return "tag_untagged"
        
        # Sanitize each tag individually
        sanitized_tags = [self._sanitize_tag_for_css(tag) for tag in tag_combination]
        
        # Join with hyphens
        class_name = '-'.join(sanitized_tags)
        
        # CSS class names cannot start with a digit
        if class_name and class_name[0].isdigit():
            class_name = 'tag_' + class_name
        
        # Truncate to 100 chars if needed, breaking on hyphen boundaries
        max_length = 100
        if len(class_name) > max_length:
            # Try to break at last hyphen within limit
            truncated = class_name[:max_length]
            last_hyphen = truncated.rfind('-')
            if last_hyphen > 0:
                class_name = truncated[:last_hyphen]
            else:
                class_name = truncated
        
        return class_name
    
    def _render_shape(self, shape: "LucidSparkShapeRecord", indent_level: int = 1) -> str:
        """Convert shape to Mermaid node syntax with appropriate shape style."""
        sanitized_node_id = self._get_node_id(shape.id)
        formatted_label = self._format_label(shape.text_area_1 or shape.id)
        
        # Determine which Mermaid shape syntax to use
        mermaid_shape_type = self._get_node_shape(shape)
        
        # Map shape type to Mermaid syntax (each type has specific bracket syntax)
        if mermaid_shape_type == "round":
            node_definition = f"({formatted_label})"
        elif mermaid_shape_type == "doc":
            node_definition = f"{{{{{formatted_label}}}}}"
        elif mermaid_shape_type == "stadium":
            node_definition = f"([{formatted_label}])"
        elif mermaid_shape_type == "subroutine":
            node_definition = f"[[{formatted_label}]]"
        elif mermaid_shape_type == "cylindrical":
            node_definition = f"[({formatted_label})]"
        elif mermaid_shape_type == "circle":
            node_definition = f"(({formatted_label}))"
        elif mermaid_shape_type == "asymmetric":
            node_definition = f">{formatted_label}]"
        elif mermaid_shape_type == "rhombus":
            node_definition = f"{{{formatted_label}}}"
        elif mermaid_shape_type == "hexagon":
            node_definition = f"{{{{{formatted_label}}}}}"
        elif mermaid_shape_type == "parallelogram":
            node_definition = f"[/{formatted_label}/]"
        elif mermaid_shape_type == "trapezoid":
            node_definition = f"[\\{formatted_label}/]"
        else:  # rect (default rectangular)
            node_definition = f"[{formatted_label}]"
        
        indentation = "  " * indent_level
        return f"{indentation}{sanitized_node_id}{node_definition}"
    
    def _get_node_shape(self, shape: "LucidSparkShapeRecord") -> str:
        """Select Mermaid shape style: predefined for known types, hash-based for others."""
        if not shape.name:
            return "rect"
        
        normalized_shape_name = shape.name.lower()
        
        # Check if this is a known shape type with explicit mapping
        if normalized_shape_name in self.SHAPE_MAPPINGS:
            return self.SHAPE_MAPPINGS[normalized_shape_name]
        
        # Unknown types get deterministic selection via MD5 hash
        shape_name_hash = int(hashlib.md5(shape.name.encode()).hexdigest(), 16)
        selected_shape_index = shape_name_hash % len(self.AVAILABLE_SHAPES)
        return self.AVAILABLE_SHAPES[selected_shape_index]
    
    def _format_label(self, label_text: str) -> str:
        """Escape special chars and wrap long text for Mermaid label syntax."""
        if not label_text:
            return '""'
        
        # Escape characters that break Mermaid syntax
        escaped_text = label_text.replace('"', '\\"')
        escaped_text = escaped_text.replace('#', '\\#')
        
        # Wrap text at configured width, breaking only on spaces
        if len(escaped_text) > self.wrap_width:
            words = escaped_text.split(' ')
            wrapped_lines = []
            current_line_words = []
            current_line_length = 0
            
            for word in words:
                word_length = len(word)
                # Account for spaces between words in length calculation
                total_with_word = current_line_length + word_length + len(current_line_words)
                
                if total_with_word > self.wrap_width and current_line_words:
                    # Current line is full, save it and start new line
                    wrapped_lines.append(' '.join(current_line_words))
                    current_line_words = [word]
                    current_line_length = word_length
                else:
                    # Add word to current line
                    current_line_words.append(word)
                    current_line_length += word_length
            
            # Add final line if any words remain
            if current_line_words:
                wrapped_lines.append(' '.join(current_line_words))
            
            # Join lines with HTML break tag (Mermaid supports this)
            escaped_text = '<br>'.join(wrapped_lines)
        
        # Wrap in quotes for Mermaid
        return f'"{escaped_text}"'
    
    def _render_container(self, container_shape: "LucidSparkShapeRecord", indent_level: int = 1) -> List[str]:
        """Generate Mermaid subgraph syntax for container with all nested children."""
        mermaid_lines = []
        indentation = "  " * indent_level
        
        # Open subgraph with sanitized ID and escaped label
        subgraph_id = self._get_node_id(container_shape.id)
        container_label = container_shape.text_area_1 or container_shape.id
        escaped_label = container_label.replace('"', '\\"')
        mermaid_lines.append(f'{indentation}subgraph {subgraph_id}["{escaped_label}"]')
        
        # Find all shapes directly contained by this container
        contained_shapes = [
            shape for shape in self.collection._shapes.values()
            if shape.contained_by == container_shape.id
        ]
        
        # Separate children into nested containers vs leaf shapes
        nested_containers = [s for s in contained_shapes if s.type() == "container"]
        leaf_shapes = [s for s in contained_shapes if s.type() == "shape"]
        
        # Render leaf shapes first (simpler, no nesting)
        for leaf_shape in leaf_shapes:
            mermaid_lines.append(self._render_shape(leaf_shape, indent_level + 1))
        
        # Recursively render nested containers
        for nested_container in nested_containers:
            mermaid_lines.extend(self._render_container(nested_container, indent_level + 1))
        
        # Close subgraph
        mermaid_lines.append(f"{indentation}end")
        
        return mermaid_lines
    
    def _get_arrow_style(self, lucidspark_arrow_type: str) -> str:
        """Return Mermaid arrow syntax (currently defaults to standard arrow)."""
        # Future: could map different arrow types to Mermaid variations
        # For now, use standard directed arrow for all connections
        return "-->"
    
    def _render_tag_styling(self) -> List[str]:
        """Generate Mermaid CSS class definitions and assignments for tag colors."""
        styling_lines = []
        
        # Define a CSS class for each unique tag combination with semantic names
        for tag_combination, hex_color in self._tag_color_map.items():
            style_class_name = self._tag_class_name_map[tag_combination]
            styling_lines.append(f"  classDef {style_class_name} fill:{hex_color}")
        
        styling_lines.append("")
        
        # Apply appropriate style class to each shape based on its tags
        for shape in self.collection._shapes.values():
            if shape.type() != "line":
                shape_tags = self._get_tag_combo(shape)
                if shape_tags and shape_tags in self._tag_color_map:
                    style_class_name = self._tag_class_name_map[shape_tags]
                    shape_node_id = self._get_node_id(shape.id)
                    styling_lines.append(f"  class {shape_node_id} {style_class_name}")
        
        return styling_lines
    
    def _render_legend(self) -> List[str]:
        """Create Color-Tag Legend subgraph showing tag-to-color mappings."""
        legend_lines = []
        legend_lines.append('  subgraph legend["Color-Tag Legend"]')
        
        # Create a legend entry for each tag combination
        for legend_index, (tag_combination, hex_color) in enumerate(self._tag_color_map.items()):
            # Join tags with commas for readable display
            tags_display_text = ', '.join(tag_combination)
            legend_node_id = f"legend{legend_index}"
            
            # Create legend node with tag text
            legend_lines.append(f'    {legend_node_id}["{tags_display_text}"]')
            
            # Apply same color style as used for shapes with these tags (semantic name)
            matching_style_class = self._tag_class_name_map[tag_combination]
            legend_lines.append(f"    class {legend_node_id} {matching_style_class}")
        
        legend_lines.append("  end")
        
        return legend_lines


@click.command()
@click.argument('csv_file', type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option('--direction', '-d', default='TB', 
              type=click.Choice(['TB', 'LR', 'RL', 'BT'], case_sensitive=False),
              help='Diagram direction (TB=top-bottom, LR=left-right, RL=right-left, BT=bottom-top)')
@click.option('--wrap-width', '-w', default=50, type=int,
              help='Maximum characters per line before wrapping (default: 50)')
def main(csv_file: str, direction: str, wrap_width: int):
    """
    Convert a LucidSpark CSV export to Mermaid flowchart syntax.
    
    Reads a LucidSpark CSV file and outputs the corresponding Mermaid diagram
    code to STDOUT. The output can be redirected to a file or piped to other tools.
    
    Example usage:
    
        python -m totodev_pub.lucidspark.mermaid_renderer diagram.csv > output.mmd
        
        python -m totodev_pub.lucidspark.mermaid_renderer --direction LR diagram.csv
        
        python -m totodev_pub.lucidspark.mermaid_renderer -d TB -w 40 diagram.csv
    """
    from .lucidspark_collection import LucidSparkShapeCollection
    
    # Load the CSV file
    collection = LucidSparkShapeCollection.from_csv(csv_file)
    
    # Render to Mermaid
    mermaid_code = collection.to_mermaid(direction=direction.upper(), wrap_width=wrap_width)
    
    # Output to STDOUT
    click.echo(mermaid_code)


if __name__ == "__main__":
    main()

