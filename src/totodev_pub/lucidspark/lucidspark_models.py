# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
LucidSpark Data Models - Primitive Serializable Records

## Purpose

This module provides the foundational Pydantic v2 data models for representing individual
shapes from LucidSpark CSV exports. These are primitive, serializable structures designed
to be used independently of the full parsing infrastructure, making them suitable for
JSON/YAML serialization, data persistence, and API communication.

## About LucidSpark

LucidSpark is an online collaborative whiteboard tool that allows teams to create visual
diagrams using shapes, containers (frames), and connecting lines. Users can export their
boards as CSV files containing structured data about all elements and their relationships.

## Module Organization

This module is part of a three-file architecture:

- **lucidspark_models.py** (this file): Primitive, serializable data models
- **lucidspark_collection.py**: Collection management, analysis, and rendering coordination
- **mermaid_renderer.py**: Mermaid flowchart diagram generation

## Design Philosophy

The separation of primitive models into this file allows:

1. **Independent serialization**: Models can be persisted without importing the full stack
2. **Data validation**: Pydantic provides type checking and validation at the data layer
3. **API integration**: Lightweight models can be used in API requests/responses
4. **Flexibility**: Other modules can build complex functionality on these primitives

## Usage Example

**Note:** The code samples below are verified by corresponding test cases in 
`test_lucidspark_parser.py::TestModuleDocstringExamples`.

```python
from lucidspark_models import LucidSparkShapeRecord

# Create a shape record directly (e.g., from API data)
shape = LucidSparkShapeRecord(
    Id="123",
    Name="LucidCardBlock",
    **{"Text Area 1": "My Task"}
)

# Type detection
print(shape.type())  # "shape"
print(shape.is_type("container"))  # False

# Serialization
import json
shape_data = shape.model_dump()
json_str = json.dumps(shape_data)

# Deserialization
restored = LucidSparkShapeRecord.model_validate(json.loads(json_str))
```

## Key Features

- **Tolerant validation**: Handles missing fields gracefully
- **Alias support**: Maps CSV column names (e.g., "Text Area 1") to Python attributes
- **Type detection**: Automatic classification as shape, container, or line
- **Hashable**: Can be used in sets and as dictionary keys
"""

from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


class LucidSparkShapeRecord(BaseModel):
    """
    Represents a single row from a LucidSpark CSV export.
    
    This is a Pydantic v2 BaseModel with tolerant validation rules to handle
    missing or malformed data gracefully. The record is hashable based on its ID.
    """
    
    # Primary key
    id: str = Field(alias="Id")
    
    # Core structure fields
    name: Optional[str] = Field(default=None, alias="Name")
    shape_library: Optional[str] = Field(default=None, alias="Shape Library")
    page_id: Optional[str] = Field(default=None, alias="Page ID")
    contained_by: Optional[str] = Field(default=None, alias="Contained By")
    group: Optional[str] = Field(default=None, alias="Group")
    
    # Line/connection fields
    line_source: Optional[str] = Field(default=None, alias="Line Source")
    line_destination: Optional[str] = Field(default=None, alias="Line Destination")
    source_arrow: Optional[str] = Field(default=None, alias="Source Arrow")
    destination_arrow: Optional[str] = Field(default=None, alias="Destination Arrow")
    
    # Categorization fields
    tags: Optional[str] = Field(default=None, alias="Tags")
    status: Optional[str] = Field(default=None, alias="Status")
    
    # Text content fields
    text_area_1: Optional[str] = Field(default=None, alias="Text Area 1")
    text_area_2: Optional[str] = Field(default=None, alias="Text Area 2")
    title: Optional[str] = Field(default=None, alias="Title")
    description: Optional[str] = Field(default=None, alias="Description")
    comments: Optional[str] = Field(default=None, alias="Comments")
    
    # Task/project management fields
    assignee: Optional[str] = Field(default=None, alias="Assignee")
    start_date: Optional[str] = Field(default=None, alias="Start Date")
    end_date: Optional[str] = Field(default=None, alias="End Date")
    estimate: Optional[str] = Field(default=None, alias="Estimate")
    t_shirt_size: Optional[str] = Field(default=None, alias="T-shirt size")
    
    model_config = ConfigDict(
        extra='allow',  # Allow additional fields not defined in the model
        populate_by_name=True,  # Allow population by both field name and alias
        validate_assignment=False,  # Don't validate on assignment (tolerant)
    )
    
    def __hash__(self) -> int:
        """Hash based on ID for use in sets and as dict keys."""
        return hash(self.id)
    
    def __eq__(self, other) -> bool:
        """Equality based on ID."""
        if not isinstance(other, LucidSparkShapeRecord):
            return False
        return self.id == other.id
    
    def type(self) -> str:
        """
        Returns the type of the shape: "line", "container", or "shape".
        
        Classification logic:
        - "line": line_source is populated
        - "container": name contains "Frame", "Container", or "Table" (case-insensitive)
        - "shape": everything else (leaf nodes)
        """
        # Check for line type
        if self.line_source:
            return "line"
        
        # Check for container type
        if self.name:
            name_lower = self.name.lower()
            if any(keyword in name_lower for keyword in ["frame", "container", "table"]):
                return "container"
        
        # Default to shape
        return "shape"
    
    def is_type(self, shape_type: str) -> bool:
        """
        Check if the shape matches the given type.
        
        Args:
            shape_type: One of "line", "container", or "shape"
            
        Returns:
            True if the shape matches the given type, False otherwise
        """
        return self.type() == shape_type

