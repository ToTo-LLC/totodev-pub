# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Tests for mermaid_intake.convert_mermaid_text — the --convert mode's
best-effort transform of a (near-)Mermaid sketch into chain-DSL text."""


from totodev_pub.folder_backed_case_support.mermaid_intake import convert_mermaid_text
from totodev_pub.folder_backed_case_support.state_chain_parser import StateChainParser


def test_valid_dsl_passes_through_untouched_and_is_idempotent():
    decl = (
        "%% a comment\n"
        "[*] --> new -- go --> open == stop ==> done --> [*]\n"
        "open --> parked : shelve [ready]"
    )
    once, notes = convert_mermaid_text(decl)
    assert once == decl
    assert notes == []
    twice, notes2 = convert_mermaid_text(once)
    assert twice == once and notes2 == []


def test_unlabeled_edge_gets_todo_trigger():
    out, notes = convert_mermaid_text("review --> approved")
    assert out == "review --> approved : to_approved   %% TODO: name this trigger"
    spec = StateChainParser.parse(out)
    assert spec.transitions[0]["trigger"] == "to_approved"


def test_free_text_label_is_slugged_with_original_kept():
    out, _ = convert_mermaid_text("new --> review : Open Ticket!")
    assert out == 'new --> review : open_ticket   %% was "Open Ticket!"'
    assert StateChainParser.parse(out).transitions[0]["trigger"] == "open_ticket"


def test_labeled_boundary_hop_sheds_its_label_with_note():
    out, notes = convert_mermaid_text("approved --> [*] : done")
    assert out == "approved --> [*]"
    assert any("dropped label 'done'" in n for n in notes)


def test_state_alias_becomes_comment():
    out, notes = convert_mermaid_text('state "Fancy Review" as review')
    assert out == '%% state review was aliased as "Fancy Review"'
    assert any("alias" in n for n in notes)


def test_note_block_is_commented_out():
    out, _ = convert_mermaid_text(
        "note right of review\n  humans look here\nend note"
    )
    assert out.splitlines() == [
        "%% note right of review",
        "%% humans look here",
        "%% end note",
    ]


def test_choice_stereotype_and_composite_braces_flagged_as_todo():
    out, notes = convert_mermaid_text(
        "state fork1 <<choice>>\nstate outer {\n}"
    )
    lines = out.splitlines()
    assert all(ln.startswith("%% TODO: unsupported construct removed:") for ln in lines)
    assert len(notes) == 3


def test_flowchart_pipe_label_edge_converts():
    out, _ = convert_mermaid_text('a -->|"Send Reply"| b')
    # quotes are non-alphanumeric: slugged away
    assert StateChainParser.parse(out).transitions[0]["trigger"] == "send_reply"


def test_dotted_arrow_maps_to_auto_with_note():
    out, notes = convert_mermaid_text("a -.-> b")
    spec = StateChainParser.parse(out)
    assert ("a", "to_b") in spec.auto_edges
    assert any("dotted arrow" in n for n in notes)


def test_thick_arrow_survives_as_manual():
    out, _ = convert_mermaid_text("a ==> b : Approve It")
    spec = StateChainParser.parse(out)
    assert spec.transitions[0]["trigger"] == "approve_it"
    assert ("a", "approve_it") not in spec.auto_edges


def test_mermaid_boilerplate_dropped_and_decor_commented():
    out, _ = convert_mermaid_text(
        "stateDiagram-v2\ndirection LR\nclassDef hot fill:#f00\n[*] --> a\na --> [*]"
    )
    lines = out.splitlines()
    assert lines[0].startswith("%% classDef")
    assert lines[1:] == ["[*] --> a", "a --> [*]"]


def test_unrecognized_line_becomes_todo_comment():
    out, notes = convert_mermaid_text("this is not anything mermaid or dsl")
    assert out.startswith("%% TODO: unsupported line:")
    assert notes


def test_full_sketch_converts_to_parseable_validating_declaration():
    sketch = """
        stateDiagram-v2
        direction LR
        state "Fancy Review" as review
        [*] --> new
        new --> review : Open Ticket!
        review --> approved
        review --> rejected : Reject / notify customer
        approved --> [*] : done
        rejected --> [*]
    """
    out, _ = convert_mermaid_text(sketch)
    spec = StateChainParser.parse(out).validate().expand_wildcards().classify()
    assert spec.initial_state == "new"
    assert spec.terminal_states == {"approved", "rejected"}
    assert "open_ticket" in spec.triggers


def test_slug_edge_cases():
    from totodev_pub.folder_backed_case_support.mermaid_intake import _slug
    assert _slug("Open Ticket!") == "open_ticket"
    assert _slug("3rd pass") == "t_3rd_pass"
    assert _slug("???") == "step"
