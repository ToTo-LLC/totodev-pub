# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""Runnable entry point for the chain-DSL CLI:

    python -m totodev_pub.folder_backed_case_support.state_chain_cli [opts] [SOURCE]

Validate an ``fsm_state_chains`` declaration (default), or render it as a
Mermaid diagram (``--render``). SOURCE is a file path, ``-`` for stdin (the
default), or a literal chain string. Run with ``--help`` for the full surface.

This module exists ONLY as a launcher: the implementation is
``StateChainParser``'s companion ``main()`` in ``state_chain_parser`` (the
grammar authority). It cannot live there as a ``python -m`` target because the
package ``__init__`` re-exports the parser — runpy would then re-execute an
already-imported module (duplicate classes plus a RuntimeWarning). This shim is
imported by nothing, so it is always a clean ``-m`` target.
"""

from totodev_pub.folder_backed_case_support.state_chain_parser import main

if __name__ == "__main__":
    raise SystemExit(main())
