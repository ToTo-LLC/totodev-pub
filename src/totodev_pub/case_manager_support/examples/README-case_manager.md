# Running a CaseManager

Worked examples for the manager side of the library. Designing a case *type* is a
separate subject with its own tooling (the `case-designer` skill); these are
about taking types you already have and **running a fleet of them**.

For the operator-facing contract — exit codes, restart policy, watchdog
thresholds, container caveats — see [`docs/case-manager-deployment.md`](../../../../docs/case-manager-deployment.md).

## Three layers

Almost every question about hosting resolves to "which layer owns this":

| Layer | Owns | Where |
|---|---|---|
| **Host** | The process: signals, exit codes, the watchdog, **and shutdown** | `case_manager_host.serve()` |
| **Signaling adapter** | The file-drop transport: fire / adopt / reclassify, correlation ids, results, dead-lettering | `signaling_adapter.SignalingAdapter` |
| **`CaseManager`** | The fleet: storage, the pool, case lifecycle | `case_manager.CaseManager` |

The adapter is **optional**. A manager with none attached is driven entirely
through its own methods — an embedded host, a batch job, a test. That is a
supported shape, not a degraded one, and it is why the manager's public API has
to be complete on its own. Shutdown works either way, because the host owns it.

## The examples

### 1. Minimal host — `example_01_minimal_host.py`

The smallest complete process: open a root, name your case types, `serve()`.

```bash
uv run python -m totodev_pub.case_manager_support.examples.example_01_minimal_host /tmp/inquiries
```

Serves no external requests. Stop with Ctrl-C (exit 0); check it with
`totodev-manager-health /tmp/inquiries`.

### 2. Batch runner — `example_02_bag_runner.py`

The "I have a folder of cases; run them" shape. `load_case_bag()` provisions a
fresh root, copies the bag in, and adopts each case; `stop_when_empty=True` makes
the process exit 0 once the pool drains.

```bash
uv run python -m totodev_pub.case_manager_support.examples.example_02_bag_runner ./my_bag --seed
```

**The bag is copied, never consumed.** Adopt *moves* a case folder into managed
storage, and running a case mutates it — record, events, logs, lease. A loader
that adopted your folders directly would destroy the input on first use and make
a second run meaningless. Run it twice; you get the same answer twice.

### 3. Request-serving host — `example_03_request_serving_host.py`

Adds a `SignalingAdapter`, so other processes can submit work.

```bash
uv run python -m ...examples.example_03_request_serving_host serve  /tmp/fleet   # terminal 1
uv run python -m ...examples.example_03_request_serving_host submit /tmp/fleet   # terminal 2
```

The client half needs no manager and no adapter — it reads the manifest and
writes files.

## Testing a fleet

`make_case_bag_fixture()` builds a pytest fixture that loads bags and stops every
manager it created at teardown:

```python
from totodev_pub.case_manager_support.bag_loading import make_case_bag_fixture
from myapp.cases import InquiryCase

case_bag = make_case_bag_fixture(register_types=[InquiryCase])

async def test_the_batch_completes(case_bag):
    manager, report = await case_bag(FIXTURES / "inquiries")
    assert report.all_adopted
    await manager.start()
```

Each load gets its own root, so one test may load several bags without collision.

## Choosing a shape

| You want | Use |
|---|---|
| A long-running service other processes talk to | Example 3 |
| A job that processes a batch and exits | Example 2 |
| A fleet inside a larger program you already run | Example 1, no adapter |
| A test | `make_case_bag_fixture()` |
