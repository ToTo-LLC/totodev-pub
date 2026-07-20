# CaseWorkbench tour template (plain Python — works in marimo, IPython, or
# `python -m asyncio`). Driving is async: remember to `await` advance/run/trigger.
#
#   python -m asyncio notebooks/case_workbench_template.py
#   # or paste cells into marimo / IPython

from __future__ import annotations

from totodev_pub.case_testing import CaseWorkbench


async def main() -> None:
    wb = CaseWorkbench.for_project()
    print(wb.help())
    print(wb.list_examples())
    # Clone a freeze-dried example when your shelf has one:
    #   wb.clone("YourCase/newly_created/minimal")
    # Or construct fresh (type or class-name string):
    #   from your_pkg import YourCase
    #   wb.create(YourCase)
    #   wb.create("YourCase", nickname="demo")
    #
    # if wb.case is not None:
    #     print(wb.status())
    #     print(wb.probe())
    #     await wb.advance()          # don't forget await
    #     await wb.run()
    #     wb.freeze_dry(nickname="group/sample", description="...")
    #     wb.cleanup()                # optional; scratch is durable by default

    print(
        "\nTip: Driving is async — use python -m asyncio, IPython, or marimo; "
        "or asyncio.run(wb.advance()) in a plain REPL."
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
