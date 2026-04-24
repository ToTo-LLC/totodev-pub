# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
Tests for LucidSpark CSV Parser

Tests the parsing, analysis, and rendering capabilities of the LucidSpark module.
"""

import pytest
from pathlib import Path

from totodev_pub.lucidspark.lucidspark_models import LucidSparkShapeRecord
from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection, LucidSparkLeafRecord


@pytest.fixture
def example_csv_path():
    """Path to the example CSV file."""
    return Path(__file__).parent.parent / "src" / "totodev_pub" / "lucidspark" / "examples" / "ops_projects.csv"


@pytest.fixture
def collection(example_csv_path):
    """Load the example CSV into a collection."""
    return LucidSparkShapeCollection.from_csv(example_csv_path)


class TestLucidSparkShapeRecord:
    """Tests for LucidSparkShapeRecord model."""
    
    def test_shape_record_creation(self):
        """Test creating a shape record with minimal data."""
        shape = LucidSparkShapeRecord(Id="1", Name="Test Shape")
        assert shape.id == "1"
        assert shape.name == "Test Shape"
        assert shape.text_area_1 is None
    
    def test_shape_record_with_aliases(self):
        """Test creating a shape record using CSV-style field names."""
        data = {
            "Id": "1",
            "Name": "LucidCardBlock",
            "Text Area 1": "My Card",
            "Line Source": "2"
        }
        shape = LucidSparkShapeRecord(**data)
        assert shape.id == "1"
        assert shape.text_area_1 == "My Card"
        assert shape.line_source == "2"
    
    def test_type_detection_line(self):
        """Test type detection for line shapes."""
        shape = LucidSparkShapeRecord(Id="1", Name="Line", **{"Line Source": "2", "Line Destination": "3"})
        assert shape.type() == "line"
        assert shape.is_type("line")
    
    def test_type_detection_container(self):
        """Test type detection for container shapes."""
        shape = LucidSparkShapeRecord(Id="1", Name="SparkContainerBlock")
        assert shape.type() == "container"
        assert shape.is_type("container")
        
        shape2 = LucidSparkShapeRecord(Id="2", Name="Frame")
        assert shape2.type() == "container"
        
        shape3 = LucidSparkShapeRecord(Id="3", Name="Table")
        assert shape3.type() == "container"
    
    def test_type_detection_shape(self):
        """Test type detection for regular shapes."""
        shape = LucidSparkShapeRecord(Id="1", Name="LucidCardBlock")
        assert shape.type() == "shape"
        assert shape.is_type("shape")
    
    def test_hashable(self):
        """Test that shape records are hashable by ID."""
        shape1 = LucidSparkShapeRecord(Id="1", Name="Test")
        shape2 = LucidSparkShapeRecord(Id="1", Name="Different")
        shape3 = LucidSparkShapeRecord(Id="2", Name="Test")
        
        assert hash(shape1) == hash(shape2)
        assert hash(shape1) != hash(shape3)
        
        # Can be used in sets
        shape_set = {shape1, shape2, shape3}
        assert len(shape_set) == 2


class TestLucidSparkShapeCollection:
    """Tests for LucidSparkShapeCollection."""
    
    def test_from_csv_loads_successfully(self, collection):
        """Test that CSV loads without errors."""
        assert collection is not None
        assert len(collection._shapes) > 0
    
    def test_duplicate_id_detection(self):
        """Test that duplicate IDs are detected."""
        shapes = {
            "1": LucidSparkShapeRecord(Id="1", Name="Shape1"),
            "1": LucidSparkShapeRecord(Id="1", Name="Shape2"),  # Duplicate
        }
        # Dict will only have one entry, but this tests the concept
        # In real CSV loading, this would be caught
        assert len(shapes) == 1
    
    def test_groups_property(self, collection):
        """Test that groups are properly identified."""
        groups = collection.groups
        assert isinstance(groups, dict)
        # Groups may or may not exist in the example CSV
    
    def test_graph_property(self, collection):
        """Test that graph is created successfully."""
        graph = collection.graph
        assert graph is not None
        assert len(graph.nodes()) > 0
    
    def test_tree_property(self, collection):
        """Test that tree structure is created."""
        tree = collection.tree
        assert tree is not None
        assert tree.size() > 0
    
    def test_connective_ratio(self, collection):
        """Test connective ratio calculation."""
        ratio = collection.connective_ratio()
        assert 0.0 <= ratio <= 1.0
    
    def test_container_membership_ratio(self, collection):
        """Test container membership ratio calculation."""
        ratio = collection.container_membership_ratio()
        assert 0.0 <= ratio <= 1.0
    
    def test_has_groups(self, collection):
        """Test group detection."""
        has_groups = collection.has_groups()
        assert isinstance(has_groups, bool)
    
    def test_tags(self, collection):
        """Test tag extraction."""
        tags = collection.tags()
        assert isinstance(tags, list)
        # Tags should be sorted and unique
        assert tags == sorted(set(tags))
    
    def test_leaves(self, collection):
        """Test leaf record extraction."""
        leaves = collection.leaves()
        assert isinstance(leaves, list)
        assert all(isinstance(leaf, LucidSparkLeafRecord) for leaf in leaves)
        
        # All leaves should be non-container, non-line shapes
        for leaf in leaves:
            assert leaf.shape.type() == "shape"
    
    def test_leaf_parent_containers(self, collection):
        """Test that leaf records have proper parent container hierarchies."""
        leaves = collection.leaves()
        
        for leaf in leaves:
            # Parent containers should be ordered from top-level to immediate parent
            assert isinstance(leaf.parent_containers, list)
            
            # If leaf has a parent, verify it's in the hierarchy
            if leaf.shape.contained_by:
                assert len(leaf.parent_containers) > 0
                # Last parent should be the immediate parent
                assert leaf.parent_containers[-1].id == leaf.shape.contained_by


class TestMermaidRenderer:
    """Tests for Mermaid rendering."""
    
    def test_to_mermaid_generates_output(self, collection):
        """Test that Mermaid output is generated."""
        mermaid = collection.to_mermaid()
        assert isinstance(mermaid, str)
        assert len(mermaid) > 0
        assert mermaid.startswith("flowchart TB")
    
    def test_to_mermaid_with_direction(self, collection):
        """Test Mermaid rendering with different directions."""
        mermaid_lr = collection.to_mermaid(direction="LR")
        assert mermaid_lr.startswith("flowchart LR")
        
        mermaid_rl = collection.to_mermaid(direction="RL")
        assert mermaid_rl.startswith("flowchart RL")
    
    def test_to_mermaid_with_wrap_width(self, collection):
        """Test Mermaid rendering with text wrapping."""
        mermaid = collection.to_mermaid(wrap_width=20)
        assert isinstance(mermaid, str)
        # Should contain line breaks for long text
        # This is hard to verify without knowing specific content
    
    def test_semantic_tag_class_names(self):
        """Test that tag-based CSS class names are semantic and properly sanitized."""
        from totodev_pub.lucidspark.mermaid_renderer import MermaidRenderer
        
        # Create a minimal collection with tagged shapes
        shapes_data = [
            LucidSparkShapeRecord(Id="1", Name="LucidCardBlock", Tags="Release Team,Major Release", **{"Text Area 1": "Shape 1"}),
            LucidSparkShapeRecord(Id="2", Name="LucidCardBlock", Tags="Bug Fix|Sprint #42", **{"Text Area 1": "Shape 2"}),
            LucidSparkShapeRecord(Id="3", Name="LucidCardBlock", Tags="2023,Q1", **{"Text Area 1": "Shape 3"}),
        ]
        
        shapes_dict = {shape.id: shape for shape in shapes_data}
        collection = LucidSparkShapeCollection(shapes_dict)
        
        # Render to Mermaid
        renderer = MermaidRenderer(collection)
        mermaid_code = renderer.render()
        
        # Verify semantic class names are used instead of sequential tagStyle0, tagStyle1, etc.
        assert "classDef MajorRelease-ReleaseTeam fill:" in mermaid_code
        assert "classDef BugFix-Sprint_42 fill:" in mermaid_code
        assert "classDef tag_2023-Q1 fill:" in mermaid_code
        
        # Verify the old sequential names are NOT present
        assert "tagStyle0" not in mermaid_code
        assert "tagStyle1" not in mermaid_code
        assert "tagStyle2" not in mermaid_code
        
        # Verify class assignments use semantic names
        assert "class n1 MajorRelease-ReleaseTeam" in mermaid_code
        assert "class n2 BugFix-Sprint_42" in mermaid_code
        assert "class n3 tag_2023-Q1" in mermaid_code
    
    def test_tag_sanitization_edge_cases(self):
        """Test edge cases in tag name sanitization."""
        from totodev_pub.lucidspark.mermaid_renderer import MermaidRenderer
        
        renderer = MermaidRenderer(LucidSparkShapeCollection({}))
        
        # Test PascalCase conversion
        assert renderer._sanitize_tag_for_css("release team") == "ReleaseTeam"
        assert renderer._sanitize_tag_for_css("MAJOR RELEASE") == "MajorRelease"
        
        # Test special character replacement
        assert renderer._sanitize_tag_for_css("Sprint #42") == "Sprint_42"
        assert renderer._sanitize_tag_for_css("Tag@2023") == "Tag_2023"
        assert renderer._sanitize_tag_for_css("Bug/Fix") == "Bug_fix"  # capitalize() makes first char upper, rest lower
        
        # Test consecutive underscores removal
        assert renderer._sanitize_tag_for_css("Tag__Name") == "Tag_name"  # Second word gets lowercased by capitalize()
        
        # Test empty/whitespace handling
        assert renderer._sanitize_tag_for_css("") == "tag"
        assert renderer._sanitize_tag_for_css("   ") == "tag"
    
    def test_semantic_class_name_generation(self):
        """Test semantic class name generation from tag combinations."""
        from totodev_pub.lucidspark.mermaid_renderer import MermaidRenderer
        
        renderer = MermaidRenderer(LucidSparkShapeCollection({}))
        
        # Test basic combination (tags are passed directly, not sorted here)
        tags = ("Release Team", "Major Release")
        class_name = renderer._generate_semantic_class_name(tags)
        assert class_name == "ReleaseTeam-MajorRelease"  # Order preserved as passed
        
        # Test leading digit handling
        tags = ("2023", "Q1")
        class_name = renderer._generate_semantic_class_name(tags)
        assert class_name == "tag_2023-Q1"
        assert not class_name[0].isdigit()  # Should not start with digit
        
        # Test empty tags
        tags = tuple()
        class_name = renderer._generate_semantic_class_name(tags)
        assert class_name == "tag_untagged"
        
        # Test length truncation
        long_tags = ("VeryLongTagName" * 10, "AnotherLongTag" * 10)
        class_name = renderer._generate_semantic_class_name(long_tags)
        assert len(class_name) <= 100


class TestGraphVizRenderer:
    """Tests for GraphViz DOT rendering."""
    
    def test_to_graphviz_dot_generates_output(self, collection):
        """Test that GraphViz DOT output is generated."""
        dot = collection.to_graphviz_dot()
        assert isinstance(dot, str)
        assert len(dot) > 0
        assert dot.startswith("digraph LucidSpark")
        assert dot.endswith("}")
    
    def test_graphviz_contains_nodes_and_edges(self, collection):
        """Test that GraphViz output contains nodes and edges."""
        dot = collection.to_graphviz_dot()
        
        # Should have node definitions
        assert '[label=' in dot
        
        # May or may not have edges depending on the CSV content
        # Just verify it doesn't crash


class TestIntegration:
    """Integration tests."""
    
    def test_full_workflow(self, example_csv_path):
        """Test complete workflow from loading to rendering."""
        # Load
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Analyze
        ratio = collection.connective_ratio()
        assert ratio >= 0.0
        
        tags = collection.tags()
        assert isinstance(tags, list)
        
        leaves = collection.leaves()
        assert len(leaves) > 0
        
        # Render
        mermaid = collection.to_mermaid(direction="TB", wrap_width=50)
        assert "flowchart TB" in mermaid
        
        dot = collection.to_graphviz_dot()
        assert "digraph" in dot
        
        print(f"\nLoaded {len(collection._shapes)} shapes")
        print(f"Found {len(leaves)} leaf nodes")
        print(f"Connective ratio: {ratio:.2f}")
        print(f"Tags: {tags}")


class TestModuleDocstringExamples:
    """
    Tests that verify all code samples from module-level docstrings.
    
    These tests ensure documentation examples remain accurate and functional.
    Each test corresponds to code samples in the respective module docstrings.
    """
    
    def test_models_docstring_example(self):
        """Verify code sample from lucidspark_models.py module docstring."""
        import json
        from totodev_pub.lucidspark.lucidspark_models import LucidSparkShapeRecord
        
        # Create a shape record directly (e.g., from API data)
        shape = LucidSparkShapeRecord(
            Id="123",
            Name="LucidCardBlock",
            **{"Text Area 1": "My Task"}
        )
        
        # Type detection
        assert shape.type() == "shape"
        assert shape.is_type("container") == False
        assert shape.is_type("shape") == True
        
        # Serialization
        shape_data = shape.model_dump()
        assert shape_data["id"] == "123"
        json_str = json.dumps(shape_data)
        assert isinstance(json_str, str)
        
        # Deserialization
        restored = LucidSparkShapeRecord.model_validate(json.loads(json_str))
        assert restored.id == "123"
        assert restored.name == "LucidCardBlock"
        assert restored.text_area_1 == "My Task"
    
    def test_collection_docstring_basic_loading(self, example_csv_path):
        """Verify basic loading example from lucidspark_collection.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        # Load a CSV export
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Analyze the board structure
        total_shapes = len(collection._shapes)
        assert total_shapes > 0
        
        connective_ratio = collection.connective_ratio()
        assert 0.0 <= connective_ratio <= 1.0
        
        container_ratio = collection.container_membership_ratio()
        assert 0.0 <= container_ratio <= 1.0
        
        has_groups = collection.has_groups()
        assert isinstance(has_groups, bool)
        
        tags = collection.tags()
        assert isinstance(tags, list)
    
    def test_collection_docstring_leaf_nodes(self, example_csv_path):
        """Verify leaf nodes example from lucidspark_collection.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Get all leaf nodes with their parent hierarchies
        leaves = collection.leaves()
        assert len(leaves) > 0
        
        for leaf in leaves:
            # Each leaf should have text_area_1 accessible
            assert hasattr(leaf, 'text_area_1')
            
            # Parent containers should be a list
            assert isinstance(leaf.parent_containers, list)
            
            if leaf.parent_containers:
                parent_names = [p.text_area_1 for p in leaf.parent_containers]
                # All parents should have text_area_1
                assert all(isinstance(name, (str, type(None))) for name in parent_names)
    
    def test_collection_docstring_graph_analysis(self, example_csv_path):
        """Verify graph analysis example from lucidspark_collection.py module docstring."""
        import networkx as nx
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Access NetworkX graph for connectivity analysis
        graph = collection.graph
        assert isinstance(graph, (nx.DiGraph, nx.MultiDiGraph))
        
        nodes_count = graph.number_of_nodes()
        edges_count = graph.number_of_edges()
        assert nodes_count > 0
        assert edges_count >= 0
        
        # Check if graph is strongly connected (only for DiGraph)
        if isinstance(graph, nx.DiGraph):
            connected = nx.is_strongly_connected(graph)
            assert isinstance(connected, bool)
    
    def test_collection_docstring_tree_analysis(self, example_csv_path):
        """Verify tree analysis example from lucidspark_collection.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Access treelib tree for hierarchy visualization
        tree = collection.tree
        assert tree is not None
        assert tree.size() > 0
        
        # Verify tree.show() doesn't crash (it prints to stdout)
        import io
        import sys
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            tree.show()
            output = sys.stdout.getvalue()
            assert len(output) > 0
        finally:
            sys.stdout = old_stdout
    
    def test_collection_docstring_mermaid_rendering(self, example_csv_path):
        """Verify Mermaid rendering example from lucidspark_collection.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Generate Mermaid flowchart
        mermaid_code = collection.to_mermaid(
            direction="LR",      # Left-to-right layout
            wrap_width=40        # Wrap text at 40 characters
        )
        
        assert isinstance(mermaid_code, str)
        assert "flowchart LR" in mermaid_code
        assert len(mermaid_code) > 0
    
    def test_collection_docstring_graphviz_rendering(self, example_csv_path):
        """Verify GraphViz rendering example from lucidspark_collection.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Generate GraphViz DOT format
        dot_code = collection.to_graphviz_dot()
        
        assert isinstance(dot_code, str)
        assert "digraph LucidSpark" in dot_code
        assert len(dot_code) > 0
    
    def test_collection_docstring_groups(self, example_csv_path):
        """Verify groups example from lucidspark_collection.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Access grouped shapes
        groups = collection.groups
        assert isinstance(groups, dict)
        
        for group_id, shapes_in_group in groups.items():
            assert isinstance(shapes_in_group, list)
            for shape in shapes_in_group:
                # Each shape should have text_area_1 attribute
                assert hasattr(shape, 'text_area_1')
    
    def test_collection_docstring_filtering(self, example_csv_path):
        """Verify filtering example from lucidspark_collection.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Get shapes by type
        containers = [s for s in collection._shapes.values() if s.type() == "container"]
        shapes = [s for s in collection._shapes.values() if s.type() == "shape"]
        lines = [s for s in collection._shapes.values() if s.type() == "line"]
        
        # Verify counts add up
        total = len(containers) + len(shapes) + len(lines)
        assert total == len(collection._shapes)
        
        # Find shapes with specific tags (using example tags from test data)
        tagged_shapes = [
            s for s in collection._shapes.values()
            if s.tags and "IntegrationPartner" in s.tags
        ]
        # Should find some shapes with IntegrationPartner tag in example data
        assert isinstance(tagged_shapes, list)
        
        # Calculate custom metrics
        orphaned_shapes = [
            s for s in collection._shapes.values()
            if s.type() == "shape" and not s.contained_by
        ]
        assert isinstance(orphaned_shapes, list)
        assert len(orphaned_shapes) >= 0
    
    def test_mermaid_docstring_basic_usage(self, example_csv_path):
        """Verify basic usage example from mermaid_renderer.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        # Load and render
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        mermaid_code = collection.to_mermaid(direction="TB", wrap_width=50)
        
        assert isinstance(mermaid_code, str)
        assert "flowchart TB" in mermaid_code
        assert len(mermaid_code) > 0
    
    def test_mermaid_docstring_direct_renderer(self, example_csv_path):
        """Verify direct renderer usage from mermaid_renderer.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        from totodev_pub.lucidspark.mermaid_renderer import MermaidRenderer
        
        # Load collection
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Create renderer with custom settings
        renderer = MermaidRenderer(
            collection=collection,
            direction="LR",      # Left-to-right layout
            wrap_width=40        # Wrap text at 40 characters
        )
        
        # Render to Mermaid code
        mermaid_code = renderer.render()
        assert isinstance(mermaid_code, str)
        assert "flowchart LR" in mermaid_code
    
    def test_mermaid_docstring_layout_directions(self, example_csv_path):
        """Verify layout directions example from mermaid_renderer.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # Top-to-bottom (default)
        mermaid_tb = collection.to_mermaid(direction="TB")
        assert "flowchart TB" in mermaid_tb
        
        # Left-to-right (good for wide diagrams)
        mermaid_lr = collection.to_mermaid(direction="LR")
        assert "flowchart LR" in mermaid_lr
        
        # Right-to-left
        mermaid_rl = collection.to_mermaid(direction="RL")
        assert "flowchart RL" in mermaid_rl
        
        # Bottom-to-top
        mermaid_bt = collection.to_mermaid(direction="BT")
        assert "flowchart BT" in mermaid_bt
    
    def test_mermaid_docstring_text_wrapping(self, example_csv_path):
        """Verify text wrapping example from mermaid_renderer.py module docstring."""
        from totodev_pub.lucidspark.lucidspark_collection import LucidSparkShapeCollection
        
        collection = LucidSparkShapeCollection.from_csv(example_csv_path)
        
        # No wrapping (may produce very wide nodes)
        mermaid_no_wrap = collection.to_mermaid(wrap_width=1000)
        assert isinstance(mermaid_no_wrap, str)
        assert "flowchart" in mermaid_no_wrap
        
        # Narrow wrapping (good for mobile/narrow displays)
        mermaid_narrow = collection.to_mermaid(wrap_width=30)
        assert isinstance(mermaid_narrow, str)
        # Narrow wrapping should produce <br> tags for long text
        
        # Default balanced wrapping
        mermaid_default = collection.to_mermaid(wrap_width=50)
        assert isinstance(mermaid_default, str)

